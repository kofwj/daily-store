"""门店 db 模块 — 见 app/db.py 的拆分说明。"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, List, Sequence

from .db_core import _now, get_report, list_metrics, month_bounds, record_edit
from .metrics_seed import to_stored


def prev_month_cum(
    conn: sqlite3.Connection, store_id: int, biz_date: date
) -> Dict[str, int]:
    """本月 1 号到昨日（不含当天）的累计。"""
    start, _end = month_bounds(biz_date)
    yesterday = date.fromordinal(biz_date.toordinal() - 1).isoformat() if biz_date.day > 1 else None
    out = {row["code"]: 0 for row in list_metrics(conn)}
    if yesterday is None:
        return out
    rows = conn.execute(
        """
        SELECT metric_code, SUM(day_value) AS total
        FROM daily_facts
        WHERE store_id=? AND biz_date>=? AND biz_date<=?
        GROUP BY metric_code
        """,
        (store_id, start, yesterday),
    )
    for row in rows:
        out[row["metric_code"]] = int(row["total"] or 0)
    return out

def month_cum_through(
    conn: sqlite3.Connection, store_id: int, biz_date: date
) -> Dict[str, int]:
    start, end = month_bounds(biz_date)
    out = {row["code"]: 0 for row in list_metrics(conn)}
    rows = conn.execute(
        """
        SELECT metric_code, SUM(day_value) AS total
        FROM daily_facts
        WHERE store_id=? AND biz_date>=? AND biz_date<=?
        GROUP BY metric_code
        """,
        (store_id, start, end),
    )
    for row in rows:
        out[row["metric_code"]] = int(row["total"] or 0)
    return out

def day_values(conn: sqlite3.Connection, store_id: int, biz_date: date) -> Dict[str, int]:
    out = {row["code"]: 0 for row in list_metrics(conn)}
    rows = conn.execute(
        """
        SELECT metric_code, day_value FROM daily_facts
        WHERE store_id=? AND biz_date=?
        """,
        (store_id, biz_date.isoformat()),
    )
    for row in rows:
        out[row["metric_code"]] = int(row["day_value"] or 0)
    return out

def save_daily(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    biz_date: date,
    values: Dict[str, int],
    user_id: int,
    compact: bool = False,
    note: str = "",
) -> None:
    codes = {row["code"] for row in list_metrics(conn)}
    day = biz_date.isoformat()
    conn.execute(
        """
        INSERT INTO daily_reports(biz_date, store_id, submitted_by, submitted_at, compact, note)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(biz_date, store_id) DO UPDATE SET
            submitted_by=excluded.submitted_by,
            submitted_at=excluded.submitted_at,
            compact=excluded.compact,
            note=excluded.note
        """,
        (day, store_id, user_id, _now(), 1 if compact else 0, note or ""),
    )
    for code, raw in values.items():
        if code not in codes:
            continue
        value = max(0, to_stored(code, raw) if not isinstance(raw, int) else max(0, raw))
        conn.execute(
            """
            INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(biz_date, store_id, metric_code) DO UPDATE SET
                day_value=excluded.day_value
            """,
            (day, store_id, code, value),
        )
    # 未提交的指标写成 0，避免旧值残留
    for code in codes:
        if code not in values:
            conn.execute(
                """
                INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(biz_date, store_id, metric_code) DO UPDATE SET
                    day_value=0
                """,
                (day, store_id, code),
            )

def week_metric_total(
    conn: sqlite3.Connection, store_id: int, start: date, end: date, metric_codes: Sequence[str]
) -> int:
    codes = [c for c in metric_codes if c]
    if not codes:
        return 0
    placeholders = ",".join("?" * len(codes))
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(day_value), 0) AS total
        FROM daily_facts
        WHERE store_id=? AND biz_date>=? AND biz_date<=? AND metric_code IN ({placeholders})
        """,
        (store_id, start.isoformat(), end.isoformat(), *codes),
    ).fetchone()
    return int(row["total"] or 0)


def set_day_value(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    biz_date: date,
    metric_code: str,
    value: int,
    user_id: int,
    note: str = "校准单元格",
) -> None:
    """只改某一天某一个指标，不动其它格子。"""
    codes = {row["code"] for row in list_metrics(conn)}
    if metric_code not in codes:
        raise ValueError("没有这个指标")
    value = max(0, to_stored(metric_code, value) if not isinstance(value, int) else int(value or 0))
    day = biz_date.isoformat()
    before = {metric_code: int(day_values(conn, store_id, biz_date).get(metric_code, 0) or 0)}
    if before[metric_code] == value:
        return
    report = get_report(conn, store_id, biz_date)
    if report is None:
        conn.execute(
            """
            INSERT INTO daily_reports(biz_date, store_id, submitted_by, submitted_at, compact, note)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (day, store_id, user_id, _now(), "管理员校准单元格"),
        )
    conn.execute(
        """
        INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(biz_date, store_id, metric_code) DO UPDATE SET
            day_value=excluded.day_value
        """,
        (day, store_id, metric_code, value),
    )
    record_edit(
        conn,
        biz_date=biz_date,
        store_id=store_id,
        user_id=user_id,
        before=before,
        after={metric_code: value},
        note=note or "校准单元格",
    )

def facts_in_range(
    conn: sqlite3.Connection, store_id: int, start: date, end: date
) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT f.biz_date, f.metric_code, f.day_value, m.name, m.section, m.sort_order
            FROM daily_facts f
            JOIN metrics m ON m.code = f.metric_code
            WHERE f.store_id=? AND f.biz_date>=? AND f.biz_date<=?
            ORDER BY f.biz_date, m.sort_order
            """,
            (store_id, start.isoformat(), end.isoformat()),
        )
    )

def dashboard_today(conn: sqlite3.Connection, store_ids: Iterable[int], biz_date: date) -> List[Dict[str, Any]]:
    ids = list(store_ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    stores = list(
        conn.execute(
            f"SELECT * FROM stores WHERE id IN ({placeholders}) ORDER BY sort_order, id",
            ids,
        )
    )
    reports = {
        row["store_id"]: row
        for row in conn.execute(
            f"""
            SELECT r.*, u.display_name AS submitter_name
            FROM daily_reports r
            LEFT JOIN users u ON u.id = r.submitted_by
            WHERE r.biz_date=? AND r.store_id IN ({placeholders})
            """,
            [biz_date.isoformat(), *ids],
        )
    }
    out = []
    for store in stores:
        report = reports.get(store["id"])
        out.append(
            {
                "store": store,
                "submitted": report is not None,
                "submitted_at": report["submitted_at"] if report else None,
                "submitter_name": report["submitter_name"] if report else None,
            }
        )
    return out


def stores_reported_in_month(
    conn: sqlite3.Connection, store_ids: Iterable[int], as_of: date
) -> set:
    """本月 1 号到 as_of 交过日报的店 id。空列表返回空集。"""
    ids = [int(sid) for sid in store_ids]
    if not ids:
        return set()
    start, end = month_bounds(as_of)
    placeholders = ",".join("?" * len(ids))
    return {
        int(row["store_id"])
        for row in conn.execute(
            f"""
            SELECT DISTINCT store_id FROM daily_reports
            WHERE biz_date>=? AND biz_date<=? AND store_id IN ({placeholders})
            """,
            [start, end, *ids],
        )
    }

