"""把当日+本月累计拼成现有微信群播报格式。"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, Mapping, Tuple

from .metrics_seed import ROLLUPS, SECTIONS

DayCum = Tuple[int, int]


def format_biz_date(biz_date: date) -> str:
    return f"{biz_date.month}/{biz_date.day}"


def _line(name: str, day: int, cum: int) -> str:
    return f"{name}：日{int(day)}；累{int(cum)}"


def render_broadcast(
    store_name: str,
    biz_date: date,
    values: Mapping[str, DayCum],
    *,
    compact: bool = False,
    compact_sections: Iterable[str] = ("digital",),
) -> str:
    """生成群播报正文。

    compact=True 时，指定分组里「日=0 且 累=0」的行不输出。
    分组标题在该组还有可见行时才输出。
    """
    compact_set = set(compact_sections)
    lines = [format_biz_date(biz_date), store_name]

    for section in SECTIONS:
        visible = []
        if section["code"] == "contract":
            spec = ROLLUPS["coin_cut_all"]
            day, cum = _sum_codes(values, spec["parts"] + spec["legacy"])
            visible.append(_line("金币直降", day, cum))
        for code, name in section["metrics"]:
            if code in ROLLUPS["coin_cut_all"]["parts"]:
                continue
            day, cum = values.get(code, (0, 0))
            day, cum = int(day or 0), int(cum or 0)
            hide = compact and section["code"] in compact_set and day == 0 and cum == 0
            if not hide:
                visible.append(_line(name, day, cum))

        if not visible:
            continue
        if section["blank_before"]:
            lines.append("")
        if section["header"]:
            lines.append(section["header"])
        lines.extend(visible)

    return "\n".join(lines) + "\n"


def month_start(biz_date: date) -> date:
    return biz_date.replace(day=1)


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
