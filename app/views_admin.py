"""管理员专属：看板、通报表、CSV、月度考核、修改审计。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from flask import g, render_template, request

from . import bulletin, db
from .helpers import (
    accessible_stores,
    admin_required,
    build_diff,
    deal_diff,
    incentive_rules,
    pagination,
    parse_date,
    store_forecast,
    store_label,
    values_for_broadcast,
    xlsx_bytes,
    xlsx_response,
)
from .metrics_seed import KPI_TARGETS, rollup_pair


def _bulletin_rows(conn, stores, biz_date: date):
    rows = []
    for store in stores:
        pairs = values_for_broadcast(conn, store["id"], biz_date)
        day_ai, month_ai = pairs.get("ai_contract", (0, 0))
        day_bisuan = bulletin.bisuan_total(
            {"bisuan": pairs.get("bisuan", (0, 0))[0], "bisuan_high": pairs.get("bisuan_high", (0, 0))[0]}
        )
        month_bisuan = bulletin.bisuan_total(
            {"bisuan": pairs.get("bisuan", (0, 0))[1], "bisuan_high": pairs.get("bisuan_high", (0, 0))[1]}
        )
        _day_coin, month_coin = rollup_pair(pairs, "coin_cut")
        report = db.get_report(conn, store["id"], biz_date)
        rows.append(
            bulletin.build_row(
                store,
                day_ai=day_ai,
                month_ai=month_ai,
                day_bisuan=day_bisuan,
                month_bisuan=month_bisuan,
                month_coin=month_coin,
                submitted=report is not None,
            )
        )
    return bulletin.apply_scales(rows)


def register_admin(app) -> None:
    @app.route("/board")
    @admin_required
    def board():
        biz_date = parse_date(request.args.get("date"))
        view = request.args.get("view") or "today"
        if view not in ("today", "month"):
            view = "today"
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            cities = []
            for s in stores:
                city_name = (s["city"] or "").strip() or "未分地市"
                if city_name not in cities:
                    cities.append(city_name)
            city = (request.args.get("city") or "").strip()
            if city and city not in cities:
                city = ""
            if city:
                stores = [s for s in stores if ((s["city"] or "").strip() or "未分地市") == city]
            kpi_targets = db.list_kpi_targets(conn)
            month_start, _month_end = db.month_bounds(biz_date)
            rules = incentive_rules(conn)
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
                    day_sum += int(day)
                    kpis.append(
                        {
                            "code": code,
                            "name": name,
                            "day": int(day),
                            "month": int(cum),
                            "target": target,
                            "progress": (int(cum) / target * 100) if target else None,
                        }
                    )
                rep = db.get_report(conn, sid, biz_date)
                reported_this_month = (
                    conn.execute(
                        "SELECT 1 FROM daily_reports WHERE store_id=? AND biz_date>=? AND biz_date<=? LIMIT 1",
                        (sid, month_start, biz_date.isoformat()),
                    ).fetchone()
                    is not None
                )
                rows.append(
                    {
                        "store": store,
                        "submitted_today": rep is not None,
                        "submitter_name": rep["submitter_name"] if rep else None,
                        "submitted_at": rep["submitted_at"] if rep else None,
                        "reported_this_month": reported_this_month,
                        "forecast": store_forecast(conn, store, biz_date, rules),
                        "kpis": kpis,
                        "day_sum": day_sum,
                        "month_sum": sum(k["month"] for k in kpis),
                    }
                )
            # 每列最高值（今天/本月）用来标榜首
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
            # 本区合计 + 覆盖率
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
            return render_template(
                "board.html",
                biz_date=biz_date,
                view=view,
                rows=rows,
                ranked=ranked,
                missing=missing,
                grand=grand,
                coverage_today=round(done_today / n * 100) if n else 0,
                coverage_month=round(done_month / n * 100) if n else 0,
                done_today=done_today,
                done_month=done_month,
                n=n,
                is_admin=g.user["role"] == "admin",
                cities=cities,
                city=city,
            )

    @app.route("/bulletin")
    @admin_required
    def bulletin_page():
        biz_date = parse_date(request.args.get("date"))
        city = (request.args.get("city") or "").strip()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            cities = sorted({(s["city"] or "南通市") for s in stores})
            if not city:
                city = "南通市" if "南通市" in cities else (cities[0] if cities else "")
            stores = [s for s in stores if (s["city"] or "南通市") == city] if city else stores
            rows = _bulletin_rows(conn, stores, biz_date)
            copy_text = bulletin.tsv(rows, biz_date)
            title_city = city.replace("市", "") if city else ""
            return render_template(
                "bulletin.html",
                biz_date=biz_date,
                rows=rows,
                totals=bulletin.totals_row(rows) if rows else None,
                month_label=bulletin.month_label(biz_date),
                day_label=bulletin.day_label(biz_date),
                copy_text=copy_text,
                city=city,
                cities=cities,
                bulletin_title=f"{title_city}vivo零售运营中心移动业务通报表" if title_city else "移动业务通报表",
            )

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
            }
            rules = incentive_rules(conn)
            for store in stores:
                judged = store_forecast(conn, store, as_of, rules)
                rows.append(judged)
                totals["store_reward"] += judged["store_reward"]
                totals["store_penalty"] += judged["store_penalty"]
                totals["advisor_penalty"] += judged["advisor_penalty"]
                totals["net"] += judged["net"]
                totals["passed"] += 1 if judged["passed"] else 0
            return render_template(
                "incentive.html",
                month=month,
                as_of=as_of,
                rows=rows,
                totals=totals,
            )

    @app.route("/edits")
    @admin_required
    def edits_page():
        """审计日志：日报 / 成交播报谁在什么时候把哪家店改成了什么。kind=all|daily|deal"""
        store_id = request.args.get("store_id", "")
        days = request.args.get("days", "7")
        kind = request.args.get("kind", "all")
        if kind not in ("all", "daily", "deal"):
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
        if kind in ("daily", "all"):
            w = "rn.edited_at >= ?"
            ps: List[Any] = [cutoff]
            if sid:
                w += " AND rn.store_id=?"
                ps.append(sid)
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
                    r["diff"] = "新增成交：" + deal_diff(before, after)
                else:
                    r["diff"] = "覆盖：" + deal_diff(before, after)
            else:
                r["diff"] = build_diff(before, after, names)
            r["store_name"] = r["store_name"] or "?"
        return render_template(
            "edits.html",
            rows=rows,
            days=days_int,
            store_id=store_id,
            stores=all_stores,
            page=page,
            pages=pages,
            total=total,
            kind=kind,
        )

    @app.route("/bulletin.xlsx")
    @admin_required
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
    @admin_required
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
