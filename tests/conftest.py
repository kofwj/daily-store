"""共享测试夹具：统一 DB 初始化、client/app_client、登录 helper。

说明：这里默认把 db.is_locked 关掉，避免测试运行到锁定时间（23:00 北京时间）之后，
保存“当天”的用例随机失败。专门的锁定/权限用例会自行重新 monkeypatch 开锁。
"""
import os
from pathlib import Path

import pytest

# create_app 模块级 import 就会执行，，先种好测试开关和密钥（fixture 里会再按需覆盖）
os.environ.setdefault("STORE_DAILY_TESTING", "1")
os.environ.setdefault("STORE_DAILY_SECRET", "test-" + "a" * 48)

from app import db, db_core
from app.web import create_app


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """一块全新的 sqlite 库，种子已初始化。"""
    path = tmp_path / "t.db"
    monkeypatch.setenv("STORE_DAILY_DB", str(path))
    monkeypatch.setenv("STORE_DAILY_DATA", str(tmp_path))
    # create_app deliberately has no deployment fallback; tests explicitly provide a key.
    monkeypatch.setenv("STORE_DAILY_SECRET", "test-" + "a" * 48)
    # 显式测试开关（不靠 "pytest" in sys.modules 探测；生产进程误 import pytest 也不会关掉 CSRF）
    monkeypatch.setenv("STORE_DAILY_TESTING", "1")
    # 测试一律用示例店，避免加载 stores_seed_local 里的真实门店
    monkeypatch.setenv("STORE_DAILY_SAMPLE_SEED", "1")
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
    app = create_app(testing=True)
    return app.test_client()


@pytest.fixture()
def app_client(tmp_db):
    """未登录的测试客户端（与 client 等价，供不同命名习惯的用例复用）。"""
    app = create_app(testing=True)
    return app.test_client()


def login(client, username="alpha", pin="123456"):
    """登录 helper：默认用店员 alpha，可传 admin/123456 等。"""
    return client.post(
        "/login", data={"username": username, "pin": pin}, follow_redirects=True
    )


@pytest.fixture()
def admin_client(app_client):
    """已登录管理员（admin/123456）的测试客户端。"""
    login(app_client, "admin", "123456")
    return app_client


@pytest.fixture()
def filler_client(app_client):
    """已登录店员（alpha/123456）的测试客户端。"""
    login(app_client, "alpha", "123456")
    return app_client


@pytest.fixture()
def city_client(app_client):
    """已登录示例市地市负责人（cityboss/654321）的测试客户端。"""
    make_city_user()
    login(app_client, "cityboss", "654321")
    return app_client


def make_city_user(username="cityboss", scope="示例市", pin="654321"):
    """建一个地市负责人账号（默认示例市）。"""
    with db.get_db() as conn:
        return db.create_user(
            conn,
            username=username,
            display_name="地市负责",
            pin=pin,
            role="city",
            store_ids=[],
            scope=scope,
        )


def store_id(code="store-alpha"):
    """按门店 code 查 id（种子目录里的店都能查到）。"""
    with db.get_db() as conn:
        return conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]

