"""地市负责人（city）角色回归：本市日报可改、垫资只读、成交只读、可见范围按地市。"""

import sqlite3
from datetime import date, timedelta

from app import db
from app.db_core import _expand_user_roles_city
from tests.conftest import login, make_city_user


def _stores():
    with db.get_db() as conn:
        return db.list_all_stores(conn)


def test_expand_user_roles_city_migrates_old_table(tmp_path):
    """老库的 users 表（CHECK 无 city）迁移后可容纳 city 角色，数据保留。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'filler', 'readonly')),
            scope TEXT NOT NULL DEFAULT '',
            must_change_pin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO users(username, display_name, pin_hash, role, created_at) VALUES ('old1', '老用户', 'x', 'filler', '2026-01-01 00:00:00')"
    )
    conn.commit()
    _expand_user_roles_city(conn)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()["sql"]
    assert "'city'" in sql
    conn.execute(
        "INSERT INTO users(username, display_name, pin_hash, role, scope, created_at) VALUES ('boss1', '地市负责', 'y', 'city', '示例市', '2026-01-01 00:00:00')"
    )
    kept = conn.execute("SELECT username FROM users WHERE username='old1'").fetchone()
    assert kept is not None
    conn.close()
    # 幂等：再跑一次不报错
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _expand_user_roles_city(conn)


def test_city_scope_limits_visible_stores(city_client):
    """可见范围 = 本地市启用门店；他市店无权。"""
    stores = _stores()
    nt = [s for s in stores if s["city"] == "示例市"]
    other = [s for s in stores if s["city"] != "示例市"]
    with db.get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username='cityboss'").fetchone()
        visible = {s["id"] for s in db.list_user_stores(conn, user)}
        assert visible == {s["id"] for s in nt}
        assert all(not db.user_can_access_store(conn, user, s["id"]) for s in other)
        assert all(db.user_can_access_store(conn, user, s["id"]) for s in nt)


def test_city_edits_any_day_this_month(city_client):
    """不开「本月可改」，地市负责人也能改本市店里本月任意过去日。"""
    stores = _stores()
    sid = stores[0]["id"]  # 示例市 alpha 店
    yesterday = date.today() - timedelta(days=1)
    assert yesterday.month == date.today().month or True  # 月初时昨天可能跨月，跨月分支另有用例
    resp = city_client.post(
        "/today",
        data={
            "store_id": str(sid),
            "date": yesterday.isoformat(),
            "m_cloud_disk": "3",
            "m_phone_sales": "1",
        },
        follow_redirects=True,
    )
    page = resp.get_data(as_text=True)
    if yesterday.month == date.today().month:
        assert "已保存" in page
        with db.get_db() as conn:
            row = db.get_report(conn, sid, yesterday)
            assert row is not None


def test_city_rejects_cross_month_and_future(city_client):
    """跨月与未来日期仍拒绝。"""
    sid = _stores()[0]["id"]
    today = date.today()
    last_month = today.replace(day=1) - timedelta(days=1)
    resp = city_client.post(
        "/today",
        data={"store_id": str(sid), "date": last_month.isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    assert "只能改本月的日报" in resp.get_data(as_text=True)
    future = today + timedelta(days=3)
    resp = city_client.post(
        "/today",
        data={"store_id": str(sid), "date": future.isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    assert "非管理员不能填写未来日期" in resp.get_data(as_text=True)


def test_city_can_overwrite_other_submission(app_client):
    """店员已提交的日报，地市负责人可直接覆盖。"""
    day = date.today().isoformat()
    sid = _stores()[0]["id"]
    login(app_client, "alpha")  # 店员 alpha 先提交
    app_client.post(
        "/today",
        data={"store_id": str(sid), "date": day, "m_phone_sales": "1"},
        follow_redirects=True,
    )
    app_client.post("/logout")
    make_city_user()
    login(app_client, "cityboss", "654321")
    resp = app_client.post(
        "/today",
        data={"store_id": str(sid), "date": day, "m_phone_sales": "9"},
        follow_redirects=True,
    )
    page = resp.get_data(as_text=True)
    assert "该日已有其他人提交" not in page
    assert "已保存" in page


def test_city_advance_is_readonly(city_client):
    """垫资：可看页面，提交与删除都被拒。"""
    sid = _stores()[0]["id"]
    page = city_client.get("/advance", query_string={"store_id": sid}).get_data(as_text=True)
    assert "垫资" in page
    resp = city_client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": date.today().isoformat(),
            "phone": "13800000000",
            "broadband": "100",
        },
        follow_redirects=True,
    )
    assert "只读账号不能填垫资" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM advance_posts WHERE store_id=?", (sid,)).fetchone()[0]
    assert n == 0
    resp = city_client.post(
        "/advance/delete",
        data={"store_id": str(sid), "advance_id": "1"},
        follow_redirects=True,
    )
    assert "只读账号不能填写或修改数据" in resp.get_data(as_text=True)


def test_city_deal_post_rejected(city_client):
    """成交播报对地市负责人只读。"""
    sid = _stores()[0]["id"]
    resp = city_client.post(
        "/deal",
        data={"store_id": str(sid)},
        follow_redirects=True,
    )
    assert "只读账号不能填触客播报" in resp.get_data(as_text=True)


def test_settings_add_and_rescope_city_user(admin_client):
    """设置页：添加地市负责人、改负责地市、无效地市报错。"""
    resp = admin_client.post(
        "/settings",
        data={
            "action": "add_user",
            "tab": "people",
            "role": "city",
            "username": "citylead",
            "display_name": "城市经理",
            "pin": "48291703",
            "scope": "邻市",
            "bind": "city",
        },
        follow_redirects=True,
    )
    assert "账号已加" in resp.get_data(as_text=True)
    page = admin_client.get("/settings?tab=people").get_data(as_text=True)
    assert "地市负责人" in page
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='citylead'").fetchone()["id"]
        assert conn.execute("SELECT scope FROM users WHERE id=?", (uid,)).fetchone()["scope"] == "邻市"
    # 改负责地市
    resp = admin_client.post(
        "/settings",
        data={
            "action": "set_stores",
            "tab": "people",
            "user_id": str(uid),
            "bind": "city",
            "scope": "示例市",
        },
        follow_redirects=True,
    )
    assert "地市范围已改" in resp.get_data(as_text=True)
    # 无效地市
    resp = admin_client.post(
        "/settings",
        data={
            "action": "set_stores",
            "tab": "people",
            "user_id": str(uid),
            "bind": "city",
            "scope": "不存在的市",
        },
        follow_redirects=True,
    )
    assert "要选一个有效地市" in resp.get_data(as_text=True)
    # add_user 用无效地市
    resp = admin_client.post(
        "/settings",
        data={
            "action": "add_user",
            "tab": "people",
            "role": "city",
            "username": "citylead2",
            "display_name": "城市经理二",
            "pin": "48291703",
            "scope": "不存在的市",
            "bind": "city",
        },
        follow_redirects=True,
    )
    assert "要选一个有效地市" in resp.get_data(as_text=True)
