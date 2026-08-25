"""SQLite 备份 / 恢复。备份文件放在 data/backups。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import List, Optional

from . import db_core

BACKUP_DIR_NAME = "backups"
BACKUP_PREFIX = "store_daily_"
MAX_KEEP = 20
REQUIRED_TABLES = ("stores", "users", "metrics", "daily_reports", "daily_facts", "advance_posts")
MAX_RESTORE_BYTES = 32 * 1024 * 1024
FINGERPRINT_NAME = ".last_offsite_fp"


def backup_dir() -> Path:
    path = Path(db_core.DATA_DIR) / BACKUP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stat_part(path: Path) -> str:
    if not path.is_file():
        return f"{path.name}:0:0"
    st = path.stat()
    return f"{path.name}:{st.st_size}:{int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000)))}"


def live_fingerprint(db_path: Optional[Path] = None, env_path: Optional[Path] = None) -> str:
    """库 + WAL + .env 的轻量指纹。有写入就会变，用来跳过重复机外拷贝。"""
    path = Path(db_path or db_core.DB_PATH)
    parts = [_stat_part(path), _stat_part(Path(str(path) + "-wal")), _stat_part(Path(str(path) + "-shm"))]
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            data_ver = con.execute("PRAGMA data_version").fetchone()[0]
            page_count = con.execute("PRAGMA page_count").fetchone()[0]
            parts.append(f"dv:{data_ver}:pc:{page_count}")
        finally:
            con.close()
    except sqlite3.Error:
        parts.append("dv:0:pc:0")
    env = Path(env_path) if env_path is not None else path.parent.parent / ".env"
    if env_path is None and not env.is_file():
        env = path.parent / ".env"
    parts.append(_stat_part(env))
    return "|".join(parts)


def fingerprint_path() -> Path:
    return backup_dir() / FINGERPRINT_NAME


def last_offsite_fingerprint() -> str:
    dest = fingerprint_path()
    if not dest.is_file():
        return ""
    return dest.read_text(encoding="utf-8").strip()


def save_offsite_fingerprint(value: str) -> None:
    dest = fingerprint_path()
    dest.write_text(value.strip() + "\n", encoding="utf-8")


def is_offsite_clean(db_path: Optional[Path] = None, env_path: Optional[Path] = None) -> bool:
    last = last_offsite_fingerprint()
    return bool(last) and last == live_fingerprint(db_path, env_path)


def _stamp() -> str:
    return datetime.now(db_core.TZ).strftime("%Y%m%d_%H%M%S")


def snapshot(tag: str = "manual") -> Path:
    """把当前库拷一份到 backups，返回文件路径。

    snapshot 会先 _prune 一次，保证总数不超过 MAX_KEEP。
    """
    dest = backup_dir() / f"{BACKUP_PREFIX}{tag}_{_stamp()}_{token_hex(4)}.db"
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


def prune() -> None:
    """启动时清一次，把每天/异机写进来的备份压回 MAX_KEEP 份。"""
    _prune()


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
    if len(data) > MAX_RESTORE_BYTES:
        raise ValueError("备份文件不能超过 32 MiB")
    if not is_sqlite(data):
        raise ValueError("这不是 SQLite 备份文件")
    if len(data) < 4096:
        raise ValueError("备份文件太小，不像完整库")
    tmp = backup_dir() / f"_incoming_{_stamp()}_{token_hex(8)}.db"
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
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份完整性校验失败")
            required_columns = {"users": {"id", "username", "pin_hash", "role"}, "advance_posts": {"id", "store_id", "biz_date", "paid"}}
            for table, columns in required_columns.items():
                actual = {row[1] for row in probe.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise ValueError(f"备份表结构不完整：{table}")
        finally:
            probe.close()
        # Validation is complete: only now preserve live state and replace it.
        safety = snapshot("before_restore")
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
            db_core._ensure_auth_events(conn)
            db_core._ensure_advance_edits(conn)
            db_core._ensure_advance_sesame(conn)
            from .db_invoice import _ensure_invoice_tables
            from .db_policies import _ensure_policy_tables

            _ensure_invoice_tables(conn)
            _ensure_policy_tables(conn)
            if not had_flag:
                db_core._add_must_change_pin(conn)
        # 旧备份可能停在更早的 schema 版本，恢复后再补索引/金额单位
        db_core.migrate()
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
