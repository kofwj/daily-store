"""按现有微信/Excel 通报表版式，把各店日值+月累拼成一行一张表。"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Sequence


def yn(flag: Any) -> str:
    return "是" if int(flag or 0) else "否"


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _hex(rgb: tuple) -> str:
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(round(x)))) for x in rgb])


def scale_color(value: int, max_value: int, kind: str) -> str:
    """热力色阶：只用浅底，保证格子里永远是深色字。

    高值上限压在浅蓝，不再走到深蓝/藏青——深底叠深字才是看不清的根因。
    """
    t = 0.0 if max_value <= 0 else max(0.0, min(1.0, float(value) / max_value))
    if kind == "month":
        low, mid, high = (248, 250, 252), (219, 234, 254), (147, 197, 253)
    else:
        low, mid, high = (255, 255, 255), (239, 246, 255), (191, 219, 254)
    if t < 0.5:
        p = t * 2
        rgb = (_lerp(low[0], mid[0], p), _lerp(low[1], mid[1], p), _lerp(low[2], mid[2], p))
    else:
        p = (t - 0.5) * 2
        rgb = (_lerp(mid[0], high[0], p), _lerp(mid[1], high[1], p), _lerp(mid[2], high[2], p))
    return _hex(rgb)


def fmt_count(value: Any) -> str:
    """整数原样；小数留一位，贴近原表的 1.0 / 10.0。0 也显示（通报表不留空白）。"""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num == 0:
        return "0"
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.1f}"


def bisuan_total(values: Mapping[str, int]) -> int:
    return int(values.get("bisuan", 0) or 0) + int(values.get("bisuan_high", 0) or 0)


def build_row(
    store: Mapping[str, Any],
    *,
    day_ai: int,
    month_ai: int,
    day_bisuan: int,
    month_bisuan: int,
    submitted: bool,
) -> Dict[str, Any]:
    follow_ai = month_ai > 0
    follow_bisuan = month_bisuan > 0
    return {
        "store_id": store["id"],
        "code": store["code"],
        "region_group": store["region_group"] or "通泰",
        "city": store["city"] or "南通市",
        "name": store["name"],
        "mobile_code": store["mobile_code"] or "",
        "area_manager": store["area_manager"] or "",
        "store_manager": store["store_manager"] or "",
        "follow_ai": follow_ai,
        "follow_bisuan": follow_bisuan,
        "follow_ai_text": yn(follow_ai),
        "follow_bisuan_text": yn(follow_bisuan),
        "month_ai": month_ai,
        "month_bisuan": month_bisuan,
        "day_ai": day_ai,
        "day_bisuan": day_bisuan,
        "month_ai_text": fmt_count(month_ai),
        "month_bisuan_text": fmt_count(month_bisuan),
        "day_ai_text": fmt_count(day_ai),
        "day_bisuan_text": fmt_count(day_bisuan),
        "ai_zero": month_ai <= 0,
        "bisuan_zero": month_bisuan <= 0,
        "day_ai_zero": day_ai <= 0,
        "day_bisuan_zero": day_bisuan <= 0,
        "submitted": submitted,
        "month_ai_color": "",
        "month_bisuan_color": "",
        "day_ai_color": "",
        "day_bisuan_color": "",
    }


def apply_scales(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_month = max([0] + [max(int(r["month_ai"]), int(r["month_bisuan"])) for r in rows])
    max_day = max([0] + [max(int(r["day_ai"]), int(r["day_bisuan"])) for r in rows])
    for row in rows:
        if not row.get("submitted"):
            # 未交行不套热力，避免和已交的浅色格子撞在一起
            wait = ""
            row["month_ai_color"] = wait
            row["month_bisuan_color"] = wait
            row["day_ai_color"] = wait
            row["day_bisuan_color"] = wait
            continue
        row["month_ai_color"] = scale_color(row["month_ai"], max_month, "month")
        row["month_bisuan_color"] = scale_color(row["month_bisuan"], max_month, "month")
        row["day_ai_color"] = scale_color(row["day_ai"], max_day, "day")
        row["day_bisuan_color"] = scale_color(row["day_bisuan"], max_day, "day")
    return rows


def totals_row(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    month_ai = sum(int(r["month_ai"] or 0) for r in rows)
    month_bisuan = sum(int(r["month_bisuan"] or 0) for r in rows)
    day_ai = sum(int(r["day_ai"] or 0) for r in rows)
    day_bisuan = sum(int(r["day_bisuan"] or 0) for r in rows)
    ai_ok = sum(1 for r in rows if r.get("follow_ai"))
    bisuan_ok = sum(1 for r in rows if r.get("follow_bisuan"))
    return {
        "name": f"合计（{n} 店）",
        "follow_ai": ai_ok == n and n > 0,
        "follow_bisuan": bisuan_ok == n and n > 0,
        "follow_ai_text": f"{ai_ok}/{n}" if n else "0/0",
        "follow_bisuan_text": f"{bisuan_ok}/{n}" if n else "0/0",
        "month_ai": month_ai,
        "month_bisuan": month_bisuan,
        "day_ai": day_ai,
        "day_bisuan": day_bisuan,
        "month_ai_text": fmt_count(month_ai),
        "month_bisuan_text": fmt_count(month_bisuan),
        "day_ai_text": fmt_count(day_ai),
        "day_bisuan_text": fmt_count(day_bisuan),
        "month_ai_color": scale_color(month_ai, month_ai or 1, "month"),
        "month_bisuan_color": scale_color(month_bisuan, month_bisuan or 1, "month"),
        "day_ai_color": scale_color(day_ai, day_ai or 1, "day"),
        "day_bisuan_color": scale_color(day_bisuan, day_bisuan or 1, "day"),
    }


def month_label(biz_date: date) -> str:
    return f"{biz_date.month}月"


def day_label(biz_date: date) -> str:
    return f"{biz_date.month}月{biz_date.day}日"


def tsv(rows: Sequence[Mapping[str, Any]], biz_date: date) -> str:
    header1 = [
        "门店概况",
        "",
        "",
        "",
        "",
        "",
        "跟进",
        "",
        month_label(biz_date),
        "",
        day_label(biz_date),
        "",
    ]
    header2 = [
        "大区",
        "地市",
        "门店名称",
        "移动编码",
        "区域经理",
        "店长",
        "AI跟进",
        "笔算破零跟进",
        "AI手机合约",
        "笔算业务",
        "AI手机合约",
        "笔算业务",
    ]
    lines = ["\t".join(header1), "\t".join(header2)]
    for row in rows:
        lines.append(_tsv_data_line(row))
    if rows:
        total = totals_row(rows)
        lines.append(
            "\t".join(
                [
                    "",
                    "",
                    total["name"],
                    "",
                    "",
                    "",
                    total["follow_ai_text"],
                    total["follow_bisuan_text"],
                    total["month_ai_text"],
                    total["month_bisuan_text"],
                    total["day_ai_text"],
                    total["day_bisuan_text"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _tsv_data_line(row: Mapping[str, Any]) -> str:
    return "\t".join(
        [
            str(row.get("region_group") or ""),
            str(row.get("city") or ""),
            str(row.get("name") or ""),
            str(row.get("mobile_code") or ""),
            str(row.get("area_manager") or ""),
            str(row.get("store_manager") or ""),
            str(row.get("follow_ai_text") or ""),
            str(row.get("follow_bisuan_text") or ""),
            str(row.get("month_ai_text") or ""),
            str(row.get("month_bisuan_text") or ""),
            str(row.get("day_ai_text") or ""),
            str(row.get("day_bisuan_text") or ""),
        ]
    )


def csv_rows(rows: Sequence[Mapping[str, Any]], biz_date: date) -> List[List[str]]:
    out = [
        [
            "大区",
            "地市",
            "门店名称",
            "移动编码",
            "区域经理",
            "店长",
            "AI跟进",
            "笔算破零跟进",
            f"{month_label(biz_date)}AI手机合约",
            f"{month_label(biz_date)}笔算业务",
            f"{day_label(biz_date)}AI手机合约",
            f"{day_label(biz_date)}笔算业务",
        ],
        *[
            [
                row["region_group"],
                row["city"],
                row["name"],
                row["mobile_code"],
                row["area_manager"],
                row["store_manager"],
                row["follow_ai_text"],
                row["follow_bisuan_text"],
                row["month_ai_text"],
                row["month_bisuan_text"],
                row["day_ai_text"],
                row["day_bisuan_text"],
            ]
            for row in rows
        ],
    ]
    if rows:
        total = totals_row(rows)
        out.append(
            [
                "",
                "",
                total["name"],
                "",
                "",
                "",
                total["follow_ai_text"],
                total["follow_bisuan_text"],
                total["month_ai_text"],
                total["month_bisuan_text"],
                total["day_ai_text"],
                total["day_bisuan_text"],
            ]
        )
    return out
