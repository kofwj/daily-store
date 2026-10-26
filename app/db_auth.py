"""口令散列、强度校验与认证安全机制。"""

from __future__ import annotations

import sqlite3
from hashlib import pbkdf2_hmac
from secrets import compare_digest, token_hex
from typing import Optional

# PBKDF2 迭代轮次；单次约 30-50ms，既防暴力破解又避免登录接口阻塞
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
