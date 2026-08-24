"""政策说明：正文、版本历史、已读记录。"""

from __future__ import annotations

import html
import re
import sqlite3
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from .db_core import _now

POLICY_REQUIRE_KEY = "policy_require_read"
ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li",
    "h2", "h3", "h4", "blockquote", "span", "div", "a",
}
VOID_TAGS = {"br"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            return
        if tag == "a":
            href = ""
            for key, val in attrs:
                if key.lower() == "href" and val:
                    raw = val.strip()
                    if raw.startswith(("http://", "https://", "/")):
                        href = html.escape(raw, quote=True)
            self.out.append(f'<a href="{href}" target="_blank" rel="noopener">' if href else "<a>")
            return
        self.out.append(f"<{tag}>")
        if tag in VOID_TAGS:
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")


def _inline_md(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', out)
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
    if re.match(r"<(p|h[2-4]|ul|ol|div|blockquote)\b", text, re.I):
        html_out = text
    else:
        html_out = render_policy_markdown(text)
    parser = _Sanitizer()
    try:
        parser.feed(html_out)
        parser.close()
    except Exception:
        return html.escape(re.sub(r"<[^>]+>", "", text)).replace("\n", "<br>")
    out = "".join(parser.out)
    return re.sub(r"(?:<br\s*/?>\s*){3,}", "<br><br>", out)


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
    from .db_core import get_setting

    return get_setting(conn, POLICY_REQUIRE_KEY, "0") == "1"


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
    pid = int(cur.lastrowid)
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
