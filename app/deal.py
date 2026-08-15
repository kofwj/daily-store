"""成交播报：每卖一台填一单，复制进群，不入库。"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping


def yn(value: str, yes: str, no: str) -> str:
    return yes if str(value or "").strip() in {"1", "是", "yes", "true", "Y"} else no


def is_today_deal(row: Any, today: date) -> bool:
    """只有业务日是当天的成交才能改/删。"""
    biz = ""
    if row is None:
        return False
    if hasattr(row, "keys") and "biz_date" in row.keys():
        biz = str(row["biz_date"] or "")
    elif isinstance(row, Mapping):
        biz = str(row.get("biz_date") or "")
    return biz[:10] == today.isoformat()


def mask_phone(phone: str, *, hide_tail: bool = True) -> str:
    raw = (phone or "").strip()
    if not hide_tail or len(raw) < 4:
        return raw
    return raw[:-4] + "****"


def render_deal(
    store_name: str,
    *,
    model: str = "",
    phone: str = "",
    spend: str = "",
    hall_query: str = "1",
    recommend: str = "",
    closed: str = "0",
    student: str = "0",
    opener: str = "",
    note: str = "",
    show_phone: str = "0",
) -> str:
    store = (store_name or "").strip() or "未选门店"
    model = (model or "").strip()
    phone = mask_phone(phone, hide_tail=yn(show_phone, "1", "0") != "1")
    spend = (spend or "").strip()
    recommend = (recommend or "").strip()
    opener = (opener or "").strip()
    note = " ".join((note or "").split())

    status = "成交" if yn(closed, "1", "0") == "1" else "未成交"
    lines = [f"{store} · {status}"]

    mid = [part for part in (model, phone, f"消费{spend}" if spend else "") if part]
    if mid:
        lines.append("｜".join(mid))

    tags = [
        yn(hall_query, "掌厅已查", "未查掌厅"),
        f"荐{recommend}" if recommend else "",
        yn(student, "中高考", "非中高考"),
    ]
    tags = [t for t in tags if t]
    if tags:
        lines.append(" · ".join(tags))
    if opener:
        lines.append(f"开口 {opener}")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines) + "\n"


def form_values(raw: Mapping[str, str] | None = None, *, posted: bool = False) -> dict:
    raw = raw or {}
    default_on = "0" if posted else "1"
    return {
        "model": (raw.get("model") or "").strip(),
        "phone": (raw.get("phone") or "").strip(),
        "spend": (raw.get("spend") or "").strip(),
        "hall_query": raw.get("hall_query") or default_on,
        "recommend": (raw.get("recommend") or "").strip(),
        "closed": raw.get("closed") or "0",
        "student": raw.get("student") or "0",
        "show_phone": raw.get("show_phone") or "0",
        "opener": (raw.get("opener") or "").strip(),
        "note": (raw.get("note") or "").strip(),
        "deal_id": (raw.get("deal_id") or "").strip(),
    }


def form_from_row(row) -> dict:
    return {
        "model": row["model"] or "",
        "phone": row["phone"] or "",
        "spend": row["spend"] or "",
        "hall_query": "1" if int(row["hall_query"] or 0) else "0",
        "recommend": row["recommend"] or "",
        "closed": "1" if int(row["closed"] or 0) else "0",
        "student": "1" if int(row["student"] or 0) else "0",
        "show_phone": "0",
        "opener": row["opener"] or "",
        "note": row["note"] or "",
        "deal_id": str(row["id"]),
    }
