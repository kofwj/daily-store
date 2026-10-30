"""V5 算账引擎纯函数（第四阶段内核）。

定位：只做算术，不碰数据库、不碰 Flask、不写任何表。
输入纯数据（分 / 笔 / 比例），输出纯数据，便于穷举单元测试。
接线（读日报事实、写表 3 / 表 4、锁月审计）留给后续 `db_v5` / `views_settlement`。

口径来源：第四阶段《V5 算账引擎与风控核算详细设计规范》，
阈值默认值取 `app/v5_baseline.py`（表 1 配置项的同名默认）。
金额一律「分」为单位的 int，比例一律 float；
四舍五入一律走 `_round_cents`（ROUND_HALF_UP，不用内置 round 的银行家舍入）。

边界语义（规范《三》E1–E6）：
- 基线缺失（≤0）视为「未配置」：不熔断提成、指标 1 计 0 分，交管理员补配置；
- 手机销量为 0：搭载率计 0，绝不抛 ZeroDivisionError；
- 净酬金为负（运营商跨期追罚）：达成率按负数进入各档，一律落到最低档；
- 追回款只在最终总包处冲减，绝不进入 `total_deduction_cents`，不污染指标 3A。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Mapping, Tuple

from .v5_baseline import (
    ADVISOR_CAP_ABS_CENTS,
    CARRIER_CARRY_TIERS,
    DEFAULT_CAP_RATIO,
    DISABLED_MODES,
    MODE_TAIZHOU_ADVANCE_ONLY,
    V5_START_MONTH,
    month_key,
    normalize_mode,
    v5_month_enabled,
)

# —— 表 3 role_share 枚举（与迁移 17 DDL 注释逐字一致）——
ROLE_SHOP_FULL_100 = "SHOP_FULL_100"        # 无顾问主力店 / 独立店：100% 提成
ROLE_ADVISOR_SHOP_50 = "ADVISOR_SHOP_50"    # 有顾问店的导购：50% 合办
ROLE_AFFILIATED_SHOP_50 = "AFFILIATED_SHOP_50"  # 挂靠小店：50% 合办
ROLE_SHARES: Tuple[str, ...] = (
    ROLE_SHOP_FULL_100,
    ROLE_ADVISOR_SHOP_50,
    ROLE_AFFILIATED_SHOP_50,
)
HALF_SHARES: Tuple[str, ...] = (ROLE_ADVISOR_SHOP_50, ROLE_AFFILIATED_SHOP_50)

# —— B5 顺差让利单店分档（单店算自己的，绝不跟随母店跳点）——
# 顺差让利科目代码：对接清单里写作 v5_m_rebate，落库实际是 v5_m7（metrics_seed）。
REBATE_METRIC_CODE = "v5_m7"
REBATE_BULK_THRESHOLD = 10      # 第 11 笔起进达量档
REBATE_BASE_CENTS = 1000        # 100% 店：前 10 笔 10 元/笔
REBATE_BULK_CENTS = 2000        # 100% 店：第 11 笔起 20 元/笔
REBATE_HALF_BASE_CENTS = 500    # 50% 合办：前 10 笔 5 元/笔
REBATE_HALF_BULK_CENTS = 1000   # 50% 合办：第 11 笔起 10 元/笔

# —— 第一道闸：申报量相对结算单实结量的正向容忍度 ——
MISREPORT_TOLERANCE = 1.2

# —— B8 全店防下滑熔断 ——
NET_FULL_LINE = 0.70            # 保底率 ≥ 70%：全额
NET_FUSE_LINE = 0.50            # 保底率 < 50%：全店提成熔断归零
PAYOUT_RATIO_MID = 0.8          # 50%–70%：8 折

# —— 指标满分值与档位 ——
SCORE_1_MAX = 60.0              # 指标 1 净酬金达成率
SCORE_2_MAX = 25.0              # 指标 2 搭载率方案 A
SCORE_2_PASS = 18.0             # 达标及格线
SCORE_2_FLOOR = 10.0            # 底线
SCORE_3A_MAX = 10.0             # 指标 3A 结算单扣款率
SCORE_3B_MAX = 5.0              # 指标 3B 四项红线
TOTAL_SCORE_MAX = 100.0
MGR_RATIO_STANDARD = 0.20       # 店长月考折算
MGR_RATIO_TAIZHOU = 0.15        # 泰州或特定店折算

# 指标 1 达成率低于满线但未熔断时的分数折扣
NET_SCORE_DISCOUNT = 0.8
# 指标 2 及格线 / 底线相对满分阈值的比例（移动 20%→15%/10%，电信 12%→9%/6%）
CARRY_PASS_FACTOR = 0.75
CARRY_FLOOR_FACTOR = 0.5

# 指标 3A 扣款率分档上限
DEDUCT_RATE_TIGHT = 0.005       # ≤0.5% → 10 分
DEDUCT_RATE_MID = 0.010         # ≤1.0% → 8 分
DEDUCT_RATE_LOOSE = 0.020       # ≤2.0% → 5 分，再高 0 分
DEDUCT_BROADBAND_PENALTY = 2.0  # 宽带扣 0.1 星级系数：再减 2 分

# —— 顾问薪酬 ——
ADVISOR_3B_SHARE = 0.5          # 3B 违规连带责任 50%
DEFAULT_MONTH_DAYS = 30


def _round_cents(value: float) -> int:
    """浮点转分，四舍五入（half-up），与 SQLite `CAST(ROUND(..))` 口径一致。"""
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _round_score(value: float) -> float:
    """分数保留两位，避免浮点尾巴写库。"""
    return round(value + 0.0, 2)


# ---------------------------------------------------------------------------
# 第一步 / 第二步：核减与分档
# ---------------------------------------------------------------------------


def rebate_prices(role_share: str) -> Tuple[int, int]:
    """顺差让利单价 (基础价, 达量价)，单位分；未知角色抛 ValueError。

    分档与单价同源：表 3 的 `unit_price_cents` 记基础价，达量差额已体现在金额里。
    """
    if role_share not in ROLE_SHARES:
        raise ValueError(f"未知分成角色：{role_share}")
    if role_share in HALF_SHARES:
        return REBATE_HALF_BASE_CENTS, REBATE_HALF_BULK_CENTS
    return REBATE_BASE_CENTS, REBATE_BULK_CENTS


def calc_rebate_commission(count: int, role_share: str) -> int:
    """顺差让利单店分档提成（分）。

    严格按本店当月核定笔数分档：前 10 笔基础价、第 11 笔起达量价，
    不跟随母店工号总量跳点。`count` 为核减后的有效笔数 N。
    """
    base, bulk = rebate_prices(role_share)
    n = max(int(count), 0)
    return min(n, REBATE_BULK_THRESHOLD) * base + max(0, n - REBATE_BULK_THRESHOLD) * bulk
def reconcile_carrier_counts(
    parent_store_id: int,
    carrier_settled_count: int,
    reports_by_store: Mapping[int, int],
) -> Dict[int, int]:
    """第一道闸：母店工号合并申报量与结算单实结量交叉比对，返回核定有效笔数。

    申报总量 ≤ 实结量 × 1.2 全量通过；超标则按各自申报量同比例核减到
    实结量 × 1.2 的封顶线（母店与挂靠子店共用同一个缩减比例）。

    母店键一定出现在返回值里（子店联动读取母店行的比对状态，母店留空会导致整组挂起）。
    """
    counts: Dict[int, int] = {int(sid): max(int(c), 0) for sid, c in reports_by_store.items()}
    counts.setdefault(int(parent_store_id), 0)

    total = sum(counts.values())
    settled = max(int(carrier_settled_count), 0)
    limit = settled * MISREPORT_TOLERANCE
    if total <= limit:
        return counts

    ratio = limit / total
    return {sid: _round_cents(c * ratio) for sid, c in counts.items()}


# ---------------------------------------------------------------------------
# 第三 / 第四步：熔断、封顶与实发总包
# ---------------------------------------------------------------------------


def shop_payout_ratio(
    net_commission_cents: int,
    net_baseline_cents: int,
    exempt_cross_month: bool = False,
) -> float:
    """B8 全店防下滑熔断三档系数（1.0 / 0.8 / 0.0）。

    基线未配置（≤0）时不熔断，按 1.0 放行；跨期异常经管理员豁免强制回 1.0。
    """
    if exempt_cross_month:
        return 1.0
    baseline = int(net_baseline_cents)
    if baseline <= 0:
        return 1.0
    rate = int(net_commission_cents) / baseline
    if rate >= NET_FULL_LINE:
        return 1.0
    if rate >= NET_FUSE_LINE:
        return PAYOUT_RATIO_MID
    return 0.0


def cap_raw_pool(
    raw_pool_cents: int,
    net_commission_cents: int,
    cap_ratio: float = DEFAULT_CAP_RATIO,
    cap_abs_cents: int = 0,
) -> int:
    """折前提成总包封顶（分）：不超净酬金 × cap_ratio，顾问店再受绝对封顶。

    两条封顶同时给时取更小值；比例 ≤0 视为不启用该条；绝对封顶 0 视为不启用。
    """
    raw = max(int(raw_pool_cents), 0)
    caps = []
    if float(cap_ratio) > 0:
        caps.append(_round_cents(max(int(net_commission_cents), 0) * float(cap_ratio)))
    if int(cap_abs_cents) > 0:
        caps.append(int(cap_abs_cents))
    if not caps:
        return raw
    return min([raw, *caps])


def shop_final_pool(
    raw_pool_cents: int,
    payout_ratio: float,
    clawback_cents: int = 0,
) -> int:
    """实发提成总包（分）= max(0, ROUND(折前总包 × 系数) − 让利追回款)。

    追回款只在这里冲减，绝不并入 `total_deduction_cents`。
    """
    ratio = float(payout_ratio)
    if ratio < 0:
        ratio = 0.0
    elif ratio > 1:
        ratio = 1.0
    after = _round_cents(max(int(raw_pool_cents), 0) * ratio)
    return max(0, after - max(int(clawback_cents), 0))


# ---------------------------------------------------------------------------
# 第四步：店长 100 分考核
# ---------------------------------------------------------------------------


def calc_manager_scores(
    net_commission_cents: int,
    baseline_cents: int,
    core_biz_count: int,
    phone_sales: int,
    target_carry_rate: float,
    tiers: Tuple[float, float, float],
    gross_commission_cents: int,
    total_deduction_cents: int,
    deduct_3b_total_cents: int,
    broadband_star_penalty: bool = False,
) -> Dict[str, float]:
    """店长 100 分考核：指标 1(60) + 指标 2(25) + 指标 3A(10) + 指标 3B(5)。

    `tiers` 为表 1 三档搭载率 `(满分, 及格, 底线)`；某档传 0 则由
    `target_carry_rate` 或满分阈值推导（及格 0.75×、底线 0.5×），
    因此移动 (0.20/0.15/0.10) 与电信 (0.12/0.09/0.06) 都能直接喂表 1 配置。
    `deduct_3b_total_cents` 为四项违规扣款合计，任一项 >0 即红线一票否决。
    """
    return {
        **_score_1(net_commission_cents, baseline_cents),
        **_score_2(core_biz_count, phone_sales, target_carry_rate, tiers),
        **_score_3a(gross_commission_cents, total_deduction_cents, broadband_star_penalty),
        **_score_3b(deduct_3b_total_cents),
        **_score_total(net_commission_cents, baseline_cents, core_biz_count, phone_sales,
                       target_carry_rate, tiers, gross_commission_cents,
                       total_deduction_cents, deduct_3b_total_cents, broadband_star_penalty),
    }


def _score_1(net_commission_cents: int, baseline_cents: int) -> Dict[str, float]:
    baseline = int(baseline_cents)
    rate = 0.0 if baseline <= 0 else int(net_commission_cents) / baseline
    if rate >= 1.0:
        score = SCORE_1_MAX
    elif rate >= NET_FULL_LINE:
        score = rate * SCORE_1_MAX
    elif rate >= NET_FUSE_LINE:
        score = rate * SCORE_1_MAX * NET_SCORE_DISCOUNT
    else:
        score = 0.0
    return {"score_1": _round_score(score), "net_rate": _round_score(rate)}


def _carry_lines(
    target_carry_rate: float,
    tiers: Tuple[float, float, float],
) -> Tuple[float, float, float]:
    """三档搭载率阈值 (满分, 及格, 底线)，缺省项由满分阈值推导。

    阈值统一抹到 6 位小数：0.20 × 0.75 的浮点尾巴是 0.15000000000000002，
    否则「正好 15%」会被判成不及格（E5 类边界）。
    """
    full = float(target_carry_rate) if float(target_carry_rate) > 0 else float(tiers[0])
    if full <= 0:
        return 0.0, 0.0, 0.0
    mid = float(tiers[1]) if float(tiers[1]) > 0 else full * CARRY_PASS_FACTOR
    floor = float(tiers[2]) if float(tiers[2]) > 0 else full * CARRY_FLOOR_FACTOR
    return round(full, 6), round(mid, 6), round(floor, 6)


def _score_2(
    core_biz_count: int,
    phone_sales: int,
    target_carry_rate: float,
    tiers: Tuple[float, float, float],
) -> Dict[str, float]:
    phones = max(int(phone_sales), 0)
    cores = max(int(core_biz_count), 0)
    rate = 0.0 if phones <= 0 else cores / phones
    full, mid, floor = _carry_lines(target_carry_rate, tiers)
    if rate >= full > 0:
        score = SCORE_2_MAX
    elif rate >= mid > 0:
        score = SCORE_2_PASS
    elif rate >= floor > 0:
        score = SCORE_2_FLOOR
    else:
        score = 0.0
    return {"score_2": _round_score(score), "actual_carry_rate": _round_score(rate)}


def _score_3a(
    gross_commission_cents: int,
    total_deduction_cents: int,
    broadband_star_penalty: bool,
) -> Dict[str, float]:
    gross = int(gross_commission_cents)
    deduct = max(int(total_deduction_cents), 0)
    if deduct <= 0:
        rate = 0.0
    elif gross > 0:
        rate = deduct / gross
    else:  # 毛酬金缺失但确有扣款：按满扣处理，避免除零
        rate = 1.0

    if rate <= DEDUCT_RATE_TIGHT:
        score = SCORE_3A_MAX
    elif rate <= DEDUCT_RATE_MID:
        score = 8.0
    elif rate <= DEDUCT_RATE_LOOSE:
        score = 5.0
    else:
        score = 0.0
    if broadband_star_penalty:
        score = max(0.0, score - DEDUCT_BROADBAND_PENALTY)
    return {"score_3a": _round_score(score), "deduct_rate": _round_score(rate)}


def _score_3b(deduct_3b_total_cents: int) -> Dict[str, float]:
    hit = max(int(deduct_3b_total_cents), 0) > 0
    return {"score_3b": 0.0 if hit else SCORE_3B_MAX, "redline_hit": 1.0 if hit else 0.0}


def _score_total(
    net_commission_cents: int,
    baseline_cents: int,
    core_biz_count: int,
    phone_sales: int,
    target_carry_rate: float,
    tiers: Tuple[float, float, float],
    gross_commission_cents: int,
    total_deduction_cents: int,
    deduct_3b_total_cents: int,
    broadband_star_penalty: bool,
) -> Dict[str, float]:
    total = (
        _score_1(net_commission_cents, baseline_cents)["score_1"]
        + _score_2(core_biz_count, phone_sales, target_carry_rate, tiers)["score_2"]
        + _score_3a(gross_commission_cents, total_deduction_cents, broadband_star_penalty)[
            "score_3a"
        ]
        + _score_3b(deduct_3b_total_cents)["score_3b"]
    )
    total = min(max(total, 0.0), TOTAL_SCORE_MAX)
    return {
        "total_100": _round_score(total),
        "mgr_score_20": _round_score(total * MGR_RATIO_STANDARD),
        "mgr_score_15": _round_score(total * MGR_RATIO_TAIZHOU),
    }


def carry_tiers_for(carrier_scope: str) -> Tuple[float, float, float]:
    """按运营商范围取表 1 三档搭载率（移动 20/15/10，电信 12/9/6）。"""
    return CARRIER_CARRY_TIERS.get((carrier_scope or "").strip().upper(), (0.0, 0.0, 0.0))


def cap_abs_for(v5_mode: str) -> int:
    """顾问店单笔提成总包绝对封顶（分）：只有专职顾问店才有 650 元封顶。"""
    if v5_mode in ("ADVISOR_A", "ADVISOR_B_NEAR"):
        return ADVISOR_CAP_ABS_CENTS
    return 0


# ---------------------------------------------------------------------------
# 第五步：专职顾问薪酬（任期按天折算）
# ---------------------------------------------------------------------------


def tenure_coef(work_days: int, month_days: int = DEFAULT_MONTH_DAYS) -> float:
    """任期折算比 k：整月（work_days ≤0 或 ≥当月天数）记 1.0，否则按天折算。"""
    days = int(month_days)
    if days <= 0:
        return 1.0
    worked = int(work_days)
    if worked <= 0 or worked >= days:
        return 1.0
    return worked / days


def calc_advisor_salary(
    retained_net_cents: int,
    tenure_config: Mapping[str, Any],
    deduct_3b_total_cents: int,
    month_days: int = DEFAULT_MONTH_DAYS,
) -> Dict[str, int]:
    """专职顾问当月薪酬（分）：折后底薪 + 净超额分成 − 3B 连带扣款。

    `tenure_config` 取 `advisor_tenure` 一行：`base_pay_cents` / `base_coef` /
    `breakeven_cents` / `excess_rate` / `work_days`。
    `retained_net_cents` 由调用方先行扣除 50% 合办提成包（`co_pool_deducted_cents`），
    即 advisor_retained_net_cents = net_commission_cents − co_pool_deducted_cents。
    """
    coef = tenure_coef(int(tenure_config.get("work_days") or 0), month_days)
    base_coef = float(tenure_config.get("base_coef") or 1.0)
    excess_rate = float(tenure_config.get("excess_rate") or 0.0)

    base_pay = _round_cents(int(tenure_config.get("base_pay_cents") or 0) * base_coef * coef)
    breakeven = _round_cents(int(tenure_config.get("breakeven_cents") or 0) * coef)
    excess = max(0, max(int(retained_net_cents), 0) - breakeven)
    bonus = _round_cents(excess * excess_rate)
    penalty = _round_cents(max(int(deduct_3b_total_cents), 0) * ADVISOR_3B_SHARE)
    return {
        "base_pay": base_pay,
        "breakeven": breakeven,
        "excess_bonus": bonus,
        "penalty": penalty,
        "final_pay": base_pay + bonus - penalty,
    }


def advisor_retained_net(net_commission_cents: int, co_pool_deducted_cents: int) -> int:
    """顾问店留存净酬金（分）：先扣掉 50% 合办提成包再进顾问考核。"""
    return int(net_commission_cents) - max(int(co_pool_deducted_cents), 0)


# ---------------------------------------------------------------------------
# 第六段：接线口径（第四阶段四项裁决的显式固化）
# ---------------------------------------------------------------------------

# 裁决 1：指标 2 分子 = 运营商结算单「四大核心随销主件」4 类 5 项。
# 移动：cat1 融合 v5_m1 / cat2 合约 v5_m2 / cat3 AI 手机 v5_m5+v5_m6 / cat4 让利 v5_m7。
# 电信：cat1 v5_t1 / cat2 v5_t2 / cat3 全屋智能主件 v5_t5+v5_t6+v5_t8；cat4 无对应科目
#      （电信不跑顺差让利垫资，表 4 该列留 0）。
# 坚决不计入：v5_m8 优选会员（0 元义务项）、v5_m3 宽带、v5_m4 升档、v5_m9 智家、v5_m10 权益；
#              电信 v5_t3 宽带、v5_t4 升档、v5_t7 权益。
CORE_CATEGORY_KEYS: Tuple[str, ...] = ("cat1_fusion", "cat2_contract", "cat3_ai", "cat4_rebate")
CORE_BIZ_CATEGORIES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "MOBILE": {
        "cat1_fusion": ("v5_m1",),
        "cat2_contract": ("v5_m2",),
        "cat3_ai": ("v5_m5", "v5_m6"),
        "cat4_rebate": ("v5_m7",),
    },
    "TELECOM": {
        "cat1_fusion": ("v5_t1",),
        "cat2_contract": ("v5_t2",),
        "cat3_ai": ("v5_t5", "v5_t6", "v5_t8"),
        "cat4_rebate": (),
    },
}
# 只做留痕/存量维系、不算搭载主件的科目：写库时这些列不进 core_biz_count。
CORE_BIZ_EXCLUDED: Tuple[str, ...] = (
    "v5_m3",
    "v5_m4",
    "v5_m8",
    "v5_m9",
    "v5_m10",
    "v5_t3",
    "v5_t4",
    "v5_t7",
)

# 裁决 2/3：追回款独立冲减提成总包，绝不进负向扣款、绝不污染 3A；
# 2026-10 ~ 2026-12 历史豁免，2027-01 起按 advance_posts 超期未回款滚动追缴。
# 顺差让利垫资账龄 T+3：2026-10 起产生的新垫资到 2027-01 才满 3 个月超期点。
CLAWBACK_EXEMPT_LAST_MONTH = "2026-12"
CLAWBACK_START_MONTH = "2027-01"

# 裁决 4：只有 14 个经营主体（南通移动 13 + 电信 1）进表 3/表 4；
# 泰州垫资店只做预存、不配考核基线，不进 V5 提成与店长分。
SETTLEMENT_EXCLUDED_MODES: Tuple[str, ...] = (MODE_TAIZHOU_ADVANCE_ONLY,) + DISABLED_MODES


def core_biz_breakdown(
    counts: Mapping[str, int], carrier_scope: str = "MOBILE"
) -> Dict[str, int]:
    """按 cat1–cat4 汇总核心随销主件（笔/户/台），负数按 0 计。

    认不出的运营商返回全 0：宁可指标 2 计 0 分，也不猜口径。
    """
    scope = (carrier_scope or "").strip().upper()
    cats = CORE_BIZ_CATEGORIES.get(scope, {})
    return {
        key: sum(max(int(counts.get(code) or 0), 0) for code in cats.get(key, ()))
        for key in CORE_CATEGORY_KEYS
    }


def core_biz_count(counts: Mapping[str, int], carrier_scope: str = "MOBILE") -> int:
    """指标 2 分子：4 类核心随销主件之和（与表 4 `core_biz_count` 列同义）。"""
    return sum(core_biz_breakdown(counts, carrier_scope).values())


def rebate_recovery_exempt(month: Any) -> bool:
    """追回款是否处于历史豁免期：2026-12 及以前一律豁免（含全部双轨历史月）。"""
    key = month_key(month)
    if not key:
        return True  # 认不出的月份按豁免处理：绝不误扣真金白银
    return key <= CLAWBACK_EXEMPT_LAST_MONTH


def rebate_clawback_due(month: Any, outstanding_cents: int) -> int:
    """当期应追缴的垫资本金（分）：豁免期恒 0；2027-01 起按超期未回款全额滚入。"""
    if rebate_recovery_exempt(month):
        return 0
    return max(int(outstanding_cents), 0)


def settlement_applies(v5_mode: str, month: Any = V5_START_MONTH) -> bool:
    """该店该月是否生成表 3 / 表 4 与店长分：V5 生效月 + 非泰州垫资/非停用模式。

    模式先过 `normalize_mode`：空串、拼写错误的模式一律当「未纳入 V5」挡在门外，
    宁可少算一家等管理员补配置，也绝不给错模式的门店出账。
    """
    if not v5_month_enabled(month):
        return False
    return normalize_mode(v5_mode) not in SETTLEMENT_EXCLUDED_MODES
