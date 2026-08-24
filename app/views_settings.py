"""设置页：账号/门店/人员/目标/权限/播报/考核规则。"""

from __future__ import annotations

import json

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import backup, bulletin, db, incentive, invoice
from .helpers import (
    admin_required,
    ascii_filename,
    brand_settings,
    company_names,
    incentive_rules,
    login_required,
    parse_brand_form,
    parse_company_names,
    review_template_setting,
    store_label,
)
from .metrics_seed import KPI_TARGETS


def _settings_tab() -> str:
    tab = request.values.get("tab") or "account"
    allowed = {
        "account",
        "people",
        "stores",
        "targets",
        "permissions",
        "broadcast",
        "rules",
        "review",
        "invoice",
        "brand",
        "backup",
        "policies",
    }
    if tab not in allowed:
        return "account"
    if g.user["role"] != "admin" and tab != "account":
        return "account"
    if int(g.user["must_change_pin"] or 0) and tab != "account":
        return "account"
    return tab


def _people_redirect():
    return redirect(url_for("settings", tab="people"))


def _store_redirect(store_id=None):
    sid = store_id or request.form.get("back_store_id") or request.form.get("store_id")
    if sid and str(sid).strip() and str(sid) != "new":
        return redirect(url_for("settings", tab="stores", store_id=sid))
    return redirect(url_for("settings", tab="stores"))


def _change_own_pin() -> None:
    old = request.form.get("old_pin") or ""
    new = request.form.get("new_pin") or ""
    again = request.form.get("new_pin2") or ""
    min_len = db.FILLER_PIN_MIN
    if len(new) < min_len:
        raise ValueError(f"新口令至少 {min_len} 位")
    if new != again:
        raise ValueError("两次新口令不一致")
    if db.is_weak_new_pin(new):
        raise ValueError("新口令不能再用系统默认口令")
    if not db.verify_pin(old, g.user["pin_hash"]):
        raise ValueError("当前口令不对")
    with db.get_db() as conn:
        db.update_user_pin(conn, g.user["id"], new)


def register_settings(app) -> None:
    @app.route("/settings/backup/<name>")
    @admin_required
    def settings_backup_download(name):
        from pathlib import Path

        safe = Path(name).name
        dest = backup.backup_dir() / safe
        if not dest.is_file() or not safe.startswith(backup.BACKUP_PREFIX):
            return Response("not found", status=404)
        return Response(
            dest.read_bytes(),
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={ascii_filename(safe)}"},
        )

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        tab = _settings_tab()
        with db.get_db() as conn:
            if request.method == "POST":
                action = request.form.get("action")
                tab = _settings_tab()
                try:
                    if action == "change_pin":
                        _change_own_pin()
                        flash("口令已改，下次用新口令登录", "ok")
                        return redirect(url_for("settings", tab="account"))
                    if int(g.user["must_change_pin"] or 0):
                        raise ValueError("请先改掉默认口令")
                    if g.user["role"] != "admin":
                        raise ValueError("需要管理员权限")
                    if action == "add_store":
                        name = (request.form.get("store_name") or "").strip()
                        short_name = (request.form.get("short_name") or "").strip()
                        if not name or not short_name:
                            raise ValueError("店名和简称都要填")
                        new_store_id = db.create_store(
                            conn,
                            name,
                            mobile_code=request.form.get("mobile_code") or "",
                            area_manager=request.form.get("area_manager") or "",
                            store_manager=request.form.get("store_manager") or "",
                            advisor_name=request.form.get("advisor_name") or "",
                            short_name=short_name,
                            region_group=request.form.get("region_group") or "通泰",
                            city=request.form.get("city") or "南通市",
                            store_grade=request.form.get("store_grade") or "A",
                            ai_target=int(request.form.get("ai_target") or 10),
                            invoice_name=request.form.get("invoice_name") or "",
                            lease_area=request.form.get("lease_area") or "",
                            lease_address=request.form.get("lease_address") or "",
                            lease_period=request.form.get("lease_period") or "",
                        )
                        flash("门店已加", "ok")
                        return redirect(url_for("settings", tab="stores", store_id=new_store_id))
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
                            region_group=request.form.get("region_group") or "",
                            city=request.form.get("city") or "",
                            store_grade=request.form.get("store_grade") or "A",
                            ai_target=int(request.form.get("ai_target") or 10),
                            invoice_name=request.form.get("invoice_name") or "",
                            lease_area=request.form.get("lease_area") or "",
                            lease_address=request.form.get("lease_address") or "",
                            lease_period=request.form.get("lease_period") or "",
                        )
                        flash("门店档案已改", "ok")
                        return redirect(url_for("settings", tab="stores", store_id=sid))
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
                                region_group=request.form.get(f"region_group_{sid}") or store["region_group"],
                                city=request.form.get(f"city_{sid}") or store["city"],
                            )
                        flash("门店档案已保存", "ok")
                    elif action == "add_user":
                        username = (request.form.get("username") or "").strip()
                        display = (request.form.get("display_name") or "").strip()
                        pin = request.form.get("pin") or ""
                        kind = request.form.get("role") or "filler"
                        if kind not in ("filler", "readonly", "manager", "area"):
                            raise ValueError("只支持填报员或只读账号")
                        if kind == "area":
                            role = "readonly"
                            scope = (request.form.get("scope") or "").strip()
                            if not scope:
                                raise ValueError("区域经理没填区域经理姓名")
                            store_ids = []
                        elif kind in ("readonly", "manager"):
                            role = "readonly"
                            scope = ""
                            store_ids = [int(x) for x in request.form.getlist("store_ids") if str(x).strip().isdigit()]
                            if not store_ids:
                                raise ValueError("店长要选一家店")
                        else:
                            role = "filler"
                            scope = ""
                            store_ids = [int(x) for x in request.form.getlist("store_ids") if str(x).strip().isdigit()]
                        if not username or not display or not pin:
                            raise ValueError("账号、姓名、口令都要填")
                        min_len = db.FILLER_PIN_MIN
                        if len(pin) < min_len:
                            raise ValueError(f"口令至少 {min_len} 位")
                        db.create_user(
                            conn,
                            username=username,
                            display_name=display,
                            pin=pin,
                            role=role,
                            store_ids=store_ids,
                            scope=scope,
                        )
                        flash("账号已加", "ok")
                        return _people_redirect()
                    elif action == "reset_pin":
                        uid = int(request.form.get("user_id") or 0)
                        target = conn.execute(
                            "SELECT id, role, display_name FROM users WHERE id=?", (uid,)
                        ).fetchone()
                        if target is None:
                            raise ValueError("查无此人")
                        pin = (
                            db.DEFAULT_ADMIN_PIN
                            if target["role"] == "admin"
                            else db.DEFAULT_FILLER_PIN
                        )
                        db.update_user_pin(conn, uid, pin)
                        name = target["display_name"] or target["id"]
                        flash(
                            f"已把 {name} 的口令重置为默认 {pin}，下次登录必须改掉。",
                            "ok",
                        )
                        if (request.form.get("tab") or "") in ("people", "stores"):
                            return _people_redirect()
                    elif action == "set_stores":
                        uid = int(request.form.get("user_id") or 0)
                        target = conn.execute("SELECT role, scope FROM users WHERE id=?", (uid,)).fetchone()
                        if target is None:
                            raise ValueError("查无此人")
                        if target["role"] == "admin":
                            raise ValueError("管理员无需分配门店")
                        if target["role"] == "readonly" and request.form.get("bind") == "area":
                            scope = (request.form.get("scope") or "").strip()
                            if not scope:
                                raise ValueError("区域经理没填区域经理姓名")
                            db.set_user_scope(conn, uid, scope)
                            db.set_user_stores(conn, uid, [])
                        else:
                            store_ids = [int(x) for x in request.form.getlist("store_ids") if str(x).strip().isdigit()]
                            db.set_user_stores(conn, uid, store_ids)
                            if target["role"] == "readonly":
                                db.set_user_scope(conn, uid, "")
                        flash("门店权限已改", "ok")
                        if (request.form.get("tab") or "") in ("people", "stores"):
                            return _people_redirect()
                    elif action == "toggle_user":
                        uid = int(request.form.get("user_id") or 0)
                        active = request.form.get("active") == "1"
                        if uid == g.user["id"] and not active:
                            raise ValueError("不能停用自己")
                        db.set_user_active(conn, uid, active)
                        flash("账号状态已改", "ok")
                        if (request.form.get("tab") or "") in ("people", "stores"):
                            return _people_redirect()
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
                        db.set_policy_require_read(conn, request.form.get("policy_require_read") == "1")
                        flash("权限设置已保存", "ok")
                    elif action == "save_policy":
                        raw_id = (request.form.get("policy_id") or "").strip()
                        pid = int(raw_id) if raw_id.isdigit() else None
                        db.save_policy(
                            conn,
                            title=request.form.get("title") or "",
                            body=request.form.get("body") or "",
                            sort_order=int(request.form.get("sort_order") or 0),
                            active=request.form.get("active") == "1",
                            user_id=g.user["id"],
                            policy_id=pid,
                        )
                        flash("政策已保存", "ok")
                        return redirect(url_for("settings", tab="policies"))
                    elif action == "toggle_policy":
                        pid = int(request.form.get("policy_id") or 0)
                        db.set_policy_active(conn, pid, request.form.get("active") == "1")
                        flash("政策状态已改", "ok")
                        return redirect(url_for("settings", tab="policies"))
                    elif action == "delete_policy":
                        pid = int(request.form.get("policy_id") or 0)
                        db.delete_policy(conn, pid)
                        flash("政策已删除", "ok")
                        return redirect(url_for("settings", tab="policies"))
                    elif action == "restore_policy":
                        pid = int(request.form.get("policy_id") or 0)
                        ver = int(request.form.get("version") or 0)
                        db.restore_policy_revision(conn, pid, ver, user_id=g.user["id"])
                        flash(f"已恢复为第 {ver} 版，现内容会再记一版", "ok")
                        return redirect(url_for("settings", tab="policies", policy_id=pid))
                    elif action == "save_brand":
                        brand = parse_brand_form(request.form)
                        for key, value in brand.items():
                            db.set_setting(conn, f"brand_{key}", value)
                        flash("登录页标题已保存", "ok")
                    elif action == "save_review":
                        names = parse_company_names(request.form)
                        for key, value in names.items():
                            db.set_setting(conn, f"org_name_{key}", value)
                        body = (request.form.get("review_template") or "").rstrip()
                        db.set_setting(conn, "review_template", body)
                        db.set_setting(conn, "review_preset", "custom" if body.strip() else "")
                        flash("复盘模板和公司名已保存", "ok")
                    elif action == "save_invoice":
                        invoice.save_invoice_settings(conn, request.form)
                        flash("开票主体已保存", "ok")
                    elif action == "save_broadcast":
                        compact = "1" if request.form.get("broadcast_compact") == "1" else "0"
                        family = "1" if request.form.get("broadcast_compact_family") == "1" else "0"
                        db.set_setting(conn, "broadcast_compact", compact)
                        db.set_setting(conn, "broadcast_compact_family", family)
                        flash("播报设置已保存", "ok")
                    elif action == "save_wecom":
                        db.set_setting(conn, "wecom_global", (request.form.get("wecom_global") or "").strip())
                        for city in ("南通市", "泰州市"):
                            db.set_setting(conn, f"wecom_city_{city}", (request.form.get(f"wecom_city_{city}") or "").strip())
                        flash("企业微信群机器人已保存", "ok")
                    elif action == "test_wecom":
                        from . import wecom
                        url = (request.form.get("test_url") or "").strip()
                        ok, msg = wecom.send_test(conn, url)
                        flash(msg, "ok" if ok else "error")
                    elif action == "make_backup":
                        path = backup.snapshot("manual")
                        flash(f"已备份 {path.name}", "ok")
                        return redirect(url_for("settings", tab="backup"))
                    elif action == "restore_named":
                        name = (request.form.get("backup_name") or "").strip()
                        safety = backup.restore_named(name)
                        flash(f"已用 {name} 恢复。恢复前现场另存为 {safety.name}", "ok")
                        return redirect(url_for("settings", tab="backup"))
                    elif action == "restore_upload":
                        uploaded = request.files.get("backup_file")
                        if uploaded is None or not uploaded.filename:
                            raise ValueError("请先选择备份文件")
                        data = uploaded.read()
                        safety = backup.restore_bytes(data)
                        flash(f"已用上传文件恢复。恢复前现场另存为 {safety.name}", "ok")
                        return redirect(url_for("settings", tab="backup"))
                    elif action == "save_rules":
                        defaults = incentive.DEFAULTS
                        rules = {}
                        for key in defaults:
                            raw = request.form.get(f"r_{key}", "").strip()
                            try:
                                rules[key] = max(0, int(raw or defaults[key]))
                            except ValueError:
                                rules[key] = defaults[key]
                        db.set_setting(conn, "incentive_rules", json.dumps(rules, ensure_ascii=False))
                        flash("考核规则已保存，立即生效", "ok")
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
            grouped_cities = {}
            city_order = []
            for s in stores:
                city = (s["city"] or "").strip() or "未分地市"
                if city not in grouped_cities:
                    grouped_cities[city] = []
                    city_order.append(city)
                grouped_cities[city].append(s)
            store_groups = [{"city": city, "stores": grouped_cities[city]} for city in city_order]
            current_store = None
            raw_sid = (request.args.get("store_id") or request.form.get("store_id") or "").strip()
            if raw_sid != "new" and stores:
                try:
                    sid = int(raw_sid) if raw_sid else 0
                except ValueError:
                    sid = 0
                current_store = store_by_id.get(sid) or stores[0]
            people = []
            people_by_store = {s["id"]: [] for s in stores}
            area_by_store = {s["id"]: [] for s in stores}
            area_people = []
            unassigned_people = []
            for u in users:
                if u["role"] == "admin":
                    continue
                sids = user_map.get(u["id"]) or []
                assigned = [store_by_id[sid] for sid in sids if sid in store_by_id]
                labels = [store_label(s) for s in assigned]
                item = {
                    "user": u,
                    "store_ids": sids,
                    "store_id": sids[0] if sids else 0,
                    "store_label": "、".join(labels) if labels else "未分配",
                }
                people.append(item)
                if u["role"] == "readonly" and (u["scope"] or "").strip():
                    item["store_label"] = "区域：" + (u["scope"] or "").strip()
                    area_people.append(item)
                    for s in stores:
                        if (s["area_manager"] or "").strip() == (u["scope"] or "").strip():
                            area_by_store[s["id"]].append(item)
                    continue
                if not sids:
                    unassigned_people.append(item)
                    continue
                for sid in sids:
                    if sid in people_by_store:
                        people_by_store[sid].append(item)
            return render_template(
                "settings.html",
                tab=tab,
                users=users,
                people=people,
                people_by_store=people_by_store,
                area_by_store=area_by_store,
                area_people=area_people,
                unassigned_people=unassigned_people,
                stores=stores,
                store_groups=store_groups,
                current_store=current_store,
                kpis=kpis,
                user_map=user_map,
                store_label=store_label,
                filler_edit_month=db.get_setting(conn, "filler_edit_month", "0") == "1",
                policy_require_read=db.policy_require_read(conn),
                policies=db.list_policies(conn) if g.user["role"] == "admin" else [],
                policy_edit=(
                    db.get_policy(conn, int(request.args.get("policy_id")))
                    if (request.args.get("policy_id") or "").isdigit()
                    else None
                ),
                policy_revisions=(
                    db.list_revisions(conn, int(request.args.get("policy_id")))
                    if (request.args.get("policy_id") or "").isdigit()
                    else []
                ),
                broadcast_compact=db.get_setting(conn, "broadcast_compact", "1") == "1",
                broadcast_compact_family=db.get_setting(conn, "broadcast_compact_family", "0") == "1",
                wecom_global=db.get_setting(conn, "wecom_global", ""),
                wecom_cities={
                    "南通市": db.get_setting(conn, "wecom_city_南通市", ""),
                    "泰州市": db.get_setting(conn, "wecom_city_泰州市", ""),
                },
                incentive_rules=incentive_rules(conn),
                brand_form=brand_settings(conn),
                company_form=company_names(conn),
                review_template=review_template_setting(conn),
                review_presets=bulletin.REVIEW_PRESETS,
                invoice_parties=invoice.invoice_parties(conn),
                invoice_handler=invoice.invoice_handler(conn),
                backups=backup.list_backups() if g.user["role"] == "admin" else [],
            )
