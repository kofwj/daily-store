"""登录锁定、默认口令强制改密、备份校验、会话过期。"""

import sqlite3
from datetime import timedelta

from app import backup, db
from app.web import create_app


def test_login_locks_after_five_failures(client):
    body = ""
    for _ in range(5):
        resp = client.post(
            "/login", data={"username": "admin", "pin": "0000"}, follow_redirects=True
        )
        body = resp.get_data(as_text=True)
    assert "连续输错太多" in body
    locked = client.post(
        "/login", data={"username": "admin", "pin": "1234"}, follow_redirects=True
    )
    assert "连续输错太多" in locked.get_data(as_text=True)
    assert locked.request.path == "/login"


def test_default_pin_must_change_before_using_app(client):
    with db.get_db() as conn:
        conn.execute("UPDATE users SET must_change_pin=1 WHERE username='admin'")
    landed = client.post(
        "/login", data={"username": "admin", "pin": "1234"}, follow_redirects=True
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
            "old_pin": "1234",
            "new_pin": "1234",
            "new_pin2": "1234",
        },
        follow_redirects=True,
    )
    assert "不能再用系统默认口令" in reuse.get_data(as_text=True)
    ok = client.post(
        "/settings",
        data={
            "action": "change_pin",
            "tab": "account",
            "old_pin": "1234",
            "new_pin": "5678",
            "new_pin2": "5678",
        },
        follow_redirects=True,
    )
    assert "口令已改" in ok.get_data(as_text=True)
    today = client.get("/today", follow_redirects=True)
    assert today.request.path == "/today"


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


def test_session_lasts_24_hours(tmp_db):
    app = create_app()
    app.config["TESTING"] = True
    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=24)
    client = app.test_client()
    client.post("/login", data={"username": "admin", "pin": "1234"})
    with client.session_transaction() as sess:
        assert sess.permanent is True
        assert sess.get("user_id")
