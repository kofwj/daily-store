from app import backup, db


def test_admin_can_backup_download_and_restore(tmp_db, client):
    client.post("/login", data={"username": "admin", "pin": "1234"})
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
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
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


def test_filler_cannot_open_backup(client):
    client.post("/login", data={"username": "jinhua", "pin": "123456"})
    page = client.get("/settings?tab=backup").get_data(as_text=True)
    assert "备份恢复" not in page
    r = client.get("/settings/backup/store_daily_manual_x.db")
    assert r.status_code == 302
