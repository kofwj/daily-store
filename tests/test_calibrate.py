"""用 POS 交易流水校准手机销量（scripts/calibrate_phone_sales.py）。

口径（2026-11-11 用真实流水核实）：
- 只认 `品类 == '手机'`；`销量` 列已带符号（退货 -1），直接求和就是净销量；
- 净销量 < 0 按 0 兜底；
- 未交日报 → 只提示、**绝不写库**（set_day_value 会凭空插 daily_reports，
  让「昨天没交」变成假象、漏报统计失真）；
- 交了日报但这店整份流水里一条记录都没有 → **也不写**（可能导出漏片区，
  写 0 是不可逆的钱坑：稀释比值、拉低考核）；
- 泰州不过滤（日报本来就有泰州门店）。

这些用例守着上面每一条——它们全是"写错了很难发现"的类型。
"""
import importlib.util
import pathlib
from datetime import date

import pytest

from app import db, db_query

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "calibrate_phone_sales.py"
_spec = importlib.util.spec_from_file_location("calibrate_phone_sales", _SCRIPT)
cal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cal)

DAY = date(2026, 11, 10)


def _store_id(code="store-alpha"):
    with db.get_db() as conn:
        return conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]


def _write_flow(tmp_path, lines, name="flow.csv"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _report(store_id, *, phone=None, **others):
    """造一条当天日报（不含 phone_sales 时等于"没填这个指标"）。"""
    with db.get_db() as conn:
        user = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO daily_reports(biz_date, store_id, submitted_by, submitted_at, compact, note)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (DAY.isoformat(), store_id, user, "2026-11-10 20:00:00", "测试"),
        )
        if phone is not None:
            conn.execute(
                "INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)"
                " VALUES (?, ?, 'phone_sales', ?)",
                (DAY.isoformat(), store_id, phone),
            )
        for code, value in others.items():
            conn.execute(
                "INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)"
                " VALUES (?, ?, ?, ?)",
                (DAY.isoformat(), store_id, code, value),
            )


def _phone(store_id):
    with db.get_db() as conn:
        return db_query.day_values(conn, store_id, DAY).get("phone_sales")


# ---- 读流水 ----

def test_load_flow_sums_only_phone_category(tmp_path):
    flow = _write_flow(
        tmp_path,
        [
            "门店名称,品类,销量,完成日期",
            "某店,手机,1,2026/11/10",
            "某店,手机,2,2026/11/10",
            "某店,第三方,9,2026/11/10",      # 配件不算手机销量
            "某店,智能终端,5,2026/11/10",    # 手表平板也不算
        ],
    )
    phone, every, dates = cal.load_flow(flow)
    assert phone["某店"] == 3          # 只累加手机
    assert every["某店"] == 17         # 全品类（用来判断"这家店有没有出现"）
    assert dates == {"2026-11-10"}     # 斜杠日期归一


def test_load_flow_returns_are_negative(tmp_path):
    """退货行的销量是 -1，直接求和就是净销量，不能再乘一次方向。"""
    flow = _write_flow(
        tmp_path,
        [
            "门店名称,品类,销量,完成日期",
            "某店,手机,3,2026/11/10",
            "某店,手机,-1,2026/11/10",
        ],
    )
    phone, _every, _d = cal.load_flow(flow)
    assert phone["某店"] == 2


def test_guess_date_needs_exactly_one_date():
    assert cal.guess_date({"2026-11-10"}) == date(2026, 11, 10)
    assert cal.guess_date({"2026-11-10", "2026-11-11"}) is None
    assert cal.guess_date(set()) is None


# ---- 判定 ----

def test_diff_is_detected_and_value_written(client):
    sid = _store_id()
    _report(sid, phone=3, lead=1, fttr=2)
    with db.get_db() as conn:
        data = cal.build_rows(
            conn, {"示例市甲街vivo体验店": 5}, {"示例市甲街vivo体验店": 5}, DAY
        )
        row = next(r for r in data["rows"] if r["store_id"] == sid)
        assert row["state"] == "fix"
        assert (row["filled"], row["pos"]) == (3, 5)

        done = cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn))
        assert len(done) == 1

    assert _phone(sid) == 5
    # 别的指标一个都不许动
    with db.get_db() as conn:
        vals = db_query.day_values(conn, sid, DAY)
    assert vals["lead"] == 1 and vals["fttr"] == 2


def test_edit_is_traceable(client):
    """校准要能在 report_edits 里查到前后值，且 phone_fix_marks 能读出来。"""
    from app.helpers import phone_fix_marks

    sid = _store_id()
    _report(sid, phone=4)
    with db.get_db() as conn:
        data = cal.build_rows(conn, {"示例市甲街vivo体验店": 8}, {"示例市甲街vivo体验店": 8}, DAY)
        cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn))
        edit = conn.execute(
            "SELECT before_json, after_json, note FROM report_edits"
            " WHERE store_id=? AND biz_date=? ORDER BY id DESC LIMIT 1",
            (sid, DAY.isoformat()),
        ).fetchone()
        assert '"phone_sales": 4' in edit["before_json"]
        assert '"phone_sales": 8' in edit["after_json"]
        assert edit["note"] == cal.NOTE

        marks = phone_fix_marks(conn, [sid], DAY, DAY)
        assert marks[sid][DAY.isoformat()]["before"] == 4
        assert marks[sid][DAY.isoformat()]["after"] == 8


def test_no_report_is_never_written(client):
    """未交日报 → 不写库。写进去会凭空造一条 daily_reports，漏报统计全乱。"""
    sid = _store_id()
    before = _count_reports()
    with db.get_db() as conn:
        data = cal.build_rows(conn, {"示例市甲街vivo体验店": 4}, {"示例市甲街vivo体验店": 4}, DAY)
        row = next(r for r in data["rows"] if r["store_id"] == sid)
        assert row["state"] == "no_report"
        assert "未交日报" in row["reason"]
        assert cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn)) == []

    assert _count_reports() == before  # 没有凭空多出一条
    assert _phone(sid) == 0


def _count_reports():
    with db.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM daily_reports WHERE biz_date=?", (DAY.isoformat(),)
        ).fetchone()[0]


def test_store_absent_from_flow_is_left_alone(client):
    """交了日报，但整份流水里没有这家店 → 不改。

    这一条是防「导出时漏选了某个片区」：把漏掉的店当成实销 0，
    是不可逆的钱坑（比值被稀释、考核被拉低）。
    """
    sid = _store_id()
    _report(sid, phone=3)
    other = "完全不存在的店"
    with db.get_db() as conn:
        data = cal.build_rows(conn, {other: 2}, {other: 2}, DAY)
        row = next(r for r in data["rows"] if r["store_id"] == sid)
        assert row["state"] == "absent"
        assert cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn)) == []

    assert _phone(sid) == 3  # 原样保留


def test_store_present_with_zero_phones_is_calibrated_to_zero(client):
    """店出现在流水里（卖了配件）但手机 0 台 → 这是真 0，可以校准。"""
    sid = _store_id()
    _report(sid, phone=2)
    with db.get_db() as conn:
        data = cal.build_rows(
            conn,
            {},  # 没有手机行
            {"示例市甲街vivo体验店": 3},  # 但有配件行
            DAY,
        )
        row = next(r for r in data["rows"] if r["store_id"] == sid)
        assert row["state"] == "fix"
        assert row["pos"] == 0
        cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn))

    assert _phone(sid) == 0


def test_negative_net_is_clamped_to_zero(client):
    """当天退的比卖的多 → 按 0 兜底，不是负数。"""
    sid = _store_id()
    _report(sid, phone=1)
    with db.get_db() as conn:
        data = cal.build_rows(
            conn, {"示例市甲街vivo体验店": -2}, {"示例市甲街vivo体验店": 3}, DAY
        )
        row = next(r for r in data["rows"] if r["store_id"] == sid)
        assert row["pos"] == 0
        assert row["state"] == "fix"
        assert "退货冲减" in row["reason"]
        cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn))

    assert _phone(sid) == 0


def test_same_value_is_noop(client):
    sid = _store_id()
    _report(sid, phone=5)
    with db.get_db() as conn:
        data = cal.build_rows(
            conn, {"示例市甲街vivo体验店": 5}, {"示例市甲街vivo体验店": 9}, DAY
        )
        row = next(r for r in data["rows"] if r["store_id"] == sid)
        assert row["state"] == "same"
        assert cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn)) == []


def test_running_twice_is_idempotent(client):
    sid = _store_id()
    _report(sid, phone=1)
    flow_phone = {"示例市甲街vivo体验店": 4}
    flow_all = {"示例市甲街vivo体验店": 4}

    with db.get_db() as conn:
        data = cal.build_rows(conn, flow_phone, flow_all, DAY)
        assert len(cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn))) == 1
    assert _phone(sid) == 4

    with db.get_db() as conn:
        data = cal.build_rows(conn, flow_phone, flow_all, DAY)
        assert len(cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn))) == 0
    assert _phone(sid) == 4


def test_unmatched_store_names_are_reported_not_written(client):
    """流水里的店名在档案里对不上 → 忽略并报出来，不猜、不新建门店。"""
    with db.get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
        data = cal.build_rows(conn, {"查无此店": 7}, {"查无此店": 7}, DAY)
        assert data["unmatched"] == {"查无此店": 7}
        after = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    assert after == before


def test_other_city_store_is_calibrated_too(client):
    """非本市门店也照常校准：串码待办排除泰州，但日报本来就有泰州，不能筛城市。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id, name, city FROM stores WHERE city<>'示例市' ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        pytest.skip("示例库里没有外市门店")
    _report(int(row["id"]), phone=1)
    with db.get_db() as conn:
        data = cal.build_rows(conn, {row["name"]: 3}, {row["name"]: 3}, DAY)
        hit = next(r for r in data["rows"] if r["store_id"] == int(row["id"]))
        assert hit["state"] == "fix"
        cal.apply_fixes(conn, data["rows"], DAY, cal.pick_admin(conn))
    assert _phone(int(row["id"])) == 3
