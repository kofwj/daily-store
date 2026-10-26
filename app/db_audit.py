"""审计日志数据查询层。

从 views_admin 中抽出：多表 UNION 审计查询、条件拼装与分页取数。
不依赖 web/helpers，保持纯粹的数据层职责。
"""

from __future__ import annotations

import sqlite3
from typing import Any, List, Optional, Sequence, Tuple

from .db_core import sql_in

_AUDIT_SOURCES = (
    # (kind, 表别名, 审计表, 日期列, 是否有 action 列)
    ("daily", "rn", "report_edits", "rn.biz_date", False),
    ("deal", "dn", "deal_edits", "dn.biz_date", True),
    ("advance", "an", "advance_edits", "an.biz_date", True),
    ("invoice", "inv", "invoice_edits", "inv.month", True),
    ("mobile", "bm", "bisuan_mobile_edits", "bm.month", True),
)


def _audit_union_sources(kind: str):
    """审计 UNION 的数据源：kind=all 全部，否则只取对应表。"""
    return [row for row in _AUDIT_SOURCES if kind in ("all", row[0])]


def _build_audit_sql(
    kind: str,
    sid: Optional[int],
    scoped_ids: Sequence[int],
    cutoff: str,
) -> Tuple[str, List[Any]]:
    """拼装审计多表 UNION ALL 的 SQL 和参数列表。"""
    parts: List[str] = []
    params: List[Any] = []
    for kind_, alias, table, date_col, has_action in _audit_union_sources(kind):
        w = f"{alias}.edited_at >= ?"
        ps: List[Any] = [cutoff]
        if sid:
            w += f" AND {alias}.store_id=?"
            ps.append(sid)
        elif scoped_ids:
            clause, ids = sql_in(f"{alias}.store_id", scoped_ids)
            w += f" AND {clause}"
            ps.extend(ids)
        action_col = f"{alias}.action" if has_action else "'' AS action"
        parts.append(
            f"SELECT '{kind_}' AS kind, {alias}.id, {date_col} AS biz_date, "
            f"{alias}.edited_at, {alias}.note, {alias}.before_json, {alias}.after_json, "
            f"{action_col}, s.name AS store_name, u.username AS user_name "
            f"FROM {table} {alias} "
            f"LEFT JOIN stores s ON s.id={alias}.store_id "
            f"LEFT JOIN users u ON u.id={alias}.user_id "
            f"WHERE {w}"
        )
        params += ps
    union_sql = " UNION ALL ".join(parts)
    return union_sql, params


def count_audit_edits(
    conn: sqlite3.Connection,
    *,
    kind: str = "all",
    sid: Optional[int] = None,
    scoped_ids: Sequence[int] = (),
    cutoff: str,
) -> int:
    """统计满足条件的审计日志总条数。"""
    union_sql, params = _build_audit_sql(kind, sid, scoped_ids, cutoff)
    row = conn.execute(f"SELECT COUNT(*) FROM ({union_sql})", params).fetchone()
    return int(row[0]) if row else 0


def list_audit_edits(
    conn: sqlite3.Connection,
    *,
    kind: str = "all",
    sid: Optional[int] = None,
    scoped_ids: Sequence[int] = (),
    cutoff: str,
    limit: int = 50,
    offset: int = 0,
) -> List[sqlite3.Row]:
    """查询指定分页的审计日志行。"""
    union_sql, params = _build_audit_sql(kind, sid, scoped_ids, cutoff)
    return list(
        conn.execute(
            f"""
            SELECT * FROM ({union_sql}) t
            ORDER BY t.edited_at DESC, t.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
    )
