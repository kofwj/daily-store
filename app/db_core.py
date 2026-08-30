"""门店 db — 底层叶子层：连接、常量、schema、迁移、种子、设置、用户/门店 CRUD。

被 db_query / db_deals 依赖；本模块不反向引用它们（无循环）。
公开 API 由 app/db.py 汇出。
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from hashlib import pbkdf2_hmac
from pathlib import Path
from secrets import compare_digest, token_hex
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .metrics_seed import KPI_TARGETS, all_metrics


def _catalog():
    """生产用 stores_seed_local（不进 git）；测试设 STORE_DAILY_SAMPLE_SEED=1 强制示例店。
    生产没有 local 文件时不要灌示例店。"""
    if os.environ.get("STORE_DAILY_SAMPLE_SEED") == "1":
        from . import stores_seed as seed
        return seed.STORES, seed.filler_accounts, seed.NINGHAI_CODE
    try:
        from . import stores_seed_local as seed
        return seed.STORES, seed.filler_accounts, seed.NINGHAI_CODE
    except ImportError:
        return [], (lambda: []), ""


def _catalog_stores():
    return _catalog()[0]


def _catalog_fillers():
    return _catalog()[1]()


def filler_accounts():
    return _catalog_fillers()


ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("STORE_DAILY_DATA", ROOT / "data"))

DB_PATH = Path(os.environ.get("STORE_DAILY_DB", DATA_DIR / "store_daily.db"))

ITERATIONS = 200_000

FILLER_PIN_MIN = 8

ADMIN_PIN_MIN = 8

DEFAULT_FILLER_PIN = "123456"

DEFAULT_ADMIN_PIN = "123456"

LEGACY_ADMIN_PIN = "1234"

DEFAULT_PINS = frozenset({DEFAULT_ADMIN_PIN, DEFAULT_FILLER_PIN, LEGACY_ADMIN_PIN})

FILLER_PIN_RESET_KEY = "filler_default_pin_v1"

LOGIN_FAIL_LIMIT = 5

LOGIN_LOCK_SECONDS = 15 * 60

# 账号级锁定：阈值高于单 IP，避免别人拿用户名刷失败把店员锁死；
# 仍能挡住换 IP 扫 6/8 位数字口令。
LOGIN_ACCOUNT_FAIL_LIMIT = 15

LOGIN_ACCOUNT_LOCK_SECONDS = 30 * 60

TZ = ZoneInfo("Asia/Shanghai")

LOCK_HOUR = int(os.environ.get("STORE_DAILY_LOCK_HOUR", "23"))

LOCK_MINUTE = int(os.environ.get("STORE_DAILY_LOCK_MINUTE", "0"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    region_group TEXT NOT NULL DEFAULT '通泰',
    city TEXT NOT NULL DEFAULT '南通市',
    mobile_code TEXT NOT NULL DEFAULT '',
    area_manager TEXT NOT NULL DEFAULT '',
    store_manager TEXT NOT NULL DEFAULT '',
    follow_ai INTEGER NOT NULL DEFAULT 0,
    follow_bisuan INTEGER NOT NULL DEFAULT 0,
    has_advisor INTEGER NOT NULL DEFAULT 0,
    advisor_name TEXT NOT NULL DEFAULT '',
    short_name TEXT NOT NULL DEFAULT '',
    store_grade TEXT NOT NULL DEFAULT '',
    ai_target INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'filler', 'readonly', 'city')),
    scope TEXT NOT NULL DEFAULT '',
    must_change_pin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_stores (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, store_id)
);

CREATE TABLE IF NOT EXISTS metrics (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    section TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    monthly_target INTEGER NOT NULL DEFAULT 0,
    highlight INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_date TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    submitted_by INTEGER REFERENCES users(id),
    submitted_at TEXT NOT NULL,
    compact INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (biz_date, store_id)
);

CREATE TABLE IF NOT EXISTS daily_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_date TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    metric_code TEXT NOT NULL REFERENCES metrics(code),
    day_value INTEGER NOT NULL DEFAULT 0,
    UNIQUE (biz_date, store_id, metric_code)
);

CREATE TABLE IF NOT EXISTS report_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_date TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    edited_at TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '',
    after_json TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edits_store_date ON report_edits(store_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_report_edits_edited_at ON report_edits(edited_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_facts_store_date ON daily_facts(store_id, biz_date);
CREATE INDEX IF NOT EXISTS idx_reports_date ON daily_reports(biz_date);

CREATE TABLE IF NOT EXISTS kpi_targets (
    code TEXT PRIMARY KEY,
    monthly_target INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deal_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    biz_date TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 1,
    model TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    spend TEXT NOT NULL DEFAULT '',
    hall_query INTEGER NOT NULL DEFAULT 1,
    recommend TEXT NOT NULL DEFAULT '',
    student INTEGER NOT NULL DEFAULT 0,
    opener TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_deal_posts_store_date ON deal_posts(store_id, biz_date);

CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT PRIMARY KEY,
    fail_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advisor_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    advisor_name TEXT NOT NULL,
    work_type TEXT NOT NULL DEFAULT '正式',
    base_coeff REAL NOT NULL DEFAULT 1.2,
    score_manager INTEGER,
    score_area INTEGER,
    score_city INTEGER,
    note TEXT NOT NULL DEFAULT '',
    updated_by INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(month, advisor_name)
);
"""

MIGRATIONS: List[Tuple[int, str, str]] = [
    # 旧版在每次启动 seed 里搬数据；改为一次性迁移，避免生产重启反复重做
    (1, "retire_legacy_coin_cut", "_retire_legacy_coin_cut"),
    (2, "split_new_user_coin_cut", "_split_new_user_coin_cut"),
    (3, "expand_user_roles_readonly", "_expand_user_roles_readonly"),
    (4, "add_must_change_pin", "_add_must_change_pin"),
    (5, "add_advance_edits", "_ensure_advance_edits"),
    (6, "audit_edited_at_indexes", "_ensure_audit_edited_at_indexes"),
    (7, "advance_amounts_to_cents", "_advance_amounts_to_cents"),
    (8, "add_auth_events", "_ensure_auth_events"),
    (9, "add_advance_sesame", "_ensure_advance_sesame"),
    (10, "mark_sesame_paid", "_mark_sesame_paid"),
    (11, "scale_bisuan_tenths", "_scale_bisuan_tenths"),
    (12, "admin_pin_six_digits", "_admin_pin_six_digits"),
    (13, "bisuan_mobile_table", "_migrate_bisuan_mobile_from_settings"),
    (14, "expand_user_roles_city", "_expand_user_roles_city"),
]

def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def today_local() -> date:
    """业务时区（北京时间）的今天。"""
    return datetime.now(TZ).date()

def store_in_clause(column: str, store_ids: Optional[Sequence[int]]) -> Tuple[str, List[int]]:
    """生成门店 ID 的 IN 子句。占位符拼接，值仍走参数绑定。"""
    ids = [int(i) for i in (store_ids or [])]
    if not ids:
        return "1=0", []
    return f"{column} IN ({','.join('?' * len(ids))})", ids

def hash_pin(pin: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = token_hex(16)
    digest = pbkdf2_hmac("sha256", pin.encode("utf-8"), salt.encode("utf-8"), ITERATIONS)
    return f"{salt}${digest.hex()}"

def verify_pin(pin: str, stored: str) -> bool:
    try:
        salt, _digest = stored.split("$", 1)
    except ValueError:
        return False
    return compare_digest(hash_pin(pin, salt), stored)

def burn_pin_time(pin: str) -> None:
    """用户不存在时也跑一遍 PBKDF2，抹平响应时间差，防用户名枚举。"""
    hash_pin(pin, "0" * 32)

def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def begin_immediate(conn: sqlite3.Connection) -> None:
    """写事务用 IMMEDIATE：先占写锁，避免 DEFERRED 事务升级写时
    快照冲突偶发 "database is locked"（timeout 对此类冲突不生效）。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "within a transaction" not in str(exc).lower():
            raise


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _ensure_store_columns(conn)
        _ensure_user_columns(conn)
        _ensure_deal_posts(conn)
        _ensure_deal_edits(conn)
        _ensure_advance_posts(conn)
        _ensure_advance_sesame(conn)
        _ensure_sesame_orders(conn)
        _ensure_advance_edits(conn)
        _ensure_app_meta(conn)
        _ensure_login_attempts(conn)
        _ensure_auth_events(conn)
        from .db_invoice import _ensure_invoice_tables
        from .db_policies import _ensure_policy_tables

        _ensure_invoice_tables(conn)
        _ensure_policy_tables(conn)
        # 种子（建表/加列/回填默认）每次幂等执行即可；真正“动数据”的迁移走 migrate() 一次
        _seed_metrics(conn)
        _seed_kpi_targets(conn)
        _seed_catalog_stores(conn)
        _seed_defaults(conn)
        _reset_filler_pins_once(conn)
    migrate()

def _bisuan_mobile_migration(conn: sqlite3.Connection) -> None:
    # 延迟引入：db_bisuan_mobile 反向依赖 db_core._now
    from .db_bisuan_mobile import _migrate_bisuan_mobile_from_settings

    _migrate_bisuan_mobile_from_settings(conn)


def migrate() -> None:
    """执行尚未跑过的数据迁移，并记录版本。独立连接，可在表重建前关外键/开旧式 ALTER。"""
    with get_db() as conn:
        # get_db 刚连上还没开写事务，这里改 pragma 即时生效
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        # 多 worker 同时启动时抢一把写锁，避免两人各跑一遍再撞 UNIQUE
        conn.execute("BEGIN IMMEDIATE")
        applied = {int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations")}
        fns: Dict[str, Callable[[sqlite3.Connection], None]] = {
            "_retire_legacy_coin_cut": _retire_legacy_coin_cut,
            "_split_new_user_coin_cut": _split_new_user_coin_cut,
            "_expand_user_roles_readonly": _expand_user_roles_readonly,
            "_expand_user_roles_city": _expand_user_roles_city,
            "_add_must_change_pin": _add_must_change_pin,
            "_ensure_advance_edits": _ensure_advance_edits,
            "_ensure_audit_edited_at_indexes": _ensure_audit_edited_at_indexes,
            "_advance_amounts_to_cents": _advance_amounts_to_cents,
            "_ensure_auth_events": _ensure_auth_events,
            "_ensure_advance_sesame": _ensure_advance_sesame,
            "_mark_sesame_paid": _mark_sesame_paid,
            "_scale_bisuan_tenths": _scale_bisuan_tenths,
            "_admin_pin_six_digits": _admin_pin_six_digits,
            "_migrate_bisuan_mobile_from_settings": _bisuan_mobile_migration,
        }
        for version, name, fn_name in sorted(MIGRATIONS):
            if version in applied:
                continue
            fn = fns.get(fn_name)
            if fn is None:
                raise RuntimeError(f"迁移 {version} {fn_name} 未注册")
            fn(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _now()),
            )
        # 旧库已经跑过 v11 但没有标记时补写，避免以后重跑再乘 10
        if 11 in applied or 11 in {
            int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations")
        }:
            _ensure_app_meta(conn)
            conn.execute(
                "INSERT OR IGNORE INTO app_meta(key, value) VALUES('bisuan_tenths_marker','1')"
            )

# 建表后给老库补列。ALTER TABLE 无法参数化，这里每列写一条完整字面量语句，
# 不做任何拼接/格式化；键是列名，值是整条 SQL。
_STORE_EXTRA_COLUMNS = {
    "sort_order": "ALTER TABLE stores ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
    "region_group": "ALTER TABLE stores ADD COLUMN region_group TEXT NOT NULL DEFAULT '通泰'",
    "city": "ALTER TABLE stores ADD COLUMN city TEXT NOT NULL DEFAULT '南通市'",
    "mobile_code": "ALTER TABLE stores ADD COLUMN mobile_code TEXT NOT NULL DEFAULT ''",
    "area_manager": "ALTER TABLE stores ADD COLUMN area_manager TEXT NOT NULL DEFAULT ''",
    "store_manager": "ALTER TABLE stores ADD COLUMN store_manager TEXT NOT NULL DEFAULT ''",
    "follow_ai": "ALTER TABLE stores ADD COLUMN follow_ai INTEGER NOT NULL DEFAULT 0",
    "follow_bisuan": "ALTER TABLE stores ADD COLUMN follow_bisuan INTEGER NOT NULL DEFAULT 0",
    "has_advisor": "ALTER TABLE stores ADD COLUMN has_advisor INTEGER NOT NULL DEFAULT 0",
    "advisor_name": "ALTER TABLE stores ADD COLUMN advisor_name TEXT NOT NULL DEFAULT ''",
    "short_name": "ALTER TABLE stores ADD COLUMN short_name TEXT NOT NULL DEFAULT ''",
    "store_grade": "ALTER TABLE stores ADD COLUMN store_grade TEXT NOT NULL DEFAULT ''",
    "ai_target": "ALTER TABLE stores ADD COLUMN ai_target INTEGER NOT NULL DEFAULT 0",
    "invoice_name": "ALTER TABLE stores ADD COLUMN invoice_name TEXT NOT NULL DEFAULT ''",
    "lease_area": "ALTER TABLE stores ADD COLUMN lease_area TEXT NOT NULL DEFAULT ''",
    "lease_address": "ALTER TABLE stores ADD COLUMN lease_address TEXT NOT NULL DEFAULT ''",
    "lease_period": "ALTER TABLE stores ADD COLUMN lease_period TEXT NOT NULL DEFAULT ''",
}

_USER_EXTRA_COLUMNS = {
    "scope": "ALTER TABLE users ADD COLUMN scope TEXT NOT NULL DEFAULT ''",
    "must_change_pin": "ALTER TABLE users ADD COLUMN must_change_pin INTEGER NOT NULL DEFAULT 0",
    # 会话纪元：每次改口令自增，旧 session 带的纪元对不上就强制重新登录
    "session_epoch": "ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0",
}

_DEAL_POST_EXTRA_COLUMNS = {
    "updated_at": "ALTER TABLE deal_posts ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
    "phone": "ALTER TABLE deal_posts ADD COLUMN phone TEXT NOT NULL DEFAULT ''",
    "spend": "ALTER TABLE deal_posts ADD COLUMN spend TEXT NOT NULL DEFAULT ''",
    "hall_query": "ALTER TABLE deal_posts ADD COLUMN hall_query INTEGER NOT NULL DEFAULT 1",
    "recommend": "ALTER TABLE deal_posts ADD COLUMN recommend TEXT NOT NULL DEFAULT ''",
    "student": "ALTER TABLE deal_posts ADD COLUMN student INTEGER NOT NULL DEFAULT 0",
    "opener": "ALTER TABLE deal_posts ADD COLUMN opener TEXT NOT NULL DEFAULT ''",
    "note": "ALTER TABLE deal_posts ADD COLUMN note TEXT NOT NULL DEFAULT ''",
    "text": "ALTER TABLE deal_posts ADD COLUMN text TEXT NOT NULL DEFAULT ''",
}


def _apply_new_columns(conn: sqlite3.Connection, statements: Dict[str, str], cols: set) -> None:
    """执行补列语句；已存在的列跳过，重复加列的并发报错按幂等处理。"""
    for name, sql in statements.items():
        if name in cols:
            continue
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

def _ensure_store_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(stores)")}
    _apply_new_columns(conn, _STORE_EXTRA_COLUMNS, cols)

def _ensure_user_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    _apply_new_columns(conn, _USER_EXTRA_COLUMNS, cols)


def _ensure_deal_posts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deal_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            user_id INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            biz_date TEXT NOT NULL,
            closed INTEGER NOT NULL DEFAULT 1,
            model TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            spend TEXT NOT NULL DEFAULT '',
            hall_query INTEGER NOT NULL DEFAULT 1,
            recommend TEXT NOT NULL DEFAULT '',
            student INTEGER NOT NULL DEFAULT 0,
            opener TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(deal_posts)")}
    _apply_new_columns(conn, _DEAL_POST_EXTRA_COLUMNS, cols)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deal_posts_store_date ON deal_posts(store_id, biz_date)"
    )
    # 同一店同日同号只留最新一笔，再上部分唯一约束防并发重入
    #（业务规则就是同号覆盖；空号不等同 NULL，多笔无号记录不该互相顶掉）
    conn.execute(
        "DELETE FROM deal_posts WHERE phone!='' AND id NOT IN ("
        "SELECT MAX(id) FROM deal_posts WHERE phone!='' GROUP BY store_id, biz_date, phone)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_posts_store_date_phone "
        "ON deal_posts(store_id, biz_date, phone) WHERE phone!=''"
    )


def _ensure_deal_edits(conn: sqlite3.Connection) -> None:
    """成交播报的修改审计表（新增/覆盖都要记）— 幂等建表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deal_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            user_id INTEGER REFERENCES users(id),
            deal_id INTEGER NOT NULL,
            biz_date TEXT NOT NULL,
            edited_at TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '',
            after_json TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deal_edits_store_date ON deal_edits(store_id, biz_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deal_edits_edited_at ON deal_edits(edited_at DESC, id DESC)"
    )


def _ensure_advance_edits(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS advance_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            advance_id INTEGER NOT NULL, store_id INTEGER NOT NULL REFERENCES stores(id),
            user_id INTEGER REFERENCES users(id), biz_date TEXT NOT NULL,
            edited_at TEXT NOT NULL, action TEXT NOT NULL DEFAULT '',
            before_json TEXT NOT NULL DEFAULT '', after_json TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_advance_edits_store_date ON advance_edits(store_id, biz_date)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_advance_edits_edited_at ON advance_edits(edited_at DESC, id DESC)"
    )


def _ensure_audit_edited_at_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_edits_edited_at ON report_edits(edited_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deal_edits_edited_at ON deal_edits(edited_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_advance_edits_edited_at ON advance_edits(edited_at DESC, id DESC)"
    )


def _advance_amounts_to_cents(conn: sqlite3.Connection) -> None:
    """把垫资金额从元(REAL)迁到分(INTEGER)。已是整数分的库跳过。

    幂等保证：app_meta 里的 advance_cents_marker 会在迁移成功后写入。
    即使 schema_migrations 记录丢失，只要标记存在就不会重跑。
    """
    _ensure_app_meta(conn)
    marker = conn.execute(
        "SELECT value FROM app_meta WHERE key='advance_cents_marker'"
    ).fetchone()
    if marker is not None:
        return  # 已经转过，绝不重跑

    cols = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(advance_posts)")}
    if not cols:
        return

    declared = (cols.get("broadband") or "").upper()
    # 新库列声明是 INTEGER：值已经是分，只写标记。
    if declared == "INTEGER":
        conn.execute(
            "INSERT OR IGNORE INTO app_meta(key, value) VALUES('advance_cents_marker','1')"
        )
        return

    # 旧库 REAL 列：当初写入的就是元（record_advance 直接存元）。
    # 一律×100 转分——包括整数元（200、500），不能靠采样值猜（整元与整分无法取值区分）。
    # 若某库当年已手动转过，列声明必是 INTEGER（新表结构），走到上面分支跳过。
    conn.execute(
        "UPDATE advance_posts SET broadband=CAST(ROUND(broadband * 100) AS INTEGER), "
        "rebate=CAST(ROUND(rebate * 100) AS INTEGER), other=CAST(ROUND(other * 100) AS INTEGER)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_meta(key, value) VALUES('advance_cents_marker','1')"
    )


def _ensure_advance_posts(conn: sqlite3.Connection) -> None:
    """门店垫资流水：一笔一行，管理员兑付。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS advance_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            user_id INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            biz_date TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            broadband INTEGER NOT NULL DEFAULT 0,
            rebate INTEGER NOT NULL DEFAULT 0,
            other INTEGER NOT NULL DEFAULT 0,
            sesame INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            ext_id TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            paid INTEGER NOT NULL DEFAULT 0,
            paid_at TEXT NOT NULL DEFAULT '',
            paid_by INTEGER REFERENCES users(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_advance_posts_store_date ON advance_posts(store_id, biz_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_advance_posts_paid ON advance_posts(paid, biz_date)"
    )
    _ensure_advance_sesame(conn)


def _ensure_advance_sesame(conn: sqlite3.Connection) -> None:
    """垫资第四类：芝麻服务费 + 官方流水号去重。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(advance_posts)")}
    if not cols:
        return
    if "sesame" not in cols:
        conn.execute("ALTER TABLE advance_posts ADD COLUMN sesame INTEGER NOT NULL DEFAULT 0")
    if "source" not in cols:
        conn.execute("ALTER TABLE advance_posts ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    if "ext_id" not in cols:
        conn.execute("ALTER TABLE advance_posts ADD COLUMN ext_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_advance_posts_ext_id ON advance_posts(ext_id) WHERE ext_id != ''"
    )


def _ensure_sesame_orders(conn: sqlite3.Connection) -> None:
    """芝麻订单信息（用于按档位分类）。只存业务字段，姓名/手机号/身份证等隐私列不落库。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sesame_orders (
            order_no TEXT PRIMARY KEY,
            store_code TEXT NOT NULL DEFAULT '',
            frozen REAL NOT NULL DEFAULT 0,
            terms INTEGER NOT NULL DEFAULT 0,
            tier TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            order_title TEXT NOT NULL DEFAULT '',
            imported_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # 早期导入的行可能没提档位，从订单标题回填（幂等，每次启动跑一遍）
    rows = conn.execute(
        "SELECT order_no, order_title FROM sesame_orders WHERE tier='' OR tier IS NULL"
    ).fetchall()
    for order_no, title in rows:
        m = re.search(r"(\d+)\s*档", str(title or ""))
        if m:
            conn.execute(
                "UPDATE sesame_orders SET tier=? WHERE order_no=?", (m.group(1), order_no)
            )


def sesame_orders_upsert(conn: sqlite3.Connection, orders: Sequence[Dict[str, Any]], imported_at: str) -> int:
    """按订单号覆盖写入订单信息，返回写入行数。"""
    n = 0
    for o in orders:
        conn.execute(
            """
            INSERT INTO sesame_orders(order_no, store_code, frozen, terms, tier, category, order_title, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_no) DO UPDATE SET
                store_code=excluded.store_code,
                frozen=excluded.frozen,
                terms=excluded.terms,
                tier=excluded.tier,
                category=excluded.category,
                order_title=excluded.order_title,
                imported_at=excluded.imported_at
            """,
            (
                str(o.get("order_no") or "")[:40],
                str(o.get("store_code") or "")[:40],
                float(o.get("frozen") or 0),
                int(o.get("terms") or 0),
                str(o.get("tier") or "")[:20],
                str(o.get("category") or "")[:20],
                str(o.get("order_title") or "")[:120],
                imported_at,
            ),
        )
        n += 1
    return n


def sesame_tier_rows(conn: sqlite3.Connection, start: str, end: str) -> List[Dict[str, Any]]:
    """芝麻流水按（档位类别, 原始档位, 门店）的小计，供档位统计。

    退款回联原单类别/档位；未匹配订单的记「未分类」、档位空。
    """
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(o.category, ''), '未分类') AS cat,
               COALESCE(NULLIF(o.tier, ''), '') AS tier,
               a.store_id AS store_id,
               COUNT(*) AS n,
               SUM(CASE WHEN a.sesame > 0 THEN 1 ELSE 0 END) AS charge_n,
               SUM(CASE WHEN a.sesame < 0 THEN 1 ELSE 0 END) AS refund_n,
               ROUND(SUM(CASE WHEN a.sesame > 0 THEN a.sesame ELSE 0 END) / 100.0, 2) AS charge,
               ROUND(SUM(CASE WHEN a.sesame < 0 THEN a.sesame ELSE 0 END) / 100.0, 2) AS refund,
               ROUND(SUM(a.sesame) / 100.0, 2) AS net
        FROM advance_posts a
        LEFT JOIN sesame_orders o
          ON o.order_no = CASE WHEN substr(a.ext_id, -2) = '_R'
               THEN substr(a.ext_id, 1, length(a.ext_id) - 2) ELSE a.ext_id END
        WHERE a.source = 'sesame' AND a.biz_date >= ? AND a.biz_date <= ?
        GROUP BY cat, tier, a.store_id
        """,
        (start, end),
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "cat": row["cat"],
                "tier": row["tier"] or "",
                "store_id": int(row["store_id"]),
                "n": int(row["n"] or 0),
                "charge_n": int(row["charge_n"] or 0),
                "refund_n": int(row["refund_n"] or 0),
                "charge": float(row["charge"] or 0),
                "refund": float(row["refund"] or 0),
                "net": float(row["net"] or 0),
            }
        )
    return out


def _mark_sesame_paid(conn: sqlite3.Connection) -> None:
    """芝麻服务费是店已充的官方扣款，导入后直接记已兑，不进兑付清单。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(advance_posts)")}
    if "source" not in cols or "paid" not in cols:
        return
    conn.execute(
        """
        UPDATE advance_posts
        SET paid=1,
            paid_at=CASE WHEN paid_at IS NULL OR paid_at='' THEN substr(created_at, 1, 10) ELSE paid_at END,
            paid_by=COALESCE(paid_by, user_id)
        WHERE source='sesame' AND paid=0
        """
    )


def _admin_pin_six_digits(conn: sqlite3.Connection) -> None:
    """仍用 4 位默认口令 1234 的管理员，改成 123456 并强制下次改密。"""
    hashed = hash_pin(DEFAULT_ADMIN_PIN)
    for row in conn.execute("SELECT id, pin_hash FROM users WHERE role='admin'"):
        if verify_pin(LEGACY_ADMIN_PIN, row["pin_hash"]):
            conn.execute(
                "UPDATE users SET pin_hash=?, must_change_pin=1 WHERE id=?",
                (hashed, row["id"]),
            )


def _scale_bisuan_tenths(conn: sqlite3.Connection) -> None:
    """旧笔算按整数存；改成 0.1 精度后，历史数乘 10。

    幂等保证：app_meta 里的 bisuan_tenths_marker 写入后绝不重跑。
    """
    _ensure_app_meta(conn)
    marker = conn.execute(
        "SELECT value FROM app_meta WHERE key='bisuan_tenths_marker'"
    ).fetchone()
    if marker is not None:
        return
    conn.execute(
        "UPDATE daily_facts SET day_value = day_value * 10 WHERE metric_code IN ('bisuan', 'bisuan_high')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_meta(key, value) VALUES('bisuan_tenths_marker','1')"
    )


def _expand_user_roles_readonly(conn: sqlite3.Connection) -> None:
    """给 users 打开 readonly 角色并加 scope 列：重建表最稳，避免旧 CHECK 挡住新角色。

    外键 user_stores.references(users) 按表名解析，重建回退名 users 后依然指向同名表。
    """
    _ensure_user_columns(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    exists_scope = "scope" in cols
    # 检查旧 CHECK 是否已包含 readonly（新库/已迁移库直接跳过重建）
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if sql and "'readonly'" in (sql["sql"] or ""):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE IF EXISTS users_old")
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'filler', 'readonly')),
            scope TEXT NOT NULL DEFAULT '',
            must_change_pin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    has_must = "must_change_pin" in cols
    if exists_scope and has_must:
        conn.execute(
            """
            INSERT INTO users(id, username, display_name, pin_hash, role, scope, must_change_pin, active, created_at)
            SELECT id, username, display_name, pin_hash, role, scope, must_change_pin, active, created_at FROM users_old
            """
        )
    elif exists_scope:
        conn.execute(
            """
            INSERT INTO users(id, username, display_name, pin_hash, role, scope, active, created_at)
            SELECT id, username, display_name, pin_hash, role, scope, active, created_at FROM users_old
            """
        )
    else:
        conn.execute(
            """
            INSERT INTO users(id, username, display_name, pin_hash, role, scope, active, created_at)
            SELECT id, username, display_name, pin_hash, role, '', active, created_at FROM users_old
            """
        )
    # 保留自增水位，避免重建后新用户 id 从 1 起重撞旧值
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM users").fetchone()[0]
    conn.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    if max_id:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('users', ?)", (max_id,))
    conn.execute("DROP TABLE users_old")
    conn.execute("PRAGMA foreign_keys=ON")


def _expand_user_roles_city(conn: sqlite3.Connection) -> None:
    """打开 city（地市负责人）角色：CHECK 里没有 'city' 就重建 users 表。

    到这条迁移时 scope/must_change_pin 列必然已由迁移 3/4 补齐，直接整列搬运。
    """
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if sql and "'city'" in (sql["sql"] or ""):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE IF EXISTS users_old")
    conn.execute("ALTER TABLE users RENAME TO users_old")
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'filler', 'readonly', 'city')),
            scope TEXT NOT NULL DEFAULT '',
            must_change_pin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO users(id, username, display_name, pin_hash, role, scope, must_change_pin, active, created_at)
        SELECT id, username, display_name, pin_hash, role, scope, must_change_pin, active, created_at FROM users_old
        """
    )
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM users").fetchone()[0]
    conn.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    if max_id:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('users', ?)", (max_id,))
    conn.execute("DROP TABLE users_old")
    conn.execute("PRAGMA foreign_keys=ON")


AUTH_EVENT_KEEP_DAYS = 90


def _ensure_auth_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT 'login',
            ip TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_events_created ON auth_events(created_at DESC, id DESC)"
    )


def record_auth_event(
    conn: sqlite3.Connection,
    *,
    user,
    action: str,
    ip: str = "",
) -> None:
    _ensure_auth_events(conn)
    name = ""
    display = ""
    role = ""
    uid = None
    if user is not None:
        uid = int(user["id"])
        name = user["username"] or ""
        display = user["display_name"] or ""
        role = user["role"] or ""
    conn.execute(
        """
        INSERT INTO auth_events(user_id, username, display_name, role, action, ip, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, name, display, role, action, (ip or "").strip()[:80], _now()),
    )
    cutoff = (datetime.now(TZ) - timedelta(days=AUTH_EVENT_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM auth_events WHERE created_at < ?", (cutoff,))


def count_auth_events(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    action: str = "",
    role: str = "",
    q: str = "",
) -> int:
    where, params = _auth_event_where(cutoff, action, role, q)
    return int(conn.execute(f"SELECT COUNT(*) FROM auth_events WHERE {where}", params).fetchone()[0] or 0)


def list_auth_events(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    action: str = "",
    role: str = "",
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> List[sqlite3.Row]:
    where, params = _auth_event_where(cutoff, action, role, q)
    params.extend([limit, offset])
    return list(
        conn.execute(
            f"SELECT * FROM auth_events WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        )
    )


def _auth_event_where(cutoff: str, action: str, role: str, q: str) -> Tuple[str, List]:
    where = ["created_at >= ?"]
    params: List = [cutoff]
    if action in ("login", "logout"):
        where.append("action=?")
        params.append(action)
    if role in ("admin", "filler", "readonly", "city"):
        where.append("role=?")
        params.append(role)
    text = (q or "").strip()
    if text:
        where.append("(username LIKE ? OR display_name LIKE ? OR ip LIKE ?)")
        like = f"%{text}%"
        params.extend([like, like, like])
    return " AND ".join(where), params


def _ensure_login_attempts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            key TEXT PRIMARY KEY,
            fail_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )


def _add_must_change_pin(conn: sqlite3.Connection) -> None:
    """给已有账号补改密标记：还是默认口令的，下次登录必须改。"""
    _ensure_user_columns(conn)
    rows = conn.execute(
        "SELECT id, pin_hash FROM users WHERE must_change_pin=0"
    ).fetchall()
    for row in rows:
        if pin_is_default(row["pin_hash"]):
            conn.execute("UPDATE users SET must_change_pin=1 WHERE id=?", (row["id"],))


def pin_is_default(stored: str) -> bool:
    return any(verify_pin(pin, stored) for pin in DEFAULT_PINS)


def is_weak_new_pin(pin: str) -> bool:
    """新口令：默认值、整串同一字符、连续递增/递减数字，都算弱。"""
    text = pin or ""
    if text in DEFAULT_PINS:
        return True
    if len(text) < FILLER_PIN_MIN:
        return False
    if len(set(text)) == 1:
        return True
    if text.isdigit():
        diffs = [int(text[i + 1]) - int(text[i]) for i in range(len(text) - 1)]
        if diffs and (all(d == 1 for d in diffs) or all(d == -1 for d in diffs)):
            return True
    return False


def new_pin_error(pin: str) -> Optional[str]:
    """新口令校验失败时返回给用户看的原因；通过则 None。"""
    if len(pin or "") < FILLER_PIN_MIN:
        return f"新口令至少 {FILLER_PIN_MIN} 位"
    if is_weak_new_pin(pin):
        return "口令太弱：不能用默认口令、重复数字或连续数字"
    return None


def bump_all_session_epochs(conn: sqlite3.Connection) -> None:
    """整库作废现有登录会话（备份恢复后用）。"""
    conn.execute("UPDATE users SET session_epoch = COALESCE(session_epoch, 0) + 1")


def _login_key_specs(username: str, ip: str) -> List[Tuple[str, str, int, int]]:
    """(kind, key, fail_limit, lock_seconds)。"""
    name = (username or "").strip().lower() or "-"
    addr = (ip or "").strip() or "-"
    return [
        ("uip", f"uip:{name}|{addr}", LOGIN_FAIL_LIMIT, LOGIN_LOCK_SECONDS),
        ("ip", f"ip:{addr}", LOGIN_FAIL_LIMIT, LOGIN_LOCK_SECONDS),
        ("u", f"u:{name}", LOGIN_ACCOUNT_FAIL_LIMIT, LOGIN_ACCOUNT_LOCK_SECONDS),
    ]


def login_lock_remaining(conn: sqlite3.Connection, username: str, ip: str) -> int:
    """还在冷却的秒数；0 表示可以继续试。"""
    now = datetime.now(TZ)
    remaining = 0
    for _kind, key, _limit, _lock_for in _login_key_specs(username, ip):
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE key=?", (key,)
        ).fetchone()
        if not row or not row["locked_until"]:
            continue
        try:
            until = datetime.strptime(row["locked_until"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        except ValueError:
            continue
        left = int((until - now).total_seconds())
        if left > remaining:
            remaining = left
    return remaining


def record_login_failure(conn: sqlite3.Connection, username: str, ip: str) -> int:
    """记一次失败。单 IP / 用户+IP 满 5 次锁 15 分钟；账号满 15 次锁 30 分钟。"""
    begin_immediate(conn)
    now = datetime.now(TZ)
    # 顺带清理 7 天没动静的行：键是 (用户名, IP) 组合，被扫的用户名/IP 会永久留行
    stale_cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM login_attempts WHERE updated_at < ?", (stale_cutoff,))
    remaining = 0
    for _kind, key, fail_limit, lock_seconds in _login_key_specs(username, ip):
        row = conn.execute(
            "SELECT fail_count, locked_until FROM login_attempts WHERE key=?", (key,)
        ).fetchone()
        count = int(row["fail_count"] or 0) + 1 if row else 1
        locked_until = ""
        if row and row["locked_until"]:
            try:
                until = datetime.strptime(row["locked_until"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                if until > now:
                    locked_until = row["locked_until"]
                    count = max(count, fail_limit)
                else:
                    count = 1
            except ValueError:
                pass
        if count >= fail_limit and not locked_until:
            locked_until = (now + timedelta(seconds=lock_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO login_attempts(key, fail_count, locked_until, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                fail_count=excluded.fail_count,
                locked_until=excluded.locked_until,
                updated_at=excluded.updated_at
            """,
            (key, count, locked_until, _now()),
        )
        if locked_until:
            try:
                until = datetime.strptime(locked_until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                left = int((until - now).total_seconds())
                if left > remaining:
                    remaining = left
            except ValueError:
                remaining = max(remaining, lock_seconds)
    return remaining


def clear_login_failures(conn: sqlite3.Connection, username: str, ip: str) -> None:
    keys = [spec[1] for spec in _login_key_specs(username, ip)]
    conn.execute(
        f"DELETE FROM login_attempts WHERE key IN ({','.join('?' * len(keys))})",
        keys,
    )


def _ensure_app_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """
    )

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# 页头每次都读的几个 setting：按库路径 + data_version 缓存。
# 别的 worker 一提交，本连接 PRAGMA data_version 会变，不会跨进程读到过期值。
_HOT_SETTING_KEYS = ("brand_mark", "brand_kicker", "brand_title", "policy_require_read")
_hot_settings_memo: Dict[str, Tuple[int, Dict[str, str]]] = {}


def invalidate_hot_settings() -> None:
    _hot_settings_memo.clear()


def hot_settings(conn: sqlite3.Connection) -> Dict[str, str]:
    path = str(DB_PATH)
    try:
        ver = int(conn.execute("PRAGMA data_version").fetchone()[0])
    except (TypeError, ValueError, sqlite3.Error):
        ver = 0
    hit = _hot_settings_memo.get(path)
    if hit is not None and hit[0] == ver:
        return dict(hit[1])
    placeholders = ",".join("?" * len(_HOT_SETTING_KEYS))
    rows = conn.execute(
        f"SELECT key, value FROM app_meta WHERE key IN ({placeholders})",
        _HOT_SETTING_KEYS,
    )
    data = {str(row[0]): str(row[1] or "") for row in rows}
    _hot_settings_memo[path] = (ver, data)
    return dict(data)

def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    invalidate_hot_settings()

def _reset_filler_pins_once(conn: sqlite3.Connection) -> None:
    done = conn.execute(
        "SELECT value FROM app_meta WHERE key=?", (FILLER_PIN_RESET_KEY,)
    ).fetchone()
    if done:
        return
    hashed = hash_pin(DEFAULT_FILLER_PIN)
    conn.execute(
        "UPDATE users SET pin_hash=?, must_change_pin=1 WHERE role='filler'",
        (hashed,),
    )
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?)",
        (FILLER_PIN_RESET_KEY, _now()),
    )

def is_locked(biz_date: date, now: Optional[datetime] = None) -> bool:
    """当天日期在锁定时间点之后即锁定；非当天日期不锁。

    传入 now 用于测试（naive 或 aware 均可）；默认用业务时区（北京时间）。
    """
    local_now = now or datetime.now(TZ)
    if local_now.tzinfo is not None:
        local_now = local_now.astimezone(TZ).replace(tzinfo=None)
    local_today = today_local()
    if biz_date != local_today:
        return False
    return (local_now.hour, local_now.minute) >= (LOCK_HOUR, LOCK_MINUTE)

def record_edit(
    conn: sqlite3.Connection,
    *,
    biz_date: date,
    store_id: int,
    user_id: int,
    before: Dict[str, int],
    after: Dict[str, int],
    note: str = "",
) -> None:
    import json as _json

    conn.execute(
        """
        INSERT INTO report_edits(biz_date, store_id, user_id, edited_at, before_json, after_json, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            biz_date.isoformat(),
            store_id,
            user_id,
            _now(),
            _json.dumps(before, ensure_ascii=False, sort_keys=True),
            _json.dumps(after, ensure_ascii=False, sort_keys=True),
            note,
        ),
    )

def _seed_metrics(conn: sqlite3.Connection) -> None:
    existing = {row["code"] for row in conn.execute("SELECT code FROM metrics")}
    highlight = {
        "bisuan",
        "bisuan_high",
        "ai_contract",
        "coin_cut_new_recharge",
        "coin_cut_new_sesame",
        "coin_cut_new_savings",
        "coin_cut_new_full",
    }
    for code, name, section, sort in all_metrics():
        if code in existing:
            conn.execute(
                "UPDATE metrics SET name=?, section=?, sort_order=? WHERE code=?",
                (name, section, sort, code),
            )
        else:
            conn.execute(
                """
                INSERT INTO metrics(code, name, section, sort_order, monthly_target, highlight, active)
                VALUES (?, ?, ?, ?, 0, ?, 1)
                """,
                (code, name, section, sort, 1 if code in highlight else 0),
            )

def _retire_legacy_coin_cut(conn: sqlite3.Connection) -> None:
    """旧的合计项拆到老用户直降，避免历史数丢、新表单重复。"""
    legacy = conn.execute("SELECT * FROM metrics WHERE code='coin_cut'").fetchone()
    if legacy is None:
        return
    if int(legacy["active"] or 0) == 0:
        return
    old = conn.execute("SELECT * FROM metrics WHERE code='coin_cut_old'").fetchone()
    if old is not None and int(legacy["monthly_target"] or 0) and not int(old["monthly_target"] or 0):
        conn.execute(
            "UPDATE metrics SET monthly_target=? WHERE code='coin_cut_old'",
            (legacy["monthly_target"],),
        )
    for row in conn.execute(
        "SELECT biz_date, store_id, day_value FROM daily_facts WHERE metric_code='coin_cut'"
    ):
        existing = conn.execute(
            """
            SELECT id, day_value FROM daily_facts
            WHERE biz_date=? AND store_id=? AND metric_code='coin_cut_old'
            """,
            (row["biz_date"], row["store_id"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE daily_facts SET day_value=? WHERE id=?",
                (int(existing["day_value"] or 0) + int(row["day_value"] or 0), existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)
                VALUES (?, ?, 'coin_cut_old', ?)
                """,
                (row["biz_date"], row["store_id"], int(row["day_value"] or 0)),
            )
    conn.execute("DELETE FROM daily_facts WHERE metric_code='coin_cut'")
    conn.execute("UPDATE metrics SET active=0, monthly_target=0 WHERE code='coin_cut'")
    _inherit_coin_kpi(conn, int(legacy["monthly_target"] or 0))

def _inherit_coin_kpi(conn: sqlite3.Connection, inherited: int) -> None:
    if not inherited:
        return
    existing_kpi = conn.execute("SELECT monthly_target FROM kpi_targets WHERE code='coin_cut'").fetchone()
    if existing_kpi is None or int(existing_kpi["monthly_target"] or 0) == 0:
        conn.execute(
            """
            INSERT INTO kpi_targets(code, monthly_target) VALUES ('coin_cut', ?)
            ON CONFLICT(code) DO UPDATE SET monthly_target=excluded.monthly_target
            """,
            (inherited,),
        )

def _split_new_user_coin_cut(conn: sqlite3.Connection) -> None:
    """旧的「新用户直降」合计记到充值，避免历史数丢、新表单重复。"""
    legacy = conn.execute("SELECT * FROM metrics WHERE code='coin_cut_new'").fetchone()
    if legacy is None or int(legacy["active"] or 0) == 0:
        leftover = conn.execute("SELECT 1 FROM daily_facts WHERE metric_code='coin_cut_new'").fetchone()
        if leftover is None:
            return
    for row in conn.execute(
        "SELECT biz_date, store_id, day_value FROM daily_facts WHERE metric_code='coin_cut_new'"
    ):
        existing = conn.execute(
            """
            SELECT id, day_value FROM daily_facts
            WHERE biz_date=? AND store_id=? AND metric_code='coin_cut_new_recharge'
            """,
            (row["biz_date"], row["store_id"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE daily_facts SET day_value=? WHERE id=?",
                (int(existing["day_value"] or 0) + int(row["day_value"] or 0), existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value)
                VALUES (?, ?, 'coin_cut_new_recharge', ?)
                """,
                (row["biz_date"], row["store_id"], int(row["day_value"] or 0)),
            )
    conn.execute("DELETE FROM daily_facts WHERE metric_code='coin_cut_new'")
    conn.execute("UPDATE metrics SET active=0, monthly_target=0 WHERE code='coin_cut_new'")

def _seed_kpi_targets(conn: sqlite3.Connection) -> None:
    for code, _name, _note in KPI_TARGETS:
        conn.execute(
            "INSERT OR IGNORE INTO kpi_targets(code, monthly_target) VALUES (?, 0)",
            (code,),
        )
    # AI 合约目标仍可从指标表带过来一次
    ai = conn.execute("SELECT monthly_target FROM metrics WHERE code='ai_contract'").fetchone()
    kpi_ai = conn.execute("SELECT monthly_target FROM kpi_targets WHERE code='ai_contract'").fetchone()
    if ai and kpi_ai and int(kpi_ai["monthly_target"] or 0) == 0 and int(ai["monthly_target"] or 0):
        conn.execute(
            "UPDATE kpi_targets SET monthly_target=? WHERE code='ai_contract'",
            (int(ai["monthly_target"]),),
        )

def list_kpi_targets(conn: sqlite3.Connection) -> Dict[str, int]:
    out = {code: 0 for code, _name, _note in KPI_TARGETS}
    for row in conn.execute("SELECT code, monthly_target FROM kpi_targets"):
        out[row["code"]] = int(row["monthly_target"] or 0)
    return out


def metric_target_map(conn: sqlite3.Connection) -> Dict[str, int]:
    """报表/填报用的指标目标：考核 KPI 只读 kpi_targets，普通指标读 metrics.monthly_target。"""
    from .metrics_seed import ROLLUPS

    out = {
        row["code"]: int(row["monthly_target"] or 0)
        for row in conn.execute("SELECT code, monthly_target FROM metrics WHERE active=1")
    }
    kpis = list_kpi_targets(conn)
    for code, target in kpis.items():
        if code in out:
            out[code] = target
        if code in ROLLUPS:
            for part in ROLLUPS[code]["parts"]:
                out[part] = target
    return out

def set_kpi_target(conn: sqlite3.Connection, code: str, target: int) -> None:
    allowed = {item[0] for item in KPI_TARGETS}
    if code not in allowed:
        raise ValueError("kpi")
    conn.execute(
        """
        INSERT INTO kpi_targets(code, monthly_target) VALUES (?, ?)
        ON CONFLICT(code) DO UPDATE SET monthly_target=excluded.monthly_target
        """,
        (code, max(0, int(target))),
    )

def _profile_values(item: dict) -> tuple:
    return (
        item.get("region_group") or "通泰",
        item.get("city") or "南通市",
        item.get("mobile_code") or "",
        item.get("area_manager") or "",
        item.get("store_manager") or "",
        item.get("short_name") or "",
    )

def _seed_catalog_stores(conn: sqlite3.Connection) -> None:
    """按官方店名补齐目录店。已有日报的宁海路店只改名，不换 code。

    新店首次出现时写全目录档案；已存在的店只补名/排序，
    不覆盖管理员在设置里改过的档案字段（区域经理、店长、顾问、停用状态等）。
    """
    by_code = {row["code"]: row for row in conn.execute("SELECT * FROM stores")}
    by_name = {row["name"]: row for row in conn.execute("SELECT * FROM stores")}
    for sort, item in enumerate(_catalog_stores(), start=1):
        code, name = item["code"], item["name"]
        profile = _profile_values(item)
        row = by_code.get(code)
        if row is None and name in by_name:
            row = by_name[name]
            try:
                conn.execute("UPDATE stores SET code=? WHERE id=?", (code, row["id"]))
                row = conn.execute("SELECT * FROM stores WHERE id=?", (row["id"],)).fetchone()
                by_code[code] = row
            except sqlite3.IntegrityError:
                pass  # 目标 code 被别家占用：保住现有档案，改名不强行改码，启动不崩
        if row is None:
            # 首次建目录店：按目录档案写入
            conn.execute(
                """
                INSERT INTO stores(
                    name, code, sort_order, region_group, city, mobile_code,
                    area_manager, store_manager, short_name, advisor_name,
                    store_grade, ai_target, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, 1, ?)
                """,
                (
                    name,
                    code,
                    sort,
                    *profile,
                    item.get("store_grade") or "A",
                    int(item.get("ai_target") or 10),
                    _now(),
                ),
            )
        else:
            # 已存在的店：只更新店名与排序，不覆盖管理员改过的档案/状态
            conn.execute(
                "UPDATE stores SET name=?, sort_order=? WHERE id=?",
                (name, sort, row["id"]),
            )
            # 新加的类别/AI目标只在空着时回填目录默认，不覆盖已改过的
            if not (row["store_grade"] if "store_grade" in row.keys() else ""):
                conn.execute(
                    "UPDATE stores SET store_grade=? WHERE id=?",
                    (item.get("store_grade") or "A", row["id"]),
                )
            if not int(row["ai_target"] if "ai_target" in row.keys() else 0):
                conn.execute(
                    "UPDATE stores SET ai_target=? WHERE id=?",
                    (int(item.get("ai_target") or 10), row["id"]),
                )
            # 目录新补的移动编码：只填空，不覆盖管理员已改过的
            if not (row["mobile_code"] if "mobile_code" in row.keys() else "" or "").strip():
                code = (item.get("mobile_code") or "").strip()
                if code:
                    conn.execute("UPDATE stores SET mobile_code=? WHERE id=?", (code, row["id"]))

def _seed_defaults(conn: sqlite3.Connection) -> None:
    all_ids = [row["id"] for row in conn.execute("SELECT id FROM stores WHERE active=1")]
    admin = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if admin is None:
        conn.execute(
            """
            INSERT INTO users(username, display_name, pin_hash, role, must_change_pin, active, created_at)
            VALUES (?, ?, ?, 'admin', 1, 1, ?)
            """,
            ("admin", "管理员", hash_pin(DEFAULT_ADMIN_PIN), _now()),
        )
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
    else:
        admin_id = admin["id"]
    for sid in all_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
            (admin_id, sid),
        )
    _seed_store_fillers(conn)

def _seed_store_fillers(conn: sqlite3.Connection) -> None:
    leftover = conn.execute("SELECT id FROM users WHERE username='ninghai' AND role='filler'").fetchone()
    if leftover:
        conn.execute("DELETE FROM user_stores WHERE user_id=?", (leftover["id"],))
        conn.execute("DELETE FROM users WHERE id=?", (leftover["id"],))
    by_code = {row["code"]: row["id"] for row in conn.execute("SELECT id, code FROM stores")}
    for item in filler_accounts():
        store_id = by_code.get(item["code"])
        if store_id is None:
            continue
        username = item["login"]
        display = item["short_name"]
        existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO users(username, display_name, pin_hash, role, must_change_pin, active, created_at)
                VALUES (?, ?, ?, 'filler', 1, 1, ?)
                """,
                (username, display, hash_pin(DEFAULT_FILLER_PIN), _now()),
            )
            user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            # 新账号补齐目录默认店
            conn.execute(
                "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
                (user_id, store_id),
            )

def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE username=? AND active=1",
        (username.strip(),),
    ).fetchone()

def list_user_stores(conn: sqlite3.Connection, user: sqlite3.Row) -> List[sqlite3.Row]:
    if user["role"] == "admin":
        return list(conn.execute("SELECT * FROM stores WHERE active=1 ORDER BY sort_order, id"))
    if user["role"] == "city":
        # 地市负责人：看本地市（scope=地市名）的启用门店
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if not scope:
            return []
        return list(
            conn.execute(
                "SELECT * FROM stores WHERE city=? AND active=1 ORDER BY sort_order, id",
                (scope,),
            )
        )
    if user["role"] == "readonly":
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if scope:
            # 区域经理：看同一区域经理名下的店
            return list(
                conn.execute(
                    "SELECT * FROM stores WHERE area_manager=? AND active=1 ORDER BY sort_order, id",
                    (scope,),
                )
            )
    return list(
        conn.execute(
            """
            SELECT s.* FROM stores s
            JOIN user_stores us ON us.store_id = s.id
            WHERE us.user_id=? AND s.active=1
            ORDER BY s.sort_order, s.id
            """,
            (user["id"],),
        )
    )

def user_can_access_store(conn: sqlite3.Connection, user: sqlite3.Row, store_id: int) -> bool:
    if user["role"] == "admin":
        row = conn.execute("SELECT id FROM stores WHERE id=? AND active=1", (store_id,)).fetchone()
        return row is not None
    if user["role"] == "city":
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if not scope:
            return False
        row = conn.execute(
            "SELECT 1 FROM stores WHERE id=? AND city=? AND active=1",
            (store_id, scope),
        ).fetchone()
        return row is not None
    if user["role"] == "readonly":
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if scope:
            row = conn.execute(
                "SELECT 1 FROM stores WHERE id=? AND area_manager=? AND active=1",
                (store_id, scope),
            ).fetchone()
            return row is not None
    row = conn.execute(
        "SELECT 1 FROM user_stores WHERE user_id=? AND store_id=?",
        (user["id"], store_id),
    ).fetchone()
    return row is not None

def list_metrics(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM metrics WHERE active=1 ORDER BY sort_order"))

def get_store(conn: sqlite3.Connection, store_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()

def month_bounds(biz_date: date) -> Tuple[str, str]:
    start = biz_date.replace(day=1).isoformat()
    return start, biz_date.isoformat()

def month_cum_through_many(
    conn: sqlite3.Connection, store_ids: Sequence[int], biz_date: date
) -> Dict[int, Dict[str, int]]:
    """多店整月累计一条 SQL 查完（结算/考核/看板用），语义同逐店调 month_cum_through。

    返回里只有库里有值的指标代码，取数方一律 .get(code, 0)。
    月内 daily_facts 规模有限，整月聚合后按 store_ids 过滤，避免动态拼 IN 子句。
    """
    allowed = {int(i) for i in (store_ids or ())}
    if not allowed:
        return {}
    start, end = month_bounds(biz_date)
    out: Dict[int, Dict[str, int]] = {}
    rows = conn.execute(
        """
        SELECT store_id, metric_code, SUM(day_value) AS total
        FROM daily_facts
        WHERE biz_date>=? AND biz_date<=?
        GROUP BY store_id, metric_code
        """,
        (start, end),
    )
    for row in rows:
        sid = int(row["store_id"])
        if sid in allowed:
            out.setdefault(sid, {})[row["metric_code"]] = int(row["total"] or 0)
    return out

def get_report(conn: sqlite3.Connection, store_id: int, biz_date: date) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.*, u.display_name AS submitter_name
        FROM daily_reports r
        LEFT JOIN users u ON u.id = r.submitted_by
        WHERE r.store_id=? AND r.biz_date=?
        """,
        (store_id, biz_date.isoformat()),
    ).fetchone()

def list_users(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM users ORDER BY id"))

def list_all_stores(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM stores ORDER BY sort_order, id"))

def user_store_ids(conn: sqlite3.Connection, user_id: int) -> List[int]:
    return [
        row["store_id"]
        for row in conn.execute(
            "SELECT store_id FROM user_stores WHERE user_id=?",
            (user_id,),
        )
    ]

def alloc_store_code(conn: sqlite3.Connection) -> str:
    """系统内部唯一编码，不给管理员手填。"""
    n = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM stores").fetchone()[0])
    code = f"s{n}"
    while conn.execute("SELECT 1 FROM stores WHERE code=?", (code,)).fetchone():
        n += 1
        code = f"s{n}"
    return code

def create_store(
    conn: sqlite3.Connection,
    name: str,
    code: str = "",
    *,
    region_group: str = "通泰",
    city: str = "南通市",
    mobile_code: str = "",
    area_manager: str = "",
    store_manager: str = "",
    follow_ai: bool = False,
    follow_bisuan: bool = False,
    advisor_name: str = "",
    short_name: str = "",
    store_grade: str = "A",
    ai_target: int = 10,
    invoice_name: str = "",
    lease_area: str = "",
    lease_address: str = "",
    lease_period: str = "",
) -> int:
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM stores").fetchone()["n"]
    store_code = (code or "").strip() or alloc_store_code(conn)
    conn.execute(
        """
        INSERT INTO stores(
            name, code, sort_order, region_group, city, mobile_code,
            area_manager, store_manager, follow_ai, follow_bisuan, has_advisor, advisor_name, short_name,
            store_grade, ai_target, invoice_name, lease_area, lease_address, lease_period, active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            name.strip(),
            store_code,
            int(nxt),
            region_group.strip() or "通泰",
            city.strip() or "南通市",
            mobile_code.strip(),
            area_manager.strip(),
            store_manager.strip(),
            1 if follow_ai else 0,
            1 if follow_bisuan else 0,
            1 if advisor_name.strip() else 0,
            advisor_name.strip(),
            (short_name or "").strip(),
            (store_grade or "A").strip().upper()[:1] or "A",
            max(0, int(ai_target or 10)),
            (invoice_name or "").strip(),
            (lease_area or "").strip(),
            (lease_address or "").strip(),
            (lease_period or "").strip(),
            _now(),
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

def update_store_profile(
    conn: sqlite3.Connection,
    store_id: int,
    *,
    mobile_code: str,
    area_manager: str,
    store_manager: str,
    advisor_name: str = "",
    region_group: Optional[str] = None,
    city: Optional[str] = None,
    store_grade: Optional[str] = None,
    ai_target: Optional[int] = None,
    invoice_name: Optional[str] = None,
    lease_area: Optional[str] = None,
    lease_address: Optional[str] = None,
    lease_period: Optional[str] = None,
) -> None:
    name = (advisor_name or "").strip()
    row = conn.execute(
        "SELECT region_group, city, store_grade, ai_target, invoice_name, lease_area, lease_address, lease_period FROM stores WHERE id=?",
        (store_id,),
    ).fetchone()
    if row is None:
        raise ValueError("没有这家店")
    next_region = (region_group if region_group is not None else row["region_group"] or "").strip() or "通泰"
    next_city = (city if city is not None else row["city"] or "").strip() or "南通市"
    next_grade = (
        (store_grade if store_grade is not None else row["store_grade"] or "A")
        .strip()
        .upper()[:1]
        or "A"
    )
    if next_grade not in ("A", "B"):
        next_grade = "A"
    if ai_target is None:
        next_ai = int(row["ai_target"] or 10)
    else:
        next_ai = max(0, int(ai_target))
    next_invoice = invoice_name if invoice_name is not None else (row["invoice_name"] or "")
    next_area = lease_area if lease_area is not None else (row["lease_area"] or "")
    next_addr = lease_address if lease_address is not None else (row["lease_address"] or "")
    next_period = lease_period if lease_period is not None else (row["lease_period"] or "")
    conn.execute(
        """
        UPDATE stores SET
            mobile_code=?, area_manager=?, store_manager=?,
            advisor_name=?, has_advisor=?, region_group=?, city=?,
            store_grade=?, ai_target=?, invoice_name=?, lease_area=?, lease_address=?, lease_period=?
        WHERE id=?
        """,
        (
            mobile_code.strip(),
            area_manager.strip(),
            store_manager.strip(),
            name,
            1 if name else 0,
            next_region,
            next_city,
            next_grade,
            next_ai,
            (next_invoice or "").strip(),
            (next_area or "").strip(),
            (next_addr or "").strip(),
            (next_period or "").strip(),
            store_id,
        ),
    )

def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    pin: str,
    role: str,
    store_ids: Iterable[int],
    scope: str = "",
) -> int:
    if role not in ("admin", "filler", "readonly", "city"):
        raise ValueError("role")
    must_change = 1 if is_weak_new_pin(pin) else 0
    conn.execute(
        """
        INSERT INTO users(username, display_name, pin_hash, role, scope, must_change_pin, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (username.strip(), display_name.strip(), hash_pin(pin), role, scope.strip(), must_change, _now()),
    )
    user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    for store_id in store_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
            (user_id, int(store_id)),
        )
    return user_id

def set_user_scope(conn: sqlite3.Connection, user_id: int, scope: str) -> None:
    conn.execute("UPDATE users SET scope=? WHERE id=?", (scope.strip(), user_id))

def update_user_pin(conn: sqlite3.Connection, user_id: int, pin: str) -> int:
    """改口令并自增会话纪元：该用户所有旧会话立即失效。

    返回新纪元值。改自己口令的调用方要把当前 session 的纪元同步刷新，
    否则会把自己也登出。
    """
    must_change = 1 if is_weak_new_pin(pin) else 0
    conn.execute(
        "UPDATE users SET pin_hash=?, must_change_pin=?, session_epoch=session_epoch+1 WHERE id=?",
        (hash_pin(pin), must_change, user_id),
    )
    row = conn.execute("SELECT session_epoch FROM users WHERE id=?", (user_id,)).fetchone()
    return int(row["session_epoch"] or 0)

def set_user_stores(conn: sqlite3.Connection, user_id: int, store_ids: Iterable[int]) -> None:
    conn.execute("DELETE FROM user_stores WHERE user_id=?", (user_id,))
    for store_id in store_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
            (user_id, int(store_id)),
        )

def set_user_active(conn: sqlite3.Connection, user_id: int, active: bool) -> None:
    conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))

_ADVISOR_SCORE_COLS = {
    "work_type",
    "base_coeff",
    "score_manager",
    "score_area",
    "score_city",
    "note",
}


def list_advisor_scores(conn: sqlite3.Connection, month: str) -> List[sqlite3.Row]:
    """某月全部顾问打分行。"""
    return list(
        conn.execute(
            "SELECT * FROM advisor_scores WHERE month=? ORDER BY advisor_name",
            (month,),
        )
    )


def upsert_advisor_score(
    conn: sqlite3.Connection,
    month: str,
    advisor_name: str,
    updates: Dict[str, Any],
    user_id: int = 0,
) -> None:
    """按 (month, advisor_name) upsert，只更新 updates 里白名单内的列。

    列名先过 _ADVISOR_SCORE_COLS 白名单再拼 SQL；值为 None 表示清空该列
    （比如把打分改回未打）。空 updates 直接跳过。
    """
    clean = {k: v for k, v in updates.items() if k in _ADVISOR_SCORE_COLS}
    name = (advisor_name or "").strip()
    if not clean or not name:
        return
    row = conn.execute(
        "SELECT id FROM advisor_scores WHERE month=? AND advisor_name=?",
        (month, name),
    ).fetchone()
    now = _now()
    if row is None:
        conn.execute(
            "INSERT INTO advisor_scores(month, advisor_name, updated_by, updated_at) VALUES (?, ?, ?, ?)",
            (month, name, user_id, now),
        )
        row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        row_id = int(row["id"])
    sets = ", ".join(f"{k}=?" for k in clean)
    params = list(clean.values()) + [user_id, now, row_id]
    conn.execute(
        f"UPDATE advisor_scores SET {sets}, updated_by=?, updated_at=? WHERE id=?",
        params,
    )


def set_store_active(conn: sqlite3.Connection, store_id: int, active: bool) -> None:
    conn.execute("UPDATE stores SET active=? WHERE id=?", (1 if active else 0, int(store_id)))


def store_has_data(conn: sqlite3.Connection, store_id: int) -> bool:
    sid = int(store_id)
    tables = (
        "daily_facts",
        "daily_reports",
        "deal_posts",
        "advance_posts",
        "invoice_months",
        "bisuan_mobile",
        # 审计表也引用 stores(id) 且无级联，删业务行后审计行仍在，漏查会让 delete_store 撞外键
        "report_edits",
        "deal_edits",
        "advance_edits",
    )
    for table in tables:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        if conn.execute(f"SELECT 1 FROM {table} WHERE store_id=? LIMIT 1", (sid,)).fetchone():
            return True
    return False


def delete_store(conn: sqlite3.Connection, store_id: int) -> None:
    sid = int(store_id)
    if store_has_data(conn, sid):
        raise ValueError("这家店已有填报/触客/垫资，不能删，只能停用")
    conn.execute("DELETE FROM user_stores WHERE store_id=?", (sid,))
    conn.execute("DELETE FROM stores WHERE id=?", (sid,))

