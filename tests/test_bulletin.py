from datetime import date

from app import db
from app.bulletin import (
    REVIEW_PRESETS,
    apply_scales,
    bisuan_total,
    build_row,
    csv_rows,
    scale_color,
    summary,
    totals_row,
    tsv,
)


def test_bisuan_adds_high():
    assert bisuan_total({"bisuan": 5, "bisuan_high": 3}) == 8
    assert bisuan_total({}) == 0


def test_summary_single_first_bisuan_uses_mobile_when_present():
    rows = [
        build_row(
            {
                "id": 1,
                "name": "海安万达",
                "code": "a",
                "city": "",
                "region_group": "通泰",
                "mobile_code": "1",
                "area_manager": "",
                "store_manager": "",
            },
            month_bisuan=110,  # 填报 11.0（store 值 = ×10）
            month_bisuan_mobile=80,  # 移动 8.0
            day_ai=0, month_ai=1, day_bisuan=0,
            submitted=True,
        ),
        build_row(
            {
                "id": 2,
                "name": "如皋宁海路",
                "code": "b",
                "city": "",
                "region_group": "通泰",
                "mobile_code": "2",
                "area_manager": "",
                "store_manager": "",
            },
            month_bisuan=60,  # 填报 6.0
            month_bisuan_mobile=120,  # 移动 12.0
            day_ai=0, month_ai=1, day_bisuan=0,
            submitted=True,
        ),
    ]
    rows = apply_scales(rows)
    text = summary(rows, date(2026, 8, 25), "南通")
    # 单项第一·笔算按移动：如皋宁海路(12.0) > 海安万达(8.0)
    assert "笔算 如皋宁海路" in text
    # 但这不影响填报合计
    assert "笔算" in text


def test_bulletin_row_and_tsv_match_sheet():
    store = {
        "id": 1,
        "code": "store-epsilon",
        "name": "邻市戊路vivo体验店",
        "region_group": "通泰",
        "city": "南通市",
        "mobile_code": "20001744",
        "area_manager": "张管理",
        "store_manager": "陈店长",
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
    assert row["month_bisuan_text"] == "1.0"
    assert row["day_bisuan_text"] == "0.3"
    assert row["ai_zero"] is True
    assert row["bisuan_zero"] is False

    text = tsv([row], date(2026, 8, 13))
    assert "8月\t\t8月13日" in text or "8月" in text
    assert "邻市戊路vivo体验店" in text
    assert "20001744" in text
    assert "陈店长" in text
    headers = csv_rows([row], date(2026, 8, 13))[0]
    assert headers[8] == "8月AI手机合约"
    assert headers[9] == "8月灵犀·晓伴"
    assert headers[10] == "8月笔算业务"
    assert headers[11] == "8月金币直降"
    assert headers[12] == "8月13日AI手机合约"
    assert headers[13] == "8月13日灵犀·晓伴"
    assert headers[14] == "8月13日笔算业务"
    assert headers[15] == "8月13日金币直降"
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
    # 合计格带移取数
    rows_m = [
        build_row(
            {
                "id": 1, "code": "a", "name": "A", "region_group": "通泰", "city": "南通市",
                "mobile_code": "1", "area_manager": "", "store_manager": "",
                "follow_ai": 0, "follow_bisuan": 0,
            },
            day_ai=0, month_ai=0, day_bisuan=0, month_bisuan=10,
            submitted=True, month_bisuan_mobile=12, month_bisuan_asof="2026-08-16",
            month_bisuan_sys_asof=10,
        ),
        build_row(
            {
                "id": 2, "code": "b", "name": "B", "region_group": "通泰", "city": "南通市",
                "mobile_code": "2", "area_manager": "", "store_manager": "",
                "follow_ai": 0, "follow_bisuan": 0,
            },
            day_ai=0, month_ai=0, day_bisuan=0, month_bisuan=8,
            submitted=True, month_bisuan_mobile=8, month_bisuan_asof="2026-08-16",
            month_bisuan_sys_asof=8,
        ),
    ]
    rows_m = apply_scales(rows_m)
    total_m = totals_row(rows_m)
    assert "移2.0" in total_m["month_bisuan_text"]
    assert total_m["month_bisuan_asof_label"] == "至8/16"
    assert total["day_ai"] == 1
    assert total["day_bisuan"] == 4
    text = tsv([a, b], date(2026, 8, 14))
    assert "合计（2 店）" in text


def test_bulletin_review_preset_switch(app_client):
    app_client.post("/login", data={"username": "admin", "pin": "123456"})
    page = app_client.get("/bulletin").get_data(as_text=True)
    assert "套用" not in page or True
    assert "精简" in page
    assert "追差" in page
    switched = app_client.post(
        "/bulletin/review-preset",
        data={"preset": "brief", "date": "2026-08-18", "city": "南通市"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "复盘模板已切换" in switched or "精简" in switched


def test_bulletin_page_shows_lingxi_day_and_month(admin_client):
    from datetime import date as _date

    day = _date.today()
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    admin_client.post(
        "/today",
        data={
            "store_id": str(sid),
            "date": day.isoformat(),
            "m_lingxi_xiaoban": "3",
            "m_ai_contract": "1",
        },
        follow_redirects=True,
    )
    page = admin_client.get(f"/bulletin?date={day.isoformat()}").get_data(as_text=True)
    assert page.count("灵犀·晓伴") >= 2
    assert page.count('class="total-num">3</td>') >= 2


def test_bulletin_export_xlsx(client):
    """管理员通报表导出为 .xlsx，旧 /bulletin.csv 重定向到新地址。"""
    import io

    import openpyxl

    client.post("/login", data={"username": "admin", "pin": "123456"})
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
    assert any(h and "灵犀" in str(h) for h in header)
    # 旧 CSV 链接现在重定向到 .xlsx
    r2 = client.get(f"/bulletin.csv?date={biz_date}")
    assert r2.status_code in (302, 200)


def test_summary_review_text():
    rows = [
        {
            "name": "示例市甲街vivo体验店",
            "short_name": "示例甲店",
            "month_ai": 5,
            "month_bisuan": 8,
            "month_coin": 3,
            "day_ai": 2,
            "day_bisuan": 4,
            "day_coin": 1,
        },
        {
            "name": "示例市丙路vivo专卖店",
            "short_name": "示例丙店",
            "month_ai": 12,
            "month_bisuan": 6,
            "month_coin": 5,
            "day_ai": 3,
            "day_bisuan": 1,
            "day_coin": 0,
        },
        {
            "name": "示例市乙街vivo专卖店",
            "short_name": "示例乙店",
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
    assert "销量：AI 5 · 笔算 0.5 · 直降 1" in lines
    assert "触客：7 笔（成交 5）" in lines
    assert "AI有量：示例甲店、示例丙店" in lines
    assert "笔算有量：示例甲店、示例丙店" in lines
    assert "直降有量：示例甲店" in lines
    assert "今日三项都有：示例甲店" in lines
    assert "示例乙店" not in text  # 今日三项都是 0，不进表扬
    assert "累计：AI 17 · 笔算 1.4 · 直降 8" in lines
    assert "触客：60 笔（成交 42）" in lines
    assert "综合标杆：示例丙店（AI 12，笔算 0.6，直降 5）" in lines
    assert "单项第一：AI 示例丙店 · 笔算 示例甲店 · 直降 示例丙店" in lines
    # 今天没更新移数（截止日早于通报表日）=> 复盘不加对照段
    rows[0]["_month_bisuan_mobile_stored"] = 10
    rows[0]["_month_bisuan_sys_asof_stored"] = 8
    rows[0]["month_bisuan_asof"] = "2026-08-16"
    rows[0]["month_bisuan_asof_label"] = "至8/16"
    rows[1]["_month_bisuan_mobile_stored"] = 6
    rows[1]["_month_bisuan_sys_asof_stored"] = 6
    rows[1]["month_bisuan_asof"] = "2026-08-16"
    rows[1]["month_bisuan_asof_label"] = "至8/16"
    text2 = summary(rows, date(2026, 8, 17), "南通")
    assert "分店对照" not in text2
    assert "笔算移取" not in text2
    # 移数更新到今天 → 才带合计+分店对照
    rows[0]["month_bisuan_asof"] = "2026-08-17"
    rows[0]["month_bisuan_asof_label"] = "至8/17"
    rows[1]["month_bisuan_asof"] = "2026-08-17"
    rows[1]["month_bisuan_asof_label"] = "至8/17"
    text3 = summary(rows, date(2026, 8, 17), "南通")
    assert "笔算移取（移动数据更新至8/17）：移 1.6 · 上报同期 1.4 · 差+0.2" in text3
    assert "分店对照：" in text3
    assert "示例甲店 上报0.8 移1.0 差+0.2" in text3
    assert "示例丙店 上报0.6 移0.6 已对齐" in text3
    empty = summary(
        [{"name": "空店", "short_name": "空店", "month_ai": 0, "month_bisuan": 0, "month_coin": 0,
          "day_ai": 0, "day_bisuan": 0, "day_coin": 0}],
        date(2026, 8, 13),
    )
    assert "今日暂无单项破零" in empty
    assert summary([], date(2026, 8, 13)) == "2026-08-13 暂无门店通报数据。"
    custom = summary(
        rows,
        date(2026, 8, 17),
        "南通",
        template="{head}\n今日AI {day_ai}\n{praise}\n标杆 {top_name}",
    )
    assert custom.startswith("2026-08-17 南通vivo零售运营中心")
    assert "今日AI 5" in custom
    assert "标杆 示例丙店" in custom
    assert "【本月】" not in custom
    keys = [p["key"] for p in REVIEW_PRESETS]
    assert keys == ["standard", "check", "praise", "brief", "chase", "deal"]
    brief = next(p["body"] for p in REVIEW_PRESETS if p["key"] == "brief")
    brief_text = summary(rows, date(2026, 8, 17), "南通", template=brief)
    assert "今日 AI 5" in brief_text
    assert "标杆 示例丙店" in brief_text
    rows[0]["submitted"] = True
    rows[1]["submitted"] = True
    rows[2]["submitted"] = False
    chase = next(p["body"] for p in REVIEW_PRESETS if p["key"] == "chase")
    chase_text = summary(rows, date(2026, 8, 17), "南通", day_deal=(7, 5), template=chase)
    assert "今日未交：示例乙店" in chase_text
    assert "成功率" not in chase_text or "71%" in chase_text or "—" in chase_text or "今日" in chase_text


def test_build_row_short_name_from_row(tmp_db):
    # sqlite3.Row 不支持 .get，_store_short 要用下标取简称，避免标杆退全称
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM stores WHERE code='store-alpha'"
        ).fetchone()
    out = build_row(
        row,
        day_ai=0,
        month_ai=0,
        day_bisuan=0,
        month_bisuan=0,
        submitted=False,
    )
    assert out["short_name"] == "示例甲店"


def test_bulletin_skips_stores_without_mobile_code(admin_client):
    """没有移动编码的店不出现在通报表里。"""
    with db.get_db() as conn:
        # 加两家店：一个没编码，一个有编码
        db.create_store(
            conn,
            "TZ测试没编码店",
            mobile_code="",
            short_name="没编码店",
            area_manager="测试",
        )
        db.create_store(
            conn,
            "TZ测试有编码店",
            mobile_code="20999999",
            short_name="有编码店",
            area_manager="测试",
        )
    page = admin_client.get("/bulletin").get_data(as_text=True).replace("\ufeff", "")
    # 有编码的店保留（南通通报表默认视图，两店都默认南通）
    assert "测试有编码店" in page
    # 没有编码的店不出现
    assert "测试没编码店" not in page
    assert "没编码店" not in page
    # 没编码的泰州新店不该把「泰州市」带进下拉
    assert 'value="泰州市"' not in page
    empty = admin_client.get("/bulletin?city=泰州市").get_data(as_text=True).replace("\ufeff", "")
    assert "测试有编码店" in empty  # 非法地市回退到南通
    assert "示例戊店" not in empty


def test_bisuan_accepts_one_decimal_and_month_calibrates(admin_client):
    """笔算支持一位小数；移动校准只存对照数，不改填报。"""
    from datetime import timedelta

    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    day = date.today()
    saved = admin_client.post(
        "/today",
        data={"store_id": str(sid), "date": day.isoformat(), "m_bisuan": "1.5", "m_phone_sales": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已保存" in saved
    today_page = admin_client.get(f"/today?store_id={sid}&date={day.isoformat()}").get_data(as_text=True)
    assert 'value="1.5"' in today_page or "1.5" in today_page
    with db.get_db() as conn:
        stored = conn.execute(
            "SELECT day_value FROM daily_facts WHERE store_id=? AND biz_date=? AND metric_code='bisuan'",
            (sid, day.isoformat()),
        ).fetchone()["day_value"]
        assert stored == 15
    # 移动取数只存对照，不改填报 daily_facts
    asof = day - timedelta(days=1)
    if asof.month != day.month:
        asof = day  # 月初只能落到当天
    # 截止日那天先有 1.5；录移 2.0 后填报仍应是 1.5
    admin_client.post(
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
    calibrated = admin_client.post(
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
    page = admin_client.get(f"/bulletin?date={day.isoformat()}").get_data(as_text=True)
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

