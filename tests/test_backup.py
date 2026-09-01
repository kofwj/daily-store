import sqlite3
from pathlib import Path

from app import backup, db


def test_admin_status_page_ok(tmp_db, admin_client):
    page = admin_client.get("/settings/status")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "服务器状态" in body
    assert "门店" in body


def test_admin_can_backup_download_and_restore(tmp_db, admin_client):
    page = admin_client.get("/settings?tab=backup").get_data(as_text=True)
    assert "备份恢复" in page
    made = admin_client.post(
        "/settings",
        data={"action": "make_backup", "tab": "backup"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已备份" in made
    items = backup.list_backups()
    assert items
    name = items[0]["name"]
    down = admin_client.get(f"/settings/backup/{name}")
    assert down.status_code == 200
    assert down.data[:16] == b"SQLite format 3\x00"
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.record_advance(
            conn,
            store_id=sid,
            user_id=uid,
            biz_date=db.today_local(),
            phone="13900005555",
            rebate=100,
            note="恢复前写入",
        )
    restored = admin_client.post(
        "/settings",
        data={
            "action": "restore_named",
            "tab": "backup",
            "backup_name": name,
            "confirm_pin": "123456",
        },
        follow_redirects=True,
    )
    body = restored.get_data(as_text=True)
    assert "已用" in body and "恢复" in body
    assert restored.request.path == "/login"
    assert "admin" in body
    with db.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM advance_posts WHERE phone='13900005555'"
        ).fetchone()["n"]
        assert n == 0


def test_restore_old_backup_creates_invoice_tables(tmp_db):
    with db.get_db() as conn:
        conn.execute("DROP TABLE IF EXISTS invoice_edits")
        conn.execute("DROP TABLE IF EXISTS invoice_months")
        conn.commit()
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "invoice_months" not in names
    from app.db_invoice import _ensure_invoice_tables

    with db.get_db() as conn:
        _ensure_invoice_tables(conn)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {row[1] for row in conn.execute("PRAGMA table_info(invoice_months)")}
    assert "invoice_months" in names
    assert "invoice_edits" in names
    assert {"service_cents", "fee_cents", "details_json", "lease_cents"} <= cols


def test_restore_failure_does_not_change_live_db(tmp_db):
    with db.get_db() as conn:
        conn.execute("INSERT INTO app_meta(key, value) VALUES ('live_marker', 'keep')")
    broken = tmp_db.parent / "broken.db"
    probe = __import__("sqlite3").connect(broken)
    probe.execute("CREATE TABLE stores(id INTEGER)")
    probe.commit()
    probe.close()
    with __import__("pytest").raises(ValueError):
        backup.restore_bytes(broken.read_bytes())
    with db.get_db() as conn:
        assert conn.execute("SELECT value FROM app_meta WHERE key='live_marker'").fetchone()["value"] == "keep"


def test_restore_named_requires_current_pin(tmp_db, admin_client):
    admin_client.post("/settings", data={"action": "make_backup", "tab": "backup"})
    name = backup.list_backups()[0]["name"]
    with db.get_db() as conn:
        conn.execute("INSERT INTO app_meta(key, value) VALUES ('restore_guard', 'keep')")
    denied = admin_client.post(
        "/settings",
        data={"action": "restore_named", "tab": "backup", "backup_name": name, "confirm_pin": "wrong"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "当前口令" in denied
    with db.get_db() as conn:
        assert conn.execute("SELECT value FROM app_meta WHERE key='restore_guard'").fetchone()["value"] == "keep"


def test_filler_cannot_open_backup(filler_client):
    page = filler_client.get("/settings?tab=backup").get_data(as_text=True)
    assert "备份恢复" not in page
    r = filler_client.get("/settings/backup/store_daily_manual_x.db")
    assert r.status_code == 302


def test_offsite_fingerprint_skips_unchanged(tmp_path):
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "offsite_fingerprint.py"
    primary = tmp_path / "primary"
    (primary / "data").mkdir(parents=True)
    db_path = primary / "data" / "store_daily.db"
    env_path = primary / ".env"
    env_path.write_text("x=1\n", encoding="utf-8")
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE t(id INTEGER)")
    con.commit()
    con.close()

    def run(*args):
        return subprocess.check_output([sys.executable, str(script), *args, "--dir", str(primary)], text=True).strip()

    assert run("check") == "COPY"
    first = run("save")
    assert run("check") == "SKIP"
    assert backup.live_fingerprint(db_path, env_path) == first
    env_path.write_text("x=2\n", encoding="utf-8")
    assert run("check") == "COPY"


def test_prune_keeps_latest_20():
    d = backup.backup_dir()
    for i in range(30):
        (d / f"store_daily_offsite_20260825_0000{i:02d}.db").write_bytes(b"x")
        import time as _t

        _t.sleep(0.01)
    assert len(list(d.glob("store_daily_*.db"))) >= 20
    backup.prune()
    left = sorted(d.glob("store_daily_*.db"), key=lambda p: p.stat().st_mtime)
    assert len(left) <= backup.MAX_KEEP
    # 最新一份仍在
    assert str(left[-1].name).endswith("000029.db")


def test_restore_reverts_restore_window_writes(tmp_db):
    """快照之后、恢复之前写入的数据，恢复后必须不存在（恢复以快照为准）。

    旧版要求恢复窗口期的写入进 WAL、断言 -wal 文件存在；但连接关闭后 SQLite
    可能已把 WAL checkpoint 掉并删除文件，该断言机器相关（macOS 上必挂）。
    真正要验的是「恢复窗口期的写入不混进新库」，由末尾行数断言承担。
    """
    with db.get_db() as conn:
        before_n = int(conn.execute("SELECT COUNT(*) AS n FROM advance_posts").fetchone()["n"])
        snap = backup.snapshot("wal_test")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO advance_posts (store_id, user_id, created_at, biz_date, broadband) VALUES (?, ?, ?, ?, ?)",
            (1, 1, "2026-08-28T00:00:00", "2026-08-28", 100),
        )
    backup.restore_bytes(snap.read_bytes())
    with db.get_db() as conn:
        n = int(conn.execute("SELECT COUNT(*) AS n FROM advance_posts").fetchone()["n"])
        assert n == before_n  # 恢复窗口期写入被回滚，没混进新库


def test_restore_safe_while_other_connection_open(tmp_db):
    # 模拟多 worker：另一个连接在恢复期间持着打开的读事务（正跑查询），
    # 恢复必须安全完成：旧事务继续读旧快照、结束事务后读到恢复后的新数据、
    # 且能继续写入，全程不出现 malformed/锁死。
    from app import db_core

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

