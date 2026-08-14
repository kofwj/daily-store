"""门店日报 Web：登录、填报、一键复制播报、日报/周报/月报。"""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional

from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from . import broadcast, bulletin, db, incentive
from .metrics_seed import KPI_TARGETS, SECTIONS, rollup_pair

SECRET_FALLBACK = "store-daily-dev-change-me"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def create_app() -> Flask:
    app = Flask(__name__)
    secret = os.environ.get("STORE_DAILY_SECRET", SECRET_FALLBACK)
    app.config["SECRET_KEY"] = secret
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    # 公网 / Cloudflare 后必须开 Secure，否则口令 Cookie 会明文落到 HTTP
    secure = os.environ.get("STORE_DAILY_SECURE", "0") == "1"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = secure
    app.config["PREFERRED_URL_SCHEME"] = "https" if secure else "http"
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if secret == SECRET_FALLBACK and secure:
        raise RuntimeError("生产环境必须设置 STORE_DAILY_SECRET，不要用默认值")
    db.init_db()

    @app.before_request
    def load_user() -> None:
        g.user = None
        uid = session.get("user_id")
        if not uid:
            return
        with db.get_db() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
            g.user = row

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            return fn(*args, **kwargs)

        return wrapper

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            if g.user["role"] != "admin":
                flash("需要管理员权限", "error")
                return redirect(url_for("today"))
            return fn(*args, **kwargs)

        return wrapper

    def store_label(store) -> str:
        if store is None:
            return ""
        short = ""
        try:
            short = (store["short_name"] if "short_name" in store.keys() else "") or ""
        except Exception:
            short = (store.get("short_name") if hasattr(store, "get") else "") or ""
        return short or store["name"]

    def parse_date(raw: Optional[str], fallback: Optional[date] = None) -> date:
        if raw and DATE_RE.match(raw):
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        if raw and len(raw) == 7 and raw[4] == "-":
            try:
                return date.fromisoformat(raw + "-01")
            except ValueError:
                pass
        return fallback or db.today_local()

    def accessible_stores(conn) -> List[Any]:
        return db.list_user_stores(conn, g.user)

    def pick_store(conn, raw_id: Optional[str]):
        stores = accessible_stores(conn)
        if not stores:
            return None, []
        if raw_id:
            try:
                sid = int(raw_id)
            except ValueError:
                sid = stores[0]["id"]
        else:
            sid = session.get("store_id") or stores[0]["id"]
        if not db.user_can_access_store(conn, g.user, sid):
            sid = stores[0]["id"]
        session["store_id"] = sid
        store = db.get_store(conn, sid)
        return store, stores

    def values_for_broadcast(conn, store_id: int, biz_date: date) -> Dict[str, broadcast.DayCum]:
        today = db.day_values(conn, store_id, biz_date)
        prev = db.prev_month_cum(conn, store_id, biz_date)
        return broadcast.add_day_to_prev(prev, today)

    def store_forecast(conn, store, as_of: date) -> Dict[str, Any]:
        month_vals = db.month_cum_through(conn, store["id"], as_of)
        ai = int(month_vals.get("ai_contract", 0) or 0)
        sesame = int(month_vals.get("coin_cut_new_sesame", 0) or 0)
        advisor_name = (store["advisor_name"] if "advisor_name" in store.keys() else "") or ""
        judged = incentive.judge(bool(advisor_name.strip()), ai, sesame)
        judged.update(
            {
                "store_id": store["id"],
                "name": store["name"],
                "store_manager": store["store_manager"] or "",
                "advisor_name": advisor_name.strip(),
                "money_text": incentive.money_text(judged),
            }
        )
        return judged

    @app.route("/health")
    def health():
        return {"ok": True, "service": "store-daily"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            pin = request.form.get("pin") or ""
            with db.get_db() as conn:
                user = db.get_user_by_username(conn, username)
                if user and db.verify_pin(pin, user["pin_hash"]):
                    session.clear()
                    session["user_id"] = user["id"]
                    nxt = request.args.get("next") or url_for("today")
                    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                        nxt = url_for("today")
                    return redirect(nxt)
            flash("账号或口令不对", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def settings_tab() -> str:
        tab = request.values.get("tab") or "account"
        allowed = {"account", "stores", "people", "targets", "permissions"}
        if tab not in allowed:
            return "account"
        if g.user["role"] != "admin" and tab != "account":
            return "account"
        return tab

    def change_own_pin() -> None:
        old = request.form.get("old_pin") or ""
        new = request.form.get("new_pin") or ""
        again = request.form.get("new_pin2") or ""
        min_len = db.FILLER_PIN_MIN if g.user["role"] == "filler" else db.ADMIN_PIN_MIN
        if len(new) < min_len:
            raise ValueError(f"新口令至少 {min_len} 位")
        if new != again:
            raise ValueError("两次新口令不一致")
        if not db.verify_pin(old, g.user["pin_hash"]):
            raise ValueError("当前口令不对")
        with db.get_db() as conn:
            db.update_user_pin(conn, g.user["id"], new)

    @app.route("/me")
    @login_required
    def me():
        return redirect(url_for("settings", tab="account"))

    @app.route("/")
    @login_required
    def home():
        return redirect(url_for("today"))

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
                filler_month = db.get_setting(conn, "filler_edit_month", "0") == "1"
                if g.user["role"] != "admin":
                    if biz_date != today and not (
                        filler_month and biz_date.year == today.year and biz_date.month == today.month
                    ):
                        flash("只能改当天（管理员开启『本月可改』后可补录本月）。历史跨月需找管理员修改。", "error")
                        return redirect(url_for("today", store_id=store["id"]))
                    if db.is_locked(biz_date) and not (filler_month and biz_date.year == today.year and biz_date.month == today.month):
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
                compact = request.form.get("compact") == "1"
                note = (request.form.get("note") or "").strip()
                before = db.day_values(conn, store["id"], biz_date)
                db.save_daily(
                    conn,
                    store_id=store["id"],
                    biz_date=biz_date,
                    values=values,
                    user_id=g.user["id"],
                    compact=compact,
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
            compact = bool(report["compact"]) if report else False
            text = broadcast.render_broadcast(
                store["name"], biz_date, pairs, compact=compact
            )
            grouped = []
            for section in SECTIONS:
                items = []
                for code, name in section["metrics"]:
                    day, cum = pairs.get(code, (0, 0))
                    items.append(
                        {
                            "code": code,
                            "name": name,
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
                    g.user["role"] == "admin"
                    or biz_date == today
                    or (filler_month and biz_date.year == today.year and biz_date.month == today.month)
                ),
                grouped=grouped,
                report=report,
                broadcast_text=text,
                compact=compact,
                kpi_cards=kpi_cards,
                forecast=forecast,
            )

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
                grid[row["metric_code"]][row["biz_date"]] = int(row["day_value"] or 0)
                totals[row["metric_code"]] += int(row["day_value"] or 0)

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
            working_days = [
                d for d in all_days
                if date.fromisoformat(d).weekday() < 5
            ]
            coverage = (
                round(len(submitted_dates) / len(working_days) * 100)
                if working_days
                else 0
            )

            # 三个考核 KPI：从 kpi_targets 读目标（与月度考核页一致），区间累计
            from .metrics_seed import rollup_amount

            kpi_targets = db.list_kpi_targets(conn)
            kpi_cards = []
            for code, name, note in KPI_TARGETS:
                if code == "ai_contract":
                    total = totals.get("ai_contract", 0)
                else:
                    total = rollup_amount(totals, code)
                target = kpi_targets.get(code, 0)
                kpi_cards.append(
                    {
                        "code": code,
                        "name": name,
                        "note": note,
                        "total": total,
                        "target": target,
                        "progress": (total / target * 100) if target else None,
                        "left": max(0, target - total) if target else None,
                    }
                )

            # 指标行：按 SECTIONS 分组；KPI 成员标记高亮；普通指标目标读 metrics.monthly_target
            from .metrics_seed import SECTIONS, section_by_code

            rows = []
            for m in metrics:
                total = totals[m["code"]]
                month_target = int(m["monthly_target"] or 0)
                rows.append(
                    {
                        "code": m["code"],
                        "name": m["name"],
                        "section": m["section"],
                        "target": month_target,
                        "total": total,
                        "progress": (total / month_target * 100) if month_target else None,
                        "days": [grid[m["code"]].get(d, 0) for d in dates],
                        "avg": round(total / max(1, len(submitted_dates)), 1) if submitted_dates else 0,
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

            # 多店对比（管理员）：各店 KPI 区间合计 / 目标完成率 / 已交天数
            store_compare = []
            if g.user["role"] == "admin":
                for s in db.list_all_stores(conn):
                    sfacts = db.facts_in_range(conn, s["id"], start, end)
                    stot: Dict[str, int] = {}
                    for row in sfacts:
                        stot[row["metric_code"]] = stot.get(row["metric_code"], 0) + int(row["day_value"] or 0)
                    s_sub = {
                        row["biz_date"]
                        for row in conn.execute(
                            """
                            SELECT biz_date FROM daily_reports
                            WHERE store_id=? AND biz_date>=? AND biz_date<=?
                            """,
                            (s["id"], start.isoformat(), end.isoformat()),
                        )
                    }
                    scard = {"store": s, "submitted": len(s_sub & set(dates))}
                    for code, _n, _t in KPI_TARGETS:
                        if code == "ai_contract":
                            v = int(stot.get("ai_contract", 0) or 0)
                        else:
                            v = rollup_amount(stot, code)
                        scard[f"k_{code}"] = v
                        tg = kpi_targets.get(code, 0)
                        scard[f"p_{code}"] = (v / tg * 100) if tg else None
                    store_compare.append(scard)
                store_compare.sort(key=lambda x: -x.get("submitted", 0))

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
                working_days=len(working_days),
                kpi_cards=kpi_cards,
                store_compare=store_compare,
            )

    @app.route("/report.csv")
    @login_required
    def report_csv():
        with db.get_db() as conn:
            store, _stores = pick_store(conn, request.args.get("store_id"))
            if store is None:
                return Response("no store", status=400)
            start = parse_date(request.args.get("start"), db.today_local().replace(day=1))
            end = parse_date(request.args.get("end"), db.today_local())
            facts = db.facts_in_range(conn, store["id"], start, end)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["日期", "门店", "分类", "指标", "日值"])
            for row in facts:
                writer.writerow(
                    [row["biz_date"], store["name"], row["section"], row["name"], row["day_value"]]
                )
            data = buf.getvalue().encode("utf-8-sig")
            filename = f"{store['code']}_{start.isoformat()}_{end.isoformat()}.csv"
            return Response(
                data,
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
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

    @app.route("/board")
    @login_required
    def board():
        biz_date = parse_date(request.args.get("date"))
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            cards = db.dashboard_today(conn, [s["id"] for s in stores], biz_date)
            detail = []
            for card in cards:
                pairs = values_for_broadcast(conn, card["store"]["id"], biz_date)
                highs = []
                for code, name, _note in KPI_TARGETS:
                    if code == "ai_contract":
                        day, cum = pairs.get("ai_contract", (0, 0))
                    else:
                        day, cum = rollup_pair(pairs, code)
                    highs.append({"name": name, "day": day, "cum": cum})
                forecast = store_forecast(conn, card["store"], biz_date)
                detail.append({**card, "highlights": highs, "forecast": forecast})
            return render_template(
                "board.html",
                biz_date=biz_date,
                cards=detail,
                is_admin=g.user["role"] == "admin",
            )

    def bulletin_rows(conn, stores, biz_date: date):
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
            report = db.get_report(conn, store["id"], biz_date)
            rows.append(
                bulletin.build_row(
                    store,
                    day_ai=day_ai,
                    month_ai=month_ai,
                    day_bisuan=day_bisuan,
                    month_bisuan=month_bisuan,
                    submitted=report is not None,
                )
            )
        return bulletin.apply_scales(rows)

    @app.route("/bulletin")
    @admin_required
    def bulletin_page():
        biz_date = parse_date(request.args.get("date"))
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            rows = bulletin_rows(conn, stores, biz_date)
            copy_text = bulletin.tsv(rows, biz_date)
            return render_template(
                "bulletin.html",
                biz_date=biz_date,
                rows=rows,
                totals=bulletin.totals_row(rows) if rows else None,
                month_label=bulletin.month_label(biz_date),
                day_label=bulletin.day_label(biz_date),
                copy_text=copy_text,
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
            for store in stores:
                judged = store_forecast(conn, store, as_of)
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
        """审计日志：谁在什么时候把哪家店哪天的日报改成了什么。"""
        store_id = request.args.get("store_id", "")
        days = request.args.get("days", "7")
        try:
            days_int = max(1, min(int(days), 90))
        except ValueError:
            days_int = 7
        where = ""
        params: List[Any] = []
        if store_id and store_id.isdigit():
            where = "AND e.store_id=?"
            params.append(int(store_id))
        with db.get_db() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    f"""
                    SELECT e.*, s.name AS store_name, u.username AS user_name
                    FROM report_edits e
                    JOIN stores s ON s.id=e.store_id
                    JOIN users u ON u.id=e.user_id
                    WHERE e.edited_at >= datetime('now', '-{days_int} days')
                    {where}
                    ORDER BY e.id DESC
                    LIMIT 300
                    """,
                    params,
                )
            ]
            all_stores = db.list_all_stores(conn)
        for r in rows:
            import json as _json
            try:
                before = _json.loads(r["before_json"] or "{}")
                after = _json.loads(r["after_json"] or "{}")
            except ValueError:
                before, after = {}, {}
            r["diff"] = _build_diff(before, after)
            r["store_name"] = r["store_name"] or "?"
        return render_template(
            "edits.html",
            rows=rows,
            days=days_int,
            store_id=store_id,
            stores=all_stores,
        )

    @app.route("/bulletin.csv")
    @admin_required
    def bulletin_csv():
        biz_date = parse_date(request.args.get("date"))
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            rows = bulletin_rows(conn, stores, biz_date)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerows(bulletin.csv_rows(rows, biz_date))
            data = buf.getvalue().encode("utf-8-sig")
            filename = f"bulletin_{biz_date.isoformat()}.csv"
            return Response(
                data,
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

    @app.route("/admin")
    @login_required
    def admin():
        return redirect(url_for("settings", tab=request.args.get("tab") or "account"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        tab = settings_tab()
        with db.get_db() as conn:
            if request.method == "POST":
                action = request.form.get("action")
                tab = request.form.get("tab") or tab
                try:
                    if action == "change_pin":
                        change_own_pin()
                        flash("口令已改，下次用新口令登录", "ok")
                        return redirect(url_for("settings", tab="account"))
                    if g.user["role"] != "admin":
                        raise ValueError("需要管理员权限")
                    if action == "add_store":
                        name = (request.form.get("store_name") or "").strip()
                        code = (request.form.get("store_code") or "").strip()
                        if not name or not code:
                            raise ValueError("门店名和编码都要填")
                        db.create_store(
                            conn,
                            name,
                            code,
                            mobile_code=request.form.get("mobile_code") or "",
                            area_manager=request.form.get("area_manager") or "",
                            store_manager=request.form.get("store_manager") or "",
                            advisor_name=request.form.get("advisor_name") or "",
                            short_name=request.form.get("short_name") or "",
                        )
                        flash("门店已加", "ok")
                    elif action == "edit_store":
                        sid = int(request.form.get("store_id") or 0)
                        if not db.user_can_access_store(conn, g.user, sid):
                            raise ValueError("无权改这家店")
                        db.update_store_profile(
                            conn,
                            sid,
                            mobile_code=request.form.get("mobile_code") or "",
                            area_manager=request.form.get("area_manager") or "",
                            store_manager=request.form.get("store_manager") or "",
                            advisor_name=request.form.get("advisor_name") or "",
                        )
                        flash("门店档案已改", "ok")
                    elif action == "save_profiles":
                        for store in db.list_all_stores(conn):
                            sid = store["id"]
                            db.update_store_profile(
                                conn,
                                sid,
                                mobile_code=request.form.get(f"mobile_code_{sid}") or "",
                                area_manager=request.form.get(f"area_manager_{sid}") or "",
                                store_manager=request.form.get(f"store_manager_{sid}") or "",
                                advisor_name=request.form.get(f"advisor_name_{sid}") or "",
                            )
                        flash("门店档案已保存", "ok")
                    elif action == "add_user":
                        username = (request.form.get("username") or "").strip()
                        display = (request.form.get("display_name") or "").strip()
                        pin = request.form.get("pin") or ""
                        role = request.form.get("role") or "filler"
                        store_ids = [int(x) for x in request.form.getlist("store_ids")]
                        if not username or not display or not pin:
                            raise ValueError("账号、姓名、口令都要填")
                        min_len = db.FILLER_PIN_MIN if role == "filler" else db.ADMIN_PIN_MIN
                        if len(pin) < min_len:
                            raise ValueError(f"口令至少 {min_len} 位")
                        db.create_user(
                            conn,
                            username=username,
                            display_name=display,
                            pin=pin,
                            role=role,
                            store_ids=store_ids,
                        )
                        flash("人员已加", "ok")
                    elif action == "reset_pin":
                        uid = int(request.form.get("user_id") or 0)
                        pin = request.form.get("pin") or ""
                        target = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
                        min_len = db.FILLER_PIN_MIN if target and target["role"] == "filler" else db.ADMIN_PIN_MIN
                        if len(pin) < min_len:
                            raise ValueError(f"口令至少 {min_len} 位")
                        db.update_user_pin(conn, uid, pin)
                        flash("口令已改", "ok")
                    elif action == "set_stores":
                        uid = int(request.form.get("user_id") or 0)
                        target = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
                        if target is None:
                            raise ValueError("查无此人")
                        if target["role"] == "admin":
                            raise ValueError("管理员无需分配门店")
                        store_ids = [int(x) for x in request.form.getlist("store_ids") if str(x).strip().isdigit()]
                        db.set_user_stores(conn, uid, store_ids)
                        flash("门店权限已改", "ok")
                    elif action == "toggle_user":
                        uid = int(request.form.get("user_id") or 0)
                        active = request.form.get("active") == "1"
                        if uid == g.user["id"] and not active:
                            raise ValueError("不能停用自己")
                        db.set_user_active(conn, uid, active)
                        flash("人员状态已改", "ok")
                    elif action == "set_targets":
                        for code, _name, _note in KPI_TARGETS:
                            raw = request.form.get(f"t_{code}", "0")
                            try:
                                db.set_kpi_target(conn, code, int(raw or 0))
                            except ValueError:
                                pass
                        flash("月目标已保存", "ok")
                    elif action == "save_permissions":
                        filler_month = "1" if request.form.get("filler_edit_month") == "1" else "0"
                        db.set_setting(conn, "filler_edit_month", filler_month)
                        flash("权限设置已保存", "ok")
                    else:
                        flash("未知操作", "error")
                except Exception as exc:  # noqa: BLE001 — 表单校验用
                    flash(str(exc), "error")
                return redirect(url_for("settings", tab=tab))

            users = db.list_users(conn) if g.user["role"] == "admin" else []
            stores = db.list_all_stores(conn) if g.user["role"] == "admin" else []
            kpis = []
            if g.user["role"] == "admin":
                targets = db.list_kpi_targets(conn)
                kpis = [
                    {"code": code, "name": name, "note": note, "target": targets.get(code, 0)}
                    for code, name, note in KPI_TARGETS
                ]
            user_map = {u["id"]: db.user_store_ids(conn, u["id"]) for u in users}
            store_by_id = {s["id"]: s for s in stores}
            people = []
            for u in users:
                sids = user_map.get(u["id"]) or []
                assigned = [store_by_id[sid] for sid in sids if sid in store_by_id]
                labels = [store_label(s) for s in assigned]
                people.append(
                    {
                        "user": u,
                        "store_ids": sids,
                        "store_id": sids[0] if sids else 0,
                        "store_label": "全部门店" if u["role"] == "admin" else ("、".join(labels) if labels else "未分配"),
                    }
                )
            return render_template(
                "settings.html",
                tab=tab,
                users=users,
                people=people,
                stores=stores,
                kpis=kpis,
                user_map=user_map,
                store_label=store_label,
                filler_edit_month=db.get_setting(conn, "filler_edit_month", "0") == "1",
            )

    @app.context_processor
    def inject_now():
        return {"today_iso": db.today_local().isoformat(), "now": datetime.now(db.TZ)}

    return app


def _build_diff(before: Dict[str, int], after: Dict[str, int]) -> str:
    """把 before/after 值差异拼成可读文本，如「手机销量 1→7」"""
    from .metrics_seed import metric_name_map

    names = metric_name_map()
    parts = []
    keys = sorted(set(before) | set(after))
    for k in keys:
        b = int(before.get(k, 0) or 0)
        a = int(after.get(k, 0) or 0)
        if b == a:
            continue
        name = names.get(k, k)
        parts.append(f"{name} {b}→{a}")
    return "；".join(parts) or "（无变化）"


app = create_app()
