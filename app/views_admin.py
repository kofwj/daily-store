"""管理员专属：通报表、CSV、月度考核、修改审计。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Dict

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import bulletin, db, invoice, settlement
from .helpers import (
    accessible_stores,
    admin_required,
    advisor_coeffs,
    advisor_edit_column,
    advisor_month_rows,
    advisor_penalty_divisor,
    advisor_score_deadline,
    advisor_score_month,
    advisor_score_open,
    build_diff,
    deal_diff,
    incentive_rules,
    login_required,
    named_advisor,
    pagination,
    parse_date,
    parse_days,
    parse_int,
    readonly_required,
    request_scope,
    review_preset_key,
    review_template_setting,
    store_forecasts,
    values_for_broadcast,
    xlsx_bytes,
    xlsx_response,
)
from .helpers import (
    month_end as _month_end,
)
from .metrics_seed import (
    format_stored,
    rollup_pair,
    to_stored,
)


def _default_bisuan_mobile_asof(biz_date: date) -> date:
    """移动取数默认截止到通报表日前一天（当天通常还没数）。"""
    month_start = biz_date.replace(day=1)
    prev = biz_date - timedelta(days=1)
    return prev if prev >= month_start else month_start


def _clamp_bisuan_mobile_asof(asof: date, biz_date: date) -> date:
    month_start = biz_date.replace(day=1)
    if asof < month_start:
        return month_start
    if asof > biz_date:
        return biz_date
    return asof


def _mobile_asof(raw: str, biz_date: date) -> date:
    """移动取数截止日：raw 是 'YYYY-MM-DD…'，非法/为空回落默认（通报表日前一天）。"""
    if raw:
        try:
            return _clamp_bisuan_mobile_asof(date.fromisoformat(raw[:10]), biz_date)
        except ValueError:
            pass
    return _default_bisuan_mobile_asof(biz_date)


def _bulletin_rows(conn, stores, biz_date: date):
    month_key = biz_date.strftime("%Y-%m")
    month_start = biz_date.replace(day=1)
    kpi_targets = db.list_kpi_targets(conn)
    mobile_map = db.bisuan_mobile_map(conn, month_key)
    asof_map = db.bisuan_mobile_asof_map(conn, month_key)
    rows = []
    for store in stores:
        if not (store["mobile_code"] or "").strip():
            # 没配移动编码的店不进通报表
            continue
        pairs = values_for_broadcast(conn, store["id"], biz_date)
        day_ai, month_ai = pairs.get("ai_contract", (0, 0))
        day_welcome, month_welcome = pairs.get("welcome_back", (0, 0))
        day_bisuan = bulletin.bisuan_total(
            {"bisuan": pairs.get("bisuan", (0, 0))[0], "bisuan_high": pairs.get("bisuan_high", (0, 0))[0]}
        )
        month_bisuan = bulletin.bisuan_total(
            {"bisuan": pairs.get("bisuan", (0, 0))[1], "bisuan_high": pairs.get("bisuan_high", (0, 0))[1]}
        )
        day_coin, month_coin = rollup_pair(pairs, "coin_cut", biz_date)
        report = db.get_report(conn, store["id"], biz_date)
        mobile = mobile_map.get(int(store["id"]))
        # 截止日按店取，没存就用默认（通报表日前一天）
        raw_asof = (asof_map.get(int(store["id"])) or "").strip()
        store_asof = _mobile_asof(raw_asof, biz_date)
        sys_asof = None
        if mobile is not None:
            # 对照用截止日同期上报数，不是通报表选中日的整月
            sys_asof = db.week_metric_total(
                conn, store["id"], month_start, store_asof, ("bisuan", "bisuan_high")
            )
        rows.append(
            bulletin.build_row(
                store,
                day_ai=day_ai,
                month_ai=month_ai,
                day_welcome=day_welcome,
                month_welcome=month_welcome,
                day_bisuan=day_bisuan,
                month_bisuan=month_bisuan,
                day_coin=day_coin,
                month_coin=month_coin,
                submitted=report is not None,
                bisuan_target=int(kpi_targets.get("bisuan_total", 0) or 0),
                ai_target=int(kpi_targets.get("ai_contract", 0) or 0),
                coin_target=int(kpi_targets.get("coin_cut", 0) or 0),
                month_bisuan_mobile=mobile,
                month_bisuan_asof=store_asof.isoformat() if mobile is not None else "",
                month_bisuan_sys_asof=sys_asof,
            )
        )
    rows = bulletin.apply_scales(rows)
    # 通报表日期晚于取数截止日：移取未更新到今日
    for row in rows:
        if row.get("_month_bisuan_mobile_stored") is None:
            continue
        asof_raw = (row.get("month_bisuan_asof") or "").strip()
        if not asof_raw:
            continue
        try:
            if date.fromisoformat(asof_raw[:10]) < biz_date:
                row["month_bisuan_mobile_stale"] = True
        except ValueError:
            pass
    return rows


def _audit_diff(r, before, after, names) -> str:
    """把审计行的 before/after 翻译成一句话 diff（按 kind 分派）。"""
    kind = r["kind"]
    action = r.get("action")
    if kind == "deal":
        prefix = {"create": "新增触客：", "delete": "删除触客："}.get(action, "覆盖：")
        return prefix + deal_diff(before, after)
    if kind == "advance":
        head = {
            "create": "新增垫资：", "update": "覆盖垫资：", "delete": "删除垫资：",
            "pay": "兑付：", "unpay": "取消兑付：",
        }.get(action, "垫资：")
        return head + json.dumps({"before": before, "after": after}, ensure_ascii=False, default=str)
    if kind == "invoice":
        head = {"create": "新增开票：", "update": "改开票：", "delete": "删除开票："}.get(action, "开票：")
        return head + db.invoice_diff(before, after)
    if kind == "mobile":
        from .metrics_seed import format_stored as _fs

        b = before.get("value_tenths")
        a = after.get("value_tenths")
        head = "新录移动：" if action == "create" else "改移动："
        if b is None:
            body = f"{_fs('bisuan', a or 0)}"
        else:
            body = f"{_fs('bisuan', b or 0)}→{_fs('bisuan', a or 0)}"
        asof_txt = (after.get("asof") or "")[:10]
        if asof_txt:
            body += f"（至{asof_txt}）"
        return head + body
    return build_diff(before, after, names)


def register_admin(app) -> None:
    @app.route("/bulletin")
    @readonly_required
    def bulletin_page():
        biz_date = parse_date(request.args.get("date"))
        city = (request.args.get("city") or "").strip()
        area = (request.args.get("area") or "").strip()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            # 通报表只看有移动编码的店；地市下拉也按这个口径，避免空店把泰州带出来
            stores = [s for s in stores if (s["mobile_code"] or "").strip()]
            cities = sorted({(s["city"] or "南通市") for s in stores})
            if city and city not in cities:
                city = ""
            if not city:
                city = "南通市" if "南通市" in cities else (cities[0] if cities else "")
            areas = sorted(
                {(s["area_manager"] or "").strip() for s in stores if (s["area_manager"] or "").strip()}
            )
            if area and area not in areas:
                area = ""
            if area:
                stores = [s for s in stores if (s["area_manager"] or "").strip() == area]
            stores = [s for s in stores if (s["city"] or "南通市") == city] if city else stores
            kpi_targets = db.list_kpi_targets(conn)
            rows = _bulletin_rows(conn, stores, biz_date)
            copy_text = bulletin.tsv(rows, biz_date, kpi_targets)
            title_city = city.replace("市", "") if city else ""
            sid_list = [r["store_id"] for r in rows] if rows else []
            month_start = biz_date.replace(day=1)
            day_deal = db.deal_counts(conn, sid_list, biz_date, biz_date)
            month_deal = db.deal_counts(conn, sid_list, month_start, biz_date)
            day_deal_sum = (sum(d["total"] for d in day_deal.values()), sum(d["closed"] for d in day_deal.values()))
            month_deal_sum = (sum(d["total"] for d in month_deal.values()), sum(d["closed"] for d in month_deal.values()))
            saved_template = review_template_setting(conn)
            saved_preset = review_preset_key(conn)
            pick = (request.args.get("preset") or "").strip()
            active_preset = pick or saved_preset or ("custom" if saved_template else "standard")
            if pick == "custom":
                template = saved_template
            elif pick:
                found = bulletin.preset_by_key(pick)
                template = found["body"] if found else saved_template
            elif saved_template:
                template = saved_template
                active_preset = "custom"
            elif saved_preset:
                found = bulletin.preset_by_key(saved_preset)
                template = found["body"] if found else ""
            else:
                template = ""
            review = (
                bulletin.summary(
                    rows,
                    biz_date,
                    title_city,
                    day_deal=day_deal_sum,
                    month_deal=month_deal_sum,
                    template=template,
                )
                if rows
                else ""
            )
            month_key = biz_date.strftime("%Y-%m")
            # 表单默认：取已录各店里最新的截止日（截止日现在按店存），都没录则前一天
            asof_values = [
                v for v in db.bisuan_mobile_asof_map(conn, month_key).values() if (v or "").strip()
            ]
            mobile_asof = _mobile_asof(max(asof_values) if asof_values else "", biz_date)
            has_mobile = any(r.get("_month_bisuan_mobile_stored") is not None for r in rows)
            mobile_asof_head = (
                f"移动数据更新至{mobile_asof.month}/{mobile_asof.day}" if has_mobile else ""
            )
            return render_template(
                "bulletin.html",
                biz_date=biz_date,
                rows=rows,
                review=review,
                totals=bulletin.totals_row(rows, kpi_targets) if rows else None,
                month_label=bulletin.month_label(biz_date),
                day_label=bulletin.day_label(biz_date),
                copy_text=copy_text,
                city=city,
                cities=cities,
                area=area,
                areas=areas,
                prev_day=(biz_date - timedelta(days=1)).isoformat(),
                next_day=(biz_date + timedelta(days=1)).isoformat(),
                is_admin=g.user["role"] == "admin",
                mobile_asof=mobile_asof,
                mobile_asof_head=mobile_asof_head,
                review_presets=bulletin.REVIEW_PRESETS,
                review_preset=active_preset,
                has_custom_review=bool(saved_template),
                bulletin_title=f"{title_city}vivo零售运营中心移动业务通报表" if title_city else "移动业务通报表",
                kpi_heads=bulletin.kpi_target_heads(kpi_targets),
            )

    @app.route("/bulletin/review-preset", methods=["POST"])
    @admin_required
    def bulletin_review_preset():
        pick = (request.form.get("preset") or "").strip()
        biz_raw = request.form.get("date") or ""
        city = (request.form.get("city") or "").strip()
        with db.get_db() as conn:
            if pick == "custom":
                db.set_setting(conn, "review_preset", "custom")
            elif pick == "standard" or pick == "":
                db.set_setting(conn, "review_preset", "")
                db.set_setting(conn, "review_template", "")
            else:
                found = bulletin.preset_by_key(pick)
                if not found:
                    flash("没有这套复盘模板", "error")
                    return redirect(url_for("bulletin_page", date=biz_raw or None, city=city or None))
                db.set_setting(conn, "review_preset", pick)
                db.set_setting(conn, "review_template", found["body"])
            flash("复盘模板已切换", "ok")
        return redirect(url_for("bulletin_page", date=biz_raw or None, city=city or None, preset=pick or None))

    @app.route("/bulletin/bisuan-mobile", methods=["POST"])
    @admin_required
    def bulletin_bisuan_mobile():
        """通报表录移动取数：只存移动侧数字，用来和填报笔算对照，不改 daily_facts。"""
        store_id = request.form.get("store_id") or ""
        biz_raw = request.form.get("date") or ""
        asof_raw = request.form.get("asof") or ""
        mobile_raw = request.form.get("mobile") or request.form.get("official") or ""
        city = (request.form.get("city") or "").strip()
        area = (request.form.get("area") or "").strip()
        try:
            sid = int(store_id)
            biz_date = date.fromisoformat(biz_raw)
            asof = (
                date.fromisoformat(asof_raw)
                if asof_raw
                else _default_bisuan_mobile_asof(biz_date)
            )
            mobile = max(0, to_stored("bisuan", mobile_raw))
        except (ValueError, TypeError):
            flash("移动取数参数不对", "error")
            return redirect(url_for("bulletin_page", date=biz_raw or None, city=city or None, area=area or None))
        if mobile_raw.strip() == "":
            flash("填移动取数", "error")
            return redirect(url_for("bulletin_page", date=biz_date.isoformat(), city=city or None, area=area or None))
        month_start = biz_date.replace(day=1)
        asof = _clamp_bisuan_mobile_asof(asof, biz_date)
        with db.get_db() as conn:
            if not db.user_can_access_store(conn, g.user, sid):
                return Response("forbidden", status=403)
            # 对照截止日同期上报数（笔算新增 + 高）；只算差额展示，不写回填报
            current = db.week_metric_total(conn, sid, month_start, asof, ("bisuan", "bisuan_high"))
            delta = mobile - current
            month_key = biz_date.strftime("%Y-%m")
            db.save_bisuan_mobile(
                conn,
                store_id=sid,
                month=month_key,
                value_tenths=mobile,
                asof=asof,
                user_id=g.user["id"],
                note=f"上报同期 {format_stored('bisuan', current)}",
            )
            sign = "+" if delta > 0 else ""
            flash(
                f"已录移 {format_stored('bisuan', mobile)}（至{asof.month}/{asof.day}），"
                f"上报同期 {format_stored('bisuan', current)}，差额 {sign}{format_stored('bisuan', delta)}（填报未改）",
                "ok",
            )
        return redirect(url_for("bulletin_page", date=biz_date.isoformat(), city=city, area=area or None))

    @app.route("/incentive")
    @admin_required
    def incentive_page():
        month, as_of = _incentive_month()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            rows = []
            totals = {
                "store_reward": 0,
                "store_penalty": 0,
                "advisor_penalty": 0,
                "net": 0,
                "passed": 0,
                "reported": 0,
            }
            rules = incentive_rules(conn)
            judged_map = store_forecasts(conn, stores, as_of, rules)
            for store in stores:
                judged = judged_map[store["id"]]
                rows.append(judged)
                totals["store_reward"] += judged["store_reward"]
                totals["store_penalty"] += judged["store_penalty"]
                totals["advisor_penalty"] += judged["advisor_penalty"]
                totals["net"] += judged["net"]
                totals["passed"] += 1 if judged["passed"] else 0
                totals["reported"] += 1 if judged.get("reported") else 0
            # 结算底稿预览数据（按区域经理分组）
            settle_rows = settlement.build_settlement_rows(conn, stores, as_of)
            settle_groups = settlement.group_rows(settle_rows)
            invoice_month = settlement.prev_month_start(month)
            return render_template(
                "incentive.html",
                month=month,
                as_of=as_of,
                invoice_month=invoice_month,
                rows=rows,
                totals=totals,
                settle_groups=settle_groups,
            )

    @app.route("/incentive.xlsx")
    @admin_required
    def incentive_xlsx():
        """导出当月运营商结算底稿：系统填 AI/直降/奖惩，开票/房补/垫资/搭载率留空。"""
        month, as_of = _incentive_month()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            data = settlement.build_settlement_xlsx(conn, stores, as_of)
        filename = f"settlement_{month.strftime('%Y_%m')}.xlsx"
        return xlsx_response(data, filename)

    def _incentive_month(default=None):
        today_d = db.today_local()
        fallback = default or today_d.replace(day=1)
        month = parse_date(request.values.get("month"), fallback).replace(day=1)
        return month, min(_month_end(month), today_d)

    @app.route("/advisors", methods=["GET", "POST"])
    @login_required
    def advisors_page():
        """运营商顾问月度打分：主推奖惩自动带出，店长/区域经理/地市负责人分列打分。"""
        today_d = db.today_local()
        month, as_of = _incentive_month(advisor_score_month(today_d))
        month_text = month.strftime("%Y-%m")
        edit_col = advisor_edit_column(g.user)
        window_open = advisor_score_open(today_d, month)
        deadline = advisor_score_deadline(month)
        can_edit = bool(edit_col) and (window_open or g.user["role"] == "admin")
        with db.get_db() as conn:
            me = named_advisor(conn, g.user)
            if request.method == "POST":
                if not edit_col:
                    flash("没有打分权限", "error")
                elif not can_edit:
                    flash(f"打分窗口已过（每月 1–5 日评上个月，截止 {deadline.isoformat()}）", "error")
                else:
                    try:
                        _save_advisor_scores(conn, month_text, edit_col)
                        flash("顾问打分已保存", "ok")
                    except ValueError as exc:
                        flash(str(exc), "error")
                return redirect(url_for("advisors_page", month=month_text))
            stores = accessible_stores(conn)
            rows = _advisor_table(conn, stores, as_of)
            if me and not edit_col:
                rows = [r for r in rows if r["advisor_name"] == me]
                if not rows:
                    all_stores = db.list_all_stores(conn)
                    rows = [r for r in _advisor_table(conn, all_stores, as_of) if r["advisor_name"] == me]
            return render_template(
                "advisors.html",
                month=month,
                as_of=as_of,
                rows=rows,
                edit_col=edit_col if can_edit else "",
                divisor=advisor_penalty_divisor(conn),
                is_admin=g.user["role"] == "admin",
                self_advisor=me,
                window_open=window_open,
                deadline=deadline,
                today=today_d,
            )

    @app.route("/advisors.xlsx")
    @admin_required
    def advisors_xlsx():
        month, as_of = _incentive_month()
        month_text = month.strftime("%Y-%m")
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            rows = _advisor_table(conn, stores, as_of)
            data = _build_advisor_xlsx(rows, month)
        filename = f"advisor_coeff_{month_text}.xlsx"
        return xlsx_response(data, filename)

    def _advisor_table(conn, stores, as_of):
        """聚合当月考核 + 已存打分，算出评分系数与最终系数。"""
        month_text = as_of.strftime("%Y-%m")
        saved = {r["advisor_name"]: r for r in db.list_advisor_scores(conn, month_text)}
        table = []
        for item in advisor_month_rows(conn, stores, as_of):
            rec = saved.get(item["advisor_name"])
            base = float(rec["base_coeff"]) if rec and rec["base_coeff"] else 1.2
            sm = rec["score_manager"] if rec else None
            sa = rec["score_area"] if rec else None
            sc = rec["score_city"] if rec else None
            coeff = advisor_coeffs(base, item["fold_coeff"], [sm, sa, sc])
            table.append(
                {
                    **item,
                    "work_type": ((rec["work_type"] if rec else "") or "正式"),
                    "base_coeff": base,
                    "score_manager": sm,
                    "score_area": sa,
                    "score_city": sc,
                    "note": (rec["note"] if rec else "") or "",
                    "rate": coeff["rate"],
                    "final": coeff["final"],
                    "updated_at": (rec["updated_at"] if rec else "") or "",
                }
            )
        return table

    def _parse_score(raw, name):
        text = (raw or "").strip()
        if not text:
            return None
        try:
            value = int(float(text))
        except ValueError as exc:
            raise ValueError(f"{name} 的打分要填 0-10 的整数") from exc
        if not 0 <= value <= 10:
            raise ValueError(f"{name} 的打分要填 0-10 的整数")
        return value

    def _parse_coeff(raw, name):
        text = (raw or "").strip()
        if not text:
            return 1.2
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"{name} 的基础系数要填数字") from exc
        if not 0 < value <= 5:
            raise ValueError(f"{name} 的基础系数要在 0-5 之间")
        return value

    def _save_advisor_scores(conn, month_text, edit_col):
        uid = int(g.user["id"])
        allowed = {
            ((s["advisor_name"] if "advisor_name" in s.keys() else "") or "").strip()
            for s in accessible_stores(conn)
        }
        allowed.discard("")
        fields = {"score_manager": "sm", "score_area": "sa", "score_city": "sc"}
        cols = list(fields) if edit_col == "all" else [edit_col]
        idx = 0
        count = 0
        while True:
            name = (request.form.get(f"advisor_{idx}") or "").strip()
            if not name:
                break
            if name not in allowed:
                raise ValueError("只能给自己可见范围内的顾问打分")
            updates = {}
            if edit_col == "all":
                wt = request.form.get(f"wt_{idx}")
                if wt is not None and wt.strip():
                    updates["work_type"] = wt.strip()[:20]
                updates["base_coeff"] = _parse_coeff(request.form.get(f"bc_{idx}"), name)
                note = request.form.get(f"note_{idx}")
                if note is not None:
                    updates["note"] = note.strip()[:200]
            for col in cols:
                updates[col] = _parse_score(request.form.get(f"{fields[col]}_{idx}"), name)
            db.upsert_advisor_score(conn, month_text, name, updates, uid)
            idx += 1
            count += 1
        if not count:
            raise ValueError("没有可保存的顾问")

    def _build_advisor_xlsx(rows, month):
        """与线下「运营商顾问工资系数」表同构的导出。"""
        header = [
            "姓名", "工作性质", "基础系数", "主推奖惩", "奖惩折算系数",
            "店长打分", "区域经理打分", "地市负责人打分", "评分系数", "最终系数",
        ]
        data = [
            [
                r["advisor_name"],
                r["work_type"],
                r["base_coeff"],
                r["penalty_total"],
                r["fold_coeff"],
                r["score_manager"] if r["score_manager"] is not None else "",
                r["score_area"] if r["score_area"] is not None else "",
                r["score_city"] if r["score_city"] is not None else "",
                r["rate"] if r["rate"] is not None else "",
                r["final"] if r["final"] is not None else "",
            ]
            for r in rows
        ]
        return xlsx_bytes(header, data, sheet=month.strftime("%Y-%m"))

    @app.route("/incentive/invoice", methods=["GET", "POST"])
    @admin_required
    def invoice_page():
        month, as_of = _incentive_month()
        month_text = month.strftime("%Y-%m")
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            by_id = {int(s["id"]): s for s in stores}
            sid = parse_int(request.values.get("store_id"), 0)
            store = by_id.get(sid) or (stores[0] if stores else None)
            if store is None:
                flash("没有可开票的门店", "error")
                return redirect(url_for("incentive_page", month=month_text))
            if request.method == "POST":
                uid = int(g.user["id"])
                if (request.form.get("action") or "save") == "delete":
                    db.delete_invoice_month(conn, int(store["id"]), month_text, user_id=uid)
                    flash("开票申请已删除", "ok")
                else:
                    db.save_invoice_from_form(
                        conn, int(store["id"]), month_text, request.form, user_id=uid
                    )
                    flash("开票申请已保存", "ok")
                return redirect(
                    url_for("invoice_page", month=month_text, store_id=store["id"])
                )
            rec = db.get_invoice_month(conn, int(store["id"]), month_text)
            return render_template(
                "invoice.html",
                month=month,
                as_of=as_of,
                store=store,
                stores=stores,
                rec=rec,
                party=invoice.invoice_parties(conn)[invoice.party_key_for_store(store)],
                handler=invoice.invoice_handler(conn),
                invoice_name=invoice.invoice_store_name(store),
            )

    @app.route("/incentive/invoice.xlsx")
    @admin_required
    def invoice_xlsx():
        month, as_of = _incentive_month()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            by_id = {int(s["id"]): s for s in stores}
            sid = parse_int(request.args.get("store_id"), 0)
            store = by_id.get(sid)
            if store is None:
                flash("没有这家店", "error")
                return redirect(url_for("incentive_page", month=month.strftime("%Y-%m")))
            data = invoice.build_invoice_xlsx(conn, store, as_of)
        return xlsx_response(data, invoice.invoice_filename(store, as_of))

    @app.route("/incentive/invoices.zip")
    @admin_required
    def invoice_zip():
        month, as_of = _incentive_month()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            data = invoice.build_invoice_zip(conn, stores, as_of)
        filename = f"invoices_{month.strftime('%Y_%m')}.zip"
        from .helpers import ascii_filename

        return Response(
            data,
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename={ascii_filename(filename)}"},
        )

    @app.route("/edits")
    @admin_required
    def edits_page():
        """审计日志：日报 / 成交播报谁在什么时候把哪家店改成了什么。kind=all|daily|deal"""
        store_id = request.args.get("store_id", "")
        kind = request.args.get("kind", "all")
        if kind not in ("all", "daily", "deal", "advance", "invoice", "mobile"):
            kind = "all"
        days_int = parse_days(request.args.get("days", "7"))
        # edited_at 存的是北京时间；用北京时间算窗口边界，避免 SQLite 的 UTC now 差 8 小时
        cutoff = (datetime.now(db.TZ) - timedelta(days=days_int)).strftime("%Y-%m-%d %H:%M:%S")
        sid = int(store_id) if store_id.isdigit() else None
        with db.get_db() as conn:
            all_stores = accessible_stores(conn)
            city_scope = request_scope(all_stores)
            scoped_ids = city_scope["ids"] if (not sid and city_scope["active"]) else []
            total = db.count_audit_edits(
                conn,
                kind=kind,
                sid=sid,
                scoped_ids=scoped_ids,
                cutoff=cutoff,
            )
            page, pages = pagination(request.args.get("page"), total)
            rows = [
                dict(r)
                for r in db.list_audit_edits(
                    conn,
                    kind=kind,
                    sid=sid,
                    scoped_ids=scoped_ids,
                    cutoff=cutoff,
                    limit=50,
                    offset=(page - 1) * 50,
                )
            ]
            all_stores = db.list_all_stores(conn)
        from .metrics_seed import metric_name_map
        names: Dict[str, str] = metric_name_map()
        for r in rows:
            try:
                before = json.loads(r["before_json"] or "{}")
                after = json.loads(r["after_json"] or "{}")
            except ValueError:
                before, after = {}, {}
            r["diff"] = _audit_diff(r, before, after, names)
            r["store_name"] = r["store_name"] or "?"
        return render_template(
            "edits.html",
            tab="edits",
            rows=rows,
            days=days_int,
            store_id=store_id,
            stores=all_stores,
            page=page,
            pages=pages,
            total=total,
            kind=kind,
            city_scope=city_scope,
        )

    @app.route("/bulletin.xlsx")
    @readonly_required
    def bulletin_xlsx():
        """通报表导出为真实 Excel（.xlsx）。保留旧 /bulletin.csv 作为兼容别名。"""
        biz_date = parse_date(request.args.get("date"))
        city = (request.args.get("city") or "").strip()
        area = (request.args.get("area") or "").strip()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            if city:
                stores = [s for s in stores if (s["city"] or "南通市") == city]
            if area:
                stores = [s for s in stores if (s["area_manager"] or "").strip() == area]
            rows = _bulletin_rows(conn, stores, biz_date)
            lines = bulletin.csv_rows(rows, biz_date, db.list_kpi_targets(conn))
            header, data_rows = lines[0], lines[1:]
            filename = f"bulletin_{biz_date.isoformat()}.xlsx"
            return xlsx_response(
                xlsx_bytes(header, data_rows, sheet="通报表"),
                filename,
            )

    @app.route("/bulletin.csv")
    @readonly_required
    def bulletin_csv():
        """旧 CSV 兼容别名：重定向到新 .xlsx。"""
        from flask import redirect as _redirect
        from flask import url_for as _url_for

        return _redirect(
            _url_for(
                "bulletin_xlsx",
                date=parse_date(request.args.get("date")).isoformat(),
                city=(request.args.get("city") or "").strip(),
            )
        )

    @app.route("/logins")
    @admin_required
    def logins_page():
        days = request.args.get("days", "7")
        action = request.args.get("action", "all")
        role = request.args.get("role", "all")
        q = (request.args.get("q") or "").strip()
        if action not in ("all", "login", "logout"):
            action = "all"
        if role not in ("all", "admin", "filler", "readonly", "city"):
            role = "all"
        days_int = parse_days(days)
        cutoff = (datetime.now(db.TZ) - timedelta(days=days_int)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_db() as conn:
            total = db.count_auth_events(
                conn,
                cutoff=cutoff,
                action="" if action == "all" else action,
                role="" if role == "all" else role,
                q=q,
            )
            page, pages = pagination(request.args.get("page"), total)
            rows = db.list_auth_events(
                conn,
                cutoff=cutoff,
                action="" if action == "all" else action,
                role="" if role == "all" else role,
                q=q,
                limit=50,
                offset=(page - 1) * 50,
            )
        return render_template(
            "logins.html",
            tab="logins",
            rows=rows,
            days=days_int,
            action=action,
            role=role,
            q=q,
            page=page,
            pages=pages,
            total=total,
        )
