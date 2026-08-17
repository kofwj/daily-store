"""报表、CSV 导出、删除单日日报、校准单个格子。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import db
from .helpers import (
    admin_required,
    login_required,
    parse_date,
    pick_store,
    xlsx_bytes,
    xlsx_response,
)
from .metrics_seed import (
    KPI_TARGETS,
    SECTIONS,
    format_stored,
    from_stored,
    is_decimal_metric,
    metric_step,
    rollup_amount,
    to_stored,
)


def register_report(app) -> None:
    @app.route("/report")
    @login_required
    def report():
        with db.get_db() as conn:
            store, stores = pick_store(conn, request.args.get("store_id"))
            if store is None:
                return render_template("empty.html")
            today_d = db.today_local()
            view = request.args.get("view") or "month"
            if view == "week":
                start = parse_date(request.args.get("start"), today_d - timedelta(days=today_d.weekday()))
                end = parse_date(request.args.get("end"), start + timedelta(days=6))
            elif view == "day":
                start = parse_date(request.args.get("start"), today_d)
                end = start
            else:
                view = "month"
                start = parse_date(request.args.get("start"), today_d.replace(day=1))
                if start.month == 12:
                    end = date(start.year + 1, 1, 1) - timedelta(days=1)
                else:
                    end = date(start.year, start.month + 1, 1) - timedelta(days=1)
                end = min(end, today_d)

            facts = db.facts_in_range(conn, store["id"], start, end)
            metrics = db.list_metrics(conn)
            dates = sorted({row["biz_date"] for row in facts})
            grid: Dict[str, Dict[str, int]] = {m["code"]: {} for m in metrics}
            totals: Dict[str, int] = {m["code"]: 0 for m in metrics}
            for row in facts:
                # 只累计/摆放“当前活跃”指标；历史停用指标的遗留 day 值不参与报表，避免 KeyError
                if row["metric_code"] not in totals:
                    continue
                stored = int(row["day_value"] or 0)
                grid[row["metric_code"]][row["biz_date"]] = stored
                totals[row["metric_code"]] += stored

            # 提交状态 + 区间总天数（工作日口径：周一~周五）
            submitted = {
                row["biz_date"]
                for row in conn.execute(
                    """
                    SELECT biz_date FROM daily_reports
                    WHERE store_id=? AND biz_date>=? AND biz_date<=?
                    """,
                    (store["id"], start.isoformat(), end.isoformat()),
                )
            }
            submitted_dates = sorted(submitted & set(dates))
            all_days = [
                (start + timedelta(days=i)).isoformat()
                for i in range((end - start).days + 1)
            ]
            # 区间总天数：日报每天都要填，不区分工作日
            total_days = all_days
            coverage = (
                round(len(submitted_dates) / len(total_days) * 100)
                if total_days
                else 0
            )

            # 三个考核 KPI：从 kpi_targets 读目标（与月度考核页一致），区间累计
            kpi_targets = db.list_kpi_targets(conn)
            kpi_cards = []
            for code, name, note in KPI_TARGETS:
                if code == "ai_contract":
                    total = totals.get("ai_contract", 0)
                else:
                    total = rollup_amount(totals, code)
                target = kpi_targets.get(code, 0)
                scale_code = "bisuan" if code == "bisuan_total" else code
                total_disp = from_stored(scale_code, total)
                kpi_cards.append(
                    {
                        "code": code,
                        "name": name,
                        "note": note,
                        "total": total_disp,
                        "total_text": format_stored(scale_code, total),
                        "target": target,
                        "progress": (total_disp / target * 100) if target else None,
                        "left": max(0, target - total_disp) if target else None,
                    }
                )

            # 指标行：按 SECTIONS 分组；考核 KPI 目标走 kpi_targets，普通指标走 metrics
            metric_targets = db.metric_target_map(conn)
            rows = []
            for m in metrics:
                total = totals[m["code"]]
                month_target = int(metric_targets.get(m["code"], 0) or 0)
                days_stored = [grid[m["code"]].get(d, 0) for d in dates]
                rows.append(
                    {
                        "code": m["code"],
                        "name": m["name"],
                        "section": m["section"],
                        "target": month_target,
                        "total": from_stored(m["code"], total),
                        "total_text": format_stored(m["code"], total),
                        "progress": (total / month_target * 100) if month_target else None,
                        "days": [from_stored(m["code"], v) for v in days_stored],
                        "days_text": [format_stored(m["code"], v) for v in days_stored],
                        "step": metric_step(m["code"]),
                        "decimal": is_decimal_metric(m["code"]),
                        "avg": round(from_stored(m["code"], total) / max(1, len(submitted_dates)), 1) if submitted_dates else 0,
                    }
                )

            # 分组视图：SECTIONS 顺序 + 每组的 KPI/普通行
            grouped = []
            for section in SECTIONS:
                sec_rows = [r for r in rows if r["section"] == section["code"]]
                if not sec_rows:
                    continue
                grouped.append(
                    {
                        "code": section["code"],
                        "header": section["header"] or section["code"],
                        "rows": sec_rows,
                    }
                )

            return render_template(
                "report.html",
                store=store,
                stores=stores,
                view=view,
                start=start,
                end=end,
                dates=dates,
                rows=rows,
                grouped=grouped,
                submitted=submitted,
                submitted_dates=submitted_dates,
                coverage=coverage,
                total_days=len(total_days),
                kpi_cards=kpi_cards,
                month_start=start.replace(day=1),
            )

    @app.route("/report.xlsx")
    @login_required
    def report_xlsx():
        """报表导出为真实 Excel（.xlsx）。"""
        with db.get_db() as conn:
            store, _stores = pick_store(conn, request.args.get("store_id"))
            if store is None:
                return Response("no store", status=400)
            start = parse_date(request.args.get("start"), db.today_local().replace(day=1))
            end = parse_date(request.args.get("end"), db.today_local())
            facts = db.facts_in_range(conn, store["id"], start, end)
            header = ["日期", "门店", "分类", "指标", "日值"]
            data_rows = [
                [row["biz_date"], store["name"], row["section"], row["name"], row["day_value"]]
                for row in facts
            ]
            filename = f"{store['code']}_{start.isoformat()}_{end.isoformat()}.xlsx"
            return xlsx_response(
                xlsx_bytes(header, data_rows, sheet="报表"),
                filename,
            )

    @app.route("/report.csv")
    @login_required
    def report_csv():
        """旧 CSV 兼容别名：重定向到新 .xlsx。"""
        return redirect(
            url_for(
                "report_xlsx",
                store_id=request.args.get("store_id"),
                start=request.args.get("start"),
                end=request.args.get("end"),
            )
        )

    @app.route("/report/delete", methods=["POST"])
    @admin_required
    def report_delete():
        """删除某店某一天的日报（连事实一起删）。锁定时间内管理员可删。"""
        store_id = request.form.get("store_id") or request.args.get("store_id")
        day = request.form.get("date") or request.args.get("date")
        try:
            sid = int(store_id or 0)
            biz_date = date.fromisoformat(day) if day else None
        except (ValueError, TypeError):
            biz_date = None
        if sid <= 0 or biz_date is None:
            return Response("bad request", status=400)
        with db.get_db() as conn:
            if not db.user_can_access_store(conn, g.user, sid):
                return Response("forbidden", status=403)
            report = db.get_report(conn, sid, biz_date)
            if report is None:
                flash("该日没有日报，无需删除。", "error")
            else:
                before = db.day_values(conn, sid, biz_date)
                db.record_edit(
                    conn,
                    biz_date=biz_date,
                    store_id=sid,
                    user_id=g.user["id"],
                    before=before,
                    after={},
                    note="删除日报",
                )
                conn.execute(
                    "DELETE FROM daily_facts WHERE store_id=? AND biz_date=?",
                    (sid, biz_date.isoformat()),
                )
                conn.execute(
                    "DELETE FROM daily_reports WHERE store_id=? AND biz_date=?",
                    (sid, biz_date.isoformat()),
                )
                flash("已删除该日日报（已记录审计）。", "ok")
        return redirect(
            url_for("report", store_id=sid, view="month", start=biz_date.replace(day=1).isoformat())
        )

    @app.route("/report/cell", methods=["POST"])
    @admin_required
    def report_cell():
        """管理员只改某一个指标格子，其它数字不动。"""
        store_id = request.form.get("store_id") or ""
        day = request.form.get("date") or ""
        code = (request.form.get("metric_code") or "").strip()
        raw = request.form.get("value") or "0"
        view = request.form.get("view") or "month"
        start = request.form.get("start") or ""
        end = request.form.get("end") or ""
        try:
            sid = int(store_id)
            biz_date = date.fromisoformat(day)
            value = max(0, to_stored(code, raw))
        except (ValueError, TypeError):
            flash("格子参数不对", "error")
            return redirect(url_for("report"))
        if not code:
            flash("没有指定指标", "error")
            return redirect(url_for("report", store_id=sid))
        with db.get_db() as conn:
            if not db.user_can_access_store(conn, g.user, sid):
                return Response("forbidden", status=403)
            try:
                db.set_day_value(
                    conn,
                    store_id=sid,
                    biz_date=biz_date,
                    metric_code=code,
                    value=value,
                    user_id=g.user["id"],
                )
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash(f"已校准 {biz_date.isoformat()} 的该指标为 {format_stored(code, value)}", "ok")
        kwargs = {"store_id": sid, "view": view}
        if start:
            kwargs["start"] = start
        if end and view != "day":
            kwargs["end"] = end
        return redirect(url_for("report", **kwargs))

    @app.route("/report/bisuan-week", methods=["POST"])
    @admin_required
    def report_bisuan_week():
        """按移动本周官方数校准笔算。差额补到区间最后一天的「比算新增」。"""
        store_id = request.form.get("store_id") or ""
        start_raw = request.form.get("start") or ""
        end_raw = request.form.get("end") or ""
        official_raw = request.form.get("official") or ""
        try:
            sid = int(store_id)
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
            official = max(0, to_stored("bisuan", official_raw))
        except (ValueError, TypeError):
            flash("周校准参数不对", "error")
            return redirect(url_for("report"))
        if end < start:
            start, end = end, start
        with db.get_db() as conn:
            if not db.user_can_access_store(conn, g.user, sid):
                return Response("forbidden", status=403)
            current = db.week_metric_total(conn, sid, start, end, ("bisuan", "bisuan_high"))
            delta = official - current
            if delta == 0:
                flash(f"本周笔算已是 {format_stored('bisuan', official)}，不用再校", "ok")
            else:
                note = f"周校准笔算 官方{format_stored('bisuan', official)}"
                days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
                if delta > 0:
                    last = db.day_values(conn, sid, end)
                    db.set_day_value(
                        conn,
                        store_id=sid,
                        biz_date=end,
                        metric_code="bisuan",
                        value=int(last.get("bisuan", 0) or 0) + delta,
                        user_id=g.user["id"],
                        note=note,
                    )
                    flash(
                        f"已按移动官方数 {format_stored('bisuan', official)} 校准，"
                        f"{end.isoformat()} 比算新增 +{format_stored('bisuan', delta)}",
                        "ok",
                    )
                else:
                    need = -delta
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
                    flash(
                        f"已按移动官方数 {format_stored('bisuan', official)} 校准，"
                        f"本周比算新增 {format_stored('bisuan', delta)}",
                        "ok",
                    )
        return redirect(url_for("report", store_id=sid, view="week", start=start.isoformat(), end=end.isoformat()))
