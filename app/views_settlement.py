"""V5 月结后台：试算、录入、确认、锁月与导出。

流程一条线：录入运营商实结 → 试算并落库（草稿）→ 全月确认 → 锁月。
锁月后拒绝重算与录入；解锁必须写原因，动作全部留痕在表 6。
2026-10 及以前不生成，一律按原口径，不回溯、不重算。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from flask import Response, flash, g, redirect, render_template, request, url_for

from . import db, db_v5
from .helpers import admin_required, xlsx_response, xlsx_sheets
from .v5_baseline import V5_START_MONTH, mode_label, month_key, v5_month_enabled

# 运营商月结录入：金额一律按「元」输入，落库换算成分（库里全是分，int）。
AMOUNT_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("gross_commission_cents", "运营商毛酬金"),
    ("commissioning_fee_cents", "调测费"),
    ("total_deduction_cents", "扣款合计"),
    ("deduct_3b_quality_cents", "3B 质量"),
    ("deduct_3b_dispute_cents", "3B 争议"),
    ("deduct_3b_workorder_cents", "3B 工单"),
    ("deduct_3b_luckynum_cents", "3B 靓号"),
    ("net_commission_cents", "实结净酬金"),
)

# 实结量：四大类笔数/台数，第一道核减与指标 2 都用它，填「0」等于确认当月没出量。
COUNT_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("settled_cat1_fusion", "融合主件"),
    ("settled_cat2_contract", "合约/金币"),
    ("settled_cat3_ai", "AI 终端"),
    ("settled_cat4_rebate", "顺差让利"),
    ("settled_cat4_rebate_member", "让利会员"),
)

# 布尔项：勾上即 1，出现即整列扣分/整月豁免，界面必须写清楚。
FLAG_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("broadband_star_penalty", "宽带星级扣罚（勾上扣 3A）"),
    ("exempt_cross_month", "跨月豁免（勾上不走折后比例）"),
)

STATUS_LABELS: Dict[str, Tuple[str, str]] = {
    db_v5.STATUS_DRAFT: ("草稿", "wait"),
    db_v5.STATUS_CONFIRMED: ("已确认", "done"),
    db_v5.STATUS_LOCKED: ("已锁月", "done"),
}

FLAG_LABELS: Dict[str, Tuple[str, str]] = {
    db_v5.AUDIT_OK: ("正常", "done"),
    db_v5.AUDIT_NET_NEGATIVE: ("净酬金为负", "miss"),
    db_v5.AUDIT_NO_SETTLEMENT: ("未录实结", "wait"),
    db_v5.AUDIT_REBATE_TRIMMED: ("让利核减", "wait"),
    db_v5.AUDIT_BASELINE_MISSING: ("缺基线", "miss"),
    db_v5.AUDIT_ADVISOR_MISSING: ("缺顾问任期", "miss"),
}
ACTION_LABELS: Dict[str, str] = {
    db_v5.ACTION_COMPUTE: "试算落库",
    db_v5.ACTION_CONFIRM: "确认",
    db_v5.ACTION_LOCK: "锁月",
    db_v5.ACTION_UNLOCK: "解锁",
    db_v5.ACTION_INPUT: "录入实结",
    db_v5.ACTION_SPLIT: "二次分配",
}


def _int(value: Any) -> int:
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _money(cents: Any, *, zero_dash: bool = False) -> str:
    """分 → 「1,234.56」；zero_dash 时 0 显示破折号，表里一片 0.00 太难读。"""
    value = _int(cents)
    if zero_dash and value == 0:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value) / 100:,.2f}"


def _rate(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def _score(value: Any) -> str:
    return f"{float(value or 0):.2f}"


def _month() -> str:
    """查询参数里的月份；认不出就落到「当月与生效月取大」。"""
    today_key = month_key(db.today_local())
    fallback = today_key if v5_month_enabled(today_key) else V5_START_MONTH
    raw = request.values.get("month")
    key = month_key(raw) if raw else ""
    return key or fallback


def _post_month() -> str:
    """写入口径的月份：表单必填，缺省用 _month() 兜底。"""
    key = month_key(request.form.get("month"))
    return key or _month()


def _uid() -> int:
    return _int(g.user["id"]) if g.user is not None else 0


def _form_cents(raw: Any, label: str) -> int:
    text = str(raw or "").strip().replace(",", "").replace("，", "")
    if not text:
        return 0
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"{label} 不是有效金额") from None
    if not math.isfinite(number) or abs(number) > 10_000_000:
        raise ValueError(f"{label} 超出范围（一千万以内）")
    return int(round(number * 100))


def _form_int(raw: Any, label: str) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        return max(int(float(text)), 0)
    except ValueError:
        raise ValueError(f"{label} 不是有效数量") from None


def _view_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """表 4 一行 → 模板直出的展示行（钱已是字符串，模板不做算术）。"""
    status = str(row.get("status") or "")
    flag = str(row.get("audit_flag") or "")
    status_label, status_class = STATUS_LABELS.get(status, ("未落库", "miss"))
    flag_label, flag_class = FLAG_LABELS.get(flag, ("—", "miss"))
    return {
        "store_id": _int(row.get("store_id")),
        "store_code": str(row.get("store_code") or ""),
        "store_name": str(row.get("store_name") or ""),
        "store_manager": str(row.get("store_manager") or ""),
        "region_group": str(row.get("region_group") or ""),
        "mode": mode_label(str(row.get("v5_mode_snap") or row.get("v5_mode") or "")),
        "status": status,
        "status_label": status_label,
        "status_class": status_class,
        "flag_label": flag_label,
        "flag_class": flag_class,
        "net": _money(row.get("net_commission_cents")),
        "gross": _money(row.get("gross_commission_cents"), zero_dash=True),
        "baseline": _money(row.get("net_baseline_snap_cents")),
        "carry": _rate(row.get("actual_carry_rate")),
        "core_biz": _int(row.get("core_biz_count")),
        "phone_sales": _int(row.get("phone_sales_snap")),
        "score": _score(row.get("total_score_100")),
        "raw_pool": _money(row.get("shop_raw_pool_cents")),
        "payout": _rate(row.get("shop_payout_ratio")),
        "final_pool": _money(row.get("shop_final_pool_cents")),
        "co_pool": _money(row.get("co_pool_deducted_cents"), zero_dash=True),
        "clawback": _money(row.get("rebate_clawback_cents"), zero_dash=True),
        "advisor_pay": _money(row.get("advisor_final_pay_cents"), zero_dash=True),
        "advisor_name": str(row.get("advisor_name_snap") or ""),
        "score_1": _score(row.get("score_1_net")),
        "score_2": _score(row.get("score_2_biz")),
        "score_3a": _score(row.get("score_3a_deduct")),
        "score_3b": _score(row.get("score_3b_redline")),
        "score_total": _score(row.get("total_score_100")),
        "mgr20": _score(row.get("mgr_score_20")),
        "mgr15": _score(row.get("mgr_score_15")),
        "remark": str(row.get("override_remark") or ""),
        "locked_at": str(row.get("locked_at") or ""),
    }


def _biz_view(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "role": str(row.get("role_share") or ""),
            "biz_code": str(row.get("biz_code") or ""),
            "source": str(row.get("source_store_name") or ""),
            "daily": _int(row.get("daily_sum_count")),
            "settled": _int(row.get("settled_count")),
            "unit": _money(row.get("unit_price_cents")),
            "amount": _money(row.get("raw_amount_cents")),
        }
        for row in rows
    ]

def _summary_view(totals: Mapping[str, Any]) -> Dict[str, Any]:
    """大盘汇总 → 卡片直出的字符串（钱按元、分数两位）。"""
    return {
        "store_count": _int(totals.get("store_count")),
        "draft_count": _int(totals.get("draft_count")),
        "confirmed_count": _int(totals.get("confirmed_count")),
        "locked_count": _int(totals.get("locked_count")),
        "advisor_count": _int(totals.get("advisor_count")),
        "clawback_count": _int(totals.get("clawback_count")),
        "net": _money(totals.get("net_commission_cents")),
        "gross": _money(totals.get("gross_commission_cents"), zero_dash=True),
        "raw_pool": _money(totals.get("shop_raw_pool_cents")),
        "final_pool": _money(totals.get("shop_final_pool_cents")),
        "co_pool": _money(totals.get("co_pool_deducted_cents"), zero_dash=True),
        "clawback": _money(totals.get("rebate_clawback_cents"), zero_dash=True),
        "advisor_pay": _money(totals.get("advisor_final_pay_cents"), zero_dash=True),
        "core_biz": _int(totals.get("core_biz_count")),
        "avg_score": _score(totals.get("avg_score_100")),
    }


def _audit_view(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """表 6 → 页面行：动作翻中文，系统动作没有操作人时写「系统」。"""
    out: List[Dict[str, Any]] = []
    for row in rows:
        action = str(row.get("action") or "")
        out.append(
            {
                "action": ACTION_LABELS.get(action, action or "—"),
                "user": str(row.get("user_name") or "系统"),
                "created_at": str(row.get("created_at") or "").replace("T", " "),
                "reason": str(row.get("reason") or ""),
            }
        )
    return out


def _money_value(cents: Any) -> float:
    """xlsx 数值列：分 → 元，两位小数（Excel 里能直接求和）。"""
    return round(_int(cents) / 100, 2)


def _split_view(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """表 5 → 页面行：金额给元字符串与可编辑的纯数字两种形态。"""
    return [
        {
            "emp_code": str(row.get("emp_code") or ""),
            "emp_name": str(row.get("emp_name") or ""),
            "role_tag": str(row.get("role_tag") or ""),
            "amount": _money(row.get("amount_cents"), zero_dash=True),
            "amount_cents": _int(row.get("amount_cents")),
            "amount_input": f"{_int(row.get('amount_cents')) / 100:.2f}",
            "remark": str(row.get("remark") or ""),
        }
        for row in rows
    ]


def _export_workbook(conn: Any, month: str) -> bytes:
    """财务月结四张标准表：大盘、店长卡、顾问工资单、导购二次分配明细。"""
    rows = db_v5.list_month_settlements(conn, month)
    splits = db_v5.month_split_rows(conn, month)
    sheet1 = [
        [
            month,
            str(row.get("store_code") or ""),
            str(row.get("store_name") or ""),
            str(row.get("store_manager") or ""),
            str(row.get("region_group") or ""),
            mode_label(str(row.get("v5_mode_snap") or "")),
            STATUS_LABELS.get(str(row.get("status") or ""), ("未落库", ""))[0],
            _money_value(row.get("gross_commission_cents")),
            _money_value(row.get("commissioning_fee_cents")),
            _money_value(row.get("total_deduction_cents")),
            _money_value(row.get("net_commission_cents")),
            _int(row.get("settled_cat1_fusion")),
            _int(row.get("settled_cat2_contract")),
            _int(row.get("settled_cat3_ai")),
            _int(row.get("settled_cat4_rebate")),
            _int(row.get("core_biz_count")),
            _int(row.get("phone_sales_snap")),
            _rate(row.get("actual_carry_rate")),
            _money_value(row.get("shop_raw_pool_cents")),
            _rate(row.get("shop_payout_ratio")),
            _money_value(row.get("shop_final_pool_cents")),
            _money_value(row.get("co_pool_deducted_cents")),
            _money_value(row.get("rebate_clawback_cents")),
            _money_value(row.get("advisor_final_pay_cents")),
            _score(row.get("total_score_100")),
            FLAG_LABELS.get(str(row.get("audit_flag") or ""), ("—", ""))[0],
        ]
        for row in rows
    ]
    sheet2 = [
        [
            month,
            str(row.get("store_code") or ""),
            str(row.get("store_name") or ""),
            _score(row.get("score_1_net")),
            _score(row.get("score_2_biz")),
            _score(row.get("score_3a_deduct")),
            _score(row.get("score_3b_redline")),
            _score(row.get("total_score_100")),
            _score(row.get("mgr_score_20")),
            _score(row.get("mgr_score_15")),
            FLAG_LABELS.get(str(row.get("audit_flag") or ""), ("—", ""))[0],
        ]
        for row in rows
    ]
    sheet3 = [
        [
            month,
            str(row.get("store_code") or ""),
            str(row.get("store_name") or ""),
            str(row.get("advisor_name_snap") or ""),
            "在任" if str(row.get("advisor_name_snap") or "") else "缺任期",
            _money_value(row.get("advisor_base_pay_cents")),
            f"{float(row.get('advisor_sys_coef') or 0):.4f}",
            _money_value(row.get("breakeven_snap_cents")),
            _money_value(row.get("advisor_excess_bonus_cents")),
            _money_value(row.get("advisor_3b_penalty_cents")),
            _money_value(row.get("advisor_final_pay_cents")),
        ]
        for row in rows
        if db_v5.is_advisor_mode(str(row.get("v5_mode_snap") or ""))
    ]
    sheet4 = [
        [
            month,
            str(row.get("store_code") or ""),
            str(row.get("store_name") or ""),
            str(row.get("emp_code") or ""),
            str(row.get("emp_name") or ""),
            str(row.get("role_tag") or ""),
            _money_value(row.get("amount_cents")),
            str(row.get("remark") or ""),
        ]
        for row in splits
    ]
    return xlsx_sheets(
        [
            {
                "title": "1-门店月度结算大盘",
                "header": [
                    "月份",
                    "门店编码",
                    "门店",
                    "店长",
                    "区域",
                    "模式",
                    "状态",
                    "毛酬金(元)",
                    "调测费(元)",
                    "负向扣款(元)",
                    "净酬金(元)",
                    "融合主件",
                    "合约金币",
                    "AI终端",
                    "顺差让利",
                    "核心随销量",
                    "手机销量",
                    "实际搭载率",
                    "折前总包(元)",
                    "全店熔断系数",
                    "折后总包(元)",
                    "合办扣减(元)",
                    "垫资追回(元)",
                    "顾问实发(元)",
                    "店长分",
                    "告警",
                ],
                "rows": sheet1,
            },
            {
                "title": "2-店长百分考核卡",
                "header": [
                    "月份",
                    "门店编码",
                    "门店",
                    "指标1净酬金(60)",
                    "指标2搭载率(25)",
                    "指标3A扣款率(10)",
                    "指标3B红线(5)",
                    "合计(100)",
                    "20分制",
                    "15分制",
                    "告警",
                ],
                "rows": sheet2,
            },
            {
                "title": "3-专职顾问工资单",
                "header": [
                    "月份",
                    "门店编码",
                    "门店",
                    "顾问",
                    "任期状态",
                    "考核底薪(元)",
                    "考核系数",
                    "平衡点(元)",
                    "净超额分成(元)",
                    "3B连带扣罚(元)",
                    "实发合计(元)",
                ],
                "rows": sheet3,
            },
            {
                "title": "4-导购二次分配",
                "header": [
                    "月份",
                    "门店编码",
                    "门店",
                    "工号",
                    "姓名",
                    "角色",
                    "分配金额(元)",
                    "备注",
                ],
                "rows": sheet4,
            },
        ]
    )
def register_settlement(app) -> None:
    """V5 月结路由：全部只给管理员，出账口径不能开着给填报员改。"""

    @app.route("/settlement")
    @admin_required
    def settlement_page():
        """月结总览：当月大盘 + 门店逐行。没落库时直接给试算预览（标注未落库）。"""
        month = _month()
        enabled = v5_month_enabled(month)
        preview_only = False
        with db.get_db() as conn:
            rows: List[Dict[str, Any]] = []
            if enabled:
                rows = db_v5.list_month_settlements(conn, month)
                if not rows:
                    rows = db_v5.compute_month(conn, month)
                    preview_only = True
        summary = _summary_view(db_v5.summarize(rows))
        return render_template(
            "settlement.html",
            month=month,
            enabled=enabled,
            start_month=V5_START_MONTH,
            preview_only=preview_only,
            rows=[_view_row(row) for row in rows],
            summary=summary,
        )

    @app.route("/settlement/compute", methods=["POST"])
    @admin_required
    def settlement_compute():
        """试算并落库：表 3/表 4 整体重写，状态一律回到草稿。"""
        month = _post_month()
        try:
            with db.get_db() as conn:
                rows = db_v5.apply_month(conn, month, user_id=_uid())
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"{month} 试算完成，{len(rows)} 家门店已落库（草稿），核对无误再确认", "ok")
        return redirect(url_for("settlement_page", month=month))

    @app.route("/settlement/confirm", methods=["POST"])
    @admin_required
    def settlement_confirm():
        """全月确认：只动草稿行，算完没再改数才允许确认。"""
        month = _post_month()
        try:
            with db.get_db() as conn:
                count = db_v5.confirm_month(conn, month, user_id=_uid())
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            if count:
                flash(f"{month} 已确认 {count} 家门店", "ok")
            else:
                flash(f"{month} 没有待确认的草稿行", "error")
        return redirect(url_for("settlement_page", month=month))

    @app.route("/settlement/lock", methods=["POST"])
    @admin_required
    def settlement_lock():
        """锁月：锁完这月的账就冻住，拒绝重算和改录入。"""
        month = _post_month()
        try:
            with db.get_db() as conn:
                count = db_v5.lock_month(conn, month, user_id=_uid())
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"{month} 已锁月，共 {count} 家门店", "ok")
        return redirect(url_for("settlement_page", month=month))

    @app.route("/settlement/unlock", methods=["POST"])
    @admin_required
    def settlement_unlock():
        """解锁必须写原因：钱的事，事后得能查清是谁、为什么放开的。"""
        month = _post_month()
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            flash("解锁必须填写原因", "error")
            return redirect(url_for("settlement_page", month=month))
        try:
            with db.get_db() as conn:
                count = db_v5.unlock_month(conn, month, user_id=_uid(), reason=reason)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"{month} 已解锁 {count} 家门店，原因已留痕", "ok")
        return redirect(url_for("settlement_page", month=month))

    @app.route("/settlement/export.xlsx")
    @admin_required
    def settlement_export():
        """财务交付物：Sheet 1 大盘 / Sheet 2 店长卡 / Sheet 3 顾问单 / Sheet 4 二次分配。"""
        month = _month()
        if not v5_month_enabled(month):
            return Response(f"{month} 未启用 V5 月结", status=400, mimetype="text/plain")
        with db.get_db() as conn:
            data = _export_workbook(conn, month)
        return xlsx_response(data, f"v5_settlement_{month.replace('-', '_')}.xlsx")

    @app.route("/settlement.xlsx")
    @admin_required
    def settlement_xlsx():
        """旧导出名保留：一律转到标准四表 workbook，别留两套口径。"""
        return redirect(url_for("settlement_export", month=_month()))

    @app.route("/settlement.csv")
    @admin_required
    def settlement_csv():
        """旧 CSV 别名：直接转到 .xlsx，别留两条导出实现。"""
        return redirect(url_for("settlement_export", month=_month()))

    @app.route("/settlement/<int:store_id>")
    @admin_required
    def settlement_store(store_id: int):
        """单店月结：录入运营商实结 + 表 3 明细 + 打分明细 + 最近操作。"""
        month = _month()
        if not v5_month_enabled(month):
            flash(f"V5 月结只对 {V5_START_MONTH} 及以后月份生成；{month} 按原口径", "error")
            return redirect(url_for("settlement_page", month=month))
        with db.get_db() as conn:
            row = db_v5.settlement_row(conn, month, store_id)
            preview_only = False
            if not row:
                computed = [
                    item
                    for item in db_v5.compute_month(conn, month)
                    if _int(item["store_id"]) == store_id
                ]
                if not computed:
                    flash("这家店不在当月经营主体范围内（停用模式或泰州垫资店）", "error")
                    return redirect(url_for("settlement_page", month=month))
                row = computed[0]
                preview_only = True
            oper = db_v5.operator_input(conn, month, store_id)
            biz = _biz_view(db_v5.store_biz_rows(conn, month, store_id))
            audits = db_v5.audit_log(conn, month, store_id, limit=20)
            splits = db_v5.list_store_splits(conn, month, store_id)
        form = {name: ("" if _int(oper.get(name)) == 0 else _money(oper.get(name), zero_dash=False))
                for name, _label in AMOUNT_FIELDS}
        counts = {name: _int(oper.get(name)) for name, _label in COUNT_FIELDS}
        split_rows = _split_view(splits)
        split_total_cents = sum(item["amount_cents"] for item in split_rows)
        split_pool_cents = _int(row.get("shop_final_pool_cents"))
        flags = {name: _int(oper.get(name)) > 0 for name, _label in FLAG_FIELDS}
        return render_template(
            "settlement_store.html",
            month=month,
            preview_only=preview_only,
            row=_view_row(row),
            locked=str(row.get("status") or "") == db_v5.STATUS_LOCKED,
            amount_fields=AMOUNT_FIELDS,
            count_fields=COUNT_FIELDS,
            flag_fields=FLAG_FIELDS,
            form=form,
            counts=counts,
            flags=flags,
            remark=str(oper.get("override_remark") or ""),
            biz=biz,
            audits=_audit_view(audits),
            split_rows=split_rows,
            split_roles=db_v5.SPLIT_ROLE_TAGS,
            split_total=split_total_cents,
            split_pool=split_pool_cents,
            split_diff=split_pool_cents - split_total_cents,
        )

    @app.route("/settlement/<int:store_id>/input", methods=["POST"])
    @admin_required
    def settlement_input(store_id: int):
        """录入运营商月结：只写录入列，状态打回草稿，必须重新试算。"""
        month = _post_month()
        data: Dict[str, Any] = {}
        try:
            for name, label in AMOUNT_FIELDS:
                data[name] = _form_cents(request.form.get(name), label)
            for name, label in COUNT_FIELDS:
                data[name] = _form_int(request.form.get(name), label)
            for name, _label in FLAG_FIELDS:
                data[name] = 1 if request.form.get(name) else 0
            data["override_remark"] = (request.form.get("override_remark") or "").strip()
            with db.get_db() as conn:
                db_v5.save_operator_input(conn, month, store_id, data, user_id=_uid())
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("录入已保存并退回草稿：请重新试算后再确认", "ok")
        return redirect(url_for("settlement_store", store_id=store_id, month=month))

    @app.route("/settlement/<int:store_id>/split", methods=["POST"])
    @admin_required
    def settlement_split(store_id: int):
        """表 5 备案：店长二次分配给导购，合计必须一分不差等于门店最终池。"""
        month = _post_month()
        codes = request.form.getlist("emp_code")
        names = request.form.getlist("emp_name")
        roles = request.form.getlist("role_tag")
        amounts = request.form.getlist("amount_cents")
        remarks = request.form.getlist("remark")
        rows: List[Dict[str, Any]] = []
        try:
            for index in range(len(names)):
                rows.append(
                    {
                        "emp_code": codes[index] if index < len(codes) else "",
                        "emp_name": names[index],
                        "role_tag": roles[index] if index < len(roles) else "",
                        "amount_cents": _form_cents(
                            amounts[index] if index < len(amounts) else "",
                            f"第 {index + 1} 行分配金额",
                        ),
                        "remark": remarks[index] if index < len(remarks) else "",
                    }
                )
            with db.get_db() as conn:
                count = db_v5.save_store_splits(conn, month, store_id, rows, user_id=_uid())
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash(f"二次分配已备案 {count} 人，合计与门店最终池一致", "ok")
        return redirect(url_for("settlement_store", store_id=store_id, month=month))
