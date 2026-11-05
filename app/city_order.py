"""地市排序：按门店目录里地市首次出现的顺序分组。

看板 / 洞察 / 偏差三页下线后只剩服务通报在用，所以从 insights.py 单独提出来。
"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence

CITY_UNASSIGNED = "未分地市"


def catalog_city_order(stores: Sequence[Mapping[str, Any]]) -> List[str]:
    """门店目录里地市首次出现的顺序。"""
    seen: List[str] = []
    for store in stores:
        city = (store["city"] or "").strip() or CITY_UNASSIGNED
        if city not in seen:
            seen.append(city)
    return seen


def group_rows_by_city_order(
    rows: Sequence[Any],
    city_order: Sequence[str],
    get_city: Any,
) -> List[Any]:
    """按目录地市切开，组内保留传入顺序（稳定排序）。"""
    rank = {c: i for i, c in enumerate(city_order)}
    return sorted(list(rows), key=lambda r: rank.get(get_city(r), len(rank)))
