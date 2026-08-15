"""登录/登出/首页/健康/入口跳转。"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, session, url_for

from . import db
from .helpers import login_required


def register_auth(app) -> None:
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

    @app.route("/me")
    @login_required
    def me():
        return redirect(url_for("settings", tab="account"))

    @app.route("/")
    @login_required
    def home():
        return redirect(url_for("today"))

    @app.route("/admin")
    @login_required
    def admin():
        return redirect(url_for("settings", tab=request.args.get("tab") or "account"))
