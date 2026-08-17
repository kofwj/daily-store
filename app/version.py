"""当前构建版本：环境变量 > VERSION 文件 > git 短哈希。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _clean(text: str) -> str:
    return (text or "").strip()


def _from_env() -> str:
    return _clean(os.environ.get("STORE_DAILY_VERSION", ""))


def _version_file() -> Path:
    return ROOT / "VERSION"


def _from_file() -> tuple[str, str, str]:
    path = _version_file()
    if not path.is_file():
        return "", "", ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", "", ""
    version = _clean(lines[0]) if lines else ""
    built = _clean(lines[1]) if len(lines) > 1 else ""
    summary = _clean(lines[2]) if len(lines) > 2 else ""
    return version, built, summary


def _from_git() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        return _clean(out)
    except (OSError, subprocess.SubprocessError):
        return ""


def _display(raw: str) -> str:
    text = _clean(raw)
    if not text or text in {"dev", "unknown"}:
        return "V0.0.0"
    if text[0] in "Vv" and "." in text:
        return text if text.startswith("V") else f"V{text[1:]}"
    return text


def current() -> dict:
    file_ver, built, summary = _from_file()
    version = _display(_from_env() or file_ver or "V0.0.0")
    return {
        "version": version,
        "git": _from_git(),
        "built_at": built,
        "summary": summary,
    }
