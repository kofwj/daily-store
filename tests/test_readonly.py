"""只读角色（店长/区域经理）：可看报表/通报/垫资，不能填报/成交/垫资/兑付/看板/考核。

- 店长：scope 空，绑 user_stores，只看自己门店
- 区域经理：scope=area_manager 姓名，看同区域经理所有门店
"""
from app import db


def _mk_readonly(client, *, username, scope="", store_code=""):
    with db.get_db() as conn:
        store_ids = []
        if store_code:
            row = conn.execute("SELECT id FROM stores WHERE code=?", (store_code,)).fetchone()
            store_ids = [row["id"]] if row else []
        db.create_user(
            conn,
            username=username,
            display_name=username,
            pin="123456",
            role="readonly",
            store_ids=store_ids,
            scope=scope,
        )
    client.post("/login", data={"username": username, "pin": "123456"}, follow_redirects=True)


def test_store_manager_sees_only_own_store(client):
    # 店长绑 haimen-jinhua（配移动编码，进通报）
    _mk_readonly(client, username="yuanyj_dz", store_code="haimen-jinhua")
    report = client.get("/report").get_data(as_text=True)
    assert "海门金花" in report
    bulletin = client.get("/bulletin").get_data(as_text=True)
    assert "海门金花" in bulletin
    # 另一家店（地区经理下同级但不绑）不该出现
    assert "启东人民" not in bulletin
    # 垫资页可看
    adv = client.get("/advance").get_data(as_text=True)
    assert "本月记录" in adv


def test_area_manager_sees_all_region_stores(client):
    # 区域经理 黄雍青：南通北片（海门/启东/通州），这些店都配了移动编码、进通报
    _mk_readonly(client, username="huangyq", scope="黄雍青")
    bulletin = client.get("/bulletin", follow_redirects=True).get_data(as_text=True)
    for name in ("海门金花", "人民中路", "金沙专卖店", "通州区万达"):
        assert name in bulletin
    # 鞠一凡辖区（海安/如皋）不该出现
    assert "海安人民" not in bulletin
    # 报表页（含未配编码店）也可切店
    report = client.get("/report").get_data(as_text=True)
    assert "海门金花" in report
    assert "启东人民" in report


def test_readonly_cannot_write(client):
    _mk_readonly(client, username="rdo", store_code="haimen-jinhua")
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]
    today = db.today_local().isoformat()
    # 不能填日报
    resp = client.post(
        "/today",
        data={"store_id": str(sid), "date": today, "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "只读账号不能填日报" in resp
    with db.get_db() as conn:
        assert db.get_report(conn, sid, db.today_local()) is None
    # 不能填成交
    resp = client.post(
        "/deal",
        data={"store_id": str(sid), "model": "X200", "closed": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "只读账号不能填成交播报" in resp
    # 不能填垫资
    resp = client.post(
        "/advance",
        data={"store_id": str(sid), "biz_date": today, "rebate": "100", "phone": "13900001111"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "只读账号不能填垫资" in resp
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM advance_posts WHERE store_id=?", (sid,)).fetchone()["n"]
        assert n == 0
    # 垫资页隐藏填报表单
    adv = client.get("/advance").get_data(as_text=True)
    assert "记一笔垫资" not in adv


def test_readonly_cannot_admin(client):
    _mk_readonly(client, username="rd_admin_block", store_code="haimen-jinhua")
    for path, flag in (
        ("/board", "多店看板"),
        ("/incentive", "月度考核"),
        ("/edits", "修改审计"),
        ("/advance/pay", "垫资兑付"),
    ):
        resp = client.get(path, follow_redirects=True)
        assert "需要管理员权限" in resp.get_data(as_text=True), path
    # 设置里没有非账号 tab
    settings = client.get("/settings").get_data(as_text=True)
    assert "门店档案" not in settings
    assert "保存口令" in settings


def test_readonly_login_lands_on_report(client):
    _mk_readonly(client, username="rd_home", store_code="haimen-jinhua")
    client.get("/logout")
    landed = client.post(
        "/login", data={"username": "rd_home", "pin": "123456"}, follow_redirects=True
    )
    assert landed.request.path == "/report"
    home = client.get("/", follow_redirects=True)
    assert home.request.path == "/report"


def test_admin_still_sees_all(client):
    _mk_readonly(client, username="rd_admin", store_code="haimen-jinhua")
    client.get("/logout")
    client.post("/login", data={"username": "admin", "pin": "1234"})
    bulletin = client.get("/bulletin").get_data(as_text=True)
    assert "海门金花" in bulletin
