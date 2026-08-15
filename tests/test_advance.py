from io import BytesIO

import openpyxl

from app import db
from app.stores_seed import STORES


def test_catalog_includes_advance_workbook_stores(tmp_db):
    names = {item["name"] for item in STORES}
    assert "TZ南通启东吾悦vivo体验店" in names
    assert "TZ南通市崇川区银河大厦专卖店" in names
    assert "TZ南通如皋万达vivo体验店" in names
    assert "TZ南通如皋吾悦vivo体验店" in names
    assert "TZ南通海安喜润城vivo体验店" in names
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM stores WHERE code='qidong-wuyue'").fetchone()
        assert row is not None
        assert row["area_manager"] == "黄雍青"


def test_filler_must_provide_phone(client):
    client.post("/login", data={"username": "jinhua", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
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
    client.post("/login", data={"username": "jinhua", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
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
    assert "13900001111" in saved
    assert "未兑" in saved
    with db.get_db() as conn:
        aid = conn.execute("SELECT id FROM advance_posts WHERE store_id=?", (sid,)).fetchone()["id"]
    client.get("/logout")
    client.post("/login", data={"username": "admin", "pin": "1234"})
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
        assert float(row["broadband"]) == 200
        assert float(row["rebate"]) == 100


def test_admin_can_save_without_phone_and_fills_settlement(tmp_db):
    from app.web import create_app

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "1234"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
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
        if row[1] == "TZ南通市海门金花vivo体验店":
            assert row[7] == 39.99
            found = True
            break
    assert found
    export = c.get(f"/advance.xlsx?month={today.strftime('%Y-%m')}")
    assert export.status_code == 200
    book = openpyxl.load_workbook(BytesIO(export.get_data()))
    assert "汇总表" in book.sheetnames
    assert "海门金花" in book.sheetnames
    assert book["海门金花"]["G3"].value == "芝麻服务费"


def test_filler_can_save_negative_amount(client):
    client.post("/login", data={"username": "jinhua", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
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
        assert float(row["broadband"]) == -100


def test_filler_cannot_open_pay_page(client):
    client.post("/login", data={"username": "jinhua", "pin": "123456"})
    r = client.get("/advance/pay", follow_redirects=True)
    assert "需要管理员权限" in r.get_data(as_text=True)


def test_store_sees_month_list_and_admin_sees_today_inbox(client):
    client.post("/login", data={"username": "jinhua", "pin": "123456"})
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
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
    assert "13900003333" in page
    assert "改" in page and "删" in page
    client.get("/logout")
    client.post("/login", data={"username": "admin", "pin": "1234"})
    inbox = client.get("/advance/pay?scope=today&paid=0").get_data(as_text=True)
    assert "今天待兑" in inbox
    assert "海门金花" in inbox
    assert "1笔" in inbox
