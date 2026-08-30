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


def test_sanitize_strips_protocol_relative_and_http():
    html = db.sanitize_policy_html(
        '<p>链</p><a href="//evil.example/x">外</a>'
        '<a href="http://evil.example/x">明文</a>'
        '<img src="//evil.example/x.png">'
        '<a href="https://example.com/ok">好</a>'
    )
    assert "//evil.example" not in html
    assert "http://evil.example" not in html
    assert "https://example.com/ok" in html


def test_sanitize_keeps_safe_img_strips_bad_src():
    html = db.sanitize_policy_html(
        '<p>图</p><img src="/uploads/policy/a.png" alt="口径" width="300">'
        '<img src="javascript:alert(1)"><img src="data:text/html;x">'
    )
    assert "/uploads/policy/a.png" in html
    assert "口径" in html
    assert "javascript" not in html.lower()
    assert "data:text" not in html.lower()
    # 无合法 src 的 img 被丢弃，不残留空 img
    assert html.count("<img") == 1


def test_sanitize_keeps_safe_style_strips_injection():
    html = db.sanitize_policy_html(
        '<p style="font-size:18px; color:#ff0000; text-align:center; line-height:2">口径</p>'
        '<span style="background-color:#ffff00">高亮</span>'
        '<p style="background:url(javascript:alert(1)); position:fixed">坏</p>'
    )
    assert 'font-size: 18px' in html
    assert 'color: #ff0000' in html
    assert 'text-align: center' in html
    assert 'line-height: 2' in html
    assert 'background-color: #ffff00' in html
    # 不在白名单的样式被丢弃
    assert 'position' not in html
    assert 'url(' not in html
    assert 'javascript' not in html.lower()


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
    client.post("/logout")
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
    client.post("/logout")
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
    client.post("/logout")
    client.post("/login", data={"username": "dz_policy", "pin": "123456"})
    blocked = client.get("/report", follow_redirects=True)
    assert blocked.request.path == "/policies"
    assert "请先阅读" in blocked.get_data(as_text=True)
    with db.get_db() as conn:
        pid = conn.execute("SELECT id FROM policies WHERE title='合约'").fetchone()["id"]
    client.post("/policies/ack", data={"policy_id": str(pid)}, follow_redirects=True)
    ok = client.get("/report", follow_redirects=True)
    assert ok.request.path == "/report"


def test_policy_read_status_counts_unread(tmp_db):
    """已读统计：只有确认当前版本的用户才算已读。"""
    with db.get_db() as conn:
        admin = conn.execute(
            "SELECT id, display_name FROM users WHERE username='admin'"
        ).fetchone()
        fillers = [
            r
            for r in conn.execute(
                "SELECT id, display_name, active, username FROM users WHERE role='filler'"
            )
        ]
        assert len(fillers) >= 2
        u1 = fillers[0]["id"]
        pid = db.save_policy(conn, title="考勤", body="第一版", user_id=admin["id"])
        # 只有 u1 已读
        db.mark_policy_read(conn, u1, pid)
        rows = db.policy_read_status(conn)
    target = next(r for r in rows if r["id"] == pid)
    assert target["title"] == "考勤"
    assert target["read"] == 1
    assert len(target["unread"]) == target["total"] - 1
    # 升版后已读作废
    with db.get_db() as conn:
        db.save_policy(
            conn, title="考勤", body="第二版", user_id=admin["id"], policy_id=pid
        )
        rows2 = db.policy_read_status(conn)
    t2 = next(r for r in rows2 if r["id"] == pid)
    assert t2["read"] == 0  # 版本号追上，之前读了也不算


def test_policy_image_upload_and_serve(admin_client):
    """政策插图：管理员可传 PNG，非图片被拒，非管理员被拦。"""
    from PIL import Image

    buf = __import__("io").BytesIO()
    Image.new("RGB", (2, 2), (16, 120, 72)).save(buf, format="PNG")
    png = buf.getvalue()
    r = admin_client.post(
        "/settings/policy-image",
        data={"file-0": (__import__("io").BytesIO(png), "口径.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    url = r.get_json()["result"][0]["url"]
    assert url.startswith("/uploads/policy/")
    # 能取回
    got = admin_client.get(url)
    assert got.status_code == 200 and got.data[:8] == b"\x89PNG\r\n\x1a\n"
    # 非图片被拒
    bad = admin_client.post(
        "/settings/policy-image",
        data={"file-0": (__import__("io").BytesIO(b"not an image"), "x.png")},
        content_type="multipart/form-data",
    )
    assert bad.status_code == 400
    # 非管理员被拦
    c2 = admin_client.application.test_client()
    c2.post("/login", data={"username": "alpha", "pin": "123456"})
    r2 = c2.post(
        "/settings/policy-image",
        data={"file-0": (__import__("io").BytesIO(png), "a.png")},
        content_type="multipart/form-data",
    )
    assert r2.status_code in (302, 403)


def test_policy_image_upload_is_resized_down(admin_client):
    """政策插图上传后服务端把超大图缩到长边 <= 1600。"""
    from io import BytesIO

    from PIL import Image

    big = BytesIO()
    Image.new("RGB", (4000, 2000), (200, 100, 50)).save(big, "PNG")
    big.seek(0)
    r = admin_client.post(
        "/settings/policy-image",
        data={"file-0": (big, "big.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    url = r.get_json()["result"][0]["url"]
    saved = admin_client.get(url)
    assert saved.status_code == 200
    im = Image.open(BytesIO(saved.data))
    assert max(im.width, im.height) <= 1600, (im.width, im.height)

