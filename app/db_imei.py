"""顺差串码待办数据层：导入的待办快照 + 门店自报（只增不改）。

为什么单独两张表：
- `imei_todo` 只存「后台导出的一份清单」的最新一次样子（按 IMEI 覆盖，不删行），
  门店看到的是清单，不是系统算出来的结论。清单外的新机器由下一次导入带进来。
- `imei_reports` 只追加，不更新不删除：门店点「已办 / 已退货 / 待核」和截图
  都落在这里，谁、什么时候、写了什么，一条不改。台账要能回头算，
  所以宁可多一条记录，也不覆盖上一条。历史上报过的「非本店」照原样显示、导出，
  但 2026-11-09 起不再接受新报（用户拍板）。

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

from .db_core import _now, begin_immediate, today_local
from .db_core import sql_in as _store_in
from .db_decl import ensure as _ensure_decl
from .db_decl import no_live_decl_sql

# 门店能自报的三种结论。前两种是「有答案」，待核是「我还得问」。
# 「非本店」2026-11-09 用户拍板下线：新报不接受，老记录仍原样显示/导出。
ACTIONS = ("已办", "已退货", "待核")

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
    """(已售天数, 剩余天数, 阶段, 起算日)。

    两套口径，由导入批次 `rule_ver` 决定：
    - 1 = 2026-11-09 之前的存量：台账给的 remain_days 原样用，不按今天重算，
      所以 91 台不会在升级当天突然从「正常」跳成「逾期」，统一按 11-16 宽限看。
    - 2 = 新导入：从起算日（手动激活日优先，否则售出日）到**今天**实时算 7 天。

    手动激活日对两套口径都优先：门店已经用改串机激活了，就从激活日重新起算，
    不能还拿台账按售出日算的旧数压着。
    """
    state = activation_state(str(row.get("user_type") or ""), str(row.get("manual_activated_at") or ""))
    start = str(row.get("manual_activated_at") or "").strip()[:10]
    today = today_local()
    if state == "已激活":
        d0 = _parse_day(start)
        if d0:
            age = max(0, (today - d0).days)
            remain = ACT_WINDOW_DAYS - age
            return age, remain, _stage_of(age), start
        return int(row.get("age_days") or 0), int(row.get("remain_days") or 0), str(row.get("stage") or ""), start

    try:
        rule_ver = int(row.get("rule_ver") or 1)
    except (TypeError, ValueError):
        rule_ver = 1
    if rule_ver <= 1:
        age = int(row.get("age_days") or 0)
        remain = int(row.get("remain_days") or 0)
        stage = str(row.get("stage") or "") or _stage_of(age)
        if state == "待激活":
            return age, remain, "待激活", ""
        return age, remain, stage, str(row.get("sold_day") or "")

    sold = _parse_day(str(row.get("sold_day") or ""))
    if sold:
        age = max(0, (today - sold).days)
        remain = ACT_WINDOW_DAYS - age
        if state == "待激活":
            return age, remain, "待激活", ""
        return age, remain, _stage_of(age), sold.isoformat()
    # 没有售出日就没法实时算，退回清单里的数，至少不崩
    age = int(row.get("age_days") or 0)
    remain = int(row.get("remain_days") or 0)
    return age, remain, ("待激活" if state == "待激活" else str(row.get("stage") or "") or _stage_of(age)), ""

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
# 异地异网号码要等门店用改串机激活，所以这类机器在激活前不计逾期、不催办。
USER_TYPES = ("本地移动", "异地异网", "未知")
USER_DEFAULT = "本地移动"
USER_REMOTE = "异地异网"
# 标记类型（页面两个按钮 + 管理员的撤销）
MARKS = ("异网", "已激活", "撤销")
# 从「售出日」或「手动激活日」起算 7 天
ACT_WINDOW_DAYS = 7
# 异地异网一直没激活的，超过这么多天进管理员核实清单（门店端只显示，不再催）
REVIEW_DAYS = 30
# 11-16 软失效宽限截止日（CST）：过了这天仍未办、非异地异网、不在 7 天激活窗口的
# 存量才标「宽限已过」。改期改这里或环境变量 STORE_DAILY_IMEI_GRACE_END，别写死在模板。
IMEI_GRACE_END = os.environ.get("STORE_DAILY_IMEI_GRACE_END", "2026-11-16")

# 管理员手动结转（V0.3.69）：只往 imei_carry_log 追加审计快照，
# **不改** imei_todo 的 state / seen / remain_days / rule_ver，也不删行；
# 结转后 grace 谓词排除该行（门店仍可补报）；撤销 = 再追加一条「撤销」，历史不删。
CARRY_ACTIONS = ("结转", "撤销")

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

_SCHEMA_STMTS = (
    """
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
        data_flag TEXT NOT NULL DEFAULT '',
        user_type TEXT NOT NULL DEFAULT '本地移动',
        manual_activated_at TEXT NOT NULL DEFAULT '',
        asof TEXT NOT NULL DEFAULT '',
        rule_ver INTEGER NOT NULL DEFAULT 1,
        seen INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_imei_todo_store ON imei_todo(store_id, remain_days)",
    "CREATE INDEX IF NOT EXISTS idx_imei_todo_status ON imei_todo(status, seen)",
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_imei_reports_imei ON imei_reports(imei, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_imei_reports_store ON imei_reports(store_id, id DESC)",
    """
    CREATE TABLE IF NOT EXISTS imei_mark_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        imei TEXT NOT NULL,
        store_id INTEGER,
        user_id INTEGER,
        action TEXT NOT NULL,
        old_user_type TEXT NOT NULL DEFAULT '',
        old_activated_at TEXT NOT NULL DEFAULT '',
        new_user_type TEXT NOT NULL DEFAULT '',
        new_activated_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_imei_mark_log_imei ON imei_mark_log(imei, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_imei_mark_log_store ON imei_mark_log(store_id, id DESC)",
    """
    CREATE TABLE IF NOT EXISTS imei_carry_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        imei TEXT NOT NULL,
        store_id INTEGER,
        user_id INTEGER,
        action TEXT NOT NULL,
        store_name TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        sold_day TEXT NOT NULL DEFAULT '',
        remain_days INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT '',
        user_type TEXT NOT NULL DEFAULT '',
        manual_activated_at TEXT NOT NULL DEFAULT '',
        data_flag TEXT NOT NULL DEFAULT '',
        rule_ver INTEGER NOT NULL DEFAULT 1,
        seen INTEGER NOT NULL DEFAULT 1,
        asof TEXT NOT NULL DEFAULT '',
        report_action TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_imei_carry_log_imei ON imei_carry_log(imei, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_imei_carry_log_store ON imei_carry_log(store_id, id DESC)",
    """
    CREATE TABLE IF NOT EXISTS imei_pool_lock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        imei TEXT NOT NULL,
        claim_store_id INTEGER NOT NULL REFERENCES stores(id),
        claim_user_id INTEGER,
        report_id INTEGER,
        action TEXT NOT NULL DEFAULT '',
        locked_at TEXT NOT NULL,
        released_at TEXT,
        released_by INTEGER
    )
    """,
    # 一台机器同时只能有**一把**未释放的锁；释放后允许重新抢（历史行全部留着，释放不删行）
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_imei_pool_lock_open ON imei_pool_lock(imei) WHERE released_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_imei_pool_lock_store ON imei_pool_lock(claim_store_id, released_at)",
)

# ensure() 的快速路径要认全这些对象；新增审计表时同步加进来，否则老库会漏建。
_SCHEMA_OBJECTS = (
    "imei_todo",
    "imei_reports",
    "imei_mark_log",
    "idx_imei_todo_store",
    "idx_imei_todo_status",
    "idx_imei_reports_imei",
    "idx_imei_reports_store",
    "idx_imei_mark_log_imei",
    "idx_imei_mark_log_store",
    "imei_carry_log",
    "idx_imei_carry_log_imei",
    "idx_imei_carry_log_store",
    "imei_pool_lock",
    "uniq_imei_pool_lock_open",
    "idx_imei_pool_lock_store",
)

# 老库上补列（表是懒建的，之前跑过的库没有这些列）
_ADDED_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT '已售'",
    "sold_store": "TEXT NOT NULL DEFAULT ''",
    "own_store": "TEXT NOT NULL DEFAULT ''",
    "dispatch_store": "TEXT NOT NULL DEFAULT ''",
    "pos_biz_type": "TEXT NOT NULL DEFAULT ''",
    "data_flag": "TEXT NOT NULL DEFAULT ''",
    "user_type": "TEXT NOT NULL DEFAULT '本地移动'",
    "manual_activated_at": "TEXT NOT NULL DEFAULT ''",
    # 1 = 2026-11 存量宽限（用库里 remain_days，不随今天跳变）；2 = 新导入，按起算日+7 实时算。
    # 老库补这一列时所有存量自动是 1，正是要的效果。
    "rule_ver": "INTEGER NOT NULL DEFAULT 1",
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
    "data_flag": ("data_flag", "数据标记"),
    "tag": ("串码标签", "标签"),
    "city": ("市", "城市"),
}


def find_stock_imei(conn: sqlite3.Connection, query: str) -> Optional[Dict[str, Any]]:
    """触客成交时按串码找池子里那台（全 15 位或尾号 6 位都认）。

    只认 `未售 + seen=1 + 无有效声明` 的机器——也就是真正还在池子里的。
    返回 None 表示「池子里没有这台」，交给调用方提示，不自由手输建脏串码。
    """
    ensure(conn)
    q = re.sub(r"\s+", "", str(query or ""))
    if not q.isdigit() or len(q) < 6:
        return None
    rows = conn.execute(
        f"""
        SELECT t.imei, t.model, t.own_store, t.dispatch_store, t.asof
        FROM imei_todo t
        WHERE t.seen=1 AND t.status=?
          AND {no_live_decl_sql()}
          AND t.imei LIKE ?
        ORDER BY t.imei
        LIMIT 2
        """,
        (STATUS_STOCK, f"%{q}"),
    ).fetchall()
    if len(rows) != 1:
        return None
    return dict(rows[0])


def ensure(conn: sqlite3.Connection) -> None:
    # 不用 executescript：sqlite3.executescript 会先发隐式 COMMIT，若外层正握着写事务会被提前提交；
    # 先只读查 sqlite_master / table_info，表与索引齐备时零 DDL 执行、零表结构锁。
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
    for table, added in (
        ("imei_todo", _ADDED_COLUMNS),
        ("imei_reports", _REPORTS_ADDED_COLUMNS),
    ):
        have = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in added.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # 声明表跟 imei 表同生：池子/待办谓词都引用它，查询前必须存在
    _ensure_decl(conn)


def user_in_imei_scope(conn: sqlite3.Connection, user: Any) -> bool:
    """判断账号是否在顺差串码待办范围内（泰州等排除城市直接隐藏导航并禁止访问）。"""
    if user is None:
        return False
    role = user["role"]
    if role == "admin":
        return True
    scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
    if role == "city" and scope in OUT_CITIES:
        return False
    from .db_stores import list_user_stores

    stores = list_user_stores(conn, user)
    if stores:
        return any(((s["city"] or "").strip() not in OUT_CITIES) for s in stores)
    if role == "readonly" and scope:
        cities = [
            (r["city"] or "").strip()
            for r in conn.execute("SELECT DISTINCT city FROM stores WHERE area_manager=?", (scope,))
        ]
        if cities:
            return any(c not in OUT_CITIES for c in cities)
    return True


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
    rule_ver: int = 2,
) -> Dict[str, Any]:
    """导入一份 CSV：台账（已售）或库存明细（未售）都认，按 IMEI 覆盖，不删行。

    台账模式：`store` = POS 销售门店 → 状态已售，派给销售店。
    库存模式：有「串码标签」列，只收 移动顺差 → 状态未售，派给库存登记店。
    泰州行直接跳过（不计入库数，单独回报）。

    `rule_ver` 只影响**新建行**（`ON CONFLICT` 不碰它，老行永远保持原值）：
    1 = 2026-11-09 前存量（按库里 remain_days + 11-16 宽限看），2 = 按起算日实时算。
    **全量导入（`mark_seen=True`）禁止写 1**：宽限是老存量的专利，外溢会让新机躲催办。
    """
    ensure(conn)
    if int(rule_ver) != 2 and mark_seen:
        raise ValueError("全量导入只能 rule_ver=2（1 只留给 2026-11-09 前存量，不能外溢）")
    rule_ver = int(rule_ver)
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
        # 库存导出往往滞后于 POS 销售：已在在跟台账里的已售机器（status='已售' AND seen=1）绝不被未售库存覆盖回池子
        conflict_guard = (
            f" WHERE imei_todo.status <> '{STATUS_SOLD}' OR imei_todo.seen = 0"
            if stock_mode
            else ""
        )
        # data_flag 只在 CSV 真含「数据标记」列时写：INSERT 带上，DO UPDATE 才更新；
        # 没有该列就用默认 ''，DO UPDATE 不碰 data_flag，保住库里的原值（回填走独立 UPDATE）。
        data_flag_set = ", data_flag=excluded.data_flag" if cols["data_flag"] else ""
        # rule_ver 故意不在 UPSERT 里更新：老行永远保持 1，重导不能把存量升成 2，
        # 否则 11-16 宽限当天就失效、27 台会跳成逾期。只有全新 IMEI 的 INSERT 写 2。
        cur = conn.execute(
            f"""
            INSERT INTO imei_todo(
                imei, status, store_id, sold_store, own_store, dispatch_store,
                model, sp, sold_day, age_days, remain_days,
                stage, state, strength, evidence, pos_biz_type, data_flag,
                asof, rule_ver, seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(imei) DO UPDATE SET
                status=excluded.status, store_id=excluded.store_id,
                sold_store=excluded.sold_store, own_store=excluded.own_store,
                dispatch_store=excluded.dispatch_store,
                model=excluded.model, sp=excluded.sp,
                sold_day=excluded.sold_day, age_days=excluded.age_days,
                remain_days=excluded.remain_days, stage=excluded.stage,
                state=excluded.state, strength=excluded.strength,
                evidence=excluded.evidence, pos_biz_type=excluded.pos_biz_type{data_flag_set},
                asof=excluded.asof,
                seen=1, updated_at=excluded.updated_at{conflict_guard}
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
                _text(row.get(cols["data_flag"]), 200) if cols["data_flag"] else "",
                stamp,
                rule_ver,
                now,
            ),
        )
        if cur.rowcount > 0:
            written += 1
            if not store_id:
                unmapped[dispatch or "(空)"] = unmapped.get(dispatch or "(空)", 0) + 1
    return {
        "rows": rows_total,
        "written": written,
        "bad_imei": bad_imei,
        "skipped_city": skipped_city,
        "unmatched": sorted(unmapped.items(), key=lambda kv: -kv[1]),
        "asof": stamp,
        "mode": "stock" if stock_mode else "ledger",
    }


def update_data_flags(
    conn: sqlite3.Connection,
    pairs: Sequence[Tuple[str, str]],
) -> Dict[str, int]:
    """独立回填 `data_flag`：只 SET 这一列，不碰 seen / rule_ver / remain_days / asof / updated_at。

    pairs = [(imei, data_flag), ...]。回填源不完整时只更命中的行，未命中的原样保留。
    事务提交；返回 {updated, missed}。绝不打印完整串码清单。
    """
    ensure(conn)
    updated = 0
    missed = 0
    begin_immediate(conn)
    for imei, flag in pairs:
        cur = conn.execute(
            "UPDATE imei_todo SET data_flag=? WHERE imei=?",
            (_text(flag, 200), _text(imei, 32)),
        )
        if cur.rowcount > 0:
            updated += 1
        else:
            missed += 1
    return {"updated": updated, "missed": missed}


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
    rule_ver: Optional[int] = None,
    include_unseen: bool = False,
) -> List[Dict[str, Any]]:
    """可见门店的待办行（已售机器），带最新自报。show=open 只给还要动手的。

    `include_unmapped` 给管理员：档案里没有的店那部分机器也一起看。
    `rule_ver` 只用来筛「11-16 存量结转清单」（1=老存量，2=新导入）；不传就全都看。
    `include_unseen` 给存量结转：最新一次清单里已经没有、但库里还留着的老行
    （`seen=0`）也带上，页面上单独标「不在最新清单」。
    排序：逾期在前（remain 越小越靠前），同一天里 IMEI 稳定的顺序。
    """
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids and not include_unmapped:
        return []
    where: List[str] = ["t.status='%s'" % STATUS_SOLD]
    if not include_unseen:
        where.insert(0, "t.seen=1")
    params: List[Any] = []
    if show != "all":
        marks = ",".join("?" * len(CLOSED_STATES))
        where.append(
            f"t.state NOT IN ({marks}) AND COALESCE(r.action,'') NOT IN ('已办','已退货')"
            f" AND {no_live_decl_sql('t')}"
        )
        params = [*CLOSED_STATES, *params]
    if rule_ver is not None:
        where.append("COALESCE(t.rule_ver,1)=?")
        params.append(int(rule_ver))
    if ids:
        clause, raw_params = _store_in("t.store_id", ids)
        params = [*params, *raw_params]
        where.append(f"({clause} OR t.store_id IS NULL)" if include_unmapped else clause)
    rows = conn.execute(
        f"""
        SELECT t.*, s.name AS store_name, s.short_name AS store_short,
               r.action AS report_action, r.biz_type AS report_biz_type,
               r.note AS report_note, r.photo AS report_photo,
               r.created_at AS report_at, u.display_name AS reporter,
               c.action AS carry_action, c.created_at AS carried_at,
               cu.display_name AS carrier
        FROM imei_todo t
        LEFT JOIN stores s ON s.id = t.store_id
        LEFT JOIN imei_reports r ON r.id = (
            SELECT MAX(id) FROM imei_reports WHERE imei = t.imei
        )
        LEFT JOIN users u ON u.id = r.user_id
        LEFT JOIN imei_carry_log c ON c.id = (
            SELECT MAX(id) FROM imei_carry_log WHERE imei = t.imei
        )
        LEFT JOIN users cu ON cu.id = c.user_id
        WHERE {' AND '.join(where)}
        ORDER BY (t.store_id IS NULL), t.store_id, t.remain_days, t.imei
        """,
        params,
    )
    return enrich([dict(row) for row in rows])


def enrich(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给每行补页面要用的东西：待激活/已激活、生效天数、是否逾期、售出几天。

    排序跟着改：同一个店里，正常要办的排前面，待激活的沉到后面单独成组。
    """
    today = today_local()
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
        sold = _parse_day(str(row.get("sold_day") or ""))
        row["sold_days"] = max(0, (today - sold).days) if sold else None
        row["review_due"] = bool(
            row["act_state"] == "待激活"
            and row["sold_days"] is not None
            and row["sold_days"] > REVIEW_DAYS
        )
        # 软失效/宽限已过（计算谓词，零迁移）：页面 chip 与 carryover scope=grace 同源
        # 管理员手动结转过的行不再算「宽限已过」（grace 谓词排除，见 is_grace_row）
        row["carried"] = str(row.get("carry_action") or "") == CARRY_ACTIONS[0]
        row["in_grace"] = is_grace_row(row, today)
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


def grace_day() -> Optional[date]:
    """11-16 软失效宽限截止日（IMEI_GRACE_END 解析成 date）。"""
    return _parse_day(IMEI_GRACE_END)


def grace_label() -> str:
    """宽限截止日的短标签（如 11-16），给页面/模板用；不把日期写死在模板里。

    跟着 `IMEI_GRACE_END` 一起走；解析不出来就原样返回环境变量值，不报错、不瞎猜。
    """
    day = grace_day()
    return f"{day.month:02d}-{day.day:02d}" if day else IMEI_GRACE_END


def grace_active(today: Optional[date] = None) -> bool:
    """今天是否已过 11-16 宽限（软失效/grace 的时间门）。11-16 当天 00:00 起生效。"""
    today = today or today_local()
    end = grace_day()
    return bool(end and today >= end)


def is_grace_row(row: Dict[str, Any], today: Optional[date] = None) -> bool:
    """软失效/grace 范围谓词（唯一来源，页面 chip、carryover scope=grace 共用）。

    条件（全部满足才算「宽限已过」）：
    - 已过 11-16 宽限（grace_active 时间门）；
    - rule_ver=1（存量，2026-11-09 前导入）；
    - seen=1（还在最新清单）；
    - 未办（清单状态不是已办/已退货，且最新自报不是已办/已退货）；
    - 非异地异网；
    - 不在 7 天激活窗口（无激活日，或激活日距今超过 ACT_WINDOW_DAYS 天）；
    - **未被管理员手动结转**（V0.3.69：结转后退出 grace，但门店仍可补报）。

    软失效只标提示、不改 state/seen/remain_days；截止后仍允许补报 已办/已退货/待核。
    """
    if not grace_active(today):
        return False
    if str(row.get("carry_action") or "") == CARRY_ACTIONS[0]:
        return False
    try:
        rule_ver = int(row.get("rule_ver") or 1)
    except (TypeError, ValueError):
        rule_ver = 1
    if rule_ver != 1 or not row.get("seen"):
        return False
    if norm_user_type(str(row.get("user_type") or "")) == USER_REMOTE:
        return False
    if str(row.get("state") or "") in CLOSED_STATES:
        return False
    if str(row.get("report_action") or "") in CLOSED_STATES:
        return False
    act = _parse_day(str(row.get("manual_activated_at") or ""))
    if not act:
        return True
    today = today or today_local()
    return (today - act).days > ACT_WINDOW_DAYS


def review_rows(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
    *,
    include_unmapped: bool = False,
) -> List[Dict[str, Any]]:
    """管理员待核实清单：异地异网、还没激活、售出已超过 30 天。

    只给管理员看。门店端原样保留在「待激活」分组里，但不计逾期、不进催办；
    这里列出来是让管理员人工核实（号码是不是真的还没激活、机器还在不在店）。
    """
    rows = todo_rows(conn, store_ids, show="open", include_unmapped=include_unmapped)
    return [row for row in rows if row["review_due"]]


def carryover_rows(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
    *,
    include_unmapped: bool = False,
    scope: str = "all",
) -> List[Dict[str, Any]]:
    """11-16 存量结转清单：2026-11-09 前导入的 `rule_ver=1` 机器。

    只给管理员看；含清单状态、已售/剩余天数、截至日、门店自报，
    导出 CSV 后逐店核对或重导。口径不动：这些行仍按库里 `remain_days` 看。
    生产上 91 行都是 rule_ver=1，其中 13 行已不在最新清单（`seen=0`），
    这里一并列出并标出来，否则清单只有 78 台，对不上「91 台」。

    `scope` 四个口径（只认这四值，其他当 all）：
    - all：rule_ver=1 全部（含 seen=0 的老行）；
    - lost：`seen=0`（最新清单已没有）；
    - grace：软失效范围 = `is_grace_row`（过 11-16 宽限、seen=1、未办、非异地异网、
      不在 7 天激活窗口、未被手动结转），与 §7 软失效谓词同源，不另写一套 SQL；
    - carried：管理员已手动结转（最新一条结转审计是「结转」），可逐行/批量撤销。
    """
    rows = todo_rows(
        conn,
        store_ids,
        show="all",
        include_unmapped=include_unmapped,
        rule_ver=1,
        include_unseen=True,
    )
    for row in rows:
        row["in_latest"] = bool(row.get("seen"))
        row["carried"] = str(row.get("carry_action") or "") == CARRY_ACTIONS[0]
        row["in_grace"] = is_grace_row(row)
    if scope == "lost":
        rows = [r for r in rows if not r["in_latest"]]
    elif scope == "grace":
        rows = [r for r in rows if r["in_grace"]]
    elif scope == "carried":
        rows = [r for r in rows if r["carried"]]
    rows.sort(
        key=lambda r: (
            r.get("store_id") is None,
            r.get("store_id") or 0,
            str(r.get("imei") or ""),
        )
    )
    return rows


def _carry_snapshot(row: Dict[str, Any]) -> Tuple[Any, ...]:
    """结转审计要存的快照列（IMEI/门店/机型/剩余/标记状态…），顺序跟 INSERT 对齐。"""
    return (
        row["imei"],
        row.get("store_id"),
        _text(row.get("store_name") or row.get("dispatch_store") or "", 60),
        _text(row.get("model") or "", 80),
        row.get("sold_day") or "",
        int(row.get("remain_days") or 0),
        row.get("state") or "",
        row.get("user_type") or "",
        row.get("manual_activated_at") or "",
        row.get("data_flag") or "",
        int(row.get("rule_ver") or 1),
        int(row.get("seen") or 0),
        row.get("asof") or "",
        row.get("report_action") or "",
    )


_CARRY_INSERT = """
    INSERT INTO imei_carry_log(
        imei, store_id, store_name, model, sold_day, remain_days,
        state, user_type, manual_activated_at, data_flag, rule_ver, seen, asof,
        report_action, action, user_id, note, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def carry_over(
    conn: sqlite3.Connection,
    imeis: Sequence[str],
    *,
    store_ids: Sequence[int] = (),
    include_unmapped: bool = False,
    is_admin: bool = False,
    user_id: Optional[int] = None,
    note: str = "",
) -> Dict[str, Any]:
    """管理员手动结转（V0.3.69）：把这批「宽限已过」的存量写一份快照到 imei_carry_log。

    - 仅 admin；**只结当前还在 grace 范围里的行**，别的行（已办/已退货/异地异网/
      未过宽限/已经结转）一律跳过并在返回里报数，不报错中断整批；
    - 不删行、不改 `state` / `seen` / `remain_days` / `rule_ver`，也不动 `updated_at`：
      结转只是管理员这边的账，门店那边照旧看得见、照旧能补报；
    - 结转后 grace 谓词排除该行（`is_grace_row` 看最新一条审计）；撤销见 `undo_carry`。

    返回 `{carried, skipped, now}`：真正写进去的台数 / 被跳过的台数 / 结转时间。
    """
    if not is_admin:
        raise PermissionError("forbidden")
    ensure(conn)
    wanted = [str(i).strip() for i in imeis if str(i).strip()]
    if not wanted:
        raise ValueError("没有选中要结转的机器")
    grace_rows = carryover_rows(
        conn, store_ids, include_unmapped=include_unmapped, scope="grace"
    )
    by_imei = {r["imei"]: r for r in grace_rows}
    hit = [by_imei[i] for i in dict.fromkeys(wanted) if i in by_imei]
    if not hit:
        raise ValueError(
            "选中的机器都不在「宽限已过」范围里（未过宽限 / 已办理 / 异地异网 / 已结转），没有可结转的"
        )
    begin_immediate(conn)
    now = _now()
    for row in hit:
        conn.execute(
            _CARRY_INSERT,
            (*_carry_snapshot(row), CARRY_ACTIONS[0], user_id, _text(note, 200), now),
        )
    return {"carried": len(hit), "skipped": len(wanted) - len(hit), "now": now}


def undo_carry(
    conn: sqlite3.Connection,
    imeis: Sequence[str],
    *,
    store_ids: Sequence[int] = (),
    include_unmapped: bool = False,
    is_admin: bool = False,
    user_id: Optional[int] = None,
    note: str = "",
) -> Dict[str, Any]:
    """撤销结转（仅 admin）：再追加一条「撤销」审计，历史不删、`imei_todo` 不动。

    撤销后这行如果仍满足软失效条件，会重新回到 grace 范围（门店又能看到「宽限已过」）。
    只认当前确实处于「已结转」状态的行。
    """
    if not is_admin:
        raise PermissionError("forbidden")
    ensure(conn)
    wanted = [str(i).strip() for i in imeis if str(i).strip()]
    if not wanted:
        raise ValueError("没有选中要撤销的机器")
    carried = carryover_rows(
        conn, store_ids, include_unmapped=include_unmapped, scope="carried"
    )
    by_imei = {r["imei"]: r for r in carried}
    hit = [by_imei[i] for i in dict.fromkeys(wanted) if i in by_imei]
    if not hit:
        raise ValueError("选中的机器不是已结转状态（可能已经撤销），没有可撤销的")
    begin_immediate(conn)
    now = _now()
    for row in hit:
        conn.execute(
            _CARRY_INSERT,
            (*_carry_snapshot(row), CARRY_ACTIONS[1], user_id, _text(note, 200), now),
        )
    return {"undone": len(hit), "skipped": len(wanted) - len(hit), "now": now}


def carry_log_rows(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
    *,
    include_unmapped: bool = False,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """结转审计留痕（最近在前）：谁、什么时候、结转还是撤销、当时那行的快照。"""
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids:
        return []
    clause, raw_params = _store_in("l.store_id", ids)
    where = f"({clause} OR l.store_id IS NULL)" if include_unmapped else clause
    limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        f"""
        SELECT l.*, s.short_name AS store_short, s.name AS db_store_name,
               COALESCE(u.display_name, u.username, '') AS actor,
               COALESCE(u.role, '') AS actor_role
        FROM imei_carry_log l
        LEFT JOIN stores s ON s.id = l.store_id
        LEFT JOIN users u ON u.id = l.user_id
        WHERE {where}
        ORDER BY l.id DESC
        LIMIT ?
        """,
        [*raw_params, limit],
    )
    return [dict(row) for row in rows]


def marked_rows(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
    *,
    include_unmapped: bool = False,
) -> List[Dict[str, Any]]:
    """管理员「门店标记清单」：当前还是待激活 / 已激活的机器。

    撤销过的不再出现在这里（已经回到本地移动），但 `imei_mark_log` 里留了历史。
    含最新清单里已经没有（`seen=0`）的行：标记还在库里，就必须能看见、能撤销，
    否则机器一从清单消失，标记就再也清不掉了。
    """
    rows = todo_rows(
        conn,
        store_ids,
        show="all",
        include_unmapped=include_unmapped,
        include_unseen=True,
    )
    rows = [row for row in rows if row.get("act_state") in ("待激活", "已激活")]
    for row in rows:
        row["in_latest"] = bool(row.get("seen"))
    rows.sort(
        key=lambda r: (
            r.get("store_id") is None,
            r.get("store_id") or 0,
            str(r.get("imei") or ""),
        )
    )
    return rows


def mark_log_rows(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
    *,
    include_unmapped: bool = False,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """标记动作审计：谁、什么时候、什么动作、原值 → 新值（最近在前）。

    只增不改；这里只读，给管理员看留痕，也方便撤销后回查。
    """
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids:
        return []
    clause, raw_params = _store_in("l.store_id", ids)
    where = f"({clause} OR l.store_id IS NULL)" if include_unmapped else clause
    limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        f"""
        SELECT l.*, s.name AS store_name, s.short_name AS store_short,
               COALESCE(u.display_name, u.username, '') AS actor,
               COALESCE(u.username, '') AS actor_username,
               COALESCE(u.role, '') AS actor_role
        FROM imei_mark_log l
        LEFT JOIN stores s ON s.id = l.store_id
        LEFT JOIN users u ON u.id = l.user_id
        WHERE {where}
        ORDER BY l.id DESC
        LIMIT ?
        """,
        [*raw_params, limit],
    )
    return [dict(row) for row in rows]


def mark_summary(conn: sqlite3.Connection, store_ids: Sequence[int]) -> List[Dict[str, Any]]:
    """各店标了多少台异地异网（管理员用：一家店全标异网就是在躲催办）。

    只统计还没关单的机器；`ratio` 是待激活占该店待办的百分比。
    """
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids:
        return []
    clause, params = _store_in("t.store_id", ids)
    closed_marks = ",".join("?" * len(CLOSED_STATES))
    rows = conn.execute(
        f"""
        WITH latest_r AS (
          SELECT imei, action
          FROM imei_reports
          WHERE id IN (SELECT MAX(id) FROM imei_reports GROUP BY imei)
        )
        SELECT t.store_id, s.short_name AS short, s.name AS name,
               COUNT(*) AS open_n,
               SUM(CASE WHEN t.user_type=? AND t.manual_activated_at='' THEN 1 ELSE 0 END) AS pending_n,
               SUM(CASE WHEN t.manual_activated_at<>'' THEN 1 ELSE 0 END) AS activated_n
        FROM imei_todo t
        LEFT JOIN stores s ON s.id = t.store_id
        LEFT JOIN latest_r r ON r.imei = t.imei
        WHERE t.seen=1 AND t.status=? AND t.state NOT IN ({closed_marks})
          AND COALESCE(r.action, '') NOT IN ({closed_marks})
          AND {no_live_decl_sql()}
          AND {clause}
        GROUP BY t.store_id
        ORDER BY pending_n DESC, open_n DESC
        """,
        [USER_REMOTE, STATUS_SOLD, *CLOSED_STATES, *CLOSED_STATES, *params],
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


def _unmapped_where() -> Tuple[str, List[Any]]:
    """待建店池子的谓词（唯一来源）：`seen=1 AND status='已售' AND store_id IS NULL`，
    **不加任何日期/天数/数据标记条件**（2026-11-10 用户拍板：超期照常能办）。

    「未办」与 `summary.unmapped`、carryover 同一谓词：清单状态不是已办/已退货，
    且最新一条自报不是已办/已退货。返回 `(where_sql, 占位参数)`，调用方自己加 join。
    """
    marks = ",".join("?" * len(CLOSED_STATES))
    sql = (
        "t.seen=1 AND t.status=? AND t.store_id IS NULL"
        f" AND t.state NOT IN ({marks})"
        f" AND COALESCE(r.action,'') NOT IN ({marks})"
    )
    return sql, [STATUS_SOLD, *CLOSED_STATES, *CLOSED_STATES]


_LATEST_R_CTE = """
        WITH latest_r AS (
          SELECT imei, action, created_at, user_id
          FROM imei_reports
          WHERE id IN (SELECT MAX(id) FROM imei_reports GROUP BY imei)
        )
"""


def pool_summary(
    conn: sqlite3.Connection, *, source: str = "stock"
) -> List[Dict[str, Any]]:
    """池子分组汇总，两种来源各自一套口径（不复制查询、不改库存语义）：

    - `source='stock'`（默认）：在库未售的顺差机，按**库存登记门店**（own_store）分组；
    - `source='unmapped'`：待建店待办（已售但门店档案里没有），按 **dispatch_store**
      （POS 销售店名）分组——已售行的 `own_store` 全是空串，按它分组会塌成一行「未登记」。
    """
    ensure(conn)
    if source == "unmapped":
        where, pool_params = _unmapped_where()
        rows = list(
            conn.execute(
                _LATEST_R_CTE
                + f"""
                SELECT t.dispatch_store AS grp, COUNT(*) AS n,
                       COUNT(DISTINCT CASE WHEN t.model<>'' THEN t.model END) AS models
                FROM imei_todo t
                LEFT JOIN latest_r r ON r.imei = t.imei
                WHERE {where}
                GROUP BY t.dispatch_store ORDER BY n DESC
                """,
                pool_params,
            )
        )
        seg: Dict[str, List[Dict[str, Any]]] = {}
        for row in conn.execute(
            _LATEST_R_CTE
            + f"""
            SELECT t.dispatch_store AS grp, t.model, COUNT(*) AS n
            FROM imei_todo t
            LEFT JOIN latest_r r ON r.imei = t.imei
            WHERE {where}
            GROUP BY t.dispatch_store, t.model ORDER BY t.dispatch_store, n DESC
            """,
            pool_params,
        ):
            bucket = seg.setdefault(row["grp"] or "", [])
            if len(bucket) < 3:
                bucket.append({"model": row["model"] or "未登记", "n": row["n"]})
        return [
            {
                "own_store": row["grp"] or "(空)",
                "dispatch_store": row["grp"] or "(空)",
                "n": row["n"],
                "models": seg.get(row["grp"] or "", []),
            }
            for row in rows
        ]
    rows = list(
        conn.execute(
            f"""
            SELECT t.own_store, COUNT(*) AS n,
                   COUNT(DISTINCT CASE WHEN t.model<>'' THEN t.model END) AS models
            FROM imei_todo t
            WHERE t.seen=1 AND t.status=?
              AND {no_live_decl_sql()}
            GROUP BY t.own_store ORDER BY n DESC
            """,
            (STATUS_STOCK,),
        )
    )
    models: Dict[str, List[Dict[str, Any]]] = {}
    for row in conn.execute(
        f"""
        SELECT own_store, model, COUNT(*) AS n FROM imei_todo t
        WHERE t.seen=1 AND t.status=? AND {no_live_decl_sql()}
        GROUP BY own_store, model ORDER BY own_store, n DESC
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
    conn: sqlite3.Connection,
    limit: int = 0,
    own_store: str = "",
    *,
    source: str = "stock",
) -> List[Dict[str, Any]]:
    """南通池子里可以动手的机器（第二阶段「可用串码查询」的口子）。

    这条查询**不按门店过滤**：池子是共享的，A 店可以卖 B 店库存的机器，
    所以谁登录都能看到全池；真要调拨还是走线下审批，这里只回答「池子里有什么」。

    `source`：
    - `stock`（默认）：在库未售，行为与改前逐行一致；
    - `unmapped`：待建店待办 = 已售但门店档案里没有的机器（`store_id IS NULL`），
      **不设任何日期门**，超期行照样在池子里、照样可提交，只多一个 `overdue` 提示。
    """
    ensure(conn)
    if source == "unmapped":
        where, pool_params = _unmapped_where()
        sql = (
            _LATEST_R_CTE
            + f"""
            SELECT t.imei, t.model, t.sp, t.sold_store, t.dispatch_store, t.store_id,
                   t.sold_day, t.age_days, t.remain_days, t.stage, t.state, t.data_flag,
                   t.user_type, t.manual_activated_at, t.asof, t.status, t.rule_ver,
                   r.action AS report_action, r.created_at AS report_at,
                   u.display_name AS reporter
            FROM imei_todo t
            LEFT JOIN latest_r r ON r.imei = t.imei
            LEFT JOIN users u ON u.id = r.user_id
            WHERE {where}
            ORDER BY t.remain_days, t.dispatch_store, t.imei
            """
        )
        if own_store:
            sql = sql.replace("ORDER BY", "AND t.dispatch_store=? ORDER BY")
            pool_params = [*pool_params, own_store]
        if limit:
            sql += " LIMIT ?"
            pool_params = [*pool_params, int(limit)]
        return enrich([dict(row) for row in conn.execute(sql, pool_params)])
    sql = f"""
        SELECT t.imei, t.model, t.own_store, t.dispatch_store, t.store_id,
               t.sold_day, t.asof, s.name AS store_name, s.short_name AS store_short
        FROM imei_todo t
        LEFT JOIN stores s ON s.id = t.store_id
        WHERE t.seen=1 AND t.status=?
          AND {no_live_decl_sql()}
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
    closed_marks = ",".join("?" * len(CLOSED_STATES))
    # 逾期/到期按「生效天数」算：
    # - rule_ver=1 的存量继续用清单里的 remain_days（不随今天跳变，11-16 宽限）；
    # - rule_ver=2 的新导入从起算日（手动激活日优先，否则售出日）到**今天**实时算；
    # - 标了异地异网还没激活的（待激活）单独计数，不计逾期、不算催办；
    # - 门店自报为已办/已退货的同步退出在跟，与列表口径保持一致。
    today_iso = today_local().isoformat()
    row = conn.execute(
        f"""
        WITH latest_r AS (
          SELECT imei, action
          FROM imei_reports
          WHERE id IN (SELECT MAX(id) FROM imei_reports GROUP BY imei)
        ),
        base AS (
          SELECT t.store_id, t.user_type, t.manual_activated_at,
                 COALESCE(t.rule_ver, 1) AS rule_ver,
                 CASE WHEN t.manual_activated_at<>''
                           AND date(t.manual_activated_at) IS NOT NULL
                      THEN MAX(0, CAST(julianday(date(?)) - julianday(date(t.manual_activated_at)) AS INTEGER))
                      WHEN COALESCE(t.rule_ver, 1) >= 2
                           AND date(t.sold_day) IS NOT NULL
                      THEN MAX(0, CAST(julianday(date(?)) - julianday(date(t.sold_day)) AS INTEGER))
                      ELSE NULL END AS age_eff,
                 t.remain_days
          FROM imei_todo t
          LEFT JOIN latest_r r ON r.imei = t.imei
          WHERE t.seen=1 AND t.status=?
            AND t.state NOT IN ({closed_marks})
            AND COALESCE(r.action, '') NOT IN ({closed_marks})
            AND {no_live_decl_sql()}
            AND {clause}
        ),
        calc AS (
          SELECT *,
                 CASE WHEN user_type=? AND manual_activated_at='' THEN NULL
                      WHEN rule_ver <= 1 AND manual_activated_at='' THEN remain_days
                      WHEN age_eff IS NOT NULL THEN ? - age_eff
                      ELSE remain_days END AS remain_eff
          FROM base
        )
        SELECT
          COUNT(*) AS open_n,
          SUM(CASE WHEN user_type=? AND manual_activated_at='' THEN 1 ELSE 0 END) AS pending_n,
          SUM(CASE WHEN manual_activated_at<>'' THEN 1 ELSE 0 END) AS activated_n,
          SUM(CASE WHEN NOT (user_type=? AND manual_activated_at='')
                    AND remain_eff < 0 THEN 1 ELSE 0 END) AS overdue_n,
          SUM(CASE WHEN NOT (user_type=? AND manual_activated_at='')
                    AND remain_eff BETWEEN 0 AND 6 THEN 1 ELSE 0 END) AS soon_n,
          COUNT(DISTINCT store_id) AS store_n
        FROM calc
        """,
        [
            today_iso,
            today_iso,
            STATUS_SOLD,
            *CLOSED_STATES,
            *CLOSED_STATES,
            *params,
            USER_REMOTE,
            ACT_WINDOW_DAYS,
            USER_REMOTE,
            USER_REMOTE,
            USER_REMOTE,
        ],
    ).fetchone()
    asof = conn.execute(
        f"SELECT MAX(t.asof) FROM imei_todo t WHERE t.seen=1 AND {clause}", params
    ).fetchone()[0]
    reported = conn.execute(
        f"""
        SELECT COUNT(DISTINCT r.imei) FROM imei_reports r
        JOIN imei_todo t ON t.imei = r.imei
        WHERE t.seen=1 AND t.status=? AND {clause}
        """,
        [STATUS_SOLD, *params],
    ).fetchone()[0]
    unmapped = 0
    if ids:
        where, where_params = _unmapped_where()
        unmapped = conn.execute(
            _LATEST_R_CTE
            + f"SELECT COUNT(*) FROM imei_todo t LEFT JOIN latest_r r ON r.imei = t.imei WHERE {where}",
            where_params,
        ).fetchone()[0]
    pool = conn.execute(
        f"SELECT COUNT(*) FROM imei_todo t WHERE t.seen=1 AND t.status=? AND {no_live_decl_sql()}",
        (STATUS_STOCK,),
    ).fetchone()[0]

    # 本店代办：待建店池子里由**本店**（claim_store_id）已经锁定的台数（未释放）
    claimed = 0
    if ids:
        claimed = conn.execute(
            f"""
            SELECT COUNT(*) FROM imei_pool_lock
            WHERE released_at IS NULL AND {_store_in('claim_store_id', ids)[0]}
            """,
            _store_in("claim_store_id", ids)[1],
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
        # 待建店池子（= unmapped，同一谓词，测试断言两者相等）；页头「待建店 M」用它
        "pool_unmapped": int(unmapped or 0),
        "claimed": int(claimed or 0),
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
    begin: bool = True,
) -> int:
    """门店自报一条（只增不改）。返回新记录 id。

    选「已办」时业务类型必填（四类之一或「其他」，选「其他」必填说明，记录不全的两类必填说明或截图）；
    选「已退货 / 待核」时不要求业务类型。「非本店」已下线，新报直接拒绝。

    `begin=False` 给「抢锁 → 写报告」同一事务用：`claim_pool_row()` 已经 `BEGIN IMMEDIATE`，
    再抢一次只会被 `db_core` 吞掉并刷 warning 日志。
    """
    ensure(conn)
    if action not in ACTIONS:
        raise ValueError("结论只能是「已办 / 已退货 / 待核」")
    if action == "已办":
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
    else:
        biz = ""
    if begin:
        begin_immediate(conn)
    cur = conn.execute(
        """
        INSERT INTO imei_reports(imei, store_id, action, biz_type, note, photo, user_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (imei, store_id, action, biz, _text(note, 200), photo, user_id, _now()),
    )
    return int(cur.lastrowid or 0)


def pool_open_row(conn: sqlite3.Connection, imei: str) -> Optional[Dict[str, Any]]:
    """按 IMEI 取一条**待建店池子行**（`store_id IS NULL` + 已售 + 未办）；不在池子返回 None。

    这层只回答「这串码能不能代办」，**不判断日期**：不设 7 天窗/remain 门槛（2026-11-10 拍板）。
    """
    ensure(conn)
    where, params = _unmapped_where()
    row = conn.execute(
        _LATEST_R_CTE
        + f"""
        SELECT t.imei, t.model, t.sold_store, t.dispatch_store, t.sold_day,
               t.remain_days, t.state, t.data_flag, t.user_type, t.manual_activated_at,
               t.rule_ver, t.asof, t.status
        FROM imei_todo t
        LEFT JOIN latest_r r ON r.imei = t.imei
        WHERE {where} AND t.imei=?
        """,
        [*params, str(imei).strip()],
    ).fetchone()
    return dict(row) if row else None


def claim_pool_row(
    conn: sqlite3.Connection,
    *,
    imei: str,
    claim_store_id: int,
    action: str = "",
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """代办待建店机器时先抢锁：`imei_pool_lock` 一台只能被一家店办（V0.3.70 一期）。

    为什么要锁而不是写回 `imei_todo.store_id`：下一份全量清单的 `DO UPDATE` 会把 store_id
    覆盖回 POS 解析结果（待建店又变 NULL），写了也会被冲掉；而锁表是只增的审计事实。

    - 本函数内含 `BEGIN IMMEDIATE`，调用方随后的 `add_report(..., begin=False)` 复用同一事务；
      任一步失败由外层 `get_db()` 回滚（锁与自报同生同死，不会只留一半）；
    - 已被别的店锁住 → `ValueError`（带店简称与锁时间，**不回显对方报告内容**）；
    - 自己已锁过 → 幂等返回原锁（同一家店重复提交不报冲突）；
    - 不在待建店池子里 → `ValueError`（已建档 / 已办 / 清单里没有）。
    """
    ensure(conn)
    imei = str(imei).strip()
    store_id = int(claim_store_id)
    begin_immediate(conn)
    # 先看锁：本店已经抢过就直接幂等返回（同一家店可以「待核 → 已办」接着补报，
    # 那条行在第一次自报后就已经不在池子里了，所以锁必须先查、池子判定后查）
    cur = conn.execute(
        """
        SELECT l.*, s.short_name AS claim_short, s.name AS claim_name
        FROM imei_pool_lock l
        LEFT JOIN stores s ON s.id = l.claim_store_id
        WHERE l.imei=? AND l.released_at IS NULL
        """,
        (imei,),
    ).fetchone()
    if cur is not None:
        if int(cur["claim_store_id"]) == store_id:
            return {
                "imei": imei,
                "claim_store_id": store_id,
                "locked_at": cur["locked_at"],
                "own": True,
                # 已经自报过的话这行可能已不在池子里（None 是正常的，调用方别当错）
                "row": pool_open_row(conn, imei),
            }
        who = (cur["claim_short"] or cur["claim_name"] or "其他门店")
        when = str(cur["locked_at"] or "")[5:16]
        raise ValueError(f"这台已被 {who} 在 {when} 办理，不用重复报")
    row = pool_open_row(conn, imei)
    if row is None:
        raise ValueError("这台不在待建店池子里（可能已办理、已退货或不在最新清单）")
    try:
        conn.execute(
            """
            INSERT INTO imei_pool_lock(
                imei, claim_store_id, claim_user_id, report_id, action, locked_at
            ) VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (imei, store_id, int(user_id) if user_id is not None else None, _text(action, 20), _now()),
        )
    except sqlite3.IntegrityError:
        raise ValueError("这台已被其他门店办理，不用重复报") from None
    return {
        "imei": imei,
        "claim_store_id": store_id,
        "locked_at": _now(),
        "own": False,
        "row": row,
    }


def attach_claim_report(conn: sqlite3.Connection, imei: str, report_id: int) -> None:
    """把锁上的 `report_id` 补成对应的自报记录（同一事务内调用）。"""
    if report_id:
        conn.execute(
            "UPDATE imei_pool_lock SET report_id=? WHERE imei=? AND released_at IS NULL",
            (int(report_id), str(imei).strip()),
        )


def release_pool_lock(
    conn: sqlite3.Connection,
    *,
    imei: str,
    is_admin: bool = False,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """管理员释放锁（只记 `released_at/released_by`，**不删行**）。

    默认不自动回池：已退货 / 办错了由管理员手动放回，留审计可查。
    """
    if not is_admin:
        raise PermissionError("forbidden")
    ensure(conn)
    imei = str(imei).strip()
    begin_immediate(conn)
    cur = conn.execute(
        "SELECT id FROM imei_pool_lock WHERE imei=? AND released_at IS NULL", (imei,)
    ).fetchone()
    if cur is None:
        raise ValueError("这台没有未释放的代办锁")
    now = _now()
    conn.execute(
        "UPDATE imei_pool_lock SET released_at=?, released_by=? WHERE id=?",
        (now, int(user_id) if user_id is not None else None, int(cur["id"])),
    )
    return {"imei": imei, "released_at": now}


def pool_claims(
    conn: sqlite3.Connection,
    store_ids: Sequence[int],
) -> List[Dict[str, Any]]:
    """本店代办过的待建店机器（锁未释放）：`/imei` 页折叠区用。

    归属只认锁表，不写回 `imei_todo.store_id`；行上带原售出店（`dispatch_store`）。
    池子行已办理完（已办/已退货）后从池子退出，但仍会留在这里，直到管理员释放。
    """
    ensure(conn)
    ids = [int(s) for s in store_ids]
    if not ids:
        return []
    clause, params = _store_in("l.claim_store_id", ids)
    rows = conn.execute(
        f"""
        SELECT l.imei, l.claim_store_id, l.action AS claim_action, l.locked_at,
               t.model, t.dispatch_store, t.sold_store, t.sold_day, t.remain_days,
               t.state, t.data_flag, t.user_type, t.manual_activated_at, t.rule_ver,
               t.asof, t.status, t.store_id,
               s.short_name AS claim_short, s.name AS claim_name,
               COALESCE(u.display_name, u.username, '') AS actor,
               r.action AS report_action, r.biz_type AS report_biz_type,
               r.created_at AS report_at, r.photo AS report_photo
        FROM imei_pool_lock l
        JOIN imei_todo t ON t.imei = l.imei
        LEFT JOIN stores s ON s.id = l.claim_store_id
        LEFT JOIN users u ON u.id = l.claim_user_id
        LEFT JOIN imei_reports r ON r.id = l.report_id
        WHERE l.released_at IS NULL AND {clause}
        ORDER BY l.locked_at DESC, l.imei
        """,
        params,
    )
    out = [dict(row) for row in rows]
    return enrich(out)


def claim_log_rows(
    conn: sqlite3.Connection, limit: int = 100
) -> List[Dict[str, Any]]:
    """代办锁审计（最近在前）：谁在哪家店抢了哪台、办了什么、释放了没有。"""
    ensure(conn)
    limit = max(1, min(int(limit), 500))
    rows = conn.execute(
        """
        SELECT l.*, s.short_name AS claim_short, s.name AS claim_name,
               COALESCE(u.display_name, u.username, '') AS actor,
               COALESCE(ru.display_name, ru.username, '') AS releaser
        FROM imei_pool_lock l
        LEFT JOIN stores s ON s.id = l.claim_store_id
        LEFT JOIN users u ON u.id = l.claim_user_id
        LEFT JOIN users ru ON ru.id = l.released_by
        ORDER BY l.locked_at DESC, l.imei
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


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
    closed_marks = ",".join("?" * len(CLOSED_STATES))
    return list(
        conn.execute(
            f"""
            WITH latest_r AS (
              SELECT imei, action
              FROM imei_reports
              WHERE id IN (SELECT MAX(id) FROM imei_reports GROUP BY imei)
            )
            SELECT s.id, s.name, s.short_name, COUNT(*) AS n
            FROM imei_todo t
            JOIN stores s ON s.id = t.store_id
            LEFT JOIN latest_r r ON r.imei = t.imei
            WHERE t.seen=1 AND t.status=?
              AND t.state NOT IN ({closed_marks})
              AND COALESCE(r.action, '') NOT IN ({closed_marks})
              AND {clause}
            GROUP BY s.id ORDER BY n DESC
            """,
            [STATUS_SOLD, *CLOSED_STATES, *CLOSED_STATES, *params],
        )
    )


def set_mark(
    conn: sqlite3.Connection,
    *,
    imei: str,
    mark: str,
    day: str = "",
    store_ids: Sequence[int] = (),
    is_admin: bool = False,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """门店的两个动作 + 管理员的撤销与修正。

    - mark='异网'：号码是异地异网，要等门店用改串机激活 → 转「待激活」，不计逾期、不催办
    - mark='已激活'：门店已用改串机激活 → 记下**真实**激活日，7 天从这一天重新起算
    - mark='撤销'：仅管理员可操作 → 回到「本地移动」，按售出日算

    门店一旦点过「已激活」即锁死起算日，非管理员不可再改期或切回异网；
    只能动自己看得见的店；待建店（store_id 为空）不许标——没有店就没有责任人。

    每次成功动作都往 `imei_mark_log` 追加一条：谁、什么时候、从什么值改成什么值。
    审计只增不改，撤销也不会删历史。
    """
    if mark not in MARKS:
        raise ValueError("动作只能是「异网」「已激活」「撤销」")
    if mark == "撤销" and not is_admin:
        raise PermissionError("forbidden")
    ensure(conn)
    begin_immediate(conn)
    row = conn.execute(
        """
        SELECT imei, store_id, sold_day, status, user_type, manual_activated_at
        FROM imei_todo WHERE imei=?
        """,
        (imei,),
    ).fetchone()
    if row is None:
        raise ValueError("这条串码不在清单里，刷新后再试")
    if row["store_id"] is None:
        raise ValueError("这家店还没建档案，先补门店档案再标")
    allowed = [int(s) for s in store_ids]
    if not is_admin and (not allowed or int(row["store_id"]) not in allowed):
        raise PermissionError("forbidden")
    if (row["manual_activated_at"] or "").strip() and not is_admin:
        raise ValueError("这台机器已激活，起算日已锁定；标错了找管理员撤销")
    now = _now()
    old_user_type = str(row["user_type"] or "")
    old_activated_at = str(row["manual_activated_at"] or "")
    store_id = int(row["store_id"])

    def log(new_user_type: str, new_activated_at: str) -> None:
        conn.execute(
            """
            INSERT INTO imei_mark_log(
                imei, store_id, user_id, action,
                old_user_type, old_activated_at, new_user_type, new_activated_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                imei,
                store_id,
                int(user_id) if user_id is not None else None,
                mark,
                old_user_type,
                old_activated_at,
                new_user_type,
                new_activated_at,
                now,
            ),
        )

    if mark == "异网":
        conn.execute(
            "UPDATE imei_todo SET user_type=?, manual_activated_at='', updated_at=? WHERE imei=?",
            (USER_REMOTE, now, imei),
        )
        log(USER_REMOTE, "")
        return {
            "imei": imei,
            "mark": mark,
            "user_type": USER_REMOTE,
            "manual_activated_at": "",
            "old_user_type": old_user_type,
            "old_activated_at": old_activated_at,
            "new_user_type": USER_REMOTE,
            "new_activated_at": "",
        }
    if mark == "撤销":
        conn.execute(
            "UPDATE imei_todo SET user_type=?, manual_activated_at='', updated_at=? WHERE imei=?",
            (USER_DEFAULT, now, imei),
        )
        log(USER_DEFAULT, "")
        return {
            "imei": imei,
            "mark": mark,
            "user_type": USER_DEFAULT,
            "manual_activated_at": "",
            "old_user_type": old_user_type,
            "old_activated_at": old_activated_at,
            "new_user_type": USER_DEFAULT,
            "new_activated_at": "",
        }
    # 已激活：必须填真实激活日（不默认今天），不能是未来、不能早于售出日
    today = today_local()
    raw = (day or "").strip()[:10]
    if not raw:
        raise ValueError("请填写真实激活日期（不能早于售出日、不能晚于今天）")
    act = _parse_day(raw)
    if act is None:
        raise ValueError("激活日要填成 2026-11-09 这样的日期")
    if act > today:
        raise ValueError("激活日不能填将来的日期")
    sold = _parse_day(str(row["sold_day"] or ""))
    if sold and act < sold:
        raise ValueError("激活日不能早于售出日")
    conn.execute(
        "UPDATE imei_todo SET manual_activated_at=?, updated_at=? WHERE imei=?",
        (act.isoformat(), now, imei),
    )
    # user_type 不变，审计里照实记（可能是本地移动直接点激活，也可能是异地异网）
    log(old_user_type, act.isoformat())
    return {
        "imei": imei,
        "mark": mark,
        "user_type": old_user_type,
        "manual_activated_at": act.isoformat(),
        "old_user_type": old_user_type,
        "old_activated_at": old_activated_at,
        "new_user_type": old_user_type,
        "new_activated_at": act.isoformat(),
    }


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
    """导出自报记录（含已办、待核，以及历史遗留的「非本店」），给台账回灌用。

    台账那边按 imei 覆盖结论：门店说「已办」就进已办，历史「非本店」仍按原记录换归属，
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
                   COALESCE(t.sold_store, t.dispatch_store, '') AS sold_store,
                   COALESCE(l.claim_store_id, 0) AS claim_store_id,
                   r.note, r.photo, r.created_at,
                   COALESCE(u.display_name, u.username, '') AS reporter
            FROM imei_reports r
            JOIN stores s ON s.id = r.store_id
            LEFT JOIN imei_todo t ON t.imei = r.imei
            LEFT JOIN imei_pool_lock l ON l.imei = r.imei AND l.report_id = r.id
            LEFT JOIN users u ON u.id = r.user_id
            WHERE {clause}
            ORDER BY r.id
            """,
            params,
        )
    )
