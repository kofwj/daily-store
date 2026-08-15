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


# 审计关心的内容字段（含前/后快照与展示标签）
_DEAL_SNAPSHOT_FIELDS = (
    "closed",
    "model",
    "phone",
    "spend",
    "hall_query",
    "recommend",
    "student",
    "opener",
    "note",
    "text",
)


def _row_snapshot(row) -> Dict[str, Any]:
    """把 deal_posts 一行转成审计内容快照（布尔归一）。"""
    return {
        f: bool(row[f]) if f in ("closed", "hall_query", "student") else (row[f] or "")
        for f in _DEAL_SNAPSHOT_FIELDS
    }


def _payload_snapshot(p: Dict[str, Any]) -> Dict[str, Any]:
    """把保存后的 payload 转成审计内容快照（与新纪录后的行为一致）。"""
    out: Dict[str, Any] = {}
    for f in _DEAL_SNAPSHOT_FIELDS:
        if f in ("closed", "hall_query", "student"):
            out[f] = bool(p[f])
        else:
            out[f] = p[f]
    return out


def record_deal_edit(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    user_id: int,
    deal_id: int,
    biz_date: date,
    action: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    note: str = "",
) -> None:
    """往 deal_edits 写一条成交播报的审计日志。action: create / update。"""
    import json as _json

    conn.execute(
        """
        INSERT INTO deal_edits(
            store_id, user_id, deal_id, biz_date, edited_at, action,
            before_json, after_json, note
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            store_id,
            user_id,
            deal_id,
            biz_date.isoformat(),
            _now(),
            action,
            _json.dumps(before, ensure_ascii=False, sort_keys=True),
            _json.dumps(after, ensure_ascii=False, sort_keys=True),
            note,
        ),
    )

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
    existing_row = None
    if deal_id:
        row = conn.execute(
            "SELECT * FROM deal_posts WHERE id=? AND store_id=?",
            (deal_id, store_id),
        ).fetchone()
        if row:
            from .deal import is_today_deal

            if not is_today_deal(row, day):
                raise ValueError("past_deal_locked")
            existing_id = int(row["id"])
            existing_row = row
    if existing_id is None and payload["phone"]:
        row = conn.execute(
            """
            SELECT * FROM deal_posts
            WHERE store_id=? AND biz_date=? AND phone=?
            ORDER BY id DESC LIMIT 1
            """,
            (store_id, day.isoformat(), payload["phone"]),
        ).fetchone()
        if row:
            existing_id = int(row["id"])
            existing_row = row
    after = _payload_snapshot(payload)
    if existing_id is not None:
        before = _row_snapshot(existing_row) if existing_row is not None else {}
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
        record_deal_edit(
            conn,
            store_id=store_id,
            user_id=user_id,
            deal_id=existing_id,
            biz_date=day,
            action="update",
            before=before,
            after=after,
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
    new_id = int(cur.lastrowid)
    record_deal_edit(
        conn,
        store_id=store_id,
        user_id=user_id,
        deal_id=new_id,
        biz_date=day,
        action="create",
        before={},
        after=after,
    )
    return new_id

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


def list_all_deal_posts(
    conn: sqlite3.Connection, start: date, end: date
) -> List[sqlite3.Row]:
    """全部门店在区间内的成交，按日期+门店排序（管理员导出用）。"""
    return list(
        conn.execute(
            """
            SELECT d.*, st.name AS store_name, st.short_name AS store_short,
                   u.display_name AS submitter_name
            FROM deal_posts d
            JOIN stores st ON st.id = d.store_id
            LEFT JOIN users u ON u.id = d.user_id
            WHERE d.biz_date>=? AND d.biz_date<=?
            ORDER BY d.biz_date, st.sort_order, st.id, d.id
            """,
            (start.isoformat(), end.isoformat()),
        )
    )


def get_deal_post(conn: sqlite3.Connection, deal_id: int, store_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM deal_posts WHERE id=? AND store_id=?",
        (deal_id, store_id),
    ).fetchone()

def delete_deal_post(
    conn: sqlite3.Connection,
    deal_id: int,
    store_id: int,
    user_id: Optional[int] = None,
) -> bool:
    row = conn.execute(
        "SELECT * FROM deal_posts WHERE id=? AND store_id=?",
        (deal_id, store_id),
    ).fetchone()
    if row is None:
        return False
    from .deal import is_today_deal

    if not is_today_deal(row, today_local()):
        return False
    conn.execute(
        "DELETE FROM deal_posts WHERE id=? AND store_id=?",
        (deal_id, store_id),
    )
    biz = date.fromisoformat(str(row["biz_date"])) if row["biz_date"] else today_local()
    record_deal_edit(
        conn,
        store_id=store_id,
        user_id=int(user_id) if user_id is not None else int(row["user_id"] or 0),
        deal_id=deal_id,
        biz_date=biz,
        action="delete",
        before=_row_snapshot(row),
        after={},
        note="删除成交",
    )
    return True

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

