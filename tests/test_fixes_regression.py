"""修复回归测试：1) set_stores 保存门店权限 2) 种子不覆盖管理员改动 3) 开放重定向 4) net 含顾问罚。"""

from pathlib import Path

import pytest

from app import db
from app.incentive import judge
from app.web import create_app


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("STORE_DAILY_DB", str(path))
    monkeypatch.setenv("STORE_DAILY_DATA", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", Path(path))
    monkeypatch.setattr(db, "DATA_DIR", Path(tmp_path))
    monkeypatch.setattr(db, "is_locked", lambda *a, **k: False)
    db.init_db()
    return path


@pytest.fixture()
def app_client(tmp_db):
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_broadcast_compact_is_admin_setting(app_client):
    from datetime import date as _date

    app_client.post("/login", data={"username": "admin", "pin": "1234"})
    today_html = app_client.get("/today").get_data(as_text=True)
    assert "数字化里日=0" not in today_html
    settings = app_client.get("/settings?tab=broadcast").get_data(as_text=True)
    assert "数字化里日=0 且累=0 的行不进群消息" in settings
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
    day = _date.today().isoformat()
    saved = app_client.post(
        "/today",
        data={"store_id": str(sid), "date": day, "m_cloud_disk": "0", "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "云盘：日0，累0" not in saved
    app_client.post("/settings", data={"action": "save_broadcast", "tab": "broadcast"}, follow_redirects=True)
    with db.get_db() as conn:
        assert db.get_setting(conn, "broadcast_compact", "1") == "0"
    again = app_client.post(
        "/today",
        data={"store_id": str(sid), "date": day, "m_cloud_disk": "0", "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "云盘：日0，累0" in again


def test_add_store_uses_city_and_hides_internal_code(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "1234"})
    page = app_client.get("/settings?tab=stores").get_data(as_text=True)
    assert "内部编码" not in page
    assert "地市" in page
    resp = app_client.post(
        "/settings",
        data={
            "action": "add_store",
            "tab": "stores",
            "store_name": "TZ泰州兴化吾悦vivo体验店",
            "short_name": "兴化吾悦",
            "region_group": "通泰",
            "city": "泰州市",
            "mobile_code": "20999999",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM stores WHERE short_name='兴化吾悦'").fetchone()
        assert row is not None
        assert row["city"] == "泰州市"
        assert row["region_group"] == "通泰"
        assert row["code"].startswith("s")
    bulletin = app_client.get("/bulletin").get_data(as_text=True)
    assert "兴化吾悦" not in bulletin
    assert "南通vivo" in bulletin or "南通vivo零售运营中心" in bulletin
    tz = app_client.get("/bulletin?city=泰州市").get_data(as_text=True)
    assert "兴化吾悦" in tz
    assert "海门金花" not in tz
    assert "泰州vivo" in tz


def test_1_set_stores_persists(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "1234"})
    with db.get_db() as conn:
        stores = db.list_all_stores(conn)
        uid = conn.execute("SELECT id FROM users WHERE username='jinhua'").fetchone()["id"]
        other = stores[-1]["id"]
    resp = app_client.post(
        "/settings",
        data={
            "action": "set_stores",
            "tab": "people",
            "user_id": str(uid),
            "store_ids": str(other),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        assert db.user_store_ids(conn, uid) == [other]


def test_2_seed_does_not_overwrite_admin_edits(tmp_db):
    with db.get_db() as conn:
        store = conn.execute("SELECT * FROM stores LIMIT 1").fetchone()
        conn.execute(
            "UPDATE stores SET active=0, store_manager='管理员手改', area_manager='手改A' WHERE id=?",
            (store["id"],),
        )
        uid = conn.execute("SELECT id FROM users WHERE username='jinhua'").fetchone()["id"]
        db.set_user_active(conn, uid, False)
        other = conn.execute("SELECT id FROM stores ORDER BY id DESC LIMIT 1").fetchone()["id"]
        db.set_user_stores(conn, uid, [other])
    # 重启（再次 init_db）不应把改动冲掉
    db.init_db()
    with db.get_db() as conn:
        s = conn.execute("SELECT * FROM stores WHERE id=?", (store["id"],)).fetchone()
        assert s["active"] == 0
        assert s["store_manager"] == "管理员手改"
        assert s["area_manager"] == "手改A"
        u = conn.execute("SELECT active FROM users WHERE username='jinhua'").fetchone()
        assert u["active"] == 0
        uid2 = conn.execute("SELECT id FROM users WHERE username='jinhua'").fetchone()["id"]
        assert db.user_store_ids(conn, uid2) == [other]
    # 管理员把店清空后重启，也不该被种子偷偷补回目录默认店
    with db.get_db() as conn:
        db.set_user_stores(conn, uid, [])
    db.init_db()
    with db.get_db() as conn:
        assert db.user_store_ids(conn, uid) == []


def test_3_open_redirect_blocked(app_client):
    resp = app_client.post(
        "/login?next=//evil.com",
        data={"username": "admin", "pin": "1234"},
        follow_redirects=True,
    )
    assert resp.request.path == "/today"


def test_report_ignores_inactive_metric_facts(tmp_db, monkeypatch):
    """即使某天留下了停用指标的 day 值，报表也不能崩，应忽略。"""
    from datetime import date as _date

    app_client = _admin_client(tmp_db)
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
        at = _date.today().isoformat()
        # 停用一个指标并故意留下它的历史 day 值
        conn.execute("UPDATE metrics SET active=0 WHERE code='watch_pack'")
        conn.execute(
            "INSERT OR REPLACE INTO daily_facts(biz_date, store_id, metric_code, day_value) VALUES (?,?,?,?)",
            (at, sid, "watch_pack", 3),
        )
        # 再留一个当前活跃指标的 day 值
        conn.execute(
            "INSERT OR REPLACE INTO daily_facts(biz_date, store_id, metric_code, day_value) VALUES (?,?,?,?)",
            (at, sid, "phone_sales", 7),
        )
    resp = app_client.get("/report")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "手机销量" in body


def _admin_client(tmp_db):
    from app.web import create_app

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "1234"})
    return c


def test_4_net_includes_advisor_penalty():
    row = judge(True, 0, 10)
    assert row["advisor_penalty"] == 100
    assert row["net"] == -100
    row = judge(True, 2, 3)
    assert row["net"] == -150
    assert judge(False, 0, 0)["net"] == -100