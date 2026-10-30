"""第四阶段接线测试：`app/db_v5.py` 数据层。

覆盖口径（对应第四阶段规范与四项裁决）：
1. 只有 V5 生效月（2026-11 起）才出表，2026-10 及以前一律拒绝；
2. 泰州垫资店、示例店、停用模式一律不进表 3 / 表 4；
3. 挂靠小店按母店工号分组做第一道核减，母店没录实结量时不能把子店清零；
4. 表 3 只落顺差让利一行，电信店没有 cat4 → 一行都不写；
5. 追回款 2026-12 及以前豁免，2027-01 起按 T+3 账龄滚动；
6. 锁月后拒绝重算与录入，解锁留审计；录入改数把已确认打回 DRAFT。

**不重测引擎算术**（`tests/test_v5_engine.py` 已覆盖），这里只验证取数、拼装与落库。
"""

from __future__ import annotations

import pytest

from app import db, db_v5, v5_engine
from app.v5_baseline import (
    MODE_ADVISOR_A,
    MODE_ADVISOR_B_NEAR,
    MODE_AFFILIATED,
    MODE_NO_ADVISOR_MAIN,
    MODE_SAMPLE_DISABLED,
    MODE_TAIZHOU_ADVANCE_ONLY,
    MODE_TELECOM_SINGLE,
)

MONTH = "2026-11"
STAMP = "2026-11-01 00:00:00"


# ---------------------------------------------------------------------------
# 小工具：直插测试门店与配置（不按 id 区间猜，全部显式 code）
# ---------------------------------------------------------------------------


def _mk_store(
    conn,
    code: str,
    mode: str,
    *,
    carrier: str = "MOBILE",
    sort_order: int = 100,
    parent: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO stores (name, code, sort_order, v5_mode, carrier_scope, affiliated_to, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (code, code, sort_order, mode, carrier, parent, STAMP),
    )
    return int(cur.lastrowid)


def _mk_baseline(
    conn,
    store_id: int,
    mode: str,
    *,
    net_cents: int = 0,
    cap_ratio: float = 0.35,
    cap_abs_cents: int = 0,
    tiers: tuple[float, float, float] = (0.20, 0.15, 0.10),
    parent: int | None = None,
    from_month: str = MONTH,
) -> None:
    conn.execute(
        """
        INSERT INTO store_baseline (store_id, from_month, to_month, v5_mode, affiliated_to,
            net_baseline_cents, target_carry_rate, carry_tier_25, carry_tier_18, carry_tier_10,
            cap_ratio, cap_abs_cents, created_at)
        VALUES (?,?,'',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            store_id,
            from_month,
            mode,
            parent,
            net_cents,
            tiers[0],
            tiers[0],
            tiers[1],
            tiers[2],
            cap_ratio,
            cap_abs_cents,
            STAMP,
        ),
    )


def _mk_tenure(
    conn,
    store_id: int,
    name: str = "王顾问",
    *,
    base_pay_cents: int = 400_000,
    base_coef: float = 1.20,
    breakeven_cents: int = 700_000,
    excess_rate: float = 0.40,
    work_days: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO advisor_tenure (store_id, advisor_name, from_month, to_month, tier_grade,
            base_pay_cents, base_coef, breakeven_cents, excess_rate, work_days, note)
        VALUES (?,?,?,'','A',?,?,?,?,?,'')
        """,
        (store_id, name, MONTH, base_pay_cents, base_coef, breakeven_cents, excess_rate, work_days),
    )


def _facts(conn, store_id: int, values: dict[str, int], biz_date: str = "2026-11-15") -> None:
    for code, value in values.items():
        conn.execute(
            "INSERT INTO daily_facts (biz_date, store_id, metric_code, day_value) VALUES (?,?,?,?)"
            " ON CONFLICT(biz_date, store_id, metric_code) DO UPDATE SET day_value=excluded.day_value",
            (biz_date, store_id, code, value),
        )


@pytest.fixture()
def scene(tmp_db):
    """一间主力母店 + 一间挂靠小店 + 一家电信店 + 一家顾问店 + 一家泰州垫资店。"""
    with db.get_db() as conn:
        main = _mk_store(conn, "t-main", MODE_NO_ADVISOR_MAIN, sort_order=10)
        sub = _mk_store(conn, "t-sub", MODE_AFFILIATED, sort_order=11, parent=main)
        tele = _mk_store(conn, "t-tele", MODE_TELECOM_SINGLE, carrier="TELECOM", sort_order=12)
        adv = _mk_store(conn, "t-adv", MODE_ADVISOR_A, sort_order=13)
        taizhou = _mk_store(conn, "t-taizhou", MODE_TAIZHOU_ADVANCE_ONLY, sort_order=14)
        _mk_baseline(conn, main, MODE_NO_ADVISOR_MAIN, net_cents=1_000_000)
        _mk_baseline(conn, sub, MODE_AFFILIATED, net_cents=500_000, parent=main)
        _mk_baseline(
            conn, tele, MODE_TELECOM_SINGLE, net_cents=100_000, tiers=(0.12, 0.09, 0.06)
        )
        _mk_baseline(conn, adv, MODE_ADVISOR_A, net_cents=700_000, cap_abs_cents=65_000)
        _mk_tenure(conn, adv)
    return {"main": main, "sub": sub, "tele": tele, "adv": adv, "taizhou": taizhou}


# ---------------------------------------------------------------------------
# 一、门禁：生效月与经营主体范围
# ---------------------------------------------------------------------------


def test_settlement_stores_covers_operating_only(scene):
    """示例店（SAMPLE_DISABLED）与泰州垫资店都不生成表 3 / 表 4。"""
    with db.get_db() as conn:
        ids = {int(row["id"]) for row in db_v5.settlement_stores(conn, MONTH)}
    assert ids == {scene["main"], scene["sub"], scene["tele"], scene["adv"]}
    assert scene["taizhou"] not in ids


def test_legacy_month_rejected(scene):
    """2026-10 及以前一律拒绝：试算、录入、落库三个入口都拦。"""
    with db.get_db() as conn:
        for month in ("2026-10", "2026-01", "2025-12"):
            with pytest.raises(ValueError):
                db_v5.compute_month(conn, month)
            with pytest.raises(ValueError):
                db_v5.apply_month(conn, month)
            with pytest.raises(ValueError):
                db_v5.save_operator_input(conn, month, scene["main"], {"net_commission_cents": 1})
        with pytest.raises(ValueError):
            db_v5.compute_month(conn, "2026-1")
        assert db_v5.list_month_settlements(conn, MONTH) == []


# ---------------------------------------------------------------------------
# 二、第一道闸：母店工号合并核减
# ---------------------------------------------------------------------------


def test_affiliated_group_reconciles_against_carrier_settlement(scene):
    """申报 20 笔 vs 实结 10 笔 → 封顶 12 笔 → 母子各按 0.6 核减到 6 笔。"""
    with db.get_db() as conn:
        _facts(conn, scene["main"], {"v5_m7": 10, "phone_sales": 40})
        _facts(conn, scene["sub"], {"v5_m7": 10, "phone_sales": 20})
        db_v5.save_operator_input(
            conn,
            MONTH,
            scene["main"],
            {
                "net_commission_cents": 200_000,
                "gross_commission_cents": 220_000,
                "settled_cat4_rebate": 10,
            },
        )
        db_v5.save_operator_input(conn, MONTH, scene["sub"], {"net_commission_cents": 50_000})
        db_v5.apply_month(conn, MONTH, user_id=1)

        sub_biz = db_v5.store_biz_rows(conn, MONTH, scene["sub"])
        main_biz = db_v5.store_biz_rows(conn, MONTH, scene["main"])
        assert len(sub_biz) == len(main_biz) == 1

        # 子店：50% 合办单价 5 元/笔，日报原报 10 笔、核定 6 笔
        assert sub_biz[0]["source_store_id"] == scene["main"]
        assert sub_biz[0]["role_share"] == db_v5.role_share_for(MODE_AFFILIATED)
        assert sub_biz[0]["daily_sum_count"] == 10
        assert sub_biz[0]["settled_count"] == 6
        assert sub_biz[0]["unit_price_cents"] == 500
        assert sub_biz[0]["raw_amount_cents"] == 3_000

        # 母店：100% 单价 10 元/笔，同样核减到 6 笔（不因总量跳达量档）
        assert main_biz[0]["unit_price_cents"] == 1_000
        assert main_biz[0]["settled_count"] == 6
        assert main_biz[0]["raw_amount_cents"] == 6_000

        rows = {row["store_id"]: row for row in db_v5.list_month_settlements(conn, MONTH)}
        assert rows[scene["main"]]["audit_flag"] == db_v5.AUDIT_REBATE_TRIMMED
        assert rows[scene["sub"]]["audit_flag"] == db_v5.AUDIT_REBATE_TRIMMED


def test_missing_carrier_settlement_keeps_child_counts(scene):
    """母店未录实结量：只打 NO_SETTLEMENT，绝不把子店整组清零。"""
    with db.get_db() as conn:
        _facts(conn, scene["main"], {"v5_m7": 8})
        _facts(conn, scene["sub"], {"v5_m7": 4})
        db_v5.apply_month(conn, MONTH)
        rows = {row["store_id"]: row for row in db_v5.list_month_settlements(conn, MONTH)}
        assert rows[scene["sub"]]["audit_flag"] == db_v5.AUDIT_NO_SETTLEMENT
        assert rows[scene["main"]]["audit_flag"] == db_v5.AUDIT_NO_SETTLEMENT
        biz = db_v5.store_biz_rows(conn, MONTH, scene["sub"])
        assert biz[0]["settled_count"] == 4
        assert biz[0]["raw_amount_cents"] == 4 * 500


# ---------------------------------------------------------------------------
# 三、表 3 / 表 4 落库
# ---------------------------------------------------------------------------


def test_apply_rewrites_biz_rows_without_duplicates(scene):
    """重算只重写表 3，留不下一堆重复行；电信店没有 cat4 → 一行都不写。"""
    with db.get_db() as conn:
        _facts(conn, scene["main"], {"v5_m7": 3})
        _facts(conn, scene["sub"], {"v5_m7": 2})
        db_v5.save_operator_input(conn, MONTH, scene["main"], {"settled_cat4_rebate": 9})
        db_v5.apply_month(conn, MONTH)
        first = conn.execute(
            "SELECT COUNT(*) AS n FROM store_month_biz WHERE month=?", (MONTH,)
        ).fetchone()["n"]
        db_v5.apply_month(conn, MONTH)
        second = conn.execute(
            "SELECT COUNT(*) AS n FROM store_month_biz WHERE month=?", (MONTH,)
        ).fetchone()["n"]
        assert first == second == 2
        assert db_v5.store_biz_rows(conn, MONTH, scene["tele"]) == []


def test_telecom_store_core_biz_ignores_preferred_member(scene):
    """电信店指标 2 分子只认 cat1/cat2/cat3 主件，权益 v5_t7 绝不进分子。"""
    with db.get_db() as conn:
        _facts(
            conn,
            scene["tele"],
            {"v5_t1": 3, "v5_t2": 0, "v5_t5": 2, "v5_t6": 1, "v5_t8": 0, "v5_t7": 9,
             "phone_sales": 50},
        )
        db_v5.save_operator_input(conn, MONTH, scene["tele"], {"net_commission_cents": 120_000})
        db_v5.apply_month(conn, MONTH)
        row = db_v5.settlement_row(conn, MONTH, scene["tele"])
        assert row["core_biz_count"] == 6
        assert row["actual_carry_rate"] == pytest.approx(0.12)
        assert row["score_2_biz"] == v5_engine.SCORE_2_MAX
        assert row["rebate_clawback_cents"] == 0
        assert db_v5.store_biz_rows(conn, MONTH, scene["tele"]) == []


def test_advisor_store_deducts_co_pool_before_salary(scene):
    """顾问店：50% 合办包先出大盘，薪酬按留存净酬金算超额分成。"""
    with db.get_db() as conn:
        _facts(conn, scene["adv"], {"v5_m7": 12, "phone_sales": 50})
        db_v5.save_operator_input(
            conn,
            MONTH,
            scene["adv"],
            {
                "net_commission_cents": 1_000_000,
                "gross_commission_cents": 1_100_000,
                "settled_cat4_rebate": 12,
            },
        )
        db_v5.apply_month(conn, MONTH)
        row = db_v5.settlement_row(conn, MONTH, scene["adv"])

        # 12 笔全在 50% 档：前 10 笔 5 元 + 2 笔 10 元
        assert row["shop_raw_pool_cents"] == 7_000
        assert row["shop_payout_ratio"] == pytest.approx(1.0)
        assert row["shop_final_pool_cents"] == 7_000
        assert row["co_pool_deducted_cents"] == 7_000
        assert row["advisor_retained_net_cents"] == 993_000
        # 底薪 4000×1.2 + (993000−700000)×40%
        assert row["advisor_base_pay_cents"] == 480_000
        assert row["breakeven_snap_cents"] == 700_000
        assert row["advisor_excess_bonus_cents"] == 117_200
        assert row["advisor_final_pay_cents"] == 597_200
        assert row["total_score_100"] == pytest.approx(100.0)
        assert row["mgr_score_20"] == pytest.approx(20.0)
        biz = db_v5.store_biz_rows(conn, MONTH, scene["adv"])
        assert biz[0]["role_share"] == db_v5.role_share_for(MODE_ADVISOR_A)


# ---------------------------------------------------------------------------
# 四、追回款：T+3 账龄 + 2027-01 起算
# ---------------------------------------------------------------------------


def test_clawback_exempt_until_2026_12_then_rolls(scene):
    """2026-12 及以前追回款恒 0；2027-01 起按满 3 个月账龄的垫资本金全额追缴。"""
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO advance_posts (store_id, user_id, created_at, biz_date, rebate, paid)"
            " VALUES (?,?,?,?,2,0)",
            (scene["main"], 1, STAMP, "2026-07-31"),
        )
        snap = db_v5.rebate_advance_snapshot(conn, scene["main"], "2026-12")
        assert snap["overdue_count"] == 2
        assert snap["overdue_cents"] == 20_000

        _facts(conn, scene["main"], {"v5_m7": 12}, biz_date="2026-12-10")
        db_v5.save_operator_input(conn, "2026-12", scene["main"], {"net_commission_cents": 900_000})
        db_v5.apply_month(conn, "2026-12")
        dec = db_v5.settlement_row(conn, "2026-12", scene["main"])
        assert dec["rebate_recovery_exempt"] == 1
        assert dec["rebate_advance_cents"] == 20_000
        assert dec["rebate_clawback_cents"] == 0

        _facts(conn, scene["main"], {"v5_m7": 12}, biz_date="2027-01-10")
        db_v5.save_operator_input(conn, "2027-01", scene["main"], {"net_commission_cents": 900_000})
        db_v5.apply_month(conn, "2027-01")
        jan = db_v5.settlement_row(conn, "2027-01", scene["main"])
        assert jan["rebate_recovery_exempt"] == 0
        assert jan["rebate_clawback_cents"] == 20_000
        # 追回款只冲减实发总包，不进扣款合计
        assert jan["total_deduction_cents"] == 0
        assert jan["shop_final_pool_cents"] == max(
            0, v5_engine.shop_final_pool(jan["shop_raw_pool_cents"], jan["shop_payout_ratio"], 0)
            - 20_000
        )


# ---------------------------------------------------------------------------
# 五、状态流转：试算 → 确认 → 锁月 → 解锁
# ---------------------------------------------------------------------------


def test_lock_blocks_recompute_and_input(scene):
    with db.get_db() as conn:
        _facts(conn, scene["main"], {"v5_m7": 3})
        db_v5.apply_month(conn, MONTH, user_id=1)

        # 还有 DRAFT 不给锁
        with pytest.raises(ValueError):
            db_v5.lock_month(conn, MONTH, user_id=1)

        assert db_v5.confirm_month(conn, MONTH, user_id=1) == 4
        assert db_v5.lock_month(conn, MONTH, user_id=1) == 4
        assert db_v5.month_totals(conn, MONTH)["locked_count"] == 4

        # 锁月后：重算跳过、录入拒绝
        assert db_v5.apply_month(conn, MONTH, user_id=1) == []
        with pytest.raises(ValueError):
            db_v5.save_operator_input(conn, MONTH, scene["main"], {"net_commission_cents": 1})

        assert db_v5.unlock_month(conn, MONTH, user_id=1, reason="运营商补录 11 月酬金") == 4
        assert db_v5.month_totals(conn, MONTH)["confirmed_count"] == 4
        assert db_v5.apply_month(conn, MONTH, user_id=1)

        log = [
            dict(row)
            for row in conn.execute(
                "SELECT action, reason FROM settlement_audit_log"
                " WHERE month=? AND store_id=? ORDER BY id",
                (MONTH, scene["main"]),
            )
        ]
        actions = [row["action"] for row in log]
        assert actions[0] == db_v5.ACTION_COMPUTE
        assert db_v5.ACTION_LOCK in actions and db_v5.ACTION_UNLOCK in actions
        assert any(row["reason"] == "运营商补录 11 月酬金" for row in log)


def test_operator_input_rejects_dirty_values_and_resets_status(scene):
    """脏输入一律落 0 不抛错；录入改数后已确认的月结退回 DRAFT。"""
    with db.get_db() as conn:
        saved = db_v5.save_operator_input(
            conn,
            MONTH,
            scene["main"],
            {"net_commission_cents": "12.6", "gross_commission_cents": None,
             "settled_cat1_fusion": "abc", "override_remark": "  运营商口径  "},
        )
        assert saved["net_commission_cents"] == 13
        assert saved["gross_commission_cents"] == 0
        assert saved["settled_cat1_fusion"] == 0
        assert saved["override_remark"] == "运营商口径"

        db_v5.apply_month(conn, MONTH)
        db_v5.confirm_month(conn, MONTH)
        assert db_v5.settlement_status(conn, MONTH, scene["main"]) == db_v5.STATUS_CONFIRMED

        db_v5.save_operator_input(conn, MONTH, scene["main"], {"net_commission_cents": 55})
        assert db_v5.settlement_status(conn, MONTH, scene["main"]) == db_v5.STATUS_DRAFT
        # 录入不动计算列，必须重新试算才生效
        row = db_v5.settlement_row(conn, MONTH, scene["main"])
        assert row["net_commission_cents"] == 55
        assert row["v5_mode_snap"] == MODE_NO_ADVISOR_MAIN


# ---------------------------------------------------------------------------
# 六、口径派生的纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "share", "advisor"),
    [
        (MODE_NO_ADVISOR_MAIN, "SHOP_FULL_100", False),
        (MODE_AFFILIATED, "AFFILIATED_SHOP_50", False),
        (MODE_ADVISOR_A, "ADVISOR_SHOP_50", True),
        (MODE_ADVISOR_B_NEAR, "ADVISOR_SHOP_50", True),
        (MODE_TELECOM_SINGLE, "SHOP_FULL_100", False),
        (MODE_TAIZHOU_ADVANCE_ONLY, "SHOP_FULL_100", False),
    ],
)
def test_role_share_for(mode, share, advisor):
    assert db_v5.role_share_for(mode) == share
    assert db_v5.is_advisor_mode(mode) == advisor


def test_root_of_survives_broken_link_and_cycle():
    """断链回落自身；两个店互相挂靠也不能死循环。"""
    stores = {
        1: {"id": 1, "affiliated_to": None},
        2: {"id": 2, "affiliated_to": 1},
        3: {"id": 3, "affiliated_to": 99},
        4: {"id": 4, "affiliated_to": 5},
        5: {"id": 5, "affiliated_to": 4},
    }
    assert db_v5._root_of(stores, 2) == 1
    assert db_v5._root_of(stores, 3) == 3
    assert db_v5._root_of(stores, 4) == 4
    assert db_v5._root_of(stores, 42) == 42


def test_rebate_biz_code_follows_carrier():
    assert db_v5.rebate_biz_code(MODE_NO_ADVISOR_MAIN) == v5_engine.REBATE_METRIC_CODE
    assert db_v5.rebate_biz_code(MODE_TELECOM_SINGLE) == ""


# ---------------------------------------------------------------------------
# 七、汇总
# ---------------------------------------------------------------------------


def test_summarize_totals_and_flags():
    rows = [
        {
            "status": db_v5.STATUS_DRAFT,
            "audit_flag": db_v5.AUDIT_OK,
            "gross_commission_cents": 100_000,
            "net_commission_cents": 90_000,
            "shop_raw_pool_cents": 5_000,
            "shop_final_pool_cents": 4_000,
            "co_pool_deducted_cents": 0,
            "rebate_clawback_cents": 0,
            "advisor_final_pay_cents": 0,
            "core_biz_count": 7,
            "total_score_100": 80.0,
        },
        {
            "status": db_v5.STATUS_LOCKED,
            "audit_flag": db_v5.AUDIT_REBATE_TRIMMED,
            "gross_commission_cents": 50_000,
            "net_commission_cents": 40_000,
            "shop_raw_pool_cents": 3_000,
            "shop_final_pool_cents": 2_000,
            "co_pool_deducted_cents": 3_000,
            "rebate_clawback_cents": 1_000,
            "advisor_final_pay_cents": 600_000,
            "core_biz_count": 3,
            "total_score_100": 91.0,
        },
    ]
    summary = db_v5.summarize(rows)
    assert summary["store_count"] == 2
    assert summary["net_commission_cents"] == 130_000
    assert summary["shop_final_pool_cents"] == 6_000
    assert summary["rebate_clawback_cents"] == 1_000
    assert summary["avg_score_100"] == pytest.approx(85.5)
    assert summary["draft_count"] == 1 and summary["locked_count"] == 1
    assert summary["advisor_count"] == 1 and summary["clawback_count"] == 1
    assert summary["flag_counts"] == {db_v5.AUDIT_OK: 1, db_v5.AUDIT_REBATE_TRIMMED: 1}
    assert db_v5.summarize([])["avg_score_100"] == 0.0


def test_sample_stores_never_produce_rows(tmp_db):
    """示例店（SAMPLE_DISABLED）即使有日报也不出表。"""
    with db.get_db() as conn:
        sample_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM stores WHERE v5_mode=?", (MODE_SAMPLE_DISABLED,)
            )
        ]
        assert sample_ids
        for store_id in sample_ids:
            _facts(conn, store_id, {"v5_m7": 5})
        assert db_v5.compute_month(conn, MONTH) == []
