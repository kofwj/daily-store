from datetime import date
from io import BytesIO

import openpyxl
import pytest

from app import db, db_core
from app.broadcast import render_broadcast
from app.stores_seed import NINGHAI_CODE, STORES, filler_accounts


def test_catalog_has_eleven_official_stores(tmp_db):
    with db.get_db() as conn:
        rows = db.list_all_stores(conn)
        assert [r["name"] for r in rows] == [item["name"] for item in STORES]
        assert [r["code"] for r in rows] == [item["code"] for item in STORES]
        ninghai = conn.execute("SELECT * FROM stores WHERE code=?", (NINGHAI_CODE,)).fetchone()
        assert ninghai["name"] == "邻市戊路vivo体验店"
        assert ninghai["mobile_code"] == "10000004"
        assert ninghai["area_manager"] == "刘经理"
        assert ninghai["store_manager"] == "赵店长"
        assert ninghai["short_name"] == "示例戊店"
        accounts = filler_accounts()
        logins = [item["login"] for item in accounts]
        assert len(logins) == len(set(logins))
        assert all(len(item["login"]) <= 8 for item in accounts)
        assert "alpha" in logins and "gamma" in logins and "zeta" in logins
        fillers = list(conn.execute("SELECT username, display_name, role FROM users WHERE role='filler' ORDER BY id"))
        assert [row["username"] for row in fillers] == [item["login"] for item in accounts]
        assert conn.execute("SELECT id FROM users WHERE username='ninghai'").fetchone() is None


def test_legacy_ninghai_name_is_renamed_not_duplicated(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("STORE_DAILY_SAMPLE_SEED", "1")
    # 真正读路径的是 db_core.connect()（模块级全局），必须 patch 属主；db 只是 re-export。
    monkeypatch.setattr(db_core, "DB_PATH", path)
    monkeypatch.setattr(db_core, "DATA_DIR", tmp_path)
    conn = db.connect()
    conn.executescript(
        """
        CREATE TABLE stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            code TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE user_stores (
            user_id INTEGER NOT NULL,
            store_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, store_id)
        );
        CREATE TABLE metrics (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            section TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            monthly_target INTEGER NOT NULL DEFAULT 0,
            highlight INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            biz_date TEXT NOT NULL,
            store_id INTEGER NOT NULL,
            submitted_by INTEGER,
            submitted_at TEXT NOT NULL,
            compact INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE (biz_date, store_id)
        );
        CREATE TABLE daily_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            biz_date TEXT NOT NULL,
            store_id INTEGER NOT NULL,
            metric_code TEXT NOT NULL,
            day_value INTEGER NOT NULL DEFAULT 0,
            UNIQUE (biz_date, store_id, metric_code)
        );
        """
    )
    conn.execute(
        "INSERT INTO stores(name, code, active, created_at) VALUES (?, ?, 1, ?)",
        ("示例戊店路店", "store-epsilon", "2026-08-13 00:00:00"),
    )
    conn.execute(
        "INSERT INTO daily_reports(biz_date, store_id, submitted_by, submitted_at) VALUES (?, 1, NULL, ?)",
        ("2026-08-13", "2026-08-13 20:00:00"),
    )
    conn.commit()
    conn.close()
    db.init_db()
    with db.get_db() as conn:
        rows = list(conn.execute("SELECT code, name FROM stores ORDER BY sort_order"))
        assert len(rows) == len(STORES)
        ninghai = conn.execute("SELECT * FROM stores WHERE code='store-epsilon'").fetchone()
        assert ninghai["id"] == 1
        assert ninghai["name"] == "邻市戊路vivo体验店"
        kept = conn.execute("SELECT COUNT(*) AS n FROM daily_reports WHERE store_id=1").fetchone()
        assert kept["n"] == 1


def test_legacy_coin_cut_moves_to_old_user(tmp_db):
    with db.get_db() as conn:
        store_id = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        user_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        conn.execute(
            "INSERT OR REPLACE INTO metrics(code, name, section, sort_order, monthly_target, highlight, active) VALUES ('coin_cut', '金币直降', 'contract', 1, 8, 0, 1)"
        )
        conn.execute(
            "INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value) VALUES (?, ?, 'coin_cut', 2)",
            ("2026-08-10", store_id),
        )
        # 模拟“还没迁移过”的旧库：清掉迁移记录，让 init_db 重跑 coin_cut → coin_cut_old 迁移
        conn.execute("DELETE FROM schema_migrations")
    db.init_db()
    with db.get_db() as conn:
        moved = conn.execute(
            "SELECT day_value FROM daily_facts WHERE store_id=? AND biz_date='2026-08-10' AND metric_code='coin_cut_old'",
            (store_id,),
        ).fetchone()
        leftover = conn.execute("SELECT 1 FROM daily_facts WHERE metric_code='coin_cut'").fetchone()
        assert moved["day_value"] == 2
        assert leftover is None
        assert conn.execute("SELECT active FROM metrics WHERE code='coin_cut'").fetchone()["active"] == 0
        db.save_daily(
            conn,
            store_id=store_id,
            biz_date=date(2026, 8, 11),
            values={
                "coin_cut_old": 1,
                "coin_cut_new_recharge": 1,
                "coin_cut_new_sesame": 1,
                "coin_cut_new_savings": 0,
                "coin_cut_xtc": 1,
            },
            user_id=user_id,
        )
        through = db.month_cum_through(conn, store_id, date(2026, 8, 11))
        from app.metrics_seed import rollup_amount

        assert rollup_amount(through, "coin_cut") == 2
        assert rollup_amount(through, "coin_cut_all") == 6


def test_save_and_month_cum(tmp_db):
    with db.get_db() as conn:
        store_id = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        user_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(
            conn,
            store_id=store_id,
            biz_date=date(2026, 8, 10),
            values={"broadband": 4, "tv": 4, "phone_sales": 5},
            user_id=user_id,
        )
        db.save_daily(
            conn,
            store_id=store_id,
            biz_date=date(2026, 8, 13),
            values={"broadband": 3, "tv": 3, "phone_sales": 1, "direct_pack": 1},
            user_id=user_id,
        )
        prev = db.prev_month_cum(conn, store_id, date(2026, 8, 13))
        today = db.day_values(conn, store_id, date(2026, 8, 13))
        through = db.month_cum_through(conn, store_id, date(2026, 8, 13))
        assert prev["broadband"] == 4
        assert today["broadband"] == 3
        assert through["broadband"] == 7
        assert through["phone_sales"] == 6


def test_health_is_public(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["service"] == "store-daily"
    assert payload.get("version")


def test_login_and_save_roundtrip(client):
    assert client.get("/today").status_code == 302
    page = client.post("/login", data={"username": "epsilon", "pin": "123456"}, follow_redirects=True)
    assert page.status_code == 200
    assert "邻市戊路vivo体验店".encode("utf-8") in page.data
    store_id = None
    with db.get_db() as conn:
        store_id = conn.execute("SELECT id FROM stores WHERE code=?", (NINGHAI_CODE,)).fetchone()["id"]
    saved = client.post(
        "/today",
        data={
            "store_id": str(store_id),
            "date": date.today().isoformat(),
            "m_phone_sales": "1",
            "m_broadband": "3",
            "compact": "1",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "已保存".encode("utf-8") in saved.data
    xlsx_resp = client.get("/report.xlsx")
    assert xlsx_resp.status_code == 200
    assert xlsx_resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    workbook = openpyxl.load_workbook(BytesIO(xlsx_resp.data))
    flat = [str(v) for row in workbook.active.iter_rows(values_only=True) for v in row if v is not None]
    assert "当天手机销量" in flat
    # 旧 /report.csv 重定向到 .xlsx
    assert client.get("/report.csv").status_code == 302
    deal_page = client.get("/deal")
    assert deal_page.status_code == 200
    assert "触客播报".encode("utf-8") in deal_page.data
    sheet = client.get("/bulletin", follow_redirects=True)
    assert "需要管理员或只读权限".encode("utf-8") in sheet.data


def test_broadcast_from_saved_facts(tmp_db):
    with db.get_db() as conn:
        store = conn.execute("SELECT * FROM stores LIMIT 1").fetchone()
        user_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(
            conn,
            store_id=store["id"],
            biz_date=date(2026, 8, 13),
            values={
                "phone_sales": 1,
                "id_check": 1,
                "lead": 0,
                "reserve": 0,
                "bisuan": 0,
                "bisuan_high": 3,
                "other_card": 0,
                "broadband": 3,
                "tv": 3,
                "direct_pack": 1,
            },
            user_id=user_id,
        )
        # 补本月更早的累计来源
        db.save_daily(
            conn,
            store_id=store["id"],
            biz_date=date(2026, 8, 1),
            values={
                "phone_sales": 12,
                "id_check": 3,
                "lead": 9,
                "reserve": 2,
                "bisuan": 5,
                "other_card": 5,
                "broadband": 7,
                "tv": 7,
                "fttr": 1,
                "gigabit": 2,
                "coin_cut_old": 1,
                "phone_discount": 6,
                "gift_2g": 1,
                "fangzha": 3,
                "he_msg": 3,
                "crbt": 2,
            },
            user_id=user_id,
        )
        # 8/13 购机让利 2，累 8 → 8/1 已记 6
        db.save_daily(
            conn,
            store_id=store["id"],
            biz_date=date(2026, 8, 13),
            values={
                "phone_sales": 1,
                "id_check": 1,
                "bisuan_high": 3,
                "broadband": 3,
                "tv": 3,
                "phone_discount": 2,
                "direct_pack": 1,
            },
            user_id=user_id,
        )
        today = db.day_values(conn, store["id"], date(2026, 8, 13))
        prev = db.prev_month_cum(conn, store["id"], date(2026, 8, 13))
        from app.broadcast import add_day_to_prev

        pairs = add_day_to_prev(prev, today)
        text = render_broadcast(store["short_name"] or store["name"], date(2026, 8, 13), pairs)
        assert text.startswith(f"8月13日\n{store['short_name']}\n")
        assert "当天手机销量：日1，累13" in text
        assert "宽带：日3，累10" in text
        assert "购机让利：日2，累8" in text
        assert "金币直降：日0，累1" in text


def test_metric_target_map_prefers_kpi_targets(tmp_db):
    with db.get_db() as conn:
        db.set_kpi_target(conn, "ai_contract", 12)
        db.set_kpi_target(conn, "coin_cut", 8)
        conn.execute("UPDATE metrics SET monthly_target=99 WHERE code='ai_contract'")
        conn.execute("UPDATE metrics SET monthly_target=3 WHERE code='broadband'")
        targets = db.metric_target_map(conn)
    assert targets["ai_contract"] == 12
    assert targets["coin_cut_new_recharge"] == 8
    assert targets["broadband"] == 3


def test_audit_tables_are_indexed(tmp_db):
    """审计表至少要有索引可用（改索引名/列不该让测试红）。"""
    with db.get_db() as conn:
        for table in ("report_edits", "deal_edits", "advance_edits"):
            idxs = conn.execute(f"PRAGMA index_list('{table}')").fetchall()
            assert idxs, table


def _seed_advance_cents(conn):
    conn.execute(
        "INSERT INTO stores(name, code, sort_order, created_at) VALUES('t','t',1,'2026-01-01')"
    )
    conn.execute(
        "INSERT INTO advance_posts(store_id, user_id, created_at, biz_date, phone, "
        "broadband, rebate, other, note, paid) "
        "VALUES(1,1,'2026-01-01','2026-01-01','139',3999,0,0,'x',0)"
    )


def _seed_sesame_frozen(conn):
    db_core.sesame_orders_upsert(
        conn,
        [{"order_no": "o1", "store_code": "s1", "frozen": 500.0, "terms": 12, "order_title": "500档"}],
        "2026-08-15 00:00:00",
    )


def _seed_bisuan_tenths(conn):
    sid = conn.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO daily_facts(biz_date, store_id, metric_code, day_value) VALUES (?, ?, 'bisuan', 15)",
        (date(2026, 8, 1).isoformat(), sid),
    )


# 迁移幂等由 app_meta marker 守卫：删掉 schema_migrations 版本标记重跑，值不变。
_IDEMPOTENT_MIGRATIONS = [
    pytest.param(7, _seed_advance_cents, "SELECT broadband FROM advance_posts", 3999, None, id="advance_cents_v7"),
    pytest.param(15, _seed_sesame_frozen, "SELECT frozen FROM sesame_orders WHERE order_no='o1'", 50000, None, id="sesame_frozen_v15"),
    pytest.param(11, _seed_bisuan_tenths, "SELECT day_value FROM daily_facts WHERE metric_code='bisuan'", 15, "bisuan_tenths_marker", id="bisuan_tenths_v11"),
]


@pytest.mark.parametrize("version, seed, query, expected, marker_key", _IDEMPOTENT_MIGRATIONS)
def test_migration_is_idempotent(tmp_db, version, seed, query, expected, marker_key):
    with db.get_db() as conn:
        seed(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version=?", (version,))
        conn.commit()
        before = conn.execute(query).fetchone()[0]
    db_core.migrate()
    with db.get_db() as conn:
        after = conn.execute(query).fetchone()[0]
        marker = (
            conn.execute("SELECT value FROM app_meta WHERE key=?", (marker_key,)).fetchone()
            if marker_key
            else None
        )
    assert int(before) == expected
    assert int(after) == expected
    if marker_key:
        assert marker is not None
def test_sesame_orders_upsert_stores_cents(tmp_db):
    """芝麻订单入库：冻结金额按分存（与 advance 金额同口径）。"""
    from app import db_core

    with db.get_db() as conn:
        db_core.sesame_orders_upsert(
            conn,
            [{"order_no": "o1", "store_code": "s1", "frozen": 500.0, "terms": 12, "order_title": "500档"}],
            "2026-08-15 00:00:00",
        )
    with db.get_db() as conn:
        row = conn.execute("SELECT frozen FROM sesame_orders WHERE order_no='o1'").fetchone()
    assert int(row["frozen"]) == 50000
    # 覆盖写（重新导入同一订单）仍按分存
    with db.get_db() as conn:
        db_core.sesame_orders_upsert(
            conn,
            [{"order_no": "o1", "store_code": "s1", "frozen": 1368.75, "terms": 3, "order_title": "1368档"}],
            "2026-08-16 00:00:00",
        )
    with db.get_db() as conn:
        row = conn.execute("SELECT frozen FROM sesame_orders WHERE order_no='o1'").fetchone()
    assert int(row["frozen"]) == 136875


def test_sesame_frozen_migration_converts_legacy_real(tmp_db):
    """旧库 frozen 按元存（REAL 列）：迁移后转成整数分。"""
    from app import db_core

    with db.get_db() as conn:
        conn.execute("DROP TABLE sesame_orders")
        conn.execute(
            """
            CREATE TABLE sesame_orders (
                order_no TEXT PRIMARY KEY,
                store_code TEXT NOT NULL DEFAULT '',
                frozen REAL NOT NULL DEFAULT 0,
                terms INTEGER NOT NULL DEFAULT 0,
                tier TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                order_title TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO sesame_orders(order_no, store_code, frozen, terms, imported_at) VALUES "
            "('legacy1', 's1', 500.0, 12, '2026-08-15 00:00:00'), "
            "('legacy2', 's2', 1368.75, 3, '2026-08-15 00:00:00')"
        )
        conn.execute("DELETE FROM schema_migrations WHERE version=15")
        # 真正的旧库从未跑过该迁移，也就没有 marker；fresh 库 init 时已写，这里要一并清掉
        conn.execute("DELETE FROM app_meta WHERE key='sesame_frozen_cents_marker'")
        conn.commit()
    db_core.migrate()
    with db.get_db() as conn:
        rows = {
            r["order_no"]: int(r["frozen"])
            for r in conn.execute("SELECT order_no, frozen FROM sesame_orders")
        }
        marker = conn.execute(
            "SELECT value FROM app_meta WHERE key='sesame_frozen_cents_marker'"
        ).fetchone()
    assert rows["legacy1"] == 50000
    assert rows["legacy2"] == 136875
    assert marker is not None


def test_money_ok_tolerates_nonfinite():
    from app import db

    assert db.money_ok(0, 0, 0) is False
    assert db.money_ok(0, 0.01, 0) is True
    assert db.money_ok(float("nan"), 0, 0) is False
    assert db.money_ok(float("inf"), 0, 0) is False
    assert db.money_ok("abc", 1, 0) is True


def test_seed_does_not_overwrite_admin_edits(tmp_db):
    """重启（再次 init_db）不应把管理员的档案/账号改动冲掉。"""
    with db.get_db() as conn:
        store = conn.execute("SELECT * FROM stores LIMIT 1").fetchone()
        conn.execute(
            "UPDATE stores SET active=0, store_manager='管理员手改', area_manager='手改A', mobile_code='' WHERE id=?",
            (store["id"],),
        )
        uid = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        db.set_user_active(conn, uid, False)
        other = conn.execute("SELECT id FROM stores ORDER BY id DESC LIMIT 1").fetchone()["id"]
        db.set_user_stores(conn, uid, [other])
    db.init_db()
    with db.get_db() as conn:
        s = conn.execute("SELECT * FROM stores WHERE id=?", (store["id"],)).fetchone()
        assert s["active"] == 0
        assert s["store_manager"] == "管理员手改"
        assert s["area_manager"] == "手改A"
        assert s["mobile_code"] == ""
        u = conn.execute("SELECT active FROM users WHERE username='alpha'").fetchone()
        assert u["active"] == 0
        uid2 = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
        assert db.user_store_ids(conn, uid2) == [other]
    # 管理员把店清空后重启，也不该被种子偷偷补回目录默认店
    with db.get_db() as conn:
        db.set_user_stores(conn, uid, [])
    db.init_db()
    with db.get_db() as conn:
        assert db.user_store_ids(conn, uid) == []


def test_bisuan_mobile_migrates_from_old_settings(tmp_db):
    """旧 app_meta 键要能搬进新表，且截止日按店保留。"""
    from app.db_bisuan_mobile import _migrate_bisuan_mobile_from_settings

    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        db.set_setting(conn, f"bisuan_mobile_{sid}_2026-07", "12.5")
        db.set_setting(conn, "bisuan_mobile_asof_2026-07", "2026-07-20")
        _migrate_bisuan_mobile_from_settings(conn)
        row = db.get_bisuan_mobile(conn, sid, "2026-07")
        assert row["value_tenths"] == 125
        assert row["asof"] == "2026-07-20"
        # 旧键已清掉，重复迁移不会翻倍
        _migrate_bisuan_mobile_from_settings(conn)
        assert db.get_bisuan_mobile(conn, sid, "2026-07")["value_tenths"] == 125
        assert db.get_setting(conn, f"bisuan_mobile_{sid}_2026-07", "") == ""


def test_bisuan_mobile_rejects_negative(tmp_db):
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores LIMIT 1").fetchone()["id"]
        with pytest.raises(ValueError):
            db.save_bisuan_mobile(
                conn, store_id=sid, month="2026-08", value_tenths=-5, asof=date(2026, 8, 20)
            )


def test_month_cum_through_many_matches_single(tmp_db):
    """批量月累计与逐店单查口径一致。"""
    today = date.today()
    with db.get_db() as conn:
        sid_a = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        sid_b = db.create_store(conn, "批量对照店", short_name="批量对照店")
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.save_daily(conn, store_id=sid_a, biz_date=today, user_id=admin_id, values={"phone_sales": 3})
        db.save_daily(conn, store_id=sid_b, biz_date=today, user_id=admin_id, values={"phone_sales": 7})
        many = db.month_cum_through_many(conn, [sid_a, sid_b, 999999], today)
        single_a = db.month_cum_through(conn, sid_a, today)
        single_b = db.month_cum_through(conn, sid_b, today)
    assert many[sid_a]["phone_sales"] == single_a["phone_sales"] == 3
    assert many[sid_b]["phone_sales"] == single_b["phone_sales"] == 7
    assert 999999 not in many


def test_comma_input_strict(tmp_db):
    """千分位正常剥；「12,5」这类小数逗号不再放大十倍。"""
    from app.db_advances import parse_money
    from app.metrics_seed import to_stored

    assert to_stored("phone_sales", "1,234") == 1234
    assert to_stored("bisuan_new", "12,5") == 0
    assert parse_money("1,234.5") == 1234.5
    with pytest.raises(ValueError):
        parse_money("12,5")

