"""垫资台账：门店记流水，管理员兑付，月底按店汇总。"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .db_core import _now, month_bounds, today_local


def parse_money(raw: Any) -> float:
    text = str(raw or "").strip().replace(",", "").replace("，", "")
    if not text:
        return 0.0
    return round(float(text), 2)


def money_ok(*amounts: float) -> bool:
    return any(abs(v) >= 0.005 for v in amounts)


def record_advance(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    user_id: int,
    biz_date: date,
    phone: str = "",
    broadband: float = 0,
    rebate: float = 0,
    other: float = 0,
    note: str = "",
    advance_id: Optional[int] = None,
) -> int:
    payload = {
        "user_id": user_id,
        "updated_at": _now(),
        "biz_date": biz_date.isoformat(),
        "phone": (phone or "").strip()[:20],
        "broadband": round(float(broadband or 0), 2),
        "rebate": round(float(rebate or 0), 2),
        "other": round(float(other or 0), 2),
        "note": (note or "").strip()[:500],
    }
    if advance_id:
        row = conn.execute(
            "SELECT * FROM advance_posts WHERE id=? AND store_id=?",
            (advance_id, store_id),
        ).fetchone()
        if row is None:
            raise ValueError("missing")
        if int(row["paid"] or 0):
            raise ValueError("paid_locked")
        conn.execute(
            """
            UPDATE advance_posts
            SET user_id=:user_id, updated_at=:updated_at, biz_date=:biz_date,
                phone=:phone, broadband=:broadband, rebate=:rebate, other=:other,
                note=:note
            WHERE id=:id
            """,
            {**payload, "id": int(advance_id)},
        )
        return int(advance_id)
    cur = conn.execute(
        """
        INSERT INTO advance_posts(
            store_id, user_id, created_at, updated_at, biz_date, phone,
            broadband, rebate, other, note, paid, paid_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
        """,
        (
            store_id,
            payload["user_id"],
            payload["updated_at"],
            payload["updated_at"],
            payload["biz_date"],
            payload["phone"],
            payload["broadband"],
            payload["rebate"],
            payload["other"],
            payload["note"],
        ),
    )
    return int(cur.lastrowid)


def get_advance(conn: sqlite3.Connection, advance_id: int, store_id: int):
    return conn.execute(
        "SELECT * FROM advance_posts WHERE id=? AND store_id=?",
        (advance_id, store_id),
    ).fetchone()


def delete_advance(
    conn: sqlite3.Connection, advance_id: int, store_id: int
) -> bool:
    row = get_advance(conn, advance_id, store_id)
    if row is None or int(row["paid"] or 0):
        return False
    conn.execute(
        "DELETE FROM advance_posts WHERE id=? AND store_id=?",
        (advance_id, store_id),
    )
    return True


def set_advance_paid(
    conn: sqlite3.Connection,
    advance_ids: Sequence[int],
    *,
    paid: bool,
    user_id: int,
    paid_at: Optional[date] = None,
) -> int:
    ids = [int(i) for i in advance_ids if str(i).isdigit() or isinstance(i, int)]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    if paid:
        day = (paid_at or today_local()).isoformat()
        cur = conn.execute(
            f"""
            UPDATE advance_posts
            SET paid=1, paid_at=?, paid_by=?, updated_at=?
            WHERE id IN ({placeholders}) AND paid=0
            """,
            [day, user_id, _now(), *ids],
        )
    else:
        cur = conn.execute(
            f"""
            UPDATE advance_posts
            SET paid=0, paid_at='', paid_by=NULL, updated_at=?
            WHERE id IN ({placeholders}) AND paid=1
            """,
            [_now(), *ids],
        )
    return int(cur.rowcount or 0)


def count_advances(
    conn: sqlite3.Connection,
    *,
    store_id: Optional[int],
    start: date,
    end: date,
    paid: Optional[int] = None,
) -> int:
    where = ["biz_date>=?", "biz_date<=?"]
    params: List[Any] = [start.isoformat(), end.isoformat()]
    if store_id:
        where.append("store_id=?")
        params.append(store_id)
    if paid is not None:
        where.append("paid=?")
        params.append(int(paid))
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM advance_posts WHERE {' AND '.join(where)}",
            params,
        ).fetchone()[0]
        or 0
    )


def list_advances(
    conn: sqlite3.Connection,
    *,
    store_id: Optional[int],
    start: date,
    end: date,
    paid: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[sqlite3.Row]:
    where = ["a.biz_date>=?", "a.biz_date<=?"]
    params: List[Any] = [start.isoformat(), end.isoformat()]
    if store_id:
        where.append("a.store_id=?")
        params.append(store_id)
    if paid is not None:
        where.append("a.paid=?")
        params.append(int(paid))
    params.extend([limit, offset])
    return list(
        conn.execute(
            f"""
            SELECT a.*, st.name AS store_name, st.short_name AS store_short,
                   st.city AS store_city, u.display_name AS submitter_name,
                   p.display_name AS payer_name,
                   ROUND(a.broadband + a.rebate + a.other, 2) AS total
            FROM advance_posts a
            JOIN stores st ON st.id = a.store_id
            LEFT JOIN users u ON u.id = a.user_id
            LEFT JOIN users p ON p.id = a.paid_by
            WHERE {' AND '.join(where)}
            ORDER BY a.biz_date DESC, a.id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        )
    )


def list_all_advances(
    conn: sqlite3.Connection, start: date, end: date
) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT a.*, st.name AS store_name, st.short_name AS store_short,
                   st.city AS store_city, st.sort_order AS store_sort,
                   ROUND(a.broadband + a.rebate + a.other, 2) AS total
            FROM advance_posts a
            JOIN stores st ON st.id = a.store_id
            WHERE a.biz_date>=? AND a.biz_date<=?
            ORDER BY st.sort_order, st.id, a.biz_date, a.id
            """,
            (start.isoformat(), end.isoformat()),
        )
    )


def advance_month_totals(
    conn: sqlite3.Connection, store_ids: Iterable[int], as_of: date
) -> Dict[int, Dict[str, float]]:
    ids = [int(sid) for sid in store_ids]
    out = {
        sid: {"broadband": 0.0, "rebate": 0.0, "other": 0.0, "total": 0.0}
        for sid in ids
    }
    if not ids:
        return out
    start, end = month_bounds(as_of)
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT store_id,
               ROUND(SUM(broadband), 2) AS broadband,
               ROUND(SUM(rebate), 2) AS rebate,
               ROUND(SUM(other), 2) AS other
        FROM advance_posts
        WHERE biz_date>=? AND biz_date<=? AND store_id IN ({placeholders})
        GROUP BY store_id
        """,
        [start, end, *ids],
    )
    for row in rows:
        sid = int(row["store_id"])
        broadband = float(row["broadband"] or 0)
        rebate = float(row["rebate"] or 0)
        other = float(row["other"] or 0)
        out[sid] = {
            "broadband": broadband,
            "rebate": rebate,
            "other": other,
            "total": round(broadband + rebate + other, 2),
        }
    return out
