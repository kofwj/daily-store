"""纠错功能回归测试：店员只改当天 / 锁定时间 / 审计记录 / 删除日报。"""

from datetime import date, datetime
from pathlib import Path

import pytest

from app import db
from app.web import create_app


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("STORE_DAILY_DB", str(path))
    monkeypatch.setenv("STORE_DAILY_DATA", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", Path(path))
    monkeypatch.setattr(db, "DATA_DIR", Path(tmp_path))
    db.init_db()
    return path


@pytest.fixture()
def client(tmp_db):
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _login(client, username, pin):
    return client.post("/login", data={"username": username, "pin": pin}, follow_redirects=True)


def _store_id(conn, code="haimen-jinhua"):
    return conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]


def test_filler_cannot_save_past_date(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch)
    today_d = date.today()
    past = today_d - __import__("datetime").timedelta(days=2)
    with db.get_db() as conn:
        sid = _store_id(conn)
    # 伪造：店员 POST 改历史日期，应被拒
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "5"},
        follow_redirects=True,
    )
    assert "只能改当天" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is None


def test_filler_month_switch_off_blocks_this_month_past(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch)
    with db.get_db() as conn:
        sid = _store_id(conn)
    # 本月 1 号（非当天），开关默认关 → 拒绝
    past = date.today().replace(day=1)
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "5"},
        follow_redirects=True,
    )
    assert "只能改当天" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is None


def test_filler_month_switch_on_allows_this_month(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch, username="admin", pin="1234")
    # 管理员开开关
    resp = client.post(
        "/settings",
        data={"action": "save_permissions", "tab": "permissions", "filler_edit_month": "1"},
        follow_redirects=True,
    )
    assert "权限设置已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_setting(conn, "filler_edit_month") == "1"
    # 店员登出、登录
    client.post("/logout")
    client.post("/login", data={"username": "jinhua", "pin": "123456"})
    with db.get_db() as conn:
        sid = _store_id(conn)
    past = date.today().replace(day=1)
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "3"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is not None


def test_admin_can_save_past_date(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch, username="admin", pin="1234")
    today_d = date.today()
    past = today_d - __import__("datetime").timedelta(days=1)
    with db.get_db() as conn:
        sid = _store_id(conn)
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "3"},
        follow_redirects=True,
    )
    assert b"speed" not in resp.data  # 不应有异常
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is not None


def test_locked_today_blocked_but_admin_ok(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch)
    today_d = date.today()
    # 模拟锁定时间后的 now
    monkeypatch.setattr(
        db, "is_locked", lambda biz_date, now=None: biz_date == today_d
    )
    with db.get_db() as conn:
        sid = _store_id(conn)
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    assert "已锁定" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, today_d) is None


def test_admin_override_lock(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch, username="admin", pin="1234")
    today_d = date.today()
    monkeypatch.setattr(db, "is_locked", lambda biz_date, now=None: biz_date == today_d)
    with db.get_db() as conn:
        sid = _store_id(conn)
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "2"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, today_d) is not None


def test_overwrite_records_audit(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch)
    today_d = date.today()
    with db.get_db() as conn:
        sid = _store_id(conn)
    # 第一次保存
    client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    # 覆盖保存
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "7"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        edits = list(conn.execute("SELECT * FROM report_edits"))
        assert len(edits) == 1
        assert edits[0]["note"] == "覆盖保存"


def test_delete_report_records_audit(tmp_db, monkeypatch):
    client = _client_auth(tmp_db, monkeypatch, username="admin", pin="1234")
    today_d = date.today()
    with db.get_db() as conn:
        sid = _store_id(conn)
    client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "4"},
        follow_redirects=True,
    )
    resp = client.post(
        "/report/delete",
        data={"store_id": str(sid), "date": today_d.isoformat()},
        follow_redirects=True,
    )
    assert "已删除" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, today_d) is None
        edits = list(conn.execute("SELECT * FROM report_edits ORDER BY id"))
        assert len(edits) == 1
        assert edits[0]["note"] == "删除日报"


def test_locked_helper():
    today_d = date.today()
    # 非当天永不锁
    assert db.is_locked(today_d - __import__("datetime").timedelta(days=1)) is False
    # 当天在锁定前不锁
    assert db.is_locked(today_d, now=datetime(2026, 8, 14, 22, 59)) is False
    # 当天在锁定后锁
    assert db.is_locked(today_d, now=datetime(2026, 8, 14, 23, 0)) is True
    assert db.is_locked(today_d, now=datetime(2026, 8, 14, 23, 30)) is True


def test_now_is_beijing_time():
    from datetime import datetime as dt, timezone

    out = db._now()
    utc = dt.now(timezone.utc)
    local = dt.fromisoformat(out)
    diff_h = (local - utc.replace(tzinfo=None)).total_seconds() / 3600
    # 允许 1 分钟内的执行抖动，但时差必须是 8 小时（北京时间）
    assert abs(diff_h - 8.0) < 0.05
    # today_local 与 UTC 日期最多差 1 天（北京时间可能已跨日）
    assert abs((db.today_local() - utc.date()).days) <= 1


def _client_auth(tmp_db, monkeypatch, username="jinhua", pin="123456"):
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    client.post("/login", data={"username": username, "pin": pin})
    return client