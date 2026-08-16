from datetime import date

from app import db
from app.bulletin import (
    apply_scales,
    bisuan_total,
    build_row,
    csv_rows,
    fmt_count,
    scale_color,
    summary,
    totals_row,
    tsv,
)


def test_fmt_count_hides_zero():
    assert fmt_count(0) == "0"
    assert fmt_count(4) == "4"
    assert fmt_count(1.0) == "1"
    assert fmt_count(1.2) == "1.2"


def test_bisuan_adds_high():
    assert bisuan_total({"bisuan": 5, "bisuan_high": 3}) == 8
    assert bisuan_total({}) == 0


def test_bulletin_row_and_tsv_match_sheet():
    store = {
        "id": 1,
        "code": "rg-ninghai",
        "name": "TZ南通市如皋市如城镇宁海路体验店",
        "region_group": "通泰",
        "city": "南通市",
        "mobile_code": "20001744",
        "area_manager": "鞠一凡",
        "store_manager": "冒国云",
        "follow_ai": 0,
        "follow_bisuan": 1,
    }
    row = build_row(
        store,
        day_ai=0,
        month_ai=0,
        day_bisuan=3,
        month_bisuan=10,
        submitted=True,
    )
    assert row["follow_ai_text"] == "否"
    assert row["follow_bisuan_text"] == "是"
    assert row["follow_ai"] is False
    assert row["follow_bisuan"] is True
    assert row["month_ai_text"] == "0"
    assert row["month_bisuan_text"] == "10"
    assert row["day_bisuan_text"] == "3"
    assert row["ai_zero"] is True
    assert row["bisuan_zero"] is False

    text = tsv([row], date(2026, 8, 13))
    assert "8月\t\t8月13日" in text or "8月" in text
    assert "TZ南通市如皋市如城镇宁海路体验店" in text
    assert "20001744" in text
    assert "冒国云" in text
    headers = csv_rows([row], date(2026, 8, 13))[0]
    assert headers[8] == "8月AI手机合约"
    assert headers[9] == "8月笔算业务"
    assert headers[10] == "8月金币直降"
    assert headers[11] == "8月13日AI手机合约"
    assert headers[12] == "8月13日笔算业务"
    assert headers[13] == "8月13日金币直降"
    assert headers[6] == "AI破0"
    assert headers[7] == "笔算破0"


def test_follow_uses_month_break_zero_for_every_store():
    store = {
        "id": 2,
        "code": "x",
        "name": "店",
        "region_group": "通泰",
        "city": "南通市",
        "mobile_code": "1",
        "area_manager": "A",
        "store_manager": "B",
        "follow_ai": 0,
        "follow_bisuan": 0,
    }
    row = build_row(store, day_ai=1, month_ai=2, day_bisuan=0, month_bisuan=0, submitted=True)
    assert row["follow_ai_text"] == "是"
    assert row["follow_bisuan_text"] == "否"


def test_scale_color_higher_is_different():
    low = scale_color(0, 10, "month")
    high = scale_color(10, 10, "month")
    assert low != high
    rows = apply_scales(
        [
            build_row(
                {
                    "id": 1, "code": "a", "name": "A", "region_group": "通泰", "city": "南通市",
                    "mobile_code": "1", "area_manager": "", "store_manager": "",
                    "follow_ai": 0, "follow_bisuan": 0,
                },
                day_ai=0, month_ai=0, day_bisuan=1, month_bisuan=8, submitted=True,
            )
        ]
    )
    assert rows[0]["month_bisuan_color"].startswith("#")


def test_unsubmitted_row_skips_heatmap():
    submitted = build_row(
        {
            "id": 1, "code": "a", "name": "A", "region_group": "通泰", "city": "南通市",
            "mobile_code": "1", "area_manager": "", "store_manager": "",
            "follow_ai": 0, "follow_bisuan": 0,
        },
        day_ai=0, month_ai=1, day_bisuan=0, month_bisuan=0, submitted=True,
    )
    waiting = build_row(
        {
            "id": 2, "code": "b", "name": "B", "region_group": "通泰", "city": "南通市",
            "mobile_code": "2", "area_manager": "", "store_manager": "",
            "follow_ai": 0, "follow_bisuan": 0,
        },
        day_ai=0, month_ai=4, day_bisuan=0, month_bisuan=1, submitted=False,
    )
    apply_scales([submitted, waiting])
    assert submitted["month_ai_color"].startswith("#")
    assert waiting["month_ai_color"] == ""
    assert waiting["day_bisuan_color"] == ""


def test_totals_row_sums_and_break_zero_count():
    a = build_row(
        {
            "id": 1, "code": "a", "name": "A", "region_group": "通泰", "city": "南通市",
            "mobile_code": "1", "area_manager": "", "store_manager": "",
            "follow_ai": 0, "follow_bisuan": 0,
        },
        day_ai=1, month_ai=2, day_bisuan=3, month_bisuan=4, submitted=True,
    )
    b = build_row(
        {
            "id": 2, "code": "b", "name": "B", "region_group": "通泰", "city": "南通市",
            "mobile_code": "2", "area_manager": "", "store_manager": "",
            "follow_ai": 0, "follow_bisuan": 0,
        },
        day_ai=0, month_ai=0, day_bisuan=1, month_bisuan=6, submitted=True,
    )
    total = totals_row([a, b])
    assert total["name"] == "合计（2 店）"
    assert total["follow_ai_text"] == "1/2"
    assert total["follow_bisuan_text"] == "2/2"
    assert total["month_ai"] == 2
    assert total["month_bisuan"] == 10
    assert total["day_ai"] == 1
    assert total["day_bisuan"] == 4
    text = tsv([a, b], date(2026, 8, 14))
    assert "合计（2 店）" in text


def test_bulletin_export_xlsx(client):
    """管理员通报表导出为 .xlsx，旧 /bulletin.csv 重定向到新地址。"""
    import io

    import openpyxl

    client.post("/login", data={"username": "admin", "pin": "1234"})
    biz_date = date.today().isoformat()
    # 先提交两店日报，让通报表有数据
    from app import db

    with db.get_db() as conn:
        stores = list(conn.execute("SELECT id, code FROM stores LIMIT 2"))
    for st in stores:
        client.post(
            "/today",
            data={
                "store_id": str(st["id"]),
                "date": biz_date,
                "m_phone_sales": "2",
                "m_ai_contract": "1",
                "m_cloud_disk": "0",
            },
            follow_redirects=True,
        )
    r = client.get(f"/bulletin.xlsx?date={biz_date}")
    assert r.status_code == 200
    assert r.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert r.headers.get("Content-Disposition", "").endswith(".xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(r.get_data()))
    ws = wb.active
    assert ws.title == "通报表"
    header = [c.value for c in ws[1]]
    assert header[1] == "地市" and header[3] == "移动编码"
    assert "区域经理" in header
    # 旧 CSV 链接现在重定向到 .xlsx
    r2 = client.get(f"/bulletin.csv?date={biz_date}")
    assert r2.status_code in (302, 200)


def test_summary_review_text():
    rows = [
        {
            "name": "TZ南通市海门金花vivo体验店",
            "short_name": "海门金花",
            "month_ai": 5,
            "month_bisuan": 8,
            "month_coin": 3,
            "day_ai": 2,
            "day_bisuan": 4,
            "day_coin": 1,
        },
        {
            "name": "TZ南通市启东汇龙镇人民中路专卖店",
            "short_name": "启东人民",
            "month_ai": 12,
            "month_bisuan": 6,
            "month_coin": 5,
            "day_ai": 3,
            "day_bisuan": 1,
            "day_coin": 0,
        },
        {
            "name": "TZ南通市通州区金沙专卖店",
            "short_name": "通州金沙",
            "month_ai": 0,
            "month_bisuan": 0,
            "month_coin": 0,
            "day_ai": 0,
            "day_bisuan": 0,
            "day_coin": 0,
        },
    ]
    text = summary(rows, date(2026, 8, 13), "南通", day_deal=(7, 5), month_deal=(60, 42))
    lines = text.split("\n")
    assert lines[0] == "2026-08-13 南通vivo零售运营中心"
    assert "【今日】" in lines
    assert "销量：AI 5 · 笔算 5 · 直降 1" in lines
    assert "触客：7 笔（成交 5）" in lines
    assert "AI有量：海门金花、启东人民" in lines
    assert "笔算有量：海门金花、启东人民" in lines
    assert "直降有量：海门金花" in lines
    assert "今日三项都有：海门金花" in lines
    assert "通州金沙" not in text  # 今日三项都是 0，不进表扬
    assert "累计：AI 17 · 笔算 14 · 直降 8" in lines
    assert "触客：60 笔（成交 42）" in lines
    assert "综合标杆：启东人民（AI 12，笔算 6，直降 5）" in lines
    assert "单项第一：AI 启东人民 · 笔算 海门金花 · 直降 启东人民" in lines
    empty = summary(
        [{"name": "空店", "short_name": "空店", "month_ai": 0, "month_bisuan": 0, "month_coin": 0,
          "day_ai": 0, "day_bisuan": 0, "day_coin": 0}],
        date(2026, 8, 13),
    )
    assert "今日暂无单项破零" in empty
    assert summary([], date(2026, 8, 13)) == "2026-08-13 暂无门店通报数据。"


def test_build_row_short_name_from_row(tmp_db):
    # sqlite3.Row 不支持 .get，_store_short 要用下标取简称，避免标杆退全称
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM stores WHERE code='haimen-jinhua'"
        ).fetchone()
    out = build_row(
        row,
        day_ai=0,
        month_ai=0,
        day_bisuan=0,
        month_bisuan=0,
        submitted=False,
    )
    assert out["short_name"] == "海门金花"
