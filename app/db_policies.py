"""政策说明：正文、版本历史、已读记录。"""

from __future__ import annotations

import html
import re
import sqlite3
from typing import Any, Dict, List, Optional

import nh3

from .db_core import _now

POLICY_REQUIRE_KEY = "policy_require_read"
ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "strike", "del",
    "ul", "ol", "li", "h2", "h3", "h4", "blockquote", "span", "div", "a",
    "table", "thead", "tbody", "tr", "th", "td",
    "ins", "img", "sub", "sup",
}
_POLICY_ADD_CLASS = {"policy-add", "policy-del"}

# 允许的内联样式属性 → 值校验正则（防 CSS 注入）
_STYLE_WHITELIST = {
    "font-size": re.compile(r"^\d{1,3}(\.\d+)?(px|em|pt|rem|%)$"),
    "color": re.compile(r"^(#[0-9a-fA-F]{3,8}|rgb\(\d{1,3},\s*\d{1,3},\s*\d{1,3}\)|[a-zA-Z]+)$"),
    "background-color": re.compile(r"^(#[0-9a-fA-F]{3,8}|rgb\(\d{1,3},\s*\d{1,3},\s*\d{1,3}\)|[a-zA-Z]+)$"),
    "text-align": re.compile(r"^(left|right|center|justify)$"),
    "line-height": re.compile(r"^\d{1,3}(\.\d+)?(px|em|%)?$"),
    "margin-left": re.compile(r"^\d{1,4}(\.\d+)?(px|em|rem|%)$"),
    "text-indent": re.compile(r"^\d{1,4}(\.\d+)?(px|em|rem|%)$"),
    "width": re.compile(r"^\d{1,4}(\.\d+)?(px|em|rem|%)$"),
    "height": re.compile(r"^\d{1,4}(\.\d+)?(px|em|rem|%)$"),
}
_NH3_ATTRIBUTES = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "span": {"class", "style"},
    "p": {"style"},
    "div": {"style"},
    "h2": {"style"},
    "h3": {"style"},
    "h4": {"style"},
    "blockquote": {"style"},
    "li": {"style"},
    "table": {"style"},
    "th": {"colspan", "rowspan", "align", "style"},
    "td": {"colspan", "rowspan", "align", "style"},
}


def _safe_style(raw: str) -> str:
    """只保留白名单内的内联样式，其余丢弃。返回干净的 style 串（可能为空）。"""
    kept = []
    for part in (raw or "").split(";"):
        if ":" not in part:
            continue
        prop, _, val = part.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        rule = _STYLE_WHITELIST.get(prop)
        if rule and rule.match(val):
            kept.append(f"{prop}: {val}")
    return "; ".join(kept)


def _safe_href(raw: str) -> Optional[str]:
    value = (raw or "").strip()
    if not value or "\\" in value or "\n" in value or "\r" in value:
        return None
    if value.startswith("//") or value.lower().startswith("javascript:"):
        return None
    if value.startswith("https://"):
        return value
    if value.startswith("/") and not value.startswith("//"):
        return value
    return None


def _safe_img_src(raw: str) -> Optional[str]:
    value = (raw or "").strip()
    if not value or ".." in value or "\\" in value:
        return None
    if value.startswith("/uploads/policy/") and "//" not in value[1:]:
        return value
    if value.startswith("https://"):
        return value
    return None


def _nh3_attribute_filter(tag: str, attr: str, value: str) -> Optional[str]:
    tag = (tag or "").lower()
    attr = (attr or "").lower()
    if attr == "href":
        return _safe_href(value)
    if attr == "src":
        return _safe_img_src(value)
    if attr == "style":
        return _safe_style(value) or None
    if attr == "class" and tag == "span":
        keep = [c for c in (value or "").split() if c in _POLICY_ADD_CLASS]
        return " ".join(keep) or None
    if attr in {"width", "height", "colspan", "rowspan"}:
        return value if str(value or "").isdigit() else None
    if attr == "align":
        text = (value or "").lower()
        return text if text in {"left", "center", "right"} else None
    if attr == "alt":
        return value
    return None


def _inline_md(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"\[([^\]]+)\]\((https://[^)\s]+)\)", r'<a href="\2">\1</a>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", out)
    return out


def render_policy_markdown(raw: str) -> str:
    """很小的 Markdown：标题、列表、粗斜体、链接、换行。"""
    lines = (raw or "").replace("\r\n", "\n").split("\n")
    blocks: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("### "):
            blocks.append(f"<h4>{_inline_md(line[4:])}</h4>")
            i += 1
            continue
        if line.startswith("## "):
            blocks.append(f"<h3>{_inline_md(line[3:])}</h3>")
            i += 1
            continue
        if line.startswith("# "):
            blocks.append(f"<h2>{_inline_md(line[2:])}</h2>")
            i += 1
            continue
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ")):
            items = []
            while i < n and lines[i].lstrip().startswith(("- ", "* ")):
                items.append("<li>" + _inline_md(lines[i].lstrip()[2:]) + "</li>")
                i += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"\d+\.\s", stripped):
            items = []
            while i < n and re.match(r"\d+\.\s", lines[i].lstrip()):
                items.append("<li>" + _inline_md(re.sub(r"^\d+\.\s", "", lines[i].lstrip())) + "</li>")
                i += 1
            blocks.append("<ol>" + "".join(items) + "</ol>")
            continue
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not lines[i].startswith(("# ", "## ", "### ")):
            nxt = lines[i].lstrip()
            if nxt.startswith(("- ", "* ")) or re.match(r"\d+\.\s", nxt):
                break
            para.append(lines[i])
            i += 1
        blocks.append("<p>" + "<br>".join(_inline_md(p) for p in para) + "</p>")
    return "".join(blocks)


def sanitize_policy_html(raw: str) -> str:
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    # 已经是我们存过的 HTML 就只消毒；否则当 Markdown 转
    if re.match(r"<(p|h[2-4]|ul|ol|div|blockquote|table)\b", text, re.I):
        html_out = text
    else:
        html_out = render_policy_markdown(text)
    try:
        out = nh3.clean(
            html_out,
            tags=ALLOWED_TAGS,
            attributes=_NH3_ATTRIBUTES,
            attribute_filter=_nh3_attribute_filter,
            url_schemes={"https"},
            link_rel="noopener noreferrer",
            strip_comments=True,
        )
    except Exception:
        return html.escape(re.sub(r"<[^>]+>", "", text)).replace("\n", "<br>")
    out = re.sub(r"<img(?![^>]*\bsrc=)[^>]*>", "", out, flags=re.I)
    return re.sub(r"(?:<br\s*/?>\s*){3,}", "<br><br>", out)


def _plain_chunks(raw: str, size: int = 40) -> List[str]:
    """去标签去空格后按固定长度切成块。

    用块而非单字做 diff，DP 的 O(n·m) 时间和内存被压到文本长度/块大小，
    避免巨型政策把 /policies 打到卡死或 OOM。块内不再细分，标注落在改动块上。
    """
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", "", text)
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _diff_ops(old: str, new: str) -> List[tuple]:
    a, b = _plain_chunks(old), _plain_chunks(new)
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    i, j = n, m
    rev: List[tuple] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1] and dp[i][j] == dp[i - 1][j - 1]:
            rev.append(("=", b[j - 1]))
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            rev.append(("~", a[i - 1], b[j - 1]))
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            rev.append(("-", a[i - 1]))
            i -= 1
        else:
            rev.append(("+", b[j - 1]))
            j -= 1
    rev.reverse()
    return rev


def render_policy_diff(old_html: str, new_html: str) -> str:
    """对照上一版：删除划线，新增绿色高亮。"""
    if not (old_html or "").strip():
        return new_html or ""
    ops = _diff_ops(old_html, new_html)
    chunks: List[str] = []
    buf = ""
    kind = ""

    def flush() -> None:
        nonlocal buf, kind
        if not buf:
            return
        esc = html.escape(buf)
        if kind == "+":
            chunks.append(f'<span class="policy-add">{esc}</span>')
        elif kind == "-":
            chunks.append(f'<del class="policy-del">{esc}</del>')
        else:
            chunks.append(esc)
        buf = ""
        kind = ""

    for op in ops:
        if op[0] == "=":
            if kind != "=":
                flush()
                kind = "="
            buf += op[1]
        elif op[0] == "+":
            if kind != "+":
                flush()
                kind = "+"
            buf += op[1]
        elif op[0] == "-":
            if kind != "-":
                flush()
                kind = "-"
            buf += op[1]
        else:
            if kind != "-":
                flush()
                kind = "-"
            buf += op[1]
            flush()
            kind = "+"
            buf += op[2]
    flush()
    return "<p>" + "".join(chunks) + "</p>"


def previous_revision_body(conn: sqlite3.Connection, policy_id: int, version: int) -> str:
    row = conn.execute(
        "SELECT body FROM policy_revisions WHERE policy_id=? AND version<? ORDER BY version DESC LIMIT 1",
        (int(policy_id), int(version)),
    ).fetchone()
    return (row["body"] if row else "") or ""


def _ensure_policy_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by INTEGER REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            policy_id INTEGER NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            edited_at TEXT NOT NULL,
            edited_by INTEGER REFERENCES users(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_policy_revisions_policy ON policy_revisions(policy_id, version DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_acks (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            policy_id INTEGER NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY (user_id, policy_id)
        )
        """
    )


def policy_require_read(conn: sqlite3.Connection) -> bool:
    from .db_core import hot_settings

    return (hot_settings(conn).get(POLICY_REQUIRE_KEY) or "0") == "1"


def set_policy_require_read(conn: sqlite3.Connection, on: bool) -> None:
    from .db_core import set_setting

    set_setting(conn, POLICY_REQUIRE_KEY, "1" if on else "0")


def _row(row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "title": row["title"] or "",
        "body": row["body"] or "",
        "sort_order": int(row["sort_order"] or 0),
        "active": int(row["active"] or 0),
        "version": int(row["version"] or 1),
        "updated_at": row["updated_at"] or "",
        "updated_by": row["updated_by"],
    }


def list_policies(conn: sqlite3.Connection, *, active_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM policies"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY sort_order, id"
    return [_row(r) for r in conn.execute(sql)]


def get_policy(conn: sqlite3.Connection, policy_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM policies WHERE id=?", (int(policy_id),)).fetchone()
    return _row(row) if row else None


def save_policy(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: str,
    sort_order: int = 0,
    active: bool = True,
    user_id: int = 0,
    policy_id: Optional[int] = None,
) -> int:
    title = (title or "").strip()[:80]
    if not title:
        raise ValueError("标题不能空")
    body = sanitize_policy_html(body)[:20000]
    now = _now()
    if policy_id:
        old = get_policy(conn, policy_id)
        if old is None:
            raise ValueError("没有这条政策")
        changed = old["title"] != title or old["body"] != body
        version = int(old["version"] or 1) + (1 if changed else 0)
        conn.execute(
            """
            UPDATE policies
            SET title=?, body=?, sort_order=?, active=?, version=?, updated_at=?, updated_by=?
            WHERE id=?
            """,
            (title, body, int(sort_order or 0), 1 if active else 0, version, now, user_id or None, int(policy_id)),
        )
        if changed:
            conn.execute(
                """
                INSERT INTO policy_revisions(policy_id, version, title, body, edited_at, edited_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(policy_id), version, title, body, now, user_id or None),
            )
        return int(policy_id)
    cur = conn.execute(
        """
        INSERT INTO policies(title, body, sort_order, active, version, updated_at, updated_by)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (title, body, int(sort_order or 0), 1 if active else 0, now, user_id or None),
    )
    pid = int(cur.lastrowid or 0)
    conn.execute(
        """
        INSERT INTO policy_revisions(policy_id, version, title, body, edited_at, edited_by)
        VALUES (?, 1, ?, ?, ?, ?)
        """,
        (pid, title, body, now, user_id or None),
    )
    return pid


def set_policy_active(conn: sqlite3.Connection, policy_id: int, active: bool) -> None:
    conn.execute(
        "UPDATE policies SET active=?, updated_at=? WHERE id=?",
        (1 if active else 0, _now(), int(policy_id)),
    )


def delete_policy(conn: sqlite3.Connection, policy_id: int) -> None:
    conn.execute("DELETE FROM policy_acks WHERE policy_id=?", (int(policy_id),))
    conn.execute("DELETE FROM policy_revisions WHERE policy_id=?", (int(policy_id),))
    conn.execute("DELETE FROM policies WHERE id=?", (int(policy_id),))


def list_revisions(conn: sqlite3.Connection, policy_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.version, r.title, r.body, r.edited_at, u.display_name
        FROM policy_revisions r
        LEFT JOIN users u ON u.id = r.edited_by
        WHERE r.policy_id=?
        ORDER BY r.version DESC
        """,
        (int(policy_id),),
    )
    return [
        {
            "version": int(r["version"]),
            "title": r["title"] or "",
            "body": r["body"] or "",
            "edited_at": r["edited_at"] or "",
            "editor": r["display_name"] or "",
        }
        for r in rows
    ]


def restore_policy_revision(
    conn: sqlite3.Connection, policy_id: int, version: int, *, user_id: int = 0
) -> None:
    row = conn.execute(
        "SELECT title, body FROM policy_revisions WHERE policy_id=? AND version=?",
        (int(policy_id), int(version)),
    ).fetchone()
    if row is None:
        raise ValueError("没有这个历史版本")
    save_policy(
        conn,
        title=row["title"],
        body=row["body"],
        sort_order=(get_policy(conn, policy_id) or {}).get("sort_order", 0),
        active=bool((get_policy(conn, policy_id) or {}).get("active", 1)),
        user_id=user_id,
        policy_id=int(policy_id),
    )


def ack_map(conn: sqlite3.Connection, user_id: int) -> Dict[int, int]:
    return {
        int(r["policy_id"]): int(r["version"])
        for r in conn.execute(
            "SELECT policy_id, version FROM policy_acks WHERE user_id=?",
            (int(user_id),),
        )
    }


def unread_policies(conn: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
    acks = ack_map(conn, user_id)
    out = []
    for item in list_policies(conn, active_only=True):
        if int(acks.get(item["id"], 0)) < int(item["version"]):
            out.append(item)
    return out


def mark_policy_read(conn: sqlite3.Connection, user_id: int, policy_id: int) -> None:
    item = get_policy(conn, policy_id)
    if item is None or not item["active"]:
        raise ValueError("没有这条政策")
    conn.execute(
        """
        INSERT INTO policy_acks(user_id, policy_id, version, read_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, policy_id) DO UPDATE SET version=excluded.version, read_at=excluded.read_at
        """,
        (int(user_id), int(policy_id), int(item["version"]), _now()),
    )


def policy_read_status(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """每项政策的已读统计：目标人数、已读人数、未读用户名单。

    读 = 该用户的 ack 版本 >= 政策当前版本。目标 = 全部可登录用户。
    """
    from .db_core import list_users

    policies = list_policies(conn, active_only=True)
    # 键 = (policy_id, user_id)，否则多用户同策会互相覆盖
    acks = {
        (int(r["policy_id"]), int(r["user_id"])): int(r["version"])
        for r in conn.execute("SELECT user_id, policy_id, version FROM policy_acks")
    }
    users = list_users(conn)
    active = [u for u in users if bool(int(u["active"] or 0))]
    rows = []
    for p in policies:
        pid = int(p["id"])
        pv = int(p["version"])
        read = sum(1 for u in active if int(acks.get((pid, u["id"]), 0)) >= pv)
        unread = [
            _user_label(u)
            for u in active
            if int(acks.get((pid, u["id"]), 0)) < pv
        ]
        rows.append(
            {
                "id": pid,
                "title": p["title"] or "",
                "version": pv,
                "total": len(active),
                "read": read,
                "pct": round(read / len(active) * 100) if active else 0,
                "unread": unread,
            }
        )
    return rows


def _user_label(u) -> str:
    name = (u["display_name"] or "").strip()
    return name or (u["username"] or "")

