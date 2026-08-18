"""酬金/租赁开票申请：主体资料 + 按样表导出。"""

from __future__ import annotations

import json
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import load_workbook

from . import db
from .db_invoice import DETAIL_ITEMS, get_invoice_month, month_key
from .helpers import company_names

TEMPLATE = Path(__file__).resolve().parent / "invoice_templates" / "invoice_apply.xlsx"

PARTY_KEYS = ("nt", "tz")
PARTY_FIELDS = (
    "seller_name",
    "seller_tax",
    "seller_addr",
    "seller_bank",
    "buyer_name",
    "buyer_tax",
    "buyer_addr",
    "buyer_bank",
    "email",
)
# 购买方是移动地市公司，样表里已固定；销售方留给设置填执照。
DEFAULT_PARTIES = {
    "nt": {
        "seller_name": "",
        "seller_tax": "",
        "seller_addr": "",
        "seller_bank": "",
        "buyer_name": "中国移动通信集团江苏有限公司南通分公司",
        "buyer_tax": "91320600714057244E",
        "buyer_addr": "江苏省南通市园林路88号0513-68589029",
        "buyer_bank": "中国工商银行股份有限公司南通崇川支行\n1111821209000012463",
        "email": "15162706007@139.com",
    },
    "tz": {
        "seller_name": "",
        "seller_tax": "",
        "seller_addr": "",
        "seller_bank": "",
        "buyer_name": "中国移动通信集团江苏有限公司泰州分公司",
        "buyer_tax": "913212007039775394",
        "buyer_addr": "泰州市医药高新区泰州大道398号   13800523222",
        "buyer_bank": "招商银行北京分行营业部   8888014600006654",
        "email": "15162706007@139.com",
    },
}
_LIMITS = {
    "seller_name": 40,
    "seller_tax": 32,
    "seller_addr": 80,
    "seller_bank": 80,
    "buyer_name": 40,
    "buyer_tax": 32,
    "buyer_addr": 80,
    "buyer_bank": 80,
    "email": 60,
    "handler": 20,
}


def party_key_for_store(store) -> str:
    city = (store["city"] or "").strip()
    return "tz" if "泰州" in city else "nt"


def invoice_store_name(store) -> str:
    raw = ""
    if hasattr(store, "keys") and "invoice_name" in store.keys():
        raw = store["invoice_name"] or ""
    return (raw or "").strip() or (store["name"] or "").strip()


def _clip(value: Any, limit: int) -> str:
    return str(value or "").replace("\r\n", "\n").strip()[:limit]


def default_party(key: str, conn=None) -> Dict[str, str]:
    base = dict(DEFAULT_PARTIES.get(key) or DEFAULT_PARTIES["nt"])
    names = company_names(conn)
    if not base["seller_name"]:
        base["seller_name"] = names.get(key) or ""
    return base


def _load_party(conn, key: str) -> Dict[str, str]:
    out = default_party(key, conn)
    raw = db.get_setting(conn, f"invoice_party_{key}", "")
    if not raw:
        return out
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return out
    if not isinstance(loaded, dict):
        return out
    for field in PARTY_FIELDS:
        if loaded.get(field):
            out[field] = _clip(loaded.get(field), _LIMITS[field])
    return out


def invoice_parties(conn=None) -> Dict[str, Dict[str, str]]:
    if conn is None:
        with db.get_db() as owned:
            return invoice_parties(owned)
    return {key: _load_party(conn, key) for key in PARTY_KEYS}


def invoice_handler(conn=None) -> str:
    if conn is None:
        with db.get_db() as owned:
            return invoice_handler(owned)
    return _clip(db.get_setting(conn, "invoice_handler", ""), _LIMITS["handler"])


def parse_parties_form(form) -> Dict[str, Dict[str, str]]:
    out = {}
    for key in PARTY_KEYS:
        item = {}
        for field in PARTY_FIELDS:
            item[field] = _clip(form.get(f"{key}_{field}"), _LIMITS[field])
        out[key] = item
    return out


def save_invoice_settings(conn, form) -> None:
    parties = parse_parties_form(form)
    for key, item in parties.items():
        db.set_setting(conn, f"invoice_party_{key}", json.dumps(item, ensure_ascii=False))
    db.set_setting(conn, "invoice_handler", _clip(form.get("handler"), _LIMITS["handler"]))


def _as_date(raw: str, fallback: date) -> date:
    text = (raw or "").strip()[:10]
    if not text:
        return fallback
    try:
        return date.fromisoformat(text)
    except ValueError:
        return fallback


def _set(ws, addr: str, value) -> None:
    ws[addr] = value if value not in ("", None) else None


def _fill_party_block(ws, party: Mapping[str, str]) -> None:
    _set(ws, "B1", party.get("seller_name"))
    _set(ws, "B2", party.get("buyer_name"))
    _set(ws, "B3", party.get("buyer_tax"))
    _set(ws, "B4", party.get("buyer_addr"))
    _set(ws, "B5", party.get("buyer_bank"))


def build_invoice_xlsx(
    conn,
    store,
    as_of: date,
    invoice: Optional[Mapping[str, Any]] = None,
) -> bytes:
    if not TEMPLATE.is_file():
        raise FileNotFoundError("缺少开票申请样表")
    month = month_key(as_of)
    rec = dict(invoice or get_invoice_month(conn, int(store["id"]), month))
    party = _load_party(conn, party_key_for_store(store))
    handler = invoice_handler(conn)
    apply_on = _as_date(rec.get("apply_date") or "", as_of)
    store_title = invoice_store_name(store)
    lease_area = rec.get("lease_area") or (store["lease_area"] if "lease_area" in store.keys() else "") or ""
    lease_address = rec.get("lease_address") or (
        store["lease_address"] if "lease_address" in store.keys() else ""
    ) or ""
    lease_period = rec.get("lease_period") or (
        store["lease_period"] if "lease_period" in store.keys() else ""
    ) or ""
    wb = load_workbook(TEMPLATE)
    comm = wb["酬金开票申请"]
    _fill_party_block(comm, party)
    comm["E9"] = rec.get("service") or None
    comm["E10"] = rec.get("fee") or None
    comm["E9"].number_format = "0.00"
    comm["E10"].number_format = "0.00"
    comm["E11"].number_format = "0.00"
    _set(comm, "B12", store_title)
    _set(comm, "E14", handler)
    comm["E15"] = datetime(apply_on.year, apply_on.month, apply_on.day)
    comm["E15"].number_format = "YYYY/M/D"
    email = party.get("email") or ""
    comm["A25"] = f"接收数电发票的邮箱号：{email}" if email else "接收数电发票的邮箱号："

    lease = wb["租赁发票开票申请"]
    _fill_party_block(lease, party)
    area_text = str(lease_area or "").strip()
    if area_text:
        try:
            lease["C9"] = float(area_text)
        except ValueError:
            lease["C9"] = area_text
    lease["D9"] = rec.get("lease") or None
    lease["D9"].number_format = "0.00"
    lease["D11"].number_format = "0.00"
    _set(lease, "B12", store_title)
    if str(lease_address).strip():
        lease["B13"] = str(lease_address).strip()
    _set(lease, "B14", lease_period)
    _set(lease, "D16", handler)
    lease["D17"] = datetime(apply_on.year, apply_on.month, apply_on.day)
    lease["D17"].number_format = "YYYY/M/D"
    lease["A28"] = f"接收数电发票的邮箱号：{email}" if email else "接收数电发票的邮箱号："

    detail = wb["明细"]
    detail["B1"] = int(as_of.strftime("%Y%m"))
    amounts = rec.get("details") or {}
    for key, row, _name in DETAIL_ITEMS:
        value = amounts.get(key)
        if value in (None, "", 0, 0.0):
            continue
        cell = detail[f"C{row}"]
        cell.value = float(value)
        cell.number_format = "0.00"
    detail["C29"].number_format = "0.00"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def invoice_filename(store, as_of: date) -> str:
    short = (store["short_name"] or store["name"] or "store").strip()
    return f"invoice_{as_of.strftime('%Y%m')}_{short}.xlsx"


def build_invoice_zip(conn, stores, as_of: date) -> bytes:
    month = month_key(as_of)
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        used = set()
        for store in stores:
            name = invoice_filename(store, as_of)
            if name in used:
                name = f"invoice_{as_of.strftime('%Y%m')}_{store['id']}.xlsx"
            used.add(name)
            data = build_invoice_xlsx(conn, store, as_of, get_invoice_month(conn, int(store["id"]), month))
            zf.writestr(name, data)
    return buf.getvalue()
