"""专职顾问任期维护（任务二 / A4 收口）测试。

两件事：
1. `db_stores` 里的任期层：新增 / 改写 / 删除、同店同期唯一、无行不动快照、
   删光清空快照、按参照月取「最近一段」反写 `stores.advisor_name`；
2. 设置页门店档案的任期维护：POST 保存/删除真的落库、派生式快照不许被普通保存覆盖、
   非法输入只 flash 不写库。

**不重测算薪**：任期参与表 3 的算法已在 `tests/test_v5_engine.py` 覆盖。
"""

from __future__ import annotations

from datetime import date

import pytest

from app import db, v5_baseline

# 参照月之外的固定日期：`advisor_snapshot_month()` 取 max(今天, 2026-11)，
# 所以测试里的任期一律用「远早于 2026-11 且不设结束月」的区间，避免依赖跑测试的机器日期。
LONG_AGO = "2020-01"
_STORE_SEQ = 0

# 每个用例都要一块全新的库：db 层用例直接调 `db.get_db()`，不经过 client 夹具，
# 不 autouse 就会写进真 dev 库（本轮踩过）。
pytestmark = pytest.mark.usefixtures("tmp_db")


def _mk_store(name: str = "任期测试店") -> int:
    """建一家干净门店；`stores.name` 有唯一约束，所以每次带个序号。"""
    global _STORE_SEQ
    _STORE_SEQ += 1
    label = f"{name}{_STORE_SEQ}"
    with db.get_db() as conn:
        return db.create_store(conn, label, short_name=label)


def _add_tenure(sid: int, **kwargs) -> int:
    data = {
        "advisor_name": "甲顾问",
        "from_month": LONG_AGO,
        "base_pay_cents": 400_000,
        "base_coef": 1.2,
        "breakeven_cents": 700_000,
        "excess_rate": 0.4,
    }
    data.update(kwargs)
    with db.get_db() as conn:
        return db.save_advisor_tenure(conn, sid, **data)


def _store_row(sid: int):
    with db.get_db() as conn:
        return conn.execute(
            "SELECT advisor_name, has_advisor FROM stores WHERE id=?", (sid,)
        ).fetchone()


def _tenures(sid: int):
    with db.get_db() as conn:
        return db.list_advisor_tenures(conn, sid)


# ---------------------------------------------------------------------------
# 档位口径
# ---------------------------------------------------------------------------


def test_tier_helpers_normalize_loose_spellings():
    assert v5_baseline.normalize_tier("A") == "A"
    assert v5_baseline.normalize_tier("a档") == "A"
    for raw in ("B", "b_near", "B 近档", "b-near", "BNEAR"):
        assert v5_baseline.normalize_tier(raw) == "B_NEAR", raw
    assert v5_baseline.normalize_tier("C") == ""
    assert v5_baseline.normalize_tier(None) == ""
    assert v5_baseline.tier_label("B_NEAR") == "B 近档"
    assert v5_baseline.tier_label("A") == "A 档"
    assert [code for code, _label in v5_baseline.tier_choices()] == list(
        v5_baseline.ADVISOR_TIER_GRADES
    )


def test_advisor_snapshot_month_never_before_v5_start():
    assert v5_baseline.V5_START_MONTH == "2026-11"
    assert db.advisor_snapshot_month(date(2026, 1, 15)) == "2026-11"
    assert db.advisor_snapshot_month(date(2026, 10, 31)) == "2026-11"
    assert db.advisor_snapshot_month(date(2027, 3, 2)) == "2027-03"


def test_tenure_range_label():
    assert db.tenure_range_label("2026-10", "2027-02") == "2026-10 至 2027-02"
    assert db.tenure_range_label("2026-11", "") == "2026-11 起"


# ---------------------------------------------------------------------------
# db 层：新增 / 改写 / 删除
# ---------------------------------------------------------------------------


def test_save_tenure_inserts_and_syncs_store_snapshot():
    sid = _mk_store()
    with db.get_db() as conn:
        conn.execute("UPDATE stores SET advisor_name='手填旧名', has_advisor=1 WHERE id=?", (sid,))
    tid = _add_tenure(sid, advisor_name="赵顾问", from_month=LONG_AGO, tier_grade="B 近档")
    assert tid > 0
    row = _tenures(sid)[0]
    assert row["advisor_name"] == "赵顾问"
    assert row["tier_grade"] == "B_NEAR", "档位必须走 normalize_tier 归一"
    assert row["base_pay_cents"] == 400_000
    assert row["breakeven_cents"] == 700_000
    snap = _store_row(sid)
    assert snap["advisor_name"] == "赵顾问"
    assert int(snap["has_advisor"]) == 1


def test_save_tenure_updates_existing_segment_in_place():
    sid = _mk_store()
    tid = _add_tenure(sid)
    with db.get_db() as conn:
        db.save_advisor_tenure(
            conn,
            sid,
            tenure_id=tid,
            advisor_name="乙顾问",
            from_month=LONG_AGO,
            base_pay_cents=500_000,
            base_coef=0.8,
            breakeven_cents=900_000,
            excess_rate=0.25,
            work_days=15,
            note="月中交接",
        )
    rows = _tenures(sid)
    assert len(rows) == 1, "改写不许变成新增一段"
    row = rows[0]
    assert int(row["id"]) == tid
    assert row["advisor_name"] == "乙顾问"
    assert row["base_pay_cents"] == 500_000
    assert row["breakeven_cents"] == 900_000
    assert int(row["work_days"]) == 15
    assert row["note"] == "月中交接"
    assert _store_row(sid)["advisor_name"] == "乙顾问"


def test_save_tenure_rejects_missing_segment():
    sid = _mk_store()
    with pytest.raises(ValueError, match="这段任期已经不在了"):
        _add_tenure(sid, tenure_id=999)


def test_save_tenure_rejects_unknown_store():
    with pytest.raises(ValueError, match="没有这家店"):
        with db.get_db() as conn:
            db.save_advisor_tenure(conn, 987_654, advisor_name="甲", from_month=LONG_AGO)


def test_delete_tenure_clears_snapshot_when_last_segment_goes():
    sid = _mk_store()
    tid = _add_tenure(sid, advisor_name="陈顾问")
    assert _store_row(sid)["advisor_name"] == "陈顾问"
    with db.get_db() as conn:
        db.delete_advisor_tenure(conn, sid, tid)
    assert _tenures(sid) == []
    snap = _store_row(sid)
    assert snap["advisor_name"] == ""
    assert int(snap["has_advisor"]) == 0, "删光最后一段要显式退回无顾问"


def test_delete_tenure_resyncs_when_other_segment_remains():
    sid = _mk_store()
    first = _add_tenure(
        sid, advisor_name="甲顾问", from_month="2020-01", to_month="2020-12"
    )
    _add_tenure(sid, advisor_name="乙顾问", from_month="2022-01")
    with db.get_db() as conn:
        db.delete_advisor_tenure(conn, sid, first)
    assert [row["advisor_name"] for row in _tenures(sid)] == ["乙顾问"]
    assert _store_row(sid)["advisor_name"] == "乙顾问"


def test_delete_tenure_rejects_unknown_segment():
    sid = _mk_store()
    with pytest.raises(ValueError, match="已经删过了"):
        with db.get_db() as conn:
            db.delete_advisor_tenure(conn, sid, 0)


# ---------------------------------------------------------------------------
# db 层：同一时段只允许一位顾问
# ---------------------------------------------------------------------------


def test_overlapping_tenure_is_rejected():
    sid = _mk_store()
    _add_tenure(sid, advisor_name="甲顾问", from_month="2026-11", to_month="2027-04")
    with pytest.raises(ValueError, match="只能有一位专职顾问") as exc:
        _add_tenure(sid, advisor_name="乙顾问", from_month="2027-01", to_month="2027-06")
    assert "甲顾问" in str(exc.value), "报错要指出是谁占着这段区间"
    assert [row["advisor_name"] for row in _tenures(sid)] == ["甲顾问"]


def test_adjacent_tenure_is_allowed():
    sid = _mk_store()
    _add_tenure(sid, advisor_name="甲顾问", from_month="2026-11", to_month="2027-04")
    _add_tenure(sid, advisor_name="乙顾问", from_month="2027-05")
    assert [row["advisor_name"] for row in _tenures(sid)] == ["甲顾问", "乙顾问"]


def test_rewriting_same_segment_does_not_conflict_with_itself():
    sid = _mk_store()
    tid = _add_tenure(sid, advisor_name="甲顾问", from_month="2026-11", to_month="2027-04")
    with db.get_db() as conn:
        db.save_advisor_tenure(
            conn, sid, tenure_id=tid, advisor_name="甲顾问", from_month="2026-11",
            to_month="2027-06",
        )
    assert _tenures(sid)[0]["to_month"] == "2027-06"


def test_duplicate_segment_is_rejected():
    sid = _mk_store()
    _add_tenure(sid, advisor_name="甲顾问", from_month="2026-11")
    with pytest.raises(ValueError):
        _add_tenure(sid, advisor_name="甲顾问", from_month="2026-11")


# ---------------------------------------------------------------------------
# db 层：表单校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"advisor_name": "  "}, "顾问姓名要填"),
        ({"from_month": "2026/11"}, "任期起始月要填成 2026-11 这种格式"),
        ({"from_month": ""}, "任期起始月要填成 2026-11 这种格式"),
        ({"to_month": "十一月"}, "任期结束月要填成 2026-11 这种格式"),
        ({"from_month": "2026-11", "to_month": "2026-05"}, "早于起始月"),
        ({"tier_grade": "C"}, "档位只能选 A 档或 B 近档"),
        ({"base_pay_cents": -1}, "保底底薪不能为负"),
        ({"base_coef": 0}, "考核系数要大于 0"),
        ({"breakeven_cents": -100}, "平衡点不能为负"),
        ({"excess_rate": 1.5}, "超额分成比例要在 0% 到 100% 之间"),
        ({"work_days": 32}, "当月工作日折算只能填 0 到 31 天"),
    ],
)
def test_tenure_validation_messages(kwargs, message):
    sid = _mk_store()
    with pytest.raises(ValueError, match=message):
        _add_tenure(sid, **kwargs)
    assert _tenures(sid) == []


# ---------------------------------------------------------------------------
# db 层：反写快照的参照月
# ---------------------------------------------------------------------------


def test_sync_without_any_tenure_leaves_store_columns_alone():
    sid = _mk_store()
    with db.get_db() as conn:
        conn.execute("UPDATE stores SET advisor_name='手填顾问', has_advisor=1 WHERE id=?", (sid,))
        assert db.sync_store_advisor_snapshot(conn, sid) == ""
    snap = _store_row(sid)
    assert snap["advisor_name"] == "手填顾问", "没有任期行就不许动旧口径快照"
    assert int(snap["has_advisor"]) == 1


def test_sync_picks_segment_covering_the_reference_month():
    sid = _mk_store()
    _add_tenure(sid, advisor_name="甲顾问", from_month="2026-11", to_month="2026-12")
    _add_tenure(sid, advisor_name="乙顾问", from_month="2027-01")
    with db.get_db() as conn:
        assert db.sync_store_advisor_snapshot(conn, sid, "2026-11") == "甲顾问"
        assert db.sync_store_advisor_snapshot(conn, sid, "2026-12") == "甲顾问"
        assert db.sync_store_advisor_snapshot(conn, sid, "2027-01") == "乙顾问"
        assert db.sync_store_advisor_snapshot(conn, sid, "2028-06") == "乙顾问"
        # 2026-10 及以前是双轨期，任何一段都没覆盖到就退回无顾问
        assert db.sync_store_advisor_snapshot(conn, sid, "2026-10") == ""
    snap = _store_row(sid)
    assert snap["advisor_name"] == ""
    assert int(snap["has_advisor"]) == 0


# ---------------------------------------------------------------------------
# 设置页：任期维护 UI
# ---------------------------------------------------------------------------


def _settings_form(sid: int, **overrides) -> dict:
    data = {
        "action": "save_tenure",
        "tab": "stores",
        "store_id": str(sid),
        "tenure_id": "",
        "advisor_name": "钱顾问",
        "from_month": LONG_AGO,
        "to_month": "",
        "tier_grade": "B_NEAR",
        "base_pay": "4200",
        "base_coef": "1.15",
        "breakeven": "7800.5",
        "excess_rate": "35",
        "work_days": "0",
        "note": "",
    }
    data.update(overrides)
    return data


def test_settings_page_shows_tenure_editor(admin_client):
    sid = _mk_store("任期页面店")
    body = admin_client.get(f"/settings?tab=stores&store_id={sid}").get_data(as_text=True)
    assert "专职顾问任期" in body
    assert "tenureDialog" in body
    assert "新增一段任期" in body
    assert "B 近档" in body, "档位下拉来自 tier_choices"
    assert "运营商顾问（由任期表派生）" not in body, "没有任期行时顾问姓名仍可手填"
    assert "还没有任期记录" in body


def test_settings_save_tenure_persists_and_syncs(admin_client):
    sid = _mk_store("任期保存店")
    resp = admin_client.post("/settings", data=_settings_form(sid), follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "顾问任期已保存" in body
    assert "运营商顾问」同步为 钱顾问" in body
    rows = _tenures(sid)
    assert len(rows) == 1
    row = rows[0]
    assert row["tier_grade"] == "B_NEAR"
    assert row["base_pay_cents"] == 420_000
    assert row["breakeven_cents"] == 780_050
    assert row["excess_rate"] == pytest.approx(0.35)
    assert "钱顾问" in body
    snap = _store_row(sid)
    assert snap["advisor_name"] == "钱顾问"
    assert int(snap["has_advisor"]) == 1
    # 有任期行后，页面把「运营商顾问」改成只读派生值
    assert "运营商顾问（由任期表派生）" in body


def test_settings_save_tenure_flashes_on_bad_month(admin_client):
    sid = _mk_store("任期报错店")
    resp = admin_client.post(
        "/settings", data=_settings_form(sid, from_month="2026/11"), follow_redirects=True
    )
    body = resp.get_data(as_text=True)
    assert "任期起始月要填成 2026-11 这种格式" in body
    assert _tenures(sid) == [], "校验失败一个字都不许落库"


def test_settings_save_tenure_flashes_on_overlap(admin_client):
    sid = _mk_store("任期重叠店")
    _add_tenure(sid, advisor_name="甲顾问", from_month="2026-11", to_month="2027-04")
    resp = admin_client.post(
        "/settings",
        data=_settings_form(sid, advisor_name="乙顾问", from_month="2027-02"),
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "只能有一位专职顾问" in body
    assert [row["advisor_name"] for row in _tenures(sid)] == ["甲顾问"]


def test_settings_delete_tenure_clears_derived_snapshot(admin_client):
    sid = _mk_store("任期删除店")
    tid = _add_tenure(sid, advisor_name="孙顾问")
    resp = admin_client.post(
        "/settings",
        data={
            "action": "delete_tenure",
            "tab": "stores",
            "store_id": str(sid),
            "tenure_id": str(tid),
        },
        follow_redirects=True,
    )
    assert "任期已删除" in resp.get_data(as_text=True)
    assert _tenures(sid) == []
    snap = _store_row(sid)
    assert snap["advisor_name"] == ""
    assert int(snap["has_advisor"]) == 0


def test_settings_edit_store_cannot_overwrite_derived_advisor(admin_client):
    sid = _mk_store("任期派生店")
    _add_tenure(sid, advisor_name="周顾问")
    resp = admin_client.post(
        "/settings",
        data={
            "action": "edit_store",
            "tab": "stores",
            "store_id": str(sid),
            "mobile_code": "20000001",
            "area_manager": "示例经理",
            "store_manager": "示例店长",
            "advisor_name": "手填乱入",
            "region_group": "示例区",
            "city": "示例市",
            "store_grade": "A",
            "ai_target": "10",
            "invoice_name": "",
            "lease_area": "",
            "lease_address": "",
            "lease_period": "",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    snap = _store_row(sid)
    assert snap["advisor_name"] == "周顾问", "有任期行时顾问姓名只能由任期表派生"
    assert len(_tenures(sid)) == 1
    # 其它档案字段照旧保存
    with db.get_db() as conn:
        row = conn.execute("SELECT store_manager FROM stores WHERE id=?", (sid,)).fetchone()
    assert row["store_manager"] == "示例店长"


def test_settings_edit_store_still_saves_manual_advisor_without_tenure(admin_client):
    sid = _mk_store("无任期店")
    admin_client.post(
        "/settings",
        data={
            "action": "edit_store",
            "tab": "stores",
            "store_id": str(sid),
            "mobile_code": "20000002",
            "area_manager": "示例经理",
            "store_manager": "示例店长",
            "advisor_name": "手工顾问",
            "region_group": "示例区",
            "city": "示例市",
            "store_grade": "A",
            "ai_target": "10",
            "invoice_name": "",
            "lease_area": "",
            "lease_address": "",
            "lease_period": "",
        },
        follow_redirects=True,
    )
    assert _store_row(sid)["advisor_name"] == "手工顾问"
