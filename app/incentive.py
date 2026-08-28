"""运营商顾问 / 无顾问门店的月度奖罚。

口径：
- AI = Ai手机合约
- 新用户直降 = 充值 + 芝麻免充 + 储蓄卡冻结 + 全品类

奖罚阈值 / 金额可从配置读（app.meta['incentive_rules']），季度可改；
不配时用下方 DEFAULTS。
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict

DEFAULTS: Dict[str, int] = {
    # 有顾问：AI + 新用户直降 ≥ 总量阈值才可能达标
    "total_threshold": 10,   # 总量达标线
    "ai_best": 3,            # AI ≥ 此值 → 高效完成
    "ai_pass": 1,            # AI ≥ 此值 → 已破 0
    "reward_best": 500,      # 高效完成奖门店
    "reward_pass": 200,      # 总量达标奖门店
    "reward_sesame_penalty": 100,  # 总量靠直降、AI 未破 0 → 罚顾问
    # 有顾问未达标：
    "ai_5": 5,              # AI ≥ 此值 → 顾问免责，罚门店
    "penal_store_ai5": 200,  # 店长带队拖后腿罚门店
    "penal_store_mid": 100,  # 整体欠佳罚门店
    "penal_advisor_mid": 50, # 整体欠佳罚顾问
    "penal_store_zero": 200, # AI 挂 0 罚门店
    "penal_advisor_zero": 100,  # AI 挂 0 罚顾问
    # 无顾问：
    "reward_no_advisor": 200,    # AI、新用户直降均破 0 → 奖门店
    "penal_store_one": 50,     # 单项破 0 → 罚门店
    "penal_store_none": 100,   # 双未破 0 → 罚门店
}


def rules_from(raw: str = "") -> Dict[str, int]:
    """从 app_meta 存的 JSON 读配置，缺的用默认。"""
    rules = copy.deepcopy(DEFAULTS)
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    for key, default in DEFAULTS.items():
        try:
            v = int(data.get(key, default))
        except (TypeError, ValueError):
            v = default
        rules[key] = v
    return rules


def judge_with_advisor(ai: int, new_cut: int, r: Dict[str, int] | None = None) -> Dict[str, Any]:
    r = r or DEFAULTS
    ai = int(ai or 0)
    new_cut = int(new_cut or 0)
    total = ai + new_cut
    if total >= r["total_threshold"]:
        if ai >= r["ai_best"]:
            return _result(
                True, "双达标", "总量达标且 AI≥高线，高效完成",
                store_reward=r["reward_best"],
            )
        if ai >= r["ai_pass"]:
            return _result(
                True, "总量达标", "总量达标且 AI 已破 0",
                store_reward=r["reward_pass"],
            )
        return _result(
            True, "总量靠直降", "总量达标但 AI 未破 0，顾问主业失职",
            advisor_penalty=r["reward_sesame_penalty"],
        )
    if ai >= r["ai_5"]:
        return _result(
            False, "顾问搭载好、总量不够",
            "AI≥高线 但总量未达标，店长带队拖后腿，顾问免责",
            store_penalty=r["penal_store_ai5"],
        )
    if ai >= r["ai_pass"]:
        return _result(
            False, "整体欠佳", "总量未达标且 AI 只有中段",
            store_penalty=r["penal_store_mid"],
            advisor_penalty=r["penal_advisor_mid"],
        )
    return _result(
        False, "整体极差", "总量未达标且 AI 挂 0，顶格处罚",
        store_penalty=r["penal_store_zero"],
        advisor_penalty=r["penal_advisor_zero"],
    )


def judge_without_advisor(ai: int, new_cut: int, r: Dict[str, int] | None = None) -> Dict[str, Any]:
    r = r or DEFAULTS
    ai = int(ai or 0)
    new_cut = int(new_cut or 0)
    # 无顾问店的“破 0”就是严格大于 0——不跟 ai_pass 走，否则管理员改 ai_pass 会连带改判定口径
    ai_ok = ai >  0
    cut_ok = new_cut >  0
    if ai_ok and cut_ok:
        return _result(True, "双破 0", "AI、新用户直降均已破 0", store_reward=r["reward_no_advisor"])
    if ai_ok or cut_ok:
        return _result(False, "单项未破 0", "只有一项破 0", store_penalty=r["penal_store_one"])
    return _result(False, "双未破 0", "AI、新用户直降都是 0", store_penalty=r["penal_store_none"])


def judge(has_advisor: bool, ai: int, new_cut: int, rules: Dict[str, int] = None) -> Dict[str, Any]:
    r = rules or DEFAULTS
    if has_advisor:
        row = judge_with_advisor(ai, new_cut, r)
        row["scheme"] = "有运营商顾问"
        row["goal"] = f"AI + 新用户直降 ≥ {r['total_threshold']}"
    else:
        row = judge_without_advisor(ai, new_cut, r)
        row["scheme"] = "无运营商顾问"
        row["goal"] = "AI、新用户直降均破 0"
    row["ai"] = int(ai or 0)
    row["new_cut"] = int(new_cut or 0)
    row["sesame"] = int(new_cut or 0)
    row["total"] = int(ai or 0) + int(new_cut or 0)
    row["has_advisor"] = bool(has_advisor)
    row["net"] = row["store_reward"] - row["store_penalty"] - row["advisor_penalty"]
    return row


def money_text(row: Dict[str, Any]) -> str:
    if row.get("store_reward"):
        return f"奖门店 {row['store_reward']}"
    parts = []
    if row.get("store_penalty"):
        parts.append(f"罚门店 {row['store_penalty']}")
    if row.get("advisor_penalty"):
        parts.append(f"罚顾问 {row['advisor_penalty']}")
    return " / ".join(parts) if parts else "—"


def _result(
    passed: bool,
    label: str,
    reason: str,
    *,
    store_reward: int = 0,
    store_penalty: int = 0,
    advisor_penalty: int = 0,
) -> Dict[str, Any]:
    return {
        "passed": passed,
        "label": label,
        "reason": reason,
        "store_reward": store_reward,
        "store_penalty": store_penalty,
        "advisor_penalty": advisor_penalty,
    }
