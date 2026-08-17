"""按现有微信/Excel 通报表版式，把各店日值+月累拼成一行一张表。"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Sequence, Tuple


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


def fmt_metric(code: str, stored: Any) -> str:
    from .metrics_seed import format_stored

    return format_stored(code, stored)


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


def _store_short(store: Mapping[str, Any]) -> str:
    """安全取门店简称：兼容普通 dict 和 sqlite3.Row。"""
    try:
        return (store["short_name"] or "").strip()
    except (KeyError, TypeError, IndexError):
        return ""


def build_row(
    store: Mapping[str, Any],
    *,
    day_ai: int,
    month_ai: int,
    day_bisuan: int,
    month_bisuan: int,
    day_coin: int = 0,
    month_coin: int = 0,
    submitted: bool,
    month_bisuan_official: Any = None,
) -> Dict[str, Any]:
    follow_ai = month_ai > 0
    follow_bisuan = month_bisuan > 0
    return {
        "store_id": store["id"],
        "code": store["code"],
        "region_group": store["region_group"] or "通泰",
        "city": store["city"] or "南通市",
        "name": store["name"],
        "short_name": _store_short(store),
        "mobile_code": store["mobile_code"] or "",
        "area_manager": store["area_manager"] or "",
        "store_manager": store["store_manager"] or "",
        "follow_ai": follow_ai,
        "follow_bisuan": follow_bisuan,
        "follow_ai_text": yn(follow_ai),
        "follow_bisuan_text": yn(follow_bisuan),
        "month_coin": month_coin,
        "month_ai": month_ai,
        "month_bisuan": month_bisuan,
        "day_coin": day_coin,
        "day_ai": day_ai,
        "day_bisuan": day_bisuan,
        "month_coin_text": fmt_metric("coin_cut_old", month_coin),
        "month_ai_text": fmt_metric("ai_contract", month_ai),
        "month_bisuan_text": fmt_metric("bisuan", month_bisuan),
        "day_coin_text": fmt_metric("coin_cut_old", day_coin),
        "day_ai_text": fmt_metric("ai_contract", day_ai),
        "day_bisuan_text": fmt_metric("bisuan", day_bisuan),
        "ai_zero": month_ai <= 0,
        "bisuan_zero": month_bisuan <= 0,
        "day_ai_zero": day_ai <= 0,
        "day_bisuan_zero": day_bisuan <= 0,
        "submitted": submitted,
        "month_coin_color": "",
        "month_ai_color": "",
        "month_bisuan_color": "",
        "month_bisuan_official": "" if month_bisuan_official in (None, "") else fmt_metric("bisuan", month_bisuan_official),
        "_month_bisuan_official_stored": month_bisuan_official if month_bisuan_official is not None else None,
        "month_bisuan_diff": "",
        "month_bisuan_diff_signed": "",
        "month_bisuan_diff_color": "",
        "day_coin_color": "",
        "day_ai_color": "",
        "day_bisuan_color": "",
    }


def apply_scales(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_month = max(
        [0]
        + [
            max(int(r["month_ai"]), int(r["month_bisuan"]), int(r.get("month_coin") or 0))
            for r in rows
        ]
    )
    max_day = max(
        [0]
        + [
            max(int(r["day_ai"]), int(r["day_bisuan"]), int(r.get("day_coin") or 0))
            for r in rows
        ]
    )
    for row in rows:
        official = row.get("_month_bisuan_official_stored")
        if official is not None and official != "":
            try:
                diff = int(official) - int(row["month_bisuan"])
            except (TypeError, ValueError):
                diff = 0
            row["month_bisuan_diff"] = fmt_metric("bisuan", diff)
            sign = "+" if diff > 0 else ""
            row["month_bisuan_diff_signed"] = f"{sign}{fmt_metric('bisuan', diff)}"
            row["month_bisuan_diff_color"] = "#ecfdf3" if diff >= 0 else "#fef3f2"
        if not row.get("submitted"):
            # 未交行不套热力，避免和已交的浅色格子撞在一起
            wait = ""
            row["month_coin_color"] = wait
            row["month_ai_color"] = wait
            row["month_bisuan_color"] = wait
            row["day_coin_color"] = wait
            row["day_ai_color"] = wait
            row["day_bisuan_color"] = wait
            continue
        row["month_coin_color"] = scale_color(int(row.get("month_coin") or 0), max_month, "month")
        row["month_ai_color"] = scale_color(row["month_ai"], max_month, "month")
        row["month_bisuan_color"] = scale_color(row["month_bisuan"], max_month, "month")
        row["day_coin_color"] = scale_color(int(row.get("day_coin") or 0), max_day, "day")
        row["day_ai_color"] = scale_color(row["day_ai"], max_day, "day")
        row["day_bisuan_color"] = scale_color(row["day_bisuan"], max_day, "day")
    return rows


def totals_row(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    month_coin = sum(int(r.get("month_coin") or 0) for r in rows)
    month_ai = sum(int(r["month_ai"] or 0) for r in rows)
    month_bisuan = sum(int(r["month_bisuan"] or 0) for r in rows)
    day_coin = sum(int(r.get("day_coin") or 0) for r in rows)
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
        "month_coin": month_coin,
        "month_ai": month_ai,
        "month_bisuan": month_bisuan,
        "day_coin": day_coin,
        "day_ai": day_ai,
        "day_bisuan": day_bisuan,
        "month_coin_text": fmt_metric("coin_cut_old", month_coin),
        "month_ai_text": fmt_metric("ai_contract", month_ai),
        "month_bisuan_text": fmt_metric("bisuan", month_bisuan),
        "day_coin_text": fmt_metric("coin_cut_old", day_coin),
        "day_ai_text": fmt_metric("ai_contract", day_ai),
        "day_bisuan_text": fmt_metric("bisuan", day_bisuan),
        "month_coin_color": scale_color(month_coin, month_coin or 1, "month"),
        "month_ai_color": scale_color(month_ai, month_ai or 1, "month"),
        "month_bisuan_color": scale_color(month_bisuan, month_bisuan or 1, "month"),
        "day_coin_color": scale_color(day_coin, day_coin or 1, "day"),
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
        "",
        day_label(biz_date),
        "",
        "",
    ]
    header2 = [
        "大区",
        "地市",
        "门店名称",
        "移动编码",
        "区域经理",
        "店长",
        "AI破0",
        "笔算破0",
        "AI手机合约",
        "笔算业务",
        "金币直降",
        "AI手机合约",
        "笔算业务",
        "金币直降",
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
                    total["month_coin_text"],
                    total["day_ai_text"],
                    total["day_bisuan_text"],
                    total["day_coin_text"],
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
            str(row.get("month_coin_text") or ""),
            str(row.get("day_ai_text") or ""),
            str(row.get("day_bisuan_text") or ""),
            str(row.get("day_coin_text") or ""),
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
            "AI破0",
            "笔算破0",
            f"{month_label(biz_date)}AI手机合约",
            f"{month_label(biz_date)}笔算业务",
            f"{month_label(biz_date)}金币直降",
            f"{day_label(biz_date)}AI手机合约",
            f"{day_label(biz_date)}笔算业务",
            f"{day_label(biz_date)}金币直降",
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
                row["month_coin_text"],
                row["day_ai_text"],
                row["day_bisuan_text"],
                row["day_coin_text"],
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
                total["month_coin_text"],
                total["day_ai_text"],
                total["day_bisuan_text"],
                total["day_coin_text"],
            ]
        )
    return out


def _row_name(row: Mapping[str, Any]) -> str:
    return (row.get("short_name") or row.get("name") or "").strip() or "未命名"


def _metric(row: Mapping[str, Any], key: str) -> int:
    return int(row.get(key) or 0)


def _join_names(names: Sequence[str]) -> str:
    return "、".join(names)


def _hitters(rows: Sequence[Mapping[str, Any]], key: str) -> List[str]:
    names = [_row_name(r) for r in rows if _metric(r, key) > 0]
    # 保序去重，方便贴群点名
    seen = set()
    out: List[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _best_name(rows: Sequence[Mapping[str, Any]], key: str) -> str:
    ranked = sorted(rows, key=lambda r: _metric(r, key), reverse=True)
    if not ranked or _metric(ranked[0], key) <= 0:
        return ""
    return _row_name(ranked[0])


def summary(
    rows: Sequence[Mapping[str, Any]],
    biz_date: date,
    title_city: str = "",
    *,
    day_deal: Tuple[int, int] = (0, 0),
    month_deal: Tuple[int, int] = (0, 0),
) -> str:
    """可贴群复盘：今日销量 + 单项表扬 + 本月累计 / 标杆。"""
    if not rows:
        return f"{biz_date.isoformat()} 暂无门店通报数据。"
    total = totals_row(rows)
    month_ai, month_bisuan, month_coin = (
        int(total["month_ai"] or 0),
        int(total["month_bisuan"] or 0),
        int(total.get("month_coin") or 0),
    )
    day_ai, day_bisuan, day_coin = (
        int(total["day_ai"] or 0),
        int(total["day_bisuan"] or 0),
        int(total.get("day_coin") or 0),
    )
    day_count, day_closed = int(day_deal[0] or 0), int(day_deal[1] or 0)
    month_count, month_closed = int(month_deal[0] or 0), int(month_deal[1] or 0)
    ranked = sorted(
        rows,
        key=lambda r: _metric(r, "month_ai") + _metric(r, "month_bisuan") + _metric(r, "month_coin"),
        reverse=True,
    )
    top = ranked[0]
    top_name = _row_name(top)
    head = biz_date.isoformat()
    if title_city:
        head = f"{head} {title_city}vivo零售运营中心"

    ai_hit = _hitters(rows, "day_ai")
    bisuan_hit = _hitters(rows, "day_bisuan")
    coin_hit = _hitters(rows, "day_coin")
    triple = [
        _row_name(r)
        for r in rows
        if _metric(r, "day_ai") > 0 and _metric(r, "day_bisuan") > 0 and _metric(r, "day_coin") > 0
    ]
    praise: List[str] = []
    if ai_hit:
        praise.append(f"AI有量：{_join_names(ai_hit)}")
    if bisuan_hit:
        praise.append(f"笔算有量：{_join_names(bisuan_hit)}")
    if coin_hit:
        praise.append(f"直降有量：{_join_names(coin_hit)}")
    if triple:
        praise.append(f"今日三项都有：{_join_names(triple)}")

    month_ai_best = _best_name(rows, "month_ai")
    month_bisuan_best = _best_name(rows, "month_bisuan")
    month_coin_best = _best_name(rows, "month_coin")
    month_bits = []
    if month_ai_best:
        month_bits.append(f"AI {month_ai_best}")
    if month_bisuan_best:
        month_bits.append(f"笔算 {month_bisuan_best}")
    if month_coin_best:
        month_bits.append(f"直降 {month_coin_best}")

    day_bisuan_text = fmt_metric("bisuan", day_bisuan)
    month_bisuan_text = fmt_metric("bisuan", month_bisuan)
    top_bisuan_text = fmt_metric("bisuan", _metric(top, "month_bisuan"))
    lines = [
        head,
        "【今日】",
        f"销量：AI {day_ai} · 笔算 {day_bisuan_text} · 直降 {day_coin}",
        f"触客：{day_count} 笔（成交 {day_closed}）",
    ]
    if praise:
        lines.append("表扬")
        lines.extend(praise)
    else:
        lines.append("表扬：今日暂无单项破零，继续加油")
    lines.extend(
        [
            "【本月】",
            f"累计：AI {month_ai} · 笔算 {month_bisuan_text} · 直降 {month_coin}",
            f"触客：{month_count} 笔（成交 {month_closed}）",
            f"综合标杆：{top_name}（AI {top['month_ai']}，笔算 {top_bisuan_text}，直降 {top.get('month_coin') or 0}）",
        ]
    )
    if month_bits:
        lines.append("单项第一：" + " · ".join(month_bits))
    # 有移动官方笔算时，列出系统 vs 官方对比，方便贴群
    calibrate_lines = []
    for r in rows:
        official = r.get("_month_bisuan_official_stored")
        if official is None or official == "":
            continue
        try:
            off_i = int(official)
            sys_i = _metric(r, "month_bisuan")
            diff = off_i - sys_i
        except (TypeError, ValueError):
            continue
        sign = "+" if diff > 0 else ""
        calibrate_lines.append(
            f"{_row_name(r)} 系统{fmt_metric('bisuan', sys_i)} "
            f"官方{fmt_metric('bisuan', off_i)} 差{sign}{fmt_metric('bisuan', diff)}"
        )
    if calibrate_lines:
        lines.append("笔算校准：")
        lines.extend(calibrate_lines)
    return "\n".join(lines)
