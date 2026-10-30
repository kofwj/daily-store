"""V5 算账引擎纯函数单测（第四阶段规范《三》E1–E6 及分档边界）。

只测算术，不碰数据库：引擎全部是纯函数，输入纯数据、输出纯数据。
"""

from __future__ import annotations

import pytest

from app import v5_engine as engine
from app.metrics_seed import v5_metric_codes

# ---------------------------------------------------------------------------
# E1：顺差让利单店分档，小店绝不跟随母店跳点
# ---------------------------------------------------------------------------


def test_e1_affiliated_shop_keeps_its_own_tier():
    # 挂靠小店 4 笔：全部按 5 元/笔，绝不因为母店工号满 15 笔而跳到 10 元
    assert engine.calc_rebate_commission(4, engine.ROLE_AFFILIATED_SHOP_50) == 2000
    # 母店自办 15 笔：前 10 笔 10 元、第 11 笔起 20 元
    assert engine.calc_rebate_commission(15, engine.ROLE_SHOP_FULL_100) == 20000


@pytest.mark.parametrize(
    "count,role,expected",
    [
        (0, engine.ROLE_SHOP_FULL_100, 0),
        (1, engine.ROLE_SHOP_FULL_100, 1000),
        (10, engine.ROLE_SHOP_FULL_100, 10000),
        (11, engine.ROLE_SHOP_FULL_100, 12000),
        (20, engine.ROLE_SHOP_FULL_100, 30000),
        (0, engine.ROLE_ADVISOR_SHOP_50, 0),
        (10, engine.ROLE_ADVISOR_SHOP_50, 5000),
        (11, engine.ROLE_ADVISOR_SHOP_50, 6000),
        (10, engine.ROLE_AFFILIATED_SHOP_50, 5000),
        (11, engine.ROLE_AFFILIATED_SHOP_50, 6000),
        (-3, engine.ROLE_SHOP_FULL_100, 0),  # 负数按 0 处理，不倒扣
    ],
)
def test_rebate_commission_tiers(count, role, expected):
    assert engine.calc_rebate_commission(count, role) == expected


def test_rebate_commission_rejects_unknown_role():
    with pytest.raises(ValueError):
        engine.calc_rebate_commission(3, "PARENT_ASSIST_50")  # 已废弃的母店代提枚举


def test_rebate_metric_code_is_a_real_v5_metric():
    # 对接清单写作 v5_m_rebate，落库实际是 v5_m7；这里钉住映射，避免引擎取错科目
    assert engine.REBATE_METRIC_CODE == "v5_m7"
    assert engine.REBATE_METRIC_CODE in v5_metric_codes()


# ---------------------------------------------------------------------------
# E2：第一道闸，母子工号合并比对与同比例核减
# ---------------------------------------------------------------------------


def test_e2_over_report_is_scaled_down_proportionally():
    # 申报 15 笔 vs 实结 10 笔，上限 12 笔，缩减比例 12/15 = 0.8
    got = engine.reconcile_carrier_counts(100, 10, {100: 10, 201: 5})
    assert got == {100: 8, 201: 4}
    assert sum(got.values()) == 12


def test_reconcile_passes_whole_group_within_tolerance():
    got = engine.reconcile_carrier_counts(100, 10, {100: 8, 201: 3})
    assert got == {100: 8, 201: 3}


def test_reconcile_tolerance_boundary_is_inclusive():
    # 申报总量正好 12 = 10 × 1.2，不触发核减
    got = engine.reconcile_carrier_counts(100, 10, {100: 8, 201: 4})
    assert got == {100: 8, 201: 4}


def test_reconcile_keeps_parent_key_when_missing():
    # 母店日报缺失也要留键，否则子店联动读母店行会整组挂起
    got = engine.reconcile_carrier_counts(100, 10, {201: 4})
    assert got == {100: 0, 201: 4}


def test_reconcile_zero_settled_zeroes_everything():
    assert engine.reconcile_carrier_counts(100, 0, {100: 5, 201: 3}) == {100: 0, 201: 0}


def test_reconcile_empty_reports_does_not_crash():
    assert engine.reconcile_carrier_counts(100, 10, {}) == {100: 0}


# ---------------------------------------------------------------------------
# E3 / E4 / B8：熔断系数与实发总包
# ---------------------------------------------------------------------------


def test_e3_below_half_baseline_fuses_shop_pool():
    ratio = engine.shop_payout_ratio(480_000, 1_000_000)
    assert ratio == 0.0
    assert engine.shop_final_pool(30000, ratio) == 0


@pytest.mark.parametrize(
    "net,baseline,expected",
    [
        (700_000, 1_000_000, 1.0),   # 70% 整
        (1_200_000, 1_000_000, 1.0),
        (690_000, 1_000_000, 0.8),
        (500_000, 1_000_000, 0.8),   # 50% 整
        (499_999, 1_000_000, 0.0),
    ],
)
def test_shop_payout_ratio_tiers(net, baseline, expected):
    assert engine.shop_payout_ratio(net, baseline) == expected


def test_e4_negative_net_commission_falls_to_zero():
    assert engine.shop_payout_ratio(-50_000, 1_000_000) == 0.0


def test_payout_ratio_exempt_and_missing_baseline():
    # 跨期追扣经管理员豁免强制回 1.0；基线没配置也不熔断
    assert engine.shop_payout_ratio(100_000, 1_000_000, exempt_cross_month=True) == 1.0
    assert engine.shop_payout_ratio(100_000, 0) == 1.0
    assert engine.shop_payout_ratio(0, 0) == 1.0


def test_shop_final_pool_subtracts_clawback_after_ratio():
    # 10000 × 0.8 − 1500，追回款只冲减、不进负向扣款
    assert engine.shop_final_pool(10000, 0.8, 1500) == 6500
    # 冲减不破 0
    assert engine.shop_final_pool(1000, 1.0, 5000) == 0


def test_shop_final_pool_rounds_half_up():
    assert engine.shop_final_pool(10001, 0.5) == 5001


def test_shop_final_pool_clamps_ratio():
    assert engine.shop_final_pool(1000, 2.0) == 1000
    assert engine.shop_final_pool(1000, -1.0) == 0


def test_cap_raw_pool_by_ratio_and_absolute():
    # 净酬金 1000 元 × 35% = 350 元封顶
    assert engine.cap_raw_pool(40000, 100_000) == 35000
    assert engine.cap_raw_pool(20000, 100_000) == 20000
    # 顾问店绝对封顶 650 元优先于比例封顶
    assert engine.cap_raw_pool(70000, 1_000_000, cap_abs_cents=engine.ADVISOR_CAP_ABS_CENTS) == 65000
    # 两条都不启用时原样返回
    assert engine.cap_raw_pool(70000, 1_000_000, cap_ratio=0, cap_abs_cents=0) == 70000


def test_cap_abs_for_only_advisor_modes():
    assert engine.cap_abs_for("ADVISOR_A") == engine.ADVISOR_CAP_ABS_CENTS
    assert engine.cap_abs_for("ADVISOR_B_NEAR") == engine.ADVISOR_CAP_ABS_CENTS
    assert engine.cap_abs_for("NO_ADVISOR_MAIN") == 0
    assert engine.cap_abs_for("AFFILIATED") == 0


# ---------------------------------------------------------------------------
# 店长 100 分考核
# ---------------------------------------------------------------------------


def _scores(**over):
    args = dict(
        net_commission_cents=100_000,
        baseline_cents=100_000,
        core_biz_count=20,
        phone_sales=100,
        target_carry_rate=0.20,
        tiers=engine.carry_tiers_for("MOBILE"),
        gross_commission_cents=1_000_000,
        total_deduction_cents=0,
        deduct_3b_total_cents=0,
    )
    args.update(over)
    return engine.calc_manager_scores(**args)


def test_scores_full_marks():
    got = _scores()
    assert got["score_1"] == 60.0
    assert got["score_2"] == 25.0
    assert got["score_3a"] == 10.0
    assert got["score_3b"] == 5.0
    assert got["total_100"] == 100.0
    assert got["mgr_score_20"] == 20.0
    assert got["mgr_score_15"] == 15.0


def test_score_1_ladder_matches_spec_examples():
    assert _scores(net_commission_cents=80_000)["score_1"] == 48.0   # 80% → 48 分
    assert _scores(net_commission_cents=70_000)["score_1"] == 42.0   # 70% → 42 分
    assert _scores(net_commission_cents=60_000)["score_1"] == 28.8   # 50–70% 打八折
    assert _scores(net_commission_cents=49_999)["score_1"] == 0.0    # <50% → 0 分
    assert _scores(net_commission_cents=-50_000)["score_1"] == 0.0
    assert _scores(net_commission_cents=100_000, baseline_cents=0)["score_1"] == 0.0


@pytest.mark.parametrize(
    "cores,expected",
    [(20, 25.0), (15, 18.0), (10, 10.0), (9, 0.0)],
)
def test_score_2_mobile_tiers(cores, expected):
    assert _scores(core_biz_count=cores)["score_2"] == expected


@pytest.mark.parametrize(
    "cores,expected",
    [(12, 25.0), (9, 18.0), (6, 10.0), (5, 0.0)],
)
def test_score_2_telecom_tiers(cores, expected):
    got = _scores(core_biz_count=cores, phone_sales=100, target_carry_rate=0.12,
                  tiers=engine.carry_tiers_for("TELECOM"))
    assert got["score_2"] == expected


def test_score_2_derives_tiers_when_only_target_rate_given():
    # tiers 全 0 时由满分阈值推导：及格 0.75×、底线 0.5×
    got = _scores(core_biz_count=15, target_carry_rate=0.20, tiers=(0.0, 0.0, 0.0))
    assert got["score_2"] == 18.0
    got = _scores(core_biz_count=10, target_carry_rate=0.20, tiers=(0.0, 0.0, 0.0))
    assert got["score_2"] == 10.0


def test_e5_zero_phone_sales_does_not_divide_by_zero():
    got = _scores(core_biz_count=0, phone_sales=0)
    assert got["score_2"] == 0.0
    assert got["actual_carry_rate"] == 0.0


@pytest.mark.parametrize(
    "deduct,expected",
    [(0, 10.0), (5_000, 10.0), (7_000, 8.0), (15_000, 5.0), (30_000, 0.0)],
)
def test_score_3a_deduction_rate_tiers(deduct, expected):
    assert _scores(total_deduction_cents=deduct)["score_3a"] == expected


def test_score_3a_broadband_penalty_and_clamp():
    assert _scores(total_deduction_cents=5_000, broadband_star_penalty=True)["score_3a"] == 8.0
    # 已经是 0 分时不再减成负数
    assert _scores(total_deduction_cents=30_000, broadband_star_penalty=True)["score_3a"] == 0.0


def test_score_3b_redline_veto():
    assert _scores(deduct_3b_total_cents=0)["score_3b"] == 5.0
    got = _scores(deduct_3b_total_cents=1)
    assert got["score_3b"] == 0.0
    assert got["total_100"] == 95.0


def test_total_score_is_clamped_to_100():
    got = _scores(net_commission_cents=10_000_000, core_biz_count=1000)
    assert got["total_100"] == 100.0


# ---------------------------------------------------------------------------
# E6：顾问薪酬按任期折算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "work_days,month_days,expected",
    [(0, 30, 1.0), (30, 30, 1.0), (31, 30, 1.0), (15, 30, 0.5), (1, 30, 1 / 30)],
)
def test_tenure_coef(work_days, month_days, expected):
    assert engine.tenure_coef(work_days, month_days) == pytest.approx(expected)


def test_e6_partial_month_halves_base_and_breakeven():
    got = engine.calc_advisor_salary(
        1_500_000,
        {
            "base_pay_cents": 300_000,
            "base_coef": 1.2,
            "breakeven_cents": 2_000_000,
            "excess_rate": 0.4,
            "work_days": 15,
        },
        1_000,
        month_days=30,
    )
    assert got["base_pay"] == 180_000   # 3000 元 × 1.2 × 0.5
    assert got["breakeven"] == 1_000_000
    assert got["excess_bonus"] == 200_000  # (15000 − 10000 元) × 40%
    assert got["penalty"] == 500           # 3B 1000 分 × 50%
    assert got["final_pay"] == 379_500


def test_advisor_full_month_tier_a_salary():
    got = engine.calc_advisor_salary(
        2_500_000,
        {
            "base_pay_cents": 300_000,
            "base_coef": 1.2,
            "breakeven_cents": 2_000_000,
            "excess_rate": 0.4,
            "work_days": 0,
        },
        0,
    )
    assert got["base_pay"] == 360_000
    assert got["breakeven"] == 2_000_000
    assert got["excess_bonus"] == 200_000
    assert got["penalty"] == 0
    assert got["final_pay"] == 560_000


def test_advisor_below_breakeven_gets_no_bonus():
    got = engine.calc_advisor_salary(
        1_000_000,
        {"base_pay_cents": 300_000, "base_coef": 1.0, "breakeven_cents": 2_000_000,
         "excess_rate": 0.7},
        0,
    )
    assert got["excess_bonus"] == 0
    assert got["final_pay"] == 300_000


def test_advisor_retained_net_deducts_co_pool():
    # 顾问店先扣掉 50% 合办提成包再比平衡点
    assert engine.advisor_retained_net(1_000_000, 200_000) == 800_000
    assert engine.advisor_retained_net(1_000_000, 0) == 1_000_000


# ---------------------------------------------------------------------------
# 第六段：接线口径（第四阶段四项裁决）
# ---------------------------------------------------------------------------


def test_core_biz_breakdown_mobile_counts_four_categories():
    counts = {
        "v5_m1": 3,
        "v5_m2": 2,
        "v5_m5": 4,
        "v5_m6": 1,
        "v5_m7": 5,
        # 以下全部不进分子
        "v5_m3": 9,
        "v5_m4": 9,
        "v5_m8": 9,
        "v5_m9": 9,
        "v5_m10": 9,
        "phone_sales": 100,
    }
    got = engine.core_biz_breakdown(counts, "MOBILE")
    assert got == {"cat1_fusion": 3, "cat2_contract": 2, "cat3_ai": 5, "cat4_rebate": 5}
    assert engine.core_biz_count(counts, "MOBILE") == 15


def test_core_biz_breakdown_telecom_uses_three_smart_home_items():
    counts = {
        "v5_t1": 1,
        "v5_t2": 2,
        "v5_t5": 3,
        "v5_t6": 4,
        "v5_t8": 5,
        "v5_t3": 9,
        "v5_t4": 9,
        "v5_t7": 9,
        "v5_m1": 9,  # 移动科目不串到电信
    }
    got = engine.core_biz_breakdown(counts, "TELECOM")
    assert got["cat1_fusion"] == 1
    assert got["cat2_contract"] == 2
    assert got["cat3_ai"] == 12
    assert got["cat4_rebate"] == 0  # 电信无顺差让利科目
    assert engine.core_biz_count(counts, "TELECOM") == 15


@pytest.mark.parametrize("scope", ["", "  ", "UNKNOWN", "mobile5"])
def test_core_biz_breakdown_unknown_carrier_is_zero(scope):
    assert engine.core_biz_count({"v5_m1": 9, "v5_t1": 9}, scope) == 0


def test_core_biz_breakdown_negative_counts_floor_at_zero():
    counts = {"v5_m1": -3, "v5_m2": 2, "v5_m5": None}
    assert engine.core_biz_breakdown(counts, "MOBILE")["cat1_fusion"] == 0
    assert engine.core_biz_count(counts, "MOBILE") == 2


def _core_codes():
    return {
        code
        for cats in engine.CORE_BIZ_CATEGORIES.values()
        for codes in cats.values()
        for code in codes
    }


def test_core_biz_excluded_never_overlaps_categories():
    assert _core_codes().isdisjoint(engine.CORE_BIZ_EXCLUDED)


def test_core_biz_codes_all_exist_in_metrics_catalog():
    assert _core_codes() <= set(v5_metric_codes())


def test_rebate_metric_code_is_in_core_biz():
    # 顺差让利既算单笔提成、又算搭载主件 cat4，两处必须同源
    assert engine.REBATE_METRIC_CODE in _core_codes()


@pytest.mark.parametrize("month", ["2026-10", "2026-11", "2026-12", "2026-12-31", "2026-01", ""])
def test_clawback_exempt_through_2026_12(month):
    assert engine.rebate_recovery_exempt(month) is True
    assert engine.rebate_clawback_due(month, 100_000) == 0


@pytest.mark.parametrize("month", ["2027-01", "2027-01-15", "2028-06"])
def test_clawback_starts_2027_01(month):
    assert engine.rebate_recovery_exempt(month) is False
    assert engine.rebate_clawback_due(month, 100_000) == 100_000


def test_clawback_negative_outstanding_floors_at_zero():
    assert engine.rebate_clawback_due("2027-01", -50_000) == 0


@pytest.mark.parametrize(
    "mode", ["ADVISOR_A", "ADVISOR_B_NEAR", "NO_ADVISOR_MAIN", "AFFILIATED", "TELECOM_SINGLE"]
)
def test_settlement_applies_for_fourteen_business_units(mode):
    assert engine.settlement_applies(mode, "2026-11") is True


@pytest.mark.parametrize(
    "mode", ["TAIZHOU_ADVANCE_ONLY", "FRANCHISE_DISABLED", "SAMPLE_DISABLED", "UNSET", ""]
)
def test_settlement_skips_taizhou_and_disabled_modes(mode):
    assert engine.settlement_applies(mode, "2026-11") is False


@pytest.mark.parametrize("month", ["2026-10", "2026-09", "", "garbage"])
def test_settlement_never_runs_before_v5_start_month(month):
    assert engine.settlement_applies("NO_ADVISOR_MAIN", month) is False


def test_rebate_prices_match_commission_tiers():
    base, bulk = engine.rebate_prices(engine.ROLE_SHOP_FULL_100)
    assert (base, bulk) == (1000, 2000)
    half_base, half_bulk = engine.rebate_prices(engine.ROLE_ADVISOR_SHOP_50)
    assert (half_base, half_bulk) == (500, 1000)
    # 10 笔基础价 + 1 笔达量价，与 calc_rebate_commission 对得上
    assert engine.calc_rebate_commission(11, engine.ROLE_SHOP_FULL_100) == 10 * base + bulk


def test_rebate_prices_unknown_role_raises():
    with pytest.raises(ValueError):
        engine.rebate_prices("PARENT_ASSIST_50")
