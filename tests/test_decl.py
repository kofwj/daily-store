"""顺差声明 + 待核让利垫资（V0.4.1）。

口径：
- 触客成交选串码 → 一条待核声明；池子里那台立刻不显示（靠谓词，不写 imei_todo）；
- 待核**不进** advance_posts，确认前不参与任何垫资合计；确认后才落 paid=0 进待兑付；
- 账记在**成交日**所在月（31 号成交、次月确认照样算上月）；
- 触客改串码/改未成交/删除 → 声明跟着作废；已兑付的写负数冲账，不删原行；
- 自报「已办 + 顺差让利」走同一出口；已有声明就幂等，不报错；
- 串码撞车**不影响触客保存**（触客条数是 KPI）。
"""
from datetime import date
from io import BytesIO

import pytest

from app import db, db_core, db_deals, db_decl, db_imei

STOCK_HEAD = "imei,store,model,sp,day,串码标签\n"
ALPHA_CODE = "store-alpha"


def _stock_csv(*rows):
    return BytesIO((STOCK_HEAD + "".join(r + "\n" for r in rows)).encode("utf-8"))


def _login(client, username="admin", pin="123456"):
    client.post("/login", data={"username": username, "pin": pin})


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    """固定「今天」= 2026-11-02，天数/月份断言不随真实日期漂。"""
    monkeypatch.setattr(db_core, "today_local", lambda: date(2026, 11, 2))
    monkeypatch.setattr(db_imei, "today_local", lambda: date(2026, 11, 2))
    monkeypatch.setattr(db_decl, "today_local", lambda: date(2026, 11, 2))
    monkeypatch.setattr(db_deals, "today_local", lambda: date(2026, 11, 2))
    monkeypatch.setattr(db, "is_locked", lambda *a, **k: False)


def _store_id(code=ALPHA_CODE):
    with db.get_db() as conn:
        return conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]


def _import_stock(client, *rows):
    return client.post(
        "/imei/import",
        data={"csv_file": (_stock_csv(*rows), "stock.csv"), "asof": "2026-11-02"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _stock_row(imei, model="Y600 Pro", store="示例市甲街vivo体验店"):
    return f"{imei},{store},{model},小李,2026-11-01,移动顺差"


def _make_store(client, imei="860000000000001"):
    """导一台在库顺差机，返回串码。"""
    _login(client)
    _import_stock(client, _stock_row(imei))
    return imei


def _deal(conn, *, store_id, imei="", phone="15500000001", closed=True, day=None):
    return db.record_deal_post(
        conn,
        store_id=store_id,
        user_id=1,
        closed=closed,
        model="Y600 Pro",
        phone=phone,
        text="",
        imei=imei,
        biz_date=day or date(2026, 11, 2),
    )


def test_deal_with_imei_creates_pending_and_leaves_pool(client):
    imei = _make_store(client)
    sid = _store_id()
    with db.get_db() as conn:
        assert any(r["imei"] == imei for r in db_imei.pool_rows(conn))
        deal_id = _deal(conn, store_id=sid, imei=imei)
        decl = db_decl.deal_decl(conn, deal_id)
        assert decl is not None
        assert decl["state"] == db_decl.DECL_PENDING
        assert int(decl["rebate_cents"]) == db_decl.DEFAULT_REBATE_CENTS
        # 出池靠谓词：imei_todo 一个字段都没被改
        row = conn.execute("SELECT status, seen FROM imei_todo WHERE imei=?", (imei,)).fetchone()
        assert row["status"] == db_imei.STATUS_STOCK
        assert int(row["seen"]) == 1
        assert not any(r["imei"] == imei for r in db_imei.pool_rows(conn))
        # 待核不参与垫资合计
        totals = db.advance_month_totals(conn, [sid], date(2026, 11, 2))
        assert totals[sid]["rebate"] == 0.0


def test_pending_not_in_advance_until_confirmed(client):
    imei = _make_store(client, "860000000000002")
    sid = _store_id()
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei=imei, phone="15500000002")
        decl = db_decl.deal_decl(conn, deal_id)
        assert db.advance_month_totals(conn, [sid], date(2026, 11, 2))[sid]["total"] == 0.0
        result = db_decl.confirm(conn, [decl["id"]], user_id=1)
        assert result["confirmed"] == 1
        totals = db.advance_month_totals(conn, [sid], date(2026, 11, 2))
        assert totals[sid]["rebate"] == 100.0
        adv = conn.execute(
            "SELECT * FROM advance_posts WHERE source='imei' AND store_id=?", (sid,)
        ).fetchone()
        assert adv is not None
        assert int(adv["paid"]) == 0  # 进待兑付，不是已兑
        assert str(adv["ext_id"]) == f"imei:{imei}:{decl['id']}"
        assert adv["phone"] == "15500000002"


def test_confirm_twice_is_idempotent(client):
    imei = _make_store(client, "860000000000003")
    sid = _store_id()
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei=imei, phone="15500000003")
        decl = db_decl.deal_decl(conn, deal_id)
        db_decl.confirm(conn, [decl["id"]], user_id=1)
        again = db_decl.confirm(conn, [decl["id"]], user_id=1)
        assert again["confirmed"] == 0 and again["skipped"] == 1
        n = conn.execute("SELECT COUNT(*) FROM advance_posts WHERE source='imei'").fetchone()[0]
        assert n == 1


def test_cross_month_confirm_uses_deal_date(client):
    """31 号成交、次月确认，账仍记在上个月（用户 2026-11-10 拍板）。"""
    imei = _make_store(client, "860000000000004")
    sid = _store_id()
    with db.get_db() as conn:
        deal_id = _deal(
            conn, store_id=sid, imei=imei, phone="15500000004", day=date(2026, 10, 31)
        )
        decl = db_decl.deal_decl(conn, deal_id)
        db_decl.confirm(conn, [decl["id"]], user_id=1)
        adv = conn.execute("SELECT biz_date FROM advance_posts WHERE source='imei'").fetchone()
        assert adv["biz_date"] == "2026-10-31"


def test_void_pending_returns_to_pool(client):
    imei = _make_store(client, "860000000000005")
    sid = _store_id()
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei=imei, phone="15500000005")
        decl = db_decl.deal_decl(conn, deal_id)
        db_decl.void_decl(conn, decl, reason="管理员作废", user_id=1)
        assert any(r["imei"] == imei for r in db_imei.pool_rows(conn))
        assert db.advance_month_totals(conn, [sid], date(2026, 11, 2))[sid]["total"] == 0.0


def test_resale_after_void_does_not_collide(client):
    """退机后再卖：ext_id 带声明 id，第二条不会撞唯一索引。"""
    imei = _make_store(client, "860000000000006")
    sid = _store_id()
    with db.get_db() as conn:
        first = _deal(conn, store_id=sid, imei=imei, phone="15500000006")
        decl1 = db_decl.deal_decl(conn, first)
        db_decl.confirm(conn, [decl1["id"]], user_id=1)
        db_decl.void_by_deal(conn, deal_id=first, reason="退货", user_id=1)
        second = _deal(conn, store_id=sid, imei=imei, phone="15500000007")
        decl2 = db_decl.deal_decl(conn, second)
        assert decl2 is not None and int(decl2["id"]) != int(decl1["id"])
        result = db_decl.confirm(conn, [decl2["id"]], user_id=1)
        assert result["confirmed"] == 1


def test_void_after_paid_writes_reversal_not_delete(client):
    imei = _make_store(client, "860000000000008")
    sid = _store_id()
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei=imei, phone="15500000008")
        decl = db_decl.deal_decl(conn, deal_id)
        db_decl.confirm(conn, [decl["id"]], user_id=1)
        adv = conn.execute("SELECT id FROM advance_posts WHERE source='imei'").fetchone()
        db.set_advance_paid(conn, [adv["id"]], paid=True, user_id=1)
        before = conn.execute("SELECT COUNT(*) FROM advance_posts").fetchone()[0]
        db_decl.void_decl(conn, db_decl.deal_decl(conn, deal_id) or decl, reason="作废", user_id=1)
        after = conn.execute("SELECT COUNT(*) FROM advance_posts").fetchone()[0]
        assert after == before + 1  # 原行保留 + 一条负数冲账
        rev = conn.execute(
            "SELECT rebate FROM advance_posts WHERE ext_id LIKE '%:R' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert rev["rebate"] == -10000


def test_deal_switch_imei_voids_old(client):
    imei_a = "860000000000010"
    imei_b = "860000000000011"
    sid = _store_id()
    _login(client)
    _import_stock(client, _stock_row(imei_a), _stock_row(imei_b))
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei=imei_a, phone="15500000010")
        assert db_decl.deal_decl(conn, deal_id)["imei"] == imei_a
        _deal(conn, store_id=sid, imei=imei_b, phone="15500000010")
        decl = db_decl.deal_decl(conn, deal_id)
        assert decl["imei"] == imei_b
        old = conn.execute("SELECT state FROM imei_decl WHERE imei=?", (imei_a,)).fetchone()
        assert old["state"] == db_decl.DECL_VOID
        assert any(r["imei"] == imei_a for r in db_imei.pool_rows(conn))


def test_deal_unclosed_voids_decl(client):
    imei = _make_store(client, "860000000000012")
    sid = _store_id()
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="15500000012")
        assert db_decl.summary(conn)["n"] == 1
        _deal(conn, store_id=sid, imei=imei, phone="15500000012", closed=False)
        assert db_decl.summary(conn)["n"] == 0


def test_delete_deal_voids_decl(client):
    imei = _make_store(client, "860000000000013")
    sid = _store_id()
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei=imei, phone="15500000013")
        assert db.delete_deal_post(conn, deal_id, sid, user_id=1)
        assert db_decl.summary(conn)["n"] == 0


def test_second_store_does_not_steal_but_deal_still_saves(client):
    """串码撞车：触客照常保存，只是不生成垫资（条数是 KPI）。"""
    imei = _make_store(client, "860000000000014")
    sid = _store_id()
    with db.get_db() as conn:
        beta = conn.execute("SELECT id FROM stores WHERE code='store-beta'").fetchone()["id"]
        first = _deal(conn, store_id=sid, imei=imei, phone="15500000014")
        second = _deal(conn, store_id=beta, imei=imei, phone="15500000015")
        assert second > 0
        assert db_decl.deal_decl(conn, first) is not None
        assert db_decl.deal_decl(conn, second) is None
        n = conn.execute("SELECT COUNT(*) FROM imei_decl").fetchone()[0]
        assert n == 1


def test_report_already_done_creates_pending(client):
    """自报「已办 + 顺差让利」走同一个出口。"""
    imei = _make_store(client, "860000000000016")
    sid = _store_id()
    with db.get_db() as conn:
        report_id = db_imei.add_report(
            conn,
            imei=imei,
            store_id=sid,
            action="已办",
            biz_type="顺差让利",
            note="受理单 123",
            user_id=1,
        )
        decl = db_decl.create_from_report(
            conn, imei=imei, store_id=sid, report_id=report_id, user_id=1
        )
        assert decl["created"] is True
        assert db_decl.summary(conn)["n"] == 1
        again = db_decl.create_from_report(
            conn, imei=imei, store_id=sid, report_id=report_id, user_id=1
        )
        assert again["created"] is False and again["reason"] == "duplicate"


def test_pending_page_admin_only_and_confirm(client, filler_client):
    imei = _make_store(client, "860000000000018")
    sid = _store_id()
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="15500000018")
    _login(client, "alpha", "123456")
    assert filler_client.get("/advance/pending").status_code == 302
    _login(client, "admin", "123456")
    resp = client.get("/advance/pending")
    assert resp.status_code == 200
    assert "让利垫资待核".encode() in resp.data
    assert imei.encode() in resp.data
    with db.get_db() as conn:
        decl_id = conn.execute("SELECT id FROM imei_decl WHERE state=?", (db_decl.DECL_PENDING,)).fetchone()["id"]
    resp = client.post(
        "/advance/pending", data={"action": "confirm", "decl_id": [str(decl_id)]}, follow_redirects=True
    )
    assert "已确认 1 笔".encode() in resp.data
    with db.get_db() as conn:
        assert db.advance_month_totals(conn, [sid], date(2026, 11, 2))[sid]["rebate"] == 100.0


def test_bundle_rebate_isolated_and_upper_bound(client):
    """档位改动只影响之后生成的待核，已生成的是快照。"""
    imei_a = "860000000000020"
    imei_b = "860000000000021"
    sid = _store_id()
    _login(client)
    _import_stock(client, _stock_row(imei_a), _stock_row(imei_b))
    with db.get_db() as conn:
        first = _deal(conn, store_id=sid, imei=imei_a, phone="15500000020")
        db_decl.set_rebate_cents(conn, 20000)
        second = _deal(conn, store_id=sid, imei=imei_b, phone="15500000021")
        assert int(db_decl.deal_decl(conn, first)["rebate_cents"]) == 10000
        assert int(db_decl.deal_decl(conn, second)["rebate_cents"]) == 20000
        # 非法值回默认，不让脏数据把金额改掉
        db_decl.set_rebate_cents(conn, -5)
        assert db_decl.rebate_cents(conn) == db_decl.DEFAULT_REBATE_CENTS


def test_imei_auto_rows_are_locked(client):
    """自动生成的垫资行不许手改，也不在批量撤回里计数。"""
    imei = _make_store(client, "860000000000022")
    sid = _store_id()
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei=imei, phone="15500000022")
        decl = db_decl.deal_decl(conn, deal_id)
        db_decl.confirm(conn, [decl["id"]], user_id=1)
        adv = conn.execute("SELECT id FROM advance_posts WHERE source='imei'").fetchone()
        with pytest.raises(ValueError):
            db.record_advance(
                conn,
                store_id=sid,
                user_id=1,
                biz_date=date(2026, 11, 2),
                rebate=999,
                advance_id=int(adv["id"]),
            )
        with pytest.raises(ValueError):
            db.delete_advance(conn, int(adv["id"]), sid, user_id=1)


def test_deal_picker_lists_only_own_stock(client):
    """触客页下拉只给**本店库存登记**的在库顺差机（900 多台全塞进去手机端没法用）。"""
    own = "示例市甲街vivo体验店"
    _login(client)
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000001,{own},S60元气版,小李,2026-11-01,移动顺差\n"
                f"861000000000002,{own},Y600 Pro,小李,2026-11-01,移动顺差\n"
                "861000000000003,示例市乙街vivo专卖店,X500 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
        opts = db_imei.pool_picker(conn, own)
        assert {o["imei"] for o in opts} == {"861000000000001", "861000000000002"}
        assert all(o["label"].startswith(o["imei"][-6:]) for o in opts)
    page = client.get("/deal").get_data(as_text=True)
    assert "本店库存里还有 2 台在库顺差机" in page
    assert "861000000000001" in page
    assert "861000000000003" not in page  # 别店的机器不进下拉，但可手填尾号


def test_deal_picker_selection_creates_pending(client):
    """下拉选一台保存 → 生成待核垫资；选完再手填尾号时以手填为准。"""
    own = "示例市甲街vivo体验店"
    _login(client)
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000011,{own},S60元气版,小李,2026-11-01,移动顺差\n"
                f"861000000000012,{own},Y600 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
    resp = client.post(
        "/deal",
        data={
            "store_id": _store_id(),
            "model": "S60元气版",
            "phone": "15500000099",
            "closed": "1",
            "imei_pick": "861000000000011",
            "note": "客户看中元气版",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "已记让利垫资待核".encode() in resp.data
    with db.get_db() as conn:
        decl = conn.execute("SELECT imei, state FROM imei_decl").fetchone()
        assert decl["imei"] == "861000000000011" and decl["state"] == db_decl.DECL_PENDING


def test_deal_picker_hand_tail_wins_when_both_given(client):
    """下拉和责任田都填时，手填优先（门店卖别店机器要靠手填尾号）。"""
    _login(client)
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                "861000000000021,示例市甲街vivo体验店,S60,小李,2026-11-01,移动顺差\n"
                "861000000000022,示例市乙街vivo专卖店,Y600,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
    resp = client.post(
        "/deal",
        data={
            "store_id": _store_id(),
            "model": "Y600",
            "phone": "15500000098",
            "closed": "1",
            "imei_pick": "861000000000021",
            "imei": "000022",  # 尾号命中乙店那台
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        decl = conn.execute("SELECT imei FROM imei_decl").fetchone()
        assert decl["imei"] == "861000000000022"
