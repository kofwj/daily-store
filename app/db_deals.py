"""门店 db 模块 — 见 app/db.py 的拆分说明。"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from .db_core import _now, today_local


def _deal_payload(
    *,
    user_id: int,
    closed: bool,
    model: str,
    phone: str,
    spend: str,
    hall_query: bool,
    recommend: str,
    student: bool,
    opener: str,
    note: str,
    text: str,
) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "closed": 1 if closed else 0,
        "model": (model or "").strip()[:80],
        "phone": (phone or "").strip()[:20],
        "spend": (spend or "").strip()[:20],
        "hall_query": 1 if hall_query else 0,
        "recommend": (recommend or "").strip()[:80],
        "student": 1 if student else 0,
        "opener": (opener or "").strip()[:40],
        "note": (note or "").strip()[:1000],
        "text": (text or "").strip()[:2000],
        "updated_at": _now(),
    }

def record_deal_post(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    user_id: int,
    closed: bool,
    model: str = "",
    phone: str = "",
    spend: str = "",
    hall_query: bool = True,
    recommend: str = "",
    student: bool = False,
    opener: str = "",
    note: str = "",
    text: str = "",
    deal_id: Optional[int] = None,
    biz_date: Optional[date] = None,
) -> int:
    day = biz_date or today_local()
    payload = _deal_payload(
        user_id=user_id,
        closed=closed,
        model=model,
        phone=phone,
        spend=spend,
        hall_query=hall_query,
        recommend=recommend,
        student=student,
        opener=opener,
        note=note,
        text=text,
    )
    existing_id = None
    if deal_id:
        row = conn.execute(
            "SELECT id FROM deal_posts WHERE id=? AND store_id=?",
            (deal_id, store_id),
        ).fetchone()
        if row:
            existing_id = int(row["id"])
    if existing_id is None and payload["phone"]:
        row = conn.execute(
            """
            SELECT id FROM deal_posts
            WHERE store_id=? AND biz_date=? AND phone=?
            ORDER BY id DESC LIMIT 1
            """,
            (store_id, day.isoformat(), payload["phone"]),
        ).fetchone()
        if row:
            existing_id = int(row["id"])
    if existing_id is not None:
        conn.execute(
            """
            UPDATE deal_posts
            SET user_id=:user_id, updated_at=:updated_at, closed=:closed, model=:model,
                phone=:phone, spend=:spend, hall_query=:hall_query, recommend=:recommend,
                student=:student, opener=:opener, note=:note, text=:text
            WHERE id=:id
            """,
            {**payload, "id": existing_id},
        )
        return existing_id
    cur = conn.execute(
        """
        INSERT INTO deal_posts(
            store_id, user_id, created_at, updated_at, biz_date, closed, model,
            phone, spend, hall_query, recommend, student, opener, note, text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            store_id,
            payload["user_id"],
            payload["updated_at"],
            payload["updated_at"],
            day.isoformat(),
            payload["closed"],
            payload["model"],
            payload["phone"],
            payload["spend"],
            payload["hall_query"],
            payload["recommend"],
            payload["student"],
            payload["opener"],
            payload["note"],
            payload["text"],
        ),
    )
    return int(cur.lastrowid)

def count_deal_posts(
    conn: sqlite3.Connection, store_id: int, start: date, end: date
) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM deal_posts WHERE store_id=? AND biz_date>=? AND biz_date<=?",
            (store_id, start.isoformat(), end.isoformat()),
        ).fetchone()[0]
        or 0
    )

def list_deal_posts(
    conn: sqlite3.Connection,
    store_id: int,
    start: date,
    end: date,
    limit: int = 50,
    offset: int = 0,
) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT d.*, u.display_name AS submitter_name
            FROM deal_posts d
            LEFT JOIN users u ON u.id = d.user_id
            WHERE d.store_id=? AND d.biz_date>=? AND d.biz_date<=?
            ORDER BY d.id DESC
            LIMIT ? OFFSET ?
            """,
            (store_id, start.isoformat(), end.isoformat(), limit, offset),
        )
    )

def get_deal_post(conn: sqlite3.Connection, deal_id: int, store_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM deal_posts WHERE id=? AND store_id=?",
        (deal_id, store_id),
    ).fetchone()

def delete_deal_post(conn: sqlite3.Connection, deal_id: int, store_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM deal_posts WHERE id=? AND store_id=?",
        (deal_id, store_id),
    )
    return cur.rowcount > 0

def deal_counts(
    conn: sqlite3.Connection,
    store_ids: Iterable[int],
    start: date,
    end: date,
) -> Dict[int, Dict[str, int]]:
    ids = [int(sid) for sid in store_ids]
    out = {sid: {"total": 0, "closed": 0} for sid in ids}
    if not ids:
        return out
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT store_id,
               COUNT(*) AS total,
               SUM(CASE WHEN closed=1 THEN 1 ELSE 0 END) AS closed
        FROM deal_posts
        WHERE store_id IN ({placeholders}) AND biz_date>=? AND biz_date<=?
        GROUP BY store_id
        """,
        [*ids, start.isoformat(), end.isoformat()],
    )
    for row in rows:
        out[int(row["store_id"])] = {
            "total": int(row["total"] or 0),
            "closed": int(row["closed"] or 0),
        }
    return out

