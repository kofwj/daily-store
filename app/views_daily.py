"""今日填报、成交播报（填一单/删除/记录分页）。"""

from __future__ import annotations

from datetime import timedelta

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import broadcast, db, deal
from .helpers import (
    admin_required,
    broadcast_compact_sections,
    broadcast_store_name,
    login_required,
    pagination,
    parse_date,
    pick_store,
    request_scope,
    store_forecast,
    values_for_broadcast,
    with_close_rate,
    xlsx_bytes,
    xlsx_response,
)
from .metrics_seed import KPI_TARGETS, SECTIONS, rollup_pair


def register_daily(app) -> None:
    @app.route("/today", methods=["GET", "POST"])
    @login_required
    def today():
        with db.get_db() as conn:
            store, stores = pick_store(conn, request.values.get("store_id"))
            if store is None:
                flash("还没有可填的门店，请管理员先建店", "error")
                return render_template("empty.html")
            biz_date = parse_date(request.values.get("date"))
            today = db.today_local()
            metrics = db.list_metrics(conn)
            if request.method == "POST":
                if g.user["role"] == "readonly":
                    flash("只读账号不能填日报", "error")
                    return redirect(url_for("report", store_id=store["id"]))
                filler_month = db.get_setting(conn, "filler_edit_month", "0") == "1"
                same_month = biz_date.year == today.year and biz_date.month == today.month
                if g.user["role"] != "admin":
                    if biz_date > today:
                        flash("非管理员不能填写未来日期。", "error")
                        return redirect(url_for("today", store_id=store["id"], date=biz_date.isoformat()))
                    if biz_date != today and not (filler_month and same_month):
                        flash("只能改当天（管理员开启『本月可改』后可补录本月）。历史跨月需找管理员修改。", "error")
                        return redirect(url_for("today", store_id=store["id"]))
                    # 当天锁定始终生效：本月可改只解锁本月的“过去日”，不放开“今天”
                    if db.is_locked(biz_date):
                        flash(
                            f"当天数据已锁定（{db.LOCK_HOUR:02d}:{db.LOCK_MINUTE:02d} 后不可改），找管理员解锁修改。",
                            "error",
                        )
                        return redirect(url_for("today", store_id=store["id"], date=biz_date.isoformat()))
                existing = db.get_report(conn, store["id"], biz_date)
                if existing and existing["submitted_by"] and existing["submitted_by"] != g.user["id"] and g.user["role"] != "admin":
                    flash("该日已有其他人提交，覆盖前请与对方确认。", "error")
                    return redirect(url_for("today", store_id=store["id"], date=biz_date.isoformat()))
                values = {}
                for m in metrics:
                    raw = request.form.get(f"m_{m['code']}", "0")
                    try:
                        values[m["code"]] = max(0, int(raw or 0))
                    except ValueError:
                        values[m["code"]] = 0
                compact_sections = broadcast_compact_sections(conn)
                note = (request.form.get("note") or "").strip()
                before = db.day_values(conn, store["id"], biz_date)
                db.save_daily(
                    conn,
                    store_id=store["id"],
                    biz_date=biz_date,
                    values=values,
                    user_id=g.user["id"],
                    compact=bool(compact_sections),
                    note=note,
                )
                if existing:
                    db.record_edit(
                        conn,
                        biz_date=biz_date,
                        store_id=store["id"],
                        user_id=g.user["id"],
                        before=before,
                        after=values,
                        note="覆盖保存",
                    )
                flash("已保存，累计已按本月重算。点「复制全文」贴进微信群。", "ok")
                return redirect(
                    url_for(
                        "today",
                        store_id=store["id"],
                        date=biz_date.isoformat(),
                        copied=1,
                    )
                    + "#broadcast"
                )

            day_vals = db.day_values(conn, store["id"], biz_date)
            pairs = values_for_broadcast(conn, store["id"], biz_date)
            kpi_targets = db.list_kpi_targets(conn)
            filler_month = db.get_setting(conn, "filler_edit_month", "0") == "1"
            report = db.get_report(conn, store["id"], biz_date)
            compact_sections = broadcast_compact_sections(conn)
            text = broadcast.render_broadcast(
                broadcast_store_name(store),
                biz_date,
                pairs,
                compact=bool(compact_sections),
                compact_sections=compact_sections,
            )
            grouped = []
            for section in SECTIONS:
                items = []
                for code, name, hint in section["metrics"]:
                    day, cum = pairs.get(code, (0, 0))
                    items.append(
                        {
                            "code": code,
                            "name": name,
                            "hint": hint,
                            "day": day_vals.get(code, 0),
                            "cum": cum,
                            "target": 0,
                        }
                    )
                grouped.append({**section, "rows": items})
            kpi_cards = []
            for code, name, note in KPI_TARGETS:
                if code == "ai_contract":
                    day, cum = pairs.get("ai_contract", (0, 0))
                else:
                    day, cum = rollup_pair(pairs, code)
                target = kpi_targets.get(code, 0)
                kpi_cards.append(
                    {
                        "code": code,
                        "name": name,
                        "note": note,
                        "day": day,
                        "cum": cum,
                        "target": target,
                        "progress": (cum / target * 100) if target else None,
                    }
                )
            forecast = store_forecast(conn, store, biz_date)
            return render_template(
                "today.html",
                store=store,
                stores=stores,
                biz_date=biz_date,
                is_today=biz_date == today,
                filler_month=filler_month,
                can_edit=(
                    g.user["role"] != "readonly"
                    and (
                        g.user["role"] == "admin"
                        or biz_date == today
                        or (filler_month and biz_date.year == today.year and biz_date.month == today.month)
                    )
                ),
                grouped=grouped,
                report=report,
                broadcast_text=text,
                compact=bool(compact_sections),
                kpi_cards=kpi_cards,
                forecast=forecast,
                is_admin=g.user["role"] == "admin",
            )

    @app.route("/deal", methods=["GET", "POST"])
    @login_required
    def deal_page():
        with db.get_db() as conn:
            store, stores = pick_store(conn, request.values.get("store_id"))
            if store is None:
                return render_template("empty.html")
            values = deal.form_values(
                request.form if request.method == "POST" else None,
                posted=request.method == "POST",
            )
            text = ""
            today_d = db.today_local()
            editable = True
            if request.method == "GET":
                raw_deal_id = request.args.get("deal_id") or ""
                try:
                    edit_id = int(raw_deal_id) if raw_deal_id else None
                except ValueError:
                    edit_id = None
                if edit_id:
                    row = db.get_deal_post(conn, edit_id, store["id"])
                    if row:
                        values = deal.form_from_row(row)
                        text = row["text"] or deal.render_deal(
                            broadcast_store_name(store),
                            **{k: v for k, v in values.items() if k != "deal_id"},
                        )
                        editable = deal.is_today_deal(row, today_d)
                if not values["opener"]:
                    values["opener"] = (g.user["display_name"] or "").strip()
            if request.method == "POST":
                if g.user["role"] == "readonly":
                    flash("只读账号不能填触客播报", "error")
                    return redirect(url_for("deal_records", store_id=store["id"]))
                deal_id = request.form.get("deal_id") or values.get("deal_id") or ""
                try:
                    deal_id_int = int(deal_id) if deal_id else None
                except ValueError:
                    deal_id_int = None
                existing = (
                    db.get_deal_post(conn, deal_id_int, store["id"]) if deal_id_int else None
                )
                if existing is not None and not deal.is_today_deal(existing, today_d):
                    flash("往日触客只能查看，不能改。", "error")
                    return redirect(
                        url_for("deal_page", store_id=store["id"], deal_id=deal_id_int)
                    )
                text = deal.render_deal(
                    broadcast_store_name(store),
                    **{k: v for k, v in values.items() if k != "deal_id"},
                )
                saved_id = db.record_deal_post(
                    conn,
                    store_id=store["id"],
                    user_id=g.user["id"],
                    closed=deal.yn(values["closed"], "1", "0") == "1",
                    model=values["model"],
                    phone=values["phone"],
                    spend=values["spend"],
                    hall_query=deal.yn(values["hall_query"], "1", "0") == "1",
                    recommend=values["recommend"],
                    student=deal.yn(values["student"], "1", "0") == "1",
                    opener=values["opener"],
                    note=values["note"],
                    text=text,
                    deal_id=deal_id_int,
                    biz_date=today_d,
                )
                values["deal_id"] = saved_id
                editable = True
            month_start = today_d.replace(day=1)
            store_ids = [s["id"] for s in stores]
            today_counts = db.deal_counts(conn, store_ids, today_d, today_d)
            month_counts = db.deal_counts(conn, store_ids, month_start, today_d)
            mine_today = with_close_rate(today_counts.get(store["id"], {"total": 0, "closed": 0}))
            mine_month = with_close_rate(month_counts.get(store["id"], {"total": 0, "closed": 0}))
            return render_template(
                "deal.html",
                store=store,
                stores=stores,
                form=values,
                deal_text=text,
                is_admin=g.user["role"] == "admin",
                mine_today=mine_today,
                mine_month=mine_month,
                editable=editable,
            )

    @app.route("/deal/delete", methods=["POST"])
    @admin_required
    def deal_delete():
        store_id = request.form.get("store_id") or ""
        deal_id = request.form.get("deal_id") or ""
        try:
            sid = int(store_id)
            did = int(deal_id)
        except (TypeError, ValueError):
            return Response("bad request", status=400)
        with db.get_db() as conn:
            if not db.user_can_access_store(conn, g.user, sid):
                return Response("forbidden", status=403)
            row = db.get_deal_post(conn, did, sid)
            if row is None:
                flash("这条记录不存在，或已删除。", "error")
            elif not deal.is_today_deal(row, db.today_local()):
                flash("往日触客只能查看，不能删。", "error")
            elif db.delete_deal_post(conn, did, sid, user_id=g.user["id"]):
                flash("已删除该触客记录。", "ok")
            else:
                flash("这条记录不存在，或已删除。", "error")
        return redirect(url_for("deal_records", store_id=sid))

    @app.route("/deal/records")
    @login_required
    def deal_records():
        """成交播报记录页。管理员默认定看今天全店；店员仍看本店近几天。"""
        with db.get_db() as conn:
            is_admin = g.user["role"] == "admin"
            raw_sid = (request.args.get("store_id") or "").strip()
            all_stores = is_admin and raw_sid in ("", "all")
            store, stores = pick_store(conn, None if all_stores else raw_sid)
            if store is None:
                return render_template("empty.html")
            scope = request_scope(stores)
            today_d = db.today_local()
            default_days = "1" if all_stores else "7"
            try:
                days_int = max(1, min(int(request.args.get("days", default_days)), 90))
            except ValueError:
                days_int = 1 if all_stores else 7
            start = today_d - timedelta(days=days_int - 1)
            sid = None if all_stores else store["id"]
            scoped_ids = None if (not all_stores or not scope["active"]) else scope["ids"]
            total = db.count_deal_posts(conn, sid, start, today_d, store_ids=scoped_ids)
            page, pages = pagination(request.args.get("page"), total)
            rows = db.list_deal_posts(
                conn, sid, start, today_d, limit=50, offset=(page - 1) * 50, store_ids=scoped_ids
            )
            month_start = today_d.replace(day=1)
            kpi_ids = [store["id"]] if not all_stores else (scope["ids"] if scope["active"] else [s["id"] for s in stores])
            today_counts = db.deal_counts(conn, kpi_ids, today_d, today_d)
            month_counts = db.deal_counts(conn, kpi_ids, month_start, today_d)
            if all_stores:
                mine_today = with_close_rate({
                    "total": sum(v["total"] for v in today_counts.values()),
                    "closed": sum(v["closed"] for v in today_counts.values()),
                })
                mine_month = with_close_rate({
                    "total": sum(v["total"] for v in month_counts.values()),
                    "closed": sum(v["closed"] for v in month_counts.values()),
                })
            else:
                mine_today = with_close_rate(today_counts.get(store["id"], {"total": 0, "closed": 0}))
                mine_month = with_close_rate(month_counts.get(store["id"], {"total": 0, "closed": 0}))
            return render_template(
                "deal_records.html",
                store=store,
                stores=stores,
                all_stores=all_stores,
                is_admin=is_admin,
                rows=rows,
                days=days_int,
                page=page,
                pages=pages,
                total=total,
                start=start,
                end=today_d,
                today=today_d.isoformat(),
                mine_today=mine_today,
                mine_month=mine_month,
                scope=scope,
            )

    @app.route("/deal/export")
    @admin_required
    def deal_export():
        """管理员导出全部门店指定区间内的成交记录。"""
        today_d = db.today_local()
        try:
            days_int = max(1, min(int(request.args.get("days", "7")), 90))
        except ValueError:
            days_int = 7
        start = today_d - timedelta(days=days_int - 1)
        with db.get_db() as conn:
            rows = db.list_all_deal_posts(conn, start, today_d)
        header = ["日期", "门店", "机型", "号码", "消费", "推荐套餐", "开口/导购", "结果", "备注"]
        data_rows = [
            [
                (r["biz_date"] or ""),
                r["store_short"] or r["store_name"] or "",
                r["model"] or "",
                r["phone"] or "",
                r["spend"] or "",
                r["recommend"] or "",
                r["opener"] or r["submitter_name"] or "",
                "成交" if r["closed"] else "未成交",
                r["note"] or "",
            ]
            for r in rows
        ]
        filename = f"deal_export_{start.isoformat()}_{today_d.isoformat()}.xlsx"
        return xlsx_response(
            xlsx_bytes(header, data_rows, sheet="触客记录"),
            filename,
        )

