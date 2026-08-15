"""门店 db — 底层叶子层：连接、常量、schema、迁移、种子、设置、用户/门店 CRUD。

被 db_query / db_deals 依赖；本模块不反向引用它们（无循环）。
公开 API 由 app/db.py 汇出。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from hashlib import pbkdf2_hmac
from pathlib import Path
from secrets import token_hex
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .metrics_seed import KPI_TARGETS, all_metrics
from .stores_seed import STORES, filler_accounts

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("STORE_DAILY_DATA", ROOT / "data"))

DB_PATH = Path(os.environ.get("STORE_DAILY_DB", DATA_DIR / "store_daily.db"))

ITERATIONS = 200_000

FILLER_PIN_MIN = 6

ADMIN_PIN_MIN = 4

DEFAULT_FILLER_PIN = "123456"

FILLER_PIN_RESET_KEY = "filler_default_pin_v1"

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
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'filler')),
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
"""

MIGRATIONS: List[Tuple[int, str, callable]] = [
    # 旧版在每次启动 seed 里搬数据；改为一次性迁移，避免生产重启反复重做
    (1, "retire_legacy_coin_cut", "_retire_legacy_coin_cut"),
    (2, "split_new_user_coin_cut", "_split_new_user_coin_cut"),
]

def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def today_local() -> date:
    """业务时区（北京时间）的今天。"""
    return datetime.now(TZ).date()

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
    return hash_pin(pin, salt) == stored

def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

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
        _ensure_deal_posts(conn)
        _ensure_deal_edits(conn)
        _ensure_app_meta(conn)
        # 种子（建表/加列/回填默认）每次幂等执行即可；真正“动数据”的迁移走 migrate() 只跑一次
        _seed_metrics(conn)
        _seed_kpi_targets(conn)
        _seed_catalog_stores(conn)
        _seed_defaults(conn)
        _reset_filler_pins_once(conn)
        migrate(conn)

def migrate(conn: sqlite3.Connection) -> None:
    """执行尚未跑过的数据迁移，并记录版本。仅在 init_db 内调用。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {int(row["version"]) for row in conn.execute("SELECT version FROM schema_migrations")}
    fns: Dict[str, callable] = {
        "_retire_legacy_coin_cut": _retire_legacy_coin_cut,
        "_split_new_user_coin_cut": _split_new_user_coin_cut,
    }
    for version, name, fn_name in sorted(MIGRATIONS):
        if version in applied:
            continue
        fn = fns.get(fn_name)
        if fn is None:
            raise RuntimeError(f"迁移 {version} {fn_name} 未注册")
        fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, _now()),
        )

def _ensure_store_columns(conn: sqlite3.Connection) -> None:
    extra = {
        "sort_order": "INTEGER NOT NULL DEFAULT 0",
        "region_group": "TEXT NOT NULL DEFAULT '通泰'",
        "city": "TEXT NOT NULL DEFAULT '南通市'",
        "mobile_code": "TEXT NOT NULL DEFAULT ''",
        "area_manager": "TEXT NOT NULL DEFAULT ''",
        "store_manager": "TEXT NOT NULL DEFAULT ''",
        "follow_ai": "INTEGER NOT NULL DEFAULT 0",
        "follow_bisuan": "INTEGER NOT NULL DEFAULT 0",
        "has_advisor": "INTEGER NOT NULL DEFAULT 0",
        "advisor_name": "TEXT NOT NULL DEFAULT ''",
        "short_name": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in extra.items():
        cols = {row[1] for row in conn.execute("PRAGMA table_info(stores)")}
        if name in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE stores ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

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
    extra = {
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "phone": "TEXT NOT NULL DEFAULT ''",
        "spend": "TEXT NOT NULL DEFAULT ''",
        "hall_query": "INTEGER NOT NULL DEFAULT 1",
        "recommend": "TEXT NOT NULL DEFAULT ''",
        "student": "INTEGER NOT NULL DEFAULT 0",
        "opener": "TEXT NOT NULL DEFAULT ''",
        "note": "TEXT NOT NULL DEFAULT ''",
        "text": "TEXT NOT NULL DEFAULT ''",
    }
    cols = {row[1] for row in conn.execute("PRAGMA table_info(deal_posts)")}
    for name, ddl in extra.items():
        if name in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE deal_posts ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deal_posts_store_date ON deal_posts(store_id, biz_date)"
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

def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

def _reset_filler_pins_once(conn: sqlite3.Connection) -> None:
    done = conn.execute(
        "SELECT value FROM app_meta WHERE key=?", (FILLER_PIN_RESET_KEY,)
    ).fetchone()
    if done:
        return
    hashed = hash_pin(DEFAULT_FILLER_PIN)
    conn.execute("UPDATE users SET pin_hash=? WHERE role='filler'", (hashed,))
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

def wipe_daily_data(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM daily_facts")
    conn.execute("DELETE FROM daily_reports")

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
    """按官方店名补齐 11 家店。已有日报的宁海路店只改名，不换 code。

    新店首次出现时写全目录档案；已存在的店只补名/排序，
    不覆盖管理员在设置里改过的档案字段（区域经理、店长、顾问、停用状态等）。
    """
    by_code = {row["code"]: row for row in conn.execute("SELECT * FROM stores")}
    by_name = {row["name"]: row for row in conn.execute("SELECT * FROM stores")}
    for sort, item in enumerate(STORES, start=1):
        code, name = item["code"], item["name"]
        profile = _profile_values(item)
        row = by_code.get(code)
        if row is None and name in by_name:
            row = by_name[name]
            conn.execute("UPDATE stores SET code=? WHERE id=?", (code, row["id"]))
            row = conn.execute("SELECT * FROM stores WHERE id=?", (row["id"],)).fetchone()
            by_code[code] = row
        if row is None:
            # 首次建目录店：按目录档案写入
            conn.execute(
                """
                INSERT INTO stores(
                    name, code, sort_order, region_group, city, mobile_code,
                    area_manager, store_manager, short_name, advisor_name, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', 1, ?)
                """,
                (name, code, sort, *profile, _now()),
            )
        else:
            # 已存在的店：只更新店名与排序，不覆盖管理员改过的档案/状态
            conn.execute(
                "UPDATE stores SET name=?, sort_order=? WHERE id=?",
                (name, sort, row["id"]),
            )

def _seed_defaults(conn: sqlite3.Connection) -> None:
    all_ids = [row["id"] for row in conn.execute("SELECT id FROM stores WHERE active=1")]
    admin = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if admin is None:
        conn.execute(
            """
            INSERT INTO users(username, display_name, pin_hash, role, active, created_at)
            VALUES (?, ?, ?, 'admin', 1, ?)
            """,
            ("admin", "管理员", hash_pin("1234"), _now()),
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
                INSERT INTO users(username, display_name, pin_hash, role, active, created_at)
                VALUES (?, ?, ?, 'filler', 1, ?)
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
) -> int:
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM stores").fetchone()["n"]
    store_code = (code or "").strip() or alloc_store_code(conn)
    conn.execute(
        """
        INSERT INTO stores(
            name, code, sort_order, region_group, city, mobile_code,
            area_manager, store_manager, follow_ai, follow_bisuan, has_advisor, advisor_name, short_name, active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
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
) -> None:
    name = (advisor_name or "").strip()
    row = conn.execute("SELECT region_group, city FROM stores WHERE id=?", (store_id,)).fetchone()
    if row is None:
        raise ValueError("没有这家店")
    next_region = (region_group if region_group is not None else row["region_group"] or "").strip() or "通泰"
    next_city = (city if city is not None else row["city"] or "").strip() or "南通市"
    conn.execute(
        """
        UPDATE stores SET
            mobile_code=?, area_manager=?, store_manager=?,
            advisor_name=?, has_advisor=?, region_group=?, city=?
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
) -> int:
    if role not in ("admin", "filler"):
        raise ValueError("role")
    conn.execute(
        """
        INSERT INTO users(username, display_name, pin_hash, role, active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (username.strip(), display_name.strip(), hash_pin(pin), role, _now()),
    )
    user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    for store_id in store_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
            (user_id, int(store_id)),
        )
    return user_id

def update_user_pin(conn: sqlite3.Connection, user_id: int, pin: str) -> None:
    conn.execute("UPDATE users SET pin_hash=? WHERE id=?", (hash_pin(pin), user_id))

def set_user_stores(conn: sqlite3.Connection, user_id: int, store_ids: Iterable[int]) -> None:
    conn.execute("DELETE FROM user_stores WHERE user_id=?", (user_id,))
    for store_id in store_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
            (user_id, int(store_id)),
        )

def set_user_active(conn: sqlite3.Connection, user_id: int, active: bool) -> None:
    conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))

def set_metric_target(conn: sqlite3.Connection, code: str, target: int) -> None:
    conn.execute("UPDATE metrics SET monthly_target=? WHERE code=?", (max(0, int(target)), code))

