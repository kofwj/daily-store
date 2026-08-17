#!/usr/bin/env python3
"""机外备份脏检查：库 / WAL / .env 没变就不用再拷。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

FP_NAME = ".last_offsite_fp"


def _stat_part(path: Path) -> str:
    if not path.is_file():
        return f"{path.name}:0:0"
    st = path.stat()
    return f"{path.name}:{st.st_size}:{int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000)))}"


def fingerprint(db_path: Path, env_path: Path) -> str:
    parts = [_stat_part(db_path), _stat_part(Path(str(db_path) + "-wal")), _stat_part(Path(str(db_path) + "-shm"))]
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            data_ver = con.execute("PRAGMA data_version").fetchone()[0]
            page_count = con.execute("PRAGMA page_count").fetchone()[0]
            parts.append(f"dv:{data_ver}:pc:{page_count}")
        finally:
            con.close()
    except sqlite3.Error:
        parts.append("dv:0:pc:0")
    parts.append(_stat_part(env_path))
    return "|".join(parts)


def fp_file(primary_dir: Path) -> Path:
    return primary_dir / "data" / "backups" / FP_NAME


def read_last(primary_dir: Path) -> str:
    dest = fp_file(primary_dir)
    if not dest.is_file():
        return ""
    return dest.read_text(encoding="utf-8").strip()


def write_last(primary_dir: Path, value: str) -> None:
    dest = fp_file(primary_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(value.strip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="机外备份脏检查")
    parser.add_argument("action", choices=("show", "check", "save"))
    parser.add_argument("--dir", required=True, help="生产目录，例如 /opt/store-daily")
    args = parser.parse_args()
    primary = Path(args.dir)
    db_path = primary / "data" / "store_daily.db"
    env_path = primary / ".env"
    now = fingerprint(db_path, env_path)
    if args.action == "show":
        print(now)
        return 0
    if args.action == "check":
        last = read_last(primary)
        print("SKIP" if last and last == now else "COPY")
        return 0
    write_last(primary, now)
    print(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
