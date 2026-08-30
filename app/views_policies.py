"""政策说明：前台阅读 / 已读确认。后台改写走设置页。"""

from __future__ import annotations

from flask import flash, g, redirect, render_template, request, url_for

from . import db
from .helpers import login_required, parse_int


def register_policies(app) -> None:
    @app.route("/policies")
    @login_required
    def policies_page():
        raw = request.args.get("id") or ""
        with db.get_db() as conn:
            items = db.list_policies(conn, active_only=True)
            acks = db.ack_map(conn, g.user["id"])
            require = db.policy_require_read(conn)
        current = None
        if items:
            want = parse_int(raw, 0)
            current = next((p for p in items if p["id"] == want), items[0])
        unread_ids = {p["id"] for p in items if int(acks.get(p["id"], 0)) < int(p["version"])}
        diff_html = ""
        read_status = []
        if g.user["role"] == "admin":
            with db.get_db() as conn:
                read_status = db.policy_read_status(conn)
        if current and int(current["version"]) > 1:
            with db.get_db() as conn:
                prev = db.previous_revision_body(conn, current["id"], current["version"])
            if prev:
                diff_html = db.render_policy_diff(prev, current["body"])
        return render_template(
            "policies.html",
            items=items,
            current=current,
            acks=acks,
            unread_ids=unread_ids,
            require=require,
            diff_html=diff_html,
            read_status=read_status,
        )

    @app.route("/policies/ack", methods=["POST"])
    @login_required
    def policy_ack():
        raw = request.form.get("policy_id") or ""
        try:
            pid = int(raw)
        except ValueError:
            flash("政策参数不对", "error")
            return redirect(url_for("policies_page"))
        with db.get_db() as conn:
            try:
                db.mark_policy_read(conn, g.user["id"], pid)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("policies_page"))
        flash("已确认阅读", "ok")
        return redirect(url_for("policies_page", id=pid))
