"""迁移 18（V5 科目体系）+ 第三阶段填报分流 / 加盟店挂靠测试。

覆盖四件事：
1. 迁移 18 把 26 个 V5 科目灌进 `metrics`，统一 `active=0`，可重复执行；
2. `broadcast.sections_for_store()` 按模式 + 月份分流，2026-10 及以前恒为旧口径；
3. 后台把加盟店挂靠到主力店：对齐运营商范围、写 `store_baseline`、非法挂靠一律拒绝；
4. 店端填报：V5 店只写本店科目、非主控加盟店被挡在门外且不落任何 fact，
   10 月的填报与历史口径逐字节不变。

**不测 V5 计算引擎**：本轮不实现，只验证结构与配置。
"""

from __future__ import annotations

from datetime import date

import pytest

from app import broadcast, db, db_query, db_schema, metrics_seed, v5_baseline

V5_CODES = set(metrics_seed.V5_METRIC_CODES)
MOBILE_CODES = {code for code, _n, _s, _sort in metrics_seed.v5_all_metrics() if _s == "v5_mobile"}
TELECOM_CODES = {code for code, _n, _s, _sort in metrics_seed.v5_all_metrics() if _s == "v5_telecom"}
TAIZHOU_CODES = {code for code, _n, _s, _sort in metrics_seed.v5_all_metrics() if _s == "v5_taizhou"}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _store_id(code: str) -> int:
    with db.get_db() as conn:
        row = conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()
        assert row is not None, code
        return int(row["id"])


def _admin_id() -> int:
    with db.get_db() as conn:
        return int(conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"])


def _section_codes(sections) -> list[str]:
    return [code for section in sections for code, _name, _hint in section["metrics"]]


def _set_mode(store_id: int, mode: str, parent: int | None = None, mobile_code: str = "20000001"):
    """走真实入口改 V5 配置（不直接 UPDATE，顺带覆盖 store_baseline 同步）。"""
    with db.get_db() as conn:
        db.update_store_profile(
            conn,
            store_id,
            mobile_code=mobile_code,
            area_manager="示例经理",
            store_manager="示例店长",
            advisor_name="",
            v5_mode=mode,
            affiliated_to=parent,
        )


def _fact_codes(store_id: int, biz_date: date) -> set[str]:
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT metric_code FROM daily_facts WHERE store_id=? AND biz_date=?",
            (store_id, biz_date.isoformat()),
        ).fetchall()
        return {str(row["metric_code"]) for row in rows}


def _report_count(store_id: int, biz_date: date) -> int:
    with db.get_db() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM daily_reports WHERE store_id=? AND biz_date=?",
                (store_id, biz_date.isoformat()),
            ).fetchone()[0]
        )


# ---------------------------------------------------------------------------
# 迁移 18：V5 科目落库
# ---------------------------------------------------------------------------


def test_migration_18_seeds_v5_metrics_inactive(tmp_db):
    with db.get_db() as conn:
        rows = conn.execute("SELECT code, name, section, active FROM metrics").fetchall()
    by_code = {str(row["code"]): row for row in rows}

    assert len(V5_CODES) == 26
    for code in V5_CODES:
        assert code in by_code, code
        assert by_code[code]["active"] == 0, f"{code} 必须与旧科目共存但不进旧表单"
    assert {code for code in by_code if code.startswith("v5_")} == V5_CODES

    # 分组按运营商归位，排序从 1000 起，不与旧科目混排
    assert MOBILE_CODES == {f"v5_m{i}" for i in range(1, 11)}
    assert TELECOM_CODES == {f"v5_t{i}" for i in range(1, 9)}
    assert TAIZHOU_CODES == {f"v5_tz{i}" for i in range(1, 9)}
    for code in MOBILE_CODES:
        assert by_code[code]["section"] == metrics_seed.V5_MOBILE_SECTION_CODE
    for code in TELECOM_CODES:
        assert by_code[code]["section"] == metrics_seed.V5_TELECOM_SECTION_CODE
    for code in TAIZHOU_CODES:
        assert by_code[code]["section"] == metrics_seed.V5_TAIZHOU_SECTION_CODE
    assert by_code["v5_m7"]["name"] == "顺差让利购机直降"
    assert by_code["v5_m8"]["name"] == "移动优选会员"

    # 通用底座仍是旧字典里的 phone_sales（V5 分组只是引用它）
    assert by_code["phone_sales"]["active"] == 1
    assert "phone_sales" not in V5_CODES


def test_migration_18_is_idempotent_and_keeps_manual_active(tmp_db):
    with db.get_db() as conn:
        conn.execute("UPDATE metrics SET active=1 WHERE code='v5_m1'")
        conn.commit()
    before = None
    with db.get_db() as conn:
        before = int(conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0])

    db_schema.migrate()
    db_schema.migrate()

    with db.get_db() as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]) == before
        assert (
            int(conn.execute("SELECT COUNT(*) FROM metrics WHERE code LIKE 'v5\\_%' ESCAPE '\\'").fetchone()[0])
            == len(V5_CODES)
        )
        # 重跑只刷 name/section/sort，不动 active
        assert conn.execute("SELECT active FROM metrics WHERE code='v5_m1'").fetchone()[0] == 1
        versions = [
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
    assert versions.count(18) == 1


def test_legacy_metric_picker_does_not_leak_v5_codes(tmp_db):
    """旧表单 / 目标表 / 通报表下拉都按 active=1 取数：V5 科目一个都不能露头。"""
    with db.get_db() as conn:
        legacy = {str(row["code"]) for row in db_query.list_metrics(conn)}
        targets = set(db.metric_target_map(conn)) if hasattr(db, "metric_target_map") else set()
    assert legacy & V5_CODES == set()
    assert "phone_sales" in legacy
    assert not (targets & V5_CODES)


# ---------------------------------------------------------------------------
# 科目分流：sections_for_store()
# ---------------------------------------------------------------------------


def test_sections_for_store_switches_from_november(tmp_db):
    sid = _store_id("store-alpha")
    _set_mode(sid, v5_baseline.MODE_NO_ADVISOR_MAIN)
    with db.get_db() as conn:
        store = db.get_store(conn, sid)

    assert broadcast.sections_for_store(store, date(2026, 10, 31)) is broadcast.SECTIONS
    mobile = broadcast.sections_for_store(store, date(2026, 11, 1))
    assert _section_codes(mobile) == ["phone_sales", *MOBILE_CODES_order()]

    _set_mode(sid, v5_baseline.MODE_TELECOM_SINGLE)
    with db.get_db() as conn:
        store = db.get_store(conn, sid)
    assert _section_codes(broadcast.sections_for_store(store, date(2026, 11, 5))) == [
        "phone_sales",
        *[f"v5_t{i}" for i in range(1, 9)],
    ]


def MOBILE_CODES_order() -> list[str]:  # noqa: N802 — 只是让上面的断言读起来像科目序列
    return [f"v5_m{i}" for i in range(1, 11)]


def test_franchise_store_has_no_sections_and_is_blocked(tmp_db):
    sid = _store_id("store-zeta")
    _set_mode(sid, v5_baseline.MODE_FRANCHISE_DISABLED)
    with db.get_db() as conn:
        store = db.get_store(conn, sid)

    assert broadcast.sections_for_store(store, date(2026, 11, 5)) == []
    assert broadcast.filing_blocked(store, date(2026, 11, 5)) == broadcast.NO_FILING_MESSAGE
    # 10 月及以前照旧：能填、按旧科目
    assert broadcast.sections_for_store(store, date(2026, 10, 20)) is broadcast.SECTIONS
    assert broadcast.filing_blocked(store, date(2026, 10, 20)) == ""


def test_v5_broadcast_uses_v5_sections(tmp_db):
    values = {"phone_sales": (4, 90), "v5_m1": (2, 15), "v5_m7": (1, 3)}
    text = broadcast.render_broadcast(
        "示例甲店", date(2026, 11, 5), values, sections=broadcast.V5_MOBILE_SECTIONS
    )
    assert "当天手机销量" in text
    assert "5G融合新入网（主卡）：日2，累15" in text
    assert "顺差让利购机直降：日1，累3" in text
    assert "南通移动（V5）" in text
    # 旧口径的分组标题与旧科目都不许混进来
    assert "重点业务" not in text
    assert "低销迎回" not in text


# ---------------------------------------------------------------------------
# 模块 1：加盟店挂靠配置
# ---------------------------------------------------------------------------


def test_affiliate_config_aligns_scope_and_writes_baseline(tmp_db):
    parent = _store_id("store-beta")
    child = _store_id("store-gamma")
    _set_mode(parent, v5_baseline.MODE_NO_ADVISOR_MAIN)
    _set_mode(child, v5_baseline.MODE_AFFILIATED, parent=parent)

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT v5_mode, carrier_scope, affiliated_to FROM stores WHERE id=?", (child,)
        ).fetchone()
        assert row["v5_mode"] == v5_baseline.MODE_AFFILIATED
        assert row["carrier_scope"] == "MOBILE"  # 跟随母店
        assert int(row["affiliated_to"]) == parent
        baseline = conn.execute(
            "SELECT v5_mode, affiliated_to, from_month FROM store_baseline WHERE store_id=?",
            (child,),
        ).fetchall()
    assert len(baseline) == 1, "只写 V5 起始月一行，不回溯历史"
    assert baseline[0]["from_month"] == v5_baseline.V5_START_MONTH
    assert baseline[0]["v5_mode"] == v5_baseline.MODE_AFFILIATED
    assert int(baseline[0]["affiliated_to"]) == parent

    # 切回独立主力店：清母店指针，基线行跟着改口径
    _set_mode(child, v5_baseline.MODE_NO_ADVISOR_MAIN)
    with db.get_db() as conn:
        row = conn.execute("SELECT affiliated_to FROM stores WHERE id=?", (child,)).fetchone()
        assert row["affiliated_to"] is None
        rows = conn.execute(
            "SELECT v5_mode FROM store_baseline WHERE store_id=?", (child,)
        ).fetchall()
    assert [r["v5_mode"] for r in rows] == [v5_baseline.MODE_NO_ADVISOR_MAIN]

    # 切成非主控加盟店：基线行删掉（不进提成与考核）
    _set_mode(child, v5_baseline.MODE_FRANCHISE_DISABLED)
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM store_baseline WHERE store_id=?", (child,)
        ).fetchone()[0] == 0
        mode = conn.execute("SELECT v5_mode FROM stores WHERE id=?", (child,)).fetchone()[0]
    assert mode == v5_baseline.MODE_FRANCHISE_DISABLED


def test_affiliate_config_rejects_bad_parent(tmp_db):
    parent = _store_id("store-beta")
    child = _store_id("store-gamma")
    franchise = _store_id("store-zeta")

    with pytest.raises(ValueError, match="必须选一家母店"):
        _set_mode(child, v5_baseline.MODE_AFFILIATED, parent=None)
    with pytest.raises(ValueError, match="不能把店挂到自己名下"):
        _set_mode(child, v5_baseline.MODE_AFFILIATED, parent=child)
    with pytest.raises(ValueError, match="母店不存在"):
        _set_mode(child, v5_baseline.MODE_AFFILIATED, parent=999999)

    # 加盟店 / 挂靠店都不能当母店
    _set_mode(franchise, v5_baseline.MODE_FRANCHISE_DISABLED)
    with pytest.raises(ValueError, match="独立主力店"):
        _set_mode(child, v5_baseline.MODE_AFFILIATED, parent=franchise)
    _set_mode(parent, v5_baseline.MODE_NO_ADVISOR_MAIN)
    _set_mode(franchise, v5_baseline.MODE_AFFILIATED, parent=parent)
    with pytest.raises(ValueError, match="独立主力店"):
        _set_mode(child, v5_baseline.MODE_AFFILIATED, parent=franchise)


def test_affiliate_parent_options_exclude_non_main_stores(tmp_db):
    main = _store_id("store-beta")
    franchise = _store_id("store-zeta")
    _set_mode(main, v5_baseline.MODE_ADVISOR_A)
    _set_mode(franchise, v5_baseline.MODE_FRANCHISE_DISABLED)

    with db.get_db() as conn:
        options = {int(row["id"]) for row in db.affiliate_parent_options(conn)}
        empty = db.affiliate_parent_options(conn)
        summary = db.store_mode_summary(conn)
    assert main in options
    assert franchise not in options
    assert empty  # 示例店里还有别的在跑的主控店
    assert summary["main_total"] == summary["mobile"] + summary["telecom"] + summary["taizhou"]
    assert "当前主控活跃" in summary["text"]


def test_store_mode_summary_pure_function():
    rows = [
        {"v5_mode": v5_baseline.MODE_ADVISOR_A, "active": 1},
        {"v5_mode": v5_baseline.MODE_NO_ADVISOR_MAIN, "active": 1},
        {"v5_mode": v5_baseline.MODE_TELECOM_SINGLE, "active": 1},
        {"v5_mode": v5_baseline.MODE_TAIZHOU_ADVANCE_ONLY, "active": 1},
        {"v5_mode": v5_baseline.MODE_AFFILIATED, "active": 1},
        {"v5_mode": v5_baseline.MODE_FRANCHISE_DISABLED, "active": 1},
        {"v5_mode": v5_baseline.MODE_UNSET, "active": 1},
        {"v5_mode": v5_baseline.MODE_ADVISOR_A, "active": 0},
    ]
    summary = v5_baseline.store_mode_summary(rows)
    assert (summary["mobile"], summary["telecom"], summary["taizhou"]) == (2, 1, 1)
    assert summary["main_total"] == 4
    assert summary["affiliated"] == 1
    assert summary["disabled"] == 3  # 加盟 + 未纳入 + 停用
    assert summary["text"] == (
        "当前主控活跃 4 家（移动 2 / 泰州 1 / 电信 1） ｜ 挂靠 1 家 ｜ 加盟/停用 3 家"
    )


# ---------------------------------------------------------------------------
# 模块 3：店端填报分流 + 拦截
# ---------------------------------------------------------------------------


def test_franchise_store_is_blocked_from_filing(admin_client):
    sid = _store_id("store-zeta")
    biz = date(2026, 11, 5)
    _set_mode(sid, v5_baseline.MODE_FRANCHISE_DISABLED)

    page = admin_client.get(f"/today?store_id={sid}&date={biz.isoformat()}").get_data(as_text=True)
    assert broadcast.NO_FILING_MESSAGE in page
    assert 'id="dailyForm"' not in page

    admin_client.post(
        "/today",
        data={"store_id": str(sid), "date": biz.isoformat(), "m_v5_m1": "9"},
        follow_redirects=True,
    )
    assert _report_count(sid, biz) == 0
    assert _fact_codes(sid, biz) == set()


def test_v5_store_filing_writes_only_its_own_codes(admin_client):
    sid = _store_id("store-alpha")
    biz = date(2026, 11, 5)
    _set_mode(sid, v5_baseline.MODE_NO_ADVISOR_MAIN)

    resp = admin_client.post(
        "/today",
        data={
            "store_id": str(sid),
            "date": biz.isoformat(),
            "m_phone_sales": "5",
            "m_v5_m1": "3",
            "m_v5_m7": "2",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with db.get_db() as conn:
        values = db.day_values(conn, sid, biz)
    assert values.get("v5_m1") == 3
    assert values.get("v5_m7") == 2
    assert values.get("phone_sales") == 5
    codes = _fact_codes(sid, biz)
    assert codes <= V5_CODES | {"phone_sales"}
    assert not (codes & {code for code in codes if code.startswith("v5_")} - V5_CODES)


def test_october_filing_keeps_legacy_codes(admin_client):
    sid = _store_id("store-alpha")
    biz = date(2026, 10, 20)
    _set_mode(sid, v5_baseline.MODE_NO_ADVISOR_MAIN)
    with db.get_db() as conn:
        store = db.get_store(conn, sid)
        legacy_codes = [str(row["code"]) for row in db_query.list_metrics(conn)]
    assert broadcast.sections_for_store(store, biz) is broadcast.SECTIONS
    assert broadcast.filing_blocked(store, biz) == ""

    picked = legacy_codes[0]
    resp = admin_client.post(
        "/today",
        data={"store_id": str(sid), "date": biz.isoformat(), f"m_{picked}": "1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    codes = _fact_codes(sid, biz)
    assert picked in codes
    assert not (codes & V5_CODES), "10 月不许写 V5 科目"


def test_set_day_value_accepts_v5_metric(tmp_db):
    sid = _store_id("store-alpha")
    biz = date(2026, 11, 5)
    _set_mode(sid, v5_baseline.MODE_NO_ADVISOR_MAIN)
    user_id = _admin_id()
    with db.get_db() as conn:
        db.set_day_value(
            conn, store_id=sid, biz_date=biz, metric_code="v5_m3", value=4, user_id=user_id
        )
        assert db.day_values(conn, sid, biz).get("v5_m3") == 4
    with pytest.raises(ValueError):
        with db.get_db() as conn:
            db.set_day_value(
                conn,
                store_id=sid,
                biz_date=biz,
                metric_code="not_a_metric",
                value=1,
                user_id=user_id,
            )


# ---------------------------------------------------------------------------
# 设置页：模式 / 母店可编辑，基线仍只读
# ---------------------------------------------------------------------------


def test_settings_edit_store_saves_affiliation(admin_client):
    parent = _store_id("store-beta")
    child = _store_id("store-gamma")
    _set_mode(parent, v5_baseline.MODE_NO_ADVISOR_MAIN)

    resp = admin_client.post(
        "/settings",
        data={
            "action": "edit_store",
            "tab": "stores",
            "store_id": str(child),
            "mobile_code": "20000002",
            "area_manager": "示例经理",
            "store_manager": "示例店长",
            "advisor_name": "",
            "region_group": "示例区",
            "city": "示例市",
            "store_grade": "A",
            "ai_target": "10",
            "invoice_name": "",
            "lease_area": "",
            "lease_address": "",
            "lease_period": "",
            "v5_mode": v5_baseline.MODE_AFFILIATED,
            "affiliated_to": str(parent),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT v5_mode, affiliated_to, carrier_scope FROM stores WHERE id=?", (child,)
        ).fetchone()
    assert row["v5_mode"] == v5_baseline.MODE_AFFILIATED
    assert int(row["affiliated_to"]) == parent
    assert row["carrier_scope"] == "MOBILE"


def test_settings_edit_store_flashes_on_bad_affiliation(admin_client):
    child = _store_id("store-gamma")
    resp = admin_client.post(
        "/settings",
        data={
            "action": "edit_store",
            "tab": "stores",
            "store_id": str(child),
            "mobile_code": "20000002",
            "area_manager": "示例经理",
            "store_manager": "示例店长",
            "advisor_name": "",
            "region_group": "示例区",
            "city": "示例市",
            "store_grade": "A",
            "ai_target": "10",
            "invoice_name": "",
            "lease_area": "",
            "lease_address": "",
            "lease_period": "",
            "v5_mode": v5_baseline.MODE_AFFILIATED,
            "affiliated_to": "",
        },
        follow_redirects=True,
    )
    assert "必须选一家母店" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        mode = conn.execute("SELECT v5_mode FROM stores WHERE id=?", (child,)).fetchone()[0]
    assert mode != v5_baseline.MODE_AFFILIATED
