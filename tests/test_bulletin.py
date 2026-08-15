from datetime import date

from app.bulletin import (
    apply_scales,
    bisuan_total,
    build_row,
    csv_rows,
    fmt_count,
    scale_color,
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
    assert headers[10] == "8月13日AI手机合约"


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
