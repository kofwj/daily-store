"""垫资台账：门店记流水，管理员兑付，月底按店汇总。"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .db_core import _now, month_bounds, today_local

MAX_AMOUNT = 10_000_000.0


def yuan_to_cents(value: Any) -> int:
    number = float(value or 0)
    if not math.isfinite(number) or abs(number) > MAX_AMOUNT:
        raise ValueError("金额必须是有限数字且不超过 1000 万")
    return int(round(number * 100))


def cents_to_yuan(value: Any) -> float:
    return round(int(value or 0) / 100, 2)


def parse_money(raw: Any) -> float:
    text = str(raw or "").strip().replace(",", "").replace("，", "")
    if not text:
        return 0.0
    return cents_to_yuan(yuan_to_cents(text))


def money_ok(*amounts: float) -> bool:
    return any(abs(yuan_to_cents(v)) >= 1 for v in amounts)


def _money_select(alias: str = "a") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"ROUND({prefix}broadband / 100.0, 2) AS broadband, "
        f"ROUND({prefix}rebate / 100.0, 2) AS rebate, "
        f"ROUND({prefix}other / 100.0, 2) AS other, "
        f"ROUND(({prefix}broadband + {prefix}rebate + {prefix}other) / 100.0, 2) AS total"
    )


def _snapshot(row, *, cents: bool = False) -> dict:
    if row is None:
        return {}
    out = {key: row[key] for key in row.keys()}
    if cents:
        for key in ("broadband", "rebate", "other"):
            if key in out:
                out[key] = cents_to_yuan(out[key])
    return out


def _audit(conn, row, *, user_id: int, action: str, before: dict, after: dict, note: str = "") -> None:
    conn.execute(
        """INSERT INTO advance_edits(advance_id, store_id, user_id, biz_date, edited_at,
           action, before_json, after_json, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["id"], row["store_id"], user_id, row["biz_date"], _now(), action,
         json.dumps(before, ensure_ascii=False, default=str),
         json.dumps(after, ensure_ascii=False, default=str), note),
    )


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
        "broadband": yuan_to_cents(broadband),
        "rebate": yuan_to_cents(rebate),
        "other": yuan_to_cents(other),
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
        before = _snapshot(row, cents=True)
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
        after = conn.execute("SELECT * FROM advance_posts WHERE id=?", (int(advance_id),)).fetchone()
        _audit(conn, after, user_id=user_id, action="update", before=before, after=_snapshot(after, cents=True))
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
    created = conn.execute("SELECT * FROM advance_posts WHERE id=?", (int(cur.lastrowid),)).fetchone()
    _audit(conn, created, user_id=user_id, action="create", before={}, after=_snapshot(created, cents=True))
    return int(cur.lastrowid)


def get_advance(conn: sqlite3.Connection, advance_id: int, store_id: int):
    return conn.execute(
        f"""
        SELECT id, store_id, user_id, created_at, updated_at, biz_date, phone, note,
               paid, paid_at, paid_by, {_money_select('')}
        FROM advance_posts WHERE id=? AND store_id=?
        """,
        (advance_id, store_id),
    ).fetchone()


def delete_advance(
    conn: sqlite3.Connection, advance_id: int, store_id: int, *, user_id: int
) -> bool:
    row = get_advance(conn, advance_id, store_id)
    if row is None or int(row["paid"] or 0):
        return False
    before = _snapshot(row)  # get_advance() 已按元返回
    conn.execute("DELETE FROM advance_posts WHERE id=? AND store_id=?", (advance_id, store_id))
    _audit(conn, row, user_id=user_id, action="delete", before=before, after={})
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
    rows = list(conn.execute(f"SELECT * FROM advance_posts WHERE id IN ({placeholders})", ids))
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
    changed = int(cur.rowcount or 0)
    for old in rows:
        if paid and int(old["paid"] or 0):
            continue
        if not paid and not int(old["paid"] or 0):
            continue
        new = conn.execute("SELECT * FROM advance_posts WHERE id=?", (old["id"],)).fetchone()
        _audit(conn, new, user_id=user_id, action="pay" if paid else "unpay",
               before=_snapshot(old, cents=True), after=_snapshot(new, cents=True), note="兑付状态变更")
    return changed


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
            SELECT a.id, a.store_id, a.user_id, a.created_at, a.updated_at, a.biz_date,
                   a.phone, a.note, a.paid, a.paid_at, a.paid_by,
                   {_money_select('a')},
                   st.name AS store_name, st.short_name AS store_short,
                   st.city AS store_city, u.display_name AS submitter_name,
                   p.display_name AS payer_name
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
            f"""
            SELECT a.id, a.store_id, a.user_id, a.created_at, a.updated_at, a.biz_date,
                   a.phone, a.note, a.paid, a.paid_at, a.paid_by,
                   {_money_select('a')},
                   st.name AS store_name, st.short_name AS store_short,
                   st.city AS store_city, st.sort_order AS store_sort
            FROM advance_posts a
            JOIN stores st ON st.id = a.store_id
            WHERE a.biz_date>=? AND a.biz_date<=?
            ORDER BY st.sort_order, st.id, a.biz_date, a.id
            """,
            (start.isoformat(), end.isoformat()),
        )
    )


def advance_today_inbox(conn: sqlite3.Connection, day: date) -> List[sqlite3.Row]:
    """当天未兑付：按店汇总，管理员用来看谁交了要兑。"""
    return list(
        conn.execute(
            """
            SELECT a.store_id,
                   st.short_name AS store_short,
                   st.name AS store_name,
                   COUNT(*) AS n,
                   ROUND(SUM(a.broadband + a.rebate + a.other) / 100.0, 2) AS total
            FROM advance_posts a
            JOIN stores st ON st.id = a.store_id
            WHERE a.biz_date=? AND a.paid=0
            GROUP BY a.store_id
            ORDER BY st.sort_order, st.id
            """,
            (day.isoformat(),),
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
               ROUND(SUM(broadband) / 100.0, 2) AS broadband,
               ROUND(SUM(rebate) / 100.0, 2) AS rebate,
               ROUND(SUM(other) / 100.0, 2) AS other
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
