"""芝麻服务费导入：解析、对店、去重、垫资联动、店员只读。"""

from io import BytesIO

import openpyxl
from openpyxl import Workbook

from app import db
from app.web import create_app


def _make_sesame_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "芝麻服务费明细1"
    ws.append(["芝麻服务费明细"])
    headers = [
        "流水号", "订单号", "省份", "城市", "地区", "商户号", "营业执照号",
        "营业执照名称", "门店编码", "门店名称", "门店类型", "统计月份",
        "订单金额", "服务费金额", "状态", "备注", "创建时间",
    ]
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sample_rows():
    return [
        [
            "1786774661473466851", "1786774661473466851", "江苏省", "示例市", "甲区",
            "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "示例公司甲",
            "JSCM_10000001", "示例甲店vivo专营店", "加盟店", "202608",
            1368.00, -9.58, "处理成功",
            "行业芝麻订单(1786774661473466851)服务费", "2026-08-15 14:19:38.0",
        ],
        [
            "1786527145403143969_R", "1786527145403143969", "江苏省", "示例市", "乙区",
            "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "示例公司甲",
            "JSCM_10000004", "示例戊店vivo专卖店", "加盟店", "202608",
            1368.00, 9.58, "处理成功",
            "行业芝麻订单(1786527145403143969)服务费退款", "2026-08-14 09:22:26.0",
        ],
        [
            "1785792754901613702", "1785792754901613702", "江苏省", "邻市", "丙区",
            "JSCM_20250116120104683937984", "91321202MA7M941H5H", "示例公司乙",
            "JSCM_10000003", "示例戊店带店vivo专卖店", "加盟店", "202608",
            792.00, -5.54, "处理成功",
            "行业芝麻订单(1785792754901613702)服务费", "2026-08-15 19:21:56.0",
        ],
        [
            "9999999999999999999", "9999999999999999999", "江苏省", "示例市", "丁区",
            "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "示例公司甲",
            "JSCM_99999999", "对不上的店", "加盟店", "202608",
            480.00, -3.36, "处理成功",
            "行业芝麻订单(9999999999999999999)服务费", "2026-08-10 10:00:00.0",
        ],
    ]


def test_parse_official_sesame_workbook():
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "芝麻服务费明细20260817082042.xlsx"
    if not path.is_file():
        return
    rows = db.parse_sesame_xlsx(path.read_bytes())
    assert len(rows) >= 40
    assert rows[0]["mobile_code"]
    assert isinstance(rows[0]["amount"], float)


def test_parse_sesame_xlsx(tmp_db):
    data = _make_sesame_xlsx(_sample_rows())
    rows = db.parse_sesame_xlsx(data)
    assert len(rows) == 4
    r0 = rows[0]
    assert r0["mobile_code"] == "10000001"
    assert r0["amount"] == 9.58  # 取反
    assert r0["refund"] is False
    assert r0["biz_date"] == "2026-08-15"
    r1 = rows[1]
    assert r1["amount"] == -9.58  # 退款取反为负
    assert r1["refund"] is True
    r2 = rows[2]
    assert r2["mobile_code"] == "10000003"  # 示例丁店


def test_classify_matches_jiangyan_and_xinghua(tmp_db):
    data = _make_sesame_xlsx(_sample_rows())
    rows = db.parse_sesame_xlsx(data)
    with db.get_db() as conn:
        stores = list(conn.execute("SELECT * FROM stores WHERE active=1"))
        groups = db.classify_sesame_rows(conn, rows, stores)
    codes = {r["mobile_code"] for r in groups["ready"]}
    assert "10000003" in codes  # 示例丁店
    unmatched_codes = {r["mobile_code"] for r in groups["unmatched"]}
    assert "99999999" in unmatched_codes


def test_import_creates_advance_rows_and_dedup(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    data = _make_sesame_xlsx(_sample_rows())
    # 第一次预览
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "sesame.xlsx")})
    # 预览存 session，需要先拿到 preview 页确认
    page = c.get("/advance/sesame").get_data(as_text=True)
    assert "可导入" in page
    assert "示例甲店" in page
    assert "9.58" in page
    assert "示例丁店" in page
    assert "对不上门店" in page
    # 确认导入
    confirm = c.post("/advance/sesame/confirm", follow_redirects=True).get_data(as_text=True)
    assert "已导入 3 笔" in confirm
    # 库里检查
    with db.get_db() as conn:
        rows = list(conn.execute(
            "SELECT sesame, source, ext_id, note, paid FROM advance_posts WHERE source='sesame' ORDER BY ext_id"
        ))
    assert len(rows) == 3
    assert all(int(r["paid"] or 0) == 1 for r in rows)
    amounts = sorted(float(r["sesame"]) / 100 for r in rows)
    assert amounts == [-9.58, 5.54, 9.58]
    # 再次预览：已导入的应跳过
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "sesame.xlsx")})
    page2 = c.get("/advance/sesame").get_data(as_text=True)
    assert "已导入跳过" in page2
    assert "可导入" not in page2 or "0" in page2


def test_imported_rows_locked_from_filler(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    data = _make_sesame_xlsx(_sample_rows()[:1])
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "s.xlsx")})
    c.post("/advance/sesame/confirm", follow_redirects=True)
    c.post("/logout")
    # 店员登录
    c.post("/login", data={"username": "alpha", "pin": "123456"})
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id, store_id FROM advance_posts WHERE source='sesame' LIMIT 1"
        ).fetchone()
    assert row is not None
    # 店员不能改
    edit = c.post(
        "/advance",
        data={
            "store_id": str(row["store_id"]),
            "advance_id": str(row["id"]),
            "biz_date": "2026-08-15",
            "phone": "13900000000",
            "rebate": "100",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "芝麻服务费是官方导入的，不能改" in edit
    # 店员不能删
    deleted = c.post(
        "/advance/delete",
        data={"store_id": str(row["store_id"]), "advance_id": str(row["id"])},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "店员不能删" in deleted
    # 管理员可以删
    c.post("/logout")
    c.post("/login", data={"username": "admin", "pin": "123456"})
    admin_del = c.post(
        "/advance/delete",
        data={"store_id": str(row["store_id"]), "advance_id": str(row["id"])},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已删除" in admin_del


def test_sesame_week_bulletin_from_imported_rows(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    data = _make_sesame_xlsx(_sample_rows())
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "s.xlsx")})
    c.post("/advance/sesame/confirm", follow_redirects=True)
    page = c.get("/advance/sesame/week?start=2026-08-10&end=2026-08-16").get_data(as_text=True)
    assert "芝麻周报" in page
    assert "【芝麻服务费】" in page
    assert "示例甲店" in page
    assert "9.58" in page
    city = c.get("/advance/sesame/week?start=2026-08-10&end=2026-08-16&city=示例市").get_data(as_text=True)
    assert "示例甲店" in city
    assert "示例戊店" not in city
    xlsx = c.get("/advance/sesame/week.xlsx?start=2026-08-10&end=2026-08-16")
    assert xlsx.status_code == 200
    book = openpyxl.load_workbook(BytesIO(xlsx.get_data()))
    assert book.active["A1"].value == "门店"
    c.post("/logout")
    c.post("/login", data={"username": "alpha", "pin": "123456"})
    blocked = c.get("/advance/sesame/week", follow_redirects=True)
    assert "需要管理员或只读权限" in blocked.get_data(as_text=True)


def test_sesame_shows_in_advance_totals(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    data = _make_sesame_xlsx(_sample_rows()[:1])
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "s.xlsx")})
    c.post("/advance/sesame/confirm", follow_redirects=True)
    page = c.get("/advance").get_data(as_text=True)
    assert "芝麻服务费" in page
    # 示例甲店 9.58
    with db.get_db() as conn:
        totals = db.advance_month_totals(
            conn,
            [conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]],
            db.today_local(),
        )
    assert abs(totals[list(totals)[0]]["sesame"] - 9.58) < 0.01


def test_sesame_export_has_column(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    data = _make_sesame_xlsx(_sample_rows()[:1])
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "s.xlsx")})
    c.post("/advance/sesame/confirm", follow_redirects=True)
    resp = c.get(f"/advance.xlsx?month={db.today_local().strftime('%Y-%m')}")
    wb = openpyxl.load_workbook(BytesIO(resp.get_data()))
    summary = wb["汇总表"]
    headers = [summary.cell(2, col).value for col in range(1, 7)]
    assert "芝麻服务费" in headers
    # 门店明细表也有
    store_sheet = wb["示例甲店"]
    store_headers = [store_sheet.cell(2, col).value for col in range(1, 11)]
    assert "芝麻服务费" in store_headers


def test_import_more_than_200_rows_imports_all(tmp_db):
    """回归：预览只展示前 200 笔，但确认时必须导入全部，不能静默丢后段。"""
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    rows = []
    for i in range(230):
        rows.append(
            [
                f"TESTEXT{i:08d}", f"TESTORD{i:08d}", "江苏省", "示例市", "甲区",
                "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "示例公司甲",
                "JSCM_10000001", "示例甲店vivo专营店", "加盟店", "202608",
                1000.00, -5.00, "处理成功",
                f"行业芝麻订单(TESTEXT{i:08d})服务费", "2026-08-15 14:19:38.0",
            ]
        )
    # 预览：ready_count 显示全量 230，预览表只展示 200
    resp = c.post(
        "/advance/sesame/preview",
        data={"sesame_file": (BytesIO(_make_sesame_xlsx(rows)), "s.xlsx")},
        follow_redirects=True,
    )
    page = resp.get_data(as_text=True)
    assert "可导入 <strong>230</strong>" in page
    assert "仅展示前 200 笔" in page
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM advance_posts WHERE source='sesame'").fetchone()["COUNT(*)"] == 0
    # 确认：全部 230 笔都要进库
    c.post("/advance/sesame/confirm", follow_redirects=True)
    with db.get_db() as conn:
        imported = conn.execute(
            "SELECT COUNT(*), SUM(sesame) FROM advance_posts WHERE source='sesame'"
        ).fetchone()
    assert imported["COUNT(*)"] == 230
    assert imported["SUM(sesame)"] == 230 * 500  # 服务费 -5.00 元→入账 +5.00，存 +500 分/笔


def test_all_store_advance_page_shows_all_rows(tmp_db):
    """回归：垫资全店页不能因 200 条上限截断列表/未兑计数。"""
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "123456"})
    rows = []
    for i in range(230):
        rows.append(
            [
                f"ALLXX{i:06d}", f"ALLORD{i:06d}", "江苏省", "示例市", "甲区",
                "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "示例公司甲",
                "JSCM_10000001", "示例甲店vivo专营店", "加盟店", "202608",
                1000.00, -5.00, "处理成功",
                f"行业芝麻订单(ALLXX{i:06d})服务费", "2026-08-15 14:19:38.0",
            ]
        )
    c.post(
        "/advance/sesame/preview",
        data={"sesame_file": (BytesIO(_make_sesame_xlsx(rows)), "s.xlsx")},
    )
    c.post("/advance/sesame/confirm", follow_redirects=True)
    # 全店视图：应显示全部 230 笔，而不是被截断到 200
    page = c.get("/advance?store_id=all").get_data(as_text=True)
    assert "230 笔" in page  # 列表计数用全量
    # 每行备注（芝麻服务费 + 订单号）渲染约 2 处；230 行远超市旧 200 条上限，
    # 故多过 200*2=400 即证明未被截断。全部导入日志确认 230 笔都进库。
    assert page.count("ALLORD") > 400
