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


def _deal(conn, *, store_id, imei="", phone="15500000001", closed=True, day=None, biz_type=None):
    """默认按「顺差让利」发单——V0.4.06 之前的口径就是「有串码就记垫资」。

    测非顺差让利的单子时显式传 ``biz_type``（传 ``""`` 就是「没选」）。
    """
    return db.record_deal_post(
        conn,
        store_id=store_id,
        user_id=1,
        closed=closed,
        model="Y600 Pro",
        phone=phone,
        text="",
        imei=imei,
        biz_type="顺差让利" if biz_type is None else biz_type,
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
            phone="15500000016",
            user_id=1,
        )
        decl = db_decl.create_from_report(
            conn,
            imei=imei,
            store_id=sid,
            report_id=report_id,
            biz_type="顺差让利",
            phone="15500000016",
            user_id=1,
        )
        assert decl["created"] is True
        assert db_decl.summary(conn)["n"] == 1
        again = db_decl.create_from_report(
            conn,
            imei=imei,
            store_id=sid,
            report_id=report_id,
            biz_type="顺差让利",
            phone="15500000016",
            user_id=1,
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
        opts = db_imei.pool_picker(conn, _store_id())
        assert {o["imei"] for o in opts} == {"861000000000001", "861000000000002"}
        # 下拉只给串码本身，不带机型（机型由页面回填到「机型」栏）
        assert "label" not in opts[0]
        assert opts[0]["model"]
    page = client.get("/deal").get_data(as_text=True)
    assert "本店在库 2 台" in page
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
            "biz_type": "顺差让利",
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


def test_deal_picker_option_text_is_imei_only(client):
    """下拉选项文字只有串码本身（用户 2026-11-10 拍板），机型颜色走「机型」那栏。"""
    own = "示例市甲街vivo体验店"
    _login(client)
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000031,{own},S60元气版全网通(12+256G) 星星海,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
    page = client.get("/deal").get_data(as_text=True)
    assert '<option value="861000000000031"' in page
    assert ">861000000000031</option>" in page  # 选项文字就是串码
    assert "星星海</option>" not in page  # 机型不塞进选项
    assert 'data-model="S60元气版全网通(12+256G) 星星海"' in page  # 但带给前端回填


def test_deal_picker_writes_model_when_picked(client):
    """下拉选中保存时，机型按串码台账回填（即使门店没填机型）。"""
    own = "示例市甲街vivo体验店"
    _login(client)
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000041,{own},Y600 Pro全网通(12+512G) 浮光金,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
    resp = client.post(
        "/deal",
        data={
            "store_id": _store_id(),
            "phone": "15500000097",
            "closed": "1",
            "imei_pick": "861000000000041",
            "note": "客户直接要这台",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        row = conn.execute("SELECT model, imei FROM deal_posts ORDER BY id DESC LIMIT 1").fetchone()
        assert row["model"] == "Y600 Pro全网通(12+512G) 浮光金"
        assert row["imei"] == "861000000000041"


# ---- 调拨（V0.4.05）：调入店记，只影响触客下拉，不改库存归属 -------------------

OWN = "示例市甲街vivo体验店"
OTHER = "示例市乙街vivo专卖店"


def _import_two_stores(client):
    """甲店一台 + 乙店一台，都在库顺差。"""
    _login(client)
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"862000000000001,{OWN},S60元气版,小李,2026-11-01,移动顺差\n"
                f"862000000000002,{OTHER},Y600 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )


def _sid(code):
    with db.get_db() as conn:
        return conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]


def test_transfer_moves_picker_both_ways(client):
    """调入店的候选出现这台，调出店的候选看不到它。"""
    _import_two_stores(client)
    a, b = _sid("store-alpha"), _sid("store-beta")
    with db.get_db() as conn:
        before_b = {o["imei"] for o in db_imei.pool_picker(conn, b)}
        assert "862000000000001" not in before_b  # 乙店本来就看不到甲店那台
        res = db_imei.record_transfer(
            conn, imei="862000000000001", to_store_id=b, biz_date=date(2026, 11, 2), user_id=1
        )
        assert res["created"] is True
        assert "862000000000001" in {o["imei"] for o in db_imei.pool_picker(conn, b)}
        assert "862000000000001" not in {o["imei"] for o in db_imei.pool_picker(conn, a)}
        # 不写 imei_todo：归属仍是甲店
        own = conn.execute(
            "SELECT own_store FROM imei_todo WHERE imei='862000000000001'"
        ).fetchone()["own_store"]
        assert own == OWN


def test_transfer_cancel_returns_to_origin(client):
    _import_two_stores(client)
    a, b = _sid("store-alpha"), _sid("store-beta")
    with db.get_db() as conn:
        tid = db_imei.record_transfer(
            conn, imei="862000000000001", to_store_id=b, biz_date=date(2026, 11, 2), user_id=1
        )["id"]
        assert db_imei.cancel_transfer(conn, tid, user_id=1, is_admin=True) is True
        assert "862000000000001" in {o["imei"] for o in db_imei.pool_picker(conn, a)}
        assert "862000000000001" not in {o["imei"] for o in db_imei.pool_picker(conn, b)}
        assert conn.execute("SELECT COUNT(*) FROM imei_transfer").fetchone()[0] == 1  # 不删行


def test_transfer_cancel_requires_admin(client):
    _import_two_stores(client)
    with db.get_db() as conn:
        tid = db_imei.record_transfer(
            conn, imei="862000000000001", to_store_id=_sid("store-beta"),
            biz_date=date(2026, 11, 2), user_id=1
        )["id"]
        with pytest.raises(PermissionError):
            db_imei.cancel_transfer(conn, tid, user_id=1, is_admin=False)


def test_transfer_rejects_when_already_transferred(client):
    """同一台同时只能有一条生效调拨：调给乙后再调给甲要报错，调回乙是幂等。"""
    _import_two_stores(client)
    a, b = _sid("store-alpha"), _sid("store-beta")
    with db.get_db() as conn:
        db_imei.record_transfer(
            conn, imei="862000000000001", to_store_id=b, biz_date=date(2026, 11, 2), user_id=1
        )
        same = db_imei.record_transfer(
            conn, imei="862000000000001", to_store_id=b, biz_date=date(2026, 11, 2), user_id=1
        )
        assert same["created"] is False and same["reason"] == "same_target"
        with pytest.raises(ValueError):
            db_imei.record_transfer(
                conn, imei="862000000000001", to_store_id=a, biz_date=date(2026, 11, 2), user_id=1
            )


def test_transfer_rejects_sold_or_unknown_imei(client):
    """已卖掉的机器、池子里没有的串码，都不许调。"""
    _import_two_stores(client)
    a, b = _sid("store-alpha"), _sid("store-beta")
    with db.get_db() as conn:
        # 先把甲店那台卖掉（成交 → 声明）
        deal_id = _deal(conn, store_id=a, imei="862000000000001", phone="15500000061")
        assert db_decl.deal_decl(conn, deal_id) is not None
        with pytest.raises(ValueError):
            db_imei.record_transfer(
                conn, imei="862000000000001", to_store_id=b, biz_date=date(2026, 11, 2), user_id=1
            )
        with pytest.raises(ValueError):
            db_imei.record_transfer(
                conn, imei="869999999999999", to_store_id=b, biz_date=date(2026, 11, 2), user_id=1
            )


def test_transfer_page_records_and_lists(client):
    """真实路由：门店账号在调拨页记一笔，列表能看到；只读账号不能记。"""
    _import_two_stores(client)
    a, b = _sid("store-alpha"), _sid("store-beta")
    _login(client, "alpha", "123456")
    page = client.get("/imei/transfer").get_data(as_text=True)
    assert "调拨台账" in page
    # 乙店那台调进甲店（甲店账号登记，调入店记）
    resp = client.post(
        "/imei/transfer",
        data={
            "imei": "862000000000002",
            "to_store_id": str(a),
            "biz_date": "2026-11-02",
            "note": "从乙店调来",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "已记调拨" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM imei_transfer").fetchone()
        assert row["imei"] == "862000000000002" and int(row["to_store_id"]) == a
        assert row["from_store"] == OTHER  # 按库存清单自动认调出店
        assert int(row["from_store_id"]) == b


def test_transfer_expires_after_next_stock_snapshot(client):
    """调拨只在「快照还没走到调拨日之后」时生效；新清单一到，归属以清单为准。"""
    _import_two_stores(client)
    a, b = _sid("store-alpha"), _sid("store-beta")
    with db.get_db() as conn:
        db_imei.record_transfer(
            conn, imei="862000000000002", to_store_id=a, biz_date=date(2026, 11, 2), user_id=1
        )
        assert "862000000000002" in {o["imei"] for o in db_imei.pool_picker(conn, a)}
        assert "862000000000002" not in {o["imei"] for o in db_imei.pool_picker(conn, b)}
        # 次日库存清单（asof=11-03）把归属仍写成乙店 → 调拨不再影响候选
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"862000000000002,{OTHER},Y600 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-03",
        )
        assert "862000000000002" in {o["imei"] for o in db_imei.pool_picker(conn, b)}
        assert "862000000000002" not in {o["imei"] for o in db_imei.pool_picker(conn, a)}
        row = db_imei.transfer_rows(conn)[0]
        assert row["taken_over"] == 1
        # 接管后允许再调一次（不会撞唯一索引）
        again = db_imei.record_transfer(
            conn, imei="862000000000002", to_store_id=a, biz_date=date(2026, 11, 3), user_id=1
        )
        assert again["created"] is True


# ---- V0.4.06：办理的终端业务决定是否进垫资 + 串码查询 ----

def test_non_rebate_biz_leaves_pool_without_money(client):
    """选「AI手机合约」等非顺差让利 → 这台照样出池，但不记钱、不进待核。"""
    sid = _store_id()
    _login(client)
    _import_stock(client, _stock_row("860000000000030"))

    with db.get_db() as conn:
        deal_id = _deal(
            conn, store_id=sid, imei="860000000000030", phone="15500000030",
            biz_type="AI手机合约",
        )
        decl = db_decl.deal_decl(conn, deal_id)
        assert decl is not None
        assert decl["biz_type"] == "AI手机合约"
        assert int(decl["rebate_cents"]) == 0
        assert decl["state"] == db_decl.DECL_CONFIRMED  # 已办，不是待核
        assert decl["advance_id"] is None
        # 出池：池子里看不到这台了
        assert not any(r["imei"] == "860000000000030" for r in db_imei.pool_rows(conn))
        # 一分钱不参与任何合计，也不进待核队列
        assert db_decl.summary(conn)["n"] == 0
        assert db_decl.confirmed_totals(conn, date(2026, 11, 2), date(2026, 11, 2))["total"] == 0.0
        assert db.advance_month_totals(conn, [sid], date(2026, 11, 2))[sid]["total"] == 0.0
        assert conn.execute("SELECT COUNT(*) FROM advance_posts").fetchone()[0] == 0


def test_empty_biz_type_does_not_create_money(client):
    """没选业务类型 → 有串码也不记钱（只把这台标成已办）。"""
    sid = _store_id()
    _login(client)
    _import_stock(client, _stock_row("860000000000031"))
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei="860000000000031", phone="15500000031", biz_type="")
        decl = db_decl.deal_decl(conn, deal_id)
        assert decl is not None
        assert str(decl["biz_type"]) == ""
        assert int(decl["rebate_cents"]) == 0
        assert decl["state"] == db_decl.DECL_CONFIRMED


def test_rebate_switch_after_deal_recounts_pending(client):
    """串码没变、业务类型改成顺差让利 → 待核金额补上；改成别的 → 钱撤回。"""
    sid = _store_id()
    _login(client)
    _import_stock(client, _stock_row("860000000000032"))
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei="860000000000032", phone="15500000032", biz_type="")
        assert db_decl.summary(conn)["n"] == 0
        _deal(conn, store_id=sid, imei="860000000000032", phone="15500000032", biz_type="顺差让利")
        decl = db_decl.deal_decl(conn, deal_id)
        assert decl["state"] == db_decl.DECL_PENDING
        assert int(decl["rebate_cents"]) == db_decl.DEFAULT_REBATE_CENTS
        assert db_decl.summary(conn)["n"] == 1
        # 改回去：钱撤回，声明仍有效（这台仍出池）
        _deal(conn, store_id=sid, imei="860000000000032", phone="15500000032", biz_type="直降合约")
        decl = db_decl.deal_decl(conn, deal_id)
        assert int(decl["rebate_cents"]) == 0
        assert decl["state"] == db_decl.DECL_CONFIRMED
        assert db_decl.summary(conn)["n"] == 0


def test_confirm_skips_non_rebate_decl(client):
    """已确认的非顺差让利声明不会落垫资行（金额 0 不进 confirm）。"""
    sid = _store_id()
    _login(client)
    _import_stock(client, _stock_row("860000000000033"))
    with db.get_db() as conn:
        deal_id = _deal(
            conn, store_id=sid, imei="860000000000033", phone="15500000033", biz_type="送2G流量"
        )
        decl = db_decl.deal_decl(conn, deal_id)
        result = db_decl.confirm(conn, [decl["id"]], user_id=1)
        assert result == {"confirmed": 0, "skipped": 1}
        assert conn.execute("SELECT COUNT(*) FROM advance_posts").fetchone()[0] == 0


def test_deal_form_posts_biz_type_and_shows_choices(client):
    """触客页有「办理的终端业务」下拉，选完能落库。"""
    sid = _store_id()
    _login(client)
    page = client.get("/deal").get_data(as_text=True)
    assert "办理的终端业务" in page
    assert 'name="biz_type"' in page
    for b in ("顺差让利", "AI手机合约", "直降合约", "送2G流量", "其他"):
        assert b in page
    _import_stock(client, _stock_row("860000000000034"))
    resp = client.post(
        "/deal",
        data={
            "store_id": sid,
            "model": "S60",
            "phone": "15500000034",
            "closed": "1",
            "biz_type": "直降合约",
            "imei_pick": "860000000000034",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "不记让利垫资".encode() in resp.data
    with db.get_db() as conn:
        row = conn.execute("SELECT biz_type FROM deal_posts ORDER BY id DESC LIMIT 1").fetchone()
        assert row["biz_type"] == "直降合约"


def test_imei_lookup_finds_by_tail_and_model(client):
    """查询接口：尾号 / 机型关键字都能命中，只返回池子里的机器。"""
    _login(client)
    own = "示例市甲街vivo体验店"
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000051,{own},S60元气版,小李,2026-11-01,移动顺差\n"
                f"861000000000052,{own},Y600 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
    data = client.get("/imei/lookup?q=000051").get_json()
    assert data["ok"] is True
    assert [r["imei"] for r in data["rows"]] == ["861000000000051"]
    assert data["rows"][0]["tail"] == "000051"
    assert data["rows"][0]["model"] == "S60元气版"
    # 机型关键字（非数字）
    data = client.get("/imei/lookup?q=Y600").get_json()
    assert [r["imei"] for r in data["rows"]] == ["861000000000052"]
    # 太短的串码不做模糊搜索（4 位以下没意义）
    assert client.get("/imei/lookup?q=123").get_json()["rows"] == []
    # 空查询不报错
    assert client.get("/imei/lookup?q=").get_json()["rows"] == []


def test_imei_lookup_accepts_4_and_5_digit_tails(client):
    """4 位 / 5 位尾号也要能查（用户 2026-11-11 反馈：为什么非得 6 位）。"""
    _login(client)
    own = "示例市甲街vivo体验店"
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000061,{own},S60元气版,小李,2026-11-01,移动顺差\n"
                f"861000000000062,{own},Y600 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
    assert [r["imei"] for r in client.get("/imei/lookup?q=0061").get_json()["rows"]] == [
        "861000000000061"
    ]
    assert [r["imei"] for r in client.get("/imei/lookup?q=00062").get_json()["rows"]] == [
        "861000000000062"
    ]


def test_imei_lookup_returns_all_collisions(client):
    """4 位尾号撞车时把几台全列出来，让人自己挑（不是报「没有这台」）。"""
    _login(client)
    own = "示例市甲街vivo体验店"
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000071,{own},S60元气版,小李,2026-11-01,移动顺差\n"
                f"861000000010071,{own},Y600 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
        # 保存路径：撞车时不给唯一结果（宁可不填，也不能填错一台）
        assert db_imei.find_stock_imei(conn, "0071") is None
        # 但查询接口把两台都返回
    rows = client.get("/imei/lookup?q=0071").get_json()["rows"]
    assert {r["imei"] for r in rows} == {"861000000000071", "861000000010071"}


def test_deal_post_with_ambiguous_tail_warns_instead_of_silent(client):
    """手填的尾号撞车时，提示「命中 N 台，请点选」，而不是含糊说池子里没有。"""
    _login(client)
    own = "示例市甲街vivo体验店"
    with db.get_db() as conn:
        db_imei.import_rows(
            conn,
            (
                "imei,store,model,sp,day,串码标签\n"
                f"861000000000081,{own},S60元气版,小李,2026-11-01,移动顺差\n"
                f"861000000010081,{own},Y600 Pro,小李,2026-11-01,移动顺差\n"
            ).encode(),
            asof="2026-11-02",
        )
    resp = client.post(
        "/deal",
        data={
            "store_id": _store_id(),
            "model": "S60",
            "phone": "15500000081",
            "closed": "1",
            "biz_type": "顺差让利",
            "imei": "0081",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "命中 2 台" in html
    with db.get_db() as conn:
        row = conn.execute("SELECT imei FROM deal_posts ORDER BY id DESC LIMIT 1").fetchone()
        assert row["imei"] == ""  # 没瞎猜，先存空
        assert conn.execute("SELECT COUNT(*) FROM imei_decl").fetchone()[0] == 0


def test_imei_lookup_excludes_sold_and_declared(client):
    """已卖出 / 已被声明的机器查不到（避免把卖过的机器又拉回来卖）。"""
    sid = _store_id()
    _login(client)
    imei = _make_store(client, "860000000000035")  # _make_store 已导这台
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="15500000035", biz_type="顺差让利")
    resp = client.get(f"/imei/lookup?q={imei}").get_json()
    assert resp["rows"] == []


def test_rebate_snapshot_not_recomputed_on_unrelated_edit(client):
    """改号码/备注不该重算待核金额：档位是生成时的快照，月中调档不影响已有待核。"""
    sid = _store_id()
    _login(client)
    _import_stock(client, _stock_row("860000000000037"))
    with db.get_db() as conn:
        deal_id = _deal(conn, store_id=sid, imei="860000000000037", phone="15500000037")
        assert int(db_decl.deal_decl(conn, deal_id)["rebate_cents"]) == 10000
        db_decl.set_rebate_cents(conn, 30000)  # 管理员月中调档
        # 同店同日同号再存一次（只改备注）→ 金额仍应是 100
        _deal(conn, store_id=sid, imei="860000000000037", phone="15500000037", biz_type="顺差让利")
        assert int(db_decl.deal_decl(conn, deal_id)["rebate_cents"]) == 10000


def test_report_non_rebate_leaves_pool_without_money(client):
    """自报「已办 + AI手机合约」→ 出池但不记账（钱只看业务类型）。"""
    imei = _make_store(client, "860000000000036")
    _login(client, "alpha", "123456")
    resp = client.post(
        "/imei/report",
        data={"imei": imei, "action": "已办", "biz_type": "AI手机合约", "note": "合约单 A1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "让利垫资待核".encode() not in resp.data
    with db.get_db() as conn:
        decl = conn.execute("SELECT * FROM imei_decl").fetchone()
        assert decl is not None
        assert decl["biz_type"] == "AI手机合约"
        assert int(decl["rebate_cents"]) == 0
        assert decl["state"] == db_decl.DECL_CONFIRMED
        assert db_decl.summary(conn)["n"] == 0
        assert conn.execute("SELECT COUNT(*) FROM advance_posts").fetchone()[0] == 0
        assert not any(r["imei"] == imei for r in db_imei.pool_rows(conn))


def test_imei_lookup_requires_login(client):
    """未登录不能查（接口也要登录态）。"""
    resp = client.get("/imei/lookup?q=861000000000051")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_deal_page_has_scan_button_and_camera_policy(client):
    """扫一扫用系统相机（input capture），不需要网页摄像头权限。

    生产是 HTTP，`navigator.mediaDevices` 在非安全上下文里根本不存在，
    getUserMedia 那条路线上永远打不开摄像头 —— 所以改成 <input capture>，
    权限头也就可以继续全关。
    """
    _login(client)
    client.get("/deal")  # 触发一次 after_request，拿下响应头
    resp = client.get("/deal")
    html = resp.get_data(as_text=True)
    assert 'id="imeiScanBtn"' in html
    assert 'id="imeiInput"' in html
    assert "imei_q" not in html  # 只有一个串码输入框（V0.4.08 合并了重复的两个）
    assert 'capture="environment"' in html
    assert 'id="imeiPhoto"' in html  # 系统相机拍照入口
    assert "<video" not in html  # 不再有网页摄像头预览
    assert resp.headers["Permissions-Policy"] == "camera=(), microphone=(), geolocation=()"
    # blob: 要放行，否则拍照预览图会被 CSP 拦掉
    assert "img-src 'self' data: blob:" in resp.headers["Content-Security-Policy"]


def test_deal_form_has_no_block_element_inside_p(client):
    """`<p>` 里不能放 `<div>`：浏览器会在 `<div>` 处提前闭合 `<p>`。

    V0.4.06 把串码区（下拉 + 输入 + 候选列表）塞进了 `<p>`，结果整格裂成
    5 个网格单元、查询框被挤到 26px、还多出一个空 `<p>` 占位。这条守着自己。
    """
    import re

    _login(client)
    html = client.get("/deal").get_data(as_text=True)
    for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", html, re.S):
        assert "<div" not in m.group(1), "deal.html 的 <p> 里出现了 <div>：%r" % m.group(1)[:80]
    # 串码区必须是跨整行的 div
    assert 'class="span-2 imei-block"' in html


def test_zxing_bundle_served(client):
    """zxing 本地打包（不联网 CDN），并且能被静态路由取到。"""
    _login(client)
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "app/static/vendor/zxing/zxing.min.js"
    assert path.is_file(), "缺 zxing 打包文件"
    assert path.stat().st_size > 200_000
    resp = client.get("/static/vendor/zxing/zxing.min.js")
    assert resp.status_code == 200
    assert b"MultiFormatReader" in resp.data


# ---- 手机号：自报 → 声明 → 确认 → 垫资（V0.4.13）----


def test_report_requires_phone_for_shuncha(client):
    """「已办 + 顺差让利」必须带号码：垫资的钱是按号码付给人的。"""
    imei = _make_store(client, "860000000000040")
    sid = _store_id()
    with db.get_db() as conn:
        with pytest.raises(ValueError):
            db_imei.add_report(
                conn,
                imei=imei,
                store_id=sid,
                action="已办",
                biz_type="顺差让利",
                note="受理单 X",
                user_id=1,
            )
        # 别的业务类型不受这条规则影响
        assert db_imei.add_report(
            conn, imei=imei, store_id=sid, action="已办", biz_type="直降合约", user_id=1
        )


def test_pending_row_shows_phone_from_deal(client):
    """声明快照没号码时，待核页显示触客单上的号码（不是空白）。"""
    imei = _make_store(client, "860000000000041")
    sid = _store_id()
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="15500000041")
        rows = db_decl.pending_rows(conn)
        assert rows and rows[0]["phone"] == "15500000041"
        assert rows[0]["decl_phone"] == ""
        assert rows[0]["deal_phone"] == "15500000041"


def test_pending_row_falls_back_to_mobile_table(client):
    """声明和触客单都没号码时，退到移动回传表。"""
    imei = _make_store(client, "860000000000042")
    sid = _store_id()
    with db.get_db() as conn:
        db_imei.import_mobile(conn, f"串号,号码\n{imei},15500000042\n".encode("utf-8"))
        _deal(conn, store_id=sid, imei=imei, phone="")
        rows = db_decl.pending_rows(conn)
        assert rows and rows[0]["phone"] == "15500000042"


def test_pending_rows_survive_missing_mobile_table(client):
    """辅助表 imei_mobile 不存在（老库/空库）也不能让待核页整页打不开。"""
    imei = _make_store(client, "860000000000043")
    sid = _store_id()
    with db.get_db() as conn:
        conn.execute("DROP TABLE IF EXISTS imei_mobile")
        _deal(conn, store_id=sid, imei=imei, phone="15500000043")
        rows = db_decl.pending_rows(conn)
        assert rows and rows[0]["phone"] == "15500000043"
        assert rows[0]["mobile_phone"] == ""


def test_set_decl_phone_only_touches_pending(client):
    """待核能补号码；已确认的补不了（钱已经在垫资行上了），空号码不收。"""
    imei = _make_store(client, "860000000000044")
    sid = _store_id()
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="")
        decl_id = int(db_decl.pending_rows(conn)[0]["id"])
        assert db_decl.set_decl_phone(conn, decl_id, "")["reason"] == "empty"
        assert db_decl.set_decl_phone(conn, 999999, "15500000044")["reason"] == "missing"
        got = db_decl.set_decl_phone(conn, decl_id, "15500000044")
        assert got["ok"] is True and got["phone"] == "15500000044"
        assert db_decl.set_decl_phone(conn, decl_id, "15500000044")["reason"] == "unchanged"
        # 号码不是钱：金额快照零改动
        assert int(db_decl.pending_rows(conn)[0]["rebate_cents"]) == db_decl.DEFAULT_REBATE_CENTS
        db_decl.confirm(conn, [decl_id], user_id=1)
        assert db_decl.set_decl_phone(conn, decl_id, "15500000099")["reason"] == "not_pending"
        adv = conn.execute("SELECT phone, paid, source FROM advance_posts").fetchone()
        assert adv["phone"] == "15500000044"
        assert int(adv["paid"]) == 0 and adv["source"] == "imei"


def test_pending_page_saves_phone_inline(client, filler_client):
    """待核页行内补号码：店员存不了，管理员存完直接带到确认后的垫资行。"""
    imei = _make_store(client, "860000000000045")
    sid = _store_id()
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="")
        decl_id = int(db_decl.pending_rows(conn)[0]["id"])
    payload = {"action": f"phone:{decl_id}", f"phone_{decl_id}": "15500000045"}
    _login(client, "alpha", "123456")
    assert client.post("/advance/pending", data=payload).status_code == 302
    with db.get_db() as conn:
        assert conn.execute("SELECT phone FROM imei_decl WHERE id=?", (decl_id,)).fetchone()["phone"] == ""
    _login(client, "admin", "123456")
    resp = client.post("/advance/pending", data=payload, follow_redirects=True)
    assert "号码已保存".encode() in resp.data
    with db.get_db() as conn:
        assert conn.execute("SELECT phone FROM imei_decl WHERE id=?", (decl_id,)).fetchone()["phone"] == "15500000045"
    client.post(
        "/advance/pending",
        data={"action": "confirm", "decl_id": [str(decl_id)]},
        follow_redirects=True,
    )
    with db.get_db() as conn:
        adv = conn.execute("SELECT phone FROM advance_posts WHERE source='imei'").fetchone()
        assert adv["phone"] == "15500000045"


def test_pending_page_renders_inline_phone_input(client):
    """待核页每行要有号码输入框 + 存号码按钮（否则补号码还得绕去别处）。"""
    imei = _make_store(client, "860000000000046")
    sid = _store_id()
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="")
        decl_id = int(db_decl.pending_rows(conn)[0]["id"])
    _login(client, "admin", "123456")
    html = client.get("/advance/pending").get_data(as_text=True)
    assert f'name="phone_{decl_id}"' in html
    assert f'value="phone:{decl_id}"' in html


def test_deal_closed_rebate_without_phone_is_refused(client):
    """成交 + 顺差让利却没号码：不落库、钱行不生成，表单原样退回让人当场补。

    这一步号码最好拿——店员正对着客户。等到月末管理员在垫资页补，顺序是反的。
    """
    imei = _make_store(client, "862000000000001")
    sid = _store_id()
    blocked = client.post(
        "/deal",
        data={
            "store_id": str(sid),
            "model": "Y600 Pro",
            "phone": "",
            "closed": "1",
            "biz_type": "顺差让利",
            "imei": imei,
            "note": "客户急着走",
        },
        follow_redirects=True,
    )
    html = blocked.get_data(as_text=True)
    assert blocked.status_code == 200
    assert "顺差让利成交要填客户手机号码" in html
    assert "客户急着走" in html  # 填过的内容不丢，补个号再交
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM deal_posts").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM imei_decl").fetchone()["n"] == 0
        assert any(r["imei"] == imei for r in db_imei.pool_rows(conn))

    ok = client.post(
        "/deal",
        data={
            "store_id": str(sid),
            "model": "Y600 Pro",
            "phone": "15500000123",
            "closed": "1",
            "biz_type": "顺差让利",
            "imei": imei,
            "note": "客户急着走",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已记让利垫资待核" in ok
    with db.get_db() as conn:
        decl = conn.execute("SELECT imei, state FROM imei_decl").fetchone()
        assert decl["imei"] == imei and decl["state"] == db_decl.DECL_PENDING


def test_deal_saves_without_phone_when_no_money_is_involved(client):
    """没号码只拦「要生钱」的那一种单：未成交、或业务不记让利，照常保存。"""
    imei = _make_store(client, "862000000000002")
    sid = _store_id()
    for closed, biz in (("0", "顺差让利"), ("1", "AI手机合约")):
        client.post(
            "/deal",
            data={
                "store_id": str(sid),
                "model": "Y600 Pro",
                "phone": "",
                "closed": closed,
                "biz_type": biz,
                "imei": imei,
            },
            follow_redirects=True,
        )
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM deal_posts").fetchone()["n"] == 2
        money = conn.execute(
            "SELECT COUNT(*) AS n FROM imei_decl WHERE state=? AND rebate_cents>0",
            (db_decl.DECL_PENDING,),
        ).fetchone()["n"]
        assert money == 0


def test_pending_page_flags_missing_phone_and_takes_inline_fix(client):
    """待核页：缺号的要有标记 + 顶部计数，并且能在本页补完再确认。"""
    imei = _make_store(client, "862000000000003")
    sid = _store_id()
    with db.get_db() as conn:
        _deal(conn, store_id=sid, imei=imei, phone="")
        decl_id = int(db_decl.pending_rows(conn)[0]["id"])
    _login(client, "admin", "123456")
    html = client.get("/advance/pending").get_data(as_text=True)
    assert "还没有客户号码" in html
    assert '<div class="muted phone-src warn">缺号码</div>' in html
    after = client.post(
        "/advance/pending",
        data={"action": f"phone:{decl_id}", f"phone_{decl_id}": "15500000124"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "号码已保存" in after
    assert "还没有客户号码" not in after
    assert '<div class="muted phone-src warn">缺号码</div>' not in after
    with db.get_db() as conn:
        got = conn.execute("SELECT phone FROM imei_decl WHERE id=?", (decl_id,)).fetchone()["phone"]
        assert got == "15500000124"
