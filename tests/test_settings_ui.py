"""设置页本轮调整的回归：门店绑定复选列表、复盘占位符分组、导航分组与命名。"""

from app import db


def _login_admin(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})


def test_people_store_bind_uses_clickable_checkboxes(app_client):
    """人员页门店绑定是点选复选列表，不是按 Ctrl 多选的原生 select。"""
    _login_admin(app_client)
    page = app_client.get("/settings?tab=people").get_data(as_text=True)
    assert 'details class="store-bind" data-bind' in page
    assert '<select name="store_ids"' not in page
    assert "Ctrl" not in page and "Command" not in page
    # 提交格式不变：store_ids 走 getlist，店长绑定两家店仍生效
    with db.get_db() as conn:
        stores = db.list_all_stores(conn)
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
    resp = app_client.post(
        "/settings",
        data={
            "action": "set_stores",
            "tab": "people",
            "user_id": str(uid),
            "store_ids": [str(stores[0]["id"]), str(stores[1]["id"])],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        assert db.user_store_ids(conn, uid) == [stores[0]["id"], stores[1]["id"]]


def test_add_user_filler_with_multiple_stores(app_client):
    """添加填报员用同样的复选列表语义提交多家门店。"""
    _login_admin(app_client)
    with db.get_db() as conn:
        stores = db.list_all_stores(conn)
    resp = app_client.post(
        "/settings",
        data={
            "action": "add_user",
            "tab": "people",
            "role": "filler",
            "username": "ceshiduodian",
            "display_name": "测试多店",
            "pin": "48291703",
            "store_ids": [str(stores[0]["id"]), str(stores[1]["id"])],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        uid = conn.execute(
            "SELECT id FROM users WHERE username='ceshiduodian'"
        ).fetchone()["id"]
        assert db.user_store_ids(conn, uid) == [stores[0]["id"], stores[1]["id"]]


def test_review_template_placeholder_groups(app_client):
    """复盘模板占位符按用途分组展示，含此前漏掉的达标数占位符。"""
    _login_admin(app_client)
    page = app_client.get("/settings?tab=review").get_data(as_text=True)
    assert 'class="ph-groups"' in page
    assert 'class="ph-tag">日报</span>' in page
    assert "月累计" in page
    assert "门店与跟进" in page
    assert "{hit_ai_n}" in page and "{triple_n}" in page
    # 一大段无分组占位符不再出现
    assert "占位符：<code>{head}</code> <code>{day_ai}</code>" not in page


def test_nav_groups_and_store_page_title(app_client):
    """导航按 考核/通报/政策与开票 分组；门店页标题与导航一致。"""
    _login_admin(app_client)
    nav = app_client.get("/settings?tab=account").get_data(as_text=True)
    for label in ("个人", "组织", "考核", "通报", "政策与开票", "系统"):
        assert f">{label}</span>" in nav
    stores_page = app_client.get("/settings?tab=stores").get_data(as_text=True)
    assert "<h1>门店档案</h1>" in stores_page
    assert "<h1>门店</h1>" not in stores_page
    assert "margin-top:16px" not in app_client.get("/settings?tab=broadcast").get_data(as_text=True)
