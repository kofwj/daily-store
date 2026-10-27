"""登录锁定、默认口令强制改密、备份校验、会话过期。"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from app import backup, db
from app.web import create_app


def test_production_rejects_missing_or_example_secret(tmp_db, monkeypatch):
    monkeypatch.delenv("STORE_DAILY_SECRET", raising=False)
    monkeypatch.delenv("STORE_DAILY_TESTING", raising=False)  # 明确生产态：不靠 sys.modules 探测，要测就删开关
    with pytest.raises(RuntimeError):
        create_app()
    monkeypatch.setenv("STORE_DAILY_SECRET", "replace-with-random-secret")
    with pytest.raises(RuntimeError):
        create_app()
    monkeypatch.setenv("STORE_DAILY_SECRET", "test-" + "b" * 48)
    assert create_app().config["SECRET_KEY"].startswith("test-")


def test_production_disables_template_auto_reload(tmp_db):
    assert create_app().config["TEMPLATES_AUTO_RELOAD"] is False
    assert create_app(testing=True).config["TEMPLATES_AUTO_RELOAD"] is True


def test_hot_settings_refresh_after_brand_save(tmp_db, client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    client.post(
        "/settings",
        data={
            "action": "save_brand",
            "tab": "brand",
            "brand_mark": "缓存",
            "brand_kicker": "缓存副标",
            "brand_title": "缓存日报",
        },
    )
    client.post("/logout")
    login = client.get("/login").get_data(as_text=True)
    assert "缓存日报" in login
    assert "缓存副标" in login


def test_login_locks_after_five_failures(client):
    body = ""
    for _ in range(5):
        resp = client.post(
            "/login", data={"username": "admin", "pin": "0000"}, follow_redirects=True
        )
        body = resp.get_data(as_text=True)
    assert "连续输错太多" in body
    locked = client.post(
        "/login", data={"username": "admin", "pin": "123456"}, follow_redirects=True
    )
    assert "连续输错太多" in locked.get_data(as_text=True)
    assert locked.request.path == "/login"


def test_default_pin_must_change_before_using_app(client):
    with db.get_db() as conn:
        conn.execute("UPDATE users SET must_change_pin=1 WHERE username='admin'")
    landed = client.post(
        "/login", data={"username": "admin", "pin": "123456"}, follow_redirects=True
    )
    assert landed.request.path == "/settings"
    assert "默认口令" in landed.get_data(as_text=True)
    blocked = client.get("/today", follow_redirects=True)
    assert blocked.request.path == "/settings"
    assert "请先改掉默认口令" in blocked.get_data(as_text=True)
    reuse = client.post(
        "/settings",
        data={
            "action": "change_pin",
            "tab": "account",
            "old_pin": "123456",
            "new_pin": "1234",
            "new_pin2": "1234",
        },
        follow_redirects=True,
    )
    assert "至少 8 位" in reuse.get_data(as_text=True)
    still_default = client.post(
        "/settings",
        data={
            "action": "change_pin",
            "tab": "account",
            "old_pin": "123456",
            "new_pin": "123456",
            "new_pin2": "123456",
        },
        follow_redirects=True,
    )
    assert "至少 8 位" in still_default.get_data(as_text=True)
    sequential = client.post(
        "/settings",
        data={
            "action": "change_pin",
            "tab": "account",
            "old_pin": "123456",
            "new_pin": "12345678",
            "new_pin2": "12345678",
        },
        follow_redirects=True,
    )
    assert "口令太弱" in sequential.get_data(as_text=True)
    ok = client.post(
        "/settings",
        data={
            "action": "change_pin",
            "tab": "account",
            "old_pin": "123456",
            "new_pin": "48291703",
            "new_pin2": "48291703",
        },
        follow_redirects=True,
    )
    assert "口令已改" in ok.get_data(as_text=True)
    today = client.get("/today", follow_redirects=True)
    assert today.request.path == "/today"


def test_admin_reset_pin_uses_default_without_manual_input(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        db.update_user_pin(conn, uid, "654321")
        conn.execute("UPDATE users SET must_change_pin=0 WHERE id=?", (uid,))
    page = client.post(
        "/settings",
        data={"action": "reset_pin", "tab": "people", "user_id": str(uid)},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "重置为默认 123456" in page
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT pin_hash, must_change_pin FROM users WHERE id=?", (uid,)
        ).fetchone()
    assert db.verify_pin("123456", row["pin_hash"])
    assert int(row["must_change_pin"]) == 1


def test_legacy_admin_pin_migrates_to_six_digits(tmp_db):
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        conn.execute(
            "UPDATE users SET pin_hash=?, must_change_pin=0 WHERE id=?",
            (db.hash_pin("1234"), uid),
        )
        conn.execute("DELETE FROM schema_migrations WHERE version=12")
    db.migrate()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT pin_hash, must_change_pin FROM users WHERE id=?", (uid,)
        ).fetchone()
    assert db.verify_pin("123456", row["pin_hash"])
    assert int(row["must_change_pin"]) == 1
    assert not db.verify_pin("1234", row["pin_hash"])


def test_xlsx_escapes_formula_like_strings():
    from io import BytesIO

    import openpyxl

    from app.helpers import xlsx_bytes

    book = openpyxl.load_workbook(BytesIO(xlsx_bytes(["名称"], [["=HYPERLINK(\"https://bad\")"], ["+cmd"], ["-1"], ["@x"]])))
    values = [book.active.cell(row, 1).value for row in range(2, 6)]
    assert values == ["'=HYPERLINK(\"https://bad\")", "'+cmd", "'-1", "'@x"]


def test_restore_rejects_sqlite_without_core_tables(tmp_db):
    path = tmp_db.parent / "fake.db"
    src = sqlite3.connect(str(path))
    src.execute("CREATE TABLE dummy(id INTEGER, blob BLOB)")
    src.execute("INSERT INTO dummy(id, blob) VALUES (1, ?)", (b"x" * 8192,))
    src.commit()
    src.close()
    try:
        backup.restore_bytes(path.read_bytes())
        raise AssertionError("should reject")
    except ValueError as exc:
        assert "缺表" in str(exc)


def test_login_log_uses_forwarded_ip_when_proxy_trusted(tmp_db, monkeypatch):
    monkeypatch.setenv("STORE_DAILY_TRUST_PROXY", "1")
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    client.post(
        "/login",
        data={"username": "admin", "pin": "123456"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    with db.get_db() as conn:
        ip = conn.execute(
            "SELECT ip FROM auth_events WHERE action='login' ORDER BY id DESC LIMIT 1"
        ).fetchone()["ip"]
    assert ip == "203.0.113.9"


def test_untrusted_proxy_ignores_forwarded_ip(tmp_db, monkeypatch):
    monkeypatch.setenv("STORE_DAILY_TRUST_PROXY", "0")
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    client.post(
        "/login",
        data={"username": "admin", "pin": "123456"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    with db.get_db() as conn:
        ip = conn.execute(
            "SELECT ip FROM auth_events WHERE action='login' ORDER BY id DESC LIMIT 1"
        ).fetchone()["ip"]
    assert ip != "203.0.113.9"


def test_successful_login_and_logout_are_logged(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    client.post("/logout", follow_redirects=True)
    # 登入 filler 账号产生一条 filler 登录与登出
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    client.post("/logout", follow_redirects=True)

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT action, username, role FROM auth_events ORDER BY id"
        ).fetchall()
    assert [(r["action"], r["username"], r["role"]) for r in rows] == [
        ("login", "admin", "admin"),
        ("logout", "admin", "admin"),
        ("login", "alpha", "filler"),
        ("logout", "alpha", "filler"),
    ]

    # 以管理员身份查看 /logins 验证筛选器真实有效性
    client.post("/login", data={"username": "admin", "pin": "123456"})

    # 1. 筛选 action=logout & role=filler：表格里应出现 <code>alpha</code>，不可出现 <code>admin</code>
    page_filler_logout = client.get("/logins?action=logout&role=filler").get_data(as_text=True)
    assert "<code>alpha</code>" in page_filler_logout
    assert "<code>admin</code>" not in page_filler_logout

    # 2. 筛选 action=login & role=admin：表格里应出现 <code>admin</code>，不可出现 <code>alpha</code>
    page_admin_login = client.get("/logins?action=login&role=admin").get_data(as_text=True)
    assert "<code>admin</code>" in page_admin_login
    assert "<code>alpha</code>" not in page_admin_login

    # 3. 筛选 q=nobody：无匹配记录
    page_empty = client.get("/logins?q=nobody").get_data(as_text=True)
    assert "没有登录记录" in page_empty
    assert "<code>admin</code>" not in page_empty
    assert "<code>alpha</code>" not in page_empty

    # 普通填报员越权访问 /logins 被拦截
    client.post("/logout", follow_redirects=True)
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    blocked = client.get("/logins", follow_redirects=True)
    assert "需要管理员权限" in blocked.get_data(as_text=True)


def test_session_lasts_24_hours(tmp_db):
    app = create_app()
    app.config["TESTING"] = True
    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=24)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with client.session_transaction() as sess:
        assert sess.permanent is True
        assert sess.get("user_id")


def test_policy_form_csrf_enforced_and_baked_token_works(tmp_db):
    # csrf_protect 在 TESTING 模式下关闭，用非测试 app 真实验证：
    # 缺 token 的保存会被拒，而服务端烘焙进表单的 _csrf_token 能保存成功。
    app = create_app()
    app.config["TESTING"] = False  # 关键：开启 CSRF 校验
    client = app.test_client()
    # 先 GET /login 让 session 生成 CSRF token，再用它登录（真实浏览器 base.html 会自动带上）
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess["_csrf_token"]
    client.post("/login", data={"username": "admin", "pin": "123456",
                                "_csrf_token": token})
    # 抓取 settings 页面里服务端烘焙进政策表单的 csrf token
    html = client.get("/settings?tab=policies").get_data(as_text=True)
    import re
    m = re.search(r'name="_csrf_token"\s+value="([0-9a-f]+)"', html)
    assert m, "政策表单必须烘焙 _csrf_token 隐藏字段"
    baked = m.group(1)
    # 不带 token → CSRF 拒绝
    r = client.post("/settings", data={"action": "save_policy", "tab": "policies",
                                        "title": "x", "body": "b"}, follow_redirects=True)
    assert "页面停留太久" in r.get_data(as_text=True)
    # 带服务端烘焙的 token → 成功（且不需 base.html 的 JS 注入）
    r = client.post("/settings", data={"action": "save_policy", "tab": "policies",
                                        "title": "csrf", "body": "ok",
                                        "_csrf_token": baked})
    assert "页面停留太久" not in r.get_data(as_text=True)
    with db.get_db() as conn:
        assert "csrf" in [x["title"] for x in conn.execute("SELECT title FROM policies")]


def test_security_headers_and_no_store(client):
    page = client.get("/login")
    assert page.headers.get("X-Frame-Options") == "DENY"
    assert page.headers.get("X-Content-Type-Options") == "nosniff"
    assert page.headers.get("Referrer-Policy") == "same-origin"
    csp = page.headers.get("Content-Security-Policy") or ""
    assert "frame-ancestors 'none'" in csp
    assert "no-store" in (page.headers.get("Cache-Control") or "")


def test_get_logout_does_not_clear_session(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    bounced = client.get("/logout")
    assert bounced.status_code == 302
    still_in = client.get("/today")
    assert still_in.status_code == 200
    assert still_in.request.path == "/today"


def test_idle_session_expires(tmp_db):
    import time

    from app.helpers import SESSION_IDLE_SECONDS
    from app.web import create_app

    app = create_app(testing=True)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with client.session_transaction() as sess:
        sess["_active_at"] = time.time() - SESSION_IDLE_SECONDS - 5
    expired = client.get("/today", follow_redirects=True)
    assert expired.request.path == "/login"


def test_login_locks_account_across_ips(client):
    for host in range(1, 4):
        env = {"REMOTE_ADDR": f"203.0.113.{host}"}
        for _ in range(5):
            client.post("/login", data={"username": "admin", "pin": "0000"}, environ_base=env)
    locked = client.post(
        "/login",
        data={"username": "admin", "pin": "123456"},
        environ_base={"REMOTE_ADDR": "203.0.113.50"},
        follow_redirects=True,
    )
    body = locked.get_data(as_text=True)
    assert "连续输错太多" in body
    assert "30 分钟后再试" in body
    with db.get_db() as conn:
        assert db.login_lock_remaining(conn, "admin", "203.0.113.50") >= 1700
    assert locked.request.path == "/login"


def test_unauth_rejects_huge_body(client):
    huge = client.post("/login", data={"username": "admin", "pin": "x", "pad": "a" * (70 * 1024)})
    assert huge.status_code == 413


def test_wsgi_refuses_testing_env(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("STORE_DAILY_TESTING", "1")
    sys.modules.pop("wsgi", None)
    with pytest.raises(RuntimeError, match="STORE_DAILY_TESTING"):
        importlib.import_module("wsgi")


def test_open_redirect_blocked(client):
    """/login?next= 协议相对的站外地址必须丢弃，回到 /today。"""
    resp = client.post(
        "/login?next=//evil.com",
        data={"username": "admin", "pin": "123456"},
        follow_redirects=True,
    )
    assert resp.request.path == "/today"


def test_reset_pin_kicks_old_session(tmp_db):
    """管理员重置口令后，被重置人的旧会话立即失效。"""
    victim = create_app(testing=True).test_client()
    victim.post("/login", data={"username": "alpha", "pin": "123456"})
    assert victim.get("/today", follow_redirects=False).status_code == 200
    admin_client = create_app(testing=True).test_client()
    admin_client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
    admin_client.post(
        "/settings",
        data={"action": "reset_pin", "tab": "people", "user_id": str(uid)},
        follow_redirects=True,
    )
    resp = victim.get("/today", follow_redirects=False)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    # 重置后的默认口令能重新登录（要求先改密，跳到账号页）
    victim.post("/login", data={"username": "alpha", "pin": "123456"})
    resp2 = victim.get("/today", follow_redirects=False)
    assert resp2.status_code == 302 and "/settings" in resp2.headers["Location"]


def test_csrf_referrer_follows_same_site_absolute_url(tmp_db):
    # Referer 几乎总是绝对 URL：同源的必须转成站内路径跳回原页，
    # 外站/协议相对的一律丢弃回 today（防开放重定向）。
    app = create_app()
    app.config["TESTING"] = False  # 开启 CSRF 校验
    client = app.test_client()
    client.get("/login")
    same_site = "http://localhost/settings?tab=backup"  # Flask 测试客户端默认 host
    r = client.post("/settings", data={"action": "bogus"}, headers={"Referer": same_site})
    assert r.status_code == 302
    assert r.headers["Location"] == "/settings?tab=backup"

    for evil in ("https://evil.example/x", "//evil.example/x", "http://localhost.evil.example/"):
        r = client.post("/settings", data={"action": "bogus"}, headers={"Referer": evil})
        assert r.status_code == 302
        loc = r.headers["Location"]
        assert loc.startswith("/") and not loc.startswith("//"), loc


def test_login_username_case_insensitive(client):
    """用户名大小写打错也能登录，认证与失败计数同一口径。"""
    resp = client.post("/login", data={"username": "ADMIN", "pin": "123456"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/today")


def test_create_user_rejects_case_variant_dup(tmp_db):
    """登录按不分大小写匹配，建号时就不能出现只差大小写的重名。"""
    with db.get_db() as conn:
        with pytest.raises(ValueError, match="用户名已存在"):
            db.create_user(
                conn, username="ADMIN", display_name="重复", pin="123456",
                role="filler", store_ids=[],
            )


def test_login_next_keeps_query_string(client):
    """会话过期被踢到登录页，登录后要带着 query string 回原页面。"""
    resp = client.get("/report?view=week", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "next=" in loc and "view%3Dweek" in loc, loc
    landed = client.post(
        "/login?next=%2Freport%3Fview%3Dweek",
        data={"username": "admin", "pin": "123456"},
        follow_redirects=True,
    )
    assert landed.request.path == "/report"
    assert landed.request.query_string == b"view=week"


def test_login_failure_recovery_and_timestamp_edge_cases(tmp_db, monkeypatch):
    """验证登录失败计数的过期重置、已锁状态更新及异常时间戳容错。"""
    with db.get_db() as conn:
        # 1. 正常记一次失败
        db.record_login_failure(conn, "bob", "198.51.100.1")

        # 2. 模拟锁定已过期（until <= now）：再次失败应重置 count = 1
        conn.execute(
            "UPDATE login_attempts SET locked_until='2020-01-01 00:00:00' WHERE key='ip:198.51.100.1'"
        )
        db.record_login_failure(conn, "bob", "198.51.100.1")
        row = conn.execute(
            "SELECT fail_count FROM login_attempts WHERE key='ip:198.51.100.1'"
        ).fetchone()
        assert row["fail_count"] == 1

        # 3. 模拟仍在锁定中（until > now）：再次失败应保留 locked_until 并 count >= limit
        future = (datetime.now(db.TZ) + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE login_attempts SET locked_until=? WHERE key='ip:198.51.100.1'",
            (future,),
        )
        rem = db.record_login_failure(conn, "bob", "198.51.100.1")
        assert rem > 0

        # 4. 模拟时间戳损坏：login_lock_remaining 与 record_login_failure 优雅容错
        conn.execute(
            "UPDATE login_attempts SET locked_until='corrupt-timestamp' WHERE key='ip:198.51.100.1'"
        )
        assert db.login_lock_remaining(conn, "bob", "198.51.100.1") == 0
        db.record_login_failure(conn, "bob", "198.51.100.1")

    # 5. verify_pin 遇到缺少 $ 的损坏哈希应安全返回 False
    assert not db.verify_pin("123456", "corrupt_hash_without_dollar")

    # 6. burn_pin_time 覆盖：验证不存在用户与错误口令均落到相同提示，并切实调用了 burn_pin_time 防枚举
    app = create_app(testing=True)
    c = app.test_client()
    resp_nonexistent = c.post(
        "/login", data={"username": "nonexistent_user", "pin": "123456"}, follow_redirects=True
    )
    assert resp_nonexistent.status_code == 200
    assert "账号或口令不对" in resp_nonexistent.get_data(as_text=True)

    resp_wrong_pin = c.post(
        "/login", data={"username": "admin", "pin": "wrong_pin"}, follow_redirects=True
    )
    assert resp_wrong_pin.status_code == 200
    assert "账号或口令不对" in resp_wrong_pin.get_data(as_text=True)

    # 变异防护：验证不存在用户登录时确实触发了 burn_pin_time 防时序枚举
    burned = False

    def fake_burn(pin: str) -> None:
        nonlocal burned
        burned = True

    monkeypatch.setattr(db, "burn_pin_time", fake_burn)
    c.post("/login", data={"username": "another_ghost", "pin": "0000"})
    assert burned, "未调用 burn_pin_time，防用户名枚举时序保护失效"

    # 7. is_weak_new_pin 8 位及以上整串同一字符（通过 db 门面调用）
    assert db.is_weak_new_pin("88888888")

