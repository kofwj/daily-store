from datetime import date

from app.broadcast import add_day_to_prev, render_broadcast
from app.metrics_seed import metric_codes


def test_render_matches_wechat_log():
    biz = date(2026, 8, 13)
    values = {
        "phone_sales": (1, 13),
        "id_check": (1, 4),
        "lead": (0, 9),
        "reserve": (0, 2),
        "bisuan": (0, 5),
        "bisuan_high": (3, 3),
        "ai_contract": (0, 0),
        "anxin_sub": (0, 0),
        "other_card": (0, 5),
        "broadband": (3, 10),
        "tv": (3, 10),
        "tv_member": (0, 0),
        "fttr": (0, 1),
        "gigabit": (0, 2),
        "security": (0, 0),
        "coin_cut_old": (0, 1),
        "phone_discount": (2, 8),
        "gift_2g": (0, 1),
        "welcome_back": (0, 0),
        "renwoxuan": (0, 0),
        "min_spend": (0, 0),
        "fangzha": (0, 3),
        "he_msg": (0, 3),
        "cloud_disk": (0, 0),
        "direct_pack": (1, 1),
        "crbt": (0, 2),
        "migu": (0, 0),
        "safe_mgr": (0, 0),
        "watch_pack": (0, 0),
        "addon": (0, 0),
        "fund": (0, 0),
        "health": (0, 0),
        "pet": (0, 0),
        "new_call": (0, 0),
    }
    text = render_broadcast("示例戊店", biz, values, compact=False)
    assert text.startswith("8月13日\n示例戊店\n")
    assert "当天手机销量：日1，累13" in text
    assert "查询身份证数：日1，累4" in text
    assert "\n重点业务\n比算新增：日0.0，累0.5\n比算新增[高]：日0.3，累0.3\n" in text
    assert "\n新增类\n安心/副卡：日0，累0\n其他卡类：日0，累5\n" in text
    assert "\n家庭类\n宽带：日3，累10\n" in text
    assert "电视会员：日0，累0" in text
    assert "\n终端合约\n金币直降：日0，累1\n购机让利：日2，累8\n" in text
    assert "老用户直降" not in text
    assert "定向包：日1，累1" in text
    assert text.endswith("个人/全家保底：日0，累0\n")


def test_compact_hides_zero_digital_rows():
    biz = date(2026, 8, 13)
    values = {
        "phone_sales": (1, 13),
        "direct_pack": (1, 1),
        "fangzha": (0, 3),
        "cloud_disk": (0, 0),
        "migu": (0, 0),
    }
    text = render_broadcast("示例戊店", biz, values, compact=True)
    assert "定向包：日1，累1" in text
    assert "防诈宝：日0，累3" in text
    assert "云盘" not in text
    assert "咪咕视频" not in text
    assert "当天手机销量：日1，累13" in text


def test_compact_hides_zero_family_rows():
    biz = date(2026, 8, 13)
    values = {
        "phone_sales": (1, 13),
        "broadband": (0, 1),
        "tv": (0, 0),
        "tv_member": (0, 0),
        "fttr": (0, 0),
    }
    text = render_broadcast(
        "示例戊店",
        biz,
        values,
        compact=True,
        compact_sections=("family",),
    )
    assert "宽带：日0，累1" in text
    assert "电视" not in text
    assert "电视会员" not in text
    assert "FTTR" not in text
    assert "宽带续费" not in text
    assert "普通组网" not in text
    assert "当天手机销量：日1，累13" in text


def test_add_day_to_prev_is_month_running_total():
    prev = {"broadband": 7, "tv": 7}
    today = {"broadband": 3, "tv": 3, "phone_sales": 1}
    pairs = add_day_to_prev(prev, today)
    assert pairs["broadband"] == (3, 10)
    assert pairs["tv"] == (3, 10)
    assert pairs["phone_sales"] == (1, 1)


def test_metric_codes_cover_all_seed_items():
    assert "direct_pack" in metric_codes()
    assert "coin_cut_old" in metric_codes()
    assert "coin_cut_new_recharge" in metric_codes()
    assert "coin_cut_new_full" in metric_codes()
    assert "tv_member" in metric_codes()
    assert "broadband_renew" in metric_codes()
    assert "wifi" in metric_codes()
    assert "coin_cut_new" not in metric_codes()
    assert "coin_cut" not in metric_codes()
    assert len(metric_codes()) == 41


def test_broadcast_rolls_coin_cut_parts_into_one_line():
    text = render_broadcast(
        "示例戊店",
        date(2026, 8, 14),
        {
            "coin_cut_old": (1, 4),
            "coin_cut_new_recharge": (1, 2),
            "coin_cut_new_sesame": (1, 1),
            "coin_cut_new_savings": (0, 0),
            "coin_cut_new_full": (2, 3),
            "coin_cut_xtc": (0, 1),
            "phone_discount": (0, 0),
        },
    )
    assert "金币直降：日5，累11" in text
    assert "老用户直降" not in text
    assert "芝麻免充" not in text
    assert "全品类" not in text
    assert "小天才直降" not in text
