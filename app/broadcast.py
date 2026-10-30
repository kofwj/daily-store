"""把当日+本月累计拼成现有微信群播报格式。"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .metrics_seed import SECTIONS, format_stored, rollup_codes

DayCum = Tuple[int, int]


def format_biz_date(biz_date: date) -> str:
    return f"{biz_date.month}月{biz_date.day}日"


def _fmt(code: str, value) -> str:
    return format_stored(code, value)


def _line(name: str, day, cum, *, code: str = "") -> str:
    return f"{name}：日{_fmt(code, day)}，累{_fmt(code, cum)}"


def sections_for_store(store: Any, as_of: Optional[date] = None) -> Iterable[Mapping[str, Any]]:
    """按门店模式取播报/填报表单使用的科目分组。

    双轨期一律回落到全局 SECTIONS：V5 的移动 10 项、电信 8 项、泰州 8 项科目
    尚未灌进 metrics，此时按模式过滤只会把播报清空。第三阶段把科目落库后，
    仅需在这里按 store["v5_mode"] 与 as_of 返回对应子集，调用点不必再改。
    """
    return SECTIONS


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
