"""把当日+本月累计拼成现有微信群播报格式。"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from . import v5_baseline
from .metrics_seed import (
    SECTIONS,
    V5_MOBILE_SECTIONS,
    V5_TAIZHOU_SECTIONS,
    V5_TELECOM_SECTIONS,
    format_stored,
    rollup_codes,
)

DayCum = Tuple[int, int]


def format_biz_date(biz_date: date) -> str:
    return f"{biz_date.month}月{biz_date.day}日"


def _fmt(code: str, value) -> str:
    return format_stored(code, value)


def _line(name: str, day, cum, *, code: str = "") -> str:
    return f"{name}：日{_fmt(code, day)}，累{_fmt(code, cum)}"


NO_FILING_MESSAGE = (
    "该店为非主控加盟店，暂未开通日报填报；如需开展业务请联系管理员配置挂靠母店"
)


def _store_field(store: Any, key: str, default: str = "") -> str:
    """安全取值：dict / sqlite3.Row / None 都能用，取不到返回 default。"""
    if store is None:
        return default
    try:
        value = store[key]
    except (KeyError, IndexError, TypeError):
        return default
    return str(value or "").strip() or default


def store_v5_mode(store: Any) -> str:
    return _store_field(store, "v5_mode")


def sections_for_store(store: Any, as_of: Optional[date] = None) -> Iterable[Mapping[str, Any]]:
    """按门店模式 + 月份取播报/填报表单使用的科目分组（唯一分流缝）。

    - 2026-10 及以前（以及取不到日期）：一律回落到全局 `SECTIONS`，双轨隔离，
      历史月份的表单、播报、导出逐字节不变；
    - 2026-11 起：移动四类模式（顾问 A/B 近档、无顾问主力、挂靠）→ 移动 10 科目；
      电信独立店 → 电信 8 科目；泰州垫资店 → 泰州 8 科目；
    - 非主控加盟店 → 空（`NO_FILING_MESSAGE` 提示联系管理员配置挂靠母店）；
    - 未纳入 V5（`UNSET`）或认不出的模式、示例店 → 仍按原 `SECTIONS`，
      避免口径未定就把人挡在门外（示例店只存在于测试环境）。
    """
    if not v5_baseline.v5_month_enabled(as_of):
        return SECTIONS
    mode = store_v5_mode(store)
    if mode in v5_baseline.MOBILE_AI_MODES:
        return V5_MOBILE_SECTIONS
    if mode == v5_baseline.MODE_TELECOM_SINGLE:
        return V5_TELECOM_SECTIONS
    if mode == v5_baseline.MODE_TAIZHOU_ADVANCE_ONLY:
        return V5_TAIZHOU_SECTIONS
    if mode == v5_baseline.MODE_FRANCHISE_DISABLED:
        return []
    return SECTIONS


def filing_blocked(store: Any, as_of: Optional[date] = None) -> str:
    """该店在该日期是否禁止填报：返回给店员看的提示语，空串表示可填。"""
    if not v5_baseline.v5_month_enabled(as_of):
        return ""
    if store_v5_mode(store) == v5_baseline.MODE_FRANCHISE_DISABLED:
        return NO_FILING_MESSAGE
    return ""


def render_broadcast(
    store_name: str,
    biz_date: date,
    values: Mapping[str, DayCum],
    *,
    compact: bool = False,
    compact_sections: Iterable[str] = ("digital",),
    sections: Optional[Iterable[Mapping[str, Any]]] = None,
) -> str:
    """生成群播报正文。

    compact=True 时，指定分组里「日=0 且 累=0」的行不输出。
    分组标题在该组还有可见行时才输出。
    sections 不传时回落到全局 SECTIONS，保持纯函数默认行为不变。
    """
    active_sections = SECTIONS if sections is None else sections
    compact_set = set(compact_sections)
    lines = [format_biz_date(biz_date), store_name]

    # 金币直降合成的口径随月份变（10 月期起含低销迎回、芝麻免充下线），按当次播报的日期取
    coin_dates_codes = set(rollup_codes("coin_cut_all", biz_date))

    for section in active_sections:
        visible = []
        if section["code"] == "contract":
            day, cum = _sum_codes(values, coin_dates_codes)
            visible.append(_line("金币直降", day, cum, code="coin_cut_old"))
        for code, name, _hint in section["metrics"]:
            if code in coin_dates_codes:
                continue
            day, cum = values.get(code, (0, 0))
            hide = compact and section["code"] in compact_set and int(day or 0) == 0 and int(cum or 0) == 0
            if not hide:
                visible.append(_line(name, day, cum, code=code))

        if not visible:
            continue
        if section["blank_before"]:
            lines.append("")
        if section["header"]:
            lines.append(section["header"])
        lines.extend(visible)

    return "\n".join(lines) + "\n"


def _sum_codes(values: Mapping[str, DayCum], codes: Iterable[str]) -> DayCum:
    day = sum(int((values.get(code) or (0, 0))[0] or 0) for code in codes)
    cum = sum(int((values.get(code) or (0, 0))[1] or 0) for code in codes)
    return day, cum


def add_day_to_prev(prev_cum: Mapping[str, int], today: Mapping[str, int]) -> Dict[str, DayCum]:
    """用「本月截至昨日累计 + 今日日值」得到播报用的 (日, 累)。"""
    codes = set(prev_cum) | set(today)
    out: Dict[str, DayCum] = {}
    for code in codes:
        day = int(today.get(code, 0) or 0)
        out[code] = (day, int(prev_cum.get(code, 0) or 0) + day)
    return out
