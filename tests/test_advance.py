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
        assert int(row["broadband"]) == 20000
        assert int(row["rebate"]) == 10000


def test_admin_can_save_without_phone_and_fills_settlement(tmp_db):
    from app.web import create_app

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "1234"})
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
    client.get("/logout")
    client.post("/login", data={"username": "admin", "pin": "1234"})
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
    client.get("/logout")
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
    client.get("/logout")
    client.post("/login", data={"username": "beta", "pin": "123456"})
    client.post(
        "/advance",
        data={"store_id": str(sid_b), "biz_date": today, "phone": "13900009222", "rebate": "20"},
    )
    client.get("/logout")
    client.post("/login", data={"username": "admin", "pin": "1234"})
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
    client.post("/login", data={"username": "admin", "pin": "1234"})
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
    assert "13900003333" in page
    assert "改" in page and "删" in page
    client.get("/logout")
    client.post("/login", data={"username": "admin", "pin": "1234"})
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
