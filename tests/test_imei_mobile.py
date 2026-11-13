"""移动串码系统导出：解析、入库、三态判定。

只覆盖 2026-11-11 新增的数据层 `imei_mobile`（`import_mobile` / `mobile_verdict` /
`mobile_map` / `mobile_summary`），**不碰 `imei_todo` 现有口径** —— 那边的用例在
`tests/test_imei.py`，这里连导入都不改它的行为。

口径来源：2026-11-11 拿到的 18 行真实导出样本，三态与三个时间字段完全对应
（待售 = 无销售时间 + 未激活；退省库 = 无销售时间 + 已激活 + 有号码；
已售 = 有销售时间 + 已激活 + 有号码），18 行无一例外。
"""
import csv
import io
from datetime import date

import pytest

from app import db, db_imei

# 真实导出的 39 列（TSV）。这里只填判定要用的，其余留空 —— DictReader 不看空列。
MOBILE_HEADER = [
    "串号", "状态", "是否可用", "是否冻结", "商品大类", "商品子类", "商品类型", "品类名称",
    "品牌", "型号", "入库版本", "单双卡类型", "网络模式", "商品名称", "供货商编码", "供货商名称",
    "供货类型", "采购模式", "下游供货模式", "入库时间", "入厅时间", "仓库名称", "销售时间",
    "地市名称", "归属组织机构编码", "归属组织机构名称", "营业厅类型", "库存类型", "调出机构名称",
    "结算状态", "调入机构名称", "是否同步MIS", "采购批次号", "是否激活", "入网地市", "入网时间",
    "号码", "采购供货商", "最后操作时间",
]


def _table(rows, header=None, delim="\t", encoding="utf-8"):
    """按表头顺序把 dict 拼成 TSV / CSV。缺的列填空串，跟真实导出一样。

    用 csv.writer 而不是手工 join：值里带逗号时会加引号，列不会被顶串。
    """
    cols = list(header if header is not None else MOBILE_HEADER)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delim, lineterminator="\n")
    writer.writerow(cols)
    for r in rows:
        writer.writerow([str(r.get(c, "")) for c in cols])
    return buf.getvalue().encode(encoding)


def _sold(imei, **kw):
    """已售：有销售时间 + 已激活 + 有号码 → 合约办完了。"""
    row = {
        "串号": imei, "状态": "已售", "是否激活": "是",
        "销售时间": "2026-10-15 14:03:00", "入网时间": "2026-10-15 14:03:05",
        "入网地市": "南通", "号码": "13800138000",
        "商品名称": "X500 Pro全网通(12+512G)", "归属组织机构编码": "NT0001",
        "归属组织机构名称": "示例市甲街vivo体验店", "地市名称": "南通市",
        "仓库名称": "门店仓", "最后操作时间": "2026-10-15 14:05:00",
    }
    row.update(kw)
    return row


def _returned(imei, **kw):
    """退省库：没有销售时间，但被插过卡（演示机），有号码。"""
    row = _sold(imei)
    row.update({
        "状态": "退省库", "销售时间": "",
        "入网时间": "2026-10-20 09:12:00", "号码": "13900139000",
        "最后操作时间": "2026-11-09 04:04:05",
    })
    row.update(kw)
    return row


def _on_sale(imei, **kw):
    """待售：干净库存，没激活没号码 —— 这才是要催门店去办的那批。"""
    row = {
        "串号": imei, "状态": "待售", "是否激活": "否",
        "入库时间": "2026-09-01 10:00:00",
        "商品名称": "Y600 Pro全网通(8+256G)", "归属组织机构编码": "NT0002",
        "归属组织机构名称": "示例市乙街vivo专卖店", "地市名称": "南通市",
        "仓库名称": "门店仓", "最后操作时间": "2026-09-01 10:00:00",
    }
    row.update(kw)
    return row


def _mobile_row(conn, imei):
    return conn.execute("SELECT * FROM imei_mobile WHERE imei=?", (imei,)).fetchone()


# ---------------------------------------------------------------- 建表 / 幂等

def test_ensure_creates_mobile_objects_idempotently(tmp_db):
    with db.get_db() as conn:
        db_imei.ensure(conn)
        db_imei.ensure(conn)  # 第二次走快速路径，不能炸
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        )}
    assert {"imei_mobile", "idx_imei_mobile_store", "idx_imei_mobile_state"} <= names


def test_sample_header_matches_real_export():
    """样本必须是真实 39 列，否则下面的解析用例就是自说自话。"""
    assert len(MOBILE_HEADER) == 39


# ---------------------------------------------------------------- 解析 / 入库

def test_import_tsv_three_states(tmp_db):
    data = _table([_sold("863000000000001"), _returned("863000000000002"),
                   _on_sale("863000000000003")])
    with db.get_db() as conn:
        res = db_imei.import_mobile(conn, data, query_day="2026-11-11")
        sold = _mobile_row(conn, "863000000000001")
        returned = _mobile_row(conn, "863000000000002")
        on_sale = _mobile_row(conn, "863000000000003")

    assert res["rows"] == 3 and res["written"] == 3
    assert res["bad_imei"] == 0 and res["blank_state"] == 0
    assert res["states"] == {"已售": 1, "退省库": 1, "待售": 1}
    assert res["unknown_states"] == []
    assert res["query_day"] == "2026-11-11" and res["batch"] == "2026-11-11"

    # 时间戳带时分秒，库里只留日期
    assert sold["state"] == "已售"
    assert sold["sale_day"] == "2026-10-15" and sold["act_day"] == "2026-10-15"
    assert sold["phone"] == "13800138000" and sold["activated"] == "是"
    assert sold["model"] == "X500 Pro全网通(12+512G)"
    assert sold["store_code"] == "NT0001" and sold["city"] == "南通市"
    assert sold["warehouse"] == "门店仓" and sold["last_op"] == "2026-10-15 14:05:00"

    assert returned["state"] == "退省库"
    assert returned["sale_day"] == "" and returned["act_day"] == "2026-10-20"
    assert returned["phone"] == "13900139000"

    assert on_sale["state"] == "待售" and on_sale["activated"] == "否"
    assert on_sale["sale_day"] == "" and on_sale["phone"] == ""


def test_import_csv_delimiter(tmp_db):
    """Excel 另存成 CSV 也认：首行没制表符就按逗号切。"""
    data = _table([_on_sale("863000000000010")], delim=",", encoding="utf-8-sig")
    with db.get_db() as conn:
        res = db_imei.import_mobile(conn, data, query_day="2026-11-11")
        row = _mobile_row(conn, "863000000000010")
    assert res["written"] == 1 and row["state"] == "待售"
    assert row["store_code"] == "NT0002"


def test_comma_in_value_does_not_shift_columns(tmp_db):
    """商品名称里带逗号（CSV 会加引号）不能把后面的列顶串。"""
    data = _table([_on_sale("863000000000011", 商品名称="Y600 Pro,全网通(8+256G)")],
                  delim=",", encoding="utf-8")
    with db.get_db() as conn:
        db_imei.import_mobile(conn, data, query_day="2026-11-11")
        row = _mobile_row(conn, "863000000000011")
    assert row["model"] == "Y600 Pro,全网通(8+256G)"
    assert row["store_code"] == "NT0002"  # 没被顶到别的列


def test_english_headers(tmp_db):
    """对方换个导出模板，列名写成英文也认。"""
    data = _table(
        [{"imei": "863000000000020", "state": "已售"}],
        header=["imei", "state"],
        delim=",",
    )
    with db.get_db() as conn:
        res = db_imei.import_mobile(conn, data, query_day="2026-11-11")
        row = _mobile_row(conn, "863000000000020")
    assert res["written"] == 1 and row["state"] == "已售"


def test_missing_column_keeps_previous_value(tmp_db):
    """这次导出没带「号码」，不能把上次查到的号码清空。"""
    with db.get_db() as conn:
        db_imei.import_mobile(
            conn, _table([_sold("863000000000030")]), query_day="2026-11-10"
        )
        # 第二次只有两列
        db_imei.import_mobile(
            conn,
            _table([{"串号": "863000000000030", "状态": "待售"}],
                   header=["串号", "状态"], delim=","),
            query_day="2026-11-11",
        )
        row = _mobile_row(conn, "863000000000030")

    assert row["state"] == "待售"          # 本次给了，覆盖
    assert row["phone"] == "13800138000"   # 本次没给，保留
    assert row["sale_day"] == "2026-10-15"
    assert row["query_day"] == "2026-11-11"
    assert row["batch"] == "2026-11-11"


def test_column_with_blank_value_is_overwritten(tmp_db):
    """「本次没给这一列」保留旧值，「给了但值空」要覆盖成空 —— 否则退库了还挂着号码。"""
    with db.get_db() as conn:
        db_imei.import_mobile(conn, _table([_sold("863000000000031")]), query_day="2026-11-10")
        db_imei.import_mobile(
            conn, _table([_sold("863000000000031", 号码="", 状态="退省库")]),
            query_day="2026-11-11",
        )
        row = _mobile_row(conn, "863000000000031")
    assert row["phone"] == "" and row["state"] == "退省库"


def test_state_alias_and_unknown(tmp_db):
    data = _table([
        {"串号": "863000000000040", "状态": "待销售"},
        {"串号": "863000000000041", "状态": "退省库存"},
        {"串号": "863000000000042", "状态": "已售出"},
        {"串号": "863000000000043", "状态": " 已售 "},
        {"串号": "863000000000044", "状态": "调拨中"},
        {"串号": "863000000000045", "状态": ""},
    ], header=["串号", "状态"], delim=",")
    with db.get_db() as conn:
        res = db_imei.import_mobile(conn, data, query_day="2026-11-11")
        states = {
            r["imei"]: r["state"]
            for r in conn.execute("SELECT imei, state FROM imei_mobile")
        }

    assert states["863000000000040"] == "待售"
    assert states["863000000000041"] == "退省库"
    assert states["863000000000042"] == "已售"
    assert states["863000000000043"] == "已售"     # 前后空格吃掉
    assert states["863000000000044"] == "调拨中"   # 认不出就原样留着
    assert states["863000000000045"] == ""
    assert res["states"] == {"待售": 1, "退省库": 1, "已售": 2}
    assert res["unknown_states"] == [("调拨中", 1)]
    assert res["blank_state"] == 1


def test_blank_imei_skipped(tmp_db):
    data = _table([
        {"串号": "", "状态": "待售"},
        {"串号": "   ", "状态": "待售"},
        {"串号": "863000000000050", "状态": "待售"},
    ], header=["串号", "状态"], delim=",")
    with db.get_db() as conn:
        res = db_imei.import_mobile(conn, data, query_day="2026-11-11")
        n = conn.execute("SELECT COUNT(*) AS n FROM imei_mobile").fetchone()["n"]
    assert res["bad_imei"] == 2 and res["rows"] == 1 and res["written"] == 1
    assert n == 1


def test_reimport_same_imei_overwrites(tmp_db):
    with db.get_db() as conn:
        db_imei.import_mobile(conn, _table([_on_sale("863000000000060")]), query_day="2026-11-10")
        res = db_imei.import_mobile(conn, _table([_sold("863000000000060")]), query_day="2026-11-11")
        rows = conn.execute("SELECT * FROM imei_mobile").fetchall()
    assert len(rows) == 1 and res["written"] == 1
    assert rows[0]["state"] == "已售" and rows[0]["query_day"] == "2026-11-11"


def test_missing_imei_column_raises(tmp_db):
    data = _table([{"状态": "待售"}], header=["状态"], delim=",")
    with db.get_db() as conn:
        with pytest.raises(ValueError, match="串号"):
            db_imei.import_mobile(conn, data)


def test_gbk_encoding_fallback(tmp_db):
    data = _table([_on_sale("863000000000070")], encoding="gbk")
    with db.get_db() as conn:
        res = db_imei.import_mobile(conn, data, query_day="2026-11-11")
        row = _mobile_row(conn, "863000000000070")
    assert res["written"] == 1 and row["state"] == "待售"


def test_default_query_day_is_today(tmp_db, monkeypatch):
    monkeypatch.setattr(db_imei, "today_local", lambda: date(2026, 11, 11))
    with db.get_db() as conn:
        res = db_imei.import_mobile(conn, _table([_on_sale("863000000000080")]))
    assert res["query_day"] == "2026-11-11" and res["batch"] == "2026-11-11"


def test_explicit_batch_label(tmp_db):
    with db.get_db() as conn:
        res = db_imei.import_mobile(
            conn, _table([_on_sale("863000000000081")]),
            query_day="2026-11-11", batch="南通第一轮",
        )
        row = _mobile_row(conn, "863000000000081")
    assert res["batch"] == "南通第一轮" and row["batch"] == "南通第一轮"


@pytest.mark.parametrize("raw, want", [
    ("2026-11-05 04:04:05", "2026-11-05"),
    ("2026/11/5", "2026-11-05"),
    ("2026.11.10", "2026-11-10"),
    ("2026-01-02", "2026-01-02"),
    ("", ""),
    ("无", "无"),          # 认不出就原样留着，不猜成空
])
def test_day_text(raw, want):
    assert db_imei._day_text(raw) == want


# ---------------------------------------------------------------- 判定

@pytest.mark.parametrize("row, verdict", [
    ({"state": "已售"}, "已结案"),
    ({"state": "退省库"}, "已结案"),
    ({"state": "待售"}, "要催"),
    ({"state": "已售出"}, "已结案"),   # 别名也认
    ({"state": "调拨中"}, "未查"),     # 认不出不下结论
    ({"state": ""}, "未查"),
    ({}, "未查"),
    (None, "未查"),                    # 没问过移动
])
def test_mobile_verdict(row, verdict):
    assert db_imei.mobile_verdict(row) == verdict


def test_mobile_verdict_after_import(tmp_db):
    with db.get_db() as conn:
        db_imei.import_mobile(
            conn,
            _table([_on_sale("863000000000090"), _sold("863000000000091")]),
            query_day="2026-11-11",
        )
        hit = db_imei.mobile_map(conn, ["863000000000090", "863000000000091",
                                        "863000000000092"])
    assert db_imei.mobile_verdict(hit.get("863000000000090")) == "要催"
    assert db_imei.mobile_verdict(hit.get("863000000000091")) == "已结案"
    assert db_imei.mobile_verdict(hit.get("863000000000092")) == "未查"
    assert "863000000000092" not in hit   # 没查过就不在表里


# ---------------------------------------------------------------- 批量查询

def test_mobile_map_chunks_beyond_500(tmp_db):
    imeis = [f"8600000{i:08d}" for i in range(600)]
    assert all(len(x) == 15 for x in imeis)
    with db.get_db() as conn:
        db_imei.import_mobile(
            conn, _table([_on_sale(x) for x in imeis]), query_day="2026-11-11"
        )
        hit = db_imei.mobile_map(conn, imeis)
    assert len(hit) == 600
    assert all(v["state"] == "待售" for v in hit.values())


def test_mobile_map_empty_input(tmp_db):
    with db.get_db() as conn:
        assert db_imei.mobile_map(conn, []) == {}
        assert db_imei.mobile_map(conn, ["", "   "]) == {}


def test_mobile_summary(tmp_db):
    with db.get_db() as conn:
        assert db_imei.mobile_summary(conn)["total"] == 0
        db_imei.import_mobile(
            conn,
            _table([_on_sale("863000000000100"), _on_sale("863000000000101"),
                    _sold("863000000000102"), _returned("863000000000103")]),
            query_day="2026-11-10",
        )
        db_imei.import_mobile(
            conn, _table([_sold("863000000000100")]), query_day="2026-11-11"
        )
        s = db_imei.mobile_summary(conn)

    assert s["total"] == 4
    assert s["by_state"] == {"已售": 2, "待售": 1, "退省库": 1}
    assert s["last_query_day"] == "2026-11-11"


# ---------------------------------------------------------------- 不越界

def test_import_does_not_touch_todo_table(tmp_db):
    """数据层是**只新增**的：灌移动数据不能动 imei_todo 一行。"""
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO imei_todo(imei, updated_at) VALUES (?, ?)",
            ("863000000000110", "2026-11-10T10:00:00"),
        )
        before = conn.execute("SELECT COUNT(*) AS n FROM imei_todo").fetchone()["n"]
        db_imei.import_mobile(
            conn, _table([_on_sale("863000000000110"), _sold("863000000000111")]),
            query_day="2026-11-11",
        )
        after = conn.execute("SELECT COUNT(*) AS n FROM imei_todo").fetchone()["n"]
        todo = conn.execute(
            "SELECT status, seen FROM imei_todo WHERE imei=?", ("863000000000110",)
        ).fetchone()
    assert before == after == 1
    assert todo["status"] == "已售" and todo["seen"] == 1
