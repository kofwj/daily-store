"""V5 月结后台视图测试：页面渲染、权限、试算→确认→锁月流程、录入拦截、导出。

**不重测算术**（`tests/test_v5_engine.py` / `tests/test_v5_db.py` 已覆盖），
这里只验证 Flask 这一层：模板能渲染、门禁能拦住、按钮背后真的改了库。
"""

from __future__ import annotations

import pytest

from app import db, db_v5
from app.v5_baseline import (
    MODE_ADVISOR_A,
    MODE_AFFILIATED,
    MODE_NO_ADVISOR_MAIN,
    MODE_TAIZHOU_ADVANCE_ONLY,
)

MONTH = "2026-11"
STAMP = "2026-11-01 00:00:00"


def _mk_store(conn, code: str, mode: str, *, sort_order: int = 100, parent=None) -> int:
    cur = conn.execute(
        "INSERT INTO stores (name, code, sort_order, v5_mode, carrier_scope, affiliated_to,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (code, code, sort_order, mode, "MOBILE", parent, STAMP),
    )
    return int(cur.lastrowid)


def _mk_baseline(conn, store_id: int, mode: str, *, net_cents: int, parent=None) -> None:
    conn.execute(
        "INSERT INTO store_baseline (store_id, from_month, to_month, v5_mode, affiliated_to,"
        " net_baseline_cents, target_carry_rate, carry_tier_25, carry_tier_18, carry_tier_10,"
        " cap_ratio, cap_abs_cents, created_at) VALUES (?,?,'',?,?,?,?,?,?,?,?,?,?)",
        (store_id, MONTH, mode, parent, net_cents, 0.20, 0.20, 0.15, 0.10, 0.35, 0, STAMP),
    )


@pytest.fixture()
def scene(tmp_db):
    """一间母店 + 一间挂靠小店 + 一家顾问店 + 一家泰州垫资店（不进表 3/4）。"""
    with db.get_db() as conn:
        main = _mk_store(conn, "v-main", MODE_NO_ADVISOR_MAIN, sort_order=10)
        sub = _mk_store(conn, "v-sub", MODE_AFFILIATED, sort_order=11, parent=main)
        adv = _mk_store(conn, "v-adv", MODE_ADVISOR_A, sort_order=12)
        taizhou = _mk_store(conn, "v-tz", MODE_TAIZHOU_ADVANCE_ONLY, sort_order=13)
        _mk_baseline(conn, main, MODE_NO_ADVISOR_MAIN, net_cents=1_000_000)
        _mk_baseline(conn, sub, MODE_AFFILIATED, net_cents=500_000, parent=main)
        _mk_baseline(conn, adv, MODE_ADVISOR_A, net_cents=700_000)
        conn.execute(
            "INSERT INTO advisor_tenure (store_id, advisor_name, from_month, to_month, tier_grade,"
            " base_pay_cents, base_coef, breakeven_cents, excess_rate, work_days, note)"
            " VALUES (?,?,'','','A',400000,1.20,700000,0.40,0,'')",
            (adv, "王顾问"),
        )
    return {"main": main, "sub": sub, "adv": adv, "taizhou": taizhou}


def _lock_month(client) -> None:
    """走完试算 → 确认 → 锁月（锁月要求全月都已确认）。"""
    client.post("/settlement/compute", data={"month": MONTH})
    client.post("/settlement/confirm", data={"month": MONTH})
    client.post("/settlement/lock", data={"month": MONTH})

# ---------------------------------------------------------------------------
# 一、页面与权限
# ---------------------------------------------------------------------------


def test_page_previews_when_month_not_computed(admin_client, scene):
    """没落库时给试算预览：管理员不该先点一次「试算」才能看见数字。"""
    resp = admin_client.get(f"/settlement?month={MONTH}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "未落库" not in html or "试算" in html
    assert "v-main" in html and "v-adv" in html
    assert "泰州" not in html
    assert "试算预览" in html


def test_page_hides_legacy_month(admin_client, scene):
    """2026-10 及以前不生成 V5 账，页面只提示按原口径。"""
    resp = admin_client.get("/settlement?month=2026-10")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "未启用 V5 月结" in html
    assert "试算并落库" not in html


def test_page_requires_admin(filler_client):
    """填报员进不去：出账口径不能开着给填报表的人改。"""
    resp = filler_client.get(f"/settlement?month={MONTH}")
    assert resp.status_code == 302
    assert "/settlement" not in resp.headers["Location"]


# ---------------------------------------------------------------------------
# 二、试算 → 确认 → 锁月 → 解锁
# ---------------------------------------------------------------------------


def test_compute_confirm_lock_flow(admin_client, scene):
    resp = admin_client.post("/settlement/compute", data={"month": MONTH}, follow_redirects=True)
    assert resp.status_code == 200
    assert "试算完成" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        rows = db_v5.list_month_settlements(conn, MONTH)
    assert {row["store_id"] for row in rows} == {scene["main"], scene["sub"], scene["adv"]}
    assert {row["status"] for row in rows} == {db_v5.STATUS_DRAFT}

    resp = admin_client.post("/settlement/confirm", data={"month": MONTH}, follow_redirects=True)
    assert "已确认" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert {row["status"] for row in db_v5.list_month_settlements(conn, MONTH)} == {
            db_v5.STATUS_CONFIRMED
        }

    resp = admin_client.post("/settlement/lock", data={"month": MONTH}, follow_redirects=True)
    assert "已锁月" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert {row["status"] for row in db_v5.list_month_settlements(conn, MONTH)} == {
            db_v5.STATUS_LOCKED
        }


def test_unlock_without_reason_refused(admin_client, scene):
    """解锁不写原因直接打回：钱的事事后得能查清是谁放开的。"""
    _lock_month(admin_client)
    resp = admin_client.post(
        "/settlement/unlock", data={"month": MONTH, "reason": "   "}, follow_redirects=True
    )
    assert "解锁必须填写原因" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert {row["status"] for row in db_v5.list_month_settlements(conn, MONTH)} == {
            db_v5.STATUS_LOCKED
        }

    resp = admin_client.post(
        "/settlement/unlock",
        data={"month": MONTH, "reason": "运营商补发 3B 争议扣款"},
        follow_redirects=True,
    )
    assert "已解锁" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert {row["status"] for row in db_v5.list_month_settlements(conn, MONTH)} == {
            db_v5.STATUS_CONFIRMED
        }


# ---------------------------------------------------------------------------
# 三、单店录入
# ---------------------------------------------------------------------------


def test_store_page_renders_inputs(admin_client, scene):
    resp = admin_client.get(f"/settlement/{scene['adv']}?month={MONTH}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "运营商实结录入" in html and "店长 100 分考核明细" in html
    assert "王顾问" in html


def test_store_out_of_scope_redirects(admin_client, scene):
    """泰州垫资店不该有月结明细页，直接打回列表。"""
    resp = admin_client.get(f"/settlement/{scene['taizhou']}?month={MONTH}")
    assert resp.status_code == 302
    assert "/settlement" in resp.headers["Location"]


def test_input_saves_into_draft_and_persists(admin_client, scene):
    resp = admin_client.post(
        f"/settlement/{scene['adv']}/input",
        data={
            "month": MONTH,
            "gross_commission_cents": "12,000.50",
            "net_commission_cents": "9000",
            "settled_cat1_fusion": "3",
            "broadband_star_penalty": "1",
            "override_remark": "运营商补发",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "录入已保存" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        row = db_v5.operator_input(conn, MONTH, scene["adv"])
        status = db_v5.settlement_status(conn, MONTH, scene["adv"])
    assert row["gross_commission_cents"] == 1_200_050
    assert row["net_commission_cents"] == 900_000
    assert row["settled_cat1_fusion"] == 3
    assert row["broadband_star_penalty"] == 1
    assert row["override_remark"] == "运营商补发"
    assert status == db_v5.STATUS_DRAFT


def test_input_rejects_junk_amount(admin_client, scene):
    resp = admin_client.post(
        f"/settlement/{scene['adv']}/input",
        data={"month": MONTH, "gross_commission_cents": "abc"},
        follow_redirects=True,
    )
    assert "不是有效金额" in resp.get_data(as_text=True)


def test_input_refused_after_lock(admin_client, scene):
    _lock_month(admin_client)
    resp = admin_client.post(
        f"/settlement/{scene['adv']}/input",
        data={"month": MONTH, "net_commission_cents": "1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "已锁月" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        row = db_v5.settlement_row(conn, MONTH, scene["adv"])
    assert row["net_commission_cents"] != 100


# ---------------------------------------------------------------------------
# 四、导出
# ---------------------------------------------------------------------------


def test_export_xlsx_and_csv_alias(admin_client, scene):
    admin_client.post("/settlement/compute", data={"month": MONTH})
    resp = admin_client.get(f"/settlement.xlsx?month={MONTH}")
    assert resp.status_code == 200
    assert "spreadsheet" in resp.headers["Content-Type"]
    assert resp.data[:2] == b"PK"

    resp = admin_client.get(f"/settlement.csv?month={MONTH}")
    assert resp.status_code == 302
    assert "/settlement.xlsx" in resp.headers["Location"]


def test_export_rejects_legacy_month(admin_client, scene):
    resp = admin_client.get("/settlement.xlsx?month=2026-10")
    assert resp.status_code == 400
