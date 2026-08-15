from app.deal import form_values, render_deal


def test_closed_deal_uses_short_layout():
    text = render_deal(
        "通州金沙",
        model="S60元气版",
        phone="15514408478",
        spend="99",
        hall_query="1",
        recommend="89",
        closed="1",
        student="0",
        opener="奚其梅",
        note="用户因价格问题来回拉扯，推荐芝麻直降1600，剩余额度办了分期免息，旧手机抵325元",
    )
    assert text.startswith("通州金沙 · 成交\n")
    assert "S60元气版｜1551440****｜消费99" in text
    assert "15514408478" not in text
    assert "掌厅已查 · 荐89 · 非中高考" in text
    assert "开口 奚其梅" in text
    assert "🌸" not in text
    assert "是否成交" not in text


def test_open_deal_marks_not_closed():
    text = render_deal("海门龙信", model="X300", closed="0", hall_query="0", student="1")
    assert text.startswith("海门龙信 · 未成交\n")
    assert "未查掌厅" in text
    assert "中高考" in text
    assert "非中高考" not in text


def test_unchecked_boxes_stay_off_after_post():
    values = form_values({"model": "X300"}, posted=True)
    assert values["hall_query"] == "0"
    assert values["closed"] == "0"
    assert values["student"] == "0"
    assert values["show_phone"] == "0"


def test_show_phone_keeps_full_number():
    text = render_deal("通州金沙", phone="15514408478", show_phone="1")
    assert "15514408478" in text
