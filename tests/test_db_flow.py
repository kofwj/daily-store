from datetime import date
from io import BytesIO

import openpyxl

from app import db, db_core
from app.broadcast import render_broadcast
from app.stores_seed import NINGHAI_CODE, STORES, filler_accounts
from app.web import create_app


def test_catalog_has_eleven_official_stores(tmp_db):
    with db.get_db() as conn:
        rows = db.list_all_stores(conn)
        assert [r["name"] for r in rows] == [item["name"] for item in STORES]
        assert [r["code"] for r in rows] == [item["code"] for item in STORES]
        ninghai = conn.execute("SELECT * FROM stores WHERE code=?", (NINGHAI_CODE,)).fetchone()
        assert ninghai["name"] == "TZ南通市如皋市如城镇宁海路体验店"
        assert ninghai["mobile_code"] == "20001744"
        assert ninghai["area_manager"] == "鞠一凡"
        assert ninghai["store_manager"] == "冒国云"
        assert ninghai["short_name"] == "如皋宁海路"
        accounts = filler_accounts()
        logins = [item["login"] for item in accounts]
        assert len(logins) == len(set(logins))
        assert all(len(item["login"]) <= 8 for item in accounts)
        assert "qdwy" in logins and "yinhe" in logins and "rgwd" in logins
        fillers = list(conn.execute("SELECT username, display_name, role FROM users WHERE role='filler' ORDER BY id"))
        assert [row["username"] for row in fillers] == [item["login"] for item in accounts]
        assert conn.execute("SELECT id FROM users WHERE username='ninghai'").fetchone() is None


def test_legacy_ninghai_name_is_renamed_not_duplicated(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
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
        ("如皋宁海路路店", "rg-ninghai", "2026-08-13 00:00:00"),
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
        ninghai = conn.execute("SELECT * FROM stores WHERE code='rg-ninghai'").fetchone()
        assert ninghai["id"] == 1
        assert ninghai["name"] == "TZ南通市如皋市如城镇宁海路体验店"
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


def test_health_is_public(tmp_db):
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["service"] == "store-daily"
    assert payload.get("version")


def test_login_and_save_roundtrip(tmp_db):
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    assert client.get("/today").status_code == 302
    page = client.post("/login", data={"username": "rgning", "pin": "123456"}, follow_redirects=True)
    assert page.status_code == 200
    assert "TZ南通市如皋市如城镇宁海路体验店".encode("utf-8") in page.data
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
    board = client.get("/board", follow_redirects=True)
    assert "需要管理员权限".encode("utf-8") in board.data
    incentive = client.get("/incentive", follow_redirects=True)
    assert "需要管理员权限".encode("utf-8") in incentive.data
    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "保存口令".encode("utf-8") in settings.data
    assert "门店档案".encode("utf-8") not in settings.data
    bad = client.post("/settings", data={"action": "change_pin", "old_pin": "9999", "new_pin": "111111", "new_pin2": "111111"}, follow_redirects=True)
    assert "当前口令不对".encode("utf-8") in bad.data
    short = client.post("/settings", data={"action": "change_pin", "old_pin": "123456", "new_pin": "1111", "new_pin2": "1111"}, follow_redirects=True)
    assert "至少 6 位".encode("utf-8") in short.data
    ok = client.post("/settings", data={"action": "change_pin", "old_pin": "123456", "new_pin": "111111", "new_pin2": "111111"}, follow_redirects=True)
    assert "口令已改".encode("utf-8") in ok.data


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


def test_audit_tables_have_edited_at_index(tmp_db):
    with db.get_db() as conn:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "idx_report_edits_edited_at" in names
    assert "idx_deal_edits_edited_at" in names
    assert "idx_advance_edits_edited_at" in names


def test_advance_cents_migration_is_idempotent(tmp_db):
    """迁移 v7 重跑不会把分再乘 100。"""
    from app import db_core

    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO stores(name, code, sort_order, created_at) VALUES('t','t',1,'2026-01-01')"
        )
        conn.execute(
            "INSERT INTO advance_posts(store_id, user_id, created_at, biz_date, phone, "
            "broadband, rebate, other, note, paid) "
            "VALUES(1,1,'2026-01-01','2026-01-01','139',3999,0,0,'x',0)"
        )
        conn.execute("DELETE FROM schema_migrations WHERE version=7")
        conn.commit()
        before = conn.execute("SELECT broadband FROM advance_posts").fetchone()[0]
    db_core.migrate()
    with db.get_db() as conn:
        after = conn.execute("SELECT broadband FROM advance_posts").fetchone()[0]
    assert before == 3999
    assert after == 3999  # 绝不重复转换


def test_money_ok_tolerates_nonfinite():
    from app import db

    assert db.money_ok(0, 0, 0) is False
    assert db.money_ok(0, 0.01, 0) is True
    assert db.money_ok(float("nan"), 0, 0) is False
    assert db.money_ok(float("inf"), 0, 0) is False
    assert db.money_ok("abc", 1, 0) is True
