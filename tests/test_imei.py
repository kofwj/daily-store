"""串码待办：导入、按店可见、门店自报、截图权限。

口径：门店只看自己店；自报只能写自己店；截图只有能看那家店的人能打开；
已售按 POS 销售门店派、未售按库存登记门店派；泰州不参与；档案里没有的店只在管理员可见。

注意：conftest 的 admin_client / filler_client 是同一个 FlaskClient（同一个 client 夹具），
一个用例里同时要两个角色时只能显式再登录一次，否则会话被后一次登录顶掉。
"""
from datetime import date
from io import BytesIO

import pytest

from app import db, db_core, db_imei, views_imei

CSV_HEAD = "imei,store,model,sp,day,age,remain,stage,state,strength,evidence\n"

ALPHA = "示例市甲街vivo体验店"
BETA = "示例市乙街vivo专卖店"


@pytest.fixture(autouse=True)
def _freeze_imei_today(monkeypatch):
    """串码用例固定「今天」= 清单 asof，天数断言才不随真实日期漂。"""
    monkeypatch.setattr(db_imei, "today_local", lambda: date(2026, 11, 2))


def _csv(*rows):
    return BytesIO((CSV_HEAD + "".join(r + "\n" for r in rows)).encode("utf-8"))


def _row(imei, store, model="Y600 Pro", remain=3, state="确认未办"):
    return f"{imei},{store},{model},小李,2026-10-29,4,{remain},D3-6,{state},无,"


def _login(client, username="admin", pin="123456"):
    client.post("/login", data={"username": username, "pin": pin})


def _upload(client, *rows, asof="2026-11-02"):
    return client.post(
        "/imei/import",
        data={"csv_file": (_csv(*rows), "todo.csv"), "asof": asof},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _make_readonly(username="dz_a", code="store-alpha"):
    with db.get_db() as conn:
        sid = conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]
        db.create_user(
            conn,
            username=username,
            display_name="甲店店长",
            pin="123456",
            role="readonly",
            store_ids=[sid],
            scope="",
        )
        conn.execute("UPDATE users SET must_change_pin=0 WHERE username=?", (username,))


def test_admin_import_then_store_sees_only_own(client):
    _login(client)
    resp = _upload(
        client,
        _row("860000000000001", ALPHA),
        _row("860000000000002", BETA),
        _row("860000000000003", "不存在的店"),
    )
    html = resp.get_data(as_text=True)
    assert "入库 3 台" in html
    assert "不存在的店" in html  # 对不上的店名要报出来，不静默丢
    assert "待建店" in html

    _login(client, "alpha")
    today = client.get("/imei").get_data(as_text=True)
    assert "860000000000001" in today
    assert "860000000000002" not in today
    # 手机端入口：每行第一列有「自报」，点开是全屏抽屉（桌面端隐藏）
    assert 'class="btn imei-open"' in today
    assert "imei-mobile-tip" in today
    # 档案里没有的店不给店员看，管理员能看到它挂在「待建店」下
    assert "860000000000003" not in today
    _login(client)
    full = client.get("/imei?show=all").get_data(as_text=True)
    assert "860000000000003" in full and "待建店" in full


def test_filler_report_own_store(client):
    _login(client)
    _upload(client, _row("860000000000011", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={
            "imei": "860000000000011",
            "action": "已办",
            "biz_type": "AI手机合约",
            "note": "受理单 A123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "已记下" in html
    # 报过的机器从「只看要办的」里消失，但「全部」里还能看到谁什么时候报的
    assert "860000000000011" not in html
    full = client.get("/imei?show=all").get_data(as_text=True)
    assert "已办" in full and "受理单 A123" in full
    with db.get_db() as conn:
        row = conn.execute("SELECT action, note, user_id FROM imei_reports").fetchone()
    assert row["action"] == "已办" and row["note"] == "受理单 A123" and row["user_id"]


def test_filler_cannot_report_other_store(client):
    _login(client)
    _upload(client, _row("860000000000021", BETA))
    _login(client, "alpha")
    resp = client.post("/imei/report", data={"imei": "860000000000021", "action": "已办", "biz_type": "直降合约"})
    assert resp.status_code == 403
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_readonly_can_see_cannot_report(client):
    _login(client)
    _upload(client, _row("860000000000031", ALPHA))
    _make_readonly()
    _login(client, "dz_a")
    page = client.get("/imei").get_data(as_text=True)
    assert "860000000000031" in page
    assert 'class="btn imei-open"' not in page and "imei-mobile-tip" not in page
    resp = client.post("/imei/report", data={"imei": "860000000000031", "action": "已办", "biz_type": "顺差让利"})
    assert resp.status_code == 302
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_import_requires_admin(client):
    _login(client, "alpha")
    resp = client.post(
        "/imei/import",
        data={"csv_file": (_csv(_row("860000000000041", ALPHA)), "t.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_todo").fetchone()[0] == 0


def test_photo_upload_scoped_to_store(client):
    _login(client)
    _upload(client, _row("860000000000051", ALPHA))
    _login(client, "alpha")

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    buf.seek(0)
    resp = client.post(
        "/imei/report",
        data={
            "imei": "860000000000051",
            "action": "已办",
            "biz_type": "送2G流量",
            "photo": (buf, "shot.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        name = conn.execute("SELECT photo FROM imei_reports").fetchone()["photo"]
    assert name.endswith(".jpg")
    assert client.get(f"/uploads/imei/{name}").headers["Content-Type"] == "image/jpeg"
    assert (db_core.DATA_DIR / "uploads" / "imei" / name).is_file()
    assert client.get(f"/uploads/imei/{name}").status_code == 200
    # 别家店员看不到（beta 店员）
    _login(client, "beta")
    assert client.get(f"/uploads/imei/{name}").status_code == 404


def test_photo_rejects_non_image(client):
    _login(client)
    _upload(client, _row("860000000000052", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={
            "imei": "860000000000052",
            "action": "已办",
            "biz_type": "直降合约",
            "photo": (BytesIO(b"not an image"), "x.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "这个文件不是图片" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_reimport_hides_gone_rows_but_keeps_them(client):
    _login(client)
    _upload(client, _row("860000000000061", ALPHA))
    _upload(client, _row("860000000000062", ALPHA))
    html = client.get("/imei?show=open").get_data(as_text=True)
    assert "860000000000061" not in html
    assert "860000000000062" in html
    with db.get_db() as conn:
        seen = conn.execute("SELECT seen FROM imei_todo WHERE imei='860000000000061'").fetchone()[
            "seen"
        ]
        total = conn.execute("SELECT COUNT(*) FROM imei_todo").fetchone()[0]
    assert seen == 0 and total == 2


def test_bad_csv_reports_error(client):
    _login(client)
    resp = client.post(
        "/imei/import",
        data={"csv_file": (BytesIO("型号,数量\nX,1\n".encode()), "bad.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "找不到 imei 列" in resp.get_data(as_text=True)


def test_admin_export_reports(client):
    _login(client)
    _upload(client, _row("860000000000071", ALPHA), _row("860000000000072", ALPHA))
    _login(client, "alpha")
    client.post(
        "/imei/report",
        data={
            "imei": "860000000000071",
            "action": "已办",
            "biz_type": "AI手机合约",
            "note": "受理单 B1",
        },
    )
    _login(client)
    resp = client.get("/imei/export")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data().decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert lines[0] == (
        "imei,门店,结论,业务类型,POS证据类型,类型一致性,核销结论,备注,截图,报人,时间,客户类型,手动激活日"
    )
    assert len(lines) == 2
    assert "860000000000071" in lines[1] and "已办" in lines[1] and "受理单 B1" in lines[1]
    assert "AI手机合约" in lines[1] and "已兑现" in lines[1]


def test_export_admin_only(client):
    _login(client, "alpha")
    resp = client.get("/imei/export")
    assert resp.status_code == 302


STOCK_HEAD = "imei,市,零售商名称,商品(SKU),串码标签,库存日期,激活日期,数量\n"


def _stock_csv(*rows):
    return BytesIO((STOCK_HEAD + "".join(r + "\n" for r in rows)).encode("utf-8"))


def test_stock_import_is_unsold_and_dispatched_by_own_store(client):
    """库存明细导入 = 未售，派给库存登记门店；只收「移动顺差」标签。"""
    _login(client)
    body = _stock_csv(
        f"860000000000101,示例市,{ALPHA},Y600 Pro,移动顺差,2026-11-02,,1",
        f"860000000000102,示例市,{ALPHA},Y600 Pro,普通,2026-11-02,,1",
    )
    client.post(
        "/imei/import",
        data={"csv_file": (body, "stock.csv"), "asof": "2026-11-02"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status, own_store, sold_store, store_id FROM imei_todo WHERE imei=?",
            ("860000000000101",),
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM imei_todo").fetchone()[0] == 1
    assert row["status"] == "未售" and row["sold_store"] == "" and row["own_store"] == ALPHA
    assert row["store_id"]

    # 未售的不进「串码待办」（那是已售机器的 7 天时钟），只在池子里
    _login(client, "alpha")
    assert "860000000000101" not in client.get("/imei").get_data(as_text=True)
    pool = client.get("/imei/pool").get_data(as_text=True)
    assert "Y600 Pro" in pool and "1 台" in pool
    detail = client.get(f"/imei/pool?store={ALPHA}").get_data(as_text=True)
    assert "860000000000101" in detail


def test_taizhou_rows_are_skipped(client):
    """泰州不参与：台账和库存两种导入都跳掉，不算入库。"""
    _login(client)
    tz = "TZ泰州某某vivo体验店"
    resp = _upload(client, _row("860000000000111", ALPHA), _row("860000000000112", tz))
    html = resp.get_data(as_text=True)
    assert "入库 1 台" in html and "1 行不是南通门店" in html
    body = _stock_csv(f"860000000000113,泰州市,{tz},Y600 Pro,移动顺差,2026-11-02,,1")
    resp = client.post(
        "/imei/import",
        data={"csv_file": (body, "stock.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "入库 0 台" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_todo").fetchone()[0] == 1


def test_unmapped_store_machine_cannot_be_reported(client):
    """待建店的机器没人能报：没有 store_id 就落不了账。"""
    _login(client)
    _upload(client, _row("860000000000121", "不存在的店"))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000121", "action": "已办", "biz_type": "直降合约"},
        follow_redirects=True,
    )
    assert "还没建账号" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_stock_import_does_not_hide_sold_todo(client):
    """已售和未售各导各的：导库存不能把台账那批标成「不推了」。"""
    _login(client)
    _upload(client, _row("860000000000131", ALPHA))
    body = _stock_csv(f"860000000000132,示例市,{ALPHA},Y600 Pro,移动顺差,2026-11-02,,1")
    client.post(
        "/imei/import",
        data={"csv_file": (body, "stock.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with db.get_db() as conn:
        seen = conn.execute(
            "SELECT status, seen FROM imei_todo WHERE imei=?", ("860000000000131",)
        ).fetchone()
    assert seen["status"] == "已售" and seen["seen"] == 1


# ---- 业务类型（2026-11-08 用户拍板：AI手机合约 / 直降合约 / 顺差让利 / 送2G流量 都算已兑现）----

POS_CSV_HEAD = (
    "imei,store,model,sp,day,age,remain,stage,state,strength,evidence,POS业务类型\n"
)


def _upload_pos(client, *rows, asof="2026-11-02"):
    body = BytesIO((POS_CSV_HEAD + "".join(r + "\n" for r in rows)).encode("utf-8"))
    return client.post(
        "/imei/import",
        data={"csv_file": (body, "todo.csv"), "asof": asof},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _pos_row(imei, store, pos_type, remain=3):
    return f"{imei},{store},Y600 Pro,小李,2026-10-29,4,{remain},D3-6,确认未办,无,,{pos_type}"


def test_report_requires_biz_type(client):
    """业务类型必填：不选就不写库，页面给提示。"""
    _login(client)
    _upload(client, _row("860000000000201", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000201", "action": "已办"},
        follow_redirects=True,
    )
    assert "业务类型要选一个" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_report_other_requires_note(client):
    """选「其他」必须写明是什么业务；写了才落库。"""
    _login(client)
    _upload(client, _row("860000000000202", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000202", "action": "已办", "biz_type": "其他"},
        follow_redirects=True,
    )
    assert "写清是什么业务" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0

    ok = client.post(
        "/imei/report",
        data={
            "imei": "860000000000202",
            "action": "已办",
            "biz_type": "其他",
            "note": "融合套餐（宽带+手机）",
        },
        follow_redirects=True,
    )
    assert "已记下" in ok.get_data(as_text=True)
    with db.get_db() as conn:
        row = conn.execute("SELECT action, biz_type, note FROM imei_reports").fetchone()
    assert row["biz_type"] == "其他" and "宽带" in row["note"]


def test_report_rejects_unknown_biz_type(client):
    """不在四个选项 + 其他里的类型一律不收（防手工改表单塞脏值）。"""
    _login(client)
    _upload(client, _row("860000000000203", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000203", "action": "已办", "biz_type": "随便写写"},
        follow_redirects=True,
    )
    assert "业务类型只能选" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_report_alias_normalized(client):
    """POS 口径的简写（顺差）在自报里归一成标准写法（顺差让利）。"""
    _login(client)
    _upload(client, _row("860000000000204", ALPHA))
    _login(client, "alpha")
    client.post(
        "/imei/report",
        data={
            "imei": "860000000000204",
            "action": "已办",
            "biz_type": "顺差让利",
            "note": "受理单 G204",
        },
        follow_redirects=True,
    )
    with db.get_db() as conn:
        assert conn.execute("SELECT biz_type FROM imei_reports").fetchone()[0] == "顺差让利"


def test_filler_cannot_report_other_store_with_biz_type(client):
    """越权：带业务类型也写不了别家店的机器，且一条都不落库。"""
    _login(client)
    _upload(client, _row("860000000000205", BETA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000205", "action": "已办", "biz_type": "AI手机合约"},
    )
    assert resp.status_code == 403
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_readonly_cannot_report_even_with_biz_type(client):
    """越权：只读（店长）带业务类型也不能自报。"""
    _login(client)
    _upload(client, _row("860000000000206", ALPHA))
    _make_readonly(username="dz_b")
    _login(client, "dz_b")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000206", "action": "已办", "biz_type": "直降合约"},
        follow_redirects=True,
    )
    assert "只读账号只能看" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_export_flags_pos_type_mismatch(client):
    """核销导出：保留门店报的类型；跟 POS 类型不一致只标注，判定仍是已兑现。"""
    _login(client)
    _upload_pos(
        client,
        _pos_row("860000000000211", ALPHA, "直降合约"),
        _pos_row("860000000000212", ALPHA, "AI手机"),
        _pos_row("860000000000213", ALPHA, ""),
    )
    _login(client, "alpha")
    for imei, biz, note in (
        ("860000000000211", "AI手机合约", ""),
        ("860000000000212", "AI手机合约", ""),
        ("860000000000213", "送2G流量", "受理单 G213"),
    ):
        client.post(
            "/imei/report",
            data={"imei": imei, "action": "已办", "biz_type": biz, "note": note},
            follow_redirects=True,
        )
    _login(client)
    body = client.get("/imei/export").get_data().decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert lines[0].split(",")[3:] == [
        "业务类型",
        "POS证据类型",
        "类型一致性",
        "核销结论",
        "备注",
        "截图",
        "报人",
        "时间",
        "客户类型",
        "手动激活日",
    ]
    by_imei = {line.split(",")[0]: line for line in lines[1:]}
    mismatch = by_imei["860000000000211"].split(",")
    assert mismatch[3] == "AI手机合约" and mismatch[4] == "直降合约"
    assert mismatch[5] == "不一致" and mismatch[6] == "已兑现"
    same = by_imei["860000000000212"].split(",")
    assert same[5] == "一致" and same[6] == "已兑现"
    # 送2G流量 POS 基本不记：不判「无POS类型（未比对）」，直接标仅门店自报
    no_pos = by_imei["860000000000213"].split(",")
    assert no_pos[4] == "" and no_pos[5] == "系统无记录（待核）" and no_pos[6] == "已兑现"


def test_export_other_type_needs_manual_check(client):
    """门店报「其他」：导出标人工核，不自动算已兑现。"""
    _login(client)
    _upload(client, _row("860000000000221", ALPHA))
    _login(client, "alpha")
    client.post(
        "/imei/report",
        data={
            "imei": "860000000000221",
            "action": "已办",
            "biz_type": "其他",
            "note": "融合套餐",
        },
        follow_redirects=True,
    )
    _login(client)
    body = client.get("/imei/export").get_data().decode("utf-8-sig")
    row = body.strip().splitlines()[1].split(",")
    assert row[3] == "其他" and row[6] == "待人工核（其他）"


def test_legacy_reports_table_gets_biz_type_column(client):
    """老库（imei_reports 已建、没有 biz_type）自动补列，不丢已报数据。"""
    with db.get_db() as conn:
        conn.execute("DROP TABLE IF EXISTS imei_reports")
        conn.execute(
            """
            CREATE TABLE imei_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imei TEXT NOT NULL,
                store_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                photo TEXT NOT NULL DEFAULT '',
                user_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        sid = conn.execute("SELECT id FROM stores WHERE code='store-alpha'").fetchone()["id"]
        conn.execute(
            "INSERT INTO imei_reports(imei, store_id, action, note, user_id, created_at)"
            " VALUES ('860000000000231', ?, '已办', '', 1, '2026-11-01 10:00:00')",
            (sid,),
        )
        from app import db_imei

        db_imei.ensure(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(imei_reports)")}
        assert "biz_type" in cols
        kept = conn.execute("SELECT imei, biz_type FROM imei_reports").fetchone()
        assert kept["imei"] == "860000000000231" and kept["biz_type"] == ""


# ---- 送2G流量 / 顺差让利：系统没记录，必须留受理单或截图（2026-11-09 用户口径）----


def _png():
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (30, 120, 200)).save(buf, format="PNG")
    buf.seek(0)
    return buf


def test_2g_flow_requires_note_or_photo(client):
    """送2G流量：只选类型不给证据 → 拒；给说明或截图才收。"""
    _login(client)
    _upload(client, _row("860000000000301", ALPHA), _row("860000000000302", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000301", "action": "已办", "biz_type": "送2G流量"},
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert "系统记录不全" in html and "请务必上传受理单或截图" in html
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0

    ok = client.post(
        "/imei/report",
        data={
            "imei": "860000000000302",
            "action": "已办",
            "biz_type": "送2G流量",
            "photo": (_png(), "shot.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "已记下" in ok.get_data(as_text=True)
    with db.get_db() as conn:
        row = conn.execute("SELECT biz_type, photo, note FROM imei_reports").fetchone()
    assert row["biz_type"] == "送2G流量" and row["photo"].endswith(".jpg") and row["note"] == ""


def test_shuncha_requires_note_or_photo(client):
    """顺差让利：POS 只约 9% 能对上，同样必须留证据。"""
    _login(client)
    _upload(client, _row("860000000000311", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={"imei": "860000000000311", "action": "已办", "biz_type": "顺差让利"},
        follow_redirects=True,
    )
    assert "系统记录不全" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0

    ok = client.post(
        "/imei/report",
        data={
            "imei": "860000000000311",
            "action": "已办",
            "biz_type": "顺差让利",
            "note": "受理单 S311",
        },
        follow_redirects=True,
    )
    assert "已记下" in ok.get_data(as_text=True)


def test_checkable_types_need_no_evidence(client):
    """AI手机合约 / 直降合约 系统可核，不用强制写说明。"""
    _login(client)
    _upload(client, _row("860000000000321", ALPHA), _row("860000000000322", ALPHA))
    _login(client, "alpha")
    for imei, biz in (("860000000000321", "AI手机合约"), ("860000000000322", "直降合约")):
        resp = client.post(
            "/imei/report",
            data={"imei": imei, "action": "已办", "biz_type": biz},
            follow_redirects=True,
        )
        assert "已记下" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 2


def test_other_still_requires_note_not_photo(client):
    """「其他」是必须写说明（截图不能代替），跟上面两类的规则不同。"""
    _login(client)
    _upload(client, _row("860000000000331", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={
            "imei": "860000000000331",
            "action": "已办",
            "biz_type": "其他",
            "photo": (_png(), "shot.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "写清是什么业务" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_reports").fetchone()[0] == 0


def test_list_shows_evidence_label(client):
    """列表按类区分：可核的标「系统可核」，两类标「仅门店自报·无系统证据」。"""
    _login(client)
    _upload(
        client,
        _row("860000000000341", ALPHA),
        _row("860000000000342", ALPHA),
    )
    _login(client, "alpha")
    client.post(
        "/imei/report",
        data={"imei": "860000000000341", "action": "已办", "biz_type": "AI手机合约"},
        follow_redirects=True,
    )
    client.post(
        "/imei/report",
        data={
            "imei": "860000000000342",
            "action": "已办",
            "biz_type": "顺差让利",
            "note": "受理单 S342",
        },
        follow_redirects=True,
    )
    html = client.get("/imei?show=all").get_data(as_text=True)
    assert "系统可核" in html
    assert "仅门店自报·POS记录不全" in html


def test_export_never_flags_no_evidence_types(client):
    """送2G流量/顺差让利 即便 POS 有别的类型，也只标「一致」或「系统无记录（待核）」。"""
    _login(client)
    _upload_pos(
        client,
        _pos_row("860000000000351", ALPHA, "直降合约"),
        _pos_row("860000000000352", ALPHA, "AI手机合约"),
    )
    _login(client, "alpha")
    for imei, biz in (("860000000000351", "送2G流量"), ("860000000000352", "顺差让利")):
        client.post(
            "/imei/report",
            data={"imei": imei, "action": "已办", "biz_type": biz, "note": "受理单 T"},
            follow_redirects=True,
        )
    _login(client)
    body = client.get("/imei/export").get_data().decode("utf-8-sig")
    by_imei = {line.split(",")[0]: line.split(",") for line in body.strip().splitlines()[1:]}
    for imei in ("860000000000351", "860000000000352"):
        # 记录不全的两类只给「一致」/「系统无记录（待核）」，绝不判不一致
        assert by_imei[imei][5] in ("一致", "系统无记录（待核）")
        assert "不一致" not in by_imei[imei][5]
        assert by_imei[imei][6] == "已兑现"


def test_export_marks_match_when_pos_happens_to_record(client):
    """POS 恰好记了送2G流量（购机送12GB）→ 标「一致」，不是「待核」。"""
    _login(client)
    _upload_pos(client, _pos_row("860000000000361", ALPHA, "送2G流量"))
    _login(client, "alpha")
    client.post(
        "/imei/report",
        data={
            "imei": "860000000000361",
            "action": "已办",
            "biz_type": "送2G流量",
            "note": "受理单 T361",
        },
        follow_redirects=True,
    )
    _login(client)
    body = client.get("/imei/export").get_data().decode("utf-8-sig")
    row = body.strip().splitlines()[1].split(",")
    assert row[3] == "送2G流量" and row[4] == "送2G流量"
    assert row[5] == "一致" and row[6] == "已兑现"


# ---- 客户端客户类型：异地异网待激活 / 手动激活日起算 ----------------------------------


def _mark(client, imei, mark, act_date=""):
    data = {"imei": imei, "mark": mark}
    if act_date:
        data["act_date"] = act_date
    return client.post("/imei/mark", data=data, follow_redirects=True)


def _store_id(code="store-alpha"):
    with db.get_db() as conn:
        return conn.execute("SELECT id FROM stores WHERE code=?", (code,)).fetchone()["id"]


def _todo_row(imei):
    with db.get_db() as conn:
        return conn.execute(
            "SELECT user_type, manual_activated_at FROM imei_todo WHERE imei=?", (imei,)
        ).fetchone()


def _cards(store_ids):
    with db.get_db() as conn:
        return db_imei.summary(conn, store_ids)


def test_mark_defaults_to_local_mobile(client):
    _login(client)
    _upload(client, _row("860000000000401", ALPHA))
    row = _todo_row("860000000000401")
    assert row["user_type"] == "本地移动" and row["manual_activated_at"] == ""
    cards = _cards([_store_id()])
    assert cards["pending"] == 0 and cards["activated"] == 0


def test_mark_remote_pending_drops_out_of_overdue(client):
    _login(client)
    # 已售 12 天、remain -5：本来是逾期
    _upload(client, f"860000000000402,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    sid = _store_id()
    assert _cards([sid])["overdue"] == 1
    html = _mark(client, "860000000000402", "异网").get_data(as_text=True)
    assert "异地异网待激活" in html
    row = _todo_row("860000000000402")
    assert row["user_type"] == "异地异网" and row["manual_activated_at"] == ""
    cards = _cards([sid])
    assert cards["pending"] == 1 and cards["overdue"] == 0 and cards["soon"] == 0
    page = client.get("/imei").get_data(as_text=True)
    assert "待激活" in page and "等激活" in page
    assert "门店用改串机" in page
    # 待激活不混在逾期里：这一行不出现「逾期 5 天」
    assert "逾期 5 天" not in page


def test_mark_activated_restarts_seven_days(client):
    _login(client)
    _upload(client, f"860000000000403,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    sid = _store_id()
    _mark(client, "860000000000403", "已激活", "2026-11-01")
    row = _todo_row("860000000000403")
    assert row["manual_activated_at"] == "2026-11-01"
    cards = _cards([sid])
    assert cards["activated"] == 1 and cards["overdue"] == 0 and cards["soon"] == 1
    page = client.get("/imei").get_data(as_text=True)
    assert "剩 6 天" in page and "自 11-01 起算" in page


def test_activated_row_goes_overdue_again_after_window(client):
    _login(client)
    _upload(client, f"860000000000404,{ALPHA},Y600 Pro,小李,2026-10-01,32,-25,D7+,确认未办,无,")
    sid = _store_id()
    _mark(client, "860000000000404", "已激活", "2026-10-20")
    cards = _cards([sid])
    # asof 2026-11-02 - 2026-10-20 = 13 天 → 逾期 6 天
    assert cards["overdue"] == 1 and cards["soon"] == 0
    assert "逾期 6 天" in client.get("/imei").get_data(as_text=True)


def test_mark_rejects_bad_activation_dates(client):
    _login(client)
    _upload(client, f"860000000000405,{ALPHA},Y600 Pro,小李,2026-10-29,4,3,D3-6,确认未办,无,")
    html = _mark(client, "860000000000405", "已激活", "2099-01-01").get_data(as_text=True)
    assert "不能填将来的日期" in html
    html = _mark(client, "860000000000405", "已激活", "2026-01-01").get_data(as_text=True)
    assert "不能早于售出日" in html
    html = _mark(client, "860000000000405", "已激活", "2026/11/02").get_data(as_text=True)
    assert "激活日要填成" in html
    assert _todo_row("860000000000405")["manual_activated_at"] == ""


def test_mark_permissions(client):
    _login(client)
    _upload(
        client,
        _row("860000000000406", ALPHA),
        _row("860000000000407", BETA),
    )
    # 店员只能标自己店；别店 403 且库里不动
    _login(client, "alpha")
    resp = client.post("/imei/mark", data={"imei": "860000000000407", "mark": "异网"})
    assert resp.status_code == 403
    assert _todo_row("860000000000407")["user_type"] == "本地移动"
    # 只读账号（店长）不能标
    _make_readonly()
    _login(client, "dz_a")
    html = _mark(client, "860000000000406", "异网").get_data(as_text=True)
    assert "只读账号只能看" in html
    assert _todo_row("860000000000406")["user_type"] == "本地移动"


def test_mark_unmapped_store_rejected(client):
    _login(client)
    _upload(client, _row("860000000000408", "不存在的店"))
    html = _mark(client, "860000000000408", "异网").get_data(as_text=True)
    assert "先补门店档案" in html
    assert _todo_row("860000000000408")["user_type"] == "本地移动"


def test_mark_undo_returns_to_local(client):
    _login(client)
    _upload(client, f"860000000000409,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    _mark(client, "860000000000409", "异网")
    assert _cards([_store_id()])["pending"] == 1
    _mark(client, "860000000000409", "撤销")
    row = _todo_row("860000000000409")
    assert row["user_type"] == "本地移动" and row["manual_activated_at"] == ""
    assert _cards([_store_id()])["pending"] == 0


def test_mark_unknown_action_rejected(client):
    _login(client)
    _upload(client, _row("860000000000410", ALPHA))
    html = _mark(client, "860000000000410", "随便标").get_data(as_text=True)
    assert "动作只能是" in html
    assert _todo_row("860000000000410")["user_type"] == "本地移动"


def test_reimport_keeps_manual_marks(client):
    _login(client)
    _upload(client, f"860000000000411,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    _mark(client, "860000000000411", "异网")
    _upload(client, f"860000000000411,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    row = _todo_row("860000000000411")
    assert row["user_type"] == "异地异网" and row["manual_activated_at"] == ""


def test_admin_sees_per_store_mark_counts(client):
    _login(client)
    rows = [
        f"86000000000042{i},{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无," for i in range(4)
    ] + [f"86000000000043{i},{BETA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无," for i in range(3)]
    _upload(client, *rows)
    for i in range(2):  # 甲店 2/4：占一半但只有 2 台，不算「偏高」
        _mark(client, f"86000000000042{i}", "异网")
    for i in range(3):  # 乙店 3/3：≥3 台且过半 → 偏高
        _mark(client, f"86000000000043{i}", "异网")
    with db.get_db() as conn:
        marks = {
            m["short"]: m
            for m in db_imei.mark_summary(conn, [_store_id(), _store_id("store-beta")])
        }
    a = [m for m in marks.values() if m["open_n"] == 4][0]
    b = [m for m in marks.values() if m["open_n"] == 3][0]
    assert a["pending_n"] == 2 and a["ratio"] == 50 and not a["suspicious"]
    assert b["pending_n"] == 3 and b["ratio"] == 100 and b["suspicious"]
    page = client.get("/imei").get_data(as_text=True)
    assert "各店标记台数" in page and "偏高" in page and "100%" in page


def test_admin_export_todo_carries_mark_columns(client):
    _login(client)
    _upload(client, f"860000000000440,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    _mark(client, "860000000000440", "已激活", "2026-11-01")
    resp = client.get("/imei/export/todo")
    assert resp.status_code == 200
    body = resp.get_data().decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert lines[0].split(",")[9:14] == ["客户类型", "手动激活日", "标记状态", "数据标记", "生效剩余天数"]
    row = {line.split(",")[0]: line.split(",") for line in lines[1:]}["860000000000440"]
    assert row[9] == "本地移动" and row[10] == "2026-11-01" and row[11] == "已激活" and row[12] == "" and row[13] == "6"
    # 非管理员不给导
    _login(client, "alpha")
    assert client.get("/imei/export/todo").status_code == 302


def test_export_reports_appends_mark_columns(client):
    _login(client)
    _upload(client, f"860000000000441,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    _mark(client, "860000000000441", "异网")
    _login(client, "alpha")
    client.post(
        "/imei/report",
        data={"imei": "860000000000441", "action": "待核", "biz_type": "AI手机合约"},
        follow_redirects=True,
    )
    _login(client)
    line = client.get("/imei/export").get_data().decode("utf-8-sig").strip().splitlines()[1].split(",")
    assert line[11] == "异地异网" and line[12] == ""


def test_remote_then_activated_keeps_remote_mark(client):
    """先标异网再点已激活：类型还是异地异网，但不再算待激活。"""
    _login(client)
    _upload(client, f"860000000000450,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")
    _mark(client, "860000000000450", "异网")
    _mark(client, "860000000000450", "已激活", "2026-11-02")
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT user_type, manual_activated_at FROM imei_todo WHERE imei=?",
            ("860000000000450",),
        ).fetchone()
        cards = db_imei.summary(conn, [_store_id()])
    assert row["user_type"] == "异地异网" and row["manual_activated_at"] == "2026-11-02"
    # 激活当天算第 0 天 → 还有整整 7 天，既不是「7 天内到期」也没逾期
    assert cards["pending"] == 0 and cards["activated"] == 1 and cards["soon"] == 0
    page = client.get("/imei").get_data(as_text=True)
    assert "异地异网·已激活 11-02" in page and "剩 7 天" in page


# ---- 2026-11-09 回归：非已办不逼选业务类型 / 激活锁死起算日 / 泰州隔离 / 卡片口径 ----


def test_non_done_actions_do_not_require_biz_type(client):
    """选 已退货 / 待核 时不需要选合约业务类型，带了残留值也不入库、不逼传凭证。"""
    _login(client)
    _upload(
        client,
        _row("860000000000501", ALPHA),
        _row("860000000000502", ALPHA),
        _row("860000000000503", ALPHA),
        _row("860000000000504", ALPHA),
    )
    _login(client, "alpha")

    # 已办 仍然必须选合约业务类型
    bad = client.post(
        "/imei/report",
        data={"imei": "860000000000501", "action": "已办", "biz_type": ""},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "业务类型要选一个" in bad

    # 已退货（就算前端残留了「送2G流量」且没传截图/备注）直接放行，库里 biz_type 清空
    r1 = client.post(
        "/imei/report",
        data={"imei": "860000000000501", "action": "已退货", "biz_type": "送2G流量"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已记下：000501 → 已退货" in r1
    assert "已退货 ·" not in r1  # 不挂多余的分隔点

    # 待核 完全不带 biz_type
    r3 = client.post(
        "/imei/report",
        data={"imei": "860000000000503", "action": "待核", "note": "核实中"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "已记下：000503 → 待核" in r3

    # 「非本店」已下线：下拉里没有，硬发 POST 也被拒，库里不落记录
    page = client.get("/imei").get_data(as_text=True)
    assert '<option value="非本店">' not in page
    r2 = client.post(
        "/imei/report",
        data={"imei": "860000000000502", "action": "非本店"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "结论只能是" in r2

    with db.get_db() as conn:
        rows = {
            r["imei"]: r
            for r in conn.execute("SELECT imei, action, biz_type FROM imei_reports").fetchall()
        }
    assert rows["860000000000501"]["action"] == "已退货" and rows["860000000000501"]["biz_type"] == ""
    assert "860000000000502" not in rows
    assert rows["860000000000503"]["action"] == "待核" and rows["860000000000503"]["biz_type"] == ""


def test_legacy_non_own_store_report_still_displays_and_exports(client):
    """历史「非本店」不再接受新报，但老记录原样显示、原样导出，不做数据清洗。"""
    _login(client)
    _upload(client, _row("860000000000505", ALPHA))
    sid = _store_id()
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO imei_reports(imei, store_id, action, biz_type, note, photo, user_id, created_at)"
            " VALUES (?, ?, '非本店', '', '历史记录', '', NULL, ?)",
            ("860000000000505", sid, "2026-11-01 10:00:00"),
        )
    page = client.get("/imei?show=all").get_data(as_text=True)
    assert "历史记录" in page and ">非本店<" in page
    body = client.get("/imei/export").get_data().decode("utf-8-sig")
    assert "860000000000505" in body and "非本店" in body


def test_summary_and_mark_summary_exclude_closed_reports(client):
    """自报了「已办 / 已退货」的机器不再算进页顶待办/逾期/待激活，也不再稀释或虚增各店标记台数。"""
    _login(client)
    _upload(
        client,
        f"860000000000511,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,",
        f"860000000000512,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,",
        f"860000000000513,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,",
    )
    _mark(client, "860000000000512", "异网")
    sid = _store_id()
    c0 = _cards([sid])
    assert (c0["open"], c0["overdue"], c0["soon"], c0["reported"], c0["pending"], c0["activated"]) == (
        3,
        2,
        0,
        0,
        1,
        0,
    )

    _login(client, "alpha")
    client.post(
        "/imei/report",
        data={"imei": "860000000000511", "action": "已办", "biz_type": "AI手机合约"},
    )
    client.post(
        "/imei/report",
        data={"imei": "860000000000512", "action": "已退货"},
    )
    # 待核还没结掉，仍留在待办里
    client.post(
        "/imei/report",
        data={"imei": "860000000000513", "action": "待核"},
    )
    c1 = _cards([sid])
    assert (c1["open"], c1["overdue"], c1["soon"], c1["reported"], c1["pending"], c1["activated"]) == (
        1,
        1,
        0,
        3,
        0,
        0,
    )
    with db.get_db() as conn:
        marks = db_imei.mark_summary(conn, [sid])
    assert marks[0]["open_n"] == 1 and marks[0]["pending_n"] == 0


def test_activated_after_asof_clamps_age_to_zero(client):
    """T-1 台账（asof=昨天）下门店填今天已激活：已过天数钳在 0，显示剩 7 天，绝不出 -1 天 / 剩 8 天。"""
    _login(client)
    _upload(
        client,
        f"860000000000521,{ALPHA},Y600 Pro,小李,2026-10-25,7,0,D3-6,确认未办,无,",
        asof="2026-11-01",
    )
    _mark(client, "860000000000521", "已激活", "2026-11-02")
    cards = _cards([_store_id()])
    assert cards["activated"] == 1 and cards["overdue"] == 0 and cards["soon"] == 0
    page = client.get("/imei").get_data(as_text=True)
    assert "剩 7 天" in page
    assert "剩 8 天" not in page and "-1 天" not in page


def test_filler_cannot_undo_and_activated_date_is_locked(client):
    """撤销只允许管理员；门店点过「已激活」后锁死起算日，不能重填或改标异网。"""
    _login(client)
    _upload(client, f"860000000000531,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,")

    _login(client, "alpha")
    # 1) 门店能标异网，但页面上没有「撤销标记」，强发 POST /imei/mark mark=撤销 返 403
    assert "异地异网待激活" in _mark(client, "860000000000531", "异网").get_data(as_text=True)
    assert "撤销标记" not in client.get("/imei").get_data(as_text=True)
    assert client.post("/imei/mark", data={"imei": "860000000000531", "mark": "撤销"}).status_code == 403
    assert _todo_row("860000000000531")["user_type"] == "异地异网"

    # 2) 门店点一次「已激活」成功，之后页面隐藏 mark-form，再 POST 已激活或异网都被拦下
    assert "起算" in _mark(client, "860000000000531", "已激活", "2026-11-01").get_data(as_text=True)
    assert _todo_row("860000000000531")["manual_activated_at"] == "2026-11-01"
    filler_html = client.get("/imei").get_data(as_text=True)
    assert 'class="mark-form"' not in filler_html

    re_act = _mark(client, "860000000000531", "已激活", "2026-11-02").get_data(as_text=True)
    assert "起算日已锁定" in re_act
    assert _todo_row("860000000000531")["manual_activated_at"] == "2026-11-01"

    re_remote = _mark(client, "860000000000531", "异网").get_data(as_text=True)
    assert "起算日已锁定" in re_remote
    assert _todo_row("860000000000531")["manual_activated_at"] == "2026-11-01"

    assert client.post("/imei/mark", data={"imei": "860000000000531", "mark": "撤销"}).status_code == 403

    # 3) 管理员可以重标或撤销解锁
    _login(client)
    admin_html = client.get("/imei").get_data(as_text=True)
    assert "撤销标记" in admin_html
    _mark(client, "860000000000531", "撤销")
    row = _todo_row("860000000000531")
    assert row["user_type"] == "本地移动" and row["manual_activated_at"] == ""


def test_stock_import_does_not_overwrite_active_sold_machine(client):
    """滞后的库存快照里还带着已售串码时，绝不能把活跃已售待办覆盖成「未售」。"""
    _login(client)
    _upload(client, _row("860000000000550", ALPHA))
    body = _stock_csv(
        f"860000000000550,示例市,{ALPHA},Y600 Pro,移动顺差,2026-11-02,,1",
        f"860000000000551,示例市,{ALPHA},Y600 Pro,移动顺差,2026-11-02,,1",
    )
    resp = client.post(
        "/imei/import",
        data={"csv_file": (body, "stock.csv"), "asof": "2026-11-02"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "入库 1 台" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        sold = conn.execute(
            "SELECT status, seen, sold_store FROM imei_todo WHERE imei='860000000000550'"
        ).fetchone()
        unsold = conn.execute(
            "SELECT status, seen FROM imei_todo WHERE imei='860000000000551'"
        ).fetchone()
    assert sold["status"] == "已售" and sold["seen"] == 1 and sold["sold_store"] == ALPHA
    assert unsold["status"] == "未售" and unsold["seen"] == 1


def test_taizhou_accounts_hide_nav_and_forbid_access(client):
    """泰州账号（店员 / 店长 / 地市负责人）顶栏隐藏「串码待办」，直接访问 /imei* 全返 403。"""
    from PIL import Image

    _login(client)
    _upload(client, _row("860000000000560", ALPHA))

    # 先传一张甲店截图，验证泰州账号连截图路由也是 404
    _login(client, "alpha")
    buf = BytesIO()
    Image.new("RGB", (8, 8), (20, 120, 200)).save(buf, format="PNG")
    buf.seek(0)
    client.post(
        "/imei/report",
        data={
            "imei": "860000000000560",
            "action": "已办",
            "biz_type": "送2G流量",
            "photo": (buf, "p.png"),
        },
        content_type="multipart/form-data",
    )
    with db.get_db() as conn:
        photo_name = conn.execute("SELECT photo FROM imei_reports").fetchone()["photo"]
        cur = conn.execute(
            "INSERT INTO stores(name, code, city, sort_order, active, created_at) "
            "VALUES('TZ泰州市海陵区测试店', 'store-tz', '泰州市', 99, 1, '2026-01-01')"
        )
        tz_sid = cur.lastrowid
        db.create_user(
            conn,
            username="tz_filler",
            display_name="泰州店员",
            pin="123456",
            role="filler",
            store_ids=[tz_sid],
            scope="",
        )
        db.create_user(
            conn,
            username="tz_city",
            display_name="泰州负责人",
            pin="123456",
            role="city",
            store_ids=[],
            scope="泰州市",
        )
        conn.execute("UPDATE users SET must_change_pin=0 WHERE username IN ('tz_filler', 'tz_city')")

    # 南通/示例市账号正常看到导航并能进
    alpha_nav = client.get("/today").get_data(as_text=True)
    assert "串码待办" in alpha_nav
    assert client.get("/imei").status_code == 200

    # 泰州店员：顶栏无「串码待办」，四个串码路由全 403，截图 404
    _login(client, "tz_filler")
    tz_filler_nav = client.get("/today").get_data(as_text=True)
    assert "串码待办" not in tz_filler_nav
    assert client.get("/imei").status_code == 403
    assert client.get("/imei/pool").status_code == 403
    assert client.post("/imei/report", data={"imei": "860000000000560", "action": "待核"}).status_code == 403
    assert client.post("/imei/mark", data={"imei": "860000000000560", "mark": "异网"}).status_code == 403
    assert client.get(f"/uploads/imei/{photo_name}").status_code == 404

    # 泰州地市负责人同样隐藏导航、访问 403
    _login(client, "tz_city")
    tz_city_nav = client.get("/compare").get_data(as_text=True)
    assert "串码待办" not in tz_city_nav
    assert client.get("/imei").status_code == 403
    assert client.get("/imei/pool").status_code == 403


def test_photo_exif_orientation_transposed(client):
    """手机竖拍带 EXIF Orientation=6（顺时针 90°）时，落盘前自动扶正像素并剥除 EXIF。"""
    from PIL import Image

    _login(client)
    _upload(client, _row("860000000000570", ALPHA))
    _login(client, "alpha")

    im = Image.new("RGB", (40, 20), (100, 150, 200))
    exif = im.getexif()
    exif[0x0112] = 6  # Orientation = Rotate 90 CW → 扶正后应变成 20x40
    buf = BytesIO()
    im.save(buf, format="JPEG", exif=exif)
    buf.seek(0)

    resp = client.post(
        "/imei/report",
        data={
            "imei": "860000000000570",
            "action": "已办",
            "biz_type": "顺差让利",
            "photo": (buf, "cam.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db.get_db() as conn:
        name = conn.execute("SELECT photo FROM imei_reports").fetchone()["photo"]
    saved = Image.open(db_core.DATA_DIR / "uploads" / "imei" / name)
    assert saved.size == (20, 40)
    assert 0x0112 not in saved.getexif()


# ---- 截图落盘：长截图保清晰 + 统一转 JPEG（2026-11-09 用户拍板）----


def _make_img(w, h, *, fmt="PNG", transparent=False, noise=False, exif_rotate=None):
    from PIL import Image

    if noise:
        im = Image.effect_noise((w, h), 64).convert("RGB")
    elif transparent:
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        im.paste((0, 0, 0, 255), (w // 2, 0, w, h))
    else:
        im = Image.new("RGB", (w, h), (30, 120, 200))
    buf = BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = exif_rotate
        im.save(buf, format=fmt, exif=exif)
    else:
        im.save(buf, format=fmt)
    return buf.getvalue()


def _open(data):
    from PIL import Image

    im = Image.open(BytesIO(data))
    im.load()
    return im


def test_shrink_long_screenshot_keeps_text_size():
    """1170×6000 的长截图：按宽度缩到 1080（高 5538），不再被压成 312×1600。"""
    out = views_imei._shrink(_make_img(1170, 6000))
    im = _open(out)
    assert im.format == "JPEG"
    assert im.width == 1080
    assert abs(im.height - 5538) <= 1


def test_shrink_very_tall_capped_by_height():
    """1000×12000 这种极端长图：宽度不超 1080，但高度由 6000 兜底。"""
    im = _open(views_imei._shrink(_make_img(1000, 12000)))
    assert im.height == 6000
    assert im.width == 500


def test_shrink_portrait_and_landscape_fit_1080():
    """竖 2000×4000、横 4000×2000：都缩到宽 1080，比例不变。"""
    tall = _open(views_imei._shrink(_make_img(2000, 4000)))
    assert (tall.width, tall.height) == (1080, 2160)
    wide = _open(views_imei._shrink(_make_img(4000, 2000)))
    assert (wide.width, wide.height) == (1080, 540)


def test_shrink_small_image_not_upscaled():
    """小图不放大，但仍然统一转成 JPEG。"""
    out = views_imei._shrink(_make_img(200, 100))
    im = _open(out)
    assert (im.width, im.height) == (200, 100)
    assert im.format == "JPEG"


def test_shrink_transparent_png_gets_white_background():
    """透明 PNG：透明区铺白，不能变成黑块。"""
    from PIL import Image

    im: Image.Image = _open(views_imei._shrink(_make_img(500, 500, transparent=True)))
    assert im.mode == "RGB"
    left = im.getpixel((10, 250))
    right = im.getpixel((490, 250))
    assert min(left) >= 240, left
    assert max(right) <= 40, right


def test_shrink_cmyk_jpeg_to_rgb():
    """印刷用的 CMYK JPEG 也能转（没有 alpha，直接转 RGB）。"""
    from PIL import Image

    buf = BytesIO()
    Image.new("CMYK", (1200, 800), (0, 0, 0, 0)).save(buf, format="JPEG")
    im = _open(views_imei._shrink(buf.getvalue()))
    assert im.mode == "RGB" and (im.width, im.height) == (1080, 720)


def test_shrink_photo_like_png_becomes_smaller_jpeg():
    """照片类内容：PNG 转 JPEG q85，体积明显变小、魔数是 JPEG。"""
    src = _make_img(1600, 1200, noise=True)
    out = views_imei._shrink(src)
    assert out[:3] == b"\xff\xd8\xff"
    assert len(out) < len(src) * 0.6, (len(src), len(out))


def test_shrink_huge_24mp_image_ok():
    """2400 万像素（旧上限 2000 万直接判「图片打不开」）现在能正常缩到 1080×720。"""
    im = _open(views_imei._shrink(_make_img(6000, 4000)))
    assert (im.width, im.height) == (1080, 720)


def test_shrink_rejects_too_many_pixels(monkeypatch):
    """超过像素上限就明确报错，别把机器拖死。"""
    import pytest

    from app import views_imei as mod

    monkeypatch.setattr(mod, "_PHOTO_MAX_PIXELS", 30_000)
    with pytest.raises(ValueError) as err:
        mod._shrink(_make_img(200, 200))
    assert "图片太大" in str(err.value)


def test_shrink_applies_exif_orientation():
    """手机竖拍照片带 EXIF 方向：转正后再缩，别让凭证横躺着。"""
    im = _open(views_imei._shrink(_make_img(4000, 2000, fmt="JPEG", exif_rotate=6)))
    # 方向对了才会是竖图 1080×2160；不认 EXIF 的话会变成 1080×540
    assert (im.width, im.height) == (1080, 2160)


def test_photo_upload_stores_jpeg_with_jpg_suffix(client):
    """走真实上传接口：PNG（含透明）存成 .jpg，取出是 JPEG。"""
    _login(client)
    _upload(client, _row("860000000000901", ALPHA))
    _login(client, "alpha")
    resp = client.post(
        "/imei/report",
        data={
            "imei": "860000000000901",
            "action": "已办",
            "biz_type": "AI手机合约",
            "photo": (BytesIO(_make_img(1170, 6000, transparent=True)), "shot.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "已记下" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        name = conn.execute("SELECT photo FROM imei_reports").fetchone()["photo"]
    assert name.endswith(".jpg")
    saved = (db_core.DATA_DIR / "uploads" / "imei" / name).read_bytes()
    assert saved[:3] == b"\xff\xd8\xff"
    got = client.get(f"/uploads/imei/{name}")
    assert got.status_code == 200 and got.headers["Content-Type"] == "image/jpeg"


# ---- 2026-11-09 13:30 三项：存量宽限 rule_ver / 超 30 天核实清单 / 非本店下线 ----


def _insert_legacy(imei, *, sold_day, age_days, remain_days, rule_ver=1):
    """直接插一条老清单（不经过导入），模拟 2026-11-09 之前的存量。"""
    sid = _store_id()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO imei_todo(
                imei, status, store_id, sold_store, own_store, dispatch_store,
                model, sp, sold_day, age_days, remain_days, stage, state,
                asof, rule_ver, updated_at
            ) VALUES (?, '已售', ?, ?, '', ?, 'Y600 Pro', '小李', ?, ?, ?, 'D7+', '确认未办',
                      '2026-11-02', ?, '2026-11-02 09:00:00')
            """,
            (imei, sid, ALPHA, ALPHA, sold_day, age_days, remain_days, rule_ver),
        )
    return imei


def test_rule_ver_new_import_is_2_and_reimport_keeps_legacy_1(client):
    """新 IMEI 插入写 2；老行重导也保持 1（UPSERT 不碰 rule_ver），11-16 宽限不被冲掉。"""
    _login(client)
    _upload(client, _row("860000000000601", ALPHA))
    _insert_legacy("860000000000602", sold_day="2026-10-29", age_days=4, remain_days=3)
    _upload(
        client,
        _row("860000000000601", ALPHA),
        _row("860000000000602", ALPHA),
        asof="2026-11-02",
    )
    with db.get_db() as conn:
        got = {
            r["imei"]: r["rule_ver"]
            for r in conn.execute(
                "SELECT imei, rule_ver FROM imei_todo WHERE imei IN (?, ?)",
                ("860000000000601", "860000000000602"),
            )
        }
    assert got["860000000000601"] == 2
    assert got["860000000000602"] == 1

    # 11-16 宽限清单重导：remain 会更新，但 rule_ver 仍是 1（存量不会被升级）
    _upload(
        client,
        _row("860000000000601", ALPHA, remain=7),
        _row("860000000000602", ALPHA, remain=7),
        asof="2026-11-02",
    )
    with db.get_db() as conn:
        got = {
            r["imei"]: (r["rule_ver"], r["remain_days"])
            for r in conn.execute(
                "SELECT imei, rule_ver, remain_days FROM imei_todo WHERE imei IN (?, ?)",
                ("860000000000601", "860000000000602"),
            )
        }
    assert got["860000000000601"] == (2, 7)
    assert got["860000000000602"] == (1, 7)


def test_rule_ver_1_uses_stored_remain_and_does_not_jump(client, monkeypatch):
    """存量按库里 remain_days 显示；今天走到 11-16 也不跳逾期。新导入则实时算。"""
    _login(client)
    _upload(
        client,
        f"860000000000604,{ALPHA},Y600 Pro,小李,2026-10-01,32,-25,D7+,确认未办,无,",
    )
    _insert_legacy("860000000000603", sold_day="2026-10-01", age_days=32, remain_days=5)
    page = client.get("/imei").get_data(as_text=True)
    assert "860000000000603" in page and "剩 5 天" in page
    assert "逾期 25 天" in page
    cards = _cards([_store_id()])
    assert cards["overdue"] == 1 and cards["soon"] == 1

    # 存量已经 11-16：remain 仍是 5，不因今天变成逾期；新导入那台继续走实时
    monkeypatch.setattr(db_imei, "today_local", lambda: date(2026, 11, 16))
    page = client.get("/imei").get_data(as_text=True)
    assert "剩 5 天" in page
    assert "逾期 25 天" not in page and "逾期 39 天" in page
    cards = _cards([_store_id()])
    assert cards["overdue"] == 1 and cards["soon"] == 1


def test_review_page_lists_remote_pending_over_30_days(client):
    """异地异网未激活超 30 天：管理员有核实页；门店端仍显示但不算逾期、不再催。"""
    _login(client)
    _upload(
        client,
        f"860000000000611,{ALPHA},Y600 Pro,小李,2026-09-20,43,-36,D7+,确认未办,无,",
        f"860000000000612,{ALPHA},Y600 Pro,小李,2026-10-20,13,-6,D7+,确认未办,无,",
    )
    _mark(client, "860000000000611", "异网")
    _mark(client, "860000000000612", "异网")

    main = client.get("/imei").get_data(as_text=True)
    assert "异地异网超 30 天未激活核实清单（1 台）" in main

    review = client.get("/imei/review")
    assert review.status_code == 200
    html = review.get_data(as_text=True)
    assert "860000000000611" in html and "860000000000612" not in html
    assert "售出已超过 30 天" in html

    # 门店端：老的那台仍看得到，标了「超30天」；待激活不计逾期/到期
    _login(client, "alpha")
    store = client.get("/imei").get_data(as_text=True)
    assert "860000000000611" in store and "超30天·管理员核实" in store
    cards = _cards([_store_id()])
    assert cards["pending"] == 2 and cards["overdue"] == 0 and cards["soon"] == 0
    # 非管理员进核实页被挡回
    assert client.get("/imei/review").status_code == 302


# ---- 2026-11-09 14:30 V0.3.66：激活日必填 + 标记留痕 + 存量结转 / 标记清单 ----


def test_activation_requires_real_date_and_logs_actor(client):
    """「已激活」不默认今天：空日期 / 早于售出日 / 未来都必须被拦；成功后审计记谁、原值→新值。"""
    _login(client)
    _upload(client, f"860000000000701,{ALPHA},Y600 Pro,小李,2026-10-29,4,3,D3-6,确认未办,无,")
    _login(client, "alpha")
    page = client.get("/imei").get_data(as_text=True)
    assert 'name="act_date" required' in page
    assert 'name="act_date" value=' not in page

    html = _mark(client, "860000000000701", "已激活").get_data(as_text=True)
    assert "请填写真实激活日期" in html
    assert _todo_row("860000000000701")["manual_activated_at"] == ""

    # 售出日之前 / 今天之后都拦下，且失败不留审计
    assert "不能早于售出日" in _mark(
        client, "860000000000701", "已激活", "2026-10-28"
    ).get_data(as_text=True)
    assert "不能填将来的日期" in _mark(
        client, "860000000000701", "已激活", "2026-11-03"
    ).get_data(as_text=True)
    assert _todo_row("860000000000701")["manual_activated_at"] == ""
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM imei_mark_log").fetchone()[0] == 0

    _mark(client, "860000000000701", "已激活", "2026-11-01")
    assert _todo_row("860000000000701")["manual_activated_at"] == "2026-11-01"
    with db.get_db() as conn:
        log = conn.execute(
            """
            SELECT action, old_user_type, old_activated_at,
                   new_user_type, new_activated_at, user_id, created_at
            FROM imei_mark_log
            """
        ).fetchone()
        alpha_id = conn.execute("SELECT id FROM users WHERE username='alpha'").fetchone()["id"]
    assert log["action"] == "已激活"
    assert log["old_user_type"] == "本地移动" and log["old_activated_at"] == ""
    assert log["new_user_type"] == "本地移动" and log["new_activated_at"] == "2026-11-01"
    assert log["user_id"] == alpha_id and log["created_at"]


def test_mark_log_sequence_and_one_click_revoke(client):
    """异网 → 已激活 → 撤销，每一步留原值/新值；管理员在标记清单里一键撤销。"""
    _login(client)
    _upload(
        client,
        f"860000000000711,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,",
        f"860000000000712,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,",
        f"860000000000713,{ALPHA},Y600 Pro,小李,2026-10-21,12,-5,D7+,确认未办,无,",
    )
    _login(client, "alpha")
    _mark(client, "860000000000711", "异网")
    _mark(client, "860000000000711", "已激活", "2026-11-01")
    _login(client, "admin")
    _login(client)  # admin

    page = client.get("/imei/marks")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "860000000000711" in html and "标记留痕" in html
    assert "860000000000712" not in html  # 没标记的不进当前清单
    assert "异地异网" in html and "2026-11-01" in html
    assert "撤销" in html

    with db.get_db() as conn:
        logs = [dict(r) for r in conn.execute(
            "SELECT action, old_user_type, old_activated_at, new_user_type, new_activated_at FROM imei_mark_log ORDER BY id"
        )]
        assert [x["action"] for x in logs] == ["异网", "已激活"]
        assert logs[0]["old_user_type"] == "本地移动"
        assert logs[1]["old_user_type"] == "异地异网"
        assert logs[1]["new_activated_at"] == "2026-11-01"

    resp = client.post(
        "/imei/mark",
        data={"imei": "860000000000711", "mark": "撤销", "back": "/imei/marks"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "已撤销" in resp.get_data(as_text=True)
    row = _todo_row("860000000000711")
    assert row["user_type"] == "本地移动" and row["manual_activated_at"] == ""
    with db.get_db() as conn:
        logs = [dict(r) for r in conn.execute(
            "SELECT action, old_user_type, old_activated_at, new_user_type, new_activated_at FROM imei_mark_log ORDER BY id"
        )]
        marked = db_imei.marked_rows(conn, [_store_id()])
    assert [x["action"] for x in logs] == ["异网", "已激活", "撤销"]
    assert logs[2]["old_user_type"] == "异地异网" and logs[2]["old_activated_at"] == "2026-11-01"
    assert logs[2]["new_user_type"] == "本地移动" and logs[2]["new_activated_at"] == ""
    assert marked == []


def test_marks_page_keeps_unseen_marked_rows_revocable(client):
    """机器从最新清单消失后，标记仍要出现在管理员标记清单里，并能撤销。"""
    _login(client)
    _upload(client, _row("860000000000741", ALPHA))
    _login(client, "alpha")
    _mark(client, "860000000000741", "异网")
    _login(client)
    with db.get_db() as conn:
        conn.execute("UPDATE imei_todo SET seen=0 WHERE imei='860000000000741'")

    html = client.get("/imei/marks").get_data(as_text=True)
    assert "860000000000741" in html and "不在最新清单" in html

    resp = client.post(
        "/imei/mark",
        data={"imei": "860000000000741", "mark": "撤销", "back": "/imei/marks"},
        follow_redirects=True,
    )
    assert resp.status_code == 200 and "已撤销" in resp.get_data(as_text=True)
    row = _todo_row("860000000000741")
    assert row["user_type"] == "本地移动" and row["manual_activated_at"] == ""
    with db.get_db() as conn:
        assert db_imei.marked_rows(conn, [_store_id()]) == []


def test_carryover_lists_all_rule_ver_1_even_unseen_and_exports(client):
    """存量结转按 91 台口径：rule_ver=1 全列，含 seen=0 的老行，新导入不进。"""
    _login(client)
    _upload(client, _row("860000000000721", ALPHA))
    _insert_legacy("860000000000722", sold_day="2026-10-31", age_days=7, remain_days=0)
    _insert_legacy("860000000000723", sold_day="2026-10-31", age_days=7, remain_days=0)
    with db.get_db() as conn:
        conn.execute("UPDATE imei_todo SET seen=0 WHERE imei='860000000000723'")

    page = client.get("/imei/carryover")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "860000000000722" in html and "860000000000723" in html
    assert "860000000000721" not in html
    assert "不在最新清单" in html and "宽限至 11-16" in html
    assert "（2 台）" in client.get("/imei").get_data(as_text=True)

    csv_resp = client.get("/imei/export/carryover")
    assert csv_resp.status_code == 200
    body = csv_resp.get_data().decode("utf-8-sig")
    assert "860000000000722" in body and "860000000000723" in body
    assert "860000000000721" not in body
    assert "否（最新清单已没有）" in body


def test_carryover_and_marks_are_admin_only(client):
    _login(client)
    _upload(client, _row("860000000000731", ALPHA))
    _login(client, "alpha")
    assert client.get("/imei/carryover").status_code == 302
    assert client.get("/imei/export/carryover").status_code == 302
    assert client.get("/imei/marks").status_code == 302


# ---- V0.3.67：P1-1 门店全量视图（只认 seen=1，不按自然月切）+ P1-2 data_flag 落库 ----


def _flag_head():
    return "imei,store,model,sp,day,age,remain,stage,state,strength,evidence,数据标记\n"


def _flag_row(imei, store, flag, remain=3):
    return _row(imei, store, remain=remain) + "," + flag


def _upload_flag(client, *rows, asof="2026-11-02"):
    data = BytesIO((_flag_head() + "".join(r + "\n" for r in rows)).encode("utf-8"))
    return client.post(
        "/imei/import",
        data={"csv_file": (data, "todo.csv"), "asof": asof},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_todo_keeps_seen1_across_month_boundary(client, monkeypatch):
    """P1-1：门店边界只认 seen=1，不按自然月切。10-31 卖的、seen=1 的机器，
    哪怕「今天」滚到 12-01 也在待办里；seen=0 的机器任何日期都不可见。"""
    _login(client)
    _upload(
        client,
        f"860000000000901,{ALPHA},Y600 Pro,小李,2026-10-31,2,3,D3-6,确认未办,无,",
        f"860000000000902,{ALPHA},Y600 Pro,小李,2026-10-31,2,3,D3-6,确认未办,无,",
    )
    # 第二次只导一台：901 还在最新清单（seen=1），902 被标 seen=0（不在最新清单）
    _upload(client, f"860000000000901,{ALPHA},Y600 Pro,小李,2026-10-31,2,3,D3-6,确认未办,无,")
    monkeypatch.setattr(db_imei, "today_local", lambda: date(2026, 12, 1))
    with db.get_db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM stores WHERE code='store-alpha'")]
        imeis = {r["imei"] for r in db_imei.todo_rows(conn, ids, show="open")}
        seen = {
            r["imei"]: r["seen"]
            for r in conn.execute(
                "SELECT imei, seen FROM imei_todo WHERE imei IN ('860000000000901','860000000000902')"
            )
        }
    assert seen == {"860000000000901": 1, "860000000000902": 0}
    assert "860000000000901" in imeis
    assert "860000000000902" not in imeis


def test_page_kpi_label_says_latest_list_not_month(client):
    """P1-1：待办统计卡写「已售未办（最新清单内）」，不写「本月」。"""
    _login(client)
    _upload(client, _row("860000000000903", ALPHA))
    page = client.get("/imei").get_data(as_text=True)
    assert "已售未办（最新清单内）" in page


def test_import_with_data_flag_column_sets_flag(client):
    """P1-2：CSV 真含「数据标记」列时才写 data_flag（多值用「；」原样保留）。"""
    _login(client)
    resp = _upload_flag(client, _flag_row("860000000000821", ALPHA, "台账无此串码；二销"))
    assert "入库 1 台" in resp.get_data(as_text=True)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT data_flag, seen, rule_ver FROM imei_todo WHERE imei='860000000000821'"
        ).fetchone()
    assert row["data_flag"] == "台账无此串码；二销"
    assert row["seen"] == 1 and row["rule_ver"] == 2


def test_import_without_data_flag_column_preserves_flag(client):
    """P1-2：没有「数据标记」列的重导不清掉库里已回填的标记。"""
    _login(client)
    _upload_flag(client, _flag_row("860000000000822", ALPHA, "台账无此串码"))
    _upload(client, _row("860000000000822", ALPHA, remain=5))
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT data_flag, remain_days FROM imei_todo WHERE imei='860000000000822'"
        ).fetchone()
    assert row["data_flag"] == "台账无此串码"
    assert row["remain_days"] == 5


def test_update_data_flags_only_touches_flag(client):
    """P1-2：独立回填只改 data_flag，seen / rule_ver / remain_days / sold_day / asof / updated_at 全不变。"""
    _login(client)
    _upload(client, _row("860000000000811", ALPHA), _row("860000000000812", ALPHA))
    frozen = ("seen", "rule_ver", "remain_days", "sold_day", "asof", "updated_at")
    with db.get_db() as conn:
        before = {
            r["imei"]: tuple(r[k] for k in frozen)
            for r in conn.execute(
                "SELECT * FROM imei_todo WHERE imei IN ('860000000000811','860000000000812')"
            )
        }
        res = db_imei.update_data_flags(
            conn, [("860000000000811", "台账无此串码"), ("860000000000999", "二销")]
        )
    assert res == {"updated": 1, "missed": 1}
    with db.get_db() as conn:
        after = {
            r["imei"]: tuple(r[k] for k in frozen)
            for r in conn.execute(
                "SELECT * FROM imei_todo WHERE imei IN ('860000000000811','860000000000812')"
            )
        }
        flags = {
            r["imei"]: r["data_flag"]
            for r in conn.execute(
                "SELECT imei, data_flag FROM imei_todo WHERE imei IN ('860000000000811','860000000000812')"
            )
        }
    assert after == before
    assert flags == {"860000000000811": "台账无此串码", "860000000000812": ""}


def test_update_data_flags_multivalue_and_empty(client):
    """P1-2：多值用「；」原样存；空标记也是合法回填（清回空）。"""
    _login(client)
    _upload(client, _row("860000000000813", ALPHA))
    with db.get_db() as conn:
        db_imei.update_data_flags(conn, [("860000000000813", "台账无此串码；二销")])
        flag = conn.execute(
            "SELECT data_flag FROM imei_todo WHERE imei='860000000000813'"
        ).fetchone()["data_flag"]
    assert flag == "台账无此串码；二销"
    with db.get_db() as conn:
        db_imei.update_data_flags(conn, [("860000000000813", "")])
        flag = conn.execute(
            "SELECT data_flag FROM imei_todo WHERE imei='860000000000813'"
        ).fetchone()["data_flag"]
    assert flag == ""


def test_export_todo_and_carryover_include_data_flag_column(client):
    """P1-2：待办导出与结转导出都带「数据标记」列；结转只含 rule_ver=1。"""
    _login(client)
    _upload_flag(
        client,
        _flag_row("860000000000831", ALPHA, "台账无此串码"),
        _flag_row("860000000000832", ALPHA, "二销"),
    )
    _insert_legacy("860000000000833", sold_day="2026-10-31", age_days=7, remain_days=0)
    with db.get_db() as conn:
        db_imei.update_data_flags(conn, [("860000000000833", "台账无此串码；二销")])

    todo = client.get("/imei/export/todo")
    assert todo.status_code == 200
    body = todo.get_data().decode("utf-8-sig")
    assert "数据标记" in body
    assert "台账无此串码" in body and "二销" in body

    carry = client.get("/imei/export/carryover")
    assert carry.status_code == 200
    cbody = carry.get_data().decode("utf-8-sig")
    assert "数据标记" in cbody
    assert "台账无此串码；二销" in cbody
    assert "860000000000831" not in cbody and "860000000000832" not in cbody
