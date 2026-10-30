"""V5 月结数据存取层（第四阶段接线）。

职责边界：
- 读：日报事实（daily_facts）、表 1 基线、表 2 顾问任期、表 4 已录入的结算单数据；
- 算：全部公式调用 `v5_engine` 纯函数，本模块自己不做任何业务算术；
- 写：表 3（单笔提成明细）、表 4（月结大盘）、表 6（锁月与配置审计）。

三条铁律：
- 只对 2026-11 及以后生成月结，历史月份一个字节都不动（`v5_month_enabled` 门禁）；
- 定位门店一律用主键 id 取自 `settlement_stores()` 的查询结果，不按 id 区间猜；
- 锁月后任何重算/录入一律拒绝，解锁必须留审计。

口径来源：第四阶段《V5 算账引擎与风控核算详细设计规范》＋四项裁决
（见 `v5_engine` 第六段常量：核心随销主件 4 类 5 项、追回款 2027-01 起、
泰州垫资店不进表 3/表 4）。
"""

from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import v5_engine as engine
from .db_core import _now
from .db_query import range_metric_totals
from .metrics_seed import v5_metric_codes
from .v5_baseline import (
    DEFAULT_CAP_RATIO,
    MODE_ADVISOR_A,
    MODE_ADVISOR_B_NEAR,
    MODE_AFFILIATED,
    carrier_for_mode,
    month_key,
    normalize_mode,
    v5_month_enabled,
)

STATUS_DRAFT = "DRAFT"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_LOCKED = "LOCKED"

# 顺差让利垫资本金：100 元/笔（对接清单「给顾客直降垫付的 100 元真金白银本金」）
REBATE_ADVANCE_UNIT_CENTS = 10_000
# 垫资账龄 T+3：满 3 个月仍未回款才进追缴池
ADVANCE_AGE_MONTHS = 3

# 搭载率分母：通用底座科目，与 V5 科目一起取月累计
PHONE_SALES_CODE = "phone_sales"

# 录入侧列：运营商月结单/垫资核对后由管理员手工录入，重算时原样回写不改。
OPERATOR_COLUMNS: Tuple[str, ...] = (
    "gross_commission_cents",
    "commissioning_fee_cents",
    "total_deduction_cents",
    "deduct_3b_quality_cents",
    "deduct_3b_dispute_cents",
    "deduct_3b_workorder_cents",
    "deduct_3b_luckynum_cents",
    "broadband_star_penalty",
    "net_commission_cents",
    "settled_cat1_fusion",
    "settled_cat2_contract",
    "settled_cat3_ai",
    "settled_cat4_rebate",
    "settled_cat4_rebate_member",
    "exempt_cross_month",
    "override_remark",
)

# 表 6 审计动作
ACTION_COMPUTE = "COMPUTE"
ACTION_CONFIRM = "CONFIRM"
ACTION_LOCK = "LOCK"
ACTION_UNLOCK = "UNLOCK"
ACTION_INPUT = "INPUT"


def fact_codes() -> Tuple[str, ...]:
    """月累计要扫的科目：26 个 V5 科目 + 手机销量底座。"""
    return tuple(sorted({*v5_metric_codes(), PHONE_SALES_CODE}))


def month_bounds(month: Any) -> Tuple[str, str]:
    """'YYYY-MM' → (首日, 末日)，给 daily_facts 区间查询用。"""
    key = _require_month(month)
    year, mon = int(key[:4]), int(key[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{key}-01", f"{key}-{last:02d}"


def month_days(month: Any) -> int:
    """当月自然天数，顾问薪酬按天折算用。"""
    start, end = month_bounds(month)
    return date.fromisoformat(end).day


def _require_month(month: Any) -> str:
    key = month_key(month)
    if not key:
        raise ValueError(f"月份格式不对：{month!r}（应为 YYYY-MM）")
    return key


def _require_enabled_month(month: Any) -> str:
    """V5 生效月门禁：2026-10 及以前一律拒绝生成月结，不回溯、不重算。"""
    key = _require_month(month)
    if not v5_month_enabled(key):
        raise ValueError(f"V5 月结只对 2026-11 及以后月份生成；{key} 按原口径，不回溯不重算")
    return key


def shift_month(month: Any, delta: int) -> str:
    """月份加减：'2027-01' + (-3) → '2026-10'。"""
    key = _require_month(month)
    index = int(key[:4]) * 12 + (int(key[5:7]) - 1) + int(delta)
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


# ---------------------------------------------------------------------------
# 读：候选门店、表 1 基线、表 2 任期、结算单录入、垫资快照
# ---------------------------------------------------------------------------


def settlement_stores(conn: sqlite3.Connection, month: Any) -> List[sqlite3.Row]:
    """该月要出表的经营主体：V5 生效月 + 模式允许（泰州垫资店与停用模式一律排除）。"""
    key = _require_month(month)
    rows = list(conn.execute("SELECT * FROM stores WHERE active=1 ORDER BY sort_order, id"))
    return [row for row in rows if engine.settlement_applies(row["v5_mode"], key)]


def month_fact_totals(
    conn: sqlite3.Connection, month: Any, store_ids: Sequence[int]
) -> Dict[int, Dict[str, int]]:
    """各店该月 V5 科目 + 手机销量的月累计（没有日报的店返回全 0）。"""
    start, end = month_bounds(month)
    return range_metric_totals(
        conn,
        store_ids,
        date.fromisoformat(start),
        date.fromisoformat(end),
        fact_codes(),
    )


def baseline_row(conn: sqlite3.Connection, store_id: int, month: Any) -> Optional[sqlite3.Row]:
    """表 1：取该月生效的基线行（from_month ≤ 当月 ≤ to_month，空 to_month 表示长期）。"""
    key = _require_month(month)
    return conn.execute(
        """
        SELECT * FROM store_baseline
        WHERE store_id=? AND from_month<=? AND (to_month='' OR to_month>=?)
        ORDER BY from_month DESC, id DESC LIMIT 1
        """,
        (int(store_id), key, key),
    ).fetchone()


def tenure_row(conn: sqlite3.Connection, store_id: int, month: Any) -> Optional[sqlite3.Row]:
    """表 2：取该月生效的顾问任期行；换人多月并存时取最近一段。"""
    key = _require_month(month)
    return conn.execute(
        """
        SELECT * FROM advisor_tenure
        WHERE store_id=? AND from_month<=? AND (to_month='' OR to_month>=?)
        ORDER BY from_month DESC, id DESC LIMIT 1
        """,
        (int(store_id), key, key),
    ).fetchone()


def operator_input(conn: sqlite3.Connection, month: Any, store_id: int) -> Dict[str, Any]:
    """表 4 里已录入的结算单数据；没录过就返回全 0 骨架（不建行、不污染）。"""
    key = _require_month(month)
    row = conn.execute(
        "SELECT * FROM store_month_settlement WHERE month=? AND store_id=?",
        (key, int(store_id)),
    ).fetchone()
    data: Dict[str, Any] = {name: 0 for name in OPERATOR_COLUMNS}
    data["override_remark"] = ""
    if row is not None:
        for name in OPERATOR_COLUMNS:
            data[name] = row[name]
        data["status"] = row["status"]
    else:
        data["status"] = ""
    return data


def settlement_status(conn: sqlite3.Connection, month: Any, store_id: int) -> str:
    """该店该月的月结状态；没有行时返回空串（表示还没试算）。"""
    key = _require_month(month)
    row = conn.execute(
        "SELECT status FROM store_month_settlement WHERE month=? AND store_id=?",
        (key, int(store_id)),
    ).fetchone()
    return str(row["status"]) if row is not None else ""


def rebate_advance_snapshot(
    conn: sqlite3.Connection, store_id: int, month: Any
) -> Dict[str, Any]:
    """顺差让利垫资快照：累计垫资、已回款、满 3 个月账龄仍未回款的笔数与本金（分）。"""
    key = _require_month(month)
    _start, end = month_bounds(key)
    aging_end = month_bounds(shift_month(key, -ADVANCE_AGE_MONTHS))[1]
    row = conn.execute(
        """
        SELECT COALESCE(SUM(rebate), 0) AS total,
               COALESCE(SUM(CASE WHEN paid=1 THEN rebate ELSE 0 END), 0) AS recovered,
               COALESCE(SUM(CASE WHEN paid=0 AND biz_date<=? THEN rebate ELSE 0 END), 0)
                   AS overdue
        FROM advance_posts
        WHERE store_id=? AND biz_date<=?
        """,
        (aging_end, int(store_id), end),
    ).fetchone()
    total = int(row["total"] or 0)
    recovered = int(row["recovered"] or 0)
    overdue = int(row["overdue"] or 0)
    ratio = 1.0 if total <= 0 else round(recovered / total, 4)
    return {
        "advance_count": total,
        "recovered_count": recovered,
        "overdue_count": overdue,
        "advance_cents": total * REBATE_ADVANCE_UNIT_CENTS,
        "recovered_cents": recovered * REBATE_ADVANCE_UNIT_CENTS,
        "overdue_cents": overdue * REBATE_ADVANCE_UNIT_CENTS,
        "recovery_ratio": ratio,
    }


# ---------------------------------------------------------------------------
# 写：运营商月结录入（只动录入列，绝不动计算列）
# ---------------------------------------------------------------------------


def _to_int(value: Any) -> int:
    """表单/库里取整数分：空串、None、'12.6'、脏数据一律落到 0，绝不抛错。"""
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _uid(user_id: Any) -> Optional[int]:
    """责任人/审计外键：0 或空一律落 NULL（`users` 没有 id=0，落 0 会被 FK 直接拒写）。"""
    value = _to_int(user_id)
    return value if value > 0 else None


def _to_text(value: Any) -> str:
    return str(value or "").strip()


def save_operator_input(
    conn: sqlite3.Connection,
    month: Any,
    store_id: int,
    data: Mapping[str, Any],
    user_id: int = 0,
) -> Dict[str, Any]:
    """写入运营商月结录入列（毛酬金/调测费/扣款/3B 四项/实结大类/豁免）。

    只看 `OPERATOR_COLUMNS`：状态、快照、计算列一个都不碰；锁月后一律拒绝。
    录入不改任何计算列 —— 必须重新试算才生效，避免「改了数看旧结果」。
    """
    key = _require_enabled_month(month)
    if settlement_status(conn, key, store_id) == STATUS_LOCKED:
        raise ValueError("该店当月已锁月，先解锁才能改录入数据")
    before = operator_input(conn, key, store_id)
    values = {
        name: _to_text(data.get(name)) if name == "override_remark" else _to_int(data.get(name))
        for name in OPERATOR_COLUMNS
    }
    # 录入改了数就必须重新试算：状态一律退回 DRAFT（锁月行在上面已经拒绝，不会被踩）
    cols = ("month", "store_id", *OPERATOR_COLUMNS, "status", "updated_at", "updated_by")
    updates = ", ".join(
        f"{name}=excluded.{name}"
        for name in (*OPERATOR_COLUMNS, "status", "updated_at", "updated_by")
    )
    conn.execute(
        f"""
        INSERT INTO store_month_settlement ({", ".join(cols)})
        VALUES ({", ".join("?" * len(cols))})
        ON CONFLICT(month, store_id) DO UPDATE SET {updates}
        """,
        (key, int(store_id), *values.values(), STATUS_DRAFT, _now(), _uid(user_id)),
    )
    _audit(
        conn,
        key,
        store_id,
        ACTION_INPUT,
        user_id,
        before=json.dumps(before, ensure_ascii=False, sort_keys=True),
        after=json.dumps(values, ensure_ascii=False, sort_keys=True),
    )
    return values


def _audit(
    conn: sqlite3.Connection,
    month: str,
    store_id: int,
    action: str,
    user_id: int = 0,
    before: str = "",
    after: str = "",
    reason: str = "",
) -> None:
    """表 6：锁月、解锁、试算、录入的审计留痕（只追加，永不更新）。"""
    conn.execute(
        """
        INSERT INTO settlement_audit_log
            (month, store_id, action, user_id, created_at, before_json, after_json, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (str(month), int(store_id), action, _uid(user_id), _now(), before, after, reason),
    )


# ---------------------------------------------------------------------------
# 算：口径派生（角色、工号母店、指标 2 分子）
# ---------------------------------------------------------------------------


def role_share_for(v5_mode: str) -> str:
    """门店模式 → 表 3 分成角色：挂靠 50%、顾问店导购 50%、其余 100%。"""
    mode = normalize_mode(v5_mode)
    if mode == MODE_AFFILIATED:
        return engine.ROLE_AFFILIATED_SHOP_50
    if mode in (MODE_ADVISOR_A, MODE_ADVISOR_B_NEAR):
        return engine.ROLE_ADVISOR_SHOP_50
    return engine.ROLE_SHOP_FULL_100


def is_advisor_mode(v5_mode: str) -> bool:
    """专职顾问店：吃「底薪 + 净超额分成」，不吃单笔提成。"""
    return normalize_mode(v5_mode) in (MODE_ADVISOR_A, MODE_ADVISOR_B_NEAR)


def rebate_biz_code(v5_mode: str) -> str:
    """该模式有没有顺差让利单笔提成科目；电信无 cat4，返回空串。"""
    carrier = carrier_for_mode(normalize_mode(v5_mode))
    codes = engine.CORE_BIZ_CATEGORIES.get(carrier, {}).get("cat4_rebate", ())
    return engine.REBATE_METRIC_CODE if engine.REBATE_METRIC_CODE in codes else ""


def _root_of(stores: Mapping[int, Any], store_id: int) -> int:
    """沿 affiliated_to 上溯到工号所属母店；断链或成环回落自身，绝不无限循环。"""
    seen: set = set()
    current = int(store_id)
    while current not in seen:
        seen.add(current)
        row = stores.get(current)
        if row is None:
            return current
        parent = _to_int(row["affiliated_to"])
        if parent <= 0 or parent not in stores:
            return current
        current = parent
    return int(store_id)


def _core_biz_count(ctx: Mapping[str, Any]) -> int:
    """指标 2 分子：优先结算单四大类实结量，没录结算单就回落日报核心科目合计。

    结算单是运营商口径的唯一事实源；空串/未录入时才退回日报申报量。
    电信不跑顺差让利垫资，其 cat4 一律不计（引擎里本来就没有对应科目）。
    """
    oper = ctx["oper"]
    cats = engine.CORE_BIZ_CATEGORIES.get(str(ctx["carrier"]), {})
    settled = {name: _to_int(oper.get(f"settled_{name}")) for name in engine.CORE_CATEGORY_KEYS}
    if any(settled[name] > 0 for name, codes in cats.items() if codes):
        return sum(max(settled[name], 0) for name, codes in cats.items() if codes)
    return engine.core_biz_count(ctx["facts"], str(ctx["carrier"]))


# 表 4 `deduct_3b_*` 四列：任一项 > 0 即指标 3B 红线一票否决
DEDUCT_3B_COLUMNS: Tuple[str, ...] = (
    "deduct_3b_quality_cents",
    "deduct_3b_dispute_cents",
    "deduct_3b_workorder_cents",
    "deduct_3b_luckynum_cents",
)

# 表 4 `audit_flag` 取值（按优先级从上到下，只记最要紧的一条）
AUDIT_OK = "OK"
AUDIT_NET_NEGATIVE = "NET_NEGATIVE"
AUDIT_NO_SETTLEMENT = "NO_SETTLEMENT"
AUDIT_REBATE_TRIMMED = "REBATE_TRIMMED"
AUDIT_BASELINE_MISSING = "BASELINE_MISSING"
AUDIT_ADVISOR_MISSING = "ADVISOR_MISSING"


def _tenure_config(tenure: Any) -> Dict[str, Any]:
    """表 2 一行 → 引擎要的任期配置字典（缺失按 0 处理，不猜底薪）。"""
    if tenure is None:
        return {}
    return {
        "base_pay_cents": _to_int(tenure["base_pay_cents"]),
        "base_coef": float(tenure["base_coef"] or 0),
        "breakeven_cents": _to_int(tenure["breakeven_cents"]),
        "excess_rate": float(tenure["excess_rate"] or 0),
        "work_days": _to_int(tenure["work_days"]),
    }


def _carry_tiers(baseline: Any, carrier: str) -> Tuple[float, float, float]:
    """表 1 三档搭载率；没配（全 0）就退回运营商默认档。"""
    if baseline is not None:
        tiers = (
            float(baseline["carry_tier_25"] or 0),
            float(baseline["carry_tier_18"] or 0),
            float(baseline["carry_tier_10"] or 0),
        )
        if any(tier > 0 for tier in tiers):
            return tiers
    return engine.carry_tiers_for(carrier)


def _store_contexts(conn: sqlite3.Connection, month: Any) -> List[Dict[str, Any]]:
    """把该月要出表的门店连同基线、任期、录入、日报事实一次性读齐（只读，不写库）。"""
    key = _require_enabled_month(month)
    rows = settlement_stores(conn, key)
    if not rows:
        return []
    ids = [int(row["id"]) for row in rows]
    facts = month_fact_totals(conn, key, ids)
    stores = {int(row["id"]): row for row in conn.execute("SELECT * FROM stores")}
    contexts: List[Dict[str, Any]] = []
    for row in rows:
        store_id = int(row["id"])
        baseline = baseline_row(conn, store_id, key)
        mode = normalize_mode(
            baseline["v5_mode"] if baseline is not None else row["v5_mode"],
            fallback=normalize_mode(row["v5_mode"]),
        )
        contexts.append(
            {
                "store_id": store_id,
                "store_code": str(row["code"]),
                "store_name": str(row["name"]),
                "mode": mode,
                "carrier": carrier_for_mode(mode),
                "role_share": role_share_for(mode),
                "root": _root_of(stores, store_id),
                "baseline": baseline,
                "tenure": tenure_row(conn, store_id, key),
                "oper": operator_input(conn, key, store_id),
                "facts": facts.get(store_id, {}),
            }
        )
    return contexts


def _reconciled_counts(
    conn: sqlite3.Connection, month: str, contexts: Sequence[Mapping[str, Any]]
) -> Dict[int, Tuple[int, int, bool, bool]]:
    """第一道闸：按工号母店分组核减顺差让利笔数。

    返回 {store_id: (日报申报量, 核定有效笔数, 是否被核减, 是否缺结算单)}。
    母店没录结算单实结量（≤0）时不做核减，只打 `NO_SETTLEMENT` 标：
    子店绝不因为母店留空而被整组清零（对接清单明确点过这个坑）。
    """
    groups: Dict[int, List[Mapping[str, Any]]] = {}
    for ctx in contexts:
        groups.setdefault(int(ctx["root"]), []).append(ctx)
    code = engine.REBATE_METRIC_CODE
    out: Dict[int, Tuple[int, int, bool, bool]] = {}
    for root, members in groups.items():
        declared = {
            int(ctx["store_id"]): max(_to_int(ctx["facts"].get(code, 0)), 0) for ctx in members
        }
        settled = _to_int(operator_input(conn, month, root).get("settled_cat4_rebate"))
        effective = (
            engine.reconcile_carrier_counts(root, settled, declared)
            if settled > 0
            else dict(declared)
        )
        for store_id, count in declared.items():
            got = max(_to_int(effective.get(store_id, 0)), 0)
            out[store_id] = (count, got, got != count, settled <= 0 and count > 0)
    return out


# 表 4 试算/重算要写的列：录入列原样回写 + 快照 + 计算列（状态列另行处理）
SETTLEMENT_WRITE_COLUMNS: Tuple[str, ...] = (
    *OPERATOR_COLUMNS,
    "core_biz_count",
    "phone_sales_snap",
    "actual_carry_rate",
    "rebate_advance_cents",
    "rebate_recovered_cents",
    "rebate_recovery_ratio_3m",
    "rebate_recovery_exempt",
    "rebate_clawback_cents",
    "audit_flag",
    "v5_mode_snap",
    "net_baseline_snap_cents",
    "target_carry_rate_snap",
    "advisor_name_snap",
    "base_coef_snap",
    "breakeven_snap_cents",
    "excess_rate_snap",
    "shop_raw_pool_cents",
    "shop_payout_ratio",
    "shop_final_pool_cents",
    "co_pool_deducted_cents",
    "advisor_retained_net_cents",
    "score_1_net",
    "score_2_biz",
    "score_3a_deduct",
    "score_3b_redline",
    "total_score_100",
    "mgr_score_20",
    "mgr_score_15",
    "advisor_sys_coef",
    "advisor_base_pay_cents",
    "advisor_excess_bonus_cents",
    "advisor_3b_penalty_cents",
    "advisor_final_pay_cents",
)


def _compute_store(
    conn: sqlite3.Connection,
    month: str,
    ctx: Mapping[str, Any],
    counts: Tuple[int, int, bool, bool],
) -> Dict[str, Any]:
    """单店一行表 4 + 表 3 明细：全部算术都调引擎，本函数只负责取数与拼装。"""
    daily_count, settled_count, trimmed, missing_settlement = counts
    oper = ctx["oper"]
    baseline = ctx["baseline"]
    tenure = ctx["tenure"]
    mode = str(ctx["mode"])
    carrier = str(ctx["carrier"])
    store_id = int(ctx["store_id"])
    advisor = is_advisor_mode(mode)

    net = _to_int(oper.get("net_commission_cents"))
    gross = _to_int(oper.get("gross_commission_cents"))
    deduction = _to_int(oper.get("total_deduction_cents"))
    deduct_3b = sum(_to_int(oper.get(name)) for name in DEDUCT_3B_COLUMNS)
    broadband = _to_int(oper.get("broadband_star_penalty")) > 0
    exempt = _to_int(oper.get("exempt_cross_month")) > 0

    baseline_net = _to_int(baseline["net_baseline_cents"]) if baseline is not None else 0
    cap_ratio = float(baseline["cap_ratio"] or 0) if baseline is not None else DEFAULT_CAP_RATIO
    cap_abs = max(
        _to_int(baseline["cap_abs_cents"]) if baseline is not None else 0,
        engine.cap_abs_for(mode),
    )
    tiers = _carry_tiers(baseline, carrier)
    target = float(baseline["target_carry_rate"] or 0) if baseline is not None else 0.0
    if target <= 0:
        target = tiers[0]
    phone_sales = max(_to_int(ctx["facts"].get(PHONE_SALES_CODE, 0)), 0)
    core_biz = _core_biz_count(ctx)

    scores = engine.calc_manager_scores(
        net_commission_cents=net,
        baseline_cents=baseline_net,
        core_biz_count=core_biz,
        phone_sales=phone_sales,
        target_carry_rate=target,
        tiers=tiers,
        gross_commission_cents=gross,
        total_deduction_cents=deduction,
        deduct_3b_total_cents=deduct_3b,
        broadband_star_penalty=broadband,
    )

    biz_code = rebate_biz_code(mode)
    unit_price, _bulk_price = engine.rebate_prices(str(ctx["role_share"])) if biz_code else (0, 0)
    raw_amount = (
        engine.calc_rebate_commission(settled_count, str(ctx["role_share"])) if biz_code else 0
    )
    raw_pool = engine.cap_raw_pool(raw_amount, net, cap_ratio, cap_abs)
    payout_ratio = engine.shop_payout_ratio(net, baseline_net, exempt)
    advance = rebate_advance_snapshot(conn, store_id, month)
    clawback = engine.rebate_clawback_due(month, advance["overdue_cents"])
    final_pool = engine.shop_final_pool(raw_pool, payout_ratio, clawback)

    # 顾问店：50% 合办包先出大盘，再拿留存净酬金跟平衡点比超额
    co_pool = raw_pool if advisor else 0
    retained = engine.advisor_retained_net(net, co_pool) if advisor else 0
    pay = (
        engine.calc_advisor_salary(retained, _tenure_config(tenure), deduct_3b, month_days(month))
        if advisor and tenure is not None
        else None
    )

    if net < 0:
        flag = AUDIT_NET_NEGATIVE
    elif missing_settlement:
        flag = AUDIT_NO_SETTLEMENT
    elif trimmed:
        flag = AUDIT_REBATE_TRIMMED
    elif baseline_net <= 0:
        flag = AUDIT_BASELINE_MISSING
    elif advisor and tenure is None:
        flag = AUDIT_ADVISOR_MISSING
    else:
        flag = AUDIT_OK

    row: Dict[str, Any] = {
        "month": month,
        "store_id": store_id,
        "store_code": str(ctx["store_code"]),
        "store_name": str(ctx["store_name"]),
        "v5_mode": mode,
        **{name: oper.get(name) for name in OPERATOR_COLUMNS},
        "core_biz_count": core_biz,
        "phone_sales_snap": phone_sales,
        "actual_carry_rate": scores["actual_carry_rate"],
        "rebate_advance_cents": advance["advance_cents"],
        "rebate_recovered_cents": advance["recovered_cents"],
        "rebate_recovery_ratio_3m": advance["recovery_ratio"],
        "rebate_recovery_exempt": 1 if engine.rebate_recovery_exempt(month) else 0,
        "rebate_clawback_cents": clawback,
        "audit_flag": flag,
        "v5_mode_snap": mode,
        "net_baseline_snap_cents": baseline_net,
        "target_carry_rate_snap": target,
        "advisor_name_snap": str(tenure["advisor_name"]) if tenure is not None else "",
        "base_coef_snap": float(tenure["base_coef"] or 0) if tenure is not None else 0.0,
        "breakeven_snap_cents": pay["breakeven"] if pay else 0,
        "excess_rate_snap": float(tenure["excess_rate"] or 0) if tenure is not None else 0.0,
        "shop_raw_pool_cents": raw_pool,
        "shop_payout_ratio": payout_ratio,
        "shop_final_pool_cents": final_pool,
        "co_pool_deducted_cents": co_pool,
        "advisor_retained_net_cents": retained,
        "score_1_net": scores["score_1"],
        "score_2_biz": scores["score_2"],
        "score_3a_deduct": scores["score_3a"],
        "score_3b_redline": scores["score_3b"],
        "total_score_100": scores["total_100"],
        "mgr_score_20": scores["mgr_score_20"],
        "mgr_score_15": scores["mgr_score_15"],
        "advisor_sys_coef": (
            engine.tenure_coef(_to_int(tenure["work_days"]), month_days(month)) if pay else 0.0
        ),
        "advisor_base_pay_cents": pay["base_pay"] if pay else 0,
        "advisor_excess_bonus_cents": pay["excess_bonus"] if pay else 0,
        "advisor_3b_penalty_cents": pay["penalty"] if pay else 0,
        "advisor_final_pay_cents": pay["final_pay"] if pay else 0,
    }
    row["biz_rows"] = (
        [
            {
                "store_id": store_id,
                "source_store_id": int(ctx["root"]),
                "role_share": str(ctx["role_share"]),
                "biz_code": biz_code,
                "daily_sum_count": int(daily_count),
                "settled_count": int(settled_count),
                "unit_price_cents": int(unit_price),
                "raw_amount_cents": int(raw_amount),
            }
        ]
        if biz_code
        else []
    )
    return row


def compute_month(conn: sqlite3.Connection, month: Any) -> List[Dict[str, Any]]:
    """只算不写：返回该月每家经营主体的表 4 行（含表 3 明细），给试算预览用。"""
    key = _require_enabled_month(month)
    contexts = _store_contexts(conn, key)
    counts = _reconciled_counts(conn, key, contexts)
    return [
        _compute_store(conn, key, ctx, counts.get(int(ctx["store_id"]), (0, 0, False, False)))
        for ctx in contexts
    ]


# ---------------------------------------------------------------------------
# 写：表 3 / 表 4 落库、锁月与状态流转
# ---------------------------------------------------------------------------


def _biz_insert(conn: sqlite3.Connection, month: str, store_id: int, biz_rows: Sequence[Mapping[str, Any]], user_id: int) -> None:
    """表 3 整体重写（先删后插）：一张店的明细要么全新，要么不留。

    零量行（日报与实结量都为 0，例如顾问店没做顺差让利）不落库，
    免得全月表 3 塞满金额为 0 的空行；被核减到 0 但原报有量的行要留，那是核减证据。
    """
    conn.execute(
        "DELETE FROM store_month_biz WHERE month=? AND store_id=?", (month, int(store_id))
    )
    for biz in biz_rows:
        if int(biz["daily_sum_count"]) <= 0 and int(biz["settled_count"]) <= 0:
            continue
        conn.execute(
            """
            INSERT INTO store_month_biz
                (month, store_id, source_store_id, role_share, biz_code,
                 daily_sum_count, settled_count, unit_price_cents, raw_amount_cents,
                 updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                month,
                int(store_id),
                int(biz["source_store_id"]),
                str(biz["role_share"]),
                str(biz["biz_code"]),
                int(biz["daily_sum_count"]),
                int(biz["settled_count"]),
                int(biz["unit_price_cents"]),
                int(biz["raw_amount_cents"]),
                _now(),
                _uid(user_id),
            ),
        )


def _settlement_upsert(
    conn: sqlite3.Connection, month: str, store_id: int, row: Mapping[str, Any], user_id: int
) -> None:
    """表 4 upsert：录入列原样回写 + 快照与计算列覆盖 + 状态打回 DRAFT。"""
    cols = ("month", "store_id", *SETTLEMENT_WRITE_COLUMNS, "status", "updated_at", "updated_by")
    updates = ", ".join(
        f"{name}=excluded.{name}"
        for name in (*SETTLEMENT_WRITE_COLUMNS, "status", "updated_at", "updated_by")
    )
    params: List[Any] = [month, int(store_id)]
    params.extend(row.get(name) for name in SETTLEMENT_WRITE_COLUMNS)
    params.extend([STATUS_DRAFT, _now(), _uid(user_id)])
    conn.execute(
        f"""
        INSERT INTO store_month_settlement ({", ".join(cols)})
        VALUES ({", ".join("?" * len(cols))})
        ON CONFLICT(month, store_id) DO UPDATE SET {updates}
        """,
        params,
    )


_AUDIT_KEYS: Tuple[str, ...] = (
    "status",
    "net_commission_cents",
    "shop_raw_pool_cents",
    "shop_payout_ratio",
    "shop_final_pool_cents",
    "rebate_clawback_cents",
    "total_score_100",
    "advisor_final_pay_cents",
    "audit_flag",
)


def _audit_json(row: Any) -> str:
    """审计只留关键结果列，别把五十多列全塞进 JSON。"""
    payload = {name: _get(row, name, "") for name in _AUDIT_KEYS if _get(row, name, None) is not None}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _get(row: Any, name: str, default: Any = 0) -> Any:
    """兼容 sqlite3.Row 与 dict 的取值（Row 没有 .get，别在这上面踩坑）。"""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[name] if name in keys else default


def _stored_row(conn: sqlite3.Connection, month: str, store_id: int) -> Any:
    return conn.execute(
        "SELECT * FROM store_month_settlement WHERE month=? AND store_id=?",
        (month, int(store_id)),
    ).fetchone()


def apply_month(conn: sqlite3.Connection, month: Any, user_id: int = 0) -> List[Dict[str, Any]]:
    """重算并落库表 3 / 表 4：已锁月的店跳过（锁月后一个字节都不许动）。"""
    key = _require_enabled_month(month)
    written: List[Dict[str, Any]] = []
    for row in compute_month(conn, key):
        store_id = int(row["store_id"])
        if settlement_status(conn, key, store_id) == STATUS_LOCKED:
            continue
        before = _audit_json(_stored_row(conn, key, store_id) or {})
        _biz_insert(conn, key, store_id, row["biz_rows"], user_id)
        _settlement_upsert(conn, key, store_id, row, user_id)
        _audit(conn, key, store_id, ACTION_COMPUTE, user_id, before=before, after=_audit_json(row))
        written.append(row)
    return written


def confirm_month(conn: sqlite3.Connection, month: Any, user_id: int = 0) -> int:
    """把该月全部 DRAFT 打回确认：内容没变才允许确认，变了必须重新试算。"""
    key = _require_enabled_month(month)
    rows = [
        row
        for row in conn.execute(
            "SELECT * FROM store_month_settlement WHERE month=? ORDER BY store_id", (key,)
        )
        if str(row["status"]) == STATUS_DRAFT
    ]
    for row in rows:
        store_id = int(row["store_id"])
        conn.execute(
            "UPDATE store_month_settlement SET status=?, updated_at=?, updated_by=? "
            "WHERE month=? AND store_id=? AND status=?",
            (STATUS_CONFIRMED, _now(), _uid(user_id), key, store_id, STATUS_DRAFT),
        )
        _audit(
            conn,
            key,
            store_id,
            ACTION_CONFIRM,
            user_id,
            before=_audit_json(row),
            after=_audit_json({**_row_dict(row), "status": STATUS_CONFIRMED}),
        )
    return len(rows)


def _row_dict(row: Any) -> Dict[str, Any]:
    return {name: row[name] for name in row.keys()} if row is not None else {}


def lock_month(conn: sqlite3.Connection, month: Any, user_id: int = 0) -> int:
    """锁月：必须全部确认后才锁；锁月后重算与录入一律拒绝。"""
    key = _require_enabled_month(month)
    rows = list(
        conn.execute(
            "SELECT * FROM store_month_settlement WHERE month=? ORDER BY store_id", (key,)
        )
    )
    if not rows:
        raise ValueError(f"{key} 还没有试算结果，先试算再锁月")
    pending = [row for row in rows if str(row["status"]) == STATUS_DRAFT]
    if pending:
        raise ValueError(f"{key} 还有 {len(pending)} 家门店未确认，先确认再锁月")
    todo = [row for row in rows if str(row["status"]) == STATUS_CONFIRMED]
    stamp = _now()
    for row in todo:
        store_id = int(row["store_id"])
        conn.execute(
            "UPDATE store_month_settlement SET status=?, locked_by=?, locked_at=?, updated_at=? "
            "WHERE month=? AND store_id=? AND status=?",
            (STATUS_LOCKED, _uid(user_id), stamp, stamp, key, store_id, STATUS_CONFIRMED),
        )
        _audit(
            conn,
            key,
            store_id,
            ACTION_LOCK,
            user_id,
            before=_audit_json(row),
            after=_audit_json({**_row_dict(row), "status": STATUS_LOCKED}),
        )
    return len(todo)


def unlock_month(conn: sqlite3.Connection, month: Any, user_id: int = 0, reason: str = "") -> int:
    """解锁：LOCKED 退回 CONFIRMED，并强制留审计原因（谁、为什么）。"""
    key = _require_enabled_month(month)
    rows = [
        row
        for row in conn.execute(
            "SELECT * FROM store_month_settlement WHERE month=? ORDER BY store_id", (key,)
        )
        if str(row["status"]) == STATUS_LOCKED
    ]
    for row in rows:
        store_id = int(row["store_id"])
        conn.execute(
            "UPDATE store_month_settlement SET status=?, locked_by=NULL, locked_at='', updated_at=?, "
            "updated_by=? WHERE month=? AND store_id=? AND status=?",
            (STATUS_CONFIRMED, _now(), _uid(user_id), key, store_id, STATUS_LOCKED),
        )
        _audit(
            conn,
            key,
            store_id,
            ACTION_UNLOCK,
            user_id,
            before=_audit_json(row),
            after=_audit_json({**_row_dict(row), "status": STATUS_CONFIRMED}),
            reason=reason,
        )
    return len(rows)

# ---------------------------------------------------------------------------
# 读：月结列表、表 3 明细、汇总统计
# ---------------------------------------------------------------------------


def settlement_row(conn: sqlite3.Connection, month: Any, store_id: int) -> Dict[str, Any]:
    """单个店的表 4 行（含门店名/编码），没有试算过返回空 dict。"""
    key = _require_month(month)
    row = conn.execute(
        """
        SELECT s.*, st.code AS store_code, st.name AS store_name,
               st.region_group AS region_group, st.store_manager AS store_manager
        FROM store_month_settlement s
        JOIN stores st ON st.id = s.store_id
        WHERE s.month=? AND s.store_id=?
        """,
        (key, int(store_id)),
    ).fetchone()
    return _row_dict(row)


def list_month_settlements(conn: sqlite3.Connection, month: Any) -> List[Dict[str, Any]]:
    """该月已落库的全部表 4 行，按门店排序位次稳定输出。"""
    key = _require_month(month)
    rows = conn.execute(
        """
        SELECT s.*, st.code AS store_code, st.name AS store_name,
               st.region_group AS region_group, st.store_manager AS store_manager
        FROM store_month_settlement s
        JOIN stores st ON st.id = s.store_id
        WHERE s.month=?
        ORDER BY st.sort_order, st.id
        """,
        (key,),
    )
    return [_row_dict(row) for row in rows]


def store_biz_rows(conn: sqlite3.Connection, month: Any, store_id: int) -> List[Dict[str, Any]]:
    """表 3：该店该月的单笔提成明细，带工号归属母店的名字。"""
    key = _require_month(month)
    rows = conn.execute(
        """
        SELECT b.*, st.name AS source_store_name, st.code AS source_store_code
        FROM store_month_biz b
        LEFT JOIN stores st ON st.id = b.source_store_id
        WHERE b.month=? AND b.store_id=?
        ORDER BY b.biz_code, b.source_store_id
        """,
        (key, int(store_id)),
    )
    return [_row_dict(row) for row in rows]


def summarize(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """大盘汇总：钱走分、分数保留两位，状态与告警按条目计数。"""
    scores: List[int] = []
    status_counts: Dict[str, int] = {}
    flag_counts: Dict[str, int] = {}
    total = {
        "gross_commission_cents": 0,
        "net_commission_cents": 0,
        "shop_raw_pool_cents": 0,
        "shop_final_pool_cents": 0,
        "co_pool_deducted_cents": 0,
        "rebate_clawback_cents": 0,
        "advisor_final_pay_cents": 0,
        "core_biz_count": 0,
    }
    for row in rows:
        for name in total:
            total[name] += _to_int(_get(row, name, 0))
        score = _get(row, "total_score_100", None)
        if score is not None:
            scores.append(_to_int(score))
        status = str(_get(row, "status", "") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        flag = str(_get(row, "audit_flag", "") or "")
        if flag:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    advisors = sum(1 for row in rows if _to_int(_get(row, "advisor_final_pay_cents", 0)) > 0)
    clawbacks = sum(1 for row in rows if _to_int(_get(row, "rebate_clawback_cents", 0)) > 0)
    return {
        "store_count": len(rows),
        **total,
        "advisor_count": advisors,
        "clawback_count": clawbacks,
        "avg_score_100": round(sum(scores) / len(scores), 2) if scores else 0.0,
        "draft_count": status_counts.get(STATUS_DRAFT, 0),
        "confirmed_count": status_counts.get(STATUS_CONFIRMED, 0),
        "locked_count": status_counts.get(STATUS_LOCKED, 0),
        "status_counts": status_counts,
        "flag_counts": flag_counts,
    }


def month_totals(conn: sqlite3.Connection, month: Any) -> Dict[str, Any]:
    """该月大盘汇总（列表页顶部卡片用）。"""
    return summarize(list_month_settlements(conn, month))
