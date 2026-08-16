"""共享测试夹具：统一 DB 初始化、client/app_client、登录 helper。

说明：这里默认把 db.is_locked 关掉，避免测试运行到锁定时间（23:00 北京时间）之后，
保存“当天”的用例随机失败。专门的锁定/权限用例会自行重新 monkeypatch 开锁。
"""
from pathlib import Path

import pytest

from app import db, db_core
from app.web import create_app


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """一块全新的 sqlite 库，种子已初始化。"""
    path = tmp_path / "t.db"
    monkeypatch.setenv("STORE_DAILY_DB", str(path))
    monkeypatch.setenv("STORE_DAILY_DATA", str(tmp_path))
    # DB_PATH/DATA_DIR 真正被读的地方是 db_core.connect()（模块级全局），
    # 而 db 只是 re-export 副本——必须 patch 属主模块，否则测试会写进真 dev 库。
    monkeypatch.setattr(db_core, "DB_PATH", Path(path))
    monkeypatch.setattr(db_core, "DATA_DIR", Path(tmp_path))
    monkeypatch.setattr(db, "is_locked", lambda *a, **k: False)
    db.init_db()
    # 种子账号仍是默认口令；正式环境会强制改密，测试里先放开以免打挂现有用例
    with db.get_db() as conn:
        conn.execute("UPDATE users SET must_change_pin=0")
    return path


@pytest.fixture()
def client(tmp_db):
    """未登录的测试客户端（Test app，TESTING=True）。登录请用 login()。"""
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def app_client(tmp_db):
    """未登录的测试客户端（与 client 等价，供不同命名习惯的用例复用）。"""
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def login(client, username="jinhua", pin="123456"):
    """登录 helper：默认用店员 jinhua，可传 admin/1234 等。"""
    return client.post(
        "/login", data={"username": username, "pin": pin}, follow_redirects=True
    )
