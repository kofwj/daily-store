"""门店 db — 底层叶子层：连接、路径、设置、会话与基础操作。

被 db_query / db_deals 依赖；本模块不反向引用它们（无循环）。
公开 API 由 app/db.py 汇出。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .metrics_seed import KPI_TARGETS

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("STORE_DAILY_DATA", ROOT / "data"))

DB_PATH = Path(os.environ.get("STORE_DAILY_DB", DATA_DIR / "store_daily.db"))

TZ = ZoneInfo("Asia/Shanghai")

LOCK_HOUR = int(os.environ.get("STORE_DAILY_LOCK_HOUR", "23"))

LOCK_MINUTE = int(os.environ.get("STORE_DAILY_LOCK_MINUTE", "0"))

def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def today_local() -> date:
    """业务时区（北京时间）的今天。"""
    return datetime.now(TZ).date()

def sql_in(column: str, ids: Optional[Sequence[int]]) -> Tuple[str, List[int]]:
    """生成 IN 子句与参数列表：(column IN (?, ?, ...), [ids])。空集合返回 ('1=0', [])。"""
    clean = [int(i) for i in (ids or [])]
    if not clean:
        return "1=0", []
    return f"{column} IN ({','.join('?' * len(clean))})", clean

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
        # 已经开着的是 DEFERRED 事务，抢锁保护没生效；不值得炸掉请求，但要让日志看见
        logging.getLogger(__name__).warning(
            "begin_immediate 在已有事务内调用，写锁保护失效"
        )

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
                # 冻结金额统一按分存（与 advance 金额同口径）；解析层保留元的展示值
                int(round(float(o.get("frozen") or 0) * 100)),
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

def bump_all_session_epochs(conn: sqlite3.Connection) -> None:
    """整库作废现有登录会话（备份恢复后用）。"""
    conn.execute("UPDATE users SET session_epoch = COALESCE(session_epoch, 0) + 1")

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

# 页头每次都读的几个 setting。单行主键点查微秒级，直接现读，不缓存。
_HOT_SETTING_KEYS = ("brand_mark", "brand_kicker", "brand_title", "policy_require_read")

def hot_settings(conn: sqlite3.Connection) -> Dict[str, str]:
    placeholders = ",".join("?" * len(_HOT_SETTING_KEYS))
    rows = conn.execute(
        f"SELECT key, value FROM app_meta WHERE key IN ({placeholders})",
        _HOT_SETTING_KEYS,
    )
    return {str(row[0]): str(row[1] or "") for row in rows}

def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

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

def list_metrics(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM metrics WHERE active=1 ORDER BY sort_order"))

def month_bounds(biz_date: date) -> Tuple[str, str]:
    start = biz_date.replace(day=1).isoformat()
    return start, biz_date.isoformat()

def month_cum_through_many(
    conn: sqlite3.Connection, store_ids: Sequence[int], biz_date: date
) -> Dict[int, Dict[str, int]]:
    """多店整月累计一条 SQL 查完（结算/考核/看板用），语义同逐店调 month_cum_through。

    返回里只有库里有值的指标代码，取数方一律 .get(code, 0)。
    """
    if not store_ids:
        return {}
    start, end = month_bounds(biz_date)
    in_clause, ids = sql_in("store_id", store_ids)
    out: Dict[int, Dict[str, int]] = {}
    rows = conn.execute(
        f"""
        SELECT store_id, metric_code, SUM(day_value) AS total
        FROM daily_facts
        WHERE biz_date>=? AND biz_date<=? AND {in_clause}
        GROUP BY store_id, metric_code
        """,
        (start, end, *ids),
    )
    for row in rows:
        sid = int(row["store_id"])
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

def delete_daily_report(conn: sqlite3.Connection, store_id: int, biz_date: date) -> None:
    """删除某门店特定日期的日报和事实记录。调用方需在事务内。"""
    date_str = biz_date.isoformat()
    conn.execute(
        "DELETE FROM daily_facts WHERE store_id=? AND biz_date=?",
        (store_id, date_str),
    )
    conn.execute(
        "DELETE FROM daily_reports WHERE store_id=? AND biz_date=?",
        (store_id, date_str),
    )

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

def require_lastrowid(cur: sqlite3.Cursor) -> int:
    """提取 INSERT 产生的主键自增 ID。游标未返回自增 ID 时明确抛出异常。"""
    if cur.lastrowid is None:
        raise RuntimeError("SQLite INSERT did not return a valid lastrowid")
    return int(cur.lastrowid)
