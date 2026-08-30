"""企业微信群机器人：配置取值、发送不挡填报。"""

from unittest.mock import patch

import pytest

from app import db, db_core
from app.wecom import get_webhook, send_test, send_text


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({}, ""),
        (
            {"wecom_global": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"},
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123",
        ),
        (
            {
                "wecom_global": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=global",
                "wecom_city_示例市": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=city",
            },
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=city",
        ),
        ({"wecom_global": "https://example.com/not-valid"}, ""),
    ],
)
def test_get_webhook_selection(tmp_db, settings, expected):
    """webhook 取值：无配置为空 → 全局 → 地市覆盖全局；无效地址忽略。"""
    with db.get_db() as conn:
        for key, value in settings.items():
            db_core.set_setting(conn, key, value)
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        assert get_webhook(conn, store) == expected


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
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local().isoformat()
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": today, "m_ai_contract": "5"},
        follow_redirects=True,
    )
    assert "已保存" in resp.get_data(as_text=True)


def test_long_chinese_content_truncated_by_bytes(tmp_db):
    """企微按 4096 字节限制，中文 3 字节/字——按字符截断会超限掉消息。"""
    import json

    from app.wecom import MAX_CONTENT

    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    with db.get_db() as conn:
        db_core.set_setting(conn, "wecom_global", url)
        store = conn.execute("SELECT * FROM stores WHERE code='store-alpha'").fetchone()
        with patch("app.wecom.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = b'{"errcode":0,"errmsg":"ok"}'
            result = send_text(conn, store, "好" * 2000, source="daily")
            assert result is True
        payload = json.loads(mock_open.call_args.args[0].data)
    content = payload["text"]["content"]
    assert len(content.encode("utf-8")) <= MAX_CONTENT
    assert content.endswith("...")


def test_broadcast_error_keeps_daily_saved(filler_client):
    """企微播报出问题也不能丢已保存的日报：先提交放锁，再播报。"""
    from datetime import date

    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        db_core.set_setting(conn, "wecom_global", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")
    with patch("app.wecom.send_text", side_effect=RuntimeError("网络炸了")):
        resp = filler_client.post(
            "/today",
            data={"store_id": str(sid), "date": date.today().isoformat(), "m_phone_sales": "5"},
            follow_redirects=True,
        )
    assert resp.status_code == 200
    with db.get_db() as conn:
        assert db.get_report(conn, sid, date.today()) is not None
