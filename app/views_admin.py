"""管理员专属：看板、通报表、CSV、月度考核、修改审计。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import bulletin, db, settlement
from .helpers import (
    accessible_stores,
    admin_required,
    build_diff,
    close_rate,
    deal_diff,
    incentive_rules,
    pagination,
    parse_date,
    readonly_required,
    request_scope,
    sql_in,
    store_forecast,
    store_label,
    values_for_broadcast,
    with_close_rate,
    xlsx_bytes,
    xlsx_response,
)
from .metrics_seed import KPI_TARGETS, format_stored, from_stored, rollup_pair, to_stored


def _bisuan_mobile_raw(conn, store_id: int, month_key: str) -> str:
    """读移动取数；兼容旧 key bisuan_official_*。"""
    raw = db.get_setting(conn, f"bisuan_mobile_{store_id}_{month_key}", "")
    if raw:
        return raw
    return db.get_setting(conn, f"bisuan_official_{store_id}_{month_key}", "")


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


def _bisuan_mobile_asof(conn, month_key: str, biz_date: date) -> date:
    raw = db.get_setting(conn, f"bisuan_mobile_asof_{month_key}", "")
    if raw:
        try:
            return _clamp_bisuan_mobile_asof(date.fromisoformat(raw[:10]), biz_date)
        except ValueError:
            pass
    return _default_bisuan_mobile_asof(biz_date)


def _bulletin_rows(conn, stores, biz_date: date):
    month_key = biz_date.strftime("%Y-%m")
    month_start = biz_date.replace(day=1)
    asof = _bisuan_mobile_asof(conn, month_key, biz_date)
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
        mobile_raw = _bisuan_mobile_raw(conn, store["id"], month_key)
        mobile = None
        try:
            mobile = int(round(float(mobile_raw) * 10)) if mobile_raw else None
        except (TypeError, ValueError):
            mobile = None
        sys_asof = None
        if mobile is not None:
            # 对照用截止日同期上报数，不是通报表选中日的整月
            sys_asof = db.week_metric_total(
                conn, store["id"], month_start, asof, ("bisuan", "bisuan_high")
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
                month_bisuan_asof=asof.isoformat() if mobile is not None else "",
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
    reported_ids = db.stores_reported_in_month(conn, store_ids, biz_date)
    deal_today_map = db.deal_counts(conn, store_ids, biz_date, biz_date)
    deal_month_map = db.deal_counts(conn, store_ids, month_begin, biz_date)
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
        reported_this_month = sid in reported_ids
        deal_today = with_close_rate(deal_today_map.get(sid, {"total": 0, "closed": 0}))
        deal_month = with_close_rate(deal_month_map.get(sid, {"total": 0, "closed": 0}))
        rows.append(
            {
                "store": store,
                "submitted_today": rep is not None,
                "submitter_name": rep["submitter_name"] if rep else None,
                "submitted_at": rep["submitted_at"] if rep else None,
                "reported_this_month": reported_this_month,
                "forecast": store_forecast(
                    conn, store, biz_date, rules, reported=reported_this_month
                ),
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
    grand = [
        {
            "code": code,
            "name": name,
            "note": note,
            "day": sum_day[code],
            "month": sum_month[code],
            "target": kpi_targets.get(code, 0) * n,
            "progress": (
                sum_month[code] / (kpi_targets.get(code, 0) * n) * 100
            )
            if kpi_targets.get(code, 0) and n
            else None,
        }
        for code, name, note in KPI_TARGETS
    ]
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
            review = (
                bulletin.summary(rows, biz_date, title_city, day_deal=day_deal_sum, month_deal=month_deal_sum)
                if rows
                else ""
            )
            month_key = biz_date.strftime("%Y-%m")
            # 表单默认：已存截止日，否则前一天
            mobile_asof = _bisuan_mobile_asof(conn, month_key, biz_date)
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
                bulletin_title=f"{title_city}vivo零售运营中心移动业务通报表" if title_city else "移动业务通报表",
            )

    @app.route("/bulletin/bisuan-mobile", methods=["POST"])
    @admin_required
    def bulletin_bisuan_mobile():
        """通报表录移动取数（笔算新增+高，截止某日累计），差额落到截止日「比算新增」。"""
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
            # 对照截止日同期上报数（笔算新增 + 高）
            current = db.week_metric_total(conn, sid, month_start, asof, ("bisuan", "bisuan_high"))
            delta = mobile - current
            month_key = biz_date.strftime("%Y-%m")
            db.set_setting(conn, f"bisuan_mobile_{sid}_{month_key}", f"{mobile / 10:.1f}")
            db.set_setting(conn, f"bisuan_mobile_asof_{month_key}", asof.isoformat())
            note = f"月校准笔算 移{format_stored('bisuan', mobile)} 至{asof.isoformat()}"
            if delta > 0:
                day_val = int(db.day_values(conn, sid, asof).get("bisuan", 0) or 0)
                db.set_day_value(
                    conn,
                    store_id=sid,
                    biz_date=asof,
                    metric_code="bisuan",
                    value=day_val + delta,
                    user_id=g.user["id"],
                    note=note,
                )
            elif delta < 0:
                need = -delta
                days = [
                    month_start + timedelta(days=i)
                    for i in range((asof - month_start).days + 1)
                ]
                for day in reversed(days):
                    cur = int(db.day_values(conn, sid, day).get("bisuan", 0) or 0)
                    if cur <= 0:
                        continue
                    take = min(cur, need)
                    db.set_day_value(
                        conn,
                        store_id=sid,
                        biz_date=day,
                        metric_code="bisuan",
                        value=cur - take,
                        user_id=g.user["id"],
                        note=note,
                    )
                    need -= take
                    if need <= 0:
                        break
            sign = "+" if delta > 0 else ""
            flash(
                f"已录移 {format_stored('bisuan', mobile)}（至{asof.month}/{asof.day}），"
                f"上报同期 {format_stored('bisuan', current)}，差额 {sign}{format_stored('bisuan', delta)}",
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
            reported_ids = db.stores_reported_in_month(conn, [s["id"] for s in stores], as_of)
            for store in stores:
                judged = store_forecast(
                    conn, store, as_of, rules, reported=store["id"] in reported_ids
                )
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
            return render_template(
                "incentive.html",
                month=month,
                as_of=as_of,
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

    @app.route("/edits")
    @admin_required
    def edits_page():
        """审计日志：日报 / 成交播报谁在什么时候把哪家店改成了什么。kind=all|daily|deal"""
        store_id = request.args.get("store_id", "")
        days = request.args.get("days", "7")
        kind = request.args.get("kind", "all")
        if kind not in ("all", "daily", "deal", "advance"):
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
        union_sql = " UNION ALL ".join(parts) if parts else \
            "SELECT 'daily' AS kind, NULL AS id, '' AS biz_date, '' AS edited_at, '' AS note, " \
            "'{}' AS before_json, '{}' AS after_json, '' AS store_name, '' AS user_name " \
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
        if role not in ("all", "admin", "filler", "readonly"):
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
