"""运营商顾问 / 无顾问门店的月度奖罚。

口径：
- AI手机 = Ai手机合约
- 芝麻直降 = 新用户直降·芝麻免充
"""

from __future__ import annotations

from typing import Any, Dict


def judge_with_advisor(ai: int, sesame: int) -> Dict[str, Any]:
    ai = int(ai or 0)
    sesame = int(sesame or 0)
    total = ai + sesame
    if total >= 10:
        if ai >= 3:
            return _result(
                True,
                "双达标",
                "总量达标且 AI≥3，高效完成",
                store_reward=500,
            )
        if ai >= 1:
            return _result(
                True,
                "总量达标",
                "总量达标且 AI 已破 0",
                store_reward=200,
            )
        return _result(
            True,
            "总量靠芝麻",
            "总量达标但 AI 未破 0，顾问主业失职",
            advisor_penalty=100,
        )
    if ai >= 5:
        return _result(
            False,
            "顾问搭载好、总量不够",
            "AI≥5 但总量未到 10，店长带队拖后腿，顾问免责",
            store_penalty=200,
        )
    if ai >= 1:
        return _result(
            False,
            "整体欠佳",
            "总量未达标且 AI 只有 1–4",
            store_penalty=100,
            advisor_penalty=50,
        )
    return _result(
        False,
        "整体极差",
        "总量未达标且 AI 挂 0，顶格处罚",
        store_penalty=200,
        advisor_penalty=100,
    )


def judge_without_advisor(ai: int, sesame: int) -> Dict[str, Any]:
    ai = int(ai or 0)
    sesame = int(sesame or 0)
    ai_ok = ai >= 1
    sesame_ok = sesame >= 1
    if ai_ok and sesame_ok:
        return _result(True, "双破 0", "AI、芝麻均已破 0", store_reward=200)
    if ai_ok or sesame_ok:
        return _result(False, "单项未破 0", "只有一项破 0", store_penalty=50)
    return _result(False, "双未破 0", "AI、芝麻都是 0", store_penalty=100)


def judge(has_advisor: bool, ai: int, sesame: int) -> Dict[str, Any]:
    if has_advisor:
        row = judge_with_advisor(ai, sesame)
        row["scheme"] = "有运营商顾问"
        row["goal"] = "AI + 芝麻 ≥ 10"
    else:
        row = judge_without_advisor(ai, sesame)
        row["scheme"] = "无运营商顾问"
        row["goal"] = "AI、芝麻均破 0"
    row["ai"] = int(ai or 0)
    row["sesame"] = int(sesame or 0)
    row["total"] = int(ai or 0) + int(sesame or 0)
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
