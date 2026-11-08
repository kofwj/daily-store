"""串码待办：导入、按店可见、门店自报、截图权限。

口径：门店只看自己店；自报只能写自己店；截图只有能看那家店的人能打开；
已售按 POS 销售门店派、未售按库存登记门店派；泰州不参与；档案里没有的店只在管理员可见。

注意：conftest 的 admin_client / filler_client 是同一个 FlaskClient（同一个 client 夹具），
一个用例里同时要两个角色时只能显式再登录一次，否则会话被后一次登录顶掉。
"""
from io import BytesIO

from app import db
from app.db_core import DATA_DIR

CSV_HEAD = "imei,store,model,sp,day,age,remain,stage,state,strength,evidence\n"

ALPHA = "示例市甲街vivo体验店"
BETA = "示例市乙街vivo专卖店"


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
    assert "860000000000031" in client.get("/imei").get_data(as_text=True)
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
    assert name.endswith(".png")
    assert (DATA_DIR / "uploads" / "imei" / name).is_file()
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
        "imei,门店,结论,业务类型,POS证据类型,类型一致性,核销结论,备注,截图,报人,时间"
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
    assert row["biz_type"] == "送2G流量" and row["photo"].endswith(".png") and row["note"] == ""


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
