"""运营洞察：本月进度对时间、周同比、未交/落后。只读聚合，不写库。"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

from .metrics_seed import KPI_TARGETS, ROLLUPS, format_display, from_stored, rollup_amount

KPI_CODES = [code for code, _name, _note in KPI_TARGETS]
FACT_CODES = (
    "bisuan",
    "bisuan_high",
    "ai_contract",
    *ROLLUPS["coin_cut"]["parts"],
    *ROLLUPS["coin_cut"]["legacy"],
)
LAG_POINTS = 15  # 实际进度比时间进度落后超过 15 个百分点算落后


def _scale(code: str) -> str:
    return "bisuan" if code == "bisuan_total" else code


def _store_name(store: Mapping[str, Any]) -> str:
    return (store["short_name"] or store["name"] or "").strip() or "未命名"


def week_span(as_of: date) -> tuple[date, date]:
    """本周一到 as_of（含）。"""
    start = as_of - timedelta(days=as_of.weekday())
    return start, as_of


def prev_week_span(as_of: date) -> tuple[date, date]:
    """上周一到上周同一天，和本周一到今天对齐。"""
    this_start, this_end = week_span(as_of)
    return this_start - timedelta(days=7), this_end - timedelta(days=7)


def _rollup_store(facts: Mapping[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for code, _name, _note in KPI_TARGETS:
        if code == "ai_contract":
            out[code] = int(facts.get("ai_contract") or 0)
        else:
            out[code] = rollup_amount(facts, code)
    return out


def _sum_maps(maps: Sequence[Mapping[str, int]]) -> Dict[str, int]:
    out = {code: 0 for code in KPI_CODES}
    for item in maps:
        for code in KPI_CODES:
            out[code] += int(item.get(code) or 0)
    return out


def build_insights(
    *,
    stores: Sequence[Mapping[str, Any]],
    as_of: date,
    kpi_targets: Mapping[str, int],
    month_facts: Mapping[int, Mapping[str, int]],
    week_facts: Mapping[int, Mapping[str, int]],
    prev_week_facts: Mapping[int, Mapping[str, int]],
    reported_today: set,
    reported_month: set,
    mobile_bisuan: Optional[Mapping[int, int]] = None,
) -> Dict[str, Any]:
    days_in_month = calendar.monthrange(as_of.year, as_of.month)[1]
    elapsed = max(1, min(as_of.day, days_in_month))
    pace = elapsed / days_in_month * 100
    n = len(stores)
    mobile_bisuan = mobile_bisuan or {}
    month_by_store: Dict[int, Dict[str, int]] = {}
    week_by_store: Dict[int, Dict[str, int]] = {}
    prev_by_store: Dict[int, Dict[str, int]] = {}
    for store in stores:
        sid = int(store["id"])
        facts = _rollup_store(month_facts.get(sid) or {})
        mv = mobile_bisuan.get(sid)
        if mv is not None:
            # 有移动校准数：当月比算总量用移动口径，评优/落后判断跟着改。
            # 周环比仍看填报（移动数只给整月）。
            facts["bisuan_total"] = int(mv)
        month_by_store[sid] = facts
        week_by_store[sid] = _rollup_store(week_facts.get(sid) or {})
        prev_by_store[sid] = _rollup_store(prev_week_facts.get(sid) or {})

    month_total = _sum_maps(list(month_by_store.values()))
    week_total = _sum_maps(list(week_by_store.values()))
    prev_total = _sum_maps(list(prev_by_store.values()))

    kpis = []
    for code, name, _note in KPI_TARGETS:
        scale = _scale(code)
        stored = month_total.get(code, 0)
        value = from_stored(scale, stored)
        target_one = int(kpi_targets.get(code, 0) or 0)
        target = target_one * n
        progress = (value / target * 100) if target else None
        kpis.append(
            {
                "code": code,
                "name": name,
                "value": value,
                "value_text": format_display(scale, value),
                "target": target,
                "target_text": format_display(scale, target) if target else "",
                "progress": progress,
                "pace": pace,
                "gap": (progress - pace) if progress is not None else None,
            }
        )

    week_kpis = []
    for code, name, _note in KPI_TARGETS:
        scale = _scale(code)
        now_v = from_stored(scale, week_total.get(code, 0))
        prev_v = from_stored(scale, prev_total.get(code, 0))
        delta = now_v - prev_v
        pct = (delta / prev_v * 100) if prev_v else (100.0 if now_v else 0.0)
        week_kpis.append(
            {
                "code": code,
                "name": name,
                "now": now_v,
                "now_text": format_display(scale, now_v),
                "prev": prev_v,
                "prev_text": format_display(scale, prev_v),
                "delta": delta,
                "delta_text": format_display(scale, delta),
                "pct": pct,
            }
        )

    missing_today = [_store_name(s) for s in stores if int(s["id"]) not in reported_today]
    missing_month = [_store_name(s) for s in stores if int(s["id"]) not in reported_month]
    laggards = []
    store_rows = []
    for store in stores:
        sid = int(store["id"])
        bits = []
        month_bits = []
        week_bits = []
        for code, name, _note in KPI_TARGETS:
            scale = _scale(code)
            target = int(kpi_targets.get(code, 0) or 0)
            month_v = from_stored(scale, month_by_store[sid].get(code, 0))
            now_v = from_stored(scale, week_by_store[sid].get(code, 0))
            prev_v = from_stored(scale, prev_by_store[sid].get(code, 0))
            delta = now_v - prev_v
            week_bits.append(
                {
                    "code": code,
                    "name": name,
                    "now": now_v,
                    "now_text": format_display(scale, now_v),
                    "prev": prev_v,
                    "prev_text": format_display(scale, prev_v),
                    "delta": delta,
                    "delta_text": format_display(scale, delta),
                }
            )
            month_progress = (month_v / target * 100) if target else None
            month_bits.append(
                {
                    "code": code,
                    "name": name,
                    "value": month_v,
                    "value_text": format_display(scale, month_v),
                    "target": target,
                    "progress": month_progress,
                }
            )
            if target and month_progress is not None and month_progress + LAG_POINTS < pace:
                bits.append(f"{name} {format_display(scale, month_v)}/{target}（{month_progress:.0f}%）")
        if bits:
            laggards.append({"name": _store_name(store), "bits": bits})
        today_ok = sid in reported_today
        month_ok = sid in reported_month
        flags = []
        if not today_ok:
            flags.append("今日未交")
        if not month_ok:
            flags.append("本月未交")
        if bits:
            flags.append("进度落后")
        week_delta_sum = sum(item["delta"] for item in week_bits)
        store_rows.append(
            {
                "id": sid,
                "name": _store_name(store),
                "city": (store["city"] or "").strip() or "未分地市",
                "manager": (store["area_manager"] or "").strip() or "—",
                "advisor": (store["advisor_name"] or "").strip() or "—",
                "today_ok": today_ok,
                "month_ok": month_ok,
                "lag_bits": bits,
                "flags": flags,
                "week": week_bits,
                "month": month_bits,
                "week_delta_sum": week_delta_sum,
                "mobile_based": sid in mobile_bisuan,
            }
        )
    store_rows.sort(
        key=lambda r: (
            0 if not r["today_ok"] else 1,
            0 if r["lag_bits"] else 1,
            r["week_delta_sum"],
            r["name"],
        )
    )

    this_start, this_end = week_span(as_of)
    prev_start, prev_end = prev_week_span(as_of)
    return {
        "as_of": as_of,
        "mobile_used": bool(mobile_bisuan),
        "pace": pace,
        "days_in_month": days_in_month,
        "elapsed": elapsed,
        "kpis": kpis,
        "week_kpis": week_kpis,
        "this_week": (this_start, this_end),
        "prev_week": (prev_start, prev_end),
        "missing_today": missing_today,
        "missing_month": missing_month,
        "laggards": laggards,
        "store_rows": store_rows,
        "n": n,
        "done_today": sum(1 for s in stores if int(s["id"]) in reported_today),
        "done_month": sum(1 for s in stores if int(s["id"]) in reported_month),
        "idle_n": sum(1 for r in store_rows if not r["month_ok"]),
    }
