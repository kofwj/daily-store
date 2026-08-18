"""酬金/租赁开票台账：按店按月记服务费、手续费、明细、租赁。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any, Dict, Optional, Sequence

from .db_advances import cents_to_yuan, parse_money, yuan_to_cents
from .db_core import _now

DETAIL_ITEMS = (
    ("hao_new", 3, "当月新增放号获利"),
    ("hao_high", 4, "高价值放号"),
    ("hao_month", 5, "当月放号"),
    ("hao_2", 6, "二返获利"),
    ("hao_3", 7, "三返获利"),
    ("hao_4", 8, "四返获利"),
    ("hao_5", 9, "五返获利"),
    ("term_2", 10, "合约机二返"),
    ("term_fill", 11, "合约机补结"),
    ("term_34", 12, "购机3-4返"),
    ("bb_all", 13, "当月整体宽带"),
    ("bb_new", 14, "新入网办理宽带获利"),
    ("bb_old", 15, "存量办理宽带获利"),
    ("bb_point", 16, "宽带积分"),
    ("biz_change", 17, "套餐变更"),
    ("biz_point", 18, "套餐变更积分"),
    ("biz_group", 19, "存量用户入融合群"),
    ("biz_vnet", 20, "家庭V网"),
    ("biz_camp", 21, "营销活动"),
    ("biz_back", 22, "回流"),
    ("add_flow", 23, "数据流量包"),
    ("add_new", 24, "新业务"),
    ("add_soft", 25, "软件安装"),
    ("add_tv", 26, "互联网电视和提速包"),
    ("collect", 27, "代收话费"),
    ("other", 28, "其他业务"),
)

DETAIL_GROUPS = (
    ("放号酬金", DETAIL_ITEMS[0:7]),
    ("终端酬金", DETAIL_ITEMS[7:10]),
    ("宽带酬金", DETAIL_ITEMS[10:14]),
    ("业务受理", DETAIL_ITEMS[14:20]),
    ("增值业务", DETAIL_ITEMS[20:24]),
    ("代收 / 其他", DETAIL_ITEMS[24:26]),
)


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


def _row_to_invoice(row) -> Dict[str, Any]:
    details = {}
    raw = row["details_json"] or "{}"
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            details = {str(k): float(v) for k, v in loaded.items() if v not in (None, "")}
    except (TypeError, ValueError, json.JSONDecodeError):
        details = {}
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
        "details": details,
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
    details: Optional[Dict[str, float]] = None,
    lease_area: str = "",
    lease: Any = 0,
    lease_address: str = "",
    lease_period: str = "",
    apply_date: str = "",
) -> Dict[str, Any]:
    payload = {
        "service_cents": yuan_to_cents(parse_money(service)),
        "fee_cents": yuan_to_cents(parse_money(fee)),
        "housing_cents": yuan_to_cents(parse_money(housing)),
        "details_json": json.dumps(details or {}, ensure_ascii=False),
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
    return get_invoice_month(conn, store_id, month)


def save_invoice_from_form(conn: sqlite3.Connection, store_id: int, month: str, form) -> Dict[str, Any]:
    return save_invoice_month(
        conn,
        store_id,
        month,
        service=form.get("service"),
        fee=form.get("fee"),
        housing=form.get("housing"),
        apply_date=form.get("apply_date") or "",
    )


def delete_invoice_month(conn: sqlite3.Connection, store_id: int, month: str) -> None:
    conn.execute(
        "DELETE FROM invoice_months WHERE store_id=? AND month=?",
        (int(store_id), month),
    )
