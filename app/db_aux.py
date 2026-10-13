"""业辅查询登记数据层：每店每天一个整数，跟手机销量一样就地填、就地改。

为什么单独一张表：这是销量对比表要用的辅助数字，不是日报指标。
放进 metrics / daily_facts 会被 list_metrics 带进日报填报表单，所以另开一张小表。
表本身很薄（只有数字 + 谁什么时候写的），改动的留痕只在这两列上，
不写 report_edits——那边是「日报指标前后值」的审计，混进非指标会污染修改审计页。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, Optional, Sequence

from .db_core import _now, begin_immediate
from .db_core import store_in_clause as _store_in


def _norm(n: Any) -> int:
    """业辅查询登记条数：负数 / 非数字一律当 0，别把脏值带进合计。"""
    try:
        value = int(n or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def aux_days(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
    start: date,
    end: date,
) -> Dict[int, Dict[str, Dict[str, Any]]]:
    """各店各日的业辅查询登记：{store_id: {biz_date: {"n", "editor", "at"}}}。

    缺的店 / 日不补 0（稀疏），调用方按 .get 取值；editor / at 是最后一次登记的人和时间，
    给格子 hover 溯源用。
    """
    ids = [int(sid) for sid in store_ids]
    if not ids or end < start:
        return {}
    clause, params = _store_in("a.store_id", ids)
    rows = conn.execute(
        f"""
        SELECT a.store_id, a.biz_date, a.n, a.updated_at, u.display_name AS editor_name
        FROM aux_queries a
        LEFT JOIN users u ON u.id = a.updated_by
        WHERE a.biz_date>=? AND a.biz_date<=? AND {clause}
        """,
        [start.isoformat(), end.isoformat(), *params],
    )
    out: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(int(row["store_id"]), {})[str(row["biz_date"])] = {
            "n": _norm(row["n"]),
            "editor": (row["editor_name"] or "").strip(),
            "at": (row["updated_at"] or "")[:16],
        }
    return out


def aux_range_total(
    conn: sqlite3.Connection,
    store_ids: Iterable[int],
    start: date,
    end: date,
) -> int:
    """区间内这些店的业辅查询登记合计（单店传 [sid] 就是这家店的累计）。"""
    ids = [int(sid) for sid in store_ids]
    if not ids or end < start:
        return 0
    clause, params = _store_in("store_id", ids)
    rows = conn.execute(
        f"""
        SELECT n FROM aux_queries
        WHERE biz_date>=? AND biz_date<=? AND {clause}
        """,
        [start.isoformat(), end.isoformat(), *params],
    )
    # 逐条归一化后再求和；若先 SUM，历史负数脏值会抵消正常登记。
    return sum(_norm(row[0]) for row in rows)


def set_aux(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    biz_date: date,
    n: int,
    user_id: Optional[int] = None,
    at: Optional[str] = None,
) -> None:
    """写一格业辅查询登记（同店同日覆盖）。

    写 0 等于没登记：整行删掉，免得「登记过 0」在表里留一行空壳——
    格子空着本来也显示 0，看起来一样，但表能少一堆噪音。
    """
    begin_immediate(conn)
    value = _norm(n)
    if value <= 0:
        conn.execute(
            "DELETE FROM aux_queries WHERE biz_date=? AND store_id=?",
            (biz_date.isoformat(), store_id),
        )
        return
    conn.execute(
        """
        INSERT INTO aux_queries(biz_date, store_id, n, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(biz_date, store_id) DO UPDATE SET
            n=excluded.n, updated_by=excluded.updated_by, updated_at=excluded.updated_at
        """,
        (biz_date.isoformat(), store_id, value, user_id, at or _now()),
    )
