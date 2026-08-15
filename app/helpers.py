"""跨路由共享：登录/权限装饰器、门店/日期/播报/考核取数、分页、审计 diff。

原先是 web.py 里 create_app 内的闭包；抽成模块级函数，方便各路由模块复用，
也让 web.py 只负责装配。不含任何 @app.route。
"""

from __future__ import annotations

import re
from datetime import date
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app, flash, g, redirect, request, session, url_for

from . import broadcast, db, incentive
from .metrics_seed import rollup_amount

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_user() -> None:
    g.user = None
    uid = session.get("user_id")
    if not uid:
        return
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        g.user = row


def csrf_protect():
    """非安全方法必须有合法 CSRF token。测试环境（TESTING）关闭。"""
    if current_app.config.get("TESTING"):
        return None
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    token = session.get("_csrf_token")
    given = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not token or not given or not _compare_digest(token, given):
        flash("页面停留太久，操作校验失败，请刷新后重试。", "error")
        return redirect(request.referrer or url_for("today"))
    return None


def _compare_digest(a: str, b: str) -> bool:
    import secrets

    return secrets.compare_digest(a, b)


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


def broadcast_store_name(store) -> str:
    """群消息只用简称，没有简称才用全称。"""
    return store_label(store)


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


def incentive_rules(conn) -> Dict[str, int]:
    return incentive.rules_from(db.get_setting(conn, "incentive_rules", ""))


def broadcast_compact_sections(conn) -> List[str]:
    sections = []
    if db.get_setting(conn, "broadcast_compact", "1") == "1":
        sections.append("digital")
    if db.get_setting(conn, "broadcast_compact_family", "0") == "1":
        sections.append("family")
    return sections


def store_forecast(conn, store, as_of: date) -> Dict[str, Any]:
    month_vals = db.month_cum_through(conn, store["id"], as_of)
    ai = int(month_vals.get("ai_contract", 0) or 0)
    new_cut = rollup_amount(month_vals, "coin_cut")
    advisor_name = (store["advisor_name"] if "advisor_name" in store.keys() else "") or ""
    judged = incentive.judge(bool(advisor_name.strip()), ai, new_cut, incentive_rules(conn))
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


def build_diff(before: Dict[str, int], after: Dict[str, int]) -> str:
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


def pagination(raw_page: Optional[str], total: int, per_page: int = 50) -> Tuple[int, int]:
    """把请求里的 page 参数夹到合法范围，返回 (page, pages)。"""
    try:
        page = max(1, int(raw_page or "1"))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, -(-total // per_page))
    if page > pages:
        page = pages
    return page, pages
