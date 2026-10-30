"""V5 方案静态基线配置 + 按 month 的隔离开关。

本模块只放**数据与开关**，不放任何计算逻辑：
- 迁移 17 用它把门店 V5 模式、考核基线（表 1）、顾问任期（表 2）灌进库；
- 页面（设置 → 门店档案）用它把中文标签、生效月份展示给管理员；
- V5 引擎（后续迭代）用 `v5_month_enabled()` 判断某个月份是否允许触发，
  2026-10 及以前一律走原口径，绝不回溯。

口径来源：docs/V5落库与store-daily改造对接清单.md（V4 终审版）第一节门店表。
定位门店一律用 `code`，因为测试库（STORE_DAILY_SAMPLE_SEED=1）里示例店是 id 1..6，
按 id 区间硬编码会命中错误的行。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional, Tuple

# V5 正式执行月（以正式下文为准）。此月及之后才允许触发 V5 计算引擎。
V5_START_MONTH = "2026-11"
# 该月及以前继续按原口径展示/归档，双轨并行，不作任何回溯重算。
V5_LEGACY_LAST_MONTH = "2026-10"

# —— 门店模式 ——
MODE_ADVISOR_A = "ADVISOR_A"           # 专职顾问 A 档（基础系数 1.20 / 超额 40%）
MODE_ADVISOR_B_NEAR = "ADVISOR_B_NEAR"  # 专职顾问 B 近档（1.00 / 70%）
MODE_NO_ADVISOR_MAIN = "NO_ADVISOR_MAIN"  # 无顾问独立主力店（100% 提成）
MODE_AFFILIATED = "AFFILIATED"          # 挂靠小店（50% 合办，落母店工号）
MODE_TELECOM_SINGLE = "TELECOM_SINGLE"  # 电信单店（搭载率口径 12%）
MODE_TAIZHOU_ADVANCE_ONLY = "TAIZHOU_ADVANCE_ONLY"  # 泰州：只做垫资预存，不进 V5 提成
MODE_FRANCHISE_DISABLED = "FRANCHISE_DISABLED"  # 加盟/非主控店，不进任何提成与考核
MODE_SAMPLE_DISABLED = "SAMPLE_DISABLED"  # 示例店，不进任何业务
MODE_UNSET = "UNSET"                    # 未纳入 V5（历史店或新录入店）

# 进移动通报表 AI 手机列的 V5 门店模式（电信 / 泰州垫资 / 加盟 / 示例店都不进）
MOBILE_AI_MODES = (MODE_ADVISOR_A, MODE_ADVISOR_B_NEAR, MODE_NO_ADVISOR_MAIN, MODE_AFFILIATED)
# V5 AI 手机拆项：同开换新 + 未同开换新；科目落库后二者之和即 AI 手机数
V5_AI_METRICS = ("v5_m5", "v5_m6")

V5_MODE_LABELS: Dict[str, str] = {
    MODE_ADVISOR_A: "专职顾问（A 档）",
    MODE_ADVISOR_B_NEAR: "专职顾问（B 近档）",
    MODE_NO_ADVISOR_MAIN: "无顾问主力店",
    MODE_AFFILIATED: "挂靠小店",
    MODE_TELECOM_SINGLE: "电信单店",
    MODE_TAIZHOU_ADVANCE_ONLY: "泰州垫资预存",
    MODE_FRANCHISE_DISABLED: "非主控（加盟）",
    MODE_SAMPLE_DISABLED: "示例店（不参与核算）",
    MODE_UNSET: "未纳入 V5",
}

# 这些模式不配考核基线、不进提成与考核计算。
DISABLED_MODES = (MODE_FRANCHISE_DISABLED, MODE_SAMPLE_DISABLED, MODE_UNSET)

CARRIER_LABELS: Dict[str, str] = {"MOBILE": "移动", "TELECOM": "电信"}

# 目标搭载率三档：(25 分满分 / 18 分及格 / 10 分底线)
CARRIER_CARRY_TIERS: Dict[str, Tuple[float, float, float]] = {
    "MOBILE": (0.20, 0.15, 0.10),
    "TELECOM": (0.12, 0.09, 0.06),
}

# 有专职顾问的门店，单店提成总包绝对封顶（分）
ADVISOR_CAP_ABS_CENTS = 65000
DEFAULT_CAP_RATIO = 0.35
DEFAULT_BASE_PAY_CENTS = 400000

SAMPLE_STORES: Tuple[Tuple[str, str], ...] = (
    ("store-alpha", "alpha"),
    ("store-beta", "beta"),
    ("store-gamma", "gamma"),
    ("store-delta", "delta"),
    ("store-epsilon", "epsilon"),
    ("store-zeta", "zeta"),
)

_TAIZHOU_CODES = (
    "jingjiang-wuyue",
    "jingjiang-yinxiang",
    "taixing-gulou",
    "jiangyan-luotang",
    "hailing-beilu",
    "taixing-wuyue",
    "hailing-wanda",
    "taizhou-wanxiang",
    "xinghua-wuyue",
    "jiangyan-sanshui",
)


def _main(code: str, baseline_yuan: int) -> Dict[str, Any]:
    return {
        "code": code,
        "v5_mode": MODE_NO_ADVISOR_MAIN,
        "carrier_scope": "MOBILE",
        "mobile_code_active": 1,
        "net_baseline_cents": baseline_yuan * 100,
        "cap_abs_cents": 0,
        "advisor": None,
    }


def _advisor(
    code: str,
    name: str,
    tier: str,
    baseline_yuan: int,
    breakeven_yuan: int,
) -> Dict[str, Any]:
    is_a = tier == "A"
    return {
        "code": code,
        "v5_mode": MODE_ADVISOR_A if is_a else MODE_ADVISOR_B_NEAR,
        "carrier_scope": "MOBILE",
        "mobile_code_active": 1,
        "net_baseline_cents": baseline_yuan * 100,
        "cap_abs_cents": ADVISOR_CAP_ABS_CENTS,
        "advisor": {
            "name": name,
            "tier_grade": tier,
            "base_pay_cents": DEFAULT_BASE_PAY_CENTS,
            "base_coef": 1.20 if is_a else 1.00,
            "breakeven_cents": breakeven_yuan * 100,
            "excess_rate": 0.40 if is_a else 0.70,
            # 任职起始月：10 月起到任，V5 从 11 月起算
            "from_month": "2026-10",
        },
    }


def _simple(code: str, mode: str, **extra: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "code": code,
        "v5_mode": mode,
        "carrier_scope": extra.pop("carrier_scope", "MOBILE"),
        "mobile_code_active": extra.pop("mobile_code_active", 0),
        "net_baseline_cents": extra.pop("net_baseline_cents", 0),
        "cap_abs_cents": extra.pop("cap_abs_cents", 0),
        "advisor": None,
    }
    item.update(extra)
    return item


# 34 家门店的 V5 模式快照，按 code 定位（与 docs 第一节门店表一一对应）。
STORE_V5_CONFIG: Tuple[Dict[str, Any], ...] = (
    # —— 专职顾问店 ——
    _advisor("rg-ninghai", "李海燕", "A", 8967, 7487),
    _advisor("haian-wanda", "徐琳琳", "A", 13753, 7167),
    _advisor("qidong-renmin", "赵凯博", "B_NEAR", 4649, 5745),
    _advisor("tongzhou-jinsha", "张庆庆", "B_NEAR", 5222, 5483),
    # —— 无顾问主力店（100% 提成）——
    _main("gangzha-wanda", 4230),
    _main("tongzhou-wanda", 5917),
    _main("haimen-jinhua", 3116),
    _main("haian-renmin", 4718),
    _main("nt-kaifaqu", 1000),
    _main("qidong-wuyue", 1000),
    # —— 电信单店（搭载率口径 12%）——
    {
        "code": "yuanrong",
        "v5_mode": MODE_TELECOM_SINGLE,
        "carrier_scope": "TELECOM",
        "mobile_code_active": 0,
        "net_baseline_cents": 1000 * 100,
        "cap_abs_cents": 0,
        "advisor": None,
    },
    # —— 挂靠小店（50% 合办，落母店工号；基线随母店）——
    _simple("chongchuan-changqiao", MODE_AFFILIATED, affiliated_to="gangzha-wanda"),
    _simple("kfq-yongwang", MODE_AFFILIATED, affiliated_to="nt-kaifaqu"),
    # —— 泰州：只做垫资预存 ——
    *[_simple(code, MODE_TAIZHOU_ADVANCE_ONLY) for code in _TAIZHOU_CODES],
    # —— 非主控（加盟）店：不进提成与考核 ——
    _simple("nt-dayoujing", MODE_FRANCHISE_DISABLED),
    _simple("haian-xirun", MODE_FRANCHISE_DISABLED),
    _simple("rugao-wanda", MODE_FRANCHISE_DISABLED),
    _simple("rugao-wuyue", MODE_FRANCHISE_DISABLED),
    _simple("chongchuan-yinhe", MODE_FRANCHISE_DISABLED),
    # —— 示例店 ——
    *[_simple(code, MODE_SAMPLE_DISABLED) for code, _login in SAMPLE_STORES],
)

# 个别门店的移动编码开关与门店表不一致处（有独立机构号仍需手动确认）
_MOBILE_CODE_ACTIVE_OVERRIDES = {
    "qidong-wuyue": 1,
    "jiangyan-luotang": 1,
    "xinghua-wuyue": 1,
}
for _cfg in STORE_V5_CONFIG:
    if _cfg["code"] in _MOBILE_CODE_ACTIVE_OVERRIDES:
        _cfg["mobile_code_active"] = _MOBILE_CODE_ACTIVE_OVERRIDES[_cfg["code"]]
del _cfg

_BY_CODE: Dict[str, Dict[str, Any]] = {item["code"]: item for item in STORE_V5_CONFIG}


def config_for(code: str) -> Optional[Dict[str, Any]]:
    """按门店 code 取 V5 配置；没有就返回 None（不抛错）。"""
    return _BY_CODE.get((code or "").strip())


def mode_label(mode: str) -> str:
    return V5_MODE_LABELS.get((mode or "").strip(), (mode or "").strip() or "未纳入 V5")


def carrier_label(scope: str) -> str:
    return CARRIER_LABELS.get((scope or "").strip().upper(), "移动")


def month_key(value: Any) -> str:
    """把 date / 'YYYY-MM-DD' / 'YYYY-MM' 归一成 'YYYY-MM'；认不出就返回 ''。"""
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    text = str(value or "").strip()
    if len(text) >= 7 and text[4] == "-":
        month = text[:7]
        if month[5:].isdigit():
            return month
    return ""


def v5_month_enabled(month: Any) -> bool:
    """该月份是否允许触发 V5 引擎。

    2026-10 及以前（含无法解析的输入）一律返回 False —— 历史月份继续按原口径，
    不重算、不回溯；只有管理员显式对 V5 生效月生成月结表时才会变成 True。
    """
    key = month_key(month)
    if not key:
        return False
    return key >= V5_START_MONTH
