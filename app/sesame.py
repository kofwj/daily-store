"""芝麻服务费官方明细：解析、对店、导入垫资。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from secrets import token_hex
from typing import Any, Dict, Iterable, List, Optional, Sequence

from openpyxl import load_workbook

from . import db_advances, db_core

MAX_IMPORT_BYTES = 4 * 1024 * 1024
HEADERS = (
    "流水号",
    "订单号",
    "省份",
    "城市",
    "地区",
    "商户号",
    "营业执照号",
    "营业执照名称",
    "门店编码",
    "门店名称",
    "门店类型",
    "统计月份",
    "订单金额",
    "服务费金额",
    "状态",
    "备注",
    "创建时间",
)


def mobile_code_of(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    if text.startswith("JSCM_"):
        text = text[5:]
    return text.strip()


def parse_created(raw: Any) -> Optional[date]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if text.endswith(".0"):
        text = text[:-2]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _cell(row: Sequence[Any], idx: int) -> Any:
    return row[idx] if idx < len(row) else None


def parse_sesame_xlsx(data: bytes) -> List[Dict[str, Any]]:
    if not data:
        raise ValueError("没有文件")
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError("文件不能超过 4 MiB")
    try:
        # 官方表标题行合并 A1:Q1，read_only 会把维度看成 1x1，只读到标题。
        wb = load_workbook(BytesIO(data), data_only=True, read_only=False)
    except Exception as exc:
        raise ValueError("打不开这份 Excel") from exc
    try:
        ws = wb.active
        rows = list(ws.iter_rows(min_row=1, max_row=max(ws.max_row or 1, 20), max_col=20, values_only=True))
    finally:
        wb.close()
    header_idx = None
    for i, row in enumerate(rows[:20]):
        labels = [str(v or "").strip() for v in row]
        if "流水号" in labels and "门店编码" in labels and "服务费金额" in labels:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("不是芝麻服务费明细（缺流水号 / 门店编码 / 服务费金额）")
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows[header_idx + 1 :]:
        ext_id = str(_cell(row, 0) or "").strip()
        if not ext_id:
            continue
        if ext_id in seen:
            continue
        seen.add(ext_id)
        fee_raw = _cell(row, 13)
        try:
            platform_fee = db_advances.parse_money(fee_raw)
        except ValueError as exc:
            raise ValueError(f"流水 {ext_id} 的服务费不是数字") from exc
        try:
            order_amt = db_advances.parse_money(_cell(row, 12))
        except ValueError:
            order_amt = 0.0
        biz_date = parse_created(_cell(row, 16))
        note = str(_cell(row, 15) or "")
        refund = "退款" in note or ext_id.endswith("_R")
        out.append(
            {
                "ext_id": ext_id[:80],
                "order_no": str(_cell(row, 1) or "").strip()[:80],
                "city": str(_cell(row, 3) or "").strip(),
                "area": str(_cell(row, 4) or "").strip(),
                "merchant": str(_cell(row, 7) or "").strip(),
                "store_code": str(_cell(row, 8) or "").strip(),
                "mobile_code": mobile_code_of(_cell(row, 8)),
                "platform_store": str(_cell(row, 9) or "").strip(),
                "month": str(_cell(row, 11) or "").strip(),
                "order_amt": order_amt,
                "platform_fee": platform_fee,
                "amount": round(-platform_fee, 2),
                "status": str(_cell(row, 14) or "").strip(),
                "note": note,
                "refund": refund,
                "biz_date": biz_date.isoformat() if biz_date else "",
            }
        )
    if not out:
        raise ValueError("明细里没有流水")
    return out


def _store_map(stores: Iterable[Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for store in stores:
        code = (store["mobile_code"] or "").strip()
        if code:
            out[code] = store
    return out


def classify_sesame_rows(conn, rows: Sequence[Dict[str, Any]], stores: Sequence[Any]) -> Dict[str, List[Dict[str, Any]]]:
    by_code = _store_map(stores)
    ext_ids = [r["ext_id"] for r in rows]
    existing = set()
    if ext_ids:
        chunk = 400
        for i in range(0, len(ext_ids), chunk):
            part = ext_ids[i : i + chunk]
            marks = ",".join("?" * len(part))
            existing.update(
                row["ext_id"]
                for row in conn.execute(
                    f"SELECT ext_id FROM advance_posts WHERE ext_id IN ({marks})",
                    part,
                )
            )
    ready: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        store = by_code.get(item["mobile_code"])
        if store is not None:
            item["store_id"] = int(store["id"])
            item["store_name"] = store["short_name"] or store["name"]
        else:
            item["store_id"] = None
            item["store_name"] = ""
        if item["status"] and item["status"] != "处理成功":
            item["reason"] = f"状态是{item['status']}"
            ignored.append(item)
            continue
        if not item.get("biz_date"):
            item["reason"] = "没有创建时间"
            ignored.append(item)
            continue
        if not item["mobile_code"] or store is None:
            item["reason"] = "对不上门店编码"
            unmatched.append(item)
            continue
        if item["ext_id"] in existing:
            item["reason"] = "已导入"
            skipped.append(item)
            continue
        if abs(db_advances.yuan_to_cents(item["amount"])) < 1:
            item["reason"] = "金额为 0"
            ignored.append(item)
            continue
        ready.append(item)
    return {"ready": ready, "skipped": skipped, "unmatched": unmatched, "ignored": ignored}


def import_sesame_rows(conn, rows: Sequence[Dict[str, Any]], *, user_id: int) -> int:
    n = 0
    for row in rows:
        biz = row["biz_date"]
        if not isinstance(biz, date):
            biz = parse_created(biz)
        if biz is None:
            raise ValueError(f"流水 {row.get('ext_id') or ''} 没有创建时间")
        note = "芝麻服务费退款" if row.get("refund") else "芝麻服务费"
        order_no = row.get("order_no") or ""
        if order_no:
            note = f"{note} {order_no}"
        db_advances.record_advance(
            conn,
            store_id=int(row["store_id"]),
            user_id=user_id,
            biz_date=biz,
            sesame=row["amount"],
            note=note[:500],
            source="sesame",
            ext_id=row["ext_id"],
        )
        n += 1
    return n


def _preview_dir() -> Path:
    path = Path(db_core.DATA_DIR) / "tmp" / "sesame"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_preview(preview: Dict[str, Any]) -> str:
    token = token_hex(16)
    dest = _preview_dir() / f"{token}.json"
    dest.write_text(json.dumps(preview, ensure_ascii=False, default=str), encoding="utf-8")
    _prune_previews()
    return token


def load_preview(token: str) -> Optional[Dict[str, Any]]:
    name = (token or "").strip()
    if not name or any(ch in name for ch in "/\\."):
        return None
    path = _preview_dir() / f"{name}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def drop_preview(token: str) -> None:
    name = (token or "").strip()
    if not name or any(ch in name for ch in "/\\."):
        return
    path = _preview_dir() / f"{name}.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _prune_previews() -> None:
    cutoff = datetime.now(db_core.TZ) - timedelta(hours=2)
    for path in _preview_dir().glob("*.json"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, db_core.TZ)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass
