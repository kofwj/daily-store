"""把当日+本月累计拼成现有微信群播报格式。"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, Mapping, Tuple

from .metrics_seed import ROLLUPS, SECTIONS, format_stored

DayCum = Tuple[int, int]


def format_biz_date(biz_date: date) -> str:
    return f"{biz_date.month}月{biz_date.day}日"


def _fmt(code: str, value) -> str:
    return format_stored(code, value)


def _line(name: str, day, cum, *, code: str = "") -> str:
    return f"{name}：日{_fmt(code, day)}，累{_fmt(code, cum)}"


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
            visible.append(_line("金币直降", day, cum, code="coin_cut_old"))
        for code, name, _hint in section["metrics"]:
            if code in ROLLUPS["coin_cut_all"]["parts"]:
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
