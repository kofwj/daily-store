"""移动校准笔算：按店按月一行，带截止日和审计。

原来存在 app_meta 的字符串键（bisuan_mobile_<店>_<月>），没有审计、
截止日全月共用一个键、值来回浮点转换。这里改成真表：
- value_tenths 整数存 0.1 精度，和 daily_facts 同口径
- asof 按店保存，不再互相覆盖
- 每次改动写 bisuan_mobile_edits，可回溯谁改的、原值多少
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any, Dict, List, Optional

from .db_core import _now


def _ensure_bisuan_mobile_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bisuan_mobile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            month TEXT NOT NULL,
            value_tenths INTEGER NOT NULL DEFAULT 0,
            asof TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by INTEGER REFERENCES users(id),
            UNIQUE(store_id, month)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bisuan_mobile_month ON bisuan_mobile(month, store_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bisuan_mobile_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            month TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id),
            edited_at TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '',
            after_json TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bisuan_mobile_edits_store ON bisuan_mobile_edits(store_id, month)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bisuan_mobile_edits_at ON bisuan_mobile_edits(edited_at DESC, id DESC)"
    )


def _migrate_bisuan_mobile_from_settings(conn: sqlite3.Connection) -> None:
    """把 app_meta 里的旧键搬进新表。幂等：搬过的 key 会删掉。"""
    _ensure_bisuan_mobile_tables(conn)
    try:
        rows = list(
            conn.execute(
                "SELECT key, value FROM app_meta WHERE key LIKE 'bisuan_mobile_%' OR key LIKE 'bisuan_official_%'"
            )
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise  # 锁等其他真错误：不吞，迁移版本不落，下次启动重试
        return  # 早期库还没有 app_meta，没有旧键可搬
    # 月级截止日（旧设计全月共用），搬迁时作为各店的初始 asof
    asof_by_month: Dict[str, str] = {}
    for key, value in rows:
        if key.startswith("bisuan_mobile_asof_"):
            asof_by_month[key[len("bisuan_mobile_asof_") :]] = (value or "")[:10]
    moved: List[str] = []
    for key, value in rows:
        if key.startswith("bisuan_mobile_asof_"):
            moved.append(key)
            continue
        for prefix in ("bisuan_mobile_", "bisuan_official_"):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix) :]
            sid_text, _, month = rest.partition("_")
            if not sid_text.isdigit() or not month:
                break
            try:
                tenths = int(round(float(value or 0) * 10))
            except (TypeError, ValueError):
                break
            conn.execute(
                """
                INSERT INTO bisuan_mobile(store_id, month, value_tenths, asof, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(store_id, month) DO UPDATE SET
                    value_tenths=excluded.value_tenths,
                    asof=CASE WHEN excluded.asof!='' THEN excluded.asof ELSE bisuan_mobile.asof END
                """,
                (int(sid_text), month, tenths, asof_by_month.get(month, ""), _now()),
            )
            moved.append(key)
            break
    for key in moved:
        conn.execute("DELETE FROM app_meta WHERE key=?", (key,))


def _row(row) -> Dict[str, Any]:
    return {
        "store_id": int(row["store_id"]),
        "month": row["month"],
        "value_tenths": int(row["value_tenths"] or 0),
        "asof": row["asof"] or "",
        "updated_at": row["updated_at"] or "",
        "updated_by": row["updated_by"],
    }


def get_bisuan_mobile(conn: sqlite3.Connection, store_id: int, month: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM bisuan_mobile WHERE store_id=? AND month=?",
        (int(store_id), month),
    ).fetchone()
    return _row(row) if row else None


def bisuan_mobile_map(conn: sqlite3.Connection, month: str) -> Dict[int, int]:
    """{store_id: value_tenths}，供评优/通报表统一取口径。"""
    return {
        int(r["store_id"]): int(r["value_tenths"] or 0)
        for r in conn.execute(
            "SELECT store_id, value_tenths FROM bisuan_mobile WHERE month=?", (month,)
        )
    }


def bisuan_mobile_asof_map(conn: sqlite3.Connection, month: str) -> Dict[int, str]:
    return {
        int(r["store_id"]): (r["asof"] or "")
        for r in conn.execute("SELECT store_id, asof FROM bisuan_mobile WHERE month=?", (month,))
    }


def save_bisuan_mobile(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    month: str,
    value_tenths: int,
    asof: date,
    user_id: int = 0,
    note: str = "",
) -> None:
    """写移动校准数并留痕。value_tenths 必须是非负整数（0.1 精度 ×10）。"""
    sid = int(store_id)
    tenths = int(value_tenths)
    if tenths < 0:
        raise ValueError("移动取数不能是负数")
    before = get_bisuan_mobile(conn, sid, month)
    after = {"value_tenths": tenths, "asof": asof.isoformat()}
    conn.execute(
        """
        INSERT INTO bisuan_mobile(store_id, month, value_tenths, asof, updated_at, updated_by)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(store_id, month) DO UPDATE SET
            value_tenths=excluded.value_tenths,
            asof=excluded.asof,
            updated_at=excluded.updated_at,
            updated_by=excluded.updated_by
        """,
        (sid, month, tenths, asof.isoformat(), _now(), user_id or None),
    )
    conn.execute(
        """
        INSERT INTO bisuan_mobile_edits(
            store_id, month, user_id, edited_at, action, before_json, after_json, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            month,
            user_id or None,
            _now(),
            "update" if before else "create",
            json.dumps(
                {"value_tenths": before["value_tenths"], "asof": before["asof"]} if before else {},
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(after, ensure_ascii=False, sort_keys=True),
            note,
        ),
    )


def list_bisuan_mobile_edits(
    conn: sqlite3.Connection, *, month: str = "", store_id: int = 0, limit: int = 200
) -> List[Dict[str, Any]]:
    where = []
    params: List[Any] = []
    if month:
        where.append("e.month=?")
        params.append(month)
    if store_id:
        where.append("e.store_id=?")
        params.append(int(store_id))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"""
        SELECT e.*, u.display_name, s.short_name, s.name AS store_name
        FROM bisuan_mobile_edits e
        LEFT JOIN users u ON u.id = e.user_id
        LEFT JOIN stores s ON s.id = e.store_id
        {clause}
        ORDER BY e.edited_at DESC, e.id DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    )
    out = []
    for r in rows:
        try:
            before = json.loads(r["before_json"] or "{}")
        except ValueError:
            before = {}
        try:
            after = json.loads(r["after_json"] or "{}")
        except ValueError:
            after = {}
        out.append(
            {
                "store_id": int(r["store_id"]),
                "store": (r["short_name"] or r["store_name"] or "").strip(),
                "month": r["month"],
                "editor": (r["display_name"] or "").strip() or "—",
                "edited_at": r["edited_at"] or "",
                "action": r["action"] or "",
                "before": before,
                "after": after,
                "note": r["note"] or "",
            }
        )
    return out
