"""上轮外部审计中严重项回归测试。。"""

from app import db
from app.incentive import DEFAULTS, judge_without_advisor


def test_without_advisor_uses_gt_zero_not_ai_pass(tmp_db):
    rules = dict(DEFAULTS)
    rules["ai_pass"] = 10_000  # 管理员调高的 ai_pass 不应影响“破 0”口径
    row = judge_without_advisor(
        50,
        50,
        rules,
    )
    assert row["store_reward"] == DEFAULTS["reward_no_advisor"]


def test_pick_store_invalid_id_prompts(tmp_db, client):
    client.post(
        "/login",
        data={"username": "admin", "pin": "123456"},
    )
    page = client.get("/today?store_id=zzz")
    assert "店号无效" in page.get_data(as_text=True)


def test_csrf_referrer_only_same_site(tmp_db, client):
    client.post(
        "/login",
        data={"username": "admin", "pin": "123456"},
    )
    resp = client.post(
        "/settings",
        data={"action": "bogus"},
        headers={"Referer": "https://evil.example/x"},
    )
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.startswith("/") and not loc.startswith("//")


def test_login_lock_keyed_by_user_and_ip(tmp_db, client):
    for _ in range(5):
        client.post(
            "/login",
            data={"username": "admin", "pin": "0000"},
        )
    with db.get_db() as conn:
        keys = {
            r[0]
            for r in conn.execute("SELECT key FROM login_attempts")
        }
        assert keys  # 有记录
        assert not any(k.startswith("user:") for k in keys)


def test_csrf_referrer_follows_same_site_absolute_url(tmp_db):
    # Referer 几乎总是绝对 URL：同源的必须转成站内路径跳回原页，
    # 外站/协议相对的一律丢弃回 today（防开放重定向）。
    from app.web import create_app

    app = create_app()
    app.config["TESTING"] = False  # 开启 CSRF 校验
    client = app.test_client()
    client.get("/login")
    same_site = "http://localhost/settings?tab=backup"  # Flask 测试客户端默认 host
    r = client.post("/settings", data={"action": "bogus"}, headers={"Referer": same_site})
    assert r.status_code == 302
    assert r.headers["Location"] == "/settings?tab=backup"

    for evil in ("https://evil.example/x", "//evil.example/x", "http://localhost.evil.example/"):
        r = client.post("/settings", data={"action": "bogus"}, headers={"Referer": evil})
        assert r.status_code == 302
        loc = r.headers["Location"]
        assert loc.startswith("/") and not loc.startswith("//"), loc


def test_restore_safe_while_other_connection_open(tmp_db):
    # 模拟多 worker：另一个连接在恢复期间持着打开的读事务（正跑查询），
    # 恢复必须安全完成：旧事务继续读旧快照、结束事务后读到恢复后的新数据、
    # 且能继续写入，全程不出现 malformed/锁死。
    import sqlite3

    from app import backup, db_core

    with db.get_db() as conn:
        conn.execute("INSERT INTO app_meta(key, value) VALUES ('marker', 'before')")
    snap = backup.snapshot("manual")
    other = sqlite3.connect(str(db_core.DB_PATH), timeout=30)
    try:
        with db.get_db() as conn:  # 备份之后再写一笔，恢复后应被抹掉
            conn.execute("UPDATE app_meta SET value='after' WHERE key='marker'")
        other.execute("BEGIN")  # 打开的读事务，横跨整个恢复过程
        assert other.execute("SELECT value FROM app_meta WHERE key='marker'").fetchall()[0][0] == "after"
        backup.restore_bytes(snap.read_bytes())
        # 恢复期间旧事务不受影响，仍读旧快照
        assert other.execute("SELECT value FROM app_meta WHERE key='marker'").fetchall()[0][0] == "after"
        other.execute("ROLLBACK")
        # 事务结束后看到恢复后的数据
        assert other.execute("SELECT value FROM app_meta WHERE key='marker'").fetchall()[0][0] == "before"
        other.execute("INSERT INTO app_meta(key, value) VALUES ('other_worker', 'ok')")
        other.commit()
        with db.get_db() as conn:
            assert conn.execute("SELECT value FROM app_meta WHERE key='other_worker'").fetchall()[0][0] == "ok"
    finally:
        other.close()
