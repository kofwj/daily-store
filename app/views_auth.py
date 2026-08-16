"""登录/登出/首页/健康/入口跳转。"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, session, url_for

from . import db
from .helpers import default_home, login_required


def _lock_message(seconds: int) -> str:
    minutes = max(1, (int(seconds) + 59) // 60)
    return f"连续输错太多，请 {minutes} 分钟后再试"


def register_auth(app) -> None:
    @app.route("/health")
    def health():
        return {"ok": True, "service": "store-daily"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            pin = request.form.get("pin") or ""
            ip = (request.remote_addr or "").strip()
            with db.get_db() as conn:
                remaining = db.login_lock_remaining(conn, username, ip)
                if remaining > 0:
                    flash(_lock_message(remaining), "error")
                    return render_template("login.html")
                user = db.get_user_by_username(conn, username)
                if user and db.verify_pin(pin, user["pin_hash"]):
                    db.clear_login_failures(conn, username, ip)
                    session.clear()
                    session["user_id"] = user["id"]
                    session.permanent = True
                    if int(user["must_change_pin"] or 0):
                        return redirect(url_for("settings", tab="account"))
                    nxt = request.args.get("next") or default_home(user)
                    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                        nxt = default_home(user)
                    if user["role"] == "readonly" and nxt.startswith("/today"):
                        nxt = default_home(user)
                    return redirect(nxt)
                remaining = db.record_login_failure(conn, username, ip)
            if remaining > 0:
                flash(_lock_message(remaining), "error")
            else:
                flash("账号或口令不对", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/me")
    @login_required
    def me():
        return redirect(url_for("settings", tab="account"))

    @app.route("/")
    @login_required
    def home():
        return redirect(default_home())

    @app.route("/admin")
    @login_required
    def admin():
        return redirect(url_for("settings", tab=request.args.get("tab") or "account"))
