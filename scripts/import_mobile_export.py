#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把移动串码系统导出的查询结果灌进 `imei_mobile`。

    # 只看库里现在有什么（不问文件也能跑）
    python3 scripts/import_mobile_export.py --summary

    # 试跑：不写库，顺便和待查清单对账
    python3 scripts/import_mobile_export.py 移动导出_20261112.tsv --dry-run \
        --check 南通待查_20261111/南通_待查串号_全量.txt

    # 真写
    python3 scripts/import_mobile_export.py 移动导出_20261112.tsv \
        --query-day 2026-11-12 --batch 南通第一轮

只碰 `imei_mobile`，不动 `imei_todo` —— 待办口径要等页面改造时再放开。

三态口径（2026-11-11 用 18 行真实样本核实，别临场改）
------------------------------------------------------
    已售   = 有销售时间 + 已激活 + 有号码  → 合约办完了，结案
    退省库 = 无销售时间 + 已激活 + 有号码  → 演示机插卡后退回，移动侧终态，结案
    待售   = 无销售时间 + 未激活          → 机器还挂着，要催门店
查不到的串号不留记录：「表里没有」只是「这次没问它」，不是「移动那边没有这台」。

退出码：0 = 正常；2 = 有需要人看一眼的（认不出的状态 / 清单里大量串号没返回）。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

REPO = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)


@contextmanager
def _conn(dry_run: bool) -> Iterator[sqlite3.Connection]:
    """dry-run 用一块内存库，保证「试跑」真的一个字都不写生产库。"""
    from app import db, db_imei

    if dry_run:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db_imei.ensure(conn)
        try:
            yield conn
        finally:
            conn.close()
        return
    with db.get_db() as c:
        yield c


def _print_summary(conn: sqlite3.Connection) -> None:
    from app import db_imei

    s = db_imei.mobile_summary(conn)
    print("库里现有移动数据：%d 台" % s["total"])
    for state, n in (s["by_state"] or {}).items():
        print("    %-6s %d" % (state or "(空)", n))
    print("最后一次查询：%s" % (s["last_query_day"] or "（没导过）"))


def _print_result(res: Dict[str, Any]) -> None:
    print("")
    print("导入完成：%d 行，写入 %d 台" % (res["rows"], res["written"]))
    if res["bad_imei"]:
        print("    串号空/非法：%d 行（已跳过）" % res["bad_imei"])
    if res["blank_state"]:
        print("    状态为空：%d 台" % res["blank_state"])
    for state in ("已售", "待售", "退省库"):
        n = (res["states"] or {}).get(state, 0)
        if n:
            print("    %-6s %d" % (state, n))
    if res["unknown_states"]:
        print("    认不出的状态：%s" % ", ".join(
            "%s×%d" % (k, v) for k, v in res["unknown_states"]
        ))
    print("    认出列：%s" % ", ".join(res["columns_found"]))
    print("查询日：%s　批次：%s" % (res["query_day"], res["batch"]))


def _read_check_list(path: str) -> List[str]:
    """读待查清单：每行一个串号，容忍 BOM / 空行 / `#` 注释。"""
    out: List[str] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            out.append(token)
    return out


def _check(conn: sqlite3.Connection, path: str) -> int:
    """拿这次的数据对一遍待查清单：结案 / 要催 / 挂起 / 查无此机，四档。

    「查无此机」和「挂起」要分开看，二者的处置完全相反（2026-11-12 用户拍板）：

    - 查无此机 = 移动系统里根本没有这个串号 → **串码另用了**，从待办剔除，
      不催门店、也不当作已办。剔除不是猜的，是移动侧确实查不到。
    - 挂起     = 移动有记录，但状态是「维修」「调拨在途」这类非终态 → 不下结论，
      等移动确认语义。机器不在门店手上，催了也没用，但也不能算办了。
    """
    from app import db_imei

    imeis: List[str] = _read_check_list(path)
    if not imeis:
        print("")
        print("对账跳过：%s 里没有串号" % path)
        return 0

    hit = db_imei.mobile_map(conn, imeis)
    missing = [x for x in imeis if x not in hit]
    # 只对「移动真返回了记录」的串号做判定，否则查无此机也会算进「未查」
    verdicts = Counter(db_imei.mobile_verdict(hit[x]) for x in imeis if x in hit)
    held = [x for x in imeis if x in hit and db_imei.mobile_verdict(hit[x]) == "未查"]
    states = Counter(str(hit[x].get("state") or "") for x in held)
    total = len(imeis)

    print("")
    print("对账（%s）：清单 %d 台" % (Path(path).name, total))
    for label, n in (
        ("已结案", verdicts.get("已结案", 0)),
        ("要催", verdicts.get("要催", 0)),
        ("挂起", len(held)),
        ("查无此机", len(missing)),
    ):
        print("    %-6s %5d 台  %5.1f%%" % (label, n, n * 100.0 / total))
    if held:
        print("    挂起明细：%s" % "，".join(
            "%s×%d" % (s or "(空状态)", c) for s, c in states.most_common()
        ))
    if missing:
        print("    查无此机（串码另用，已剔除）前 5 个：%s" % ", ".join(missing[:5]))
    return len(missing)


def main() -> int:
    ap = argparse.ArgumentParser(description="导入移动串码系统导出到 imei_mobile")
    ap.add_argument("file", nargs="?", default="", help="移动发回来的 TSV / CSV")
    ap.add_argument("--query-day", default="", help="这次查询的日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--batch", default="", help="批次标签，默认跟查询日一致")
    ap.add_argument("--db", default="", help="库路径（默认走 STORE_DAILY_DB 环境变量）")
    ap.add_argument("--dry-run", action="store_true", help="只解析不写库")
    ap.add_argument("--summary", action="store_true", help="顺带打印库里现有移动数据")
    ap.add_argument(
        "--check", default="",
        help="待查清单 txt（逐行一个串号）：既用来对账，也记进 imei_mobile_query"
             "（清单里没返回的记 hit=0 = 串码另用），判定层靠它区分「移动没有这台」"
             "和「还没问过」",
    )
    a = ap.parse_args()

    if a.db:
        os.environ["STORE_DAILY_DB"] = a.db

    if not a.file and not (a.summary or a.check):
        ap.error("要么给导出文件，要么至少加 --summary / --check")

    if a.file and not os.path.exists(a.file):
        raise SystemExit("导出文件不存在：%s" % a.file)

    code = 0
    with _conn(a.dry_run) as conn:
        if a.summary:
            _print_summary(conn)

        if a.file:
            from app import db_imei

            # 给了 --check 就把清单一起传进去：`imei_mobile_query` 要记「这批问了哪些
            # 串号、谁没回来」。不传的话查无此机的串号没有 hit=0 记录，判定层分不清
            # 「移动那边没这台」和「还没问过」，196 台串码另用会被当成待核对再催门店。
            expected = _read_check_list(a.check) if a.check else None
            res = db_imei.import_mobile(
                conn,
                Path(a.file).read_bytes(),
                query_day=a.query_day,
                batch=a.batch,
                expected=expected,
            )
            _print_result(res)
            if res.get("orphan"):
                print("    清单里查无此机：%d 台（串码另用，已登记 hit=0）"
                      % res["orphan"])
            if res["unknown_states"]:
                code = 2   # 有状态认不出，得人看一眼是不是新枚举

        if a.check:
            _check(conn, a.check)

        if a.file and not a.dry_run:
            print("")
            _print_summary(conn)

    if a.dry_run:
        print("")
        print("dry-run：未写库。确认无误后去掉 --dry-run 执行导入。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
