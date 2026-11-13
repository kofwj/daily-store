"""判定层：移动状态为准，账龄只排序（2026-11-12 用户拍板「先改判断」）。

覆盖 `app/db_imei.py` 新增的 `age_band` / `_days_between` / `mobile_freshness` /
`mobile_asked` / `todo_verdict` / `todo_board`，以及 `import_mobile` 的 `expected`
参数（记「这批问了哪些串号」，用来分「查无此机」和「还没问过」）。

最重要的回归点：**D7+ 的机器不再掉出待办**。旧逻辑里账龄 >= 7 天判成「确认未办」，
而待办清单只取账龄 3-6 天，于是越严重的越看不见（实测 D7+ 未结案 4121 台全部消失）。
"""
import csv
import io
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app import db, db_imei
from app.db_core import today_local

REPO = Path(__file__).resolve().parent.parent


def _tsv(rows):
    """最小可用的移动导出：只要「串号」「状态」两列，其余 import_mobile 会当没带。"""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(["串号", "状态"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _mobile(conn, rows, expected=None, query_day="2026-11-12", batch="T1"):
    return db_imei.import_mobile(
        conn,
        _tsv(rows),
        query_day=query_day,
        batch=batch,
        expected=expected,
    )

def _todo(conn, imei, *, store_id=1, sold_day="2026-08-01", state="确认未办",
          own_store="示例店", sold_store=None, status="已售", model="X300", seen=1):
    """直接写 `imei_todo` 一行：绕过 import_rows 的复杂入参，只测判定。

    `dispatch_store` 按 `import_rows` 的口径算（已售按 POS 销售店派活）。
    """
    sold = own_store if sold_store is None else sold_store
    conn.execute(
        "INSERT INTO imei_todo(imei, status, store_id, sold_store, own_store,"
        " dispatch_store, model, sold_day, state, seen, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (imei, status, store_id, sold, own_store, sold or own_store, model,
         sold_day, state, seen, "2026-11-12 09:00:00"),
    )


# ---------------------------------------------------------------- 纯函数

@pytest.mark.parametrize("age,band", [
    (0, "D0-2"), (2, "D0-2"), (3, "D3-6"), (6, "D3-6"),
    (7, "D7-30"), (30, "D7-30"), (31, "D31-60"), (60, "D31-60"),
    (61, "D61-90"), (90, "D61-90"), (91, "D90+"), (133, "D90+"),
])
def test_age_band(age, band):
    assert db_imei.age_band(age) == band


@pytest.mark.parametrize("text", ["2026-11-05", "2026/11/5", "2026.11.5"])
def test_days_between_accepts_common_formats(text):
    assert db_imei._days_between(text, "2026-11-12") == 7


def test_days_between_unknown_is_none_not_zero():
    """认不出的日期返回 None —— 不能当 0 天，那会变成「刚卖的」被放过。"""
    assert db_imei._days_between("", "2026-11-12") is None
    assert db_imei._days_between("不是日期", "2026-11-12") is None


# ---------------------------------------------------------------- 新鲜度

def test_freshness_ok(tmp_db):
    day = (today_local() - timedelta(days=2)).isoformat()
    with db.get_db() as conn:
        _mobile(conn, [["860000000000001", "待售"]], query_day=day)
        fresh = db_imei.mobile_freshness(conn)
    assert fresh["level"] == "ok"
    assert fresh["days_ago"] == 2
    assert fresh["last_query_day"] == day


@pytest.mark.parametrize("days,level", [(8, "warn"), (15, "stale")])
def test_freshness_degrades(tmp_db, days, level):
    day = (today_local() - timedelta(days=days)).isoformat()
    with db.get_db() as conn:
        _mobile(conn, [["860000000000001", "待售"]], query_day=day)
        assert db_imei.mobile_freshness(conn)["level"] == level


def test_freshness_none_when_never_imported(tmp_db):
    with db.get_db() as conn:
        fresh = db_imei.mobile_freshness(conn)
    assert fresh["level"] == "none"
    assert fresh["days_ago"] is None


# ---------------------------------------------------------------- 问过没

def test_asked_records_hit_and_miss(tmp_db):
    """expected 里有、导出里没有 -> hit=0；两者都能查出来。"""
    with db.get_db() as conn:
        out = _mobile(
            conn,
            [["860000000000001", "待售"]],
            expected=["860000000000001", "860000000000002"],
        )
        assert out["orphan"] == 1
        assert out["orphan_imeis"] == ["860000000000002"]
        asked = db_imei.mobile_asked(conn, ["860000000000001", "860000000000002"])
    assert asked["860000000000001"]["hit"] == 1
    assert asked["860000000000002"]["hit"] == 0


def test_asked_absent_means_never_asked(tmp_db):
    """不传 expected 时不能凭空说「查无此机」——只有命中记录，没问过的不出现。"""
    with db.get_db() as conn:
        out = _mobile(conn, [["860000000000001", "待售"]])
        assert out["orphan"] == 0
        asked = db_imei.mobile_asked(conn, ["860000000000001", "860000000000009"])
    assert "860000000000001" in asked
    assert "860000000000009" not in asked


def test_asked_uses_latest_batch(tmp_db):
    """同一串号问过两批，结论以最新一批为准。"""
    with db.get_db() as conn:
        _mobile(conn, [["860000000000001", "待售"]], expected=[], batch="旧",
                query_day="2026-11-01")
        _mobile(conn, [], expected=["860000000000001"], batch="新",
                query_day="2026-11-12")
        asked = db_imei.mobile_asked(conn, ["860000000000001"])
    assert asked["860000000000001"]["hit"] == 0
    assert asked["860000000000001"]["batch"] == "新"


# ---------------------------------------------------------------- 单台判定

def test_verdict_pos_returned_closes():
    assert db_imei.todo_verdict({"state": "已退货"}, None)[0] == db_imei.TODO_CLOSED


@pytest.mark.parametrize("mobile_state", ["已售", "退省库"])
def test_verdict_mobile_closed(mobile_state):
    verdict, _ = db_imei.todo_verdict({"state": "确认未办"}, {"state": mobile_state})
    assert verdict == db_imei.TODO_CLOSED


def test_verdict_mobile_on_sale_urges():
    verdict, reason = db_imei.todo_verdict({"state": "确认未办"}, {"state": "待售"})
    assert verdict == db_imei.TODO_URGE
    assert "待售" in reason


def test_verdict_mobile_on_sale_beats_pos_done():
    """台账认已办但移动说待售 -> 仍然要催，理由指向「补登记」。"""
    verdict, reason = db_imei.todo_verdict({"state": "已办"}, {"state": "待售"})
    assert verdict == db_imei.TODO_URGE
    assert "补登记" in reason


@pytest.mark.parametrize("mobile_state", ["维修", "调拨在途"])
def test_verdict_unknown_mobile_state_holds(mobile_state):
    """非终态（维修 / 调拨在途）不结案也不催，挂起等人确认语义。"""
    verdict, _ = db_imei.todo_verdict({"state": "确认未办"}, {"state": mobile_state})
    assert verdict == db_imei.TODO_HOLD


def test_verdict_orphan_when_asked_but_missing():
    verdict, reason = db_imei.todo_verdict(
        {"state": "确认未办"}, None, {"hit": 0, "query_day": "2026-11-12"}
    )
    assert verdict == db_imei.TODO_ORPHAN
    assert "查无此机" in reason


def test_verdict_pending_when_never_asked():
    verdict, _ = db_imei.todo_verdict({"state": "确认未办"}, None, None)
    assert verdict == db_imei.TODO_PENDING


# ---------------------------------------------------------------- 看板

def test_board_d7_plus_no_longer_disappears(tmp_db):
    """核心回归：账龄 100 天 + 移动待售，必须出现在待办里。

    旧逻辑这条会被判「确认未办」并被待办清单（只要 3-6 天）排除，
    于是店里最该办的机器反而看不见。
    """
    imei = "860000000000101"
    with db.get_db() as conn:
        _todo(conn, imei, sold_day="2026-08-04")     # 距 11-12 正好 100 天
        _mobile(conn, [[imei, "待售"]])
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["urge"] == 1
    row = board["rows"][0]
    assert row["imei"] == imei
    assert row["age"] == 100
    assert row["band"] == "D90+"
    assert row["verdict"] == db_imei.TODO_URGE


def test_board_counts_four_kinds(tmp_db):
    """已结案 / 要催 / 挂起 / 串码另用四种同时存在时各归各位。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000201")                       # 移动已售 -> 结案
        _todo(conn, "860000000000202")                       # 移动待售 -> 催
        _todo(conn, "860000000000203")                       # 维修   -> 挂起
        _todo(conn, "860000000000204")                       # 查无   -> 剔除
        _mobile(
            conn,
            [["860000000000201", "已售"],
             ["860000000000202", "待售"],
             ["860000000000203", "维修"]],
            expected=["860000000000201", "860000000000202",
                      "860000000000203", "860000000000204"],
        )
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["verdicts"][db_imei.TODO_CLOSED] == 1
    assert board["verdicts"][db_imei.TODO_URGE] == 1
    assert board["verdicts"][db_imei.TODO_HOLD] == 1
    assert board["verdicts"][db_imei.TODO_ORPHAN] == 1
    assert board["urge"] == 1
    assert board["rows"][0]["imei"] == "860000000000202"


def test_board_pending_falls_back_to_age(tmp_db):
    """没有移动数据时按账龄兜底，但理由要写明「未与移动核对」。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000301", sold_day="2026-10-01")   # 42 天
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["mode"] == "降级：按账龄"
    assert board["urge"] == 1
    assert "未与移动核对" in board["rows"][0]["reason"]


def test_board_young_pending_stays_quiet(tmp_db):
    """刚卖出去、还没到账龄的，先不催。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000302", sold_day="2026-11-11")   # 1 天
        board = db_imei.todo_board(conn, asof="2026-11-12", min_age=3)
    assert board["urge"] == 0


def test_board_age_is_recomputed_not_stored(tmp_db):
    """账龄按 asof 现算，不认 imei_todo.age_days（那是导入时写死的死值）。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000303", sold_day="2026-08-04")
        conn.execute("UPDATE imei_todo SET age_days=5 WHERE imei='860000000000303'")
        _mobile(conn, [["860000000000303", "待售"]])
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["rows"][0]["age"] == 100


def test_board_store_filter(tmp_db):
    with db.get_db() as conn:
        _todo(conn, "860000000000401", store_id=1)
        _todo(conn, "860000000000402", store_id=2)
        _mobile(conn, [["860000000000401", "待售"], ["860000000000402", "待售"]])
        board = db_imei.todo_board(conn, store_ids=[1], asof="2026-11-12")
    assert [r["imei"] for r in board["rows"]] == ["860000000000401"]


def test_board_skips_unseen_rows(tmp_db):
    """seen=0 是上一份清单里被替掉的行，不该再催。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000501", seen=0)
        _mobile(conn, [["860000000000501", "待售"]])
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["urge"] == 0


def test_board_store_rollup(tmp_db):
    with db.get_db() as conn:
        _todo(conn, "860000000000601", store_id=7, own_store="示例店A", sold_day="2026-08-04")
        _todo(conn, "860000000000602", store_id=7, own_store="示例店A", sold_day="2026-10-20")
        _mobile(conn, [["860000000000601", "待售"], ["860000000000602", "待售"]])
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert len(board["stores"]) == 1
    s = board["stores"][0]
    assert s["store"] == "示例店A"
    assert s["urge"] == 2
    assert s["oldest"] == 100
    assert s["d90"] == 1
    assert s["d7_30"] == 1
    assert s["over90"] == 1


# -------------------------------------------- 2026-11-12 复核补的回归

def test_board_ignores_unsold_stock_rows(tmp_db):
    """未售库存池（status='未售'）不进待办。

    `imei_todo` 同时存已售台账和未售库存（生产上 900+ 台），导库存时「库存日期」
    又写进了 sold_day。不过滤的话在库天数会被当成账龄去催门店办合约
    （实测 urge 从 50 涨到 987）。与 `todo_rows()` 的 `t.status='已售'` 对齐。
    """
    with db.get_db() as conn:
        _todo(conn, "860000000000701", sold_day="2026-08-04")
        _todo(conn, "860000000000702", status="未售", sold_day="2026-08-04", state="")
        _mobile(conn, [["860000000000701", "待售"], ["860000000000702", "待售"]])
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["total"] == 1
    assert [r["imei"] for r in board["rows"]] == ["860000000000701"]


def test_board_unmapped_stores_kept_apart(tmp_db):
    """待建店（store_id 为空）按店名分行，不能塌成一家、也不能互相改名。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000711", store_id=None, own_store="万象城")
        _todo(conn, "860000000000712", store_id=None, own_store="海门龙信")
        _mobile(conn, [["860000000000711", "待售"], ["860000000000712", "待售"]])
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert len(board["stores"]) == 2
    assert {s["store"] for s in board["stores"]} == {"万象城", "海门龙信"}
    assert {r["store"] for r in board["rows"]} == {"万象城", "海门龙信"}


def test_board_uses_pos_store_not_stock_store(tmp_db):
    """已售机器按 POS 销售店派活，店名不能显示成库存登记店。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000721", store_id=5,
              own_store="库存登记店", sold_store="POS销售店")
        _mobile(conn, [["860000000000721", "待售"]])
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["rows"][0]["store"] == "POS销售店"


def test_board_unknown_sold_day_is_not_treated_as_new(tmp_db):
    """售出日认不出时不能当 0 天：会被 min_age 过滤掉，等于漏催。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000731", sold_day="")
        board = db_imei.todo_board(conn, asof="2026-11-12", min_age=3)
    assert board["urge"] == 1
    row = board["rows"][0]
    assert row["age"] is None
    assert row["band"] == "未知"
    assert "售出日认不出" in row["reason"]


def test_board_unknown_age_sorts_last(tmp_db):
    """账龄未知的排最后，不冒充 0 天挤到最前面。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000761", sold_day="")
        _todo(conn, "860000000000762", sold_day="2026-09-01")
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert [r["imei"] for r in board["rows"]] == ["860000000000762", "860000000000761"]


def test_board_reports_degraded_count(tmp_db):
    """按账龄降级催来的台数单独计数，别混进移动给的结论里。"""
    with db.get_db() as conn:
        _todo(conn, "860000000000751", sold_day="2026-10-01")
        board = db_imei.todo_board(conn, asof="2026-11-12")
    assert board["degraded"] == 1
    assert board["urge"] == 1


def test_asked_same_day_keeps_latest_batch(tmp_db):
    """同一天导两批时认最后一次：按 batch 名字排会让旧批次盖掉新批次。"""
    with db.get_db() as conn:
        _mobile(conn, [], expected=["860000000000741"], batch="第二轮")   # hit=0
        _mobile(conn, [["860000000000741", "待售"]], batch="第三轮")      # hit=1
        asked = db_imei.mobile_asked(conn, ["860000000000741"])
    assert asked["860000000000741"]["hit"] == 1


def test_cli_check_list_marks_missing_as_orphan(tmp_path):
    """命令行导入给了 --check 就要把清单传给 expected。

    不传的话「查无此机」没有 hit=0 记录，判定层分不清「移动那边没这台」和
    「还没问过」，196 台串码另用会被降级成要催再压门店一遍。
    """
    tsv = tmp_path / "export.tsv"
    tsv.write_text("串号\t状态\n860000000000801\t待售\n", encoding="utf-8")
    check = tmp_path / "check.txt"
    check.write_text("# 待查清单\n860000000000801\n860000000000802\n", encoding="utf-8")
    db_path = tmp_path / "t.db"
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "import_mobile_export.py"),
         str(tsv), "--db", str(db_path), "--query-day", "2026-11-12",
         "--batch", "测试批", "--check", str(check)],
        check=True, capture_output=True,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        rows = {r[0]: r[1] for r in conn.execute("SELECT imei, hit FROM imei_mobile_query")}
    finally:
        conn.close()
    assert rows == {"860000000000801": 1, "860000000000802": 0}
