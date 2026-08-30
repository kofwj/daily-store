"""设置页回归：门店绑定复选列表、门店增删守卫、审计分页、导航分组与命名。"""

from datetime import date

import pytest

from app import db


def test_people_store_bind_uses_clickable_checkboxes(admin_client):
    """人员页门店绑定是点选复选列表，不是按 Ctrl 多选的原生 select。"""
    page = admin_client.get("/settings?tab=people").get_data(as_text=True)
    assert 'details class="store-bind" data-bind' in page
    assert '<select name="store_ids"' not in page
    assert "Ctrl" not in page and "Command" not in page
    # 提交格式不变：store_ids 走 getlist，店长绑定两家店仍生效
    with db.get_db() as conn:
        stores = db.list_all_stores(conn)
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
    resp = admin_client.post(
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
    page = admin_client.get("/settings?tab=people").get_data(as_text=True)
    # 动态文案不进内联 onclick，绑定控件不再走原生多选
    assert "onclick=\"applyReviewPreset" not in page
    assert "people-store-select" not in page


def test_add_user_filler_with_multiple_stores(admin_client):
    """添加填报员用同样的复选列表语义提交多家门店。"""
    with db.get_db() as conn:
        stores = db.list_all_stores(conn)
    resp = admin_client.post(
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


def test_review_template_placeholder_groups(admin_client):
    """复盘模板占位符按用途分组展示，含此前漏掉的达标数占位符。"""
    page = admin_client.get("/settings?tab=review").get_data(as_text=True)
    assert 'class="ph-groups"' in page
    assert 'class="ph-tag">日报</span>' in page
    assert "月累计" in page
    assert "门店与跟进" in page
    assert "{hit_ai_n}" in page and "{triple_n}" in page
    # 一大段无分组占位符不再出现
    assert "占位符：<code>{head}</code> <code>{day_ai}</code>" not in page


def test_nav_groups_and_store_page_title(admin_client):
    """导航按 考核/通报/政策与开票 分组；门店页标题与导航一致。"""
    nav = admin_client.get("/settings?tab=account").get_data(as_text=True)
    for label in ("个人", "组织", "考核", "通报", "政策与开票", "系统"):
        assert f">{label}</span>" in nav
    stores_page = admin_client.get("/settings?tab=stores").get_data(as_text=True)
    assert "<h1>门店档案</h1>" in stores_page
    assert "<h1>门店</h1>" not in stores_page
    assert "margin-top:16px" not in admin_client.get("/settings?tab=broadcast").get_data(as_text=True)


def test_add_store_uses_city_and_hides_internal_code(admin_client):
    """新增店走地市字段，不暴露内部编码；通报表按地市过滤。"""
    page = admin_client.get("/settings?tab=stores").get_data(as_text=True)
    assert "内部编码" not in page
    assert "地市" in page
    resp = admin_client.post(
        "/settings",
        data={
            "action": "add_store",
            "tab": "stores",
            "store_name": "TZ测试新店vivo体验店",
            "short_name": "测试新店",
            "region_group": "示例",
            "city": "邻市",
            "mobile_code": "20999999",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM stores WHERE short_name='测试新店'").fetchone()
        assert row is not None
        assert row["city"] == "邻市"
        assert row["region_group"] == "示例"
        assert row["code"].startswith("s")
    bulletin = admin_client.get("/bulletin?city=示例市").get_data(as_text=True)
    assert "测试新店" not in bulletin
    tz = admin_client.get("/bulletin?city=邻市").get_data(as_text=True)
    assert "测试新店" in tz
    assert "示例甲店" not in tz


def test_delete_store_guard_blocks_stores_with_history(admin_client):
    """删店守卫：有业务行、或业务行已删但审计行还在（外键来源）都不能删；空店可删。"""
    with db.get_db() as conn:
        empty = db.create_store(conn, "空店", short_name="空店")
        audit_only = db.create_store(conn, "审计空店", short_name="审计空店")
        used = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(
            conn,
            store_id=used,
            biz_date=date.today(),
            user_id=admin_id,
            values={"phone_sales": 1},
        )
        deal_id = db.record_deal_post(
            conn, store_id=audit_only, user_id=admin_id, closed=True, model="测试", phone="13800000001"
        )
        assert db.delete_deal_post(conn, deal_id, audit_only, admin_id)
        # 业务行已删，只剩 deal_edits 审计行
        assert conn.execute("SELECT 1 FROM deal_posts WHERE store_id=?", (audit_only,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM deal_edits WHERE store_id=?", (audit_only,)).fetchone()
        with pytest.raises(ValueError):
            db.delete_store(conn, audit_only)
    blocked = admin_client.post(
        "/settings",
        data={"action": "delete_store", "tab": "stores", "store_id": str(used)},
        follow_redirects=True,
    )
    assert "不能删" in blocked.get_data(as_text=True)
    ok = admin_client.post(
        "/settings",
        data={"action": "delete_store", "tab": "stores", "store_id": str(empty)},
        follow_redirects=True,
    )
    assert "门店已删除" in ok.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT 1 FROM stores WHERE id=?", (empty,)).fetchone() is None


def test_edits_page_paginates(admin_client):
    """修改审计超过一页时分页，过滤参数保持。"""
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        # 造 55 条审计记录
        for i in range(55):
            db.record_edit(
                conn,
                biz_date=date.today(),
                store_id=sid,
                user_id=uid,
                before={"phone_sales": i},
                after={"phone_sales": i + 1},
                note="压测",
            )
    page1 = admin_client.get("/edits").get_data(as_text=True)
    assert "共 55 条" in page1
    assert "下一页" in page1
    assert "上一页" in page1  # 不到最后一页时上一页也应可用
    page2 = admin_client.get("/edits?page=2").get_data(as_text=True)
    assert "第 2/2" in page2
    # 带类型筛选翻页时 kind 不能丢
    deal_page = admin_client.get("/edits?kind=daily").get_data(as_text=True)
    assert "kind=daily" in deal_page
    last = admin_client.get("/edits?page=10").get_data(as_text=True)
    assert "第 2/2" in last  # 越界被夹回最大页
    assert "disabled" in last  # 最后一页的下一页禁用


def test_bisuan_mobile_shows_in_audit_page(admin_client):
    """移动校准要能在修改审计里查到谁改的。"""
    day = db.today_local()
    with db.get_db() as conn:
        sid = conn.execute(
            "SELECT id FROM stores WHERE COALESCE(mobile_code,'')!='' LIMIT 1"
        ).fetchone()["id"]
    admin_client.post(
        "/bulletin/bisuan-mobile",
        data={
            "store_id": str(sid),
            "date": day.isoformat(),
            "asof": day.isoformat(),
            "mobile": "9.5",
            "city": "",
        },
        follow_redirects=True,
    )
    page = admin_client.get("/edits?kind=mobile&days=1").get_data(as_text=True)
    assert "移动校准" in page


def test_store_picker_has_city_and_manager_groupby(admin_client):
    """选店下拉按地市/经理分组，设置页也有同样的分组开关。"""
    rec = admin_client.get("/deal/records").get_data(as_text=True)
    assert 'data-group="city"' in rec
    assert 'data-group="manager"' in rec
    assert "张管理" in rec
    settings = admin_client.get("/settings?tab=stores").get_data(as_text=True)
    assert 'id="storeGroupBy"' in settings
    assert 'data-group="manager"' in settings

