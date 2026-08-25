import sqlite3
from pathlib import Path

from app import backup, db


def test_admin_status_page_ok(tmp_db, client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    page = client.get("/settings/status")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "服务器状态" in body
    assert "门店" in body


def test_admin_can_backup_download_and_restore(tmp_db, client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    page = client.get("/settings?tab=backup").get_data(as_text=True)
    assert "备份恢复" in page
    made = client.post(
        "/settings",
        data={"action": "make_backup", "tab": "backup"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已备份" in made
    items = backup.list_backups()
    assert items
    name = items[0]["name"]
    down = client.get(f"/settings/backup/{name}")
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
    restored = client.post(
        "/settings",
        data={"action": "restore_named", "tab": "backup", "backup_name": name},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已用" in restored and "恢复" in restored
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


def test_filler_cannot_open_backup(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    page = client.get("/settings?tab=backup").get_data(as_text=True)
    assert "备份恢复" not in page
    r = client.get("/settings/backup/store_daily_manual_x.db")
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
