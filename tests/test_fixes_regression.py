"""修复回归测试：1) set_stores 保存门店权限 2) 种子不覆盖管理员改动 3) 开放重定向 4) net 含顾问罚。"""

from datetime import date

from app import db
from app.incentive import judge
from app.web import create_app


def test_login_brand_is_admin_setting(app_client):
    page = app_client.get("/login").get_data(as_text=True)
    assert "零售运营中心" in page
    assert "南通零售运营中心" not in page
    assert ">vivo</span>" in page
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    settings = app_client.get("/settings?tab=brand").get_data(as_text=True)
    assert "登录页" in settings
    saved = app_client.post(
        "/settings",
        data={
            "action": "save_brand",
            "tab": "brand",
            "brand_mark": "品牌",
            "brand_kicker": "示例运营中心",
            "brand_title": "示例日报",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "登录页标题已保存" in saved
    app_client.get("/logout")
    login = app_client.get("/login").get_data(as_text=True)
    assert "示例运营中心" in login
    assert "示例日报" in login
    assert ">品牌</span>" in login
    home = app_client.post("/login", data={"username": "admin", "pin": "123456"}, follow_redirects=True)
    assert "示例日报" in home.get_data(as_text=True)


def test_broadcast_compact_is_admin_setting(app_client):
    from datetime import date as _date

    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    today_html = app_client.get("/today").get_data(as_text=True)
    assert "数字化里日=0" not in today_html
    settings = app_client.get("/settings?tab=broadcast").get_data(as_text=True)
    assert "数字化里日=0 且累=0 的行不进群消息" in settings
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    day = _date.today().isoformat()
    saved = app_client.post(
        "/today",
        data={"store_id": str(sid), "date": day, "m_cloud_disk": "0", "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "云盘：日0，累0" not in saved
    app_client.post("/settings", data={"action": "save_broadcast", "tab": "broadcast"}, follow_redirects=True)
    with db.get_db() as conn:
        assert db.get_setting(conn, "broadcast_compact", "1") == "0"
    again = app_client.post(
        "/today",
        data={"store_id": str(sid), "date": day, "m_cloud_disk": "0", "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "云盘：日0，累0" in again


def test_add_store_uses_city_and_hides_internal_code(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    page = app_client.get("/settings?tab=stores").get_data(as_text=True)
    assert "内部编码" not in page
    assert "地市" in page
    resp = app_client.post(
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
    bulletin = app_client.get("/bulletin?city=示例市").get_data(as_text=True)
    assert "测试新店" not in bulletin
    tz = app_client.get("/bulletin?city=邻市").get_data(as_text=True)
    assert "测试新店" in tz
    assert "示例甲店" not in tz


def test_1_set_stores_persists(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        stores = db.list_all_stores(conn)
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        other = stores[-1]["id"]
    resp = app_client.post(
        "/settings",
        data={
            "action": "set_stores",
            "tab": "people",
            "user_id": str(uid),
            "store_ids": str(other),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        assert db.user_store_ids(conn, uid) == [other]
    people = app_client.get("/settings?tab=people").get_data(as_text=True)
    assert "人员账号" in people
    assert "添加账号" in people
    assert "本店账号" not in people
    stores_page = app_client.get("/settings?tab=stores").get_data(as_text=True)
    assert "门店" in stores_page
    assert "本店账号" not in stores_page
    assert "添加账号" not in stores_page


def test_2_seed_does_not_overwrite_admin_edits(tmp_db):
    with db.get_db() as conn:
        store = conn.execute("SELECT * FROM stores LIMIT 1").fetchone()
        conn.execute(
            "UPDATE stores SET active=0, store_manager='管理员手改', area_manager='手改A' WHERE id=?",
            (store["id"],),
        )
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        db.set_user_active(conn, uid, False)
        other = conn.execute("SELECT id FROM stores ORDER BY id DESC LIMIT 1").fetchone()["id"]
        db.set_user_stores(conn, uid, [other])
    # 重启（再次 init_db）不应把改动冲掉
    db.init_db()
    with db.get_db() as conn:
        s = conn.execute("SELECT * FROM stores WHERE id=?", (store["id"],)).fetchone()
        assert s["active"] == 0
        assert s["store_manager"] == "管理员手改"
        assert s["area_manager"] == "手改A"
        u = conn.execute("SELECT active FROM users WHERE username='alpha'").fetchone()
        assert u["active"] == 0
        uid2 = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        assert db.user_store_ids(conn, uid2) == [other]
    # 管理员把店清空后重启，也不该被种子偷偷补回目录默认店
    with db.get_db() as conn:
        db.set_user_stores(conn, uid, [])
    db.init_db()
    with db.get_db() as conn:
        assert db.user_store_ids(conn, uid) == []


def test_delete_empty_store_and_block_store_with_facts(tmp_db, app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        empty = db.create_store(conn, "空店", short_name="空店")
        used = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        db.save_daily(
            conn,
            store_id=used,
            biz_date=date.today(),
            user_id=conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"],
            values={"phone_sales": 1},
        )
    blocked = app_client.post(
        "/settings",
        data={"action": "delete_store", "tab": "stores", "store_id": str(used)},
        follow_redirects=True,
    )
    assert "不能删" in blocked.get_data(as_text=True)
    ok = app_client.post(
        "/settings",
        data={"action": "delete_store", "tab": "stores", "store_id": str(empty)},
        follow_redirects=True,
    )
    assert "门店已删除" in ok.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT 1 FROM stores WHERE id=?", (empty,)).fetchone() is None


def test_3_open_redirect_blocked(app_client):
    resp = app_client.post(
        "/login?next=//evil.com",
        data={"username": "admin", "pin": "123456"},
        follow_redirects=True,
    )
    assert resp.request.path == "/today"


def test_report_ignores_inactive_metric_facts(tmp_db, monkeypatch):
    """即使某天留下了停用指标的 day 值，报表也不能崩，应忽略。"""
    from datetime import date as _date

    app_client = _admin_client(tmp_db)
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        at = _date.today().isoformat()
        # 停用一个指标并故意留下它的历史 day 值
        conn.execute("UPDATE metrics SET active=0 WHERE code='watch_pack'")
        conn.execute(
            "INSERT OR REPLACE INTO daily_facts(biz_date, store_id, metric_code, day_value) VALUES (?,?,?,?)",
            (at, sid, "watch_pack", 3),
        )
        # 再留一个当前活跃指标的 day 值
        conn.execute(
            "INSERT OR REPLACE INTO daily_facts(biz_date, store_id, metric_code, day_value) VALUES (?,?,?,?)",
            (at, sid, "phone_sales", 7),
        )
    resp = app_client.get("/report")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "手机销量" in body


def _admin_client(tmp_db):

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    return c


def test_csrf_blocks_unsigned_post(tmp_db, monkeypatch):
    """正式（非 TESTING）下，写操作必须带 CSRF token。"""
    from app.web import create_app as _create_app

    app = _create_app()
    app.config["TESTING"] = False
    client = app.test_client()
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        today = date.today().isoformat()
    # 1) 登录（带 pre-login token）
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "prelogin"
    client.post("/login", data={"username": "admin", "pin": "123456", "_csrf_token": "prelogin"})
    # 2) 登录后获取新 token
    client.get("/today")
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    assert token and token != "prelogin"
    # 3) 不带 token 的 POST 被拒
    bad = client.post(
        "/today",
        data={"store_id": str(sid), "date": today, "m_phone_sales": "3"},
        follow_redirects=True,
    )
    assert "校验失败" in bad.get_data(as_text=True)
    with db.get_db() as conn:
        assert db.get_report(conn, sid, date.today()) is None
    # 4) 带 token 的 POST 成功
    ok = client.post(
        "/today",
        data={
            "store_id": str(sid), "date": today, "m_phone_sales": "3", "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert "已保存" in ok.get_data(as_text=True)
    assert "校验失败" not in ok.get_data(as_text=True)


def test_edits_page_paginates(tmp_db, monkeypatch):
    """修改审计超过一页时分页，过滤参数保持。"""
    from datetime import date as _date

    app_client = _admin_client(tmp_db)
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        day = _date.today().isoformat()
        # 造 55 条审计记录
        for i in range(55):
            db.record_edit(
                conn,
                biz_date=_date.today(),
                store_id=sid,
                user_id=uid,
                before={"phone_sales": i},
                after={"phone_sales": i + 1},
                note="压测",
            )
            _ = day
    page1 = app_client.get("/edits").get_data(as_text=True)
    assert "settings-nav" in page1
    assert "备份恢复" in page1
    assert "共 55 条" in page1
    assert "下一页" in page1
    assert "上一页" in page1  # 不到最后一页时上一页也应可用
    page2 = app_client.get("/edits?page=2").get_data(as_text=True)
    assert "第 2/2" in page2
    # 带类型筛选翻页时 kind 不能丢
    deal_page = app_client.get("/edits?kind=daily").get_data(as_text=True)
    assert "kind=daily" in deal_page
    last = app_client.get("/edits?page=10").get_data(as_text=True)
    assert "第 2/2" in last  # 越界被夹回最大页
    assert "第 10" not in last
    assert "disabled" in last  # 最后一页的下一页禁用


def test_bisuan_accepts_one_decimal_and_month_calibrates(app_client):
    from datetime import date as _date

    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    day = _date.today()
    saved = app_client.post(
        "/today",
        data={"store_id": str(sid), "date": day.isoformat(), "m_bisuan": "1.5", "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已保存" in saved
    today_page = app_client.get(f"/today?store_id={sid}&date={day.isoformat()}").get_data(as_text=True)
    assert 'value="1.5"' in today_page or "1.5" in today_page
    with db.get_db() as conn:
        stored = conn.execute(
            "SELECT day_value FROM daily_facts WHERE store_id=? AND biz_date=? AND metric_code='bisuan'",
            (sid, day.isoformat()),
        ).fetchone()["day_value"]
        assert stored == 15
    # 移动取数只存对照，不改填报 daily_facts
    from datetime import timedelta as _td

    asof = day - _td(days=1)
    if asof.month != day.month:
        asof = day  # 月初只能落到当天
    # 截止日那天先有 1.5；录移 2.0 后填报仍应是 1.5
    app_client.post(
        "/today",
        data={"store_id": str(sid), "date": asof.isoformat(), "m_bisuan": "1.5", "m_phone_sales": "1"},
        follow_redirects=True,
    )
    with db.get_db() as conn:
        before_total = conn.execute(
            "SELECT COALESCE(SUM(day_value),0) AS n FROM daily_facts "
            "WHERE store_id=? AND biz_date>=? AND biz_date<=? AND metric_code IN ('bisuan','bisuan_high')",
            (sid, day.replace(day=1).isoformat(), asof.isoformat()),
        ).fetchone()["n"]
    calibrated = app_client.post(
        "/bulletin/bisuan-mobile",
        data={
            "store_id": str(sid),
            "date": day.isoformat(),
            "asof": asof.isoformat(),
            "mobile": "2.0",
            "city": "",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已录移" in calibrated or "移" in calibrated
    assert "填报未改" in calibrated
    with db.get_db() as conn:
        month_start = day.replace(day=1).isoformat()
        asof_total = conn.execute(
            "SELECT COALESCE(SUM(day_value),0) AS n FROM daily_facts "
            "WHERE store_id=? AND biz_date>=? AND biz_date<=? AND metric_code IN ('bisuan','bisuan_high')",
            (sid, month_start, asof.isoformat()),
        ).fetchone()["n"]
        assert asof_total == before_total == 15  # 填报 1.5 不变
        # 移动校准数现在落在 bisuan_mobile 表（整数 0.1 精度），并留审计
        row = db.get_bisuan_mobile(conn, sid, day.strftime("%Y-%m"))
        assert row and row["value_tenths"] == 20
        assert row["asof"] == asof.isoformat()
        edits = db.list_bisuan_mobile_edits(conn, month=day.strftime("%Y-%m"), store_id=sid)
        assert edits and edits[0]["after"]["value_tenths"] == 20
    page = app_client.get(f"/bulletin?date={day.isoformat()}").get_data(as_text=True)
    assert "移2.0" in page
    # 今天没更新移数（截止日早于通报表日）=> 复盘不带分店对照
    if asof < day:
        assert "分店对照" not in page
        assert "移动数据更新至" in page  # 表头仍标截止日
    else:
        assert "笔算移取" in page or "分店对照" in page
    # 表单默认截止日前一天
    if day.day > 1:
        assert f'name="asof" value="{asof.isoformat()}"' in page or asof.isoformat() in page


def test_4_net_includes_advisor_penalty():
    row = judge(True, 0, 10)
    assert row["advisor_penalty"] == 100
    assert row["net"] == -100
    row = judge(True, 2, 3)
    assert row["net"] == -150
    assert judge(False, 0, 0)["net"] == -100


def test_board_shows_deals_and_exports_xlsx(tmp_db):
    """看板含成交列, 点店名进报表, 当前视图可导出 Excel。"""
    from io import BytesIO

    import openpyxl

    from app.web import create_app

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(
            conn,
            store_id=sid,
            biz_date=date.today(),
            values={"ai_contract": 1, "bisuan": 2},
            user_id=uid,
        )
        db.record_deal_post(
            conn,
            store_id=sid,
            user_id=uid,
            closed=True,
            model="S60",
            phone="15500001111",
            spend="99",
        )
    page = c.get("/board").get_data(as_text=True)
    assert "触客" in page
    assert "成交/触客" in page
    assert "示例甲店" in page
    assert "/report?" in page
    r = c.get("/board.xlsx?view=today")
    assert r.status_code == 200
    assert r.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "filename=board_today_" in r.headers.get("Content-Disposition", "")
    wb = openpyxl.load_workbook(BytesIO(r.get_data()))
    header = [cell.value for cell in wb.active[1]]
    assert header[:3] == ["排名", "门店", "地市"]
    assert "触客" in header and "成功率" in header


def test_bulletin_skips_stores_without_mobile_code(app_client):
    """没有移动编码的店不出现在通报表里。"""
    from app import db as _db

    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    with _db.get_db() as conn:
        # 加两家店：一个没编码，一个有编码
        _db.create_store(
            conn,
            "TZ测试没编码店",
            mobile_code="",
            short_name="没编码店",
            area_manager="测试",
        )
        _db.create_store(
            conn,
            "TZ测试有编码店",
            mobile_code="20999999",
            short_name="有编码店",
            area_manager="测试",
        )
    page = app_client.get("/bulletin").get_data(as_text=True).replace("\ufeff", "")
    # 有编码的店保留（南通通报表默认视图，两店都默认南通）
    assert "测试有编码店" in page
    # 没有编码的店不出现
    assert "测试没编码店" not in page
    assert "没编码店" not in page
    # 没编码的泰州新店不该把「泰州市」带进下拉
    assert 'value="泰州市"' not in page
    empty = app_client.get("/bulletin?city=泰州市").get_data(as_text=True).replace("\ufeff", "")
    assert "测试有编码店" in empty  # 非法地市回退到南通
    assert "示例戊店" not in empty


def test_store_picker_has_city_and_manager_groupby(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    rec = app_client.get("/deal/records").get_data(as_text=True)
    assert 'data-group="city"' in rec
    assert 'data-group="manager"' in rec
    assert "张管理" in rec
    settings = app_client.get("/settings?tab=stores").get_data(as_text=True)
    assert 'id="storeGroupBy"' in settings
    assert 'data-group="manager"' in settings


def test_settings_preserves_multi_store_permissions_and_escapes_dynamic_text(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        stores = db.list_all_stores(conn)
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        selected = [stores[0]["id"], stores[1]["id"]]
    response = app_client.post(
        "/settings",
        data={
            "action": "set_stores",
            "tab": "people",
            "user_id": str(uid),
            "store_ids": [str(store_id) for store_id in selected],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with db.get_db() as conn:
        assert db.user_store_ids(conn, uid) == selected
    page = app_client.get("/settings?tab=people").get_data(as_text=True)
    assert 'class="people-store-select" multiple' in page
    assert "onclick=\"applyReviewPreset" not in page


def test_bisuan_mobile_migrates_from_old_settings(tmp_db):
    """旧 app_meta 键要能搬进新表，且截止日按店保留。"""
    from app.db_bisuan_mobile import _migrate_bisuan_mobile_from_settings

    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        db.set_setting(conn, f"bisuan_mobile_{sid}_2026-07", "12.5")
        db.set_setting(conn, "bisuan_mobile_asof_2026-07", "2026-07-20")
        _migrate_bisuan_mobile_from_settings(conn)
        row = db.get_bisuan_mobile(conn, sid, "2026-07")
        assert row["value_tenths"] == 125
        assert row["asof"] == "2026-07-20"
        # 旧键已清掉，重复迁移不会翻倍
        _migrate_bisuan_mobile_from_settings(conn)
        assert db.get_bisuan_mobile(conn, sid, "2026-07")["value_tenths"] == 125
        assert db.get_setting(conn, f"bisuan_mobile_{sid}_2026-07", "") == ""


def test_bisuan_mobile_rejects_negative(tmp_db):
    from datetime import date as _date

    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        try:
            db.save_bisuan_mobile(
                conn, store_id=sid, month="2026-08", value_tenths=-5, asof=_date(2026, 8, 20)
            )
        except ValueError:
            pass
        else:
            raise AssertionError("负数应被拒绝")


def test_bisuan_mobile_shows_in_audit_page(app_client):
    """移动校准要能在修改审计里查到谁改的。"""
    day = db.today_local()
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute(
            "SELECT id FROM stores WHERE COALESCE(mobile_code,'')!='' LIMIT 1"
        ).fetchone()["id"]
    app_client.post(
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
    page = app_client.get("/edits?kind=mobile&days=1").get_data(as_text=True)
    assert "移动校准" in page


def test_build_deviation_board_sorts_and_signs():
    """偏差榜：按绝对值降序，少报=正、多报=负，无移动的店排除。"""
    from app.insights import build_deviation_board

    stores = [
        {"id": 1, "short_name": "甲店", "name": "甲店", "city": "南通"},
        {"id": 2, "short_name": "乙店", "name": "乙店", "city": "泰州"},
        {"id": 3, "short_name": "丙店", "name": "丙店", "city": ""},
    ]
    facts = {
        1: {"bisuan": 100, "bisuan_high": 20},  # 填报120 < 移动150 → +30 少报
        2: {"bisuan": 200, "bisuan_high": 0},   # 填报200 > 移动150 → -50 多报
    }
    mobile = {1: 150, 2: 150}
    rows = build_deviation_board(
        stores=stores, month_facts=facts, mobile_bisuan=mobile
    )
    assert [r["id"] for r in rows] == [2, 1]  # |−50| 排前
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["diff"] == 30 and by_id[1]["under"] is True
    assert by_id[2]["diff"] == -50 and by_id[2]["over"] is True
    assert 3 not in by_id  # 无移动校准数，排除


def test_deviation_page_admin_only(app_client):
    """偏差路由是管理员专属，且能渲染（单位是个，不是元）。"""
    with db.get_db() as conn:
        conn.execute("UPDATE users SET must_change_pin=0")
        sid = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        db.save_bisuan_mobile(
            conn, store_id=sid, month="2026-08", value_tenths=120, asof=db.today_local()
        )
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    r = app_client.get("/deviation?month=2026-08-01")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "填报偏差榜" in html
    assert "温差" in html
    assert "元" not in html  # 计数单位是个，不是金额
    assert "少报" in html and "多报" in html


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
        u2 = fillers[1]["id"]
        pid = db.save_policy(conn, title="考勤", body="第一版", user_id=admin["id"])
        # 只有 u1 已读
        db.mark_policy_read(conn, u1, pid)
        _ = u2
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
    assert t2["read"] == 0  # 版本号追上 b4，之前读了也不算


def test_policy_image_upload_and_serve(app_client):
    """政策插图：管理员可传 PNG，非图片被拒，非管理员被拦。"""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    r = app_client.post(
        "/settings/policy-image",
        data={"file-0": (__import__("io").BytesIO(png), "口径.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    url = r.get_json()["result"][0]["url"]
    assert url.startswith("/uploads/policy/")
    # 能取回
    got = app_client.get(url)
    assert got.status_code == 200 and got.data[:8] == b"\x89PNG\r\n\x1a\n"
    # 非图片被拒
    bad = app_client.post(
        "/settings/policy-image",
        data={"file-0": (__import__("io").BytesIO(b"not an image"), "x.png")},
        content_type="multipart/form-data",
    )
    assert bad.status_code == 400
    # 非管理员被拦
    c2 = app_client.application.test_client()
    c2.post("/login", data={"username": "alpha", "pin": "123456"})
    r2 = c2.post(
        "/settings/policy-image",
        data={"file-0": (__import__("io").BytesIO(png), "a.png")},
        content_type="multipart/form-data",
    )
    assert r2.status_code in (302, 403)


def test_policy_image_upload_is_resized_down(app_client):
    """政策插图上传后服务端把超大图缩到长边 <= 1600。"""
    from io import BytesIO

    from PIL import Image

    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    big = BytesIO()
    Image.new("RGB", (4000, 2000), (200, 100, 50)).save(big, "PNG")
    big.seek(0)
    r = app_client.post(
        "/settings/policy-image",
        data={"file-0": (big, "big.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    url = r.get_json()["result"][0]["url"]
    saved = app_client.get(url)
    assert saved.status_code == 200
    im = Image.open(BytesIO(saved.data))
    assert max(im.width, im.height) <= 1600, (im.width, im.height)
