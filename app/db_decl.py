"""顺差串码声明 + 待核让利垫资（一张表两用，V0.4.01）。

为什么只有一张表：
- 「这台顺差机卖出去了」和「这笔购机让利要垫资」是同一个事件的两种视角，
  合成一条 `imei_decl` 记录，就不再需要第二张表去关联它们；
- 状态只有三个：`待核 / 已确认 / 作废`。已兑付之后的改动靠 `advance_posts`
  的负数冲账行表达，不新增状态。

为什么待核不进 `advance_posts`：
- 那张表的金额被十几处直接求和（垫资页 KPI、兑付页合计、结算底稿的实际酬金列）。
  加一个「未确认」状态位，就要挨个加过滤，漏一处就是静默多算钱。
- 待在 `imei_decl` 里，确认前一分钱不参与任何合计，现有钱口径零改动。

为什么不写 `imei_todo`：
- 用户每天导库存清单，`import_rows` 会 UPSERT 覆盖 `status / store_id / sold_day`，
  写进去也会被冲掉。
- 出池改用查询谓词 `live_decl_sql()`（池子排除有有效声明的串码），
  跟 `imei_todo.status` 无关；声明一作废，这台自动回池。

两条来源汇到同一个出口：
- `src='deal'`：触客成交时选串码（主动路）；
- `src='report'`：串码待办自报「已办 + 顺差让利」（兜底路，卖出去却没填触客的机器）。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .db_core import _now, begin_immediate, require_lastrowid, today_local
from .db_core import sql_in as _store_in

# 声明状态
DECL_PENDING = "待核"
DECL_CONFIRMED = "已确认"
DECL_VOID = "作废"
DECL_STATES = (DECL_PENDING, DECL_CONFIRMED, DECL_VOID)

# 来源
SRC_DEAL = "deal"
SRC_REPORT = "report"

# 触发垫资的业务类型：只有「顺差让利」是购机让利垫资。
# AI手机合约 / 直降合约 / 送2G流量 只闭环待办，不产生购机让利垫资（口径见实施计划 §1）。
REBATE_BIZ = "顺差让利"

# 让利档位（分）。默认 100 元/台，管理员可在设置页改。
DEFAULT_REBATE_CENTS = 10000
REBATE_SETTING = "imei_rebate_cents"

_SCHEMA_STMTS = (
    """
    CREATE TABLE IF NOT EXISTS imei_decl (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        imei TEXT NOT NULL,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        own_store TEXT NOT NULL DEFAULT '',
        deal_id INTEGER,
        report_id INTEGER,
        src TEXT NOT NULL DEFAULT 'deal',
        biz_date TEXT NOT NULL,
        rebate_cents INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT '待核',
        advance_id INTEGER,
        confirmed_by INTEGER,
        confirmed_at TEXT NOT NULL DEFAULT '',
        void_reason TEXT NOT NULL DEFAULT '',
        voided_at TEXT NOT NULL DEFAULT '',
        user_id INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_imei_decl_live ON imei_decl(imei) WHERE state <> '作废'",
    "CREATE INDEX IF NOT EXISTS idx_imei_decl_queue ON imei_decl(state, biz_date)",
    "CREATE INDEX IF NOT EXISTS idx_imei_decl_advance ON imei_decl(advance_id)",
)

_SCHEMA_OBJECTS = ("imei_decl", "uniq_imei_decl_live", "idx_imei_decl_queue", "idx_imei_decl_advance")


def ensure(conn: sqlite3.Connection) -> None:
    """懒建声明表（与 imei_todo / aux_queries 同款做法，不写迁移号）。"""
    existing = {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN (%s)"
            % ",".join("?" * len(_SCHEMA_OBJECTS)),
            _SCHEMA_OBJECTS,
        )
    }
    if len(existing) < len(_SCHEMA_OBJECTS):
        for stmt in _SCHEMA_STMTS:
            conn.execute(stmt)


def live_decl_sql(alias: str = "t") -> str:
    """「有未作废声明」的 SQL 片段（唯一来源）。

    调用方把它塞进 WHERE 即可：``AND {live_decl_sql()}``。
    语义：这台串码已经由某家店声明卖出、且声明没被作废——也就是「已办」。
    """
    return (
        f"EXISTS (SELECT 1 FROM imei_decl d "
        f"WHERE d.imei={alias}.imei AND d.state<>'{DECL_VOID}')"
    )


def no_live_decl_sql(alias: str = "t") -> str:
    """「没有未作废声明」= 还在池子里的 SQL 片段（唯一来源）。"""
    return "NOT " + live_decl_sql(alias)


def rebate_cents(conn: sqlite3.Connection) -> int:
    """当前让利档位（分）。非法值回默认，不让设置页的脏数据把金额改掉。"""
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (REBATE_SETTING,)).fetchone()
    try:
        value = int(str(row[0] if row else "").strip())
    except (TypeError, ValueError, IndexError):
        return DEFAULT_REBATE_CENTS
    if value < 0 or value > 100_000_00:
        return DEFAULT_REBATE_CENTS
    return value


def set_rebate_cents(conn: sqlite3.Connection, cents: int) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (REBATE_SETTING, str(int(cents))),
    )


def live_decl(conn: sqlite3.Connection, imei: str) -> Optional[sqlite3.Row]:
    """这台串码当前的有效声明（没有就 None）。"""
    ensure(conn)
    return conn.execute(
        "SELECT * FROM imei_decl WHERE imei=? AND state<>? ORDER BY id DESC LIMIT 1",
        (str(imei).strip(), DECL_VOID),
    ).fetchone()


def _insert_decl(
    conn: sqlite3.Connection,
    *,
    imei: str,
    store_id: int,
    own_store: str,
    src: str,
    biz_date: date,
    rebate: int,
    deal_id: Optional[int],
    report_id: Optional[int],
    user_id: Optional[int],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO imei_decl(
            imei, store_id, own_store, deal_id, report_id, src, biz_date,
            rebate_cents, state, user_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(imei).strip(),
            int(store_id),
            (own_store or "").strip()[:80],
            int(deal_id) if deal_id else None,
            int(report_id) if report_id else None,
            src,
            biz_date.isoformat(),
            int(rebate),
            DECL_PENDING,
            int(user_id) if user_id else None,
            _now(),
        ),
    )
    return require_lastrowid(cur)


def create_from_deal(
    conn: sqlite3.Connection,
    *,
    imei: str,
    store_id: int,
    own_store: str = "",
    deal_id: int,
    biz_date: date,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """触客成交生成（或复用）一条待核垫资声明。

    返回值给页面提示用：
    - ``{"created": True, "decl_id": N, "rebate_cents": M}`` 新建；
    - ``{"created": False, "reason": "declared", "owner": "某店"}`` 这台已被别家声明卖出，
      **不报错、不影响触客保存**（触客条数是 KPI，不能因为串码撞了就丢）；
    - ``{"created": False, "reason": "duplicate"}`` 本单已经有一条有效声明（重复提交）。
    调用方必须已经在写事务里（``record_deal_post`` 已 ``BEGIN IMMEDIATE``）。
    """
    ensure(conn)
    imei = str(imei or "").strip()
    if not imei:
        return {"created": False, "reason": "empty"}
    cur = live_decl(conn, imei)
    if cur is not None:
        if deal_id and cur["deal_id"] and int(cur["deal_id"]) == int(deal_id):
            return {"created": False, "reason": "duplicate", "decl_id": int(cur["id"])}
        owner = conn.execute(
            "SELECT short_name, name FROM stores WHERE id=?", (int(cur["store_id"]),)
        ).fetchone()
        who = (owner["short_name"] or owner["name"] or "") if owner else ""
        return {"created": False, "reason": "declared", "owner": who}
    rebate = rebate_cents(conn)
    decl_id = _insert_decl(
        conn,
        imei=imei,
        store_id=store_id,
        own_store=own_store,
        src=SRC_DEAL,
        biz_date=biz_date,
        rebate=rebate,
        deal_id=deal_id,
        report_id=None,
        user_id=user_id,
    )
    return {"created": True, "decl_id": decl_id, "rebate_cents": rebate}


def create_from_report(
    conn: sqlite3.Connection,
    *,
    imei: str,
    store_id: int,
    own_store: str = "",
    report_id: int,
    biz_date: Optional[date] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """串码待办自报「已办 + 顺差让利」生成待核垫资（兜底路）。

    已经由成交闭环的机器不再生成第二条（幂等返回 ``duplicate``），
    门店看到的是「已由成交闭环」，不是报错。
    """
    ensure(conn)
    imei = str(imei or "").strip()
    if not imei:
        return {"created": False, "reason": "empty"}
    cur = live_decl(conn, imei)
    if cur is not None:
        return {"created": False, "reason": "duplicate", "decl_id": int(cur["id"])}
    rebate = rebate_cents(conn)
    decl_id = _insert_decl(
        conn,
        imei=imei,
        store_id=store_id,
        own_store=own_store,
        src=SRC_REPORT,
        biz_date=biz_date or today_local(),
        rebate=rebate,
        deal_id=None,
        report_id=report_id,
        user_id=user_id,
    )
    return {"created": True, "decl_id": decl_id, "rebate_cents": rebate}


def _advance_row(conn: sqlite3.Connection, advance_id: Optional[int]) -> Optional[sqlite3.Row]:
    if not advance_id:
        return None
    return conn.execute("SELECT * FROM advance_posts WHERE id=?", (int(advance_id),)).fetchone()


def void_decl(
    conn: sqlite3.Connection,
    decl: sqlite3.Row | Dict[str, Any],
    *,
    reason: str,
    user_id: Optional[int] = None,
    cascade_advance: bool = True,
) -> Dict[str, Any]:
    """作废一条声明（行不删，留审计）。

    级联规则（实施计划 §5）：
    - 待核：直接作废，池子里立刻回到这台；
    - 已确认、未兑付：删掉对应的垫资行（现有 ``delete_advance`` 允许删未兑付），再作废；
    - 已确认、已兑付：**不删原行**，写一条负数冲账（``ext_id`` 带 ``:R``），再作废。
    """
    ensure(conn)
    row = decl
    state = str(row["state"] or "")
    if state == DECL_VOID:
        return {"voided": False, "reason": "already_void"}
    out: Dict[str, Any] = {"voided": True, "state": state}
    adv = _advance_row(conn, row["advance_id"]) if cascade_advance else None
    if adv is not None:
        if int(adv["paid"] or 0):
            from .db_advances import cents_to_yuan, record_advance

            imei = str(row["imei"])
            decl_id = int(row["id"])
            record_advance(
                conn,
                store_id=int(row["store_id"]),
                user_id=int(user_id) if user_id else int(row["user_id"] or 0),
                biz_date=date.fromisoformat(str(row["biz_date"])[:10]),
                phone=str(adv["phone"] or ""),
                rebate=-cents_to_yuan(row["rebate_cents"]),
                note=f"作废冲账 {imei} · {reason}",
                source="imei",
                ext_id=f"imei:{imei}:{decl_id}:R",
            )
            out["reversed"] = True
        else:
            from .db_advances import delete_advance

            # allow_imported：自动生成的行本来就不许手删，这里是「作废」这一条正规路径
            delete_advance(
                conn,
                int(adv["id"]),
                int(row["store_id"]),
                user_id=int(user_id) if user_id else int(row["user_id"] or 0),
                allow_imported=True,
            )
            out["deleted_advance"] = int(adv["id"])
    conn.execute(
        "UPDATE imei_decl SET state=?, void_reason=?, voided_at=? WHERE id=?",
        (DECL_VOID, (reason or "").strip()[:120], _now(), int(row["id"])),
    )
    return out


def void_by_deal(
    conn: sqlite3.Connection,
    *,
    deal_id: int,
    reason: str,
    user_id: Optional[int] = None,
) -> int:
    """触客覆盖 / 改未成交 / 删除时，作废这张单名下所有有效声明。"""
    ensure(conn)
    rows = conn.execute(
        "SELECT * FROM imei_decl WHERE deal_id=? AND state<>?", (int(deal_id), DECL_VOID)
    ).fetchall()
    n = 0
    for row in rows:
        void_decl(conn, row, reason=reason, user_id=user_id)
        n += 1
    return n


def sync_deal(
    conn: sqlite3.Connection,
    *,
    store_id: int,
    deal_id: int,
    imei: str,
    closed: bool,
    biz_date: date,
    user_id: Optional[int] = None,
    own_store: str = "",
) -> Dict[str, Any]:
    """让声明跟着触客单走：串码变了废旧开新，改成未成交就作废，一致则不动。

    调用方是 ``record_deal_post``（同一个 ``BEGIN IMMEDIATE`` 事务内），
    所以触客与声明同生同死，不会只写一半。
    """
    ensure(conn)
    imei = str(imei or "").strip()
    kept = 0
    rows = conn.execute(
        "SELECT * FROM imei_decl WHERE deal_id=? AND state<>?", (int(deal_id), DECL_VOID)
    ).fetchall()
    for row in rows:
        same = bool(closed) and str(row["imei"]) == imei
        if same:
            kept += 1
            continue
        void_decl(
            conn,
            row,
            reason="触客改成未成交" if not closed else "触客换了串码",
            user_id=user_id,
        )
    if not closed or not imei:
        return {"created": False, "kept": kept, "reason": "not_closed" if not closed else "no_imei"}
    if kept:
        return {"created": False, "kept": kept, "reason": "unchanged"}
    return create_from_deal(
        conn,
        imei=imei,
        store_id=store_id,
        own_store=own_store,
        deal_id=deal_id,
        biz_date=biz_date,
        user_id=user_id,
    )


def confirm(
    conn: sqlite3.Connection,
    decl_ids: Iterable[int | str],
    *,
    user_id: int,
) -> Dict[str, Any]:
    """管理员一键确认：待核 → 已确认，并落一条 ``paid=0`` 的垫资（进待兑付）。

    ``biz_date`` 用**成交日**（用户 2026-11-10 拍板：31 号的垫资 1、2 号确认，
    照样算上月），所以跨月确认不会把账算到本月。
    金额用声明里的**快照**，月中改档不影响已有待核。
    """
    ensure(conn)
    ids = [int(i) for i in decl_ids if str(i).isdigit() or isinstance(i, int)]
    if not ids:
        return {"confirmed": 0, "skipped": 0}
    from .db_advances import cents_to_yuan, record_advance

    begin_immediate(conn)
    confirmed = 0
    skipped = 0
    for decl_id in ids:
        row = conn.execute("SELECT * FROM imei_decl WHERE id=?", (decl_id,)).fetchone()
        if row is None or str(row["state"] or "") != DECL_PENDING:
            skipped += 1
            continue
        amount = cents_to_yuan(row["rebate_cents"])
        if amount <= 0:
            skipped += 1
            continue
        phone = ""
        if row["deal_id"]:
            deal = conn.execute(
                "SELECT phone, model FROM deal_posts WHERE id=?", (int(row["deal_id"]),)
            ).fetchone()
            if deal is not None:
                phone = str(deal["phone"] or "")
        model = ""
        todo = conn.execute("SELECT model FROM imei_todo WHERE imei=?", (str(row["imei"]),)).fetchone()
        if todo is not None:
            model = str(todo["model"] or "")
        note = f"顺差让利 · {row['imei']}"
        if model:
            note += f" · {model}"
        adv_id = record_advance(
            conn,
            store_id=int(row["store_id"]),
            user_id=int(user_id),
            biz_date=date.fromisoformat(str(row["biz_date"])[:10]),
            phone=phone,
            rebate=amount,
            note=note[:500],
            source="imei",
            ext_id=f"imei:{row['imei']}:{int(row['id'])}",
            paid=False,
        )
        conn.execute(
            "UPDATE imei_decl SET state=?, advance_id=?, confirmed_by=?, confirmed_at=? WHERE id=?",
            (DECL_CONFIRMED, adv_id, int(user_id), _now(), decl_id),
        )
        confirmed += 1
    return {"confirmed": confirmed, "skipped": skipped}


def pending_rows(
    conn: sqlite3.Connection,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    store_ids: Optional[Sequence[int]] = None,
    limit: int = 0,
) -> List[Dict[str, Any]]:
    """待核清单（管理员用），带门店、机型、号码、来源、快照金额。"""
    ensure(conn)
    where = ["d.state=?"]
    params: List[Any] = [DECL_PENDING]
    if start:
        where.append("d.biz_date>=?")
        params.append(start.isoformat())
    if end:
        where.append("d.biz_date<=?")
        params.append(end.isoformat())
    if store_ids is not None:
        clause, ids = _store_in("d.store_id", store_ids)
        where.append(clause)
        params.extend(ids)
    sql = f"""
        SELECT d.*, st.name AS store_name, st.short_name AS store_short,
               t.model AS model, t.own_store AS todo_own_store,
               dp.phone AS phone, dp.model AS deal_model,
               ROUND(d.rebate_cents / 100.0, 2) AS rebate
        FROM imei_decl d
        LEFT JOIN stores st ON st.id = d.store_id
        LEFT JOIN imei_todo t ON t.imei = d.imei
        LEFT JOIN deal_posts dp ON dp.id = d.deal_id
        WHERE {' AND '.join(where)}
        ORDER BY d.biz_date, d.id
    """
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params)]


def summary(conn: sqlite3.Connection, store_ids: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """待核笔数与金额（按分累加，不飘小数）。"""
    ensure(conn)
    where = ["state=?"]
    params: List[Any] = [DECL_PENDING]
    if store_ids is not None:
        clause, ids = _store_in("store_id", store_ids)
        where.append(clause)
        params.extend(ids)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n, SUM(rebate_cents) AS cents
        FROM imei_decl WHERE {' AND '.join(where)}
        """,
        params,
    ).fetchone()
    n = int(row["n"] or 0)
    cents = int(row["cents"] or 0)
    return {"n": n, "cents": cents, "total": round(cents / 100.0, 2)}


def confirmed_totals(conn: sqlite3.Connection, start: date, end: date) -> Dict[str, Any]:
    """区间内已确认的笔数与快照金额（对账用，不读垫资表）。"""
    ensure(conn)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n, SUM(rebate_cents) AS cents
        FROM imei_decl
        WHERE state=? AND biz_date>=? AND biz_date<=?
        """,
        (DECL_CONFIRMED, start.isoformat(), end.isoformat()),
    ).fetchone()
    n = int(row["n"] or 0)
    cents = int(row["cents"] or 0)
    return {"n": n, "cents": cents, "total": round(cents / 100.0, 2)}


def deal_decl(conn: sqlite3.Connection, deal_id: int) -> Optional[sqlite3.Row]:
    """这张触客单当前挂着的有效声明（页面回显用）。"""
    ensure(conn)
    return conn.execute(
        "SELECT * FROM imei_decl WHERE deal_id=? AND state<>? ORDER BY id DESC LIMIT 1",
        (int(deal_id), DECL_VOID),
    ).fetchone()


def cents_text(cents: int) -> str:
    """分 → 元文本（有小数留两位，整数不留 .00）。"""
    value = int(cents or 0) / 100.0
    return f"{value:.0f}" if value % 1 == 0 else f"{value:.2f}"
