from datetime import date

from app.insights import build_insights, prev_week_span, week_span


def _store(sid, name, city="南通市", manager="张经理"):
    return {
        "id": sid,
        "name": name,
        "short_name": name,
        "city": city,
        "area_manager": manager,
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
    assert by_name["甲店"]["week"][1]["now"] == 1  # ai_contract
    assert by_name["甲店"]["week"][1]["delta"] == -1
    assert "今日未交：乙店" in payload["copy_text"]
    assert "本月未交：乙店" in payload["copy_text"]


def test_insights_page_admin_only(app_client):
    denied = app_client.get("/insights")
    assert denied.status_code in (302, 401, 403)
    app_client.post("/login", data={"username": "alpha", "pin": "123456"})
    filler = app_client.get("/insights")
    assert filler.status_code in (302, 403)
    app_client.get("/logout")
    app_client.post("/login", data={"username": "admin", "pin": "1234"})
    page = app_client.get("/insights").get_data(as_text=True)
    assert "洞察" in page
    assert "本月时间进度" in page
    assert "本周 vs 上周" in page
    assert "分店明细" in page
    assert "区域经理" in page
