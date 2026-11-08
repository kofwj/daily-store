"""顺差串码待办数据层：导入的待办快照 + 门店自报（只增不改）。

为什么单独两张表：
- `imei_todo` 只存「后台导出的一份清单」的最新一次样子（按 IMEI 覆盖，不删行），
  门店看到的是清单，不是系统算出来的结论。清单外的新机器由下一次导入带进来。
- `imei_reports` 只追加，不更新不删除：门店点「已办 / 已退货 / 非本店 / 待核」
  和截图都落在这里，谁、什么时候、写了什么，一条不改。台账要能回头算，
  所以宁可多一条记录，也不覆盖上一条。

派活口径（2026-11-08 用户拍板）：
- 只做南通：泰州不参与，导入时就丢掉（`SCOPE_CITY`）。
- 南通是共享池，串码会跨店调拨，所以不能按「库存登记门店」派活：
  * 已售（有 POS 销售记录）→ 派给 **POS 销售门店**
  * 未售（还在库存里）  → 派给 **库存登记门店**（谁持有谁负责）
  * `dispatch_store` 就是这一条规则算出来的结果，同时保留 sold_store / own_store 原样，
    以后要改口径不用重新导数据。
- 门店档案里没有的店（在营但没开账号）：行照样入库，`store_id` 留空，
  只在管理员视图里出现（待建店），绝不静默归到第一家店。

两张表都懒建（CREATE TABLE IF NOT EXISTS），不写迁移：与 aux_queries 同款做法，
表很薄、没历史数据要搬，放进 db_schema.SCHEMA 也行，放这里省一次改动。
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .db_core import _now, begin_immediate
from .db_core import sql_in as _store_in

# 门店能自报的四种结论。前三种是「有答案」，待核是「我还得问」。
ACTIONS = ("已办", "已退货", "非本店", "待核")

# 清单里这些状态不需要门店再动手：已经查到合约 / 已经退货。
CLOSED_STATES = ("已办", "已退货")

# 第一阶段只做南通：泰州不参与，导入时就丢掉。
# 写成「排除泰州」而不是「只要南通市」，这样示例店（示例市）和以后新开的城市不会被误杀；
# 要加排除城市就设环境变量 STORE_DAILY_IMEI_OUT_CITIES=泰州市,某某市。
SCOPE_CITY = "南通市"
OUT_CITIES = tuple(
    x.strip()
    for x in (os.environ.get("STORE_DAILY_IMEI_OUT_CITIES") or "泰州市").split(",")
    if x.strip()
)

# 串码状态
STATUS_SOLD = "已售"
STATUS_STOCK = "未售"

# 账上店名 → 门店档案店名（改名 / 别名）。
# 空着是故意的：改名要人确认，代码不替人猜（见「待确认映射表」）。
# 填法：{"TZ南通某某vivo体验店": "TZ南通现有店vivo体验店"}
ALIASES: Dict[str, str] = {}

# 对不上档案时用来判「是不是泰州」的店名特征（南通店名里不会出现这些）。
_OUT_OF_SCOPE_HINT = (
    "泰州", "泰兴", "兴化", "靖江", "姜堰", "海陵", "高港", "黄桥", "戴南", "口岸",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS imei_todo (
    imei TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT '已售',
    store_id INTEGER,
    sold_store TEXT NOT NULL DEFAULT '',
    own_store TEXT NOT NULL DEFAULT '',
    dispatch_store TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    sp TEXT NOT NULL DEFAULT '',
    sold_day TEXT NOT NULL DEFAULT '',
    age_days INTEGER NOT NULL DEFAULT 0,
    remain_days INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    strength TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    asof TEXT NOT NULL DEFAULT '',
    seen INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imei_todo_store ON imei_todo(store_id, remain_days);
CREATE INDEX IF NOT EXISTS idx_imei_todo_status ON imei_todo(status, seen);
CREATE TABLE IF NOT EXISTS imei_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imei TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    action TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    photo TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imei_reports_imei ON imei_reports(imei, id DESC);
CREATE INDEX IF NOT EXISTS idx_imei_reports_store ON imei_reports(store_id, id DESC);
"""

# 老库上补列（表是懒建的，之前跑过的库没有这些列）
_ADDED_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT '已售'",
    "sold_store": "TEXT NOT NULL DEFAULT ''",
    "own_store": "TEXT NOT NULL DEFAULT ''",
    "dispatch_store": "TEXT NOT NULL DEFAULT ''",
}

_COLUMNS = {
    "imei": ("imei", "串码", "IMEI"),
    "store": ("store", "门店", "门店全称", "销售门店", "零售商名称"),
    "own_store": ("own_store", "库存登记门店", "库存门店"),
    "model": ("model", "机型", "商品(SKU)"),
    "sp": ("sp", "营业员", "顾问"),
    "day": ("day", "sold_day", "售出日期", "开单日期", "库存日期"),
    "age": ("age", "age_days", "已售天数"),
    "remain": ("remain", "remain_days", "剩余天数"),
    "stage": ("stage", "阶段"),
    "state": ("state", "状态"),
    "strength": ("strength", "证据强度"),
    "evidence": ("evidence", "证据"),
    "tag": ("串码标签", "标签"),
    "city": ("市", "城市"),
}


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    have = {str(r[1]) for r in conn.execute("PRAGMA table_info(imei_todo)")}
    for name, decl in _ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE imei_todo ADD COLUMN {name} {decl}")


def _ensure(conn: sqlite3.Connection) -> None:
    ensure(conn)


def _int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _text(value: Any, limit: int = 120) -> str:
    return (str(value or "").strip())[:limit]


def _norm_store_name(name: str) -> str:
    """门店名归一：去掉「TZ / 市 / vivo体验店 / 专卖店 / 空格」，只留能对上的字。

    清单里的店名来自 POS 与库存导出，跟门店档案不是同一套写法（多一个「市」、
    少一个「vivo体验店」都很常见），所以先精确、再归一，最后才判「对不上」。
    """
    text = (name or "").strip().upper()
    text = text.replace("TZ", "")
    for token in ("VIVO体验店", "VIVO专卖店", "体验店", "专卖店", "旗舰店", " ", "　"):
        text = text.replace(token, "")
    text = text.replace("市辖区", "").replace("市", "")
    return re.sub(r"[\s\-_()（）]", "", text)


def store_index(conn: sqlite3.Connection) -> Dict[str, int]:
    """门店档案的三种键：全称、简称、归一后的名字 → store_id。"""
    index: Dict[str, int] = {}
    for row in conn.execute("SELECT id, name, short_name FROM stores"):
        for key in (
            (row["name"] or "").strip(),
            (row["short_name"] or "").strip(),
            _norm_store_name(row["name"] or ""),
        ):
            if key:
                index.setdefault(key, int(row["id"]))
    return index


def _store_cities(conn: sqlite3.Connection) -> Dict[int, str]:
    return {int(r["id"]): (r["city"] or "") for r in conn.execute("SELECT id, city FROM stores")}


def _looks_out_of_scope(name: str, city: str = "") -> bool:
    """泰州（非南通）判定：先看门店档案的 city，档案里没有才看店名特征。"""
    if city:
        return city in OUT_CITIES
    return any(hint in (name or "") for hint in _OUT_OF_SCOPE_HINT)


def _pick(header: Sequence[str], names: Sequence[str]) -> Optional[str]:
    """按候选列名找真实列名（容忍 BOM / 空格 / 大小写）。"""
    normalized = {(h or "").strip().lower(): h for h in header}
    for name in names:
        hit = normalized.get(name.lower())
        if hit is not None:
            return hit
    return None


def _resolve_store(
    conn: sqlite3.Connection,
    name: str,
    index: Dict[str, int],
    cities: Dict[int, str],
) -> Tuple[Optional[int], str]:
    """店名 → (store_id, city)。对不上返回 (None, "")，交给调用方决定怎么报。"""
    raw = (name or "").strip()
    key = ALIASES.get(raw, raw)
    store_id = index.get(key) or index.get(_norm_store_name(key))
    if store_id is None and key != raw:
        store_id = index.get(raw) or index.get(_norm_store_name(raw))
    return store_id, (cities.get(store_id, "") if store_id else "")


def import_rows(
    conn: sqlite3.Connection,
    data: bytes,
    asof: str = "",
    *,
    mark_seen: bool = True,
) -> Dict[str, Any]:
    """导入一份 CSV：台账（已售）或库存明细（未售）都认，按 IMEI 覆盖，不删行。

    台账模式：`store` = POS 销售门店 → 状态已售，派给销售店。
    库存模式：有「串码标签」列，只收 移动顺差 → 状态未售，派给库存登记店。
    泰州行直接跳过（不计入库数，单独回报）。
    """
    ensure(conn)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("gbk", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    cols = {key: _pick(header, names) for key, names in _COLUMNS.items()}
    if not cols["imei"]:
        raise ValueError("CSV 里找不到 imei 列")
    stock_mode = bool(cols["tag"])
    mode_status = STATUS_STOCK if stock_mode else STATUS_SOLD

    index = store_index(conn)
    cities = _store_cities(conn)
    now = _now()
    rows_total = 0
    written = 0
    bad_imei = 0
    skipped_city = 0
    unmapped: Dict[str, int] = {}
    stamp = (asof or "").strip() or now[:10]
    begin_immediate(conn)
    if mark_seen:
        # 先把这一类标旧（已售/未售各导各的，互不覆盖），再把自己见过的标回来：
        # 清单里消失的行不删，只是不再推给门店。
        conn.execute("UPDATE imei_todo SET seen=0 WHERE status=?", (mode_status,))
    for row in reader:
        imei = _text(row.get(cols["imei"]), 32)
        if not imei:
            bad_imei += 1
            continue
        if stock_mode and _text(row.get(cols["tag"]), 20) != "移动顺差":
            continue
        rows_total += 1

        raw_store = _text(row.get(cols["store"]), 80) if cols["store"] else ""
        raw_own = _text(row.get(cols["own_store"]), 80) if cols["own_store"] else ""
        city_hint = _text(row.get(cols["city"]), 20) if cols["city"] else ""
        if stock_mode:
            status, sold_store, own_store = STATUS_STOCK, "", raw_store
        else:
            status, sold_store, own_store = STATUS_SOLD, raw_store, raw_own

        # 派活门店：已售按销售店，未售按库存登记店（南通共享池，认持有不认登记）
        dispatch = sold_store or own_store
        store_id, city = _resolve_store(conn, dispatch, index, cities)
        if not city:
            city = city_hint
        if _looks_out_of_scope(dispatch, city):
            skipped_city += 1
            continue
        if not store_id:
            unmapped[dispatch or "(空)"] = unmapped.get(dispatch or "(空)", 0) + 1

        conn.execute(
            """
            INSERT INTO imei_todo(
                imei, status, store_id, sold_store, own_store, dispatch_store,
                model, sp, sold_day, age_days, remain_days,
                stage, state, strength, evidence, asof, seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(imei) DO UPDATE SET
                status=excluded.status, store_id=excluded.store_id,
                sold_store=excluded.sold_store, own_store=excluded.own_store,
                dispatch_store=excluded.dispatch_store,
                model=excluded.model, sp=excluded.sp,
                sold_day=excluded.sold_day, age_days=excluded.age_days,
                remain_days=excluded.remain_days, stage=excluded.stage,
                state=excluded.state, strength=excluded.strength,
                evidence=excluded.evidence, asof=excluded.asof,
                seen=1, updated_at=excluded.updated_at
            """,
            (
                imei,
                status,
                store_id,
                sold_store,
                own_store,
                dispatch,
                _text(row.get(cols["model"])) if cols["model"] else "",
                _text(row.get(cols["sp"]), 40) if cols["sp"] else "",
                _text(row.get(cols["day"]), 20) if cols["day"] else "",
                _int(row.get(cols["age"])) if cols["age"] else 0,
                _int(row.get(cols["remain"])) if cols["remain"] else 0,
                _text(row.get(cols["stage"]), 20) if cols["stage"] else "",
                _text(row.get(cols["state"]), 20) if cols["state"] else "",
                _text(row.get(cols["strength"]), 20) if cols["strength"] else "",
                _text(row.get(cols["evidence"]), 200) if cols["evidence"] else "",
                stamp,
                now,
            ),
        )
        written += 1
    return {
        "rows": rows_total,
        "written": written,
        "bad_imei": bad_imei,
        "skipped_city": skipped_city,
        "unmatched": sorted(unmapped.items(), key=lambda kv: -kv[1]),
        "asof": stamp,
        "mode": "stock" if stock_mode else "ledger",
    }


def latest_reports(
    conn: sqlite3.Connection, imeis: Sequence[str]
) -> Dict[str, Dict[str, Any]]:
    """这批 IMEI 各自最后一条自报（一条 SQL 取最大 id，不逐条查）。"""
    ids = [str(i) for i in imeis if i]
    if not ids:
        return {}
    clause, params = sql_in_text(ids)
    rows = conn.execute(
        f"""
        SELECT r.*, u.display_name AS reporter
        FROM imei_reports r
        LEFT JOIN users u ON u.id = r.user_id
        WHERE r.id IN (SELECT MAX(id) FROM imei_reports WHERE imei IN ({clause}) GROUP BY imei)
        """,
        params,
    )
    return {
        str(row["imei"]): {
            "action": row["action"],
            "note": row["note"],
            "photo": row["photo"],
            "reporter": row["reporter"] or "",
            "at": (row["created_at"] or "")[:16],
        }
        for row in rows
    }


def sql_in_text(values: Sequence[str]):
    """占位符串 + 参数（imei 是文本，不走 db_core.sql_in 的整型假定）。"""
    return ",".join("?" * len(values)), list(values)


def todo_rows(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
    *,
    show: str = "open",
    include_unmapped: bool = False,
) -> List[Dict[str, Any]]:
    """可见门店的待办行（已售机器），带最新自报。show=open 只给还要动手的。

    `include_unmapped` 给管理员：档案里没有的店那部分机器也一起看。
    排序：逾期在前（remain 越小越靠前），同一天里 IMEI 稳定的顺序。
    """
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids and not include_unmapped:
        return []
    where: List[str] = ["t.seen=1", "t.status='%s'" % STATUS_SOLD]
    params: List[Any] = []
    if show != "all":
        marks = ",".join("?" * len(CLOSED_STATES))
        where.append(f"t.state NOT IN ({marks}) AND COALESCE(r.action,'') NOT IN ('已办','已退货')")
        params = [*CLOSED_STATES, *params]
    if ids:
        clause, raw_params = _store_in("t.store_id", ids)
        params = [*params, *raw_params]
        where.append(f"({clause} OR t.store_id IS NULL)" if include_unmapped else clause)
    rows = conn.execute(
        f"""
        SELECT t.*, s.name AS store_name, s.short_name AS store_short,
               r.action AS report_action, r.note AS report_note, r.photo AS report_photo,
               r.created_at AS report_at, u.display_name AS reporter
        FROM imei_todo t
        LEFT JOIN stores s ON s.id = t.store_id
        LEFT JOIN imei_reports r ON r.id = (
            SELECT MAX(id) FROM imei_reports WHERE imei = t.imei
        )
        LEFT JOIN users u ON u.id = r.user_id
        WHERE {' AND '.join(where)}
        ORDER BY (t.store_id IS NULL), t.store_id, t.remain_days, t.imei
        """,
        params,
    )
    return [dict(row) for row in rows]


def pool_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """池子按库存门店汇总：店名、台数、前三个机型（手机弱网别一上来铺 968 行）。"""
    ensure(conn)
    rows = list(
        conn.execute(
            """
            SELECT t.own_store, COUNT(*) AS n,
                   COUNT(DISTINCT CASE WHEN t.model<>'' THEN t.model END) AS models
            FROM imei_todo t
            WHERE t.seen=1 AND t.status=?
            GROUP BY t.own_store ORDER BY n DESC
            """,
            (STATUS_STOCK,),
        )
    )
    models: Dict[str, List[Dict[str, Any]]] = {}
    for row in conn.execute(
        """
        SELECT own_store, model, COUNT(*) AS n FROM imei_todo
        WHERE seen=1 AND status=? GROUP BY own_store, model ORDER BY own_store, n DESC
        """,
        (STATUS_STOCK,),
    ):
        bucket = models.setdefault(row["own_store"] or "", [])
        if len(bucket) < 3:
            bucket.append({"model": row["model"] or "未登记", "n": row["n"]})
    return [
        {
            "own_store": row["own_store"],
            "n": row["n"],
            "models": models.get(row["own_store"] or "", []),
        }
        for row in rows
    ]


def pool_rows(
    conn: sqlite3.Connection, limit: int = 0, own_store: str = ""
) -> List[Dict[str, Any]]:
    """南通池子里还没卖的顺差机器（第二阶段「可用串码查询」的口子）。

    这条查询**不按门店过滤**：池子是共享的，A 店可以卖 B 店库存的机器，
    所以谁登录都能看到全池；真要调拨还是走线下审批，这里只回答「池子里有什么」。
    """
    ensure(conn)
    sql = """
        SELECT t.imei, t.model, t.own_store, t.dispatch_store, t.store_id,
               t.sold_day, t.asof, s.name AS store_name, s.short_name AS store_short
        FROM imei_todo t
        LEFT JOIN stores s ON s.id = t.store_id
        WHERE t.seen=1 AND t.status=?
        ORDER BY t.own_store, t.model, t.imei
    """
    params: List[Any] = [STATUS_STOCK]
    if own_store:
        sql = sql.replace("ORDER BY", "AND t.own_store=? ORDER BY")
        params.append(own_store)
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params)]


def summary(conn: sqlite3.Connection, store_ids: Sequence[int]) -> Dict[str, Any]:
    """页头那几个数：在跟、逾期、本周内到期、门店已自报、待建店、池子、数据截至。"""
    ensure(conn)
    ids = [int(s) for s in store_ids]
    clause, raw_params = _store_in("t.store_id", ids) if ids else ("1=0", [])
    params: List[Any] = list(raw_params)
    row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS open_n,
          SUM(CASE WHEN t.remain_days < 0 THEN 1 ELSE 0 END) AS overdue_n,
          SUM(CASE WHEN t.remain_days BETWEEN 0 AND 6 THEN 1 ELSE 0 END) AS soon_n,
          MAX(t.asof) AS asof,
          COUNT(DISTINCT t.store_id) AS store_n
        FROM imei_todo t
        WHERE t.seen=1 AND t.status=? AND t.state NOT IN ({",".join("?" * len(CLOSED_STATES))})
          AND {clause}
        """,
        [STATUS_SOLD, *CLOSED_STATES, *params],
    ).fetchone()
    reported = conn.execute(
        f"""
        SELECT COUNT(DISTINCT r.imei) FROM imei_reports r
        JOIN imei_todo t ON t.imei = r.imei
        WHERE {clause}
        """,
        params,
    ).fetchone()[0]
    unmapped = 0
    if ids:
        unmapped = conn.execute(
            """
            SELECT COUNT(*) FROM imei_todo t
            WHERE t.seen=1 AND t.status=? AND t.store_id IS NULL
              AND t.state NOT IN (%s)
            """ % ",".join("?" * len(CLOSED_STATES)),
            [STATUS_SOLD, *CLOSED_STATES],
        ).fetchone()[0]
    pool = conn.execute(
        "SELECT COUNT(*) FROM imei_todo WHERE seen=1 AND status=?", (STATUS_STOCK,)
    ).fetchone()[0]
    return {
        "open": int(row["open_n"] or 0),
        "overdue": int(row["overdue_n"] or 0),
        "soon": int(row["soon_n"] or 0),
        "reported": int(reported or 0),
        "asof": (row["asof"] or "")[:10],
        "stores": int(row["store_n"] or 0),
        "unmapped": int(unmapped or 0),
        "pool": int(pool or 0),
    }


def add_report(
    conn: sqlite3.Connection,
    *,
    imei: str,
    store_id: int,
    action: str,
    note: str = "",
    photo: str = "",
    user_id: Optional[int] = None,
) -> int:
    """门店自报一条（只增不改）。返回新记录 id。"""
    ensure(conn)
    if action not in ACTIONS:
        raise ValueError("结论只能是「已办 / 已退货 / 非本店 / 待核」")
    begin_immediate(conn)
    cur = conn.execute(
        """
        INSERT INTO imei_reports(imei, store_id, action, note, photo, user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (imei, store_id, action, _text(note, 200), photo, user_id, _now()),
    )
    return int(cur.lastrowid or 0)


def photo_store(conn: sqlite3.Connection, name: str) -> Optional[int]:
    """截图文件名 → 落在哪家店。看图的人必须能看那家店，别拿链接猜别家清单。"""
    row = conn.execute(
        "SELECT store_id FROM imei_reports WHERE photo=? ORDER BY id DESC LIMIT 1", (name,)
    ).fetchone()
    return int(row["store_id"]) if row else None


def todo_stores(conn: sqlite3.Connection, store_ids: Iterable[int]) -> List[sqlite3.Row]:
    """可见门店里有待办的那些（页面上按店分组，没待办的店不占地方）。"""
    ids = [int(s) for s in store_ids]
    if not ids:
        return []
    clause, raw_params = _store_in("t.store_id", ids)
    params: List[Any] = list(raw_params)
    return list(
        conn.execute(
            f"""
            SELECT s.id, s.name, s.short_name, COUNT(*) AS n
            FROM imei_todo t JOIN stores s ON s.id = t.store_id
            WHERE t.seen=1 AND t.status=?
              AND t.state NOT IN ({','.join('?' * len(CLOSED_STATES))}) AND {clause}
            GROUP BY s.id ORDER BY n DESC
            """,
            [STATUS_SOLD, *CLOSED_STATES, *params],
        )
    )


def export_reports(conn: sqlite3.Connection, store_ids: Sequence[int]) -> List[sqlite3.Row]:
    """导出自报记录（含已办、待核、非本店），给台账回灌用。

    台账那边按 imei 覆盖结论：门店说「已办」就进已办，「非本店」就换归属，
    「待核」保持待办。导出不改库，可以反复导。
    """
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids:
        return []
    clause, raw_params = _store_in("r.store_id", ids)
    params: List[Any] = list(raw_params)
    return list(
        conn.execute(
            f"""
            SELECT r.imei, s.name AS store_name, r.action, r.note, r.photo, r.created_at,
                   COALESCE(u.display_name, u.username, '') AS reporter
            FROM imei_reports r
            JOIN stores s ON s.id = r.store_id
            LEFT JOIN users u ON u.id = r.user_id
            WHERE {clause}
            ORDER BY r.id
            """,
            params,
        )
    )
