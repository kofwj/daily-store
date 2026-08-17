"""企业微信群机器人：配置取值、发送不挡填报。"""

from unittest.mock import patch

from app import db, db_core
from app.wecom import get_webhook, send_test, send_text


def test_no_webhook_returns_empty(tmp_db):
    with db.get_db() as conn:
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        assert get_webhook(conn, store) == ""


def test_global_webhook_used(tmp_db):
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"
    with db.get_db() as conn:
        db_core.set_setting(conn, "wecom_global", url)
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        assert get_webhook(conn, store) == url


def test_city_webhook_overrides_global(tmp_db):
    global_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=global"
    city_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=city"
    with db.get_db() as conn:
        db_core.set_setting(conn, "wecom_global", global_url)
        db_core.set_setting(conn, "wecom_city_示例市", city_url)
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        assert get_webhook(conn, store) == city_url


def test_invalid_webhook_ignored(tmp_db):
    with db.get_db() as conn:
        db_core.set_setting(conn, "wecom_global", "https://example.com/not-valid")
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        assert get_webhook(conn, store) == ""


def test_send_failure_does_not_raise(tmp_db):
    with db.get_db() as conn:
        db_core.set_setting(conn, "wecom_global", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        result = send_text(conn, store, "测试消息", source="test")
        # 网络不通会返回 False，不抛异常
        assert result is False


def test_send_success(tmp_db):
    fake_resp = '{"errcode":0,"errmsg":"ok"}'
    with db.get_db() as conn:
        db_core.set_setting(conn, "wecom_global", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        with patch("app.wecom.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = fake_resp.encode()
            result = send_text(conn, store, "日报测试", source="daily")
            assert result is True


def test_test_send_rejects_invalid_url(tmp_db):
    with db.get_db() as conn:
        ok, msg = send_test(conn, "https://example.com")
        assert ok is False
        assert "有效" in msg


def test_daily_save_triggers_wecom(client):
    """保存日报后尝试发群，webhook 没配也不会挡保存。"""
    client.post("/login", data={"username": "admin", "pin": "1234"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local().isoformat()
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": today, "m_ai_contract": "5"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)
