"""权限规则：daily_edit_gate 判定矩阵 + 门店写入校验回归。

gate 是纯函数，直接测矩阵；pick_store 的「写路径拒绝回退」走 HTTP 回归，
确认店员 POST 别家店的 store_id 时被拒、且不会静默写进第一家可见店。
"""

from datetime import date

from app import db, policy
from tests.conftest import store_id

TODAY = date(2026, 8, 14)
FUTURE = date(2026, 8, 15)
PAST = date(2026, 8, 13)   # 本月过去日
CROSS = date(2026, 7, 31)  # 上月（跨月）


def test_admin_always_allowed():
    for biz_date in (TODAY, PAST, FUTURE, CROSS):
        gate = policy.daily_edit_gate("admin", biz_date, TODAY, filler_month=False)
        assert gate.allowed
        assert gate.reason == ""


def test_readonly_never_allowed():
    gate = policy.daily_edit_gate("readonly", TODAY, TODAY, filler_month=False)
    assert not gate.allowed
    assert "只读" in gate.reason


def test_filler_future_rejected_even_with_switch():
    gate = policy.daily_edit_gate("filler", FUTURE, TODAY, filler_month=True, locked=False)
    assert not gate.allowed
    assert "未来" in gate.reason


def test_filler_cross_month_rejected():
    gate = policy.daily_edit_gate("filler", CROSS, TODAY, filler_month=True)
    assert not gate.allowed
    assert "本月" in gate.reason


def test_filler_past_requires_switch():
    off = policy.daily_edit_gate("filler", PAST, TODAY, filler_month=False)
    assert not off.allowed
    assert "只能改当天" in off.reason
    on = policy.daily_edit_gate("filler", PAST, TODAY, filler_month=True)
    assert on.allowed


def test_filler_today_allowed_by_default():
    gate = policy.daily_edit_gate("filler", TODAY, TODAY, filler_month=False)
    assert gate.allowed


def test_city_and_admin_rules_do_not_depend_on_filler_month():
    # city 与 admin 的「本月可改」开关都不需要开
    assert policy.daily_edit_gate("city", PAST, TODAY, filler_month=False).allowed
    assert policy.daily_edit_gate("admin", PAST, TODAY, filler_month=False).allowed


def test_city_past_and_today_open_but_future_and_cross_rejected():
    assert policy.daily_edit_gate("city", TODAY, TODAY, filler_month=False).allowed
    assert policy.daily_edit_gate("city", PAST, TODAY, filler_month=False).allowed
    assert not policy.daily_edit_gate("city", CROSS, TODAY, filler_month=False).allowed
    assert not policy.daily_edit_gate("city", FUTURE, TODAY, filler_month=False).allowed


def test_city_ignores_today_lock():
    gate = policy.daily_edit_gate("city", TODAY, TODAY, filler_month=False, locked=True)
    assert gate.allowed


def test_filler_today_locked_only_when_locked_flag():
    open_gate = policy.daily_edit_gate("filler", TODAY, TODAY, filler_month=True, locked=False)
    assert open_gate.allowed
    locked_gate = policy.daily_edit_gate("filler", TODAY, TODAY, filler_month=True, locked=True)
    assert not locked_gate.allowed
    assert "锁定" in locked_gate.reason


def test_filler_past_not_affected_by_lock_flag():
    # 非今天日期不看 locked（与 db.is_locked 口径一致）
    gate = policy.daily_edit_gate("filler", PAST, TODAY, filler_month=True, locked=True)
    assert gate.allowed


def test_filler_post_other_stores_id_refused(filler_client):
    """店员 POST 别家店的 store_id：拒绝保存，且不静默写进自己的店。"""
    alpha = store_id("store-alpha")
    beta = store_id("store-beta")
    today_d = db.today_local()
    resp = filler_client.post(
        "/today",
        data={"store_id": str(beta), "date": today_d.isoformat(), "m_phone_sales": "5"},
        follow_redirects=True,
    )
    assert "没有这家店" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, beta, today_d) is None
        assert db.get_report(conn, alpha, today_d) is None


def test_filler_deal_post_other_stores_id_refused(filler_client):
    """触客页 POST 别家店的 store_id：拒绝保存。"""
    beta = store_id("store-beta")
    resp = filler_client.post(
        "/deal",
        data={"store_id": str(beta), "model": "X100", "phone": "13800000000", "closed": "1"},
        follow_redirects=True,
    )
    assert "没有这家店" in resp.get_data(as_text=True)


def test_filler_get_other_stores_id_still_falls_back(filler_client):
    """只读浏览保持原行为：带无权 store_id 访问时回退到第一家可见店。"""
    alpha = store_id("store-alpha")
    beta = store_id("store-beta")
    resp = filler_client.get(f"/today?store_id={beta}")
    assert resp.status_code == 200
    with filler_client.session_transaction() as sess:
        assert sess.get("store_id") == alpha
