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
    app_client.post("/login", data={"username": "admin", "pin": "1234"})
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
    home = app_client.post("/login", data={"username": "admin", "pin": "1234"}, follow_redirects=True)
    assert "示例日报" in home.get_data(as_text=True)


def test_broadcast_compact_is_admin_setting(app_client):
    from datetime import date as _date

    app_client.post("/login", data={"username": "admin", "pin": "1234"})
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
    app_client.post("/login", data={"username": "admin", "pin": "1234"})
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
    app_client.post("/login", data={"username": "admin", "pin": "1234"})
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
    assert "账户" in people
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


def test_3_open_redirect_blocked(app_client):
    resp = app_client.post(
        "/login?next=//evil.com",
        data={"username": "admin", "pin": "1234"},
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
    c.post("/login", data={"username": "admin", "pin": "1234"})
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
    client.post("/login", data={"username": "admin", "pin": "1234", "_csrf_token": "prelogin"})
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


def test_bisuan_accepts_one_decimal_and_week_calibrates(app_client):
    from datetime import date as _date
    from datetime import timedelta

    app_client.post("/login", data={"username": "admin", "pin": "1234"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    monday = _date.today() - timedelta(days=_date.today().weekday())
    saved = app_client.post(
        "/today",
        data={"store_id": str(sid), "date": monday.isoformat(), "m_bisuan": "1.5", "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已保存" in saved
    today_page = app_client.get(f"/today?store_id={sid}&date={monday.isoformat()}").get_data(as_text=True)
    assert 'value="1.5"' in today_page or "1.5" in today_page
    with db.get_db() as conn:
        stored = conn.execute(
            "SELECT day_value FROM daily_facts WHERE store_id=? AND biz_date=? AND metric_code='bisuan'",
            (sid, monday.isoformat()),
        ).fetchone()["day_value"]
        assert stored == 15
    sunday = monday + timedelta(days=6)
    calibrated = app_client.post(
        "/report/bisuan-week",
        data={"store_id": str(sid), "start": monday.isoformat(), "end": sunday.isoformat(), "official": "2.0"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "官方数" in calibrated
    with db.get_db() as conn:
        week = conn.execute(
            "SELECT COALESCE(SUM(day_value),0) AS n FROM daily_facts WHERE store_id=? AND biz_date>=? AND biz_date<=? AND metric_code IN ('bisuan','bisuan_high')",
            (sid, monday.isoformat(), sunday.isoformat()),
        ).fetchone()["n"]
        assert week == 20


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
    c.post("/login", data={"username": "admin", "pin": "1234"})
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

    app_client.post("/login", data={"username": "admin", "pin": "1234"})
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
    app_client.post("/login", data={"username": "admin", "pin": "1234"})
    rec = app_client.get("/deal/records").get_data(as_text=True)
    assert 'data-group="city"' in rec
    assert 'data-group="manager"' in rec
    assert "张管理" in rec
    settings = app_client.get("/settings?tab=stores").get_data(as_text=True)
    assert 'id="storeGroupBy"' in settings
    assert 'data-group="manager"' in settings
