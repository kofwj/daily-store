import re
from datetime import date
from io import BytesIO

import openpyxl

from app import db
from app.stores_seed import STORES


def test_catalog_includes_advance_workbook_stores(tmp_db):
    names = {item["name"] for item in STORES}
    assert "示例市甲街vivo体验店" in names
    assert "邻市戊路vivo体验店" in names
    assert "邻市巳街vivo专卖店" in names
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM stores WHERE code='store-gamma'").fetchone()
        assert row is not None
        assert row["area_manager"] == "张管理"


def test_filler_must_provide_phone(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    page = client.post(
        "/advance",
        data={"store_id": str(sid), "biz_date": db.today_local().isoformat(), "rebate": "100", "note": "购机让利"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "必须带号码" in page
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM advance_posts WHERE store_id=?", (sid,)).fetchone()["n"]
        assert n == 0


def test_filler_saves_and_admin_pays(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local().isoformat()
    saved = client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": today,
            "phone": "13900001111",
            "broadband": "200",
            "rebate": "100",
            "note": "宽带电视",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "垫资已保存" in saved
    # 店员视角号码打码
    assert "139****1111" in saved
    assert "13900001111" not in saved
    assert "未兑" in saved
    with db.get_db() as conn:
        aid = conn.execute("SELECT id FROM advance_posts WHERE store_id=?", (sid,)).fetchone()["id"]
    client.post("/logout")
    client.post("/login", data={"username": "admin", "pin": "123456"})
    paid = client.post(
        "/advance/pay",
        data={"action": "pay", "advance_id": [str(aid)], "month": today[:7], "paid": "0"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已兑付 1 笔" in paid
    locked = client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "advance_id": str(aid),
            "biz_date": today,
            "phone": "13900001111",
            "rebate": "50",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已兑付的垫资不能改" in locked
    gone = client.post(
        "/advance/delete",
        data={"store_id": str(sid), "advance_id": str(aid)},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已兑付的垫资不能删" in gone
    with db.get_db() as conn:
        row = conn.execute("SELECT paid, rebate, broadband FROM advance_posts WHERE id=?", (aid,)).fetchone()
        assert int(row["paid"]) == 1
        assert int(row["broadband"]) == 20000
        assert int(row["rebate"]) == 10000


def test_admin_can_save_without_phone_and_fills_settlement(tmp_db, admin_client):
    c = admin_client
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local()
    page = c.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": today.isoformat(),
            "other": "39.99",
            "note": "芝麻服务费",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "垫资已保存" in page
    assert "芝麻服务费" in page
    r = c.get(f"/incentive.xlsx?month={today.strftime('%Y-%m')}")
    assert r.status_code == 200
    wb = openpyxl.load_workbook(BytesIO(r.get_data()))
    ws = wb["移动接入"]
    found = False
    for row in ws.iter_rows(min_row=5, max_col=8, values_only=True):
        if row[1] == "示例市甲街vivo体验店":
            assert row[7] == 39.99
            found = True
            break
    assert found
    export = c.get(f"/advance.xlsx?month={today.strftime('%Y-%m')}")
    assert export.status_code == 200
    book = openpyxl.load_workbook(BytesIO(export.get_data()))
    assert "汇总表" in book.sheetnames
    assert "示例甲店" in book.sheetnames
    assert book["示例甲店"]["H3"].value == "芝麻服务费"
    names = [row[0] for row in book["汇总表"].iter_rows(min_col=1, max_col=1, values_only=True)]
    assert "示例公司甲" not in names
    assert "南通运营公司" in names or any("南通" in str(n) for n in names if n)


def test_filler_rejects_future_and_nonfinite_amounts(client):
    from datetime import timedelta

    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    future = client.post("/advance", data={"store_id": sid, "biz_date": (db.today_local() + timedelta(days=1)).isoformat(), "phone": "13900000000", "rebate": "10"}, follow_redirects=True)
    assert "未来日期" in future.get_data(as_text=True)
    invalid = client.post("/advance", data={"store_id": sid, "biz_date": db.today_local().isoformat(), "phone": "13900000000", "rebate": "nan"}, follow_redirects=True)
    assert "金额请填数字" in invalid.get_data(as_text=True)


def test_filler_can_save_negative_amount(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    page = client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": db.today_local().isoformat(),
            "phone": "13900004444",
            "broadband": "-100",
            "note": "退网络电视",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "垫资已保存" in page
    assert "退款填负数" in page
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT broadband FROM advance_posts WHERE store_id=? AND phone='13900004444'",
            (sid,),
        ).fetchone()
        assert int(row["broadband"]) == -10000


def test_advance_actions_are_audited(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local().isoformat()
    client.post("/advance", data={"store_id": sid, "biz_date": today, "phone": "13900007777", "rebate": "10"})
    with db.get_db() as conn:
        aid = conn.execute("SELECT id FROM advance_posts WHERE phone='13900007777'").fetchone()["id"]
    client.post("/advance", data={"store_id": sid, "advance_id": aid, "biz_date": today, "phone": "13900007777", "rebate": "20"})
    client.post("/logout")
    client.post("/login", data={"username": "admin", "pin": "123456"})
    client.post("/advance/pay", data={"action": "pay", "advance_id": [str(aid)]})
    client.post("/advance/pay", data={"action": "unpay", "advance_id": [str(aid)]})
    client.post("/advance/delete", data={"store_id": sid, "advance_id": aid})
    with db.get_db() as conn:
        actions = [row["action"] for row in conn.execute("SELECT action FROM advance_edits ORDER BY id")]
    assert actions == ["create", "update", "pay", "unpay", "delete"]


def test_anonymous_cannot_delete_advance(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local().isoformat()
    client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": today,
            "phone": "13900006666",
            "rebate": "10",
        },
        follow_redirects=True,
    )
    with db.get_db() as conn:
        aid = conn.execute(
            "SELECT id FROM advance_posts WHERE phone='13900006666'"
        ).fetchone()["id"]
    client.post("/logout")
    resp = client.post(
        "/advance/delete",
        data={"store_id": str(sid), "advance_id": str(aid)},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login" in (resp.headers.get("Location") or "")
    with db.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM advance_posts WHERE id=?", (aid,)
        ).fetchone()["n"]
        assert n == 1


def test_filler_cannot_open_pay_page(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    r = client.get("/advance/pay", follow_redirects=True)
    assert "需要管理员权限" in r.get_data(as_text=True)


def test_admin_advance_defaults_to_all_stores(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid_a = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        sid_b = conn.execute("SELECT id FROM stores WHERE code='store-beta'").fetchone()["id"]
    today = db.today_local().isoformat()
    client.post(
        "/advance",
        data={"store_id": str(sid_a), "biz_date": today, "phone": "13900009111", "rebate": "10"},
    )
    client.post("/logout")
    client.post("/login", data={"username": "beta", "pin": "123456"})
    client.post(
        "/advance",
        data={"store_id": str(sid_b), "biz_date": today, "phone": "13900009222", "rebate": "20"},
    )
    client.post("/logout")
    client.post("/login", data={"username": "admin", "pin": "123456"})
    page = client.get("/advance").get_data(as_text=True)
    assert "本月全店垫资" in page
    assert "13900009111" in page
    assert "13900009222" in page
    assert "示例甲店" in page
    assert "示例乙店" in page
    assert "记一笔垫资" not in page
    one = client.get(f"/advance?store_id={sid_a}").get_data(as_text=True)
    assert "记一笔垫资" in one
    assert "13900009111" in one
    assert "13900009222" not in one


def test_admin_advance_filter_by_city(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        nt = conn.execute("SELECT id FROM stores WHERE short_name='示例乙店'").fetchone()["id"]
        tz = conn.execute("SELECT id FROM stores WHERE short_name='示例丁店'").fetchone()["id"]
    today = db.today_local().isoformat()
    client.post("/advance", data={"store_id": str(nt), "biz_date": today, "phone": "13900009333", "rebate": "11"})
    client.post("/advance", data={"store_id": str(tz), "biz_date": today, "phone": "13900009444", "rebate": "22"})
    all_page = client.get("/advance").get_data(as_text=True)
    assert "13900009333" in all_page
    assert "13900009444" in all_page
    city = client.get("/advance?city=邻市").get_data(as_text=True)
    assert "13900009444" in city
    assert "13900009333" not in city


def test_store_sees_month_list_and_admin_sees_today_inbox(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local().isoformat()
    client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": today,
            "phone": "13900003333",
            "rebate": "100",
            "note": "购机让利",
        },
        follow_redirects=True,
    )
    page = client.get("/advance").get_data(as_text=True)
    assert "本月记录" in page
    # 店员视角号码打码
    assert "139****3333" in page
    assert "13900003333" not in page
    assert "改" in page and "删" in page
    client.post("/logout")
    client.post("/login", data={"username": "admin", "pin": "123456"})
    inbox = client.get("/advance/pay?scope=today&paid=0").get_data(as_text=True)
    assert "今天待兑" in inbox
    assert "示例甲店" in inbox
    assert "1笔" in inbox


def test_advance_stores_cents_and_reads_yuan(client):
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": db.today_local().isoformat(),
            "phone": "13900008888",
            "other": "39.99",
        },
        follow_redirects=True,
    )
    with db.get_db() as conn:
        raw = conn.execute(
            "SELECT other FROM advance_posts WHERE phone='13900008888'"
        ).fetchone()
        viewed = db.get_advance(conn, conn.execute(
            "SELECT id FROM advance_posts WHERE phone='13900008888'"
        ).fetchone()["id"], sid)
    assert int(raw["other"]) == 3999
    assert float(viewed["other"]) == 39.99
    assert float(viewed["total"]) == 39.99


def test_advance_range_sums_and_pay_page(client):
    client.post("/login", data={"username": "admin", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    today = db.today_local()
    client.post(
        "/advance",
        data={"store_id": str(sid), "biz_date": today.isoformat(), "phone": "13900009555", "rebate": "10.5"},
    )
    client.post(
        "/advance",
        data={"store_id": str(sid), "biz_date": today.isoformat(), "phone": "13900009556", "broadband": "20"},
    )
    with db.get_db() as conn:
        aid = conn.execute("SELECT id FROM advance_posts WHERE phone='13900009555'").fetchone()["id"]
        sums = db.advance_range_sums(conn, start=today, end=today, store_id=sid)
    assert sums["rebate"] == 10.5
    assert sums["broadband"] == 20.0
    assert sums["total"] == 30.5
    assert sums["unpaid"] == 2
    client.post("/advance/pay", data={"action": "pay", "advance_id": [str(aid)]})
    page = client.get("/advance/pay?scope=today").get_data(as_text=True)
    assert "30.50" in page or "30.5" in page
    assert "未兑 1 笔" in page
    with db.get_db() as conn:
        after = db.advance_range_sums(conn, start=today, end=today, store_id=sid)
    assert after["unpaid"] == 1
    assert after["total"] == 30.5


def test_advance_old_real_whole_yuan_converted(tmp_path):
    """旧库 REAL 列存的是元——整元（200、500）也必须乘100，不能只靠采样猜。"""

    import sqlite3

    from app import db_core

    conn = sqlite3.connect(str(tmp_path / "old.db"))
    db_core._ensure_app_meta(conn)
    conn.execute("CREATE TABLE advance_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, broadband REAL NOT NULL DEFAULT 0, rebate REAL NOT NULL DEFAULT 0, other REAL NOT NULL DEFAULT 0)")
    conn.executemany(
        "INSERT INTO advance_posts (broadband, rebate, other) VALUES (?, ?, ?)",
        [(200, 35.5, 0), (500, 0, 12.25)],
    )
    db_core._advance_amounts_to_cents(conn)
    rows = conn.execute(
        "SELECT broadband, rebate, other FROM advance_posts ORDER BY id"
    ).fetchall()
    # 整元 200→20000 分、500→50000 分；小数照转
    assert list(rows) == [(20000, 3550, 0), (50000, 0, 1225)]
    marker = conn.execute("SELECT value FROM app_meta WHERE key='advance_cents_marker'").fetchone()
    assert marker[0] == "1"
    conn.close()


def test_advance_form_keeps_input_on_error(filler_client):
    """垫资校验失败要回显已填内容，不能让店员重敲一遍。"""
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    resp = filler_client.post(
        "/advance",
        data={
            "store_id": str(sid),
            "biz_date": date.today().isoformat(),
            "phone": "13800138000",
            "broadband": "abc",
            "note": "宽带垫资备注",
        },
    )
    page = resp.get_data(as_text=True)
    assert "金额请填数字" in page
    assert "13800138000" in page
    assert "宽带垫资备注" in page


def test_advance_phone_masked_for_filler(filler_client):
    """垫资记录对店员打码，管理员保留完整号码（兑付对账用）。"""
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.record_advance(
            conn, store_id=sid, user_id=admin_id, biz_date=date.today(),
            phone="13812345678", broadband=100,
        )
    filler_page = filler_client.get("/advance").get_data(as_text=True)
    assert "138****5678" in filler_page
    assert "13812345678" not in filler_page
    filler_client.post("/logout")
    filler_client.post("/login", data={"username": "admin", "pin": "123456"})
    admin_page = filler_client.get("/advance").get_data(as_text=True)
    assert "13812345678" in admin_page


def _seed_advances(rows, store_code="store-alpha"):
    """把 (门店编码, 让利金额) 灌成当天的未兑付记录；返回 (store_id, admin_id)。"""
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code=?", (store_code,)).fetchone()["id"]
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        for code, amount in rows:
            target = sid
            if code is not None:
                target = conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]
            db.record_advance(
                conn, store_id=target, user_id=admin_id, biz_date=db.today_local(), rebate=amount
            )
    return sid, admin_id


def test_advance_pay_pick_all_and_selected_total(tmp_db, admin_client):
    """勾选区：全选按钮、每行金额、批量按钮上的笔数与合计都得出得来。"""
    c = admin_client
    _seed_advances([(None, 10.0), (None, 20.0), (None, 30.5)])
    page = c.get("/advance/pay?scope=today").get_data(as_text=True)
    assert 'id="pickAllBtn"' in page and 'id="pickNoneBtn"' in page
    assert 'id="pickSum"' in page and "已选 0 笔 · 合计 0.00 元" in page
    # 每行带自己的合计，浏览器按分累加
    for amount in ("10.00", "20.00", "30.50"):
        assert 'data-total="%s"' % amount in page
    # 批量按钮的笔数 / 金额由服务端算
    assert "全部兑付（未兑 3 笔 · 60.50）" in page
    assert 'data-confirm="按当前筛选兑付全部 3 笔，合计 60.50 元？"' in page
    assert 'id="payForm"' in page
    # 未兑付视图下不该出现撤回按钮
    assert "全部取消兑付" not in page


def test_advance_pay_all_covers_every_page(tmp_db, admin_client):
    """一页 50 笔：第二页的也能被「全部兑付」一次处理掉。"""
    c = admin_client
    today = db.today_local()
    _seed_advances([(None, 1.0)] * 51)
    page = c.get("/advance/pay?scope=today").get_data(as_text=True)
    assert "本页 50 笔 · 本期筛选共 51 笔" in page
    assert "全部兑付（未兑 51 笔 · 51.00）" in page
    done = c.post(
        "/advance/pay",
        data={"action": "pay_all", "scope": "today", "month": today.strftime("%Y-%m"), "paid": "0"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已兑付 51 笔（当前筛选下全部）" in done
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM advance_posts WHERE paid=0").fetchone()[0] == 0


def test_advance_unpay_all_keeps_sesame(tmp_db, admin_client):
    """批量取消兑付不能动芝麻导入的记录（它导入即已兑）。"""
    c = admin_client
    today = db.today_local()
    sid, admin_id = _seed_advances([(None, 10.0)])
    with db.get_db() as conn:
        db.record_advance(
            conn, store_id=sid, user_id=admin_id, biz_date=today, sesame=9.58,
            source="sesame", ext_id="ses-1", paid=True,
        )
        conn.execute("UPDATE advance_posts SET paid=1 WHERE rebate=1000 AND source=''")
    page = c.get("/advance/pay?scope=today&paid=1").get_data(as_text=True)
    # 芝麻那条不算「可撤回」，按钮只报普通那 1 笔
    assert "全部取消兑付（已兑 1 笔 · 10.00）" in page
    c.post(
        "/advance/pay",
        data={"action": "unpay_all", "scope": "today", "month": today.strftime("%Y-%m"), "paid": "1"},
        follow_redirects=True,
    )
    with db.get_db() as conn:
        assert int(conn.execute("SELECT paid FROM advance_posts WHERE ext_id='ses-1'").fetchone()["paid"]) == 1
        left = conn.execute("SELECT COUNT(*) FROM advance_posts WHERE source!='sesame' AND paid=1").fetchone()[0]
    assert left == 0


def test_advance_bulk_respects_city_scope(tmp_db, admin_client):
    """按地市筛着用批量兑付，不能碰到别的地市的记录。"""
    c = admin_client
    today = db.today_local()
    _seed_advances([(None, 10.0)])            # store-alpha：示例市
    _seed_advances([("store-delta", 20.0)])   # store-delta：邻市
    page = c.get("/advance/pay?scope=today&city=示例市").get_data(as_text=True)
    # 地市范围要挂在表单 action 上，POST 才认得出当前范围
    assert "city=" in page
    c.post(
        "/advance/pay?city=示例市",
        data={"action": "pay_all", "scope": "today", "month": today.strftime("%Y-%m"), "paid": "0"},
        follow_redirects=True,
    )
    with db.get_db() as conn:
        paid = {
            row["code"]: int(row["paid"])
            for row in conn.execute(
                "SELECT s.code AS code, a.paid AS paid FROM advance_posts a "
                "JOIN stores s ON s.id = a.store_id"
            )
        }
        ids = db.list_advance_ids(
            conn,
            store_id=None,
            start=today,
            end=today,
            paid=0,
            store_ids=[int(r["id"]) for r in conn.execute("SELECT id FROM stores WHERE city='示例市'")],
        )
    assert paid == {"store-alpha": 1, "store-delta": 0}
    assert ids == []


def test_advance_pay_all_hits_cardinality_guard(tmp_db, admin_client, monkeypatch):
    """区间大到离谱时宁可不动：护栏拦住并提示缩小范围。"""
    c = admin_client
    today = db.today_local()
    _seed_advances([(None, 10.0), (None, 20.0)])
    from app import views_advance

    monkeypatch.setattr(views_advance, "MAX_BULK_PAY", 1)
    page = c.post(
        "/advance/pay",
        data={"action": "pay_all", "scope": "today", "month": today.strftime("%Y-%m"), "paid": "0"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "超过一次 1 笔的上限" in page
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM advance_posts WHERE paid=0").fetchone()[0] == 2


def test_advance_bulk_refuses_inaccessible_store(tmp_db, admin_client):
    """表单里带着的门店已经停用 / 不在范围：批量必须中止，不能回落成「全部门店」。"""
    c = admin_client
    today = db.today_local()
    sid, _ = _seed_advances([(None, 10.0)])       # store-alpha
    _seed_advances([("store-delta", 20.0)])       # store-delta
    with db.get_db() as conn:
        conn.execute("UPDATE stores SET active=0 WHERE code='store-alpha'")
    page = c.post(
        "/advance/pay",
        data={
            "action": "pay_all",
            "scope": "today",
            "month": today.strftime("%Y-%m"),
            "paid": "0",
            "store_id": str(sid),
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已停用或不在你的范围内" in page
    with db.get_db() as conn:
        # 一笔都不许兑：尤其不能把别家店的未兑付顺手兑掉
        assert conn.execute("SELECT COUNT(*) FROM advance_posts WHERE paid=1").fetchone()[0] == 0


def test_advance_paid_view_has_no_checkbox_for_sesame(tmp_db, admin_client):
    """已兑付视图里芝麻那行不给勾：勾了也撤不动，不能让人以为撤掉了。"""
    c = admin_client
    today = db.today_local()
    sid, admin_id = _seed_advances([(None, 10.0)])
    with db.get_db() as conn:
        db.record_advance(
            conn, store_id=sid, user_id=admin_id, biz_date=today, phone="13900000009",
            sesame=9.58, source="sesame", ext_id="ses-9", paid=True,
        )
        conn.execute("UPDATE advance_posts SET paid=1 WHERE rebate=1000 AND source=''")
    page = c.get("/advance/pay?scope=today&paid=1").get_data(as_text=True)
    rows = re.findall(r"<tr>.*?</tr>", page, re.S)
    sesame_row = next(r for r in rows if "13900000009" in r)
    normal_row = next(r for r in rows if "13900000009" not in r and "10.00" in r)
    assert "js-pick" not in sesame_row
    assert "芝麻服务费导入即已兑" in sesame_row
    assert 'class="js-pick"' in normal_row
