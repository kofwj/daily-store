"""门店、用户实体 CRUD 与权限映射。"""

from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Optional, Set

from .db_auth import hash_pin, is_weak_new_pin
from .db_core import _now


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    # COLLATE NOCASE 与登录锁定计数的 lower 口径一致：大小写打错也在打同一个账号的失败额度，
    # 不会出现"Admin 认证失败却把 admin 刷锁定"的错位
    return conn.execute(
        "SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1",
        (username.strip(),),
    ).fetchone()


def list_user_stores(conn: sqlite3.Connection, user: sqlite3.Row) -> List[sqlite3.Row]:
    if user["role"] == "admin":
        return list(conn.execute("SELECT * FROM stores WHERE active=1 ORDER BY sort_order, id"))
    if user["role"] == "city":
        # 地市负责人：看本地市（scope=地市名）的启用门店
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if not scope:
            return []
        return list(
            conn.execute(
                "SELECT * FROM stores WHERE city=? AND active=1 ORDER BY sort_order, id",
                (scope,),
            )
        )
    if user["role"] == "readonly":
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if scope:
            # 区域经理：看同一区域经理名下的店
            return list(
                conn.execute(
                    "SELECT * FROM stores WHERE area_manager=? AND active=1 ORDER BY sort_order, id",
                    (scope,),
                )
            )
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
    if user["role"] == "city":
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if not scope:
            return False
        row = conn.execute(
            "SELECT 1 FROM stores WHERE id=? AND city=? AND active=1",
            (store_id, scope),
        ).fetchone()
        return row is not None
    if user["role"] == "readonly":
        scope = (user["scope"] or "").strip() if "scope" in user.keys() else ""
        if scope:
            row = conn.execute(
                "SELECT 1 FROM stores WHERE id=? AND area_manager=? AND active=1",
                (store_id, scope),
            ).fetchone()
            return row is not None
    row = conn.execute(
        "SELECT 1 FROM user_stores WHERE user_id=? AND store_id=?",
        (user["id"], store_id),
    ).fetchone()
    return row is not None


def get_store(conn: sqlite3.Connection, store_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()


def list_users(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM users ORDER BY id"))


def get_user(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def list_all_stores(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM stores ORDER BY sort_order, id"))


def active_store_ids(conn: sqlite3.Connection) -> Set[int]:
    """返回当前所有已启用门店的 ID 集合。"""
    return {int(row["id"]) for row in conn.execute("SELECT id FROM stores WHERE active=1")}


def active_area_managers(conn: sqlite3.Connection) -> Set[str]:
    """返回当前所有已启用门店的有效区域经理姓名集合。"""
    return {
        (row["area_manager"] or "").strip()
        for row in conn.execute("SELECT area_manager FROM stores WHERE active=1")
        if (row["area_manager"] or "").strip()
    }


def system_entity_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """系统设置页运行状态统计：全部门店数与非管理员用户数。"""
    stores_count = int(conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0])
    users_count = int(conn.execute("SELECT COUNT(*) FROM users WHERE role!='admin'").fetchone()[0])
    return {"stores": stores_count, "users": users_count}


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
    store_grade: str = "A",
    ai_target: int = 10,
    invoice_name: str = "",
    lease_area: str = "",
    lease_address: str = "",
    lease_period: str = "",
) -> int:
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM stores").fetchone()["n"]
    store_code = (code or "").strip() or alloc_store_code(conn)
    conn.execute(
        """
        INSERT INTO stores(
            name, code, sort_order, region_group, city, mobile_code,
            area_manager, store_manager, follow_ai, follow_bisuan, has_advisor, advisor_name, short_name,
            store_grade, ai_target, invoice_name, lease_area, lease_address, lease_period, active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
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
            (store_grade or "A").strip().upper()[:1] or "A",
            max(0, int(ai_target or 10)),
            (invoice_name or "").strip(),
            (lease_area or "").strip(),
            (lease_address or "").strip(),
            (lease_period or "").strip(),
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
    store_grade: Optional[str] = None,
    ai_target: Optional[int] = None,
    invoice_name: Optional[str] = None,
    lease_area: Optional[str] = None,
    lease_address: Optional[str] = None,
    lease_period: Optional[str] = None,
) -> None:
    name = (advisor_name or "").strip()
    row = conn.execute(
        "SELECT region_group, city, store_grade, ai_target, invoice_name, lease_area, lease_address, lease_period FROM stores WHERE id=?",
        (store_id,),
    ).fetchone()
    if row is None:
        raise ValueError("没有这家店")
    next_region = (region_group if region_group is not None else row["region_group"] or "").strip() or "通泰"
    next_city = (city if city is not None else row["city"] or "").strip() or "南通市"
    next_grade = (
        (store_grade if store_grade is not None else row["store_grade"] or "A")
        .strip()
        .upper()[:1]
        or "A"
    )
    if next_grade not in ("A", "B"):
        next_grade = "A"
    if ai_target is None:
        next_ai = int(row["ai_target"] or 10)
    else:
        next_ai = max(0, int(ai_target))
    next_invoice = invoice_name if invoice_name is not None else (row["invoice_name"] or "")
    next_area = lease_area if lease_area is not None else (row["lease_area"] or "")
    next_addr = lease_address if lease_address is not None else (row["lease_address"] or "")
    next_period = lease_period if lease_period is not None else (row["lease_period"] or "")
    conn.execute(
        """
        UPDATE stores SET
            mobile_code=?, area_manager=?, store_manager=?,
            advisor_name=?, has_advisor=?, region_group=?, city=?,
            store_grade=?, ai_target=?, invoice_name=?, lease_area=?, lease_address=?, lease_period=?
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
            next_grade,
            next_ai,
            (next_invoice or "").strip(),
            (next_area or "").strip(),
            (next_addr or "").strip(),
            (next_period or "").strip(),
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
    scope: str = "",
) -> int:
    if role not in ("admin", "filler", "readonly", "city"):
        raise ValueError("role")
    name = username.strip()
    dup = conn.execute(
        "SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (name,)
    ).fetchone()
    if dup:
        # 登录按不分大小写匹配，重名（含只差大小写）会让身份含糊，建号时就挡住
        raise ValueError("用户名已存在（不分大小写）")
    must_change = 1 if is_weak_new_pin(pin) else 0
    conn.execute(
        """
        INSERT INTO users(username, display_name, pin_hash, role, scope, must_change_pin, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (name, display_name.strip(), hash_pin(pin), role, scope.strip(), must_change, _now()),
    )
    user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    for store_id in store_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
            (user_id, int(store_id)),
        )
    return user_id


def set_user_scope(conn: sqlite3.Connection, user_id: int, scope: str) -> None:
    conn.execute("UPDATE users SET scope=? WHERE id=?", (scope.strip(), user_id))


def update_user_pin(conn: sqlite3.Connection, user_id: int, pin: str) -> int:
    """改口令并自增会话纪元：该用户所有旧会话立即失效。

    返回新纪元值。改自己口令的调用方要把当前 session 的纪元同步刷新，
    否则会把自己也登出。
    """
    must_change = 1 if is_weak_new_pin(pin) else 0
    conn.execute(
        "UPDATE users SET pin_hash=?, must_change_pin=?, session_epoch=session_epoch+1 WHERE id=?",
        (hash_pin(pin), must_change, user_id),
    )
    row = conn.execute("SELECT session_epoch FROM users WHERE id=?", (user_id,)).fetchone()
    return int(row["session_epoch"] or 0)


def set_user_stores(conn: sqlite3.Connection, user_id: int, store_ids: Iterable[int]) -> None:
    conn.execute("DELETE FROM user_stores WHERE user_id=?", (user_id,))
    for store_id in store_ids:
        conn.execute(
            "INSERT OR IGNORE INTO user_stores(user_id, store_id) VALUES (?, ?)",
            (user_id, int(store_id)),
        )


def set_user_active(conn: sqlite3.Connection, user_id: int, active: bool) -> None:
    conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))


def set_store_active(conn: sqlite3.Connection, store_id: int, active: bool) -> None:
    conn.execute("UPDATE stores SET active=? WHERE id=?", (1 if active else 0, int(store_id)))


def store_has_data(conn: sqlite3.Connection, store_id: int) -> bool:
    sid = int(store_id)
    tables = (
        "daily_facts",
        "daily_reports",
        "deal_posts",
        "advance_posts",
        "invoice_months",
        "bisuan_mobile",
        # 辅助登记同样引用门店；有登记时必须保留门店档案
        "aux_queries",
        # 审计表也引用 stores(id) 且无级联，删业务行后审计行仍在，漏查会让 delete_store 撞外键
        "report_edits",
        "deal_edits",
        "advance_edits",
        "invoice_edits",
    )
    for table in tables:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        if conn.execute(f"SELECT 1 FROM {table} WHERE store_id=? LIMIT 1", (sid,)).fetchone():
            return True
    return False


def delete_store(conn: sqlite3.Connection, store_id: int) -> None:
    sid = int(store_id)
    if store_has_data(conn, sid):
        raise ValueError("这家店已有填报/触客/垫资，不能删，只能停用")
    conn.execute("DELETE FROM user_stores WHERE store_id=?", (sid,))
    conn.execute("DELETE FROM stores WHERE id=?", (sid,))
