from datetime import date

from app import db
from app.insights import (
    DEV_MIN_ABS,
    build_deviation_board,
    build_insights,
    catalog_city_order,
    chase_copy_text,
    chase_lag_copy_text,
    clamp_mobile_asof,
    deviation_ref,
    fold_bisuan_reported,
    group_rows_by_city_order,
    prev_week_span,
    store_bulletin_city,
    week_span,
)
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
    assert payload["laggards"][0]["id"] in (1, 2)
    assert "乙店" in payload["chase_text"]
    assert "【催交】" in payload["chase_text"]
    assert "【落后】" in payload["chase_text"]
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
    with db.get_db() as conn:
        db.set_kpi_target(conn, "bisuan_total", 10)
    page = client.get("/insights").get_data(as_text=True)
    assert "洞察" in page
    assert "本月时间进度" in page
    assert "本周 vs 上周" in page
    assert "分店明细" in page
    assert "区域经理" in page
    assert "运营商顾问" in page
    assert "只看有顾问" in page
    assert "insight-metric-h" in page
    assert "上是本月累计" in page
    assert "环比" in page
    assert "同比" not in page
    assert "复制文案" not in page
    assert "复制催办" in page
    assert "落后于时间" in page
    assert "【催交】" in page
    assert "class=\"chase-text\"" in page
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
    assert f"store_id={sid}" in page
    assert f"start={date.today().replace(day=1).isoformat()}" in page
    assert f"end={date.today().isoformat()}" in page
    idle_page = client.get("/insights?idle=1").get_data(as_text=True)
    assert "insight-month" in idle_page
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
    assert "复制催交" in page
    assert "【催交】" in page
    assert f"store_id={sid}" in page
    assert f"start={date.today().replace(day=1).isoformat()}" in page
    assert f"end={date.today().isoformat()}" in page
    assert "class=\"chase-text\"" in page
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
    reported = {
        1: 120,  # 填报120 < 移动150 → +30 少报
        2: 200,  # 填报200 > 移动150 → -50 多报
    }
    mobile = {1: 150, 2: 150}
    rows = build_deviation_board(stores=stores, reported=reported, mobile_bisuan=mobile)
    assert [r["id"] for r in rows] == [2, 1]  # |−50| 排前
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["diff"] == 30 and by_id[1]["under"] is True
    assert by_id[2]["diff"] == -50 and by_id[2]["over"] is True
    assert 3 not in by_id  # 无移动校准数，排除


def test_clamp_mobile_asof_and_fold_cuts_after_asof():
    """空 asof 用 ref；非法回落；按店截止日后的填报不计入。"""
    month_start = date(2026, 8, 1)
    ref = date(2026, 8, 31)
    assert clamp_mobile_asof("", month_start=month_start, ref=ref) == ref
    assert clamp_mobile_asof("坏", month_start=month_start, ref=ref) == ref
    assert clamp_mobile_asof("2026-08-10", month_start=month_start, ref=ref) == date(2026, 8, 10)
    assert clamp_mobile_asof("2026-07-31", month_start=month_start, ref=ref) == month_start
    assert clamp_mobile_asof("2026-09-01", month_start=month_start, ref=ref) == ref
    daily = [
        {"store_id": 1, "biz_date": "2026-08-05", "metric_code": "bisuan", "day_value": 50},
        {"store_id": 1, "biz_date": "2026-08-05", "metric_code": "bisuan_high", "day_value": 10},
        {"store_id": 1, "biz_date": "2026-08-20", "metric_code": "bisuan", "day_value": 80},
        {"store_id": 2, "biz_date": "2026-08-20", "metric_code": "bisuan", "day_value": 30},
    ]
    folded = fold_bisuan_reported(
        daily,
        store_ids=[1, 2],
        asof_by_store={1: date(2026, 8, 10), 2: date(2026, 8, 31)},
    )
    assert folded[1] == 60  # 8/20 的 80 被截掉
    assert folded[2] == 30


def test_build_deviation_board_asof_end_matches_old_full_month():
    """asof=月末时，结果与「整月填报 vs 移动」相同。"""
    stores = [{"id": 1, "short_name": "甲店", "name": "甲店", "city": "南通"}]
    rows = build_deviation_board(
        stores=stores,
        reported={1: 120},
        mobile_bisuan={1: 150},
        asof_by_store={1: date(2026, 8, 31)},
        asof_raw={1: "2026-08-31"},
        month_end=date(2026, 8, 31),
    )
    assert rows[0]["diff"] == 30
    assert rows[0]["asof_text"] == "8/31"
    blank = build_deviation_board(
        stores=stores,
        reported={1: 120},
        mobile_bisuan={1: 150},
        asof_by_store={1: date(2026, 8, 31)},
        asof_raw={1: ""},
        month_end=date(2026, 8, 31),
    )
    assert blank[0]["asof_text"] == "月末"


def test_board_ranks_by_bisuan_not_sum_and_hides_idle_month(admin_client):
    """看板按比算排序，不按三项加总；本月未交店只出现在缺交区。"""
    today = date.today()
    with db.get_db() as conn:
        alpha = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        beta = conn.execute("SELECT id FROM stores WHERE code='store-beta'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(
            conn,
            store_id=alpha,
            biz_date=today,
            values={"bisuan": 20, "ai_contract": 1},
            user_id=uid,
        )
        db.save_daily(
            conn,
            store_id=beta,
            biz_date=today,
            values={"bisuan": 5, "ai_contract": 80},
            user_id=uid,
        )
    today_page = admin_client.get("/board?view=today").get_data(as_text=True)
    assert "按比算新增排序" in today_page
    assert "三项合计" not in today_page
    ia = today_page.find("示例甲店")
    ib = today_page.find("示例乙店")
    assert 0 <= ia < ib  # 甲比算更高，排在乙前；若按加总会是乙在前
    month_page = admin_client.get("/board?view=month").get_data(as_text=True)
    assert "按比算新增排序" in month_page
    assert "三 KPI 累计" not in month_page and "三项累计" not in month_page
    assert "按本月比算新增累计排名" in month_page
    assert "示例丙店" in month_page
    import re
    with db.get_db() as conn:
        gamma = conn.execute("SELECT id FROM stores WHERE code='store-gamma'").fetchone()["id"]
    assert re.search(rf"/today\?[^\"']*store_id={gamma}", month_page)
    assert not re.search(rf"/report\?[^\"']*store_id={gamma}", month_page)


def test_board_month_empty_copy_and_city_scope(admin_client):
    empty = admin_client.get("/board?view=month").get_data(as_text=True)
    assert "本月没有已交的店可排" in empty
    assert "今天没有店交数" not in empty
    scoped = admin_client.get("/board?city=邻市").get_data(as_text=True)
    assert "【范围】邻市" in scoped
    assert "示例戊店" in scoped
    assert "class=\"chase-text\"" in scoped


def test_chase_copy_and_deviation_ref():
    as_of = date(2026, 8, 16)
    assert chase_copy_text(as_of=as_of, names=[]) == ""
    assert chase_copy_text(as_of=as_of, names=["甲店", "乙店"]) == "【催交】8月16日未交日报（2家）\n甲店、乙店"
    assert "还没交过" in chase_copy_text(as_of=as_of, names=["丙店"], kind="month")
    text = chase_lag_copy_text(
        as_of=as_of,
        pace=51.6,
        missing_today=["乙店"],
        laggards=[{"name": "乙店", "bits": ["比算新增 0.0/10（0%）"]}],
    )
    assert "【催交】8月16日未交（1家）" in text
    assert "乙店：比算新增 0.0/10（0%）" in text
    assert "落后超过 15 个百分点" in text
    scoped = chase_copy_text(as_of=as_of, names=["甲店"], scope_label="邻市")
    assert scoped.startswith("【范围】邻市")
    lag_scoped = chase_lag_copy_text(
        as_of=as_of, pace=50, missing_today=["乙店"], laggards=[], scope_label="示例市"
    )
    assert lag_scoped.startswith("【范围】示例市")
    assert deviation_ref(date(2026, 10, 1), date(2026, 10, 31), date(2026, 9, 21)) == date(2026, 10, 1)
    assert deviation_ref(date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 21)) == date(2026, 8, 31)
    assert deviation_ref(date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 21)) == date(2026, 9, 21)
    cities = ["示例市", "邻市"]
    assert store_bulletin_city({"mobile_code": "1", "city": "邻市"}, cities) == "邻市"
    assert store_bulletin_city({"mobile_code": "", "city": "邻市"}, cities) == ""
    assert store_bulletin_city({"mobile_code": "1", "city": "未名市"}, cities) == ""


def test_insights_monday_notes_one_day_week(admin_client):
    page = admin_client.get("/insights?date=2026-09-21").get_data(as_text=True)
    assert "本周仅 1 天" in page
    assert "环比" in page
    later = admin_client.get("/insights?date=2026-09-22").get_data(as_text=True)
    assert "本周仅 1 天" not in later


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
    assert "截止日同期" in html
    assert "元" not in html  # 计数单位是个，不是金额
    assert "少报" in html and "多报" in html
    assert "少报" in html and "多报" in html
    assert "/bulletin?" in html
    assert "date=2026-08-31" in html


def test_deviation_page_cuts_reported_at_asof(admin_client):
    """月中 asof 之后的填报不算进温差；整月对照会得到 0。"""
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(
            conn, store_id=sid, biz_date=date(2026, 8, 5), values={"bisuan": 50}, user_id=uid
        )
        db.save_daily(
            conn, store_id=sid, biz_date=date(2026, 8, 20), values={"bisuan": 50}, user_id=uid
        )
        db.save_bisuan_mobile(
            conn, store_id=sid, month="2026-08", value_tenths=100, asof=date(2026, 8, 10)
        )
    html = admin_client.get("/deviation?month=2026-08-01").get_data(as_text=True)
    assert "至 8/10" in html
    assert "+5.0" in html
    assert "一致" not in html
    assert "date=2026-08-10" in html
    assert "/bulletin?" in html


def test_deviation_empty_when_mobile_only_on_inactive_store(admin_client):
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        db.save_bisuan_mobile(
            conn, store_id=sid, month="2026-08", value_tenths=120, asof=date(2026, 8, 31)
        )
        db.set_store_active(conn, sid, False)
    html = admin_client.get("/deviation?month=2026-08-01").get_data(as_text=True)
    assert "本月没有移动校准数" in html
    assert "纳入对比" not in html


def test_deviation_without_mobile_code_links_report(admin_client):
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code='store-gamma'").fetchone()["id"]
        db.save_bisuan_mobile(
            conn, store_id=sid, month="2026-08", value_tenths=50, asof=date(2026, 8, 10)
        )
    html = admin_client.get("/deviation?month=2026-08-01").get_data(as_text=True)
    assert f"store_id={sid}" in html
    assert "/report?" in html
    assert "/bulletin?" not in html


def test_insights_mobile_used_follows_scope(admin_client):
    with db.get_db() as conn:
        conn.execute("UPDATE stores SET advisor_name='李顾问' WHERE code='store-beta'")
        alpha = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        db.save_bisuan_mobile(
            conn,
            store_id=alpha,
            month=date.today().strftime("%Y-%m"),
            value_tenths=80,
            asof=date.today(),
        )
    yes = admin_client.get("/insights?advisor=yes").get_data(as_text=True)
    assert "按移动校准数" not in yes
    no = admin_client.get("/insights?advisor=no").get_data(as_text=True)
    assert "按移动校准数" in no


def test_copy_js_does_not_fake_success_when_execcommand_fails():
    from pathlib import Path

    src = Path("app/static/copy.js").read_text(encoding="utf-8")
    assert "ok = !!document.execCommand('copy')" in src
    assert "请长按文本框全选复制" in src
    assert "setCopyHint(doneMsg)" in src
    # 失败路径必须露源，不能只提示已复制
    fail_at = src.find("ok = !!document.execCommand")
    reveal_at = src.find("revealCopySource")
    assert 0 <= fail_at < reveal_at or src.count("revealCopySource") >= 1


def test_catalog_city_order_groups_stable_and_keeps_rank():
    stores = [
        _store(1, "甲店", "示例市"),
        _store(2, "乙店", "邻市"),
        _store(3, "丙店", "示例市"),
        _store(4, "丁店", ""),
    ]
    order = catalog_city_order(stores)
    assert order == ["示例市", "邻市", "未分地市"]
    rows = [
        {"name": "乙店", "city": "邻市", "rank": 1},
        {"name": "丙店", "city": "示例市", "rank": 2},
        {"name": "甲店", "city": "示例市", "rank": 3},
        {"name": "丁店", "city": "未分地市", "rank": 4},
    ]
    grouped = group_rows_by_city_order(rows, order, lambda r: r["city"])
    assert [r["name"] for r in grouped] == ["丙店", "甲店", "乙店", "丁店"]
    assert [r["rank"] for r in grouped] == [2, 3, 1, 4]
    assert DEV_MIN_ABS == 10


def test_board_groups_by_city_keeps_global_rank(admin_client):
    """目录地市切开后组内仍按比算；# 仍是全区名次。"""
    import re

    today = date.today()
    with db.get_db() as conn:
        alpha = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        epsilon = conn.execute("SELECT id FROM stores WHERE code='store-epsilon'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(conn, store_id=alpha, biz_date=today, values={"bisuan": 5}, user_id=uid)
        db.save_daily(conn, store_id=epsilon, biz_date=today, values={"bisuan": 50}, user_id=uid)
    page = admin_client.get("/board?view=today").get_data(as_text=True)
    pairs = re.findall(
        r'class="rank-col">(\d+)</td>\s*<td class="left store-cell">\s*<a class="store-name"[^>]*>([^<]+)</a>',
        page,
    )
    assert pairs[0] == ("2", "示例甲店")
    assert pairs[1] == ("1", "示例戊店")
    assert "city-start" in page
    assert "city-band" not in page
    assert page.find("</tbody>") < page.find("city-sub")
    assert "示例市 · 1 家" in page
    assert "邻市 · 1 家" in page
    assert "按地市分组（目录顺序）" in page
    assert "# 是全区名次" in page
    assert f"store_id={alpha}" in page
    assert f"start={today.replace(day=1).isoformat()}" in page
    assert f"end={today.isoformat()}" in page


def test_insights_store_table_groups_by_catalog_city(admin_client):
    page = admin_client.get("/insights?idle=1").get_data(as_text=True)
    start = page.find("insight-stores")
    assert start >= 0
    chunk = page[start : page.find("</table>", start)]
    shi = ["示例甲店", "示例乙店", "示例丙店"]
    lin = ["示例丁店", "示例戊店", "示例巳店"]
    for a in shi:
        for b in lin:
            assert chunk.index(a) < chunk.index(b)
    assert "city-start" in chunk
    assert "city-band" not in chunk
    assert "分店明细" in page
    assert "按地市分组（目录顺序）" in page
    assert "已显示本月未交" in page
    assert 'name="idle"' in page


def test_deviation_hides_small_diff_groups_by_city(admin_client):
    with db.get_db() as conn:
        alpha = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        beta = conn.execute("SELECT id FROM stores WHERE code='store-beta'").fetchone()["id"]
        delta = conn.execute("SELECT id FROM stores WHERE code='store-delta'").fetchone()["id"]
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(
            conn, store_id=alpha, biz_date=date(2026, 8, 5), values={"bisuan": 100}, user_id=uid
        )
        db.save_daily(
            conn, store_id=beta, biz_date=date(2026, 8, 5), values={"bisuan": 100}, user_id=uid
        )
        db.save_daily(
            conn, store_id=delta, biz_date=date(2026, 8, 5), values={"bisuan": 200}, user_id=uid
        )
        db.save_bisuan_mobile(
            conn, store_id=alpha, month="2026-08", value_tenths=200, asof=date(2026, 8, 10)
        )
        db.save_bisuan_mobile(
            conn, store_id=beta, month="2026-08", value_tenths=105, asof=date(2026, 8, 10)
        )
        db.save_bisuan_mobile(
            conn, store_id=delta, month="2026-08", value_tenths=50, asof=date(2026, 8, 10)
        )
    html = admin_client.get("/deviation?month=2026-08-01").get_data(as_text=True)
    assert "示例甲店" in html
    assert "示例乙店" not in html
    assert "示例丁店" in html
    assert html.index("示例甲店") < html.index("示例丁店")
    assert "city-start" in html
    assert "city-band" not in html
    assert html.find("</tbody>") < html.find("city-sub")
    assert "示例市 · 1 家" in html
    assert "邻市 · 1 家" in html
    assert "已藏 |温差| 不到 1.0 的 1 店" in html
    assert "all=1" in html
    assert "side=under" in html
    assert "side=over" in html
    assert "3 店没有移动校准，未进对比" in html
    assert "不是温差 0" in html
    assert "/bulletin?" in html
    assert "date=2026-08-10" in html
    all_html = admin_client.get("/deviation?month=2026-08-01&all=1").get_data(as_text=True)
    assert "示例乙店" in all_html
    assert "含 |温差| 不到 1.0 的 1 店" in all_html
    under = admin_client.get("/deviation?month=2026-08-01&side=under").get_data(as_text=True)
    assert "示例甲店" in under
    assert "示例丁店" not in under
    over = admin_client.get("/deviation?month=2026-08-01&side=over").get_data(as_text=True)
    assert "示例丁店" in over
    assert "示例甲店" not in over

