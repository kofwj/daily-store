"""SQLite 备份 / 恢复。备份文件放在 data/backups。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

from . import db_core

BACKUP_DIR_NAME = "backups"
BACKUP_PREFIX = "store_daily_"
MAX_KEEP = 20
REQUIRED_TABLES = ("stores", "users")


def backup_dir() -> Path:
    path = Path(db_core.DATA_DIR) / BACKUP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stamp() -> str:
    return datetime.now(db_core.TZ).strftime("%Y%m%d_%H%M%S")


def snapshot(tag: str = "manual") -> Path:
    """把当前库拷一份到 backups，返回文件路径。"""
    dest = backup_dir() / f"{BACKUP_PREFIX}{tag}_{_stamp()}.db"
    src = sqlite3.connect(str(db_core.DB_PATH))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _prune()
    return dest


def _prune() -> None:
    files = sorted(backup_dir().glob(f"{BACKUP_PREFIX}*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[MAX_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_backups() -> List[dict]:
    items = []
    for path in sorted(backup_dir().glob(f"{BACKUP_PREFIX}*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, db_core.TZ).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return items


def is_sqlite(data: bytes) -> bool:
    return data[:16] == b"SQLite format 3\x00"


def restore_bytes(data: bytes) -> Path:
    if not is_sqlite(data):
        raise ValueError("这不是 SQLite 备份文件")
    if len(data) < 4096:
        raise ValueError("备份文件太小，不像完整库")
    safety = snapshot("before_restore")
    tmp = backup_dir() / f"_incoming_{_stamp()}.db"
    tmp.write_bytes(data)
    try:
        probe = sqlite3.connect(str(tmp))
        try:
            names = {
                row[0]
                for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = [name for name in REQUIRED_TABLES if name not in names]
            if missing:
                raise ValueError("备份里缺表：" + "、".join(missing))
        finally:
            probe.close()
        dest = Path(db_core.DB_PATH)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(tmp))
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        # 旧备份可能缺新列/表，恢复后补上，避免当场把登录打挂
        with db_core.get_db() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            had_flag = "must_change_pin" in cols
            db_core._ensure_user_columns(conn)
            db_core._ensure_login_attempts(conn)
            if not had_flag:
                db_core._add_must_change_pin(conn)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return safety


def restore_named(name: str) -> Path:
    safe = Path(name).name
    if not safe.startswith(BACKUP_PREFIX) or not safe.endswith(".db"):
        raise ValueError("备份文件名不对")
    path = backup_dir() / safe
    if not path.is_file():
        raise ValueError("找不到这份备份")
    return restore_bytes(path.read_bytes())
