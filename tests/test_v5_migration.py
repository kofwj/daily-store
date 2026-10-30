"""迁移 17（V5 落库）与设置页联调测试。

口径来源：docs/V5落库与store-daily改造对接清单.md。

本文件只验证四件事：
1. 迁移 17 建出表 1–6 + `stores` 四列，且可重复执行（幂等）；
2. 34 家门店的 V5 模式 / 考核基线 / 顾问任期按 `code` 灌入，数值与门店表一致；
3. 2026-10 及以前的存量报表「一个字节都不动」，V5 表里不允许出现历史月份行（双轨）；
4. 设置页门店档案只读展示 V5 口径；POST 只在表单显式提交 `v5_mode` 时才改模式与挂靠。

**不测 V5 计算引擎**：本轮不实现，`v5_month_enabled()` 只是按月份隔离的开关。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app import db, db_core, db_schema, v5_baseline

FIXTURE = Path(__file__).parent / "fixtures" / "stores_34.json"
FIXTURE_STORES = json.loads(FIXTURE.read_text(encoding="utf-8"))
FIXTURE_BY_CODE = {row["code"]: row for row in FIXTURE_STORES}

V5_EXPECT_COLUMNS = {
    "store_baseline": {
        "id",
        "store_id",
        "from_month",
        "to_month",
        "v5_mode",
        "affiliated_to",
        "net_baseline_cents",
        "target_carry_rate",
        "carry_tier_25",
        "carry_tier_18",
        "carry_tier_10",
        "cap_ratio",
        "cap_abs_cents",
        "created_at",
    },
    "advisor_tenure": {
        "id",
        "store_id",
        "advisor_name",
        "from_month",
        "to_month",
        "tier_grade",
        "base_pay_cents",
        "base_coef",
        "breakeven_cents",
        "excess_rate",
        "work_days",
        "note",
    },
    "store_month_biz": {
        "month",
        "store_id",
        "source_store_id",
        "role_share",
        "biz_code",
        "daily_sum_count",
        "settled_count",
        "unit_price_cents",
        "raw_amount_cents",
        # D12：笔数调整要留审计痕迹
        "updated_at",
        "updated_by",
    },
    "store_month_settlement": {
        "month",
        "store_id",
        "gross_commission_cents",
        "total_deduction_cents",
        "net_commission_cents",
        "rebate_clawback_cents",
        "rebate_recovery_exempt",
        "v5_mode_snap",
        "net_baseline_snap_cents",
        "shop_final_pool_cents",
        "total_score_100",
        "status",
        "locked_at",
    },
    "store_month_split": {
        "id",
        "month",
        "store_id",
        "emp_code",
        "emp_name",
        "role_tag",
        "amount_cents",
        "confirmed_by",
    },
    "settlement_audit_log": {
        "id",
        "month",
        "store_id",
        "action",
        "user_id",
        "created_at",
        "before_json",
        "after_json",
        "reason",
    },
}

V5_INDEXES = (
    "idx_store_baseline_store",
    "idx_advisor_tenure_store",
    "idx_store_month_biz_month",
    "idx_store_month_split_month",
    "idx_settlement_audit_month",
)


def _boot(tmp_path, monkeypatch, *, sample_env: bool) -> Path:
    """建一块新库：schema 走 init_db，门店一律来自 34 家夹具（不碰本地私有种子）。"""
    path = tmp_path / "v5.db"
    monkeypatch.setenv("STORE_DAILY_DB", str(path))
    monkeypatch.setenv("STORE_DAILY_DATA", str(tmp_path))
    monkeypatch.setenv("STORE_DAILY_TESTING", "1")
    monkeypatch.setenv("STORE_DAILY_SUPPRESS_INSECURE_WARNING", "1")
    monkeypatch.setenv("STORE_DAILY_SECRET", "test-" + "a" * 48)
    if sample_env:
        monkeypatch.setenv("STORE_DAILY_SAMPLE_SEED", "1")
    else:
        # 非示例环境：示例店要停用、账号要下线，这是 A2 条款里唯一允许动 active 的地方
        monkeypatch.delenv("STORE_DAILY_SAMPLE_SEED", raising=False)
    monkeypatch.setattr(db_core, "DB_PATH", path)
    monkeypatch.setattr(db_core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "is_locked", lambda *a, **k: False)
    # 目录固定走夹具：既不 import stores_seed_local（CI 没有这个文件），也不灌示例店
    monkeypatch.setattr(db_schema, "_catalog", lambda: ([], lambda: [], ""))
    db.init_db()
    with db.get_db() as conn:
        conn.execute("UPDATE users SET must_change_pin=0")
    return path


def _load_fixture(conn) -> dict[str, int]:
    """把 34 家门店 + 6 个示例店账号灌进去，返回 code -> id。"""
    created = db_core._now()
    for row in FIXTURE_STORES:
        conn.execute(
            """
            INSERT INTO stores(
                id, name, code, short_name, sort_order, region_group, city, mobile_code,
                area_manager, store_manager, follow_ai, follow_bisuan, has_advisor,
                advisor_name, store_grade, ai_target, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["name"],
                row["code"],
                row.get("short_name") or "",
                row["sort_order"],
                row["region_group"],
                row["city"],
                row["mobile_code"],
                row["area_manager"],
                row["store_manager"],
                row["follow_ai"],
                row["follow_bisuan"],
                row["has_advisor"],
                row["advisor_name"],
                row["store_grade"],
                row["ai_target"],
                row["active"],
                created,
            ),
        )
    ids = {row["code"]: int(row["id"]) for row in conn.execute("SELECT id, code FROM stores")}
    for code, login in v5_baseline.SAMPLE_STORES:
        db.create_user(
            conn,
            username=login,
            display_name=f"示例{login}",
            pin="123456",
            role="filler",
            store_ids=[ids[code]],
        )
    return ids


def _seed_legacy_month(conn, ids: dict[str, int]) -> None:
    """造 2026-10 及更早的存量报表：迁移 17 之后必须原样还在。"""
    admin_id = int(conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"])
    for code, day in (
        ("rg-ninghai", date(2026, 10, 15)),
        ("hangang" if False else "gangzha-wanda", date(2026, 10, 16)),
        ("gangzha-wanda", date(2026, 10, 16)),
    ):
        db.save_daily(
            conn,
            store_id=ids[code],
            biz_date=day,
            values={"phone_sales": 3},
            user_id=admin_id,
            note="历史月份原口径",
        )


def _history_snapshot(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            """
            SELECT biz_date, store_id, metric_code, day_value
              FROM daily_facts
             WHERE biz_date < '2026-11'
             ORDER BY biz_date, store_id, metric_code
            """
        )
    ]


def _prepare(tmp_path, monkeypatch, *, sample_env: bool = False):
    """init_db（此时门店为空，17 空跑）→ 灌夹具 + 历史月报表 → 真跑一次 migrate()。"""
    _boot(tmp_path, monkeypatch, sample_env=sample_env)
    with db.get_db() as conn:
        ids = _load_fixture(conn)
        _seed_legacy_month(conn, ids)
        history = _history_snapshot(conn)
        # 去掉 init_db 时那次空跑留下的 17 记录，让下面的 migrate() 真正执行迁移 17
        conn.execute("DELETE FROM schema_migrations WHERE version=17")
    db.migrate()
    return ids, history


@pytest.fixture()
def v5_db(tmp_path, monkeypatch):
    ids, history = _prepare(tmp_path, monkeypatch)
    with db.get_db() as conn:
        yield conn, ids, history


def test_migration_17_creates_tables_and_columns(v5_db):
    conn, _ids, _history = v5_db
    rows = {
        row["name"]: row
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, columns in V5_EXPECT_COLUMNS.items():
        assert table in rows, f"迁移 17 没建出 {table}"
        actual = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        missing = columns - actual
        assert not missing, f"{table} 缺列 {sorted(missing)}"

    indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for name in V5_INDEXES:
        assert name in indexes, f"缺索引 {name}"

    store_columns = {r["name"] for r in conn.execute("PRAGMA table_info(stores)")}
    assert {"v5_mode", "carrier_scope", "mobile_code_active", "affiliated_to"} <= store_columns

    applied = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert applied == list(range(1, 20)), "迁移 1–19 应各记一次"
    assert applied.count(17) == 1
    assert applied.count(18) == 1
    assert applied.count(19) == 1
    # 迁移 19：审计表 user_id 允许空（批量试算/锁月没有操作人）
    audit_user = {r["name"]: r for r in conn.execute("PRAGMA table_info(settlement_audit_log)")}["user_id"]
    assert int(audit_user["notnull"]) == 0


def test_migration_17_is_idempotent(v5_db):
    conn, _ids, _history = v5_db
    before = (
        conn.execute("SELECT COUNT(*) FROM store_baseline").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM advisor_tenure").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
    )
    # 再跑一遍整条迁移链（17 已记录，应当是纯 no-op）
    db.migrate()
    # 再直接重放迁移 17 本体，验证 DDL/INSERT OR IGNORE 都能重复执行
    db_schema._v5_schema_and_baseline(conn)
    db_schema._v5_schema_and_baseline(conn)
    after = (
        conn.execute("SELECT COUNT(*) FROM store_baseline").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM advisor_tenure").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
    )
    assert after == before
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version=17"
    ).fetchone()[0] == 1


def test_baseline_and_tenure_values(v5_db):
    conn, ids, _history = v5_db
    # 23 家纳入考核的门店各一行基线（加盟 5 + 示例 6 不配基线）
    expected = [cfg for cfg in v5_baseline.STORE_V5_CONFIG if cfg["v5_mode"] not in v5_baseline.DISABLED_MODES]
    assert len(expected) == 23
    rows = {
        row["store_id"]: row
        for row in conn.execute("SELECT * FROM store_baseline")
    }
    assert set(rows) == {ids[cfg["code"]] for cfg in expected}
    for row in rows.values():
        assert row["from_month"] == v5_baseline.V5_START_MONTH
        assert row["to_month"] == ""
        assert row["cap_ratio"] == pytest.approx(v5_baseline.DEFAULT_CAP_RATIO)

    # 专职顾问店：基线、封顶、档位取自门店表
    ninghai = rows[ids["rg-ninghai"]]
    assert ninghai["v5_mode"] == v5_baseline.MODE_ADVISOR_A
    assert ninghai["net_baseline_cents"] == 896700
    assert ninghai["cap_abs_cents"] == 65000
    assert ninghai["carry_tier_25"] == pytest.approx(0.20)
    assert ninghai["carry_tier_18"] == pytest.approx(0.15)
    assert ninghai["carry_tier_10"] == pytest.approx(0.10)

    # 电信单店：搭载率口径 12%/9%/6%
    yuanrong = rows[ids["yuanrong"]]
    assert yuanrong["v5_mode"] == v5_baseline.MODE_TELECOM_SINGLE
    assert (yuanrong["carry_tier_25"], yuanrong["carry_tier_18"], yuanrong["carry_tier_10"]) == (
        pytest.approx(0.12),
        pytest.approx(0.09),
        pytest.approx(0.06),
    )

    # 挂靠小店：基线随母店，affiliated_to 指到母店
    changqiao = rows[ids["chongchuan-changqiao"]]
    assert changqiao["v5_mode"] == v5_baseline.MODE_AFFILIATED
    assert changqiao["affiliated_to"] == ids["gangzha-wanda"]

    # 顾问任期：4 段，10 月起到任（11 月起才进 V5 计算）
    tenures = list(conn.execute("SELECT * FROM advisor_tenure ORDER BY store_id"))
    assert len(tenures) == 4
    by_store = {row["store_id"]: row for row in tenures}
    li = by_store[ids["rg-ninghai"]]
    assert (li["advisor_name"], li["tier_grade"]) == ("李海燕", "A")
    assert li["from_month"] == "2026-10"
    assert li["to_month"] == ""
    assert li["base_coef"] == pytest.approx(1.20)
    assert li["breakeven_cents"] == 748700
    assert li["excess_rate"] == pytest.approx(0.40)
    b_near = by_store[ids["qidong-renmin"]]
    assert (b_near["advisor_name"], b_near["tier_grade"]) == ("赵凯博", "B_NEAR")
    assert b_near["base_coef"] == pytest.approx(1.00)
    assert b_near["excess_rate"] == pytest.approx(0.70)

    # 顾问任期表是唯一事实源：stores 上的两列只是当前在任快照
    stores = {
        row["code"]: row for row in conn.execute("SELECT code, advisor_name, has_advisor FROM stores")
    }
    assert stores["rg-ninghai"]["advisor_name"] == "李海燕"
    assert stores["rg-ninghai"]["has_advisor"] == 1
    assert stores["gangzha-wanda"]["advisor_name"] == ""


def test_store_v5_mode_columns_follow_config(v5_db):
    conn, ids, _history = v5_db
    stores = {row["code"]: row for row in conn.execute("SELECT * FROM stores")}
    assert len(stores) == 34
    for cfg in v5_baseline.STORE_V5_CONFIG:
        row = stores[cfg["code"]]
        assert row["v5_mode"] == cfg["v5_mode"], cfg["code"]
        assert row["carrier_scope"] == cfg["carrier_scope"], cfg["code"]
        assert int(row["mobile_code_active"]) == int(cfg["mobile_code_active"]), cfg["code"]
        expected_parent = ids[cfg["affiliated_to"]] if cfg.get("affiliated_to") else None
        assert row["affiliated_to"] == expected_parent, cfg["code"]

    # 加盟店只记模式，不翻转 active（A2 只授权停用示例店，历史可见性要保住）
    for code in ("nt-dayoujing", "haian-xirun", "rugao-wanda", "rugao-wuyue", "chongchuan-yinhe"):
        assert stores[code]["v5_mode"] == v5_baseline.MODE_FRANCHISE_DISABLED
        assert int(stores[code]["active"]) == FIXTURE_BY_CODE[code]["active"]


def test_v5_tables_hold_no_legacy_month_rows(v5_db):
    conn, _ids, history = v5_db
    # 表 3–6 本轮只建空表，V5 引擎没跑过就不该有任何行
    for table in ("store_month_biz", "store_month_settlement", "store_month_split", "settlement_audit_log"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # 表 1 的生效月必须晚于历史口径
    assert conn.execute(
        "SELECT COUNT(*) FROM store_baseline WHERE from_month <= ?",
        (v5_baseline.V5_LEGACY_LAST_MONTH,),
    ).fetchone()[0] == 0
    # 2026-10 及以前的报表一条不少、一个值不改
    assert history, "夹具里应该有历史月份数据，否则这条断言没意义"
    assert _history_snapshot(conn) == history


def test_sample_stores_disabled_outside_sample_env(v5_db):
    conn, ids, _history = v5_db
    for code, login in v5_baseline.SAMPLE_STORES:
        assert int(conn.execute("SELECT active FROM stores WHERE code=?", (code,)).fetchone()[0]) == 0
        user = conn.execute("SELECT active FROM users WHERE username=?", (login,)).fetchone()
        assert user is not None and int(user["active"]) == 0, f"{login} 账号应下线"
        assert conn.execute("SELECT v5_mode FROM stores WHERE code=?", (code,)).fetchone()[0] == (
            v5_baseline.MODE_SAMPLE_DISABLED
        )
    # 34 家 − 6 家示例店 = 28 家仍在用
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == 28
    assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=0").fetchone()[0] == 6
    # 示例店不配基线
    sample_ids = [ids[code] for code, _login in v5_baseline.SAMPLE_STORES]
    assert conn.execute(
        f"SELECT COUNT(*) FROM store_baseline WHERE store_id IN ({','.join('?' * len(sample_ids))})",
        sample_ids,
    ).fetchone()[0] == 0


def test_sample_env_keeps_sample_stores_alive(tmp_path, monkeypatch):
    """STORE_DAILY_SAMPLE_SEED=1：示例店和 alpha 账号必须原样活着，否则几十个旧单测全失联。"""
    ids, _history = _prepare(tmp_path, monkeypatch, sample_env=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0] == 34
        for code, login in v5_baseline.SAMPLE_STORES:
            assert int(conn.execute("SELECT active FROM stores WHERE code=?", (code,)).fetchone()[0]) == 1
            assert int(
                conn.execute("SELECT active FROM users WHERE username=?", (login,)).fetchone()["active"]
            ) == 1
        # DDL 与基线灌入在示例环境照常执行，只是不动示例店的 active
        assert conn.execute("SELECT COUNT(*) FROM store_baseline").fetchone()[0] == 23
        assert conn.execute("SELECT COUNT(*) FROM advisor_tenure").fetchone()[0] == 4
        assert ids["store-alpha"]


def test_v5_month_enabled_boundaries():
    assert v5_baseline.v5_month_enabled("2026-10") is False
    assert v5_baseline.v5_month_enabled("2026-09") is False
    assert v5_baseline.v5_month_enabled("2025-12") is False
    assert v5_baseline.v5_month_enabled("2026-11") is True
    assert v5_baseline.v5_month_enabled("2027-01") is True
    # 带日期的输入按月份截断
    assert v5_baseline.v5_month_enabled(date(2026, 10, 31)) is False
    assert v5_baseline.v5_month_enabled(date(2026, 11, 1)) is True
    assert v5_baseline.v5_month_enabled("2026-11-05") is True
    # 认不出的输入一律不开（宁可走原口径，也不许误触发重算）
    for blank in ("", None, "  ", "2026", "not-a-month", "2026/11"):
        assert v5_baseline.v5_month_enabled(blank) is False


def test_store_v5_snapshot_is_read_only_view(v5_db):
    conn, ids, _history = v5_db
    snapshot = db.store_v5_snapshot(conn, ids["rg-ninghai"])
    assert snapshot["mode"] == v5_baseline.MODE_ADVISOR_A
    assert snapshot["mode_label"] == v5_baseline.V5_MODE_LABELS[v5_baseline.MODE_ADVISOR_A]
    assert snapshot["carrier_label"] == "移动"
    assert snapshot["mobile_code_active"] is True
    assert snapshot["parent"] is None
    assert snapshot["start_month"] == v5_baseline.V5_START_MONTH
    assert snapshot["legacy_last_month"] == v5_baseline.V5_LEGACY_LAST_MONTH
    assert snapshot["baseline"]["from_month"] == "2026-11"
    assert snapshot["baseline"]["net_baseline"] == "8,967"
    assert snapshot["baseline"]["target_carry_rate"] == "20%"
    assert snapshot["baseline"]["cap_abs"] == "650"
    assert snapshot["tenures"][0]["advisor_name"] == "李海燕"
    assert snapshot["tenures"][0]["breakeven"] == "7,487"

    affiliated = db.store_v5_snapshot(conn, ids["chongchuan-changqiao"])
    assert affiliated["mode"] == v5_baseline.MODE_AFFILIATED
    assert affiliated["in_v5"] is True
    assert affiliated["parent"]["label"] == FIXTURE_BY_CODE["gangzha-wanda"]["name"]
    assert affiliated["tenures"] == []

    disabled = db.store_v5_snapshot(conn, ids["nt-dayoujing"])
    assert disabled["mode"] == v5_baseline.MODE_FRANCHISE_DISABLED
    assert disabled["disabled"] is True
    assert disabled["baseline"] is None

    # 老库还没补列时（模拟只读重查询）不能抛错
    assert db.store_v5_snapshot(conn, 999999) == {}


# ---------------------------------------------------------------------------
# 页面联调：设置 → 门店档案 的 V5 只读区块
# ---------------------------------------------------------------------------


def _store_id(code: str) -> int:
    with db.get_db() as conn:
        return int(conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"])


def test_settings_store_page_shows_v5_block(admin_client):
    sid = _store_id("store-alpha")
    body = admin_client.get(f"/settings?tab=stores&store_id={sid}").get_data(as_text=True)
    assert "V5 口径（只读）" in body
    assert v5_baseline.mode_label(v5_baseline.MODE_SAMPLE_DISABLED) in body
    assert v5_baseline.V5_START_MONTH in body
    assert v5_baseline.V5_LEGACY_LAST_MONTH in body
    # 模块 1 起「模式 + 挂靠母店」在编辑表单里可改；基线金额/档位仍然只读
    assert 'name="v5_mode"' in body
    assert 'name="affiliated_to"' in body
    assert 'name="net_baseline_cents"' not in body
    assert 'name="carry_tier_25"' not in body
    # C10 模式分布提示条
    assert "当前主控活跃" in body


def test_settings_store_page_renders_baseline_and_tenure(admin_client):
    """有基线 + 有任期的店：页面要真的把数值渲染出来，不只是空壳。"""
    with db.get_db() as conn:
        sid = _store_id("store-alpha")
        conn.execute(
            """
            UPDATE stores SET v5_mode=?, carrier_scope='MOBILE', mobile_code_active=1 WHERE id=?
            """,
            (v5_baseline.MODE_NO_ADVISOR_MAIN, sid),
        )
        conn.execute(
            """
            INSERT INTO store_baseline(
                store_id, from_month, to_month, v5_mode, affiliated_to,
                net_baseline_cents, target_carry_rate, carry_tier_25, carry_tier_18, carry_tier_10,
                cap_ratio, cap_abs_cents, created_at
            ) VALUES (?, '2026-11', '', ?, NULL, 423000, 0.20, 0.20, 0.15, 0.10, 0.35, 0, ?)
            """,
            (sid, v5_baseline.MODE_NO_ADVISOR_MAIN, db_core._now()),
        )
        conn.execute(
            """
            INSERT INTO advisor_tenure(
                store_id, advisor_name, from_month, to_month, tier_grade,
                base_pay_cents, base_coef, breakeven_cents, excess_rate, work_days, note
            ) VALUES (?, '李海燕', '2026-10', '', 'A', 400000, 1.20, 748700, 0.40, 0, '')
            """,
            (sid,),
        )
    body = admin_client.get(f"/settings?tab=stores&store_id={sid}").get_data(as_text=True)
    assert v5_baseline.mode_label(v5_baseline.MODE_NO_ADVISOR_MAIN) in body
    assert "4,230 元" in body  # 净酬金基线
    assert "目标搭载率" in body and "20%" in body
    assert "≥18%" in body  # 搭载率档位
    assert "35%" in body  # 提成封顶比例
    assert "李海燕 · A 档" in body
    assert "7,487 元" in body  # 顾问平衡点


def test_settings_store_page_marks_unset_mode(admin_client):
    """没纳入 V5 的新店：明确说清按原口径，不伪造基线。"""
    with db.get_db() as conn:
        sid = db.create_store(conn, "未纳入V5测试店", short_name="未纳入V5")
    body = admin_client.get(f"/settings?tab=stores&store_id={sid}").get_data(as_text=True)
    assert "尚未纳入 V5" in body
    assert "考核基线" not in body


def test_settings_edit_store_does_not_touch_v5_fields(admin_client):
    with db.get_db() as conn:
        sid = _store_id("store-alpha")
        before = conn.execute(
            "SELECT v5_mode, carrier_scope, mobile_code_active, affiliated_to FROM stores WHERE id=?",
            (sid,),
        ).fetchone()
        baseline_before = conn.execute("SELECT COUNT(*) FROM store_baseline").fetchone()[0]
        tenure_before = conn.execute("SELECT COUNT(*) FROM advisor_tenure").fetchone()[0]
    resp = admin_client.post(
        "/settings",
        data={
            "action": "edit_store",
            "tab": "stores",
            "store_id": str(sid),
            "mobile_code": "20000001",
            "area_manager": "示例经理",
            "store_manager": "示例店长",
            "advisor_name": "示例顾问",
            "region_group": "示例区",
            "city": "示例市",
            "store_grade": "B",
            "ai_target": "5",
            "invoice_name": "",
            "lease_area": "",
            "lease_address": "",
            "lease_period": "",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        after = conn.execute(
            "SELECT v5_mode, carrier_scope, mobile_code_active, affiliated_to FROM stores WHERE id=?",
            (sid,),
        ).fetchone()
        assert tuple(after) == tuple(before), "普通保存不许改 V5 口径字段"
        assert conn.execute("SELECT COUNT(*) FROM store_baseline").fetchone()[0] == baseline_before
        assert conn.execute("SELECT COUNT(*) FROM advisor_tenure").fetchone()[0] == tenure_before
        # 旧口径字段照旧可编辑（双轨：10 月及以前仍靠 stores.advisor_name）
        assert conn.execute("SELECT advisor_name FROM stores WHERE id=?", (sid,)).fetchone()[0] == (
            "示例顾问"
        )
