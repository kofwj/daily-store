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
            "1786774661473466851", "1786774661473466851", "江苏省", "南通市", "海门市",
            "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "南通财顺电子有限公司",
            "JSCM_20284034", "海门金花vivo指定专营店", "加盟店", "202608",
            1368.00, -9.58, "处理成功",
            "行业芝麻订单(1786774661473466851)服务费", "2026-08-15 14:19:38.0",
        ],
        [
            "1786527145403143969_R", "1786527145403143969", "江苏省", "南通市", "港闸区",
            "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "南通财顺电子有限公司",
            "JSCM_20122957", "南通北城万达vivo专卖店", "加盟店", "202608",
            1368.00, 9.58, "处理成功",
            "行业芝麻订单(1786527145403143969)服务费退款", "2026-08-14 09:22:26.0",
        ],
        [
            "1785792754901613702", "1785792754901613702", "江苏省", "泰州市", "姜堰区",
            "JSCM_20250116120104683937984", "91321202MA7M941H5H", "泰州市财汇电子有限公司",
            "JSCM_21114643", "姜堰金鑫花园带店加盟店vivo专卖店", "加盟店", "202608",
            792.00, -5.54, "处理成功",
            "行业芝麻订单(1785792754901613702)服务费", "2026-08-15 19:21:56.0",
        ],
        [
            "9999999999999999999", "9999999999999999999", "江苏省", "南通市", "海安县",
            "JSCM_20250116110103117937984", "91320602MA7E6JC70A", "南通财顺电子有限公司",
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
    assert rows[0]["mobile_code"] == "20389153"
    assert rows[0]["amount"] == 9.58


def test_parse_sesame_xlsx(tmp_db):
    data = _make_sesame_xlsx(_sample_rows())
    rows = db.parse_sesame_xlsx(data)
    assert len(rows) == 4
    r0 = rows[0]
    assert r0["mobile_code"] == "20284034"
    assert r0["amount"] == 9.58  # 取反
    assert r0["refund"] is False
    assert r0["biz_date"] == "2026-08-15"
    r1 = rows[1]
    assert r1["amount"] == -9.58  # 退款取反为负
    assert r1["refund"] is True
    r2 = rows[2]
    assert r2["mobile_code"] == "21114643"  # 姜堰罗塘


def test_classify_matches_jiangyan_and_xinghua(tmp_db):
    data = _make_sesame_xlsx(_sample_rows())
    rows = db.parse_sesame_xlsx(data)
    with db.get_db() as conn:
        stores = list(conn.execute("SELECT * FROM stores WHERE active=1"))
        groups = db.classify_sesame_rows(conn, rows, stores)
    codes = {r["mobile_code"] for r in groups["ready"]}
    assert "21114643" in codes  # 姜堰罗塘
    unmatched_codes = {r["mobile_code"] for r in groups["unmatched"]}
    assert "99999999" in unmatched_codes


def test_import_creates_advance_rows_and_dedup(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "1234"})
    data = _make_sesame_xlsx(_sample_rows())
    # 第一次预览
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "sesame.xlsx")})
    # 预览存 session，需要先拿到 preview 页确认
    page = c.get("/advance/sesame").get_data(as_text=True)
    assert "可导入" in page
    assert "海门金花" in page
    assert "9.58" in page
    assert "姜堰罗塘" in page
    assert "对不上门店" in page
    # 确认导入
    confirm = c.post("/advance/sesame/confirm", follow_redirects=True).get_data(as_text=True)
    assert "已导入 3 笔" in confirm
    # 库里检查
    with db.get_db() as conn:
        rows = list(conn.execute(
            "SELECT sesame, source, ext_id, note FROM advance_posts WHERE source='sesame' ORDER BY ext_id"
        ))
    assert len(rows) == 3
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
    c.post("/login", data={"username": "admin", "pin": "1234"})
    data = _make_sesame_xlsx(_sample_rows()[:1])
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "s.xlsx")})
    c.post("/advance/sesame/confirm", follow_redirects=True)
    c.get("/logout")
    # 店员登录
    c.post("/login", data={"username": "jinhua", "pin": "123456"})
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
    c.get("/logout")
    c.post("/login", data={"username": "admin", "pin": "1234"})
    admin_del = c.post(
        "/advance/delete",
        data={"store_id": str(row["store_id"]), "advance_id": str(row["id"])},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已删除" in admin_del


def test_sesame_shows_in_advance_totals(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "1234"})
    data = _make_sesame_xlsx(_sample_rows()[:1])
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "s.xlsx")})
    c.post("/advance/sesame/confirm", follow_redirects=True)
    page = c.get("/advance").get_data(as_text=True)
    assert "芝麻服务费" in page
    # 海门金花 9.58
    with db.get_db() as conn:
        totals = db.advance_month_totals(
            conn,
            [conn.execute("SELECT id FROM stores WHERE code='haimen-jinhua'").fetchone()["id"]],
            db.today_local(),
        )
    assert abs(totals[list(totals)[0]]["sesame"] - 9.58) < 0.01


def test_sesame_export_has_column(tmp_db):
    app = create_app(testing=True)
    c = app.test_client()
    c.post("/login", data={"username": "admin", "pin": "1234"})
    data = _make_sesame_xlsx(_sample_rows()[:1])
    c.post("/advance/sesame/preview", data={"sesame_file": (BytesIO(data), "s.xlsx")})
    c.post("/advance/sesame/confirm", follow_redirects=True)
    resp = c.get(f"/advance.xlsx?month={db.today_local().strftime('%Y-%m')}")
    wb = openpyxl.load_workbook(BytesIO(resp.get_data()))
    summary = wb["汇总表"]
    headers = [summary.cell(2, col).value for col in range(1, 7)]
    assert "芝麻服务费" in headers
    # 门店明细表也有
    store_sheet = wb["海门金花"]
    store_headers = [store_sheet.cell(2, col).value for col in range(1, 11)]
    assert "芝麻服务费" in store_headers
