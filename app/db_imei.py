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
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .db_core import _now, begin_immediate
from .db_core import sql_in as _store_in

# 门店能自报的四种结论。前三种是「有答案」，待核是「我还得问」。
ACTIONS = ("已办", "已退货", "非本店", "待核")

# 「已兑现」认这四类业务（2026-11-08 用户拍板）：AI 手机合约、直降合约、顺差让利、送 2G 流量。
# 「其他」允许选，但必须写清是什么业务，核销时人工看说明，不自动算已兑现。
BIZ_TYPES = ("AI手机合约", "直降合约", "顺差让利", "送2G流量")
BIZ_OTHER = "其他"
BIZ_CHOICES = (*BIZ_TYPES, BIZ_OTHER)

# 这两类的系统记录不全：送2G流量（= 购机送12GB流量，2GB/月×6期）POS 一般不带类型，
# 顺差让利 POS 只能对上约 9%。查到就能对上，查不到也算正常 → 不能拿它判「不一致」，
# 而且门店自报时必须留下人工证据（受理单号或截图）。
SYSTEM_CHECKABLE = ("AI手机合约", "直降合约")
NO_SYSTEM_EVIDENCE = ("送2G流量", "顺差让利")

# POS / 台账那边的写法不一定跟下拉框一模一样，比对前先归一。
_BIZ_ALIAS = {
    "AI手机合约": "AI手机合约",
    "AI手机": "AI手机合约",
    "AI合约": "AI手机合约",
    "直降合约": "直降合约",
    "直降": "直降合约",
    "直降让利": "直降合约",
    "顺差让利": "顺差让利",
    "顺差": "顺差让利",
    "送2G流量": "送2G流量",
    "送2G": "送2G流量",
    "2G流量": "送2G流量",
    "其他": "其他",
}


def norm_user_type(value: str) -> str:
    """客户类型归一成三种；认不出返回空串（由调用方决定用默认值还是报错）。"""
    text = re.sub(r"\s+", "", str(value or "")).replace("（", "(").replace("）", ")")
    return _USER_ALIAS.get(text, "")


def _parse_day(value: str) -> Optional[date]:
    text = (value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _stage_of(age: int) -> str:
    if age < 0:
        return "D0-2"
    if age <= 2:
        return "D0-2"
    if age <= 6:
        return "D3-6"
    return "D7+"


def activation_state(user_type: str, manual_activated_at: str) -> str:
    """待激活 = 标了异地异网但还没点「已激活」；已激活 = 有激活日；否则常规。"""
    if (manual_activated_at or "").strip():
        return "已激活"
    if norm_user_type(user_type) == USER_REMOTE:
        return "待激活"
    return ""


def effective_days(
    row: Dict[str, Any],
) -> Tuple[int, int, str, str]:
    """(已售天数, 剩余天数, 阶段, 起算日) —— 有手动激活日就从那天重新起算 7 天。

    清单里的 age_days / remain_days 是台账按「售出日」算的；客户异地异网、
    机器晚激活时，那套数会提前把机器推成逾期。激活日一填，就以激活日为准。
    """
    state = activation_state(str(row.get("user_type") or ""), str(row.get("manual_activated_at") or ""))
    start = str(row.get("manual_activated_at") or "").strip()[:10]
    if state == "已激活":
        d0, d1 = _parse_day(start), _parse_day(str(row.get("asof") or ""))
        if d0 and d1:
            age = (d1 - d0).days
            remain = ACT_WINDOW_DAYS - age
            return age, remain, _stage_of(age), start
        return int(row.get("age_days") or 0), int(row.get("remain_days") or 0), str(row.get("stage") or ""), start
    age = int(row.get("age_days") or 0)
    remain = int(row.get("remain_days") or 0)
    stage = str(row.get("stage") or "") or _stage_of(age)
    if state == "待激活":
        return age, remain, "待激活", ""
    return age, remain, stage, str(row.get("sold_day") or "")


def norm_biz_type(value: str) -> str:
    """业务类型归一：去空格、全角括号转半角、别名归到标准写法，再比对。"""
    text = (value or "").strip().replace("（", "(").replace("）", ")")
    text = re.sub(r"[\s　]", "", text)
    return _BIZ_ALIAS.get(text, text)


def evidence_label(biz_type: str) -> str:
    """页面上给门店/管理员看的可信度：这两类只有门店自述，POS 记录不全。"""
    biz = norm_biz_type(biz_type)
    if not biz:
        return ""
    if biz in NO_SYSTEM_EVIDENCE:
        return "仅门店自报·POS记录不全"
    if biz == BIZ_OTHER:
        return "其他·人工核"
    return "系统可核"


def type_flag(biz_type: str, pos_type: str) -> str:
    """门店报的类型 vs POS 证据类型：只标注，不改变判定。

    送2G流量 / 顺差让利 的系统记录不全（POS 一般不带类型 / 只对得上约 9%），
    所以这两个只给两种结果：「一致」或「系统无记录（待核）」，绝不判「不一致」。
    注：一台机可能同时有别的合约，POS 里是另一种类型时，本类型仍算「系统无记录（待核）」。
    """
    pos = norm_biz_type(pos_type)
    biz = norm_biz_type(biz_type)
    if biz in NO_SYSTEM_EVIDENCE:
        return "一致" if pos and biz == pos else "系统无记录（待核）"
    if not pos:
        return "无POS类型（未比对）"
    if not biz:
        return "门店未报类型"
    if biz == BIZ_OTHER:
        return "其他·人工核"
    return "一致" if biz == pos else "不一致"


def redeem_state(action: str, biz_type: str) -> str:
    """核销结论：四类业务 + 已办 = 已兑现；其他/没填类型留给人工。"""
    if action != "已办":
        return action or "未报"
    if norm_biz_type(biz_type) in {norm_biz_type(t) for t in BIZ_TYPES}:
        return "已兑现"
    if norm_biz_type(biz_type) == BIZ_OTHER:
        return "待人工核（其他）"
    return "待人工核（未填类型）"

# 清单里这些状态不需要门店再动手：已经查到合约 / 已经退货。
CLOSED_STATES = ("已办", "已退货")

# 客户类型：门店提交时自己判断（2026-11-09 用户拍板「门店绝大部分能判断」）。
# 异地异网客户要等客户手动激活，所以这类机器在激活前不计逾期、不催办。
USER_TYPES = ("本地移动", "异地异网", "未知")
USER_DEFAULT = "本地移动"
USER_REMOTE = "异地异网"
# 标记类型（页面两个按钮 + 管理员的撤销）
MARKS = ("异网", "已激活", "撤销")
# 从「售出日」或「手动激活日」起算 7 天
ACT_WINDOW_DAYS = 7

_USER_ALIAS = {
    "异地异网": USER_REMOTE,
    "异网": USER_REMOTE,
    "异地": USER_REMOTE,
    "外地": USER_REMOTE,
    "异地用户": USER_REMOTE,
    "本地": USER_DEFAULT,
    "本地移动": USER_DEFAULT,
    "本地用户": USER_DEFAULT,
    "未知": "未知",
}

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
    pos_biz_type TEXT NOT NULL DEFAULT '',
    user_type TEXT NOT NULL DEFAULT '本地移动',
    manual_activated_at TEXT NOT NULL DEFAULT '',
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
    biz_type TEXT NOT NULL DEFAULT '',
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
    "pos_biz_type": "TEXT NOT NULL DEFAULT ''",
    "user_type": "TEXT NOT NULL DEFAULT '本地移动'",
    "manual_activated_at": "TEXT NOT NULL DEFAULT ''",
}

# 自报表也要能给老库补列（生产上表已经建了，新版加了 biz_type）
_REPORTS_ADDED_COLUMNS = {
    "biz_type": "TEXT NOT NULL DEFAULT ''",
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
    "pos_type": ("pos_biz_type", "POS业务类型", "业务类型", "合约类型", "合约名称"),
    "tag": ("串码标签", "标签"),
    "city": ("市", "城市"),
}


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for table, added in (
        ("imei_todo", _ADDED_COLUMNS),
        ("imei_reports", _REPORTS_ADDED_COLUMNS),
    ):
        have = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in added.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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
                stage, state, strength, evidence, pos_biz_type, asof, seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(imei) DO UPDATE SET
                status=excluded.status, store_id=excluded.store_id,
                sold_store=excluded.sold_store, own_store=excluded.own_store,
                dispatch_store=excluded.dispatch_store,
                model=excluded.model, sp=excluded.sp,
                sold_day=excluded.sold_day, age_days=excluded.age_days,
                remain_days=excluded.remain_days, stage=excluded.stage,
                state=excluded.state, strength=excluded.strength,
                evidence=excluded.evidence, pos_biz_type=excluded.pos_biz_type,
                asof=excluded.asof,
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
                _text(row.get(cols["pos_type"]), 40) if cols["pos_type"] else "",
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
            "biz_type": row["biz_type"],
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
               r.action AS report_action, r.biz_type AS report_biz_type,
               r.note AS report_note, r.photo AS report_photo,
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
    return enrich([dict(row) for row in rows])


def enrich(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给每行补三样页面要用的东西：待激活/已激活、按激活日重算的天数、是否逾期。

    排序跟着改：同一个店里，正常要办的排前面，待激活的沉到后面单独成组。
    """
    for row in rows:
        age, remain, stage, start = effective_days(row)
        row["act_state"] = activation_state(
            str(row.get("user_type") or ""), str(row.get("manual_activated_at") or "")
        )
        row["age_eff"] = age
        row["remain_eff"] = remain
        row["stage_eff"] = stage
        row["start_day"] = start
        row["overdue"] = row["act_state"] != "待激活" and remain < 0
    rows.sort(
        key=lambda r: (
            r["act_state"] == "待激活",
            r.get("store_id") is None,
            r.get("store_id") or 0,
            r["remain_eff"],
            str(r.get("imei") or ""),
        )
    )
    return rows


def mark_summary(conn: sqlite3.Connection, store_ids: Sequence[int]) -> List[Dict[str, Any]]:
    """各店标了多少台异地异网（管理员用：一家店全标异网就是在躲催办）。

    只统计还没关单的机器；`ratio` 是待激活占该店待办的百分比。
    """
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids:
        return []
    clause, params = _store_in("t.store_id", ids)
    rows = conn.execute(
        f"""
        SELECT t.store_id, s.short_name AS short, s.name AS name,
               COUNT(*) AS open_n,
               SUM(CASE WHEN t.user_type=? AND t.manual_activated_at='' THEN 1 ELSE 0 END) AS pending_n,
               SUM(CASE WHEN t.manual_activated_at<>'' THEN 1 ELSE 0 END) AS activated_n
        FROM imei_todo t
        LEFT JOIN stores s ON s.id = t.store_id
        WHERE t.seen=1 AND t.status=? AND t.state NOT IN ({','.join('?' * len(CLOSED_STATES))})
          AND {clause}
        GROUP BY t.store_id
        ORDER BY pending_n DESC, open_n DESC
        """,
        [USER_REMOTE, STATUS_SOLD, *CLOSED_STATES, *params],
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        open_n = int(item["open_n"] or 0)
        pending_n = int(item["pending_n"] or 0)
        item["open_n"] = open_n
        item["pending_n"] = pending_n
        item["activated_n"] = int(item["activated_n"] or 0)
        item["ratio"] = int(round(100.0 * pending_n / open_n)) if open_n else 0
        # 偏高的口子：3 台以上且过半标成待激活，值得问一句
        item["suspicious"] = pending_n >= 3 and open_n > 0 and pending_n * 2 >= open_n
        out.append(item)
    return out


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
    # 逾期/到期按「生效天数」算：填了手动激活日的，从激活日重新起算 7 天；
    # 标了异地异网还没激活的（待激活）单独计数，不计逾期、不算催办。
    row = conn.execute(
        f"""
        WITH base AS (
          SELECT t.store_id, t.user_type, t.manual_activated_at,
                 CASE WHEN t.manual_activated_at<>''
                           AND date(t.asof) IS NOT NULL
                           AND date(t.manual_activated_at) IS NOT NULL
                      THEN CAST(julianday(date(t.asof)) - julianday(date(t.manual_activated_at)) AS INTEGER)
                      ELSE NULL END AS age_act,
                 t.remain_days
          FROM imei_todo t
          WHERE t.seen=1 AND t.status=? AND t.state NOT IN ({",".join("?" * len(CLOSED_STATES))})
            AND {clause}
        )
        SELECT
          COUNT(*) AS open_n,
          SUM(CASE WHEN user_type=? AND manual_activated_at='' THEN 1 ELSE 0 END) AS pending_n,
          SUM(CASE WHEN manual_activated_at<>'' THEN 1 ELSE 0 END) AS activated_n,
          SUM(CASE WHEN NOT (user_type=? AND manual_activated_at='')
                    AND (CASE WHEN age_act IS NULL THEN remain_days ELSE ? - age_act END) < 0
                   THEN 1 ELSE 0 END) AS overdue_n,
          SUM(CASE WHEN NOT (user_type=? AND manual_activated_at='')
                    AND (CASE WHEN age_act IS NULL THEN remain_days ELSE ? - age_act END) BETWEEN 0 AND 6
                   THEN 1 ELSE 0 END) AS soon_n,
          COUNT(DISTINCT store_id) AS store_n
        FROM base
        """,
        [
            STATUS_SOLD,
            *CLOSED_STATES,
            *params,
            USER_REMOTE,
            USER_REMOTE,
            ACT_WINDOW_DAYS,
            USER_REMOTE,
            ACT_WINDOW_DAYS,
        ],
    ).fetchone()
    asof = conn.execute(
        f"SELECT MAX(t.asof) FROM imei_todo t WHERE {clause}", params
    ).fetchone()[0]
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
        "pending": int(row["pending_n"] or 0),
        "activated": int(row["activated_n"] or 0),
        "reported": int(reported or 0),
        "asof": (asof or "")[:10],
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
    biz_type: str = "",
    note: str = "",
    photo: str = "",
    user_id: Optional[int] = None,
) -> int:
    """门店自报一条（只增不改）。返回新记录 id。

    业务类型必填，只能是四类之一或「其他」；选「其他」必须写说明。
    """
    ensure(conn)
    if action not in ACTIONS:
        raise ValueError("结论只能是「已办 / 已退货 / 非本店 / 待核」")
    biz = norm_biz_type(biz_type)
    if not biz:
        raise ValueError("业务类型要选一个（AI手机合约 / 直降合约 / 顺差让利 / 送2G流量 / 其他）")
    if biz not in BIZ_CHOICES:
        raise ValueError("业务类型只能选 AI手机合约 / 直降合约 / 顺差让利 / 送2G流量 / 其他")
    if biz == BIZ_OTHER and not (note or "").strip():
        raise ValueError("选「其他」要在说明里写清是什么业务")
    if biz in NO_SYSTEM_EVIDENCE and not (note or "").strip() and not photo:
        raise ValueError(
            f"「{biz}」这类业务系统记录不全，查到就能对上，查不到也算正常，"
            "请务必上传受理单或截图（说明、截图任填一项）"
        )
    begin_immediate(conn)
    cur = conn.execute(
        """
        INSERT INTO imei_reports(imei, store_id, action, biz_type, note, photo, user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (imei, store_id, action, biz, _text(note, 200), photo, user_id, _now()),
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


def set_mark(
    conn: sqlite3.Connection,
    *,
    imei: str,
    mark: str,
    day: str = "",
    store_ids: Sequence[int] = (),
) -> Dict[str, Any]:
    """门店的两个动作 + 管理员的撤销。

    - mark='异网'：客户是异地异网，要等他自己激活 → 转「待激活」，不计逾期、不催办
    - mark='已激活'：客户已经激活 → 记下激活日，7 天从这一天重新起算
    - mark='撤销'：标错了 → 回到「本地移动」，按售出日算

    只能动自己看得见的店；待建店（store_id 为空）不许标——没有店就没有责任人。
    """
    if mark not in MARKS:
        raise ValueError("动作只能是「异网」「已激活」「撤销」")
    ensure(conn)
    row = conn.execute(
        "SELECT imei, store_id, sold_day, status FROM imei_todo WHERE imei=?", (imei,)
    ).fetchone()
    if row is None:
        raise ValueError("这条串码不在清单里，刷新后再试")
    if row["store_id"] is None:
        raise ValueError("这家店还没建档案，先补门店档案再标")
    allowed = [int(s) for s in store_ids]
    if allowed and int(row["store_id"]) not in allowed:
        raise PermissionError("forbidden")
    now = _now()
    if mark == "异网":
        conn.execute(
            "UPDATE imei_todo SET user_type=?, manual_activated_at='', updated_at=? WHERE imei=?",
            (USER_REMOTE, now, imei),
        )
        return {"imei": imei, "mark": mark, "user_type": USER_REMOTE, "manual_activated_at": ""}
    if mark == "撤销":
        conn.execute(
            "UPDATE imei_todo SET user_type=?, manual_activated_at='', updated_at=? WHERE imei=?",
            (USER_DEFAULT, now, imei),
        )
        return {"imei": imei, "mark": mark, "user_type": USER_DEFAULT, "manual_activated_at": ""}
    # 已激活：激活日默认今天，不能是未来、不能早于售出日
    raw = (day or "").strip()[:10]
    act = _parse_day(raw) if raw else date.today()
    if act is None:
        raise ValueError("激活日要填成 2026-11-09 这样的日期")
    if raw and act > date.today():
        raise ValueError("激活日不能填将来的日期")
    sold = _parse_day(str(row["sold_day"] or ""))
    if sold and act < sold:
        raise ValueError("激活日不能早于售出日")
    conn.execute(
        "UPDATE imei_todo SET manual_activated_at=?, updated_at=? WHERE imei=?",
        (act.isoformat(), now, imei),
    )
    return {"imei": imei, "mark": mark, "user_type": "", "manual_activated_at": act.isoformat()}


def export_todo(
    conn: sqlite3.Connection, store_ids: Sequence[int], *, include_unmapped: bool = False
) -> List[Dict[str, Any]]:
    """导出待办清单（含客户类型和手动激活日），给台账回读门店的标记。

    台账那边按 imei 对：user_type=异地异网 且 manual_activated_at 为空 = 待激活，
    不计逾期；有激活日的按激活日重算 7 天。导出不改库，可以反复导。
    """
    ids = [int(s) for s in store_ids]
    rows = todo_rows(conn, ids, show="all", include_unmapped=include_unmapped)
    for row in rows:
        row["act_state"] = activation_state(
            str(row.get("user_type") or ""), str(row.get("manual_activated_at") or "")
        )
    return rows


def export_reports(conn: sqlite3.Connection, store_ids: Sequence[int]) -> List[sqlite3.Row]:
    """导出自报记录（含已办、待核、非本店），给台账回灌用。

    台账那边按 imei 覆盖结论：门店说「已办」就进已办，「非本店」就换归属，
    「待核」保持待办。导出不改库，可以反复导。

    带业务类型，并且跟清单里 POS 证据类型（`imei_todo.pos_biz_type`）对一下：
    不一致只在导出里标出来，**不改变「已兑现」判定**（四类业务 + 已办 = 已兑现）。
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
            SELECT r.imei, s.name AS store_name, r.action, r.biz_type,
                   COALESCE(t.pos_biz_type, '') AS pos_type,
                   COALESCE(t.user_type, '') AS user_type,
                   COALESCE(t.manual_activated_at, '') AS manual_activated_at,
                   r.note, r.photo, r.created_at,
                   COALESCE(u.display_name, u.username, '') AS reporter
            FROM imei_reports r
            JOIN stores s ON s.id = r.store_id
            LEFT JOIN imei_todo t ON t.imei = r.imei
            LEFT JOIN users u ON u.id = r.user_id
            WHERE {clause}
            ORDER BY r.id
            """,
            params,
        )
    )
