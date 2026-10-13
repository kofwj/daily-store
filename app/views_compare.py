"""触客通报：手机销量 vs 触客条数的单日全店主表 + 单店逐日下钻，可事后修正手机销量。
口径（与 README「口径」一致）：
- 触客条数 = 该店当日 deal_posts 的全部记录数（COUNT(*)，不看是否成交）
- 手机销量 = 该店当日日报的 phone_sales
- 差 = 触客条数 − 手机销量（正 = 少报，负 = 多报，与偏差榜同向）
- 没交日报的店也留一行，差写「—」而不是 0；触客 0 时不算差率（分母 0）
- 表里的合计只算「已交日报」的店 / 天，未交那部分单独点名
- 改手机销量只给管理员 / 地市负责人（沿用 policy.daily_edit_gate），改完不重发群播报
- 业辅查询登记：每店每天一个整数，跟手机销量一样就地填 / 就地改（管理员 / 地市负责人，
  city 限本市），同样只有已交日报的店行可填；只留「谁什么时候写的」两个字段，不写 report_edits
- 表里 4 列「本月累计」= 当月 1 日 → 所选日的触客 / 手机销量 / 差 / 业辅查询登记，每个店一行；
  触客含未交日报那几天，销量只可能出自已交日报，某店这月没交过日报就差写「—」
- 触客按定义就是当天的：deal_posts.biz_date 恒等于录入当天，没有补交一说，
  所以「昨天触客 0 条」就是当天真没录，本表不做任何回填
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import wraps
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import db, insights, policy
from .helpers import (
    accessible_stores,
    default_home,
    filter_stores,
    login_required,
    month_end,
    parse_date,
    parse_int,
    phone_fix_marks,
    pick_store,
    scope_options,
    sql_in,
    xlsx_bytes,
    xlsx_response,
)
from .metrics_seed import format_stored, to_stored

PHONE_CODE = "phone_sales"
COMPARE_TITLE = "触客通报"
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")
# 只有这两个角色能进这页（地市负责人要用它改本市手机销量）
_EDIT_ROLES = ("admin", "city")


def compare_required(fn):
    """对比表：管理员 + 地市负责人；填报员 / 只读不进（别改口径外的权限面）。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] not in _EDIT_ROLES:
            flash("需要管理员或地市负责人权限", "error")
            return redirect(default_home())
        return fn(*args, **kwargs)

    return wrapper


def _can_edit_cell(role: str, biz_date: date, today: date) -> bool:
    """这一格能不能改：只有管理员 / 地市负责人，且过日报编辑闸（city 只限本月）。"""
    if role not in _EDIT_ROLES:
        return False
    gate = policy.daily_edit_gate(
        role,
        biz_date,
        today,
        filler_month=False,
        locked=db.is_locked(biz_date),
    )
    return gate.allowed


def _diff_text(diff: Optional[int]) -> str:
    if diff is None:
        return "—"
    return "0" if diff == 0 else f"{diff:+d}"


def _rate_text(rate: Optional[float]) -> str:
    """差率 = 差 ÷ 触客条数。触客 0（分母 0）与未交日报都给「—」，不给 0%。"""
    if rate is None:
        return "—"
    pct = rate * 100
    return "0%" if abs(pct) < 0.5 else f"{pct:+.0f}%"


def _side(diff: Optional[int]) -> str:
    """underr=少报（正差）/ over=多报（负差）/ even=一致 / none=没得比。"""
    if diff is None:
        return "none"
    if diff > 0:
        return "under"
    if diff < 0:
        return "over"
    return "even"


def _aux_title(meta: Optional[Mapping[str, Any]]) -> str:
    """业辅格子的 hover 溯源：最后一次登记的人和时刻（表里只留这两样）。"""
    if not meta:
        return ""
    bits = [(meta.get("editor") or "").strip(), (meta.get("at") or "").strip()]
    bits = [b for b in bits if b]
    return f"业辅查询登记：{' · '.join(bits)}" if bits else ""


def _report_days(conn, store_ids: Sequence[int], start: date, end: date) -> Dict[int, int]:
    """区间内各店交过日报的天数：月累计差要不要出，就看这个是不是 0。"""
    clause, params = sql_in("r.store_id", store_ids)
    if clause == "1=0" or end < start:
        return {}
    rows = conn.execute(
        f"""
        SELECT r.store_id, COUNT(*) AS n
        FROM daily_reports r
        WHERE r.biz_date>=? AND r.biz_date<=? AND {clause}
        GROUP BY r.store_id
        """,
        [start.isoformat(), end.isoformat(), *params],
    )
    return {int(row["store_id"]): int(row["n"]) for row in rows}


def _month_stats(conn, store_ids: Sequence[int], biz_date: date) -> Dict[str, Any]:
    """本月累计（当月 1 日 → 所选日）按店拆开：触客 / 手机销量 / 业辅查询登记 / 差。

    这几个数以前是页头三张卡，现在直接进表里每个店一行，所以本体是 {store_id: 数字}。
    口径：触客含未交日报那几天（触客只有当天，没有补交），手机销量只可能出自已交日报；
    差 = 触客 − 销量，某店这月一天日报都没交就写「—」（跟单日格子一个规矩）。
    """
    month_start = biz_date.replace(day=1)
    ids = [int(sid) for sid in store_ids]
    if biz_date.day == 1:
        span = f"{month_start.month}月1日"
    else:
        span = f"{month_start.month}月1日–{biz_date.month}月{biz_date.day}日"
    if not ids:
        empty: Dict[int, Any] = {}
        return {"label": span, "touch": empty, "sold": empty, "aux": empty, "diff": empty}
    deals = db.deal_daily_counts(conn, ids, month_start, biz_date)
    sales = db.range_metric_totals(conn, ids, month_start, biz_date, (PHONE_CODE,))
    aux_days = db.aux_days(conn, ids, month_start, biz_date)
    reported = _report_days(conn, ids, month_start, biz_date)
    touch = {sid: sum(int(v or 0) for v in deals.get(sid, {}).values()) for sid in ids}
    sold = {sid: int((sales.get(sid) or {}).get(PHONE_CODE, 0) or 0) for sid in ids}
    aux = {
        sid: sum(int((meta or {}).get("n") or 0) for meta in aux_days.get(sid, {}).values())
        for sid in ids
    }
    diff: Dict[int, Optional[int]] = {
        sid: (touch[sid] - sold[sid] if reported.get(sid) else None) for sid in ids
    }
    return {"label": span, "touch": touch, "sold": sold, "aux": aux, "diff": diff}


def _month_total(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """月累计合计 = 表里看得见那几行的加总（合计跟行对得上，别偷偷多算）。"""
    with_diff = [r for r in rows if r["m_diff"] is not None]
    total: Dict[str, Any] = {
        "n": len(with_diff),
        "touch": sum(int(r["m_touch"]) for r in rows),
        "sold": sum(int(r["m_sold"]) for r in rows),
        "aux": sum(int(r["m_aux"]) for r in rows),
        "diff": sum(int(r["m_diff"]) for r in with_diff),
        "_touch_base": sum(int(r["m_touch"]) for r in with_diff),
    }
    total["diff_text"] = _diff_text(total["diff"])
    total["side"] = _side(total["diff"])
    total["rate_text"] = _rate_text(
        None if total["_touch_base"] <= 0 else total["diff"] / total["_touch_base"]
    )
    return total


def _reports_on(conn, store_ids: Sequence[int], biz_date: date) -> Dict[int, Any]:
    """当天哪些店交了日报（按 daily_reports 有没有行判定）。"""
    clause, params = sql_in("r.store_id", store_ids)
    if clause == "1=0":
        return {}
    rows = conn.execute(
        f"""
        SELECT r.store_id, r.submitted_at, r.note, u.display_name AS submitter_name
        FROM daily_reports r
        LEFT JOIN users u ON u.id = r.submitted_by
        WHERE r.biz_date=? AND {clause}
        """,
        [biz_date.isoformat(), *params],
    )
    return {int(row["store_id"]): row for row in rows}


def _city_of(store) -> str:
    return (store["city"] or "").strip() or "未分地市"

def _day_scope(stores) -> Dict[str, Any]:
    """单日视图的地市 / 区域经理 / 顾问筛选：跟通报表、洞察页同一套控件，只有默认值不同。

    通报表默认落在南通市，这页默认「全部」——打开是单日全店视野，想看某个
    地市 / 哪位区域经理 / 哪类顾问再选。传了不认识的值当没传，别把表滤成空的。
    顾问筛选照洞察页（`?advisor=yes|no`），只作用于单日视图，单店逐日不涉及。
    """
    cities, managers = scope_options(stores)
    city = (request.args.get("city") or "").strip()
    area = (request.args.get("area") or "").strip()
    advisor = (request.args.get("advisor") or "").strip()
    if city not in cities:
        city = ""
    if area not in managers:
        area = ""
    if advisor not in ("yes", "no"):
        advisor = ""
    picked = filter_stores(stores, city, area)
    if advisor == "yes":
        picked = [s for s in picked if (s["advisor_name"] or "").strip()]
    elif advisor == "no":
        picked = [s for s in picked if not (s["advisor_name"] or "").strip()]
    bits = [b for b in (city, area) if b]
    if advisor:
        bits.append("有顾问" if advisor == "yes" else "无顾问")
    return {
        "stores": picked,
        "city": city,
        "cities": cities,
        "area": area,
        "areas": managers,
        "advisor": advisor,
        "label": " · ".join(bits),
    }


def _order_rows(
    rows: List[Dict[str, Any]], city_order: Sequence[str], sort: str
) -> List[Dict[str, Any]]:
    """地市按目录顺序分组；组内默认 |差| 从大到小（未交日报垫底），可切目录顺序。"""
    if sort == "catalog":
        ordered = list(rows)  # 目录顺序 = accessible_stores 的顺序（目录表顺序）
    else:
        # 未交日报没有差，排在本组最后；其余按 |差| 降序；同 |差| 保持目录顺序
        ordered = sorted(
            rows,
            key=lambda r: (1 if r["diff"] is None else 0, -abs(int(r["diff"] or 0))),
        )
    return insights.group_rows_by_city_order(ordered, city_order, lambda r: r["city"])


def day_copy_text(
    *,
    biz_date: date,
    rows: Sequence[Mapping[str, Any]],
    totals: Mapping[str, Any],
    missing_names: Sequence[str],
    silent_n: int = 0,
    scope_label: str = "",
    month: Optional[Mapping[str, Any]] = None,
) -> str:
    """单日通报文案（贴微信群）：一句合计 + 本月累计 + 少报 / 多报 / 一致 / 未交点名。

    行序固定按 |差| 降序，与页面排序开关无关——同一份数据不该复制出两种顺序。
    没交日报又没触客的那批只报个数，不把名单铺满。
    业辅查询登记那天没登记就不出这一段（天天挂个「0 条」只是噪音）；
    本月累计给两行：一行是全表合计、一行按店铺开（跟着当前筛选走）。
    """
    if not rows:
        return ""  # 没有店进表（全静默）：没有可通报的内容
    label = (scope_label or "").strip()
    head = f"【触客通报】{biz_date.month}月{biz_date.day}日"
    if label:
        head += f"（{label}）"
    parts: List[str] = [head]
    if int(totals["n"]):
        line = (
            f"合计（已交 {totals['n']} 家）：触客 {totals['touch']} 条 · "
            f"手机销量 {totals['sold']} 台 · 差 {totals['diff_text']}"
        )
        aux_n = int(totals.get("aux", 0) or 0)
        if aux_n:
            line += f" · 业辅查询登记 {aux_n} 条"
        parts.append(line)
    if month:
        total = month.get("total") or {}
        parts.append(
            f"本月累计（{month.get('label') or ''}）：触客 {total.get('touch', 0)} 条 · "
            f"手机销量 {total.get('sold', 0)} 台 · 差 {total.get('diff_text', '—')} · "
            f"业辅查询登记 {total.get('aux', 0)} 条"
        )
        # 每家店的月累计单独一行：通报里也要看得出是哪个店拉的口子
        per_store = []
        for r in rows:
            bit = (
                f"{r['name']} 触客 {int(r.get('m_touch') or 0)} · "
                f"销量 {int(r.get('m_sold') or 0)} · 差 {r.get('m_diff_text') or '—'}"
            )
            m_aux = int(r.get("m_aux") or 0)
            if m_aux:
                bit += f" · 业辅 {m_aux}"
            per_store.append(bit)
        if per_store:
            parts.append("本月累计 · 各店：" + "；".join(per_store))
    under = [r for r in rows if r["side"] == "under"]
    over = [r for r in rows if r["side"] == "over"]
    even = [r["name"] for r in rows if r["side"] == "even"]
    if under:
        bits = "、".join(f"{r['name']} {r['diff_text']}" for r in under)
        parts.append(f"少报 {len(under)} 家：{bits}")
    if over:
        bits = "、".join(f"{r['name']} {r['diff_text']}" for r in over)
        parts.append(f"多报 {len(over)} 家：{bits}")
    if even:
        parts.append(f"一致 {len(even)} 家：" + "、".join(even))
    if missing_names:
        parts.append(f"未交日报 {len(missing_names)} 家：" + "、".join(missing_names))
    if silent_n:
        parts.append(f"另有 {silent_n} 家没交日报也没触客记录")
    return "\n".join(parts) if len(parts) > 1 else ""


def _day_payload(
    conn, stores, biz_date: date, *, role: str, today: date, sort: str, scope_label: str = ""
):
    """单日主表：全店一行，触客条数 vs 日报手机销量 + 业辅查询登记。"""
    ids = [int(s["id"]) for s in stores]
    day_key = biz_date.isoformat()
    deals = db.deal_daily_counts(conn, ids, biz_date, biz_date)
    sales = db.range_metric_totals(conn, ids, biz_date, biz_date, (PHONE_CODE,))
    reports = _reports_on(conn, ids, biz_date)
    marks = phone_fix_marks(conn, ids, biz_date, biz_date, metric_code=PHONE_CODE)
    aux = db.aux_days(conn, ids, biz_date, biz_date)

    rows: List[Dict[str, Any]] = []
    silent_n = 0
    for store in stores:
        sid = int(store["id"])
        touch = int(deals.get(sid, {}).get(day_key, 0) or 0)
        report = reports.get(sid)
        aux_meta = (aux.get(sid) or {}).get(day_key)
        aux_n = int((aux_meta or {}).get("n") or 0)
        if report is None and not touch and not aux_n:
            # 既没交日报、当天也没触客、也没业辅登记：不占表行，单独报个数
            silent_n += 1
            continue
        sold = int((sales.get(sid) or {}).get(PHONE_CODE, 0) or 0)
        diff = None if report is None else touch - sold
        rows.append(
            {
                "id": sid,
                # 表 / 文案 / 图片 / 导出里的门店一律写全称；简称留给 hover 提示对号
                "name": (store["name"] or store["short_name"] or "").strip() or "未命名",
                "short_name": (store["short_name"] or "").strip(),
                "city": _city_of(store),
                "touch": touch,
                "sold": sold,
                "diff": diff,
                "diff_text": _diff_text(diff),
                "rate_text": _rate_text(None if diff is None or touch <= 0 else diff / touch),
                "side": _side(diff),
                "submitted": report is not None,
                "submitter": (report["submitter_name"] or "").strip() if report else "",
                "submitted_at": (report["submitted_at"] or "")[:16] if report else "",
                "fix": (marks.get(sid) or {}).get(day_key),
                "aux": aux_n,
                "aux_title": _aux_title(aux_meta),
            }
        )

    # 本月累计按店算：先把要显示的行定下来，再算这 4 列（合计才等于看得见的行之和）
    month = _month_stats(conn, [r["id"] for r in rows], biz_date)
    for r in rows:
        sid = r["id"]
        r["m_touch"] = int(month["touch"].get(sid, 0))
        r["m_sold"] = int(month["sold"].get(sid, 0))
        r["m_aux"] = int(month["aux"].get(sid, 0))
        r["m_diff"] = month["diff"].get(sid)
        r["m_diff_text"] = _diff_text(r["m_diff"])
        r["m_side"] = _side(r["m_diff"])
        r["m_reported"] = r["m_diff"] is not None
        r["m_rate_text"] = _rate_text(
            None if r["m_diff"] is None or r["m_touch"] <= 0 else r["m_diff"] / r["m_touch"]
        )
    month["total"] = _month_total(rows)

    city_order = insights.catalog_city_order(stores)
    ordered = _order_rows(rows, city_order, sort)

    reported_rows = [r for r in ordered if r["submitted"]]
    totals: Dict[str, Any] = {
        "n": len(reported_rows),
        "touch": sum(int(r["touch"]) for r in reported_rows),
        "sold": sum(int(r["sold"]) for r in reported_rows),
        "aux": sum(int(r["aux"]) for r in reported_rows),
        "diff": sum(int(r["diff"]) for r in reported_rows),
    }
    totals["diff_text"] = _diff_text(totals["diff"])
    totals["side"] = _side(totals["diff"])
    totals["rate_text"] = _rate_text(
        None if totals["touch"] <= 0 else totals["diff"] / totals["touch"]
    )

    city_groups: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for row in ordered:
        g = index.get(row["city"])
        if g is None:
            g = {
                "city": row["city"],
                "n": 0,
                "missing": 0,
                "touch": 0,
                "sold": 0,
                "diff": 0,
                "aux": 0,
                "m_touch": 0,
                "m_sold": 0,
                "m_diff": 0,
                "m_aux": 0,
                "m_n": 0,
            }
            index[row["city"]] = g
            city_groups.append(g)
        if row["submitted"]:
            g["n"] += 1
            g["touch"] += int(row["touch"])
            g["sold"] += int(row["sold"])
            g["diff"] += int(row["diff"])
            g["aux"] += int(row["aux"])
        else:
            g["missing"] += 1
        # 本月累计不受「已交」限制：这一组所有店的月数（触客含未交日报那几天）
        g["m_touch"] += int(row["m_touch"])
        g["m_sold"] += int(row["m_sold"])
        g["m_aux"] += int(row["m_aux"])
        if row["m_diff"] is not None:
            g["m_n"] += 1
            g["m_diff"] += int(row["m_diff"])
    for g in city_groups:
        # 小计行跟店铺行用同一套徽标口径（少报 / 多报 / 一致），别一边是词一边是裸数字
        g["diff_text"] = _diff_text(g["diff"]) if g["n"] else "—"
        g["side"] = _side(g["diff"]) if g["n"] else "none"
        g["m_diff_text"] = _diff_text(g["m_diff"])
        g["m_side"] = _side(g["m_diff"])
    if len(city_groups) <= 1:
        city_groups = []

    missing_names = [r["name"] for r in ordered if not r["submitted"]]
    cities, _managers = scope_options(stores)
    # 抬头：筛了就地市+区域经理；没筛时只有「本来就看得到单市」才带城市名
    label = (scope_label or "").strip() or (cities[0] if len(cities) == 1 else "")
    return {
        "rows": ordered,
        "city_groups": city_groups,
        "totals": totals,
        "missing_n": len(ordered) - totals["n"],
        "missing_names": missing_names,
        "silent_n": silent_n,
        "can_edit": _can_edit_cell(role, biz_date, today),
        "scope_label": label,
        "copy_text": day_copy_text(
            biz_date=biz_date,
            rows=_order_rows(rows, city_order, "abs"),
            totals=totals,
            missing_names=missing_names,
            silent_n=silent_n,
            scope_label=label,
            month=month,
        ),
        "month": month,
    }


def _store_month_payload(conn, store, month: date, *, role: str, today: date):
    """单店逐日下钻：一个月每天一行，触客条数 vs 当天日报手机销量 + 业辅查询登记。"""
    month_start = month.replace(day=1)
    last_day = month_end(month_start)
    end = min(last_day, today)
    if end < month_start:  # 未来月：只留月初一行，别凭空铺一整月
        end = month_start
    sid = int(store["id"])

    deals = db.deal_daily_counts(conn, [sid], month_start, end).get(sid, {})
    facts = db.range_metric_days(conn, [sid], month_start, end, (PHONE_CODE,))
    sales = {str(row["biz_date"]): int(row["day_value"] or 0) for row in facts}
    # 日报按天取：一次查完整月，避免逐日查库
    clause, params = sql_in("r.store_id", [sid])
    report_days = {
        str(row["biz_date"]): row
        for row in conn.execute(
            f"""
            SELECT r.biz_date, r.submitted_at, u.display_name AS submitter_name
            FROM daily_reports r
            LEFT JOIN users u ON u.id = r.submitted_by
            WHERE r.biz_date>=? AND r.biz_date<=? AND {clause}
            """,
            [month_start.isoformat(), end.isoformat(), *params],
        )
    }
    marks = phone_fix_marks(conn, [sid], month_start, end, metric_code=PHONE_CODE).get(sid, {})
    aux_days = db.aux_days(conn, [sid], month_start, end).get(sid, {})

    day_rows: List[Dict[str, Any]] = []
    cursor = month_start
    while cursor <= end:
        key = cursor.isoformat()
        touch = int(deals.get(key, 0) or 0)
        sold = int(sales.get(key, 0) or 0)
        report = report_days.get(key)
        aux_meta = aux_days.get(key)
        aux_n = int((aux_meta or {}).get("n") or 0)
        diff = None if report is None else touch - sold
        day_rows.append(
            {
                "iso": key,
                "day": cursor.day,
                "weekday": _WEEKDAYS[cursor.weekday()],
                "touch": touch,
                "sold": sold,
                "diff": diff,
                "diff_text": _diff_text(diff),
                "rate_text": _rate_text(None if diff is None or touch <= 0 else diff / touch),
                "side": _side(diff),
                "submitted": report is not None,
                "submitter": (report["submitter_name"] or "").strip() if report else "",
                "submitted_at": (report["submitted_at"] or "")[:16] if report else "",
                "fix": marks.get(key),
                "aux": aux_n,
                "aux_title": _aux_title(aux_meta),
                "editable": _can_edit_cell(role, cursor, today),
            }
        )
        cursor += timedelta(days=1)

    reported = [r for r in day_rows if r["submitted"]]
    totals: Dict[str, Any] = {
        "days": len(day_rows),
        "n": len(reported),
        "missing": len(day_rows) - len(reported),
        "touch": sum(int(r["touch"]) for r in reported),
        "sold": sum(int(r["sold"]) for r in reported),
        "diff": sum(int(r["diff"]) for r in reported),
        "aux": sum(int(r["aux"]) for r in reported),
        "touch_unsubmitted": sum(int(r["touch"]) for r in day_rows if not r["submitted"]),
    }
    totals["diff_text"] = _diff_text(totals["diff"])
    totals["side"] = _side(totals["diff"])
    totals["rate_text"] = _rate_text(
        None if totals["touch"] <= 0 else totals["diff"] / totals["touch"]
    )

    return {
        "store": store,
        "month": month_start,
        "month_end": last_day,
        "month_prev": (month_start - timedelta(days=1)).replace(day=1),
        "month_next": (last_day + timedelta(days=1)).replace(day=1),
        "days": day_rows,
        "totals": totals,
        "can_edit": any(r["editable"] for r in day_rows),
    }


def _back_kwargs(form) -> Dict[str, Any]:
    """保存后回跳同一个视图（别把筛选丢了）。"""
    view = (form.get("view") or "day").strip()
    if view == "store":
        kwargs: Dict[str, Any] = {"view": "store"}
        if form.get("store_id"):
            kwargs["store_id"] = form.get("store_id")
        if form.get("month"):
            kwargs["month"] = form.get("month")
        return kwargs
    kwargs = {}
    if form.get("date"):
        kwargs["date"] = form.get("date")
    # 筛选也得带回：改完一格还回到同一个地市 / 区域经理视野
    for key in ("city", "area", "advisor"):
        if form.get(key):
            kwargs[key] = form.get(key)
    if (form.get("sort") or "") == "catalog":
        kwargs["sort"] = "catalog"
    return kwargs


def register_compare(app) -> None:
    @app.route("/compare")
    @compare_required
    def compare():
        today = db.today_local()
        role = g.user["role"]
        view = (request.args.get("view") or "day").strip()
        if view not in ("day", "store"):
            view = "day"
        sort = (request.args.get("sort") or "").strip()
        if sort not in ("abs", "catalog"):
            sort = "abs"
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            if not stores:
                return render_template("empty.html")
            if view == "store":
                store, _all = pick_store(conn, request.args.get("store_id"))
                if store is None:
                    return render_template("empty.html")
                month = parse_date(
                    request.args.get("month"), today.replace(day=1)
                ).replace(day=1)
                payload = _store_month_payload(conn, store, month, role=role, today=today)
                return render_template(
                    "compare.html",
                    view="store",
                    sort=sort,
                    stores=stores,
                    **payload,
                )
            biz_date = parse_date(request.args.get("date"), today - timedelta(days=1))
            scope = _day_scope(stores)
            payload = _day_payload(
                conn,
                scope["stores"],
                biz_date,
                role=role,
                today=today,
                sort=sort,
                scope_label=scope["label"],
            )
        return render_template(
            "compare.html",
            view="day",
            sort=sort,
            date=biz_date,
            prev_date=biz_date - timedelta(days=1),
            next_date=None if biz_date >= today else biz_date + timedelta(days=1),
            today=today,
            city=scope["city"],
            cities=scope["cities"],
            area=scope["area"],
            areas=scope["areas"],
            advisor=scope["advisor"],
            **payload,
        )

    @app.route("/compare/cell", methods=["POST"])
    @login_required
    def compare_cell():
        """只改某店某天的手机销量一格，其它数字不动；改完不重发群播报。"""
        role = g.user["role"]
        if role not in _EDIT_ROLES:
            return Response("forbidden", status=403)
        back = _back_kwargs(request.form)
        sid = parse_int(request.form.get("store_id"), 0)
        try:
            biz_date = date.fromisoformat((request.form.get("date") or "").strip())
        except ValueError:
            flash("格子参数不对", "error")
            return redirect(url_for("compare", **back))
        if sid <= 0:
            flash("格子参数不对", "error")
            return redirect(url_for("compare", **back))
        value = max(0, to_stored(PHONE_CODE, request.form.get("value") or "0"))
        with db.get_db() as conn:
            if not db.user_can_access_store(conn, g.user, sid):
                return Response("forbidden", status=403)
            if db.get_report(conn, sid, biz_date) is None:
                flash("该店当天未交日报，请先去「报表」页补录", "error")
                return redirect(url_for("compare", **back))
            gate = policy.daily_edit_gate(
                role,
                biz_date,
                db.today_local(),
                filler_month=False,
                locked=db.is_locked(biz_date),
            )
            if not gate.allowed:
                flash(gate.reason, "error")
                return redirect(url_for("compare", **back))
            try:
                db.set_day_value(
                    conn,
                    store_id=sid,
                    biz_date=biz_date,
                    metric_code=PHONE_CODE,
                    value=value,
                    user_id=g.user["id"],
                    note="触客通报校准手机销量",
                )
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash(
                    f"已把 {biz_date.isoformat()} 的手机销量改成 "
                    f"{format_stored(PHONE_CODE, value)}",
                    "ok",
                )
        return redirect(url_for("compare", **back))

    @app.route("/compare/aux", methods=["POST"])
    @login_required
    def compare_aux():
        """只改某店某天的业辅查询登记一格：不进日报指标、不写修改审计、不重发群播报。"""
        role = g.user["role"]
        if role not in _EDIT_ROLES:
            return Response("forbidden", status=403)
        back = _back_kwargs(request.form)
        sid = parse_int(request.form.get("store_id"), 0)
        try:
            biz_date = date.fromisoformat((request.form.get("date") or "").strip())
        except ValueError:
            flash("格子参数不对", "error")
            return redirect(url_for("compare", **back))
        if sid <= 0:
            flash("格子参数不对", "error")
            return redirect(url_for("compare", **back))
        value = max(0, parse_int(request.form.get("value"), 0))
        with db.get_db() as conn:
            if not db.user_can_access_store(conn, g.user, sid):
                return Response("forbidden", status=403)
            if db.get_report(conn, sid, biz_date) is None:
                flash("该店当天未交日报，请先去「报表」页补录", "error")
                return redirect(url_for("compare", **back))
            gate = policy.daily_edit_gate(
                role,
                biz_date,
                db.today_local(),
                filler_month=False,
                locked=db.is_locked(biz_date),
            )
            if not gate.allowed:
                flash(gate.reason, "error")
                return redirect(url_for("compare", **back))
            db.set_aux(
                conn,
                store_id=sid,
                biz_date=biz_date,
                n=value,
                user_id=g.user["id"],
            )
            flash(f"已把 {biz_date.isoformat()} 的业辅查询登记改成 {value} 条", "ok")
        return redirect(url_for("compare", **back))

    @app.route("/compare.xlsx")
    @compare_required
    def compare_xlsx():
        """导出当前视图：单日主表或单店逐日。"""
        today = db.today_local()
        role = g.user["role"]
        view = (request.args.get("view") or "day").strip()
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            if not stores:
                return Response("no store", status=400)
            if view == "store":
                store, _all = pick_store(conn, request.args.get("store_id"))
                if store is None:
                    return Response("no store", status=400)
                month = parse_date(
                    request.args.get("month"), today.replace(day=1)
                ).replace(day=1)
                payload = _store_month_payload(conn, store, month, role=role, today=today)
                header = [
                    "日期",
                    "星期",
                    "触客条数",
                    "手机销量",
                    "差",
                    "差率",
                    "业辅查询登记",
                    "日报",
                    "日报提交人",
                    "提交时间",
                    "手机销量修正",
                ]
                data_rows = [
                    [
                        r["iso"],
                        r["weekday"],
                        r["touch"],
                        r["sold"],
                        r["diff_text"],
                        r["rate_text"],
                        r["aux"],
                        "已交" if r["submitted"] else "未交日报",
                        r["submitter"],
                        r["submitted_at"],
                        (r["fix"] or {}).get("text", ""),
                    ]
                    for r in payload["days"]
                ]
                store_totals = payload["totals"]
                data_rows.append(
                    [
                        f"合计（已交 {store_totals['n']} 天）",
                        "",
                        store_totals["touch"],
                        store_totals["sold"],
                        store_totals["diff_text"],
                        store_totals["rate_text"],
                        store_totals["aux"],
                        "",
                        "",
                        "",
                        "",
                    ]
                )
                filename = (
                    f"compare_{store['code']}_{payload['month'].isoformat()}.xlsx"
                )
                sheet = "逐日对比"
            else:
                biz_date = parse_date(request.args.get("date"), today - timedelta(days=1))
                sort = (request.args.get("sort") or "").strip()
                sort = sort if sort in ("abs", "catalog") else "abs"
                scope = _day_scope(stores)
                payload = _day_payload(
                    conn,
                    scope["stores"],
                    biz_date,
                    role=role,
                    today=today,
                    sort=sort,
                    scope_label=scope["label"],
                )
                month = payload["month"]
                header = [
                    "日期",
                    "门店",
                    "地市",
                    f"本月累计触客条数（{month['label']}）",
                    f"本月累计手机销量（{month['label']}）",
                    f"本月累计差（{month['label']}）",
                    f"本月累计业辅查询登记（{month['label']}）",
                    "触客条数",
                    "手机销量",
                    "差",
                    "差率",
                    "业辅查询登记",
                    "日报",
                    "日报提交人",
                    "提交时间",
                    "手机销量修正",
                ]
                data_rows = [
                    [
                        biz_date.isoformat(),
                        r["name"],
                        r["city"],
                        r["m_touch"],
                        r["m_sold"],
                        r["m_diff_text"],
                        r["m_aux"],
                        r["touch"],
                        r["sold"],
                        r["diff_text"],
                        r["rate_text"],
                        r["aux"],
                        "已交" if r["submitted"] else "未交日报",
                        r["submitter"],
                        r["submitted_at"],
                        (r["fix"] or {}).get("text", ""),
                    ]
                    for r in payload["rows"]
                ]
                # 表尾合计：前 4 列是本月累计（表里看得见的行加总），后面 5 列是当日的
                # （当日只算已交日报的店，月累计口径见 _month_stats）
                day_totals = payload["totals"]
                month_total = payload["month"]["total"]
                data_rows.append(
                    [
                        "",
                        f"合计（已交 {day_totals['n']} 家）",
                        "",
                        month_total["touch"],
                        month_total["sold"],
                        month_total["diff_text"],
                        month_total["aux"],
                        day_totals["touch"],
                        day_totals["sold"],
                        day_totals["diff_text"],
                        day_totals["rate_text"],
                        day_totals["aux"],
                        "",
                        "",
                        "",
                        "",
                    ]
                )
                filename = f"compare_{biz_date.isoformat()}.xlsx"
                sheet = "单日对比"
        return xlsx_response(
            xlsx_bytes(header, data_rows, sheet=sheet), filename
        )


# 供模板 / 测试复用（避免在模板里写口径判断）
COMPARE_HELPERS: Tuple[Any, ...] = (_diff_text, _rate_text, _side, day_copy_text)
