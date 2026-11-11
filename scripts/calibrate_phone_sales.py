#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 POS 交易流水校准前一天（或指定日）的「当天手机销量」。

    python3 scripts/calibrate_phone_sales.py --flow 交易流水_20261111.xlsx
    python3 scripts/calibrate_phone_sales.py --flow 交易流水_20261111.xlsx --apply
    python3 scripts/calibrate_phone_sales.py --flow 交易流水_20261111.xlsx --date 2026-11-10 --db /path/copy.db

不带 ``--apply`` 只打印差异表（dry-run），带 ``--apply`` 才写库。
写库走 ``db_query.set_day_value``：只改 `phone_sales` 一个格子、自动记 `report_edits`
留痕、**不触发企微群播报**。

口径（2026-11-11 用真实流水核实过，别临场改）
------------------------------------------------
1. **只要 `品类 == '手机'`**。流水的 `销量` 列已带符号（退货是 -1），
   直接求和就是净销量，不要再乘一次方向。
   实测：`品类=手机` 的和 与 `六大营收-二级分类 ∈ {线下手机, O2O手机}` 一致（76 台）。
2. **净销量 < 0 按 0 兜底**（当天退的比卖的多），并在结果里标「含退货冲减」。
3. **泰州不过滤**：日报本来就有泰州门店，流水一起校准。
4. **店名对不上档案的流水行直接忽略**（这些店在 store-daily 里没有账号，
   它们的销量不属于任何可校准的格子），但记数并在结尾报出来。
5. **未交日报 = 只提示、绝不写库**。`set_day_value` 在 `daily_reports` 没有记录时
   会自己插一条（其它指标全 0），那会让「昨天按时交了」变成假象、漏报统计失真。
6. **完全不在流水里的店 = 不写库**。看起来像「实销 0」，但也可能是导出时漏了片区；
   实测 11-11 这份流水覆盖了全部 28 家在营店，所以「一家都不出现」本身就该报警，
   由人确认后再单独处理，脚本不擅自把它改成 0。

退出码：0 = 正常（含无所改动）；2 = 有需要人看一眼的异常（未交日报 / 店名对不上）。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


def _find_repo() -> str:
    """找到能 `import app` 的根目录。

    本地是仓库根（`scripts/` 的上一级）；在容器里跑时脚本被拷到 `/app/scripts/`，
    往上一级是 `/app`，正好也是容器里的工作目录。两条都试一下，别写死。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.dirname(here), "/app", os.getcwd()):
        if os.path.isdir(os.path.join(cand, "app")):
            return cand
    return os.path.dirname(here)


REPO = _find_repo()
sys.path.insert(0, REPO)

PHONE_CODE = "phone_sales"
PHONE_CATEGORY = "手机"
NOTE = "POS流水校准"

# 流水里认这几列名（容忍空格 / 别名）
COL_STORE = ("门店名称", "门店", "零售商名称")
COL_CATEGORY = ("品类", "商品大类", "三级分类")
COL_QTY = ("销量", "数量", "销售数量")
COL_DATE = ("完成日期", "业务日期", "销售日期")


def _pick(header: List[str], names: Tuple[str, ...]) -> Optional[int]:
    """按候选列名找下标（去空格）。"""
    norm = {str(h or "").strip(): i for i, h in enumerate(header)}
    for name in names:
        if name in norm:
            return norm[name]
    return None


def load_flow(path: str) -> Tuple[Dict[str, float], Dict[str, float], set]:
    """读流水 → ({店名: 手机净销量}, {店名: 全品类销量}, {出现的日期})。

    只认 ``品类 == 手机``；``销量`` 已带符号，直接累加。
    """
    if path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        # 不能开 read_only：这份导出的 dimension 记成 1x1，
        # 只读模式会照着一行一列读，表头直接丢光（实测）。
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    else:
        import csv

        with open(path, encoding="utf-8-sig") as f:
            rows = [tuple(r) for r in csv.reader(f)]
    if not rows:
        raise SystemExit("流水是空的：%s" % path)

    header = [str(c or "").strip() for c in rows[0]]
    i_store = _pick(header, COL_STORE)
    i_cat = _pick(header, COL_CATEGORY)
    i_qty = _pick(header, COL_QTY)
    i_date = _pick(header, COL_DATE)
    if i_store is None or i_cat is None or i_qty is None:
        raise SystemExit(
            "流水缺列：店名=%s 品类=%s 销量=%s（表头：%s）"
            % (i_store, i_cat, i_qty, "/".join(header[:12]))
        )

    phone: Dict[str, float] = defaultdict(float)
    every: Dict[str, float] = defaultdict(float)
    dates: set = set()
    for row in rows[1:]:
        if len(row) <= max(i_store, i_cat, i_qty):
            continue
        store = str(row[i_store] or "").strip()
        if not store:
            continue
        try:
            qty = float(row[i_qty] or 0)
        except (TypeError, ValueError):
            continue
        every[store] += qty
        if str(row[i_cat] or "").strip() == PHONE_CATEGORY:
            phone[store] += qty
        if i_date is not None:
            day = str(row[i_date] or "").strip().replace("/", "-")[:10]
            if day:
                dates.add(day)
    return dict(phone), dict(every), dates


def guess_date(dates: set) -> Optional[datetime.date]:
    """从流水的完成日期推业务日（流水是 T-1 导出的，日期就是成交日）。"""
    if len(dates) != 1:
        return None
    try:
        return datetime.date.fromisoformat(next(iter(dates)))
    except ValueError:
        return None


def build_rows(
    conn: sqlite3.Connection,
    flow_phone: Dict[str, float],
    flow_all: Dict[str, float],
    biz_date: datetime.date,
) -> Dict[str, Any]:
    """把流水聚合成「每家系统门店一行」，含填报值、流水值、判定与原因。

    ``flow_phone`` 是手机净销量（只 品类=手机），用来定值；
    ``flow_all`` 是全品类（不分品类），只用来判断「这家店今天到底有没有出现在流水里」。
    两者必须分开：一家店可能只卖了配件（手机 0 台），那它照样算「出现在流水里」，
    不能报成「导出漏了这家店」。
    """
    from app import db_imei

    index = db_imei.store_index(conn)
    cities = db_imei._store_cities(conn)

    all_stores = {
        int(r["id"]): {
            "name": r["name"] or "",
            "short": r["short_name"] or "",
            "city": r["city"] or "",
        }
        for r in conn.execute("SELECT id, name, short_name, city FROM stores WHERE active=1")
    }

    # 流水店名 → store_id（手机销量用它算，「这家店有没有出现在流水里」用全品类算）
    per_store: Dict[int, float] = defaultdict(float)
    present: set = set()
    unmatched: Dict[str, float] = defaultdict(float)
    for name, qty in flow_phone.items():
        sid, _city = db_imei._resolve_store(conn, name, index, cities)
        if sid is None:
            unmatched[name] += qty
        else:
            per_store[sid] += qty
    for name in flow_all:
        sid, _city = db_imei._resolve_store(conn, name, index, cities)
        if sid is not None:
            present.add(sid)

    # 当天交了日报、且填了 phone_sales 的门店
    reported: Dict[int, int] = {}
    for r in conn.execute(
        """
        SELECT r.store_id AS sid,
               COALESCE((SELECT f.day_value FROM daily_facts f
                         WHERE f.store_id=r.store_id AND f.biz_date=r.biz_date
                           AND f.metric_code=?), 0) AS value
        FROM daily_reports r WHERE r.biz_date=?
        """,
        (PHONE_CODE, biz_date.isoformat()),
    ):
        reported[int(r["sid"])] = int(r["value"] or 0)

    rows: List[Dict[str, Any]] = []
    for sid in sorted(all_stores):
        info = all_stores[sid]
        pos = int(per_store.get(sid, 0))
        negative = pos < 0
        pos_net = max(0, pos)
        in_flow = sid in present
        if sid not in reported:
            cur = None
            state = "no_report"
            if pos_net:
                reason = "未交日报（流水实售 %d 台），催交后再校准" % pos_net
            else:
                reason = "未交日报，流水也无手机销售"
        elif not in_flow:
            # 交了日报，但这家店**整个没出现在今天的流水里**。
            # 有可能是真 0（当天一单没开），也可能是导出漏了片区——
            # 分不清就不动它，交给人确认。写 0 是**不可逆的钱坑**（会稀释比值、拉低考核）。
            cur = reported[sid]
            state = "absent"
            if cur:
                reason = "流水里没有这家店，未改（填报 %d 台；若确认真 0 台请手工改）" % cur
            else:
                reason = "流水里没有这家店（填报本来就 0，无可校准）"
        else:
            cur = reported[sid]
            if cur == pos_net:
                state, reason = "same", "与流水一致"
            else:
                state, reason = "fix", "待校准 %d → %d" % (cur, pos_net)
                if negative:
                    reason += "（含退货冲减，按 0 兜底）"
        rows.append(
            {
                "store_id": sid,
                "store": info["short"] or info["name"],
                "city": info["city"],
                "filled": cur,
                "pos": pos_net,
                "state": state,
                "reason": reason,
            }
        )

    absent = [r["store"] for r in rows if r["state"] == "absent"]
    return {
        "rows": rows,
        "unmatched": dict(unmatched),
        "absent": absent,
        "reported_n": len(reported),
    }


def print_table(data: Dict[str, Any], biz_date: datetime.date) -> None:
    rows = data["rows"]
    fixes = [r for r in rows if r["state"] == "fix"]
    same = [r for r in rows if r["state"] == "same"]
    norep = [r for r in rows if r["state"] == "no_report"]
    absent_rows = [r for r in rows if r["state"] == "absent"]

    print("")
    print("POS 流水校准 · 业务日 %s" % biz_date.isoformat())
    print("=" * 78)
    print("%-14s %6s %6s %6s  %s" % ("门店", "填报", "流水", "差额", "判定"))
    print("-" * 78)
    marks = {
        "fix": "☑ 待校准",
        "same": "✅ 一致",
        "no_report": "⚠️ 未交日报",
        "absent": "❓ 流水无此店，不改",
    }
    for r in rows:
        filled = "—" if r["filled"] is None else str(r["filled"])
        diff = "" if r["filled"] is None else "%+d" % (r["pos"] - r["filled"])
        print("%-14s %6s %6d %6s  %s" % (r["store"], filled, r["pos"], diff, marks[r["state"]]))
    print("-" * 78)
    print(
        "待校准 %d 家 · 一致 %d 家 · 未交日报 %d 家 · 流水无此店 %d 家（共 %d 家在营店）"
        % (len(fixes), len(same), len(norep), len(absent_rows), len(rows))
    )

    if absent_rows:
        print("")
        print("⚠️ 这些店交了日报，但整份流水里一条记录都没有 —— 可能导出漏了片区，脚本一律不改：")
        for r in absent_rows:
            print("    %-14s %s" % (r["store"], r["reason"]))
    if data["unmatched"]:
        total = sum(data["unmatched"].values())
        print("")
        print(
            "ℹ️ 流水里 %d 家店在系统档案里没有账号，共 %d 台手机销量，无格子可校准（已忽略）："
            % (len(data["unmatched"]), int(total))
        )
        for name, qty in sorted(data["unmatched"].items(), key=lambda x: -x[1])[:12]:
            print("    %-30s %d 台" % (name, int(qty)))
        if len(data["unmatched"]) > 12:
            print("    …… 还有 %d 家" % (len(data["unmatched"]) - 12))


def apply_fixes(
    conn: sqlite3.Connection, rows: List[Dict[str, Any]], biz_date: datetime.date, user_id: int
) -> List[Dict[str, Any]]:
    """只写「已交日报且数量不一致」的门店，逐家一次 set_day_value。"""
    from app import db_query

    done: List[Dict[str, Any]] = []
    for r in rows:
        if r["state"] != "fix":
            continue
        if r["filled"] is None:
            continue
        db_query.set_day_value(
            conn,
            store_id=int(r["store_id"]),
            biz_date=biz_date,
            metric_code=PHONE_CODE,
            value=int(r["pos"]),
            user_id=user_id,
            note=NOTE,
        )
        done.append(r)
    return done


def pick_admin(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise SystemExit("库里没有可用的管理员账号，无法记留痕")
    return int(row["id"])


def main() -> int:
    ap = argparse.ArgumentParser(description="用 POS 交易流水校准某天的手机销量")
    ap.add_argument("--flow", required=True, help="交易流水 xlsx / csv")
    ap.add_argument("--date", default="", help="业务日 YYYY-MM-DD（默认取流水里的完成日期）")
    ap.add_argument("--db", default="", help="库路径（默认走 STORE_DAILY_DB 环境变量）")
    ap.add_argument("--apply", action="store_true", help="真的写库；不带就是 dry-run")
    a = ap.parse_args()

    if not os.path.exists(a.flow):
        raise SystemExit("流水文件不存在：%s" % a.flow)

    if a.db:
        os.environ["STORE_DAILY_DB"] = a.db

    flow_phone, flow_all, dates = load_flow(a.flow)
    if a.date:
        biz_date = datetime.date.fromisoformat(a.date)
    else:
        biz_date = guess_date(dates)
        if biz_date is None:
            raise SystemExit(
                "流水里完成日期不唯一或缺失（%s），请用 --date 指定业务日"
                % (sorted(dates) or "无")
            )

    from app import db

    with db.get_db() as conn:
        data = build_rows(conn, flow_phone, flow_all, biz_date)
        print_table(data, biz_date)

        fixes = [r for r in data["rows"] if r["state"] == "fix"]
        if not a.apply:
            print("")
            print("dry-run：未写库。确认无误后加 --apply 执行校准。")
            return 0 if not (data["absent"] or data["unmatched"]) else 2

        if not fixes:
            print("")
            print("没有需要校准的门店，库未改动。")
            return 0 if not (data["absent"] or data["unmatched"]) else 2

        user_id = pick_admin(conn)
        done = apply_fixes(conn, data["rows"], biz_date, user_id)
        print("")
        print("已校准 %d 家（记 report_edits，备注「%s」）：" % (len(done), NOTE))
        for r in done:
            print("    %-14s %d → %d" % (r["store"], int(r["filled"] or 0), int(r["pos"])))

    return 0 if not (data["absent"] or data["unmatched"]) else 2


if __name__ == "__main__":
    sys.exit(main())
