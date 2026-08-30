"""管理员专属：看板、通报表、CSV、月度考核、修改审计。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import bulletin, db, insights, invoice, settlement
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
    close_rate,
    deal_diff,
    incentive_rules,
    login_required,
    named_advisor,
    pagination,
    parse_date,
    readonly_required,
    request_scope,
    review_preset_key,
    review_template_setting,
    sql_in,
    store_forecasts,
    store_label,
    values_for_broadcast,
    with_close_rate,
    xlsx_bytes,
    xlsx_response,
)
from .metrics_seed import (
    KPI_TARGETS,
    format_display,
    format_stored,
    from_stored,
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


def _bulletin_rows(conn, stores, biz_date: date):
    month_key = biz_date.strftime("%Y-%m")
    month_start = biz_date.replace(day=1)
    mobile_map = db.bisuan_mobile_map(conn, month_key)
    asof_map = db.bisuan_mobile_asof_map(conn, month_key)
    rows = []
    for store in stores:
        if not (store["mobile_code"] or "").strip():
            # 没配移动编码的店不进通报表
            continue
        pairs = values_for_broadcast(conn, store["id"], biz_date)
        day_ai, month_ai = pairs.get("ai_contract", (0, 0))
        day_bisuan = bulletin.bisuan_total(
            {"bisuan": pairs.get("bisuan", (0, 0))[0], "bisuan_high": pairs.get("bisuan_high", (0, 0))[0]}
        )
        month_bisuan = bulletin.bisuan_total(
            {"bisuan": pairs.get("bisuan", (0, 0))[1], "bisuan_high": pairs.get("bisuan_high", (0, 0))[1]}
        )
        day_coin, month_coin = rollup_pair(pairs, "coin_cut")
        report = db.get_report(conn, store["id"], biz_date)
        mobile = mobile_map.get(int(store["id"]))
        # 截止日按店取，没存就用默认（通报表日前一天）
        raw_asof = (asof_map.get(int(store["id"])) or "").strip()
        store_asof = _default_bisuan_mobile_asof(biz_date)
        if raw_asof:
            try:
                store_asof = _clamp_bisuan_mobile_asof(date.fromisoformat(raw_asof[:10]), biz_date)
            except ValueError:
                pass
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
                day_bisuan=day_bisuan,
                month_bisuan=month_bisuan,
                day_coin=day_coin,
                month_coin=month_coin,
                submitted=report is not None,
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


def _board_payload(conn, biz_date: date, view: str, city: str = ""):
    """看板页面和导出共用的取数。"""
    stores = accessible_stores(conn)
    cities = []
    for s in stores:
        city_name = (s["city"] or "").strip() or "未分地市"
        if city_name not in cities:
            cities.append(city_name)
    if city and city not in cities:
        city = ""
    if city:
        stores = [s for s in stores if ((s["city"] or "").strip() or "未分地市") == city]
    kpi_targets = db.list_kpi_targets(conn)
    rules = incentive_rules(conn)
    store_ids = [s["id"] for s in stores]
    month_begin = biz_date.replace(day=1)
    deal_today_map = db.deal_counts(conn, store_ids, biz_date, biz_date)
    deal_month_map = db.deal_counts(conn, store_ids, month_begin, biz_date)
    judged_map = store_forecasts(conn, stores, biz_date, rules)
    rows = []
    for store in stores:
        sid = store["id"]
        pairs = values_for_broadcast(conn, sid, biz_date)
        kpis = []
        day_sum = 0
        for code, name, _note in KPI_TARGETS:
            if code == "ai_contract":
                day, cum = pairs.get("ai_contract", (0, 0))
            else:
                day, cum = rollup_pair(pairs, code)
            target = kpi_targets.get(code, 0)
            scale = "bisuan" if code == "bisuan_total" else code
            day_disp = from_stored(scale, day)
            month_disp = from_stored(scale, cum)
            day_sum += day_disp
            kpis.append(
                {
                    "code": code,
                    "name": name,
                    "day": day_disp,
                    "day_text": format_stored(scale, day),
                    "month": month_disp,
                    "month_text": format_stored(scale, cum),
                    "target": target,
                    "progress": (month_disp / target * 100) if target else None,
                }
            )
        rep = db.get_report(conn, sid, biz_date)
        deal_today = with_close_rate(deal_today_map.get(sid, {"total": 0, "closed": 0}))
        deal_month = with_close_rate(deal_month_map.get(sid, {"total": 0, "closed": 0}))
        rows.append(
            {
                "store": store,
                "submitted_today": rep is not None,
                "submitter_name": rep["submitter_name"] if rep else None,
                "submitted_at": rep["submitted_at"] if rep else None,
                "reported_this_month": bool(judged_map[sid].get("reported")),
                "forecast": judged_map[sid],
                "kpis": kpis,
                "day_sum": day_sum,
                "month_sum": sum(k["month"] for k in kpis),
                "deal_today": deal_today,
                "deal_month": deal_month,
            }
        )
    max_day = {k: 0 for k, _n, _x in KPI_TARGETS}
    max_month = {k: 0 for k, _n, _x in KPI_TARGETS}
    for r in rows:
        for k in r["kpis"]:
            max_day[k["code"]] = max(max_day[k["code"]], k["day"])
            max_month[k["code"]] = max(max_month[k["code"]], k["month"])
    if view == "today":
        ranked = [r for r in rows if r["submitted_today"]]
        missing = [r for r in rows if not r["submitted_today"]]
        ranked.sort(key=lambda r: (-r["day_sum"], store_label(r["store"])))
    else:
        ranked = list(rows)
        missing = [r for r in rows if not r["reported_this_month"]]
        ranked.sort(key=lambda r: (-r["month_sum"], store_label(r["store"])))
    for rank, r in enumerate(ranked, 1):
        r["rank"] = rank
        for k in r["kpis"]:
            k["top_day"] = k["day"] > 0 and k["day"] == max_day[k["code"]]
            k["top_month"] = k["month"] > 0 and k["month"] == max_month[k["code"]]
    n = len(rows)
    sum_day = {k: 0 for k, _n, _x in KPI_TARGETS}
    sum_month = {k: 0 for k, _n, _x in KPI_TARGETS}
    done_today = sum(1 for r in rows if r["submitted_today"])
    done_month = sum(1 for r in rows if r["reported_this_month"])
    for r in rows:
        for k in r["kpis"]:
            sum_day[k["code"]] += k["day"]
            sum_month[k["code"]] += k["month"]
    grand = []
    for code, name, note in KPI_TARGETS:
        scale = "bisuan" if code == "bisuan_total" else code
        day_v = sum_day[code]
        month_v = sum_month[code]
        target_v = kpi_targets.get(code, 0) * n
        grand.append(
            {
                "code": code,
                "name": name,
                "note": note,
                "day": day_v,
                "month": month_v,
                "day_text": format_display(scale, day_v),
                "month_text": format_display(scale, month_v),
                "target": target_v,
                "progress": (month_v / target_v * 100) if target_v and n else None,
            }
        )
    deal_grand = {
        "day_total": sum(int(r["deal_today"]["total"]) for r in rows),
        "day_closed": sum(int(r["deal_today"]["closed"]) for r in rows),
        "month_total": sum(int(r["deal_month"]["total"]) for r in rows),
        "month_closed": sum(int(r["deal_month"]["closed"]) for r in rows),
    }
    deal_grand["day_rate"] = close_rate(deal_grand["day_closed"], deal_grand["day_total"])
    deal_grand["month_rate"] = close_rate(deal_grand["month_closed"], deal_grand["month_total"])
    return {
        "biz_date": biz_date,
        "view": view,
        "rows": rows,
        "ranked": ranked,
        "missing": missing,
        "grand": grand,
        "deal_grand": deal_grand,
        "coverage_today": round(done_today / n * 100) if n else 0,
        "coverage_month": round(done_month / n * 100) if n else 0,
        "done_today": done_today,
        "done_month": done_month,
        "n": n,
        "cities": cities,
        "city": city,
    }


def register_admin(app) -> None:
    @app.route("/board")
    @admin_required
    def board():
        biz_date = parse_date(request.args.get("date"))
        view = request.args.get("view") or "today"
        if view not in ("today", "month"):
            view = "today"
        city = (request.args.get("city") or "").strip()
        with db.get_db() as conn:
            payload = _board_payload(conn, biz_date, view, city)
            payload["is_admin"] = g.user["role"] == "admin"
            return render_template("board.html", **payload)

    @app.route("/insights")
    @admin_required
    def insights_page():
        as_of = parse_date(request.args.get("date"))
        with db.get_db() as conn:
            all_stores = accessible_stores(conn)
            scope = request_scope(all_stores)
            stores = scope["stores"]
            advisor = (request.args.get("advisor") or "").strip()
            if advisor not in ("yes", "no"):
                advisor = ""
            if advisor == "yes":
                stores = [s for s in stores if (s["advisor_name"] or "").strip()]
            elif advisor == "no":
                stores = [s for s in stores if not (s["advisor_name"] or "").strip()]
            store_ids = [int(s["id"]) for s in stores]
            month_start = as_of.replace(day=1)
            this_start, this_end = insights.week_span(as_of)
            prev_start, prev_end = insights.prev_week_span(as_of)
            month_facts = db.range_metric_totals(conn, store_ids, month_start, as_of, insights.FACT_CODES)
            week_facts = db.range_metric_totals(conn, store_ids, this_start, this_end, insights.FACT_CODES)
            prev_facts = db.range_metric_totals(conn, store_ids, prev_start, prev_end, insights.FACT_CODES)
            reported_month = db.stores_reported_in_month(conn, store_ids, as_of)
            if store_ids:
                clause, params = sql_in("store_id", store_ids)
                reported_today = {
                    int(row["store_id"])
                    for row in conn.execute(
                        f"SELECT store_id FROM daily_reports WHERE biz_date=? AND {clause}",
                        [as_of.isoformat(), *params],
                    )
                }
            else:
                reported_today = set()
            month_key = as_of.strftime("%Y-%m")
            mobile_bisuan = db.bisuan_mobile_map(conn, month_key)
            payload = insights.build_insights(
                stores=stores,
                as_of=as_of,
                kpi_targets=db.list_kpi_targets(conn),
                month_facts=month_facts,
                week_facts=week_facts,
                prev_week_facts=prev_facts,
                reported_today=reported_today,
                reported_month=reported_month,
                mobile_bisuan=mobile_bisuan,
            )
            if advisor == "yes":
                scope["label"] = scope["label"] + " · 有顾问"
            elif advisor == "no":
                scope["label"] = scope["label"] + " · 无顾问"
            payload.update({
                "scope": scope,
                "advisor": advisor,
                "show_idle": (request.args.get("idle") or "").strip() == "1",
            })
            return render_template("insights.html", **payload)

    @app.route("/deviation")
    @admin_required
    def deviation():
        """填报偏差榜：当月填报比算 vs 移动校准比算。

        温差=移动−填报。正=少报/低报，负=多报。只列有移动数的店。
        """
        today_d = db.today_local()
        month = parse_date(request.args.get("month"), today_d.replace(day=1)).replace(day=1)
        if month.month == 12:
            month_end = date(month.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(month.year, month.month + 1, 1) - timedelta(days=1)
        month_key = month.strftime("%Y-%m")
        if month.month == 1:
            prev_month = date(month.year - 1, 12, 1)
        else:
            prev_month = date(month.year, month.month - 1, 1)
        if month.month == 12:
            next_month = date(month.year + 1, 1, 1)
        else:
            next_month = date(month.year, month.month + 1, 1)
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            store_ids = [int(s["id"]) for s in stores]
            month_facts = db.range_metric_totals(
                conn, store_ids, month, month_end, ("bisuan", "bisuan_high")
            )
            mobile = db.bisuan_mobile_map(conn, month_key)
            rows = insights.build_deviation_board(
                stores=stores, month_facts=month_facts, mobile_bisuan=mobile
            )
        has_mobile = bool(mobile)
        under_n = sum(1 for r in rows if r["under"])
        over_n = sum(1 for r in rows if r["over"])
        max_abs = max((int(r["abs_diff"]) for r in rows), default=0)
        net_diff = sum(int(r["diff"]) for r in rows)
        return render_template(
            "deviation.html",
            rows=rows,
            month=month,
            month_key=month_key,
            prev_month=prev_month,
            next_month=next_month,
            has_mobile=has_mobile,
            under_n=under_n,
            over_n=over_n,
            even_n=len(rows) - under_n - over_n,
            max_abs=max_abs,
            net_diff=net_diff,
        )

    @app.route("/board.xlsx")
    @admin_required
    def board_xlsx():
        """导出当前看板视图(今日/本月 + 地市)为 Excel。"""
        biz_date = parse_date(request.args.get("date"))
        view = request.args.get("view") or "today"
        if view not in ("today", "month"):
            view = "today"
        city = (request.args.get("city") or "").strip()
        with db.get_db() as conn:
            payload = _board_payload(conn, biz_date, view, city)
        header = ["排名", "门店", "地市"]
        header += [g["name"] for g in payload["grand"]]
        header += ["触客", "成交", "成功率", "考核", "奖罚"]
        data_rows = []
        for r in payload["ranked"]:
            deal = r["deal_today"] if view == "today" else r["deal_month"]
            data_rows.append(
                [
                    r["rank"],
                    r["store"]["short_name"] or r["store"]["name"],
                    r["store"]["city"] or "",
                    *[k["day"] if view == "today" else k["month"] for k in r["kpis"]],
                    deal["total"],
                    deal["closed"],
                    deal["rate"],
                    r["forecast"]["label"],
                    r["forecast"]["money_text"],
                ]
            )
        tag = "today" if view == "today" else "month"
        city_tag = re.sub(r"[^A-Za-z0-9]+", "", city) or "all"
        filename = f"board_{tag}_{biz_date.isoformat()}_{city_tag}.xlsx"
        return xlsx_response(xlsx_bytes(header, data_rows, sheet="多店看板"), filename)

    @app.route("/bulletin")
    @readonly_required
    def bulletin_page():
        biz_date = parse_date(request.args.get("date"))
        city = (request.args.get("city") or "").strip()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            # 通报表只看有移动编码的店；地市下拉也按这个口径，避免空店把泰州带出来
            stores = [s for s in stores if (s["mobile_code"] or "").strip()]
            cities = sorted({(s["city"] or "南通市") for s in stores})
            if city and city not in cities:
                city = ""
            if not city:
                city = "南通市" if "南通市" in cities else (cities[0] if cities else "")
            stores = [s for s in stores if (s["city"] or "南通市") == city] if city else stores
            rows = _bulletin_rows(conn, stores, biz_date)
            copy_text = bulletin.tsv(rows, biz_date)
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
            mobile_asof = _default_bisuan_mobile_asof(biz_date)
            if asof_values:
                try:
                    mobile_asof = _clamp_bisuan_mobile_asof(
                        date.fromisoformat(max(asof_values)[:10]), biz_date
                    )
                except ValueError:
                    pass
            has_mobile = any(r.get("_month_bisuan_mobile_stored") is not None for r in rows)
            mobile_asof_head = (
                f"移动数据更新至{mobile_asof.month}/{mobile_asof.day}" if has_mobile else ""
            )
            return render_template(
                "bulletin.html",
                biz_date=biz_date,
                rows=rows,
                review=review,
                totals=bulletin.totals_row(rows) if rows else None,
                month_label=bulletin.month_label(biz_date),
                day_label=bulletin.day_label(biz_date),
                copy_text=copy_text,
                city=city,
                cities=cities,
                is_admin=g.user["role"] == "admin",
                mobile_asof=mobile_asof,
                mobile_asof_head=mobile_asof_head,
                review_presets=bulletin.REVIEW_PRESETS,
                review_preset=active_preset,
                has_custom_review=bool(saved_template),
                bulletin_title=f"{title_city}vivo零售运营中心移动业务通报表" if title_city else "移动业务通报表",
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
            return redirect(url_for("bulletin_page", date=biz_raw or None, city=city or None))
        if mobile_raw.strip() == "":
            flash("请填移动取数", "error")
            return redirect(url_for("bulletin_page", date=biz_date.isoformat(), city=city or None))
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
        return redirect(url_for("bulletin_page", date=biz_date.isoformat(), city=city))

    @app.route("/incentive")
    @admin_required
    def incentive_page():
        today_d = db.today_local()
        month = parse_date(request.args.get("month"), today_d.replace(day=1)).replace(day=1)
        if month.month == 12:
            month_end = date(month.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(month.year, month.month + 1, 1) - timedelta(days=1)
        as_of = min(month_end, today_d)
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
        today_d = db.today_local()
        month = parse_date(request.args.get("month"), today_d.replace(day=1)).replace(day=1)
        if month.month == 12:
            month_end = date(month.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(month.year, month.month + 1, 1) - timedelta(days=1)
        as_of = min(month_end, today_d)
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            data = settlement.build_settlement_xlsx(conn, stores, as_of)
        filename = f"settlement_{month.strftime('%Y_%m')}.xlsx"
        return xlsx_response(data, filename)

    def _incentive_month(default=None):
        today_d = db.today_local()
        fallback = default or today_d.replace(day=1)
        month = parse_date(request.values.get("month"), fallback).replace(day=1)
        if month.month == 12:
            month_end = date(month.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(month.year, month.month + 1, 1) - timedelta(days=1)
        return month, min(month_end, today_d)

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
            try:
                sid = int(request.values.get("store_id") or 0)
            except ValueError:
                sid = 0
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
            try:
                sid = int(request.args.get("store_id") or 0)
            except ValueError:
                sid = 0
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
        days = request.args.get("days", "7")
        kind = request.args.get("kind", "all")
        if kind not in ("all", "daily", "deal", "advance", "invoice", "mobile"):
            kind = "all"
        try:
            days_int = max(1, min(int(days), 90))
        except ValueError:
            days_int = 7
        # edited_at 存的是北京时间；用北京时间算窗口边界，避免 SQLite 的 UTC now 差 8 小时
        cutoff = (datetime.now(db.TZ) - timedelta(days=days_int)).strftime("%Y-%m-%d %H:%M:%S")
        sid = int(store_id) if store_id.isdigit() else None
        parts: List[str] = []
        params: List[Any] = []
        with db.get_db() as conn:
            all_stores = accessible_stores(conn)
            city_scope = request_scope(all_stores)
            scoped_ids = city_scope["ids"] if (not sid and city_scope["active"]) else []
        if kind in ("daily", "all"):
            w = "rn.edited_at >= ?"
            ps: List[Any] = [cutoff]
            if sid:
                w += " AND rn.store_id=?"
                ps.append(sid)
            elif scoped_ids:
                clause, ids = sql_in("rn.store_id", scoped_ids)
                w += f" AND {clause}"
                ps.extend(ids)
            parts.append(
                "SELECT 'daily' AS kind, rn.id, rn.biz_date, rn.edited_at, rn.note, "
                "rn.before_json, rn.after_json, '' AS action, "
                "s.name AS store_name, u.username AS user_name "
                "FROM report_edits rn "
                "LEFT JOIN stores s ON s.id=rn.store_id "
                "LEFT JOIN users u ON u.id=rn.user_id "
                f"WHERE {w}"
            )
            params += ps
        if kind in ("deal", "all"):
            w = "dn.edited_at >= ?"
            ps = [cutoff]
            if sid:
                w += " AND dn.store_id=?"
                ps.append(sid)
            elif scoped_ids:
                clause, ids = sql_in("dn.store_id", scoped_ids)
                w += f" AND {clause}"
                ps.extend(ids)
            parts.append(
                "SELECT 'deal' AS kind, dn.id, dn.biz_date, dn.edited_at, dn.note, "
                "dn.before_json, dn.after_json, dn.action, "
                "s.name AS store_name, u.username AS user_name "
                "FROM deal_edits dn "
                "LEFT JOIN stores s ON s.id=dn.store_id "
                "LEFT JOIN users u ON u.id=dn.user_id "
                f"WHERE {w}"
            )
            params += ps
        if kind in ("advance", "all"):
            w = "an.edited_at >= ?"
            ps = [cutoff]
            if sid:
                w += " AND an.store_id=?"
                ps.append(sid)
            elif scoped_ids:
                clause, ids = sql_in("an.store_id", scoped_ids)
                w += f" AND {clause}"
                ps.extend(ids)
            parts.append(
                "SELECT 'advance' AS kind, an.id, an.biz_date, an.edited_at, an.note, "
                "an.before_json, an.after_json, an.action, s.name AS store_name, u.username AS user_name "
                "FROM advance_edits an LEFT JOIN stores s ON s.id=an.store_id "
                "LEFT JOIN users u ON u.id=an.user_id WHERE " + w
            )
            params += ps
        if kind in ("invoice", "all"):
            w = "inv.edited_at >= ?"
            ps = [cutoff]
            if sid:
                w += " AND inv.store_id=?"
                ps.append(sid)
            elif scoped_ids:
                clause, ids = sql_in("inv.store_id", scoped_ids)
                w += f" AND {clause}"
                ps.extend(ids)
            parts.append(
                "SELECT 'invoice' AS kind, inv.id, inv.month AS biz_date, inv.edited_at, inv.note, "
                "inv.before_json, inv.after_json, inv.action, s.name AS store_name, u.username AS user_name "
                "FROM invoice_edits inv LEFT JOIN stores s ON s.id=inv.store_id "
                "LEFT JOIN users u ON u.id=inv.user_id WHERE " + w
            )
            params += ps
        if kind in ("mobile", "all"):
            w = "bm.edited_at >= ?"
            ps = [cutoff]
            if sid:
                w += " AND bm.store_id=?"
                ps.append(sid)
            elif scoped_ids:
                clause, ids = sql_in("bm.store_id", scoped_ids)
                w += f" AND {clause}"
                ps.extend(ids)
            parts.append(
                "SELECT 'mobile' AS kind, bm.id, bm.month AS biz_date, bm.edited_at, bm.note, "
                "bm.before_json, bm.after_json, bm.action, s.name AS store_name, u.username AS user_name "
                "FROM bisuan_mobile_edits bm LEFT JOIN stores s ON s.id=bm.store_id "
                "LEFT JOIN users u ON u.id=bm.user_id WHERE " + w
            )
            params += ps
        union_sql = " UNION ALL ".join(parts) if parts else \
            "SELECT 'daily' AS kind, NULL AS id, '' AS biz_date, '' AS edited_at, '' AS note, " \
            "'{}' AS before_json, '{}' AS after_json, '' AS action, '' AS store_name, '' AS user_name " \
            "WHERE 0"
        with db.get_db() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM ({union_sql})", params
            ).fetchone()[0]
            page, pages = pagination(request.args.get("page"), total)
            rows = [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT * FROM ({union_sql}) t
                    ORDER BY t.edited_at DESC, t.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*params, 50, (page - 1) * 50],
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
            if r["kind"] == "deal":
                if r.get("action") == "create":
                    prefix = "新增触客："
                elif r.get("action") == "delete":
                    prefix = "删除触客："
                else:
                    prefix = "覆盖："
                r["diff"] = prefix + deal_diff(before, after)
            elif r["kind"] == "advance":
                action = {"create": "新增垫资：", "update": "覆盖垫资：", "delete": "删除垫资：", "pay": "兑付：", "unpay": "取消兑付："}.get(r.get("action"), "垫资：")
                r["diff"] = action + json.dumps({"before": before, "after": after}, ensure_ascii=False, default=str)
            elif r["kind"] == "invoice":
                action = {"create": "新增开票：", "update": "改开票：", "delete": "删除开票："}.get(
                    r.get("action"), "开票："
                )
                r["diff"] = action + db.invoice_diff(before, after)
            elif r["kind"] == "mobile":
                from .metrics_seed import format_stored as _fs

                b = before.get("value_tenths")
                a = after.get("value_tenths")
                head = "新录移动：" if r.get("action") == "create" else "改移动："
                if b is None:
                    body = f"{_fs('bisuan', a or 0)}"
                else:
                    body = f"{_fs('bisuan', b or 0)}→{_fs('bisuan', a or 0)}"
                asof_txt = (after.get("asof") or "")[:10]
                if asof_txt:
                    body += f"（至{asof_txt}）"
                r["diff"] = head + body
            else:
                r["diff"] = build_diff(before, after, names)
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
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            if city:
                stores = [s for s in stores if (s["city"] or "南通市") == city]
            rows = _bulletin_rows(conn, stores, biz_date)
            lines = bulletin.csv_rows(rows, biz_date)
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
        try:
            days_int = max(1, min(int(days), 90))
        except ValueError:
            days_int = 7
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
