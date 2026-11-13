"""2026-11-13 第四轮外部审查 3 条问题的回归（页面级的那两条在 test_imei.py）。

这里只放判定层的几条：
1. `todo_board(store_ids=[])` 不能退化成「不过滤」把全站串码漏出去（权限口径）；
2. `todo_rows(store_ids=[])` 同一个坑（`include_unmapped=True` 时早退守卫不生效）；
3. `effective_days_asof()` 起算日算不出来时，剩余天数要给 None 而不是清单里的 0。
"""
import csv
import io

from app import db, db_imei


def _tsv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(["串号", "状态"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _mobile(conn, rows, query_day="2026-11-12", batch="T1"):
    return db_imei.import_mobile(conn, _tsv(rows), query_day=query_day, batch=batch)


def _todo(conn, imei, *, store_id=1, sold_day="2026-08-01", state="确认未办",
          own_store="示例店", status="已售"):
    sold = own_store
    conn.execute(
        "INSERT INTO imei_todo(imei, status, store_id, sold_store, own_store,"
        " dispatch_store, model, sold_day, state, seen, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (imei, status, store_id, sold, own_store, sold, "X300", sold_day,
         state, 1, "2026-11-12 09:00:00"),
    )


def test_board_empty_store_ids_does_not_leak_other_stores(tmp_db):
    """传空列表 = 这个人一家店都看不到，不能退化成「不过滤」查出全站串码。

    页面把 `scope["ids"]` 原样传给 `todo_board()`：账号没绑店、或「城市 + 区域经理」
    选出一个没有匹配门店的组合时它就是 `[]`。旧代码 `if ids:` 直接跳过门店过滤，
    结果查出全站所有门店的串码（`summary()` 一直用 `1=0` 挡着，两边口径不一致）。
    `None` 才是「调用方明确要全库」（离线脚本 / 单测）。
    """
    with db.get_db() as conn:
        _todo(conn, "860000000000811", store_id=1)
        _todo(conn, "860000000000812", store_id=2, own_store="另一家店")
        _todo(conn, "860000000000813", store_id=None, own_store="待建店甲")
        _mobile(conn, [[i, "待售"] for i in
                       ("860000000000811", "860000000000812", "860000000000813")])
        empty = db_imei.todo_board(conn, [], asof="2026-11-12")
        empty_admin = db_imei.todo_board(conn, [], asof="2026-11-12",
                                         include_unmapped=True)
        everything = db_imei.todo_board(conn, None, asof="2026-11-12")
        single = db_imei.todo_board(conn, [1], asof="2026-11-12")
    assert empty["rows"] == [] and empty["urge"] == 0
    # 管理员视角（include_unmapped）空集合时只留待建店，同样不能退化成全库
    assert [r["imei"] for r in empty_admin["rows"]] == ["860000000000813"]
    assert len(everything["rows"]) == 3
    assert [r["imei"] for r in single["rows"]] == ["860000000000811"]


def test_todo_rows_empty_store_ids_does_not_leak_other_stores(tmp_db):
    """`todo_rows()` 同一个坑：`include_unmapped=True` 时早退守卫不生效。

    守卫写的是 `if not ids and not include_unmapped: return []`；管理员看得见待建店时
    它不生效，旧代码 `if ids:` 就把门店过滤整个跳过 → 查出全站。空集合下应只留
    `store_id IS NULL` 的待建店。
    """
    with db.get_db() as conn:
        _todo(conn, "860000000000821", store_id=1)
        _todo(conn, "860000000000822", store_id=2, own_store="另一家店")
        _todo(conn, "860000000000823", store_id=None, own_store="待建店甲")
        scoped = db_imei.todo_rows(conn, [], show="all")
        admin = db_imei.todo_rows(conn, [], show="all", include_unmapped=True)
        everything = db_imei.todo_rows(conn, [1, 2], show="all", include_unmapped=True)
    assert scoped == []
    assert [r["imei"] for r in admin] == ["860000000000823"]
    assert len(everything) == 3


def test_effective_days_unknown_start_is_none_not_zero():
    """起算日算不出来时剩余天数给 None，不能拿清单里的 remain_days（0）冒充「剩 0 天」。

    `rule_ver>=2` 却没有 `sold_day`（含导入时日期脏了）就走这条路；给 0 的话页面
    「算不出（售出日缺）」那个分支永远进不去，门店会以为窗口已经用完。
    """
    row = {"rule_ver": 2, "sold_day": "", "remain_days": 0, "age_days": 0,
           "user_type": "", "manual_activated_at": ""}
    assert db_imei.effective_days_asof(row, "2026-11-12") == (None, None, "", "", False)


def test_effective_days_pending_still_none():
    """异地异网未激活的行：待激活、两项都 None（不逐日递减），与 summary() 同口径。"""
    row = {"rule_ver": 2, "sold_day": "2026-08-01", "remain_days": 0, "age_days": 0,
           "user_type": db_imei.USER_REMOTE, "manual_activated_at": ""}
    age_eff, remain_eff, act_state, start, frozen = db_imei.effective_days_asof(
        row, "2026-11-12"
    )
    assert (age_eff, remain_eff, act_state, start, frozen) == (None, None, "待激活", "", False)
