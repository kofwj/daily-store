"""酬金/租赁开票台账：按店按月记服务费、手续费、租赁。

表里的 details_json 列是早期明细功能的遗留，一直没有写入路径（恒为 '{}'），
保留列只为老库兼容；对应的明细常量与参数已随休眠功能一并移除。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any, Dict, Sequence

from .db_advances import cents_to_yuan, parse_money, yuan_to_cents
from .db_core import _now


def _ensure_invoice_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invoice_months (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
            month TEXT NOT NULL,
            service_cents INTEGER NOT NULL DEFAULT 0,
            fee_cents INTEGER NOT NULL DEFAULT 0,
            housing_cents INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            lease_area TEXT NOT NULL DEFAULT '',
            lease_cents INTEGER NOT NULL DEFAULT 0,
            lease_address TEXT NOT NULL DEFAULT '',
            lease_period TEXT NOT NULL DEFAULT '',
            apply_date TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(store_id, month)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invoice_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            user_id INTEGER REFERENCES users(id),
            month TEXT NOT NULL,
            edited_at TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '',
            after_json TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoice_edits_store_month ON invoice_edits(store_id, month)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invoice_edits_edited_at ON invoice_edits(edited_at DESC, id DESC)"
    )


def month_key(as_of: date) -> str:
    return as_of.strftime("%Y-%m")


def empty_invoice(store_id: int, month: str) -> Dict[str, Any]:
    return {
        "id": 0,
        "store_id": int(store_id),
        "month": month,
        "service": 0.0,
        "fee": 0.0,
        "housing": 0.0,
        "invoice_total": 0.0,
        "details": {},
        "lease_area": "",
        "lease": 0.0,
        "lease_address": "",
        "lease_period": "",
        "apply_date": "",
    }


def _snapshot(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "service": rec.get("service") or 0,
        "fee": rec.get("fee") or 0,
        "housing": rec.get("housing") or 0,
        "apply_date": rec.get("apply_date") or "",
        "invoice_total": rec.get("invoice_total") or 0,
    }


def _audit(
    conn,
    *,
    store_id: int,
    user_id: int,
    month: str,
    action: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> None:
    conn.execute(
        """INSERT INTO invoice_edits(
            store_id, user_id, month, edited_at, action, before_json, after_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            int(store_id),
            int(user_id) if user_id else None,
            month,
            _now(),
            action,
            json.dumps(before, ensure_ascii=False, default=str),
            json.dumps(after, ensure_ascii=False, default=str),
        ),
    )


def _row_to_invoice(row) -> Dict[str, Any]:
    service = cents_to_yuan(row["service_cents"])
    fee = cents_to_yuan(row["fee_cents"])
    return {
        "id": int(row["id"]),
        "store_id": int(row["store_id"]),
        "month": row["month"],
        "service": service,
        "fee": fee,
        "housing": cents_to_yuan(row["housing_cents"]),
        "invoice_total": round(service + fee, 2),
        "lease_area": row["lease_area"] or "",
        "lease": cents_to_yuan(row["lease_cents"]),
        "lease_address": row["lease_address"] or "",
        "lease_period": row["lease_period"] or "",
        "apply_date": row["apply_date"] or "",
    }


def get_invoice_month(conn: sqlite3.Connection, store_id: int, month: str) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM invoice_months WHERE store_id=? AND month=?",
        (int(store_id), month),
    ).fetchone()
    if row is None:
        return empty_invoice(store_id, month)
    return _row_to_invoice(row)


def list_invoice_months(
    conn: sqlite3.Connection, store_ids: Sequence[int], month: str
) -> Dict[int, Dict[str, Any]]:
    ids = [int(i) for i in store_ids]
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM invoice_months WHERE month=? AND store_id IN ({q})",
        [month, *ids],
    ).fetchall()
    return {int(row["store_id"]): _row_to_invoice(row) for row in rows}


def save_invoice_month(
    conn: sqlite3.Connection,
    store_id: int,
    month: str,
    *,
    service: Any = 0,
    fee: Any = 0,
    housing: Any = 0,
    lease_area: str = "",
    lease: Any = 0,
    lease_address: str = "",
    lease_period: str = "",
    apply_date: str = "",
    user_id: int = 0,
) -> Dict[str, Any]:
    before = get_invoice_month(conn, store_id, month)
    payload = {
        "service_cents": yuan_to_cents(parse_money(service)),
        "fee_cents": yuan_to_cents(parse_money(fee)),
        "housing_cents": yuan_to_cents(parse_money(housing)),
        # 明细功能未启用，恒写空表；列保留只为老库兼容
        "details_json": "{}",
        "lease_area": (lease_area or "").strip()[:40],
        "lease_cents": yuan_to_cents(parse_money(lease)),
        "lease_address": (lease_address or "").strip()[:120],
        "lease_period": (lease_period or "").strip()[:40],
        "apply_date": (apply_date or "").strip()[:10],
        "updated_at": _now(),
    }
    conn.execute(
        """
        INSERT INTO invoice_months(
            store_id, month, service_cents, fee_cents, housing_cents, details_json,
            lease_area, lease_cents, lease_address, lease_period, apply_date, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(store_id, month) DO UPDATE SET
            service_cents=excluded.service_cents,
            fee_cents=excluded.fee_cents,
            housing_cents=excluded.housing_cents,
            details_json=excluded.details_json,
            lease_area=excluded.lease_area,
            lease_cents=excluded.lease_cents,
            lease_address=excluded.lease_address,
            lease_period=excluded.lease_period,
            apply_date=excluded.apply_date,
            updated_at=excluded.updated_at
        """,
        (
            int(store_id),
            month,
            payload["service_cents"],
            payload["fee_cents"],
            payload["housing_cents"],
            payload["details_json"],
            payload["lease_area"],
            payload["lease_cents"],
            payload["lease_address"],
            payload["lease_period"],
            payload["apply_date"],
            payload["updated_at"],
        ),
    )
    after = get_invoice_month(conn, store_id, month)
    if user_id:
        action = "create" if not before.get("id") else "update"
        _audit(
            conn,
            store_id=store_id,
            user_id=user_id,
            month=month,
            action=action,
            before=_snapshot(before) if before.get("id") else {},
            after=_snapshot(after),
        )
    return after


def save_invoice_from_form(
    conn: sqlite3.Connection, store_id: int, month: str, form, user_id: int = 0
) -> Dict[str, Any]:
    return save_invoice_month(
        conn,
        store_id,
        month,
        service=form.get("service"),
        fee=form.get("fee"),
        housing=form.get("housing"),
        apply_date=form.get("apply_date") or "",
        user_id=user_id,
    )


def invoice_diff(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    labels = {
        "service": "服务费",
        "fee": "手续费",
        "housing": "房补",
        "apply_date": "申请日",
    }
    parts = []
    for key, label in labels.items():
        b = before.get(key) or (0 if key != "apply_date" else "")
        a = after.get(key) or (0 if key != "apply_date" else "")
        if b == a:
            continue
        if key == "apply_date":
            parts.append(f"{label} {b or '空'}→{a or '空'}")
        else:
            parts.append(f"{label} {float(b):.2f}→{float(a):.2f}")
    return "；".join(parts) or "（无变化）"


def delete_invoice_month(
    conn: sqlite3.Connection, store_id: int, month: str, user_id: int = 0
) -> None:
    before = get_invoice_month(conn, store_id, month)
    conn.execute(
        "DELETE FROM invoice_months WHERE store_id=? AND month=?",
        (int(store_id), month),
    )
    if user_id and before.get("id"):
        _audit(
            conn,
            store_id=store_id,
            user_id=user_id,
            month=month,
            action="delete",
            before=_snapshot(before),
            after={},
        )
