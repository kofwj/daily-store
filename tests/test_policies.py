"""政策说明：保存、版本、已读门槛。"""

from app import db


def test_sanitize_strips_script():
    html = db.sanitize_policy_html('<p>口径</p><script>alert(1)</script><a href="javascript:x">x</a>')
    assert "script" not in html.lower()
    assert "javascript" not in html.lower()
    assert "口径" in html


def test_markdown_keeps_newlines_and_lists():
    html = db.sanitize_policy_html("第一行\n第二行\n\n## 小标题\n- 甲\n- 乙\n\n**重点**")
    assert "<br>" in html
    assert "第一行" in html and "第二行" in html
    assert "<h3>" in html and "小标题" in html
    assert "<ul>" in html and "<li>" in html
    assert "<strong>重点</strong>" in html


def test_sanitize_keeps_table():
    html = db.sanitize_policy_html(
        '<table><tr><th colspan="2">档</th></tr><tr><td>A</td><td>B</td></tr></table><script>x</script>'
    )
    assert "<table>" in html and "<th colspan=\"2\">" in html
    assert "<td>A</td>" in html
    assert "script" not in html.lower()


def test_diff_marks_insert_and_delete():
    html = db.render_policy_diff("融合服务，剔除线上卡", "融合服务新增，线上卡")
    assert 'class="policy-add"' in html and "新增" in html
    assert 'class="policy-del"' in html and "剔除" in html


def test_save_policy_bumps_version(tmp_db):
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        pid = db.save_policy(conn, title="金币", body="第一版", user_id=uid)
        first = db.get_policy(conn, pid)
        db.save_policy(conn, title="金币", body="第二版", user_id=uid, policy_id=pid)
        second = db.get_policy(conn, pid)
        revs = db.list_revisions(conn, pid)
    assert first["version"] == 1
    assert second["version"] == 2
    assert [r["version"] for r in revs] == [2, 1]


def test_restore_policy_revision(tmp_db):
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        pid = db.save_policy(conn, title="表", body="<p>有表格</p>", user_id=uid)
        db.save_policy(conn, title="表", body="<p>删掉了</p>", user_id=uid, policy_id=pid)
        db.restore_policy_revision(conn, pid, 1, user_id=uid)
        now = db.get_policy(conn, pid)
    assert "有表格" in now["body"]
    assert now["version"] == 3


def test_unread_gate_blocks_today_when_enabled(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_policy(conn, title="宽带", body="调测费口径", user_id=uid)
        db.set_policy_require_read(conn, True)
        filler = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        sid = conn.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()["id"]
        db.set_user_stores(conn, filler, [sid])
    client.get("/logout")
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    blocked = client.post(
        "/today",
        data={"store_id": str(sid), "date": db.today_local().isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    assert blocked.request.path == "/policies"
    assert "请先阅读" in blocked.get_data(as_text=True)
    with db.get_db() as conn:
        pid = conn.execute("SELECT id FROM policies WHERE title='宽带'").fetchone()["id"]
    acked = client.post("/policies/ack", data={"policy_id": str(pid)}, follow_redirects=True)
    assert "已确认阅读" in acked.get_data(as_text=True)
    ok = client.post(
        "/today",
        data={"store_id": str(sid), "date": db.today_local().isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    assert ok.request.path == "/today"


def test_gate_off_does_not_block(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_policy(conn, title="芝麻", body="服务费", user_id=uid)
        db.set_policy_require_read(conn, False)
        filler = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        sid = conn.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()["id"]
        db.set_user_stores(conn, filler, [sid])
    client.get("/logout")
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    page = client.post(
        "/today",
        data={"store_id": str(sid), "date": db.today_local().isoformat(), "m_phone_sales": "1"},
        follow_redirects=True,
    )
    assert page.request.path == "/today"


def test_unread_gate_blocks_readonly_pages(client):
    client.post("/login", data={"username": "admin", "pin": "1234"})
    with db.get_db() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_policy(conn, title="合约", body="店长也要读", user_id=uid)
        db.set_policy_require_read(conn, True)
        sid = conn.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()["id"]
        db.create_user(
            conn,
            username="dz_policy",
            display_name="店长测",
            pin="123456",
            role="readonly",
            store_ids=[sid],
            scope="",
        )
        conn.execute("UPDATE users SET must_change_pin=0 WHERE username='dz_policy'")
    client.get("/logout")
    client.post("/login", data={"username": "dz_policy", "pin": "123456"})
    blocked = client.get("/report", follow_redirects=True)
    assert blocked.request.path == "/policies"
    assert "请先阅读" in blocked.get_data(as_text=True)
    with db.get_db() as conn:
        pid = conn.execute("SELECT id FROM policies WHERE title='合约'").fetchone()["id"]
    client.post("/policies/ack", data={"policy_id": str(pid)}, follow_redirects=True)
    ok = client.get("/report", follow_redirects=True)
    assert ok.request.path == "/report"
