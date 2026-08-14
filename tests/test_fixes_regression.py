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
    db.init_db()
    return path


@pytest.fixture()
def app_client(tmp_db):
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


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


def test_4_net_includes_advisor_penalty():
    row = judge(True, 0, 10)
    assert row["advisor_penalty"] == 100
    assert row["net"] == -100
    row = judge(True, 2, 3)
    assert row["net"] == -150
    assert judge(False, 0, 0)["net"] == -100