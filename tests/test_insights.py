from datetime import date

from app import db
from app.insights import build_deviation_board, build_insights, prev_week_span, week_span
from app.metrics_seed import effective_month_bisuan


def _store(sid, name, city="南通市", manager="张经理", advisor="李顾问"):
    return {
        "id": sid,
        "name": name,
        "short_name": name,
        "city": city,
        "area_manager": manager,
        "advisor_name": advisor,
    }


def test_week_span_aligns_same_weekdays():
    as_of = date(2026, 8, 18)  # 周二
    assert week_span(as_of) == (date(2026, 8, 17), date(2026, 8, 18))
    assert prev_week_span(as_of) == (date(2026, 8, 10), date(2026, 8, 11))
    monday = date(2026, 8, 17)
    assert week_span(monday) == (date(2026, 8, 17), date(2026, 8, 17))
    assert prev_week_span(monday) == (date(2026, 8, 10), date(2026, 8, 10))


def test_insights_pace_week_compare_and_laggards():
    as_of = date(2026, 8, 16)  # 16/31 ≈ 51.6%
    stores = [_store(1, "甲店"), _store(2, "乙店")]
    payload = build_insights(
        stores=stores,
        as_of=as_of,
        kpi_targets={"bisuan_total": 10, "ai_contract": 5, "coin_cut": 5},
        month_facts={
            1: {"bisuan": 20, "bisuan_high": 0, "ai_contract": 4, "coin_cut_new_recharge": 1},
            2: {"bisuan": 0, "ai_contract": 0},
        },
        week_facts={
            1: {"bisuan": 10, "ai_contract": 1},
            2: {"bisuan": 0, "ai_contract": 0},
        },
        prev_week_facts={
            1: {"bisuan": 5, "ai_contract": 2},
            2: {"bisuan": 5, "ai_contract": 0},
        },
        reported_today={1},
        reported_month={1},
    )
    assert abs(payload["pace"] - 16 / 31 * 100) < 0.2
    assert payload["done_today"] == 1
    assert payload["missing_today"] == ["乙店"]
    assert payload["missing_month"] == ["乙店"]
    names = {k["code"]: k for k in payload["kpis"]}
    assert names["ai_contract"]["value_text"] == "4"
    assert names["bisuan_total"]["value_text"] == "2.0"
    week = {k["code"]: k for k in payload["week_kpis"]}
    assert week["ai_contract"]["now_text"] == "1"
    assert week["ai_contract"]["prev_text"] == "2"
    assert week["ai_contract"]["delta"] == -1
    lag_names = [x["name"] for x in payload["laggards"]]
    assert "乙店" in lag_names
    by_name = {r["name"]: r for r in payload["store_rows"]}
    assert by_name["乙店"]["flags"] == ["今日未交", "本月未交", "进度落后"]
    assert by_name["乙店"]["month_ok"] is False
    assert payload["idle_n"] == 1
    assert by_name["甲店"]["advisor"] == "李顾问"
    assert by_name["甲店"]["week"][1]["now"] == 1  # ai_contract
    assert by_name["甲店"]["week"][1]["delta"] == -1


def test_insights_mobile_bisuan_overrides_month_for_ranking():
    as_of = date(2026, 8, 16)
    stores = [_store(1, "甲店"), _store(2, "乙店")]
    payload = build_insights(
        stores=stores,
        as_of=as_of,
        kpi_targets={"bisuan_total": 10},
        month_facts={
            1: {"bisuan": 5, "bisuan_high": 0},  # 填报 5 台
            2: {"bisuan": 30, "bisuan_high": 0},  # 填报 30 台
        },
        week_facts={1: {}, 2: {}},
        prev_week_facts={1: {}, 2: {}},
        reported_today=set(),
        reported_month={1, 2},
        mobile_bisuan={1: 80},  # 甲店移动校准 8.0（store 80 = 8.0×10）
    )
    by_name = {r["name"]: r for r in payload["store_rows"]}
    # 甲店当月比算用移动 8.0，而非填报 0.5
    m1 = {b["code"]: b for b in by_name["甲店"]["month"]}
    assert m1["bisuan_total"]["value"] == 8.0
    # 乙店无移动校准，仍是填报 3.0
    m2 = {b["code"]: b for b in by_name["乙店"]["month"]}
    assert m2["bisuan_total"]["value"] == 3.0
    # 总进度跟着移动口径
    kpi = {k["code"]: k for k in payload["kpis"]}["bisuan_total"]
    assert kpi["value"] == 11.0


def test_effective_month_bisuan_is_single_caliber():
    """移动口径的单一入口：有移动用移动，否则填报，坏值回落。"""
    assert effective_month_bisuan(120, 60) == 120
    assert effective_month_bisuan(None, 60) == 60
    assert effective_month_bisuan("", 60) == 60
    assert effective_month_bisuan("坏", 60) == 60
    assert effective_month_bisuan(None, 0) == 0


def test_insights_page_admin_only(client):
    denied = client.get("/insights")
    assert denied.status_code in (302, 401, 403)
    client.post("/login", data={"username": "alpha", "pin": "123456"})
    filler = client.get("/insights")
    assert filler.status_code in (302, 403)
    client.post("/logout")
    client.post("/login", data={"username": "admin", "pin": "123456"})
    page = client.get("/insights").get_data(as_text=True)
    assert "洞察" in page
    assert "本月时间进度" in page
    assert "本周 vs 上周" in page
    assert "分店明细" in page
    assert "区域经理" in page
    assert "运营商顾问" in page
    assert "只看有顾问" in page
    assert "insight-metric-h" in page
    assert "复制文案" not in page
    filtered = client.get("/insights?advisor=yes").get_data(as_text=True)
    assert 'value="yes"' in filtered


def test_report_ignores_inactive_metric_facts(admin_client):
    """即使某天留下了停用指标的 day 值，报表也不能崩，应忽略。"""
    at = date.today().isoformat()
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
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
    resp = admin_client.get("/report")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "手机销量" in body


def test_week_report_range_clamped(admin_client):
    """周报区间钳到今天且最大 62 天，恶意大日期不能撑爆内存。"""
    page = admin_client.get("/report?view=week&start=2000-01-01&end=9999-12-31").get_data(as_text=True)
    assert "9999" not in page
    assert "2000-01" not in page
    # 起点在未来：起点跟着钳后的终点走，不出现倒挂区间
    page = admin_client.get("/report?view=week&start=9999-12-01&end=9999-12-31").get_data(as_text=True)
    assert "9999" not in page


def test_board_shows_deals_and_exports_xlsx(admin_client):
    """看板含成交列，点店名进报表，当前视图可导出 Excel。"""
    from io import BytesIO

    import openpyxl

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
    page = admin_client.get("/board").get_data(as_text=True)
    assert "触客" in page
    assert "成交/触客" in page
    assert "示例甲店" in page
    assert "/report?" in page
    r = admin_client.get("/board.xlsx?view=today")
    assert r.status_code == 200
    assert r.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "filename=board_today_" in r.headers.get("Content-Disposition", "")
    wb = openpyxl.load_workbook(BytesIO(r.get_data()))
    header = [cell.value for cell in wb.active[1]]
    assert header[:3] == ["排名", "门店", "地市"]
    assert "触客" in header and "成功率" in header


def test_build_deviation_board_sorts_and_signs():
    """偏差榜：按绝对值降序，少报=正、多报=负，无移动的店排除。"""
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
    rows = build_deviation_board(stores=stores, month_facts=facts, mobile_bisuan=mobile)
    assert [r["id"] for r in rows] == [2, 1]  # |−50| 排前
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["diff"] == 30 and by_id[1]["under"] is True
    assert by_id[2]["diff"] == -50 and by_id[2]["over"] is True
    assert 3 not in by_id  # 无移动校准数，排除


def test_deviation_page_admin_only(admin_client):
    """偏差路由是管理员专属，且能渲染（单位是个，不是元）。"""
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        db.save_bisuan_mobile(
            conn, store_id=sid, month="2026-08", value_tenths=120, asof=date.today()
        )
    r = admin_client.get("/deviation?month=2026-08-01")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "填报偏差榜" in html
    assert "温差" in html
    assert "元" not in html  # 计数单位是个，不是金额
    assert "少报" in html and "多报" in html
