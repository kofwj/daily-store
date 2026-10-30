"""门店、用户实体 CRUD 与权限映射。"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Set

from .db_auth import hash_pin, is_weak_new_pin
from .db_core import _now, today_local


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


def _resolve_v5_link(
    conn: sqlite3.Connection, store_id: int, mode_raw: Any, parent_raw: Any
) -> Any:
    """把表单里的 v5_mode / affiliated_to 归一成 (模式, 运营商范围, 母店 id)。

    挂靠店的铁律：必须挂一家在跑的独立主力店（顾问店或主力店），并跟随母店的
    运营商范围——因为落单用的是母店的渠道工号；自己挂自己、挂到另一家挂靠店、
    挂到加盟店都会被挡下来。
    """
    from . import v5_baseline

    mode = v5_baseline.normalize_mode(mode_raw)
    if mode != v5_baseline.MODE_AFFILIATED:
        # 非挂靠模式：母店指针一律清空，运营商范围按模式定（电信单店→TELECOM）
        return mode, v5_baseline.carrier_for_mode(mode), None
    try:
        parent_id = int(parent_raw or 0)
    except (TypeError, ValueError):
        parent_id = 0
    if not parent_id:
        raise ValueError("挂靠小店必须选一家母店")
    if parent_id == int(store_id):
        raise ValueError("不能把店挂到自己名下")
    parent = conn.execute(
        "SELECT id, name, short_name, v5_mode, carrier_scope FROM stores WHERE id=?",
        (parent_id,),
    ).fetchone()
    if parent is None:
        raise ValueError("母店不存在")
    if not v5_baseline.can_be_parent(parent["v5_mode"]):
        raise ValueError("母店必须是独立主力店（顾问店或主力店），加盟店和挂靠店不能当母店")
    scope = str(parent["carrier_scope"] or "").strip().upper() or "MOBILE"
    return mode, scope, parent_id


def sync_store_v5_baseline(conn: sqlite3.Connection, store_id: int) -> None:
    """按门店当前 v5_mode 同步 `store_baseline` 里 V5 起始月那一行。

    只动 `from_month = 2026-11` 这一行：更早的月份一行都不写（双轨期不回溯、
    不重算），非主控/停用模式则把这一行删掉（不进提成与考核）。
    """
    from . import v5_baseline

    if not _table_exists(conn, "store_baseline"):
        return
    store = conn.execute(
        "SELECT v5_mode, carrier_scope, affiliated_to FROM stores WHERE id=?",
        (int(store_id),),
    ).fetchone()
    if store is None:
        return
    mode = v5_baseline.normalize_mode(store["v5_mode"])
    if mode in v5_baseline.DISABLED_MODES:
        conn.execute(
            "DELETE FROM store_baseline WHERE store_id=? AND from_month=?",
            (int(store_id), v5_baseline.V5_START_MONTH),
        )
        return
    scope = str(store["carrier_scope"] or "").strip().upper() or "MOBILE"
    tiers = v5_baseline.CARRIER_CARRY_TIERS.get(scope, v5_baseline.CARRIER_CARRY_TIERS["MOBILE"])
    tier_25, tier_18, tier_10 = tiers
    existing = conn.execute(
        "SELECT net_baseline_cents FROM store_baseline WHERE store_id=? AND from_month=?",
        (int(store_id), v5_baseline.V5_START_MONTH),
    ).fetchone()
    # 已有的基线净额是业务数字，重配模式时不能清零
    net = int(existing["net_baseline_cents"] or 0) if existing is not None else 0
    cap_abs = (
        v5_baseline.ADVISOR_CAP_ABS_CENTS
        if mode in (v5_baseline.MODE_ADVISOR_A, v5_baseline.MODE_ADVISOR_B_NEAR)
        else 0
    )
    conn.execute(
        """
        INSERT INTO store_baseline(
            store_id, from_month, to_month, v5_mode, affiliated_to,
            net_baseline_cents, target_carry_rate, carry_tier_25, carry_tier_18,
            carry_tier_10, cap_ratio, cap_abs_cents, created_at
        ) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(store_id, from_month) DO UPDATE SET
            v5_mode=excluded.v5_mode,
            affiliated_to=excluded.affiliated_to,
            target_carry_rate=excluded.target_carry_rate,
            carry_tier_25=excluded.carry_tier_25,
            carry_tier_18=excluded.carry_tier_18,
            carry_tier_10=excluded.carry_tier_10,
            cap_ratio=excluded.cap_ratio,
            cap_abs_cents=excluded.cap_abs_cents
        """,
        (
            int(store_id),
            v5_baseline.V5_START_MONTH,
            mode,
            store["affiliated_to"],
            net,
            tier_25,
            tier_25,
            tier_18,
            tier_10,
            v5_baseline.DEFAULT_CAP_RATIO,
            cap_abs,
            _now(),
        ),
    )


def affiliate_parent_options(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """可当挂靠母店的门店：在跑的独立主力店（顾问店 / 主力店）。"""
    from . import v5_baseline

    rows = conn.execute(
        "SELECT id, code, name, short_name, city, v5_mode FROM stores WHERE active=1"
    ).fetchall()
    return [row for row in rows if v5_baseline.can_be_parent(row["v5_mode"])]


def store_mode_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """设置页 C10 提示条的数据：按 V5 模式统计门店家数。"""
    from . import v5_baseline

    rows = conn.execute("SELECT id, v5_mode, active FROM stores ORDER BY id").fetchall()
    return v5_baseline.store_mode_summary(rows)


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
    v5_mode: Optional[str] = None,
    affiliated_to: Optional[int] = None,
) -> None:
    name = (advisor_name or "").strip()
    row = conn.execute(
        """
        SELECT region_group, city, store_grade, ai_target, invoice_name,
               lease_area, lease_address, lease_period,
               v5_mode, carrier_scope, affiliated_to
          FROM stores WHERE id=?
        """,
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
    next_mode = str(row["v5_mode"] or "").strip()
    next_scope = str(row["carrier_scope"] or "").strip().upper() or "MOBILE"
    next_parent = row["affiliated_to"]
    if v5_mode is not None:
        # 只有显式提交了 v5_mode 才动 V5 配置：批量改档、建店都不受影响
        next_mode, next_scope, next_parent = _resolve_v5_link(
            conn, store_id, v5_mode, affiliated_to
        )
    next_invoice = invoice_name if invoice_name is not None else (row["invoice_name"] or "")
    next_area = lease_area if lease_area is not None else (row["lease_area"] or "")
    next_addr = lease_address if lease_address is not None else (row["lease_address"] or "")
    next_period = lease_period if lease_period is not None else (row["lease_period"] or "")
    conn.execute(
        """
        UPDATE stores SET
            mobile_code=?, area_manager=?, store_manager=?,
            advisor_name=?, has_advisor=?, region_group=?, city=?,
            store_grade=?, ai_target=?, invoice_name=?, lease_area=?, lease_address=?, lease_period=?,
            v5_mode=?, carrier_scope=?, affiliated_to=?
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
            next_mode,
            next_scope,
            next_parent,
            store_id,
        ),
    )
    if v5_mode is not None:
        sync_store_v5_baseline(conn, store_id)


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
        # 迁移 17 的 V5 表同样引用 stores(id) 且无级联：有 V5 基线/月结行时不能删，
        # 否则 DELETE 直接撞外键，用户看到 500 而不是「只能停用」。
        "store_baseline",
        "advisor_tenure",
        "store_month_biz",
        "store_month_settlement",
        "store_month_split",
        "settlement_audit_log",
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


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """按列名取值；老库还没补列时返回默认值（不让页面 500）。"""
    try:
        value = row[key]
    except IndexError:
        return default
    return default if value is None else value


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _yuan(cents: Any) -> str:
    """分转元展示，整数不带小数尾巴。"""
    try:
        value = int(cents or 0) / 100
    except (TypeError, ValueError):
        value = 0.0
    return f"{value:,.0f}" if abs(value - round(value)) < 0.005 else f"{value:,.2f}"


def _pct(rate: Any) -> str:
    try:
        value = float(rate or 0) * 100
    except (TypeError, ValueError):
        value = 0.0
    return f"{value:g}%"


def store_v5_snapshot(conn: sqlite3.Connection, store_id: int) -> Dict[str, Any]:
    """门店的 V5 只读快照：模式、运营商范围、挂靠母店、考核基线、顾问任期。

    **只读**：本函数只 SELECT，不写任何表；V5 口径的真实事实源是
    `store_baseline` / `advisor_tenure`，`stores` 上四列只是给筛选用的快照。
    2026-10 及以前的报表与算薪不走这里，双轨并行。
    """
    from . import v5_baseline

    store = get_store(conn, store_id)
    if store is None:
        return {}
    mode = str(_row_get(store, "v5_mode", "") or "").strip() or v5_baseline.MODE_UNSET
    carrier = str(_row_get(store, "carrier_scope", "") or "").strip().upper() or "MOBILE"
    snapshot: Dict[str, Any] = {
        "store_id": int(store["id"]),
        "mode": mode,
        "mode_label": v5_baseline.mode_label(mode),
        "in_v5": mode != v5_baseline.MODE_UNSET and mode not in v5_baseline.DISABLED_MODES,
        "disabled": mode in v5_baseline.DISABLED_MODES,
        "carrier": carrier,
        "carrier_label": v5_baseline.carrier_label(carrier),
        "mobile_code_active": bool(int(_row_get(store, "mobile_code_active", 1) or 0)),
        "parent": None,
        "baseline": None,
        "tenures": [],
        "start_month": v5_baseline.V5_START_MONTH,
        "legacy_last_month": v5_baseline.V5_LEGACY_LAST_MONTH,
        "advisor_name": str(_row_get(store, "advisor_name", "") or "").strip(),
    }
    parent_id = _row_get(store, "affiliated_to", None)
    if parent_id:
        parent = conn.execute(
            "SELECT id, code, name, short_name FROM stores WHERE id=?", (int(parent_id),)
        ).fetchone()
        if parent is not None:
            snapshot["parent"] = {
                "id": int(parent["id"]),
                "code": parent["code"] or "",
                "label": (parent["short_name"] or "").strip() or parent["name"] or "",
            }
    if _table_exists(conn, "store_baseline"):
        row = conn.execute(
            "SELECT * FROM store_baseline WHERE store_id=? ORDER BY from_month DESC, id DESC LIMIT 1",
            (int(store_id),),
        ).fetchone()
        if row is not None:
            snapshot["baseline"] = {
                "from_month": row["from_month"],
                "to_month": row["to_month"] or "",
                "range_label": row["from_month"]
                + (f" 至 {row['to_month']}" if row["to_month"] else " 起"),
                "net_baseline": _yuan(row["net_baseline_cents"]),
                "target_carry_rate": _pct(row["target_carry_rate"]),
                "tiers": [
                    ("≥25%", _pct(row["carry_tier_25"])),
                    ("≥18%", _pct(row["carry_tier_18"])),
                    ("≥10%", _pct(row["carry_tier_10"])),
                ],
                "cap_ratio": _pct(row["cap_ratio"]),
                "cap_abs": _yuan(row["cap_abs_cents"]) if row["cap_abs_cents"] else "",
            }
    if _table_exists(conn, "advisor_tenure"):
        for row in conn.execute(
            "SELECT * FROM advisor_tenure WHERE store_id=? ORDER BY from_month DESC, id DESC",
            (int(store_id),),
        ):
            snapshot["tenures"].append(
                {
                    "advisor_name": row["advisor_name"],
                    "tier_grade": row["tier_grade"] or "",
                    "range_label": row["from_month"]
                    + (f" 至 {row['to_month']}" if row["to_month"] else " 起"),
                    "base_coef": f"{float(row['base_coef'] or 0):.2f}",
                    "base_pay": _yuan(row["base_pay_cents"]),
                    "breakeven": _yuan(row["breakeven_cents"]),
                    "excess_rate": _pct(row["excess_rate"]),
                    "work_days": int(row["work_days"] or 0),
                }
            )
    return snapshot


# ---------------------------------------------------------------------------
# V5 表 2：专职顾问任期维护（A4 收口）
# ---------------------------------------------------------------------------


def advisor_snapshot_month(today: Optional[Any] = None) -> str:
    """派生 `stores.advisor_name` 时拿哪个月当「当前在任」的参照月。

    用业务时区的今天，但不早于 V5 生效月：2026-10 及以前是双轨期，旧口径仍靠
    `stores.advisor_name`，不能因为「任期从 11 月起」就把它清空。
    """
    from . import v5_baseline

    key = (today or today_local()).strftime("%Y-%m")
    return max(key, v5_baseline.V5_START_MONTH)


def list_advisor_tenures(conn: sqlite3.Connection, store_id: int) -> List[sqlite3.Row]:
    """一家店的全部任期段，按起始月升序（页面按时间线展示）。"""
    if not _table_exists(conn, "advisor_tenure"):
        return []
    return list(
        conn.execute(
            "SELECT * FROM advisor_tenure WHERE store_id=? ORDER BY from_month, id",
            (int(store_id),),
        )
    )


def tenure_range_label(from_month: str, to_month: str) -> str:
    """任期区间文案：`2026-10 至 2027-02` / `2026-11 起`。"""
    return f"{from_month}" + (f" 至 {to_month}" if to_month else " 起")


def sync_store_advisor_snapshot(
    conn: sqlite3.Connection, store_id: int, month: Any = None
) -> str:
    """以任期表为唯一事实源，反写 `stores.advisor_name` / `has_advisor`。

    返回同步后的顾问姓名（空串 = 该月无人在任）。**一条任期都没有时不动**
    `stores` 的两列：老库或还没配任期的店仍靠手工填写的顾问姓名跑旧口径，
    不能被一次保存误清。
    """
    from . import v5_baseline

    rows = list_advisor_tenures(conn, store_id)
    if not rows:
        return ""
    key = v5_baseline.month_key(month) or advisor_snapshot_month()
    active: Optional[sqlite3.Row] = None
    for row in rows:  # 升序遍历，后命中的起始月更大，即「最近一段」
        start = row["from_month"]
        end = row["to_month"] or ""
        if start <= key and (not end or end >= key):
            active = row
    name = (active["advisor_name"] or "").strip() if active is not None else ""
    conn.execute(
        "UPDATE stores SET advisor_name=?, has_advisor=? WHERE id=?",
        (name, 1 if name else 0, int(store_id)),
    )
    return name


def _normalize_tenure_input(
    *,
    advisor_name: str,
    from_month: str,
    to_month: str,
    tier_grade: str,
    base_pay_cents: int,
    base_coef: float,
    breakeven_cents: int,
    excess_rate: float,
    work_days: int,
) -> Dict[str, Any]:
    """任期表单的校验与归一；任一项不合口径就抛 ValueError（文案直接给用户看）。"""
    from . import v5_baseline

    name = (advisor_name or "").strip()
    if not name:
        raise ValueError("顾问姓名要填")
    start = v5_baseline.month_key(from_month)
    if not start:
        raise ValueError("任期起始月要填成 2026-11 这种格式")
    end_raw = (to_month or "").strip()
    end = v5_baseline.month_key(end_raw) if end_raw else ""
    if end_raw and not end:
        raise ValueError("任期结束月要填成 2026-11 这种格式，留空表示至今在任")
    if end and end < start:
        raise ValueError(f"任期结束月 {end} 早于起始月 {start}，先改日期")
    tier = v5_baseline.normalize_tier(tier_grade)
    if not tier:
        raise ValueError("档位只能选 A 档或 B 近档")
    pay = int(base_pay_cents or 0)
    if pay < 0:
        raise ValueError("保底底薪不能为负")
    coef = float(base_coef or 0)
    if coef <= 0:
        raise ValueError("考核系数要大于 0")
    breakeven = int(breakeven_cents or 0)
    if breakeven < 0:
        raise ValueError("平衡点不能为负")
    rate = float(excess_rate or 0)
    if not 0 <= rate <= 1:
        raise ValueError("超额分成比例要在 0% 到 100% 之间")
    days = int(work_days or 0)
    if not 0 <= days <= 31:
        raise ValueError("当月工作日折算只能填 0 到 31 天（0 = 按全月算）")
    return {
        "advisor_name": name,
        "from_month": start,
        "to_month": end,
        "tier_grade": tier,
        "base_pay_cents": pay,
        "base_coef": coef,
        "breakeven_cents": breakeven,
        "excess_rate": rate,
        "work_days": days,
    }


def _tenure_conflict(
    conn: sqlite3.Connection, store_id: int, start: str, end: str, keep_id: int
) -> Optional[sqlite3.Row]:
    """找与 [start, end] 重叠的其它任期段；同店同期只允许一位专职顾问。"""
    span_end = end or "9999-12"
    for row in list_advisor_tenures(conn, store_id):
        if keep_id and int(row["id"]) == keep_id:
            continue
        other_start = row["from_month"]
        other_end = row["to_month"] or "9999-12"
        if other_start <= span_end and start <= other_end:
            return row
    return None


def save_advisor_tenure(
    conn: sqlite3.Connection,
    store_id: int,
    *,
    advisor_name: str,
    from_month: str,
    to_month: str = "",
    tier_grade: str = "A",
    base_pay_cents: int = 0,
    base_coef: float = 1.0,
    breakeven_cents: int = 0,
    excess_rate: float = 0.0,
    work_days: int = 0,
    note: str = "",
    tenure_id: Optional[int] = None,
) -> int:
    """新增或改写一段顾问任期，并把 `stores` 的顾问快照重新派生一次。

    `tenure_id` 给 0/None 是新增，给了就是改那一段。返回任期行 id。
    """
    sid = int(store_id)
    if get_store(conn, sid) is None:
        raise ValueError("没有这家店")
    if not _table_exists(conn, "advisor_tenure"):
        raise ValueError("这个库还没跑 Migration 17，先升级再维护顾问任期")
    clean = _normalize_tenure_input(
        advisor_name=advisor_name,
        from_month=from_month,
        to_month=to_month,
        tier_grade=tier_grade,
        base_pay_cents=base_pay_cents,
        base_coef=base_coef,
        breakeven_cents=breakeven_cents,
        excess_rate=excess_rate,
        work_days=work_days,
    )
    tid = int(tenure_id or 0)
    clash = _tenure_conflict(conn, sid, clean["from_month"], clean["to_month"], tid)
    if clash is not None:
        label = tenure_range_label(clash["from_month"], clash["to_month"] or "")
        raise ValueError(
            f"{label} 已经是 {clash['advisor_name']} 的任期，同一家店同一时间只能有一位专职顾问"
        )
    dup = conn.execute(
        "SELECT 1 FROM advisor_tenure WHERE store_id=? AND advisor_name=?"
        " AND from_month=? AND id<>?",
        (sid, clean["advisor_name"], clean["from_month"], tid),
    ).fetchone()
    if dup is not None:
        raise ValueError(f"{clean['advisor_name']} 在 {clean['from_month']} 已经有一段任期了")
    values = (
        clean["advisor_name"],
        clean["from_month"],
        clean["to_month"],
        clean["tier_grade"],
        clean["base_pay_cents"],
        clean["base_coef"],
        clean["breakeven_cents"],
        clean["excess_rate"],
        clean["work_days"],
        (note or "").strip(),
    )
    if tid:
        exists = conn.execute(
            "SELECT 1 FROM advisor_tenure WHERE id=? AND store_id=?", (tid, sid)
        ).fetchone()
        if exists is None:
            raise ValueError("这段任期已经不在了，刷新页面再看")
        conn.execute(
            """
            UPDATE advisor_tenure SET
                advisor_name=?, from_month=?, to_month=?, tier_grade=?,
                base_pay_cents=?, base_coef=?, breakeven_cents=?, excess_rate=?,
                work_days=?, note=?
            WHERE id=? AND store_id=?
            """,
            (*values, tid, sid),
        )
        new_id = tid
    else:
        cur = conn.execute(
            """
            INSERT INTO advisor_tenure(
                advisor_name, from_month, to_month, tier_grade,
                base_pay_cents, base_coef, breakeven_cents, excess_rate,
                work_days, note, store_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, sid),
        )
        new_id = int(cur.lastrowid or 0)
    sync_store_advisor_snapshot(conn, sid)
    return new_id


def delete_advisor_tenure(conn: sqlite3.Connection, store_id: int, tenure_id: int) -> None:
    """删掉一段任期并重新派生顾问快照；删光最后一段时快照退回「无顾问」。"""
    sid = int(store_id)
    tid = int(tenure_id or 0)
    if not _table_exists(conn, "advisor_tenure"):
        raise ValueError("这个库还没跑 Migration 17，没有任期表")
    if not tid or conn.execute(
        "SELECT 1 FROM advisor_tenure WHERE id=? AND store_id=?", (tid, sid)
    ).fetchone() is None:
        raise ValueError("这段任期已经删过了，刷新页面再看")
    conn.execute("DELETE FROM advisor_tenure WHERE id=? AND store_id=?", (tid, sid))
    if list_advisor_tenures(conn, sid):
        sync_store_advisor_snapshot(conn, sid)
    else:
        # 任期表里已无事实：快照跟着退回无顾问，不留幽灵姓名
        conn.execute(
            "UPDATE stores SET advisor_name='', has_advisor=0 WHERE id=?", (sid,)
        )

