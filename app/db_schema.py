"""数据库模式定义、数据迁移与默认种子灌入。"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Callable, Dict, List, Tuple

from .db_auth import (
    DEFAULT_ADMIN_PIN,
    DEFAULT_FILLER_PIN,
    LEGACY_ADMIN_PIN,
    _ensure_auth_events,
    _ensure_login_attempts,
    hash_pin,
    pin_is_default,
    verify_pin,
)
from .db_core import _now, begin_immediate, get_db
from .metrics_seed import KPI_TARGETS, all_metrics


def _catalog():
    """生产用 stores_seed_local（不进 git）；测试设 STORE_DAILY_SAMPLE_SEED=1 强制示例店。
    生产没有 local 文件时不要灌示例店。"""
    if os.environ.get("STORE_DAILY_SAMPLE_SEED") == "1":
        from . import stores_seed as seed
        return seed.STORES, seed.filler_accounts, seed.NINGHAI_CODE
    try:
        from . import stores_seed_local as local_seed
        return local_seed.STORES, local_seed.filler_accounts, local_seed.NINGHAI_CODE
    except ImportError:
        return [], (lambda: []), ""

def _catalog_stores():
    return _catalog()[0]

def _catalog_fillers():
    return _catalog()[1]()

def filler_accounts():
    return _catalog_fillers()

FILLER_PIN_RESET_KEY = "filler_default_pin_v1"

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
    -- V5 口径（迁移 17 补齐）：模式快照、运营商范围、移动编码是否参与、挂靠母店
    v5_mode TEXT NOT NULL DEFAULT 'UNSET',
    carrier_scope TEXT NOT NULL DEFAULT 'MOBILE',
    mobile_code_active INTEGER NOT NULL DEFAULT 1,
    affiliated_to INTEGER REFERENCES stores(id),
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

CREATE TABLE IF NOT EXISTS aux_queries (
    biz_date TEXT NOT NULL,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    n INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (biz_date, store_id)
);

CREATE INDEX IF NOT EXISTS idx_aux_queries_date ON aux_queries(biz_date);

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
    (15, "sesame_frozen_to_cents", "_sesame_frozen_to_cents"),
    (16, "oct_coin_cut_rework", "_oct_coin_cut_rework"),
    # 迁移 17：V5 落库。只建表 1–6、补 stores 四列、灌基线/顾问任期；
    # 不重算、不回填 2026-10 及以前任何月份的报表与算薪数据。
    (17, "v5_schema_and_baseline", "_v5_schema_and_baseline"),
]

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
        # V5 表 1–6 建表（结构与迁移 17 一致，页面读取不依赖迁移是否已跑）
        _ensure_v5_tables(conn)
        # 种子（建表/加列/回填默认）每次幂等执行即可；真正“动数据”的迁移走 migrate() 一次
        # seed 是"先 SELECT 再 INSERT"，多 worker 同时启动会撞 UNIQUE；和 migrate() 一样先抢写锁
        conn.commit()
        begin_immediate(conn)
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
            "_sesame_frozen_to_cents": _sesame_frozen_to_cents,
            "_oct_coin_cut_rework": _oct_coin_cut_rework,
            "_v5_schema_and_baseline": _v5_schema_and_baseline,
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
    # 迁移 17：V5 门店口径（只加列，不动历史月份任何数据）
    "v5_mode": "ALTER TABLE stores ADD COLUMN v5_mode TEXT NOT NULL DEFAULT 'UNSET'",
    "carrier_scope": "ALTER TABLE stores ADD COLUMN carrier_scope TEXT NOT NULL DEFAULT 'MOBILE'",
    "mobile_code_active": "ALTER TABLE stores ADD COLUMN mobile_code_active INTEGER NOT NULL DEFAULT 1",
    "affiliated_to": "ALTER TABLE stores ADD COLUMN affiliated_to INTEGER REFERENCES stores(id)",
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

def _sesame_frozen_to_cents(conn: sqlite3.Connection) -> None:
    """把芝麻订单「冻结金额」从元(REAL)迁到分(INTEGER)。已是整数分的库跳过。

    幂等保证：app_meta 里的 sesame_frozen_cents_marker 写入后绝不重跑。
    与 _advance_amounts_to_cents 同口径：一律 ×100，不靠采样值猜。
    """
    _ensure_app_meta(conn)
    marker = conn.execute(
        "SELECT value FROM app_meta WHERE key='sesame_frozen_cents_marker'"
    ).fetchone()
    if marker is not None:
        return

    cols = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(sesame_orders)")}
    if not cols:
        return

    if (cols.get("frozen") or "").upper() == "INTEGER":
        # 新库列声明是 INTEGER：值已经是分，只写标记。
        conn.execute(
            "INSERT OR IGNORE INTO app_meta(key, value) VALUES('sesame_frozen_cents_marker','1')"
        )
        return

    # 旧库 REAL 列：当初写入的就是元，一律 ×100 转分。
    conn.execute(
        "UPDATE sesame_orders SET frozen=CAST(ROUND(COALESCE(frozen, 0) * 100) AS INTEGER)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_meta(key, value) VALUES('sesame_frozen_cents_marker','1')"
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
            frozen INTEGER NOT NULL DEFAULT 0,
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

def _add_must_change_pin(conn: sqlite3.Connection) -> None:
    """给已有账号补改密标记：还是默认口令的，下次登录必须改。"""
    _ensure_user_columns(conn)
    rows = conn.execute(
        "SELECT id, pin_hash FROM users WHERE must_change_pin=0"
    ).fetchall()
    for row in rows:
        if pin_is_default(row["pin_hash"]):
            conn.execute("UPDATE users SET must_change_pin=1 WHERE id=?", (row["id"],))

def _ensure_app_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """
    )

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

def _seed_metrics(conn: sqlite3.Connection) -> None:
    existing = {row["code"] for row in conn.execute("SELECT code FROM metrics")}
    highlight = {
        "bisuan",
        "bisuan_high",
        "ai_contract",
        "welcome_back",
        "coin_cut_new_recharge",
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

def _oct_coin_cut_rework(conn: sqlite3.Connection) -> None:
    """10 月期起金币直降改口径：芝麻免充下线，老用户直降拆成低销迎回 / 全球通优惠。

    「低销迎回」沿用原「底部迎回」的 code（welcome_back），历史累计不丢；
    老用户直降、芝麻免充只停用、不清数据，历史月份照旧能看、能导出。
    """
    conn.execute(
        "UPDATE metrics SET active=0, monthly_target=0 WHERE code IN ('coin_cut_old', 'coin_cut_new_sesame')"
    )

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


# ---------------------------------------------------------------------------
# 迁移 17：V5 落库（表 1–6 + stores 四列 + 基线/顾问任期灌入）
#
# 边界（对齐 docs/V5落库与store-daily改造对接清单.md）：
# * 只做结构新增与配置灌入，**不重算、不回填** 2026-10 及以前任何月份的报表/算薪；
# * 表 3–6 本次只建空表，V5 引擎按 month 触发后才写数据，历史月份双轨并行；
# * 门店一律按 code 定位（测试库示例店是 id 1..6，禁止按 id 区间硬编码）；
# * 6 家示例店的停用与账号下线只在非示例环境（STORE_DAILY_SAMPLE_SEED!=1）执行，
#   否则 alpha 默认登录的数十个既有单测会全部失联。
# ---------------------------------------------------------------------------

V5_TABLES: Tuple[str, ...] = (
    "store_baseline",
    "advisor_tenure",
    "store_month_biz",
    "store_month_settlement",
    "store_month_split",
    "settlement_audit_log",
)

V5_TABLE_DDL: Tuple[str, ...] = (
    # 表 1：门店考核基线与搭载率配置表
    """
    CREATE TABLE IF NOT EXISTS store_baseline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        from_month TEXT NOT NULL,
        to_month TEXT NOT NULL DEFAULT '',
        v5_mode TEXT NOT NULL,
        affiliated_to INTEGER REFERENCES stores(id),
        net_baseline_cents INTEGER NOT NULL DEFAULT 0,
        target_carry_rate REAL NOT NULL DEFAULT 0.20,
        carry_tier_25 REAL NOT NULL DEFAULT 0.20,
        carry_tier_18 REAL NOT NULL DEFAULT 0.15,
        carry_tier_10 REAL NOT NULL DEFAULT 0.10,
        cap_ratio REAL NOT NULL DEFAULT 0.35,
        cap_abs_cents INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(store_id, from_month)
    )
    """,
    # 表 2：专职顾问任期与成本平衡点表（支持换人分段与折算）
    """
    CREATE TABLE IF NOT EXISTS advisor_tenure (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        advisor_name TEXT NOT NULL,
        from_month TEXT NOT NULL,
        to_month TEXT NOT NULL DEFAULT '',
        tier_grade TEXT NOT NULL DEFAULT 'A',
        base_pay_cents INTEGER NOT NULL DEFAULT 400000,
        base_coef REAL NOT NULL DEFAULT 1.20,
        breakeven_cents INTEGER NOT NULL DEFAULT 700000,
        excess_rate REAL NOT NULL DEFAULT 0.40,
        work_days INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        UNIQUE(store_id, advisor_name, from_month)
    )
    """,
    # 表 3：门店月度单笔提成档位核算表（带变更审计列）
    """
    CREATE TABLE IF NOT EXISTS store_month_biz (
        month TEXT NOT NULL,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        source_store_id INTEGER NOT NULL REFERENCES stores(id),
        role_share TEXT NOT NULL DEFAULT 'SHOP_FULL_100',
        biz_code TEXT NOT NULL,
        daily_sum_count INTEGER NOT NULL DEFAULT 0,
        settled_count INTEGER NOT NULL DEFAULT 0,
        unit_price_cents INTEGER NOT NULL DEFAULT 0,
        raw_amount_cents INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT '',
        updated_by INTEGER REFERENCES users(id),
        PRIMARY KEY (month, store_id, source_store_id, role_share, biz_code)
    )
    """,
    # 表 4：门店月度结算、店长 100 分考核、专职顾问薪酬总表（追回款独立列）
    """
    CREATE TABLE IF NOT EXISTS store_month_settlement (
        month TEXT NOT NULL,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        gross_commission_cents INTEGER NOT NULL DEFAULT 0,
        commissioning_fee_cents INTEGER NOT NULL DEFAULT 0,
        total_deduction_cents INTEGER NOT NULL DEFAULT 0,
        deduct_3b_quality_cents INTEGER NOT NULL DEFAULT 0,
        deduct_3b_dispute_cents INTEGER NOT NULL DEFAULT 0,
        deduct_3b_workorder_cents INTEGER NOT NULL DEFAULT 0,
        deduct_3b_luckynum_cents INTEGER NOT NULL DEFAULT 0,
        broadband_star_penalty INTEGER NOT NULL DEFAULT 0,
        net_commission_cents INTEGER NOT NULL DEFAULT 0,
        settled_cat1_fusion INTEGER NOT NULL DEFAULT 0,
        settled_cat2_contract INTEGER NOT NULL DEFAULT 0,
        settled_cat3_ai INTEGER NOT NULL DEFAULT 0,
        settled_cat4_rebate INTEGER NOT NULL DEFAULT 0,
        settled_cat4_rebate_member INTEGER NOT NULL DEFAULT 0,
        core_biz_count INTEGER NOT NULL DEFAULT 0,
        phone_sales_snap INTEGER NOT NULL DEFAULT 0,
        actual_carry_rate REAL NOT NULL DEFAULT 0.0,
        rebate_advance_cents INTEGER NOT NULL DEFAULT 0,
        rebate_recovered_cents INTEGER NOT NULL DEFAULT 0,
        rebate_recovery_ratio_3m REAL NOT NULL DEFAULT 1.0,
        rebate_recovery_exempt INTEGER NOT NULL DEFAULT 0,
        rebate_clawback_cents INTEGER NOT NULL DEFAULT 0,
        exempt_cross_month INTEGER NOT NULL DEFAULT 0,
        audit_flag TEXT NOT NULL DEFAULT 'OK',
        override_remark TEXT NOT NULL DEFAULT '',
        v5_mode_snap TEXT NOT NULL DEFAULT '',
        net_baseline_snap_cents INTEGER NOT NULL DEFAULT 0,
        target_carry_rate_snap REAL NOT NULL DEFAULT 0.20,
        advisor_name_snap TEXT NOT NULL DEFAULT '',
        base_coef_snap REAL NOT NULL DEFAULT 0,
        breakeven_snap_cents INTEGER NOT NULL DEFAULT 0,
        excess_rate_snap REAL NOT NULL DEFAULT 0,
        shop_raw_pool_cents INTEGER NOT NULL DEFAULT 0,
        shop_payout_ratio REAL NOT NULL DEFAULT 1.0,
        shop_final_pool_cents INTEGER NOT NULL DEFAULT 0,
        co_pool_deducted_cents INTEGER NOT NULL DEFAULT 0,
        advisor_retained_net_cents INTEGER NOT NULL DEFAULT 0,
        score_1_net REAL NOT NULL DEFAULT 0,
        score_2_biz REAL NOT NULL DEFAULT 0,
        score_3a_deduct REAL NOT NULL DEFAULT 0,
        score_3b_redline REAL NOT NULL DEFAULT 0,
        total_score_100 REAL NOT NULL DEFAULT 0,
        mgr_score_20 REAL NOT NULL DEFAULT 0,
        mgr_score_15 REAL NOT NULL DEFAULT 0,
        advisor_sys_coef REAL NOT NULL DEFAULT 0,
        advisor_base_pay_cents INTEGER NOT NULL DEFAULT 0,
        advisor_excess_bonus_cents INTEGER NOT NULL DEFAULT 0,
        advisor_3b_penalty_cents INTEGER NOT NULL DEFAULT 0,
        advisor_final_pay_cents INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'DRAFT',
        locked_by INTEGER REFERENCES users(id),
        locked_at TEXT NOT NULL DEFAULT '',
        updated_by INTEGER REFERENCES users(id),
        updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (month, store_id)
    )
    """,
    # 表 5：店长二次分配备案留痕表（自增主键，重名不撞库）
    """
    CREATE TABLE IF NOT EXISTS store_month_split (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT NOT NULL,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        emp_code TEXT NOT NULL DEFAULT '',
        emp_name TEXT NOT NULL,
        role_tag TEXT NOT NULL DEFAULT '导购',
        amount_cents INTEGER NOT NULL DEFAULT 0,
        remark TEXT NOT NULL DEFAULT '',
        confirmed_by INTEGER REFERENCES users(id),
        confirmed_at TEXT NOT NULL DEFAULT ''
    )
    """,
    # 表 6：月结锁月与配置审计日志表
    """
    CREATE TABLE IF NOT EXISTS settlement_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT NOT NULL,
        store_id INTEGER NOT NULL REFERENCES stores(id),
        action TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        before_json TEXT NOT NULL DEFAULT '',
        after_json TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_store_baseline_store ON store_baseline(store_id, from_month)",
    "CREATE INDEX IF NOT EXISTS idx_advisor_tenure_store ON advisor_tenure(store_id, from_month)",
    "CREATE INDEX IF NOT EXISTS idx_store_month_biz_month ON store_month_biz(month, store_id)",
    "CREATE INDEX IF NOT EXISTS idx_store_month_split_month ON store_month_split(month, store_id)",
    "CREATE INDEX IF NOT EXISTS idx_settlement_audit_month ON settlement_audit_log(month, store_id)",
)


def _ensure_v5_tables(conn: sqlite3.Connection) -> None:
    """幂等建 V5 表 1–6；逐条执行（migrate 里已开事务，不能用 executescript 抢提交）。"""
    for sql in V5_TABLE_DDL:
        conn.execute(sql)


def _v5_schema_and_baseline(conn: sqlite3.Connection) -> None:
    """迁移 17：补 stores 四列 → 建表 1–6 → 按 code 灌基线/顾问任期。"""
    _ensure_store_columns(conn)
    _ensure_v5_tables(conn)
    _seed_v5_baseline(conn)


def _seed_v5_baseline(conn: sqlite3.Connection) -> None:
    """把 V5 静态基线灌进库：门店模式、表 1 基线、表 2 顾问任期。

    全部 UPDATE/INSERT OR IGNORE，可重复执行；只碰 V5 口径字段，
    不写 `daily_*`、不改 2026-10 及以前任何月份的既有数据。
    """
    from . import v5_baseline

    ids = {row["code"]: int(row["id"]) for row in conn.execute("SELECT id, code FROM stores")}
    if not ids:
        return
    created = _now()
    for cfg in v5_baseline.STORE_V5_CONFIG:
        sid = ids.get(cfg["code"])
        if sid is None:
            continue
        parent_id = ids.get((cfg.get("affiliated_to") or "").strip())
        conn.execute(
            """
            UPDATE stores
               SET v5_mode=?, carrier_scope=?, mobile_code_active=?, affiliated_to=?
             WHERE code=?
            """,
            (
                cfg["v5_mode"],
                cfg["carrier_scope"],
                int(cfg.get("mobile_code_active") or 0),
                parent_id,
                cfg["code"],
            ),
        )
        if cfg["v5_mode"] in v5_baseline.DISABLED_MODES:
            # 非主控/示例店不配考核基线，也不进提成与考核
            continue
        tier_25, tier_18, tier_10 = v5_baseline.CARRIER_CARRY_TIERS[cfg["carrier_scope"]]
        conn.execute(
            """
            INSERT OR IGNORE INTO store_baseline(
                store_id, from_month, to_month, v5_mode, affiliated_to,
                net_baseline_cents, target_carry_rate,
                carry_tier_25, carry_tier_18, carry_tier_10,
                cap_ratio, cap_abs_cents, created_at
            ) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                v5_baseline.V5_START_MONTH,
                cfg["v5_mode"],
                parent_id,
                int(cfg.get("net_baseline_cents") or 0),
                tier_25,
                tier_25,
                tier_18,
                tier_10,
                v5_baseline.DEFAULT_CAP_RATIO,
                int(cfg.get("cap_abs_cents") or 0),
                created,
            ),
        )
        advisor = cfg.get("advisor")
        if not advisor:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO advisor_tenure(
                store_id, advisor_name, from_month, to_month, tier_grade,
                base_pay_cents, base_coef, breakeven_cents, excess_rate, work_days, note
            ) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, 0, '')
            """,
            (
                sid,
                advisor["name"],
                advisor["from_month"],
                advisor["tier_grade"],
                int(advisor["base_pay_cents"]),
                float(advisor["base_coef"]),
                int(advisor["breakeven_cents"]),
                float(advisor["excess_rate"]),
            ),
        )
        # 顾问任期表是唯一事实源，stores 上两列只是当前在任的只读快照
        conn.execute(
            "UPDATE stores SET advisor_name=?, has_advisor=1 WHERE code=?",
            (advisor["name"], cfg["code"]),
        )
    if os.environ.get("STORE_DAILY_SAMPLE_SEED") != "1":
        # 非示例环境：6 家示例店停用、账号下线（示例测试环境保留，既有单测照旧能登录）
        for code, login in v5_baseline.SAMPLE_STORES:
            conn.execute("UPDATE stores SET active=0 WHERE code=?", (code,))
            conn.execute(
                "UPDATE users SET active=0 WHERE username=? AND role='filler'", (login,)
            )
