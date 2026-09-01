"""纠错功能回归测试：店员只改当天 / 锁定时间 / 审计记录 / 删除日报。"""

from datetime import date, datetime, timedelta

from app import db
from tests.conftest import store_id


def test_filler_cannot_save_past_date(filler_client):
    today_d = date.today()
    past = today_d - timedelta(days=2)
    sid = store_id()
    # 伪造：店员 POST 改历史日期，应被拒
    resp = filler_client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "5"},
        follow_redirects=True,
    )
    assert "只能改当天" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is None


def test_filler_month_switch_off_blocks_this_month_past(filler_client):
    sid = store_id()
    today_d = date.today()
    if today_d.day > 1:
        # 本月 1 号（非当天），开关默认关 → 拒绝
        past = today_d.replace(day=1)
        expect = "只能改当天"
    else:
        # 今天就是 1 号：本月中旬没有「本月过去日」，用昨天（跨月）验证往日仍不可改
        past = today_d - timedelta(days=1)
        expect = "只能改本月"
    resp = filler_client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "5"},
        follow_redirects=True,
    )
    assert expect in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is None


def test_filler_month_switch_still_rejects_future_date(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    client.post("/settings", data={"action": "save_permissions", "tab": "permissions", "filler_edit_month": "1"})
    client.post("/logout")
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    sid = store_id()
    future = date.today() + timedelta(days=1)
    response = client.post("/today", data={"store_id": sid, "date": future.isoformat(), "m_phone_sales": "1"}, follow_redirects=True)
    assert "未来日期" in response.get_data(as_text=True)


def test_filler_month_switch_on_allows_this_month(client):
    # 管理员开开关
    client.post("/login", data={"username": "admin", "pin": "123456"})
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
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    sid = store_id()
    past = date.today().replace(day=1)
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "3"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is not None


def test_admin_can_save_past_date(admin_client):
    today_d = date.today()
    past = today_d - timedelta(days=1)
    sid = store_id()
    resp = admin_client.post(
        "/today",
        data={"store_id": str(sid), "date": past.isoformat(), "m_phone_sales": "3"},
        follow_redirects=True,
    )
    assert b"speed" not in resp.data  # 不应有异常
    with db.get_db() as conn:
        assert db.get_report(conn, sid, past) is not None


def test_locked_today_blocked_but_admin_ok(filler_client, monkeypatch):
    today_d = date.today()
    # 模拟锁定时间后的 now
    monkeypatch.setattr(
        db, "is_locked", lambda biz_date, now=None: biz_date == today_d
    )
    sid = store_id()
    resp = filler_client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    assert "已锁定" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, today_d) is None


def test_admin_override_lock(admin_client, monkeypatch):
    today_d = date.today()
    monkeypatch.setattr(db, "is_locked", lambda biz_date, now=None: biz_date == today_d)
    sid = store_id()
    resp = admin_client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "2"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, today_d) is not None


def test_overwrite_records_audit(filler_client):
    today_d = date.today()
    sid = store_id()
    # 第一次保存
    filler_client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    # 覆盖保存
    resp = filler_client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "7"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        edits = list(conn.execute("SELECT * FROM report_edits"))
        assert len(edits) == 1
        assert edits[0]["note"] == "覆盖保存"


def test_delete_report_records_audit(admin_client):
    today_d = date.today()
    sid = store_id()
    admin_client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "4"},
        follow_redirects=True,
    )
    resp = admin_client.post(
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
    assert db.is_locked(today_d - timedelta(days=1)) is False
    # 当天在锁定前不锁
    assert db.is_locked(today_d, now=datetime(2026, 8, 14, 22, 59)) is False
    # 当天在锁定后锁
    assert db.is_locked(today_d, now=datetime(2026, 8, 14, 23, 0)) is True
    assert db.is_locked(today_d, now=datetime(2026, 8, 14, 23, 30)) is True


def test_month_switch_does_not_unlock_today(client, monkeypatch):
    """开启「本月可改」后，店员也不能改『今天』锁定后的数据——本月可改只放开本月过去日。"""
    client.post("/login", data={"username": "admin", "pin": "123456"})
    resp = client.post(
        "/settings",
        data={"action": "save_permissions", "tab": "permissions", "filler_edit_month": "1"},
        follow_redirects=True,
    )
    assert "权限设置已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_setting(conn, "filler_edit_month") == "1"
    client.post("/logout")
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    today_d = date.today()
    # 锁定今天
    monkeypatch.setattr(db, "is_locked", lambda biz_date, now=None: biz_date == today_d)
    sid = store_id()
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "9"},
        follow_redirects=True,
    )
    assert "已锁定" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, today_d) is None


def test_admin_can_fix_one_cell(admin_client):
    today_d = date.today()
    sid = store_id()
    admin_client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "4", "m_ai_contract": "2"},
        follow_redirects=True,
    )
    resp = admin_client.post(
        "/report/cell",
        data={
            "store_id": str(sid),
            "date": today_d.isoformat(),
            "metric_code": "phone_sales",
            "value": "9",
            "view": "month",
        },
        follow_redirects=True,
    )
    assert "已校准" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        vals = db.day_values(conn, sid, today_d)
        assert vals["phone_sales"] == 9
        assert vals["ai_contract"] == 2
        edits = list(conn.execute("SELECT note FROM report_edits"))
        assert any(row["note"] == "校准单元格" for row in edits)


def test_filler_cannot_fix_cell(filler_client):
    today_d = date.today()
    sid = store_id()
    filler_client.post(
        "/today",
        data={"store_id": str(sid), "date": today_d.isoformat(), "m_phone_sales": "4"},
        follow_redirects=True,
    )
    resp = filler_client.post(
        "/report/cell",
        data={
            "store_id": str(sid),
            "date": today_d.isoformat(),
            "metric_code": "phone_sales",
            "value": "99",
        },
        follow_redirects=True,
    )
    assert "需要管理员权限" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.day_values(conn, sid, today_d)["phone_sales"] == 4


def test_now_is_beijing_time():
    from datetime import datetime as dt
    from datetime import timezone

    out = db._now()
    utc = dt.now(timezone.utc)
    local = dt.fromisoformat(out)
    diff_h = (local - utc.replace(tzinfo=None)).total_seconds() / 3600
    # 允许 1 分钟内的执行抖动，但时差必须是 8 小时（北京时间）
    assert abs(diff_h - 8.0) < 0.05
    # today_local 与 UTC 日期最多差 1 天（北京时间可能已跨日）
    assert abs((db.today_local() - utc.date()).days) <= 1


def test_pick_store_invalid_id_prompts(admin_client):
    """/today 带非法 store_id 时给明确提示，而不是 500。"""
    page = admin_client.get("/today?store_id=zzz")
    assert "店号无效" in page.get_data(as_text=True)
