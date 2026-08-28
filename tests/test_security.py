"""登录锁定、默认口令强制改密、备份校验、会话过期。"""

import sqlite3
from datetime import timedelta

import pytest

from app import backup, db
from app.web import create_app


def test_production_rejects_missing_or_example_secret(monkeypatch):
    monkeypatch.delenv("STORE_DAILY_SECRET", raising=False)
    monkeypatch.delenv("STORE_DAILY_TESTING", raising=False)  # 明确生产态：不靠 sys.modules 探测，要测就删开关
    with pytest.raises(RuntimeError):
        create_app()
    monkeypatch.setenv("STORE_DAILY_SECRET", "replace-with-random-secret")
    with pytest.raises(RuntimeError):
        create_app()
    monkeypatch.setenv("STORE_DAILY_SECRET", "test-" + "b" * 48)
    assert create_app().config["SECRET_KEY"].startswith("test-")


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
    assert "至少 6 位" in reuse.get_data(as_text=True)
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
    assert "不能再用系统默认口令" in still_default.get_data(as_text=True)
    ok = client.post(
        "/settings",
        data={
            "action": "change_pin",
            "tab": "account",
            "old_pin": "123456",
            "new_pin": "567890",
            "new_pin2": "567890",
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
    from app import db_core

    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        conn.execute(
            "UPDATE users SET pin_hash=?, must_change_pin=0 WHERE id=?",
            (db.hash_pin("1234"), uid),
        )
        conn.execute("DELETE FROM schema_migrations WHERE version=12")
    db_core.migrate()
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
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT action, username, role FROM auth_events ORDER BY id"
        ).fetchall()
    assert [r["action"] for r in rows] == ["login"]
    assert rows[0]["username"] == "admin"
    page = client.get("/logins").get_data(as_text=True)
    assert "登录日志" in page
    assert "admin" in page
    assert "settings-nav" in page
    assert "备份恢复" in page
    client.get("/logout", follow_redirects=True)
    with db.get_db() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM auth_events ORDER BY id")]
    assert actions == ["login", "logout"]
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
