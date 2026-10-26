"""口令散列、强度校验与认证安全机制。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from hashlib import pbkdf2_hmac
from secrets import compare_digest, token_hex
from typing import List, Optional, Tuple

from .db_core import LOCK_HOUR, LOCK_MINUTE, TZ, _now, begin_immediate, today_local

ITERATIONS = 200_000

FILLER_PIN_MIN = 8

ADMIN_PIN_MIN = 8

DEFAULT_FILLER_PIN = "123456"

DEFAULT_ADMIN_PIN = "123456"

LEGACY_ADMIN_PIN = "1234"

DEFAULT_PINS = frozenset({DEFAULT_ADMIN_PIN, DEFAULT_FILLER_PIN, LEGACY_ADMIN_PIN})


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

LOGIN_FAIL_LIMIT = 5

LOGIN_LOCK_SECONDS = 15 * 60

# 账号级锁定：阈值高于单 IP，避免别人拿用户名刷失败把店员锁死；
# 仍能挡住换 IP 扫 6/8 位数字口令。
LOGIN_ACCOUNT_FAIL_LIMIT = 15

LOGIN_ACCOUNT_LOCK_SECONDS = 30 * 60

AUTH_EVENT_KEEP_DAYS = 90

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
