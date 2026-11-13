"""顺差串码待办：门店只读清单 + 门店自报（含截图）+ 管理员导入清单。

权限口径跟日报一致：
- 店员（filler）：只看自己绑的店，能自报这四家店的机器
- 只读 / 店长 / 区域经理（readonly）：看辖区，不能写
- 地市负责人（city）：看本市，能写本市（跟改日报同一档）
- 管理员：全部，能导入清单、能补记
"""
from __future__ import annotations

import csv
import io
import secrets
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from . import db, db_core, db_decl, db_imei
from .helpers import (
    accessible_stores,
    admin_required,
    login_required,
    request_scope,
)

# 原图允许到 15MB（现代手机直拍常见 6–12MB），落盘前统一缩到宽 1080 并转 JPEG（约 100–600KB）
_PHOTO_MAX_BYTES = 15 * 1024 * 1024
# 凭证要能看清字：宽度按 1080 缩（手机整屏宽度那一档），高度 6000 兜底，
# 免得 1170×6000 的长截图被「长边 1600」压成 312 px 宽、字全糊。
_PHOTO_MAX_W = 1080
_PHOTO_MAX_H = 6000
_PHOTO_JPEG_QUALITY = 85
# 6400 万像素：48MP 手机原图还能进，同时挡住解压炸弹（PIL 超过 2 倍会直接报错）
_PHOTO_MAX_PIXELS = 64_000_000
_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
)


def _looks_like_image(data: bytes) -> bool:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return any(data.startswith(magic) for magic in _IMAGE_MAGIC)


def _carryover_scope(value: str) -> str:
    """carryover 页/CSV 的 scope 参数：只认 all/lost/grace/carried，其他值一律当 all。"""
    return value if value in ("all", "lost", "grace", "carried") else "all"


def _pool_tab(value: str) -> str:
    """串码池的 tab：stock（默认＝原有在库视图）/ unmapped（待建店待办），其他值当 stock。"""
    return "unmapped" if value == "unmapped" else "stock"


def _to_rgb(im: Any) -> Any:
    """透明图先铺白底再转 RGB——直接砍 alpha，透明区的文字会压成黑块/花边。"""
    from PIL import Image

    if im.mode == "RGB":
        return im
    if im.mode == "L":
        return im.convert("RGB")
    if im.mode not in ("RGBA", "LA", "P"):
        # CMYK 之类的印刷色彩空间直接转 RGB，没有 alpha 可铺
        return im.convert("RGB")
    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def _shrink(data: bytes) -> bytes:
    """统一转 JPEG q85，等比例缩到 宽 ≤1080 / 高 ≤6000；扶正 EXIF 方向并丢掉 EXIF（含定位）。

    PNG 重编码常常比原图还大（手机截图尤甚），统一转 JPEG 体积约为原来的四成多；
    长截图按宽度缩（高度不是主要约束），单张超长图才由 6000 兜底。
    """
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = _PHOTO_MAX_PIXELS
    try:
        im: Any = Image.open(BytesIO(data))
        if im.width * im.height > _PHOTO_MAX_PIXELS:
            raise ValueError("图片太大了（超过 6400 万像素），先压缩再传")
        im.load()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — 任何解码失败都当「不是图片」
        raise ValueError("图片打不开，换一张试试") from exc
    im = ImageOps.exif_transpose(im)
    im.thumbnail((_PHOTO_MAX_W, _PHOTO_MAX_H), Image.Resampling.LANCZOS)
    out = BytesIO()
    _to_rgb(im).save(out, format="JPEG", quality=_PHOTO_JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _save_photo(upload, store_id: int) -> str:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in _PHOTO_EXTS:
        raise ValueError("截图只支持 png / jpg / webp")
    data = upload.read()
    if not data or len(data) > _PHOTO_MAX_BYTES:
        raise ValueError("截图是空的或超过 15MB")
    if not _looks_like_image(data):
        raise ValueError("这个文件不是图片")
    # 存下来一律是 JPEG，后缀跟着内容走（不然浏览器按 .png 的 MIME 读 JPEG）
    name = f"{secrets.token_hex(12)}.jpg"
    folder = db_core.DATA_DIR / "uploads" / "imei"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(_shrink(data))
    return name


def _can_write(role: str) -> bool:
    return role in ("admin", "filler", "city")


def _claim_target_store(conn, raw: str, back: str) -> Optional[int]:
    """待建店池子行该记到哪家店：`target_store_id` 优先；没传且只有 1 家可访问店就默认那家。

    - 只有 1 家店：不用选，直接那家（覆盖大多数店员账号）；
    - 多家店 / admin：必须显式选，没选就 flash 让重选（**不静默落默认店**）；
    - 选了没权限的店 → 403（由调用方按返回值判，这里直接抛给路由）。
    """
    stores = accessible_stores(conn)
    ids = [int(s["id"]) for s in stores]
    if not ids:
        flash("你的账号没有可写的门店，不能代办", "error")
        return None
    if raw:
        try:
            store_id = int(raw)
        except ValueError:
            flash("代办门店选得不对，请重选", "error")
            return None
        return store_id
    if len(ids) == 1:
        return ids[0]
    flash("这条是待建店（销售门店还没建账号），先在行里选「本店代办」要记到哪家店", "error")
    return None

def _group_by_store(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 `todo_board()` 的扁平行按门店折成模板要的 group 列表（保持 board 的排序）。

    `todo_board()` 已经把「哪台归哪家店」算完（含待建店按店名各自成组），这里只做
    分桶，不再挑行、不再判断——卡片数字与折叠区台数必须 1:1。
    """
    groups: List[Dict[str, Any]] = []
    by_key: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        # 待建店（store_id 为空）用 store 名当键，否则 10 家未建档店会塌成一组
        key: Any = row["store_id"] if row["store_id"] is not None else "?" + row["store"]
        group = by_key.get(key)
        if group is None:
            group = {
                "id": key,
                "name": row["store"] or "门店档案里没有",
                "short": row["store_short"] or row["store"] or "待建店",
                "unmapped": row["store_id"] is None,
                "rows": [],
            }
            by_key[key] = group
            groups.append(group)
        group["rows"].append(row)
    return groups


def register_imei(app) -> None:
    @app.route("/imei")
    @login_required
    def imei_page():
        """门店页：只回答「今天该办哪几台」，每行一个「自报」按钮。

        数据只有一个来源 `db_imei.todo_board()`（判定只在那里做一次），模板不再自己
        挑行：`active_rows` / `pending_rows` / `reported_rows` / `held_rows` 四个桶
        直接对应四个折叠区，卡片数字与折叠区台数 1:1。

        管理员那套（导入清单 / 结转 / 标记台数 / 各子台账）全部在 `/imei/admin`，
        门店页看不到，也不再吃首屏。

        `show` 决定这次渲染哪几块（默认 open = 今天要办 + 待激活）：
        open / reported / held / all。
        """
        show = request.args.get("show") or "open"
        if show not in ("open", "reported", "held", "all"):
            show = "open"
        is_admin = g.user["role"] == "admin"
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            ids = scope["ids"]
            # 待建店（store_id 为空）只在管理员看全站时并进来：店员不该看到他办不了的表
            include_unmapped = is_admin and not scope["active"]
            board = db_imei.todo_board(conn, ids, include_unmapped=include_unmapped)
            # 串码池（在库）那两个台数还挂在页头按钮上，仍从 summary 取
            cards = db_imei.summary(conn, ids)
            # 本店代办过（未释放）的待建店机器：归属只认锁表，不写回 imei_todo.store_id
            claims = db_imei.pool_claims(conn, ids)
            # 「本店代办」下拉只列本人可写的门店（不受页面 city/manager 筛选影响）
            target_stores = stores
        return render_template(
            "imei.html",
            board=board,
            groups=_group_by_store(board["active_rows"]),
            pending_groups=_group_by_store(board["pending_rows"]),
            reported_groups=_group_by_store(board["reported_rows"]),
            held_groups=_group_by_store(board["held_rows"]),
            cards=cards,
            claims=claims,
            target_stores=target_stores,
            today_iso=db.today_local().isoformat(),
            grace_label=db_imei.grace_label(),
            scope=scope,
            show=show,
            can_write=_can_write(g.user["role"]),
            actions=db_imei.ACTIONS,
            biz_choices=db_imei.BIZ_CHOICES,
            biz_other=db_imei.BIZ_OTHER,
            needs_evidence=list(db_imei.NO_SYSTEM_EVIDENCE),
            evidence_label=db_imei.evidence_label,
            is_admin=is_admin,
        )

    @app.route("/imei/admin")
    @admin_required
    def imei_admin():
        """管理员工作台：导入清单 + 各子台账入口 + 各店标记台数 + 导出。

        原来这些都铺在门店页首屏（要滚 1083px 才见到正文），2026-11-12 整体搬过来：
        门店页只留一个自报按钮，进出货/核实的活在管理员这边做。
        """
        with db.get_db() as conn:
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            ids = scope["ids"]
            include_unmapped = not scope["active"]
            cards = db_imei.summary(conn, ids)
            board = db_imei.todo_board(conn, ids, include_unmapped=include_unmapped)
            # 各店标了多少台异网/已激活：用来发现「全标异网躲催办」
            marks = db_imei.mark_summary(conn, ids)
            # 异地异网超过 30 天还没激活：人工核实清单
            review_n = len(
                db_imei.review_rows(conn, ids, include_unmapped=include_unmapped)
            )
            # 两个台账：11-16 存量结转（rule_ver=1）、门店标记清单
            carryover_n = len(
                db_imei.carryover_rows(conn, ids, include_unmapped=include_unmapped)
            )
            marked_n = len(
                db_imei.marked_rows(conn, ids, include_unmapped=include_unmapped)
            )
        return render_template(
            "imei_admin.html",
            cards=cards,
            board=board,
            marks=marks,
            review_n=review_n,
            carryover_n=carryover_n,
            marked_n=marked_n,
            today_iso=db.today_local().isoformat(),
            grace_label=db_imei.grace_label(),
            scope=scope,
        )

    @app.route("/imei/review")
    @admin_required
    def imei_review():
        """管理员待核实清单：异地异网、未激活、售出超过 30 天。"""
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            rows = db_imei.review_rows(
                conn, scope["ids"], include_unmapped=not scope["active"]
            )
        return render_template(
            "imei_review.html",
            rows=rows,
            scope=scope,
            today_iso=db.today_local().isoformat(),
        )

    @app.route("/imei/carryover")
    @admin_required
    def imei_carryover():
        """11-16 存量结转清单：rule_ver=1 的机器 + CSV 导出（仅管理员）。

        支持 `scope=all|lost|grace|carried` 四个 tab：all 全列、lost=seen=0、
        grace=软失效范围（与 §7 谓词同源）、carried=管理员已手动结转；
        tab 计数与行集同源。
        """
        scope_arg = _carryover_scope(request.args.get("scope", "all"))
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            include_unmapped = not scope["active"]
            all_rows = db_imei.carryover_rows(
                conn, scope["ids"], include_unmapped=include_unmapped, scope="all"
            )
            rows = db_imei.carryover_rows(
                conn, scope["ids"], include_unmapped=include_unmapped, scope=scope_arg
            )
            logs = db_imei.carry_log_rows(
                conn, scope["ids"], include_unmapped=include_unmapped, limit=50
            )
        counts = {
            "all": len(all_rows),
            "lost": sum(1 for r in all_rows if not r["in_latest"]),
            "grace": sum(1 for r in all_rows if r["in_grace"]),
            "carried": sum(1 for r in all_rows if r["carried"]),
        }
        return render_template(
            "imei_carryover.html",
            rows=rows,
            counts=counts,
            logs=logs,
            active_scope=scope_arg,
            scope=scope,
            grace_label=db_imei.grace_label(),
            today_iso=db.today_local().isoformat(),
        )

    @app.route("/imei/carryover/mark", methods=["POST"])
    @admin_required
    def imei_carryover_mark():
        """管理员手动结转 / 撤销结转（V0.3.69）：逐台或整批。

        - 只写 `imei_carry_log` 审计快照（谁、何时、当时那行的门店/机型/剩余/标记状态），
          **不删行、不改 state/seen/remain_days/rule_ver**，门店仍可补报；
        - 逐台：`imei_one` 一次一台；批量：勾选 `imei` 或 `batch=1`（整批按服务端
          重算的当前范围，不信前端传来的清单）；
        - 仅 admin（路由级 `@admin_required` + 数据层 `is_admin` 双保险）。
        """
        mode = (request.form.get("mode") or "").strip()
        if mode not in ("carry", "undo"):
            flash("不认识的结转动作", "error")
            return redirect(url_for("imei_carryover"))
        back = request.form.get("back") or url_for("imei_carryover", scope="grace")
        if not back.startswith("/") or back.startswith("//"):
            back = url_for("imei_carryover", scope="grace")
        one = (request.form.get("imei_one") or "").strip()
        batch = request.form.get("batch") == "1"
        imeis = [one] if one else [i.strip() for i in request.form.getlist("imei") if i.strip()]
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            include_unmapped = not scope["active"]
            if batch:
                target = "grace" if mode == "carry" else "carried"
                imeis = [
                    r["imei"]
                    for r in db_imei.carryover_rows(
                        conn,
                        scope["ids"],
                        include_unmapped=include_unmapped,
                        scope=target,
                    )
                ]
            try:
                if mode == "carry":
                    res = db_imei.carry_over(
                        conn,
                        imeis,
                        store_ids=scope["ids"],
                        include_unmapped=include_unmapped,
                        is_admin=True,
                        user_id=g.user["id"],
                    )
                    msg = f"已结转 {res['carried']} 台（门店仍可补报，可在「已结转」撤销）"
                    if res["skipped"]:
                        msg += f"；{res['skipped']} 台不在宽限范围，跳过"
                    flash(msg, "ok")
                else:
                    res = db_imei.undo_carry(
                        conn,
                        imeis,
                        store_ids=scope["ids"],
                        include_unmapped=include_unmapped,
                        is_admin=True,
                        user_id=g.user["id"],
                    )
                    msg = f"已撤销结转 {res['undone']} 台"
                    if res["skipped"]:
                        msg += f"；{res['skipped']} 台不是已结转状态，跳过"
                    flash(msg, "ok")
            except PermissionError:
                return Response("forbidden", status=403)
            except ValueError as exc:
                flash(str(exc), "error")
        return redirect(back)

    @app.route("/imei/export/carryover")
    @admin_required
    def imei_export_carryover():
        """导出 11-16 存量结转清单（UTF-8 BOM，Excel 直接打开）。

        与页面同筛选：`scope` 透传同一 all/lost/grace/carried，行数与页面一致。
        """
        scope_arg = _carryover_scope(request.args.get("scope", "all"))
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            rows = db_imei.carryover_rows(
                conn, scope["ids"], include_unmapped=not scope["active"], scope=scope_arg
            )
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "门店",
                "机型",
                "串码",
                "营业员",
                "售出日期",
                "清单已售天数",
                "清单剩余天数",
                "阶段",
                "清单状态",
                "客户类型",
                "手动激活日",
                "标记状态",
                "数据标记",
                "门店自报",
                "报人",
                "自报时间",
                "清单截至",
                "是否在最新清单",
                "结转时间",
                "结转人",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["store_name"] or row.get("dispatch_store") or "",
                    row["model"],
                    row["imei"],
                    row["sp"],
                    row["sold_day"],
                    row["age_days"],
                    row["remain_days"],
                    row["stage"],
                    row["state"],
                    row["user_type"],
                    row["manual_activated_at"],
                    row["act_state"],
                    row.get("data_flag", ""),
                    row["report_action"] or "",
                    row["reporter"] or "",
                    (row["report_at"] or "")[:16],
                    (row["asof"] or "")[:10],
                    "是" if row["in_latest"] else "否（最新清单已没有）",
                    (row.get("carried_at") or "")[:16] if row.get("carried") else "",
                    (row.get("carrier") or "") if row.get("carried") else "",
                ]
            )
        body = "\ufeff" + out.getvalue()
        return Response(
            body,
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=imei_carryover.csv",
                "Cache-Control": "no-store",
            },
        )

    @app.route("/imei/marks")
    @admin_required
    def imei_marks():
        """门店标记清单 + 标记动作审计（仅管理员，可逐行一键撤销）。"""
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            include_unmapped = not scope["active"]
            rows = db_imei.marked_rows(
                conn, scope["ids"], include_unmapped=include_unmapped
            )
            logs = db_imei.mark_log_rows(
                conn, scope["ids"], include_unmapped=include_unmapped
            )
        return render_template(
            "imei_marks.html",
            rows=rows,
            logs=logs,
            scope=scope,
            today_iso=db.today_local().isoformat(),
        )

    @app.route("/imei/transfer")
    @login_required
    def imei_transfer():
        """调拨台账：调入店登记，管理员可撤销。只读列表对所有人开放（按 scope 过滤）。"""
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            open_only = request.args.get("show", "open") != "all"
            rows = db_imei.transfer_rows(
                conn,
                store_ids=scope["ids"] if scope["active"] else None,
                open_only=open_only,
            )
            pool = db_imei.pool_rows(conn)
            pool_by_imei = {str(r["imei"]): r for r in pool}
        return render_template(
            "imei_transfer.html",
            rows=rows,
            stores=stores,
            scope=scope,
            open_only=open_only,
            can_write=_can_write(g.user["role"]),
            is_admin=g.user["role"] == "admin",
            pool_n=len(pool_by_imei),
            today_iso=db.today_local().isoformat(),
        )

    @app.route("/imei/lookup")
    @login_required
    def imei_lookup():
        """串码查询（触客页查询框 / 扫一扫用）：按尾号或机型关键字找池子里的机器。

        只读、只返回池子里还在的（`未售 + seen=1 + 无有效声明`），
        所以扫到一台已经卖掉的机器会返回空列表 —— 页面提示「池子里没有」，
        不会把已售机器又拉回来卖一次。
        """
        q = (request.args.get("q") or "").strip()
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return jsonify({"ok": False, "error": "forbidden", "rows": []}), 403
            rows = db_imei.search_pool(conn, q) if q else []
        return jsonify(
            {
                "ok": True,
                "q": q,
                "rows": [
                    {
                        "imei": str(r["imei"]),
                        "model": str(r["model"] or ""),
                        "own_store": str(r["own_store"] or ""),
                        "tail": str(r["imei"])[-6:],
                    }
                    for r in rows
                ],
            }
        )

    @app.route("/imei/transfer", methods=["POST"])
    @login_required
    def imei_transfer_post():
        """记一条调拨（调入店记）：串码 + 从哪店 + 哪天 + 说明。撤销只有管理员能做。"""
        back = url_for("imei_transfer")
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            if not _can_write(g.user["role"]):
                flash("只读账号不能记调拨", "error")
                return redirect(back)
            action = (request.form.get("action") or "record").strip()
            if action == "cancel":
                if g.user["role"] != "admin":
                    flash("只有管理员能撤销调拨", "error")
                    return redirect(back)
                tid = (request.form.get("transfer_id") or "").strip()
                if not tid.isdigit():
                    flash("撤销时传的调拨 id 不对", "error")
                    return redirect(back)
                if db_imei.cancel_transfer(
                    conn, int(tid), user_id=g.user["id"], is_admin=True
                ):
                    flash("已撤销这条调拨，机器回到原店候选", "ok")
                else:
                    flash("这条调拨已经撤销过了", "warn")
                return redirect(back)
            imei = (request.form.get("imei") or "").strip()
            to_raw = (request.form.get("to_store_id") or "").strip()
            if not imei or not to_raw.isdigit():
                flash("要填串码，并选调到哪家店", "error")
                return redirect(back)
            if not db.user_can_access_store(conn, g.user, int(to_raw)):
                return Response("forbidden", status=403)
            day_raw = (request.form.get("biz_date") or "").strip()
            day = db.today_local()
            if day_raw:
                try:
                    day = date.fromisoformat(day_raw[:10])
                except ValueError:
                    flash("日期格式不对", "error")
                    return redirect(back)
            from_raw = (request.form.get("from_store_id") or "").strip()
            try:
                result = db_imei.record_transfer(
                    conn,
                    imei=imei,
                    to_store_id=int(to_raw),
                    biz_date=day,
                    from_store_id=int(from_raw) if from_raw.isdigit() else None,
                    note=(request.form.get("note") or "").strip(),
                    user_id=g.user["id"],
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(back)
        if result["created"]:
            flash(f"已记调拨：{imei[-6:]} 调入 {day.isoformat()}", "ok")
        else:
            flash("这台已经调给本店了，不用重复记", "warn")
        return redirect(back)

    @app.route("/imei/report", methods=["POST"])
    @login_required
    def imei_report():
        """门店自报一条。

        两类行：
        - 已建档行（`imei_todo.store_id` 有值）：只写自己店，**忽略** `target_store_id`（防改派）；
        - 待建店池子行（`store_id IS NULL`）：读 `target_store_id`（代办店）→ 先抢
          `imei_pool_lock` 再写自报，两者同一事务。**不判断任何日期/天数**（超期照常能办）。
        """
        imei = (request.form.get("imei") or "").strip()
        action = (request.form.get("action") or "").strip()
        biz_type = (request.form.get("biz_type") or "").strip()
        note = (request.form.get("note") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        target_raw = (request.form.get("target_store_id") or "").strip()
        back = request.form.get("back") or url_for("imei_page")
        if not back.startswith("/") or back.startswith("//"):
            back = url_for("imei_page")
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            if not _can_write(g.user["role"]):
                flash("只读账号只能看，不能自报", "error")
                return redirect(back)
            row = conn.execute(
                "SELECT store_id, own_store FROM imei_todo WHERE imei=?", (imei,)
            ).fetchone()
            if row is None:
                flash("这条串码不在清单里，刷新后再试", "error")
                return redirect(back)
            pool_row = None
            if row["store_id"] is None:
                pool_row = db_imei.pool_open_row(conn, imei)
                if pool_row is None:
                    flash("这台不在待建店池子里（可能已办完或不在最新清单），刷新后再试", "error")
                    return redirect(back)
                store_id = _claim_target_store(conn, target_raw, back)
                if store_id is None:
                    return redirect(back)
                if not db.user_can_access_store(conn, g.user, store_id):
                    # 代办的店也必须是本人可写的店（admin=在营、city=本市、filler=绑定店）
                    return Response("forbidden", status=403)
            else:
                store_id = int(row["store_id"])
                if not db.user_can_access_store(conn, g.user, store_id):
                    # 写路径不静默落到别家店：宁可拒绝，也不能把结论记到别的店
                    return Response("forbidden", status=403)
            decl_note: dict = {}
            upload = request.files.get("photo")
            has_photo = upload is not None and bool(upload.filename)
            # 送2G流量 / 顺差让利 系统没有记录：仅在报「已办」时先拦一道，别白存图片再报错
            if (
                action == "已办"
                and db_imei.norm_biz_type(biz_type) in db_imei.NO_SYSTEM_EVIDENCE
                and not note
                and not has_photo
            ):
                flash(
                    "「送2G流量」「顺差让利」这类业务系统记录不全，查到就能对上，查不到也算正常，"
                    "请务必上传受理单或截图（二者填一项）",
                    "error",
                )
                return redirect(back)
            # 顺差让利必带号码：垫资的钱是按号码付给人的，没号码只能人工翻截图
            if (
                action == "已办"
                and db_imei.norm_biz_type(biz_type) == "顺差让利"
                and not phone
            ):
                flash("顺差让利要填客户手机号码（垫资付款要对得上人）", "error")
                return redirect(back)
            photo = ""
            if has_photo:
                try:
                    photo = _save_photo(upload, store_id)
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(back)
            try:
                if pool_row is not None:
                    # 抢锁 → 自报：同一事务（add_report 传 begin=False，别二次 BEGIN IMMEDIATE）
                    db_imei.claim_pool_row(
                        conn,
                        imei=imei,
                        claim_store_id=store_id,
                        action=action,
                        user_id=g.user["id"],
                    )
                    report_id = db_imei.add_report(
                        conn,
                        imei=imei,
                        store_id=store_id,
                        action=action,
                        biz_type=biz_type,
                        note=note,
                        photo=photo,
                        phone=phone,
                        user_id=g.user["id"],
                        begin=False,
                    )
                    db_imei.attach_claim_report(conn, imei, report_id)
                else:
                    report_id = db_imei.add_report(
                        conn,
                        imei=imei,
                        store_id=store_id,
                        action=action,
                        biz_type=biz_type,
                        note=note,
                        photo=photo,
                        phone=phone,
                        user_id=g.user["id"],
                    )
                # 自报「已办」= 这台有结论了 → 落一条声明（占唯一索引 = 出池）。
                # 钱只看业务类型：顺差让利 → 待核垫资；其他类型 → 金额 0、直接「已确认」。
                # 与触客成交汇到同一个出口；已有声明就幂等返回，门店看到的是「已由成交闭环」。
                if action == "已办":
                    decl = db_decl.create_from_report(
                        conn,
                        imei=imei,
                        store_id=store_id,
                        own_store=str(row["own_store"] or ""),
                        report_id=report_id,
                        biz_type=biz_type,
                        phone=phone,
                        user_id=g.user["id"],
                    )
                    decl_note = decl
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(back)
        norm_biz = db_imei.norm_biz_type(biz_type) if action == "已办" else ""
        suffix = f" · {norm_biz}" if norm_biz else ""
        if decl_note and decl_note.get("created") and int(decl_note.get("rebate_cents") or 0) > 0:
            suffix += f" · 已记让利垫资待核 {db_decl.cents_text(decl_note['rebate_cents'])} 元"
        if pool_row is not None:
            flash(f"已记下：{imei[-6:]} → {action}{suffix}（待建店 · 本店代办）", "ok")
        else:
            flash(f"已记下：{imei[-6:]} → {action}{suffix}", "ok")
        return redirect(back)

    @app.route("/imei/mark", methods=["POST"])
    @login_required
    def imei_mark():
        """门店标「异地异网待激活」/「已激活」（点过已激活即锁死起算日），仅管理员可撤销或改期。

        这两个动作**不是**自报结论：不改「已办/未办」，只调 7 天怎么算。
        """
        imei = (request.form.get("imei") or "").strip()
        mark = (request.form.get("mark") or "").strip()
        day = (request.form.get("act_date") or "").strip()
        back = request.form.get("back") or url_for("imei_page")
        if not back.startswith("/") or back.startswith("//"):
            back = url_for("imei_page")
        is_admin = g.user["role"] == "admin"
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            if not _can_write(g.user["role"]):
                flash("只读账号只能看，不能标记", "error")
                return redirect(back)
            ids = [s["id"] for s in accessible_stores(conn)]
            try:
                res = db_imei.set_mark(
                    conn,
                    imei=imei,
                    mark=mark,
                    day=day,
                    store_ids=ids,
                    is_admin=is_admin,
                    user_id=g.user["id"],
                )
            except PermissionError:
                return Response("forbidden", status=403)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(back)
        tail = imei[-6:]
        if res["mark"] == "异网":
            flash(f"已标记：{tail} 异地异网待激活（门店用改串机激活后点「已激活」，期间不算逾期）", "ok")
        elif res["mark"] == "撤销":
            flash(f"已撤销：{tail} 回到本地移动，按售出日算", "ok")
        else:
            flash(f"已激活：{tail} 从 {res['manual_activated_at']} 起算 7 天", "ok")
        return redirect(back)

    @app.route("/imei/export")
    @admin_required
    def imei_export():
        """把门店自报导出成 CSV（带 BOM，Excel 直接打开不乱码），给台账回灌。"""
        with db.get_db() as conn:
            rows = db_imei.export_reports(conn, [s['id'] for s in accessible_stores(conn)])
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "imei",
                "门店",
                "结论",
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
                "原售出店",
                "是否本店代办",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["imei"],
                    row["store_name"],
                    row["action"],
                    row["biz_type"],
                    row["pos_type"],
                    db_imei.type_flag(row["biz_type"], row["pos_type"]),
                    db_imei.redeem_state(row["action"], row["biz_type"]),
                    row["note"],
                    row["photo"],
                    row["reporter"],
                    row["created_at"],
                    row["user_type"] if "user_type" in row.keys() else "",
                    row["manual_activated_at"] if "manual_activated_at" in row.keys() else "",
                    row["sold_store"] if "sold_store" in row.keys() else "",
                    "是（其他店售出）" if row["claim_store_id"] else "否",
                ]
            )
        body = "\ufeff" + out.getvalue()
        return Response(
            body,
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=imei_reports.csv",
                "Cache-Control": "no-store",
            },
        )

    @app.route("/imei/export/todo")
    @admin_required
    def imei_export_todo():
        """导出待办清单（带客户类型 + 手动激活日），给台账回读门店的标记。"""
        is_admin = g.user["role"] == "admin"
        with db.get_db() as conn:
            rows = db_imei.export_todo(
                conn, [s["id"] for s in accessible_stores(conn)], include_unmapped=is_admin
            )
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "imei",
                "门店",
                "机型",
                "营业员",
                "售出日期",
                "已售天数",
                "剩余天数",
                "阶段",
                "清单状态",
                "客户类型",
                "手动激活日",
                "标记状态",
                "数据标记",
                "生效剩余天数",
                "清单截至",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["imei"],
                    row["store_name"] or row.get("dispatch_store") or "",
                    row["model"],
                    row["sp"],
                    row["sold_day"],
                    row["age_days"],
                    row["remain_days"],
                    row["stage"],
                    row["state"],
                    row["user_type"],
                    row["manual_activated_at"],
                    row["act_state"],
                    row.get("data_flag", ""),
                    row["remain_eff"],
                    (row["asof"] or "")[:10],
                ]
            )
        body = "\ufeff" + out.getvalue()
        return Response(
            body,
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=imei_todo.csv",
                "Cache-Control": "no-store",
            },
        )

    @app.route("/imei/import", methods=["POST"])
    @admin_required
    def imei_import():
        upload = request.files.get("csv_file")
        if upload is None or not upload.filename:
            flash("选一份清单 CSV", "error")
            return redirect(url_for("imei_page"))
        if not (upload.filename or "").lower().endswith(".csv"):
            flash("只支持 .csv（Excel 先另存为 CSV）", "error")
            return redirect(url_for("imei_page"))
        asof = (request.form.get("asof") or "").strip()
        # 导入模式：默认增量（2026-11-10 用户拍板）。
        # 增量 = 只写本次清单里的行，不动清单外的行；全量 = 先把这一类 seen=0 再标回来。
        mode = (request.form.get("import_mode") or "incremental").strip()
        mark_seen = mode == "full"
        with db.get_db() as conn:
            try:
                result = db_imei.import_rows(conn, upload.read(), asof, mark_seen=mark_seen)
            except ValueError as exc:
                flash(f"导入失败：{exc}", "error")
                return redirect(url_for("imei_page"))
        kind = "库存（未售，按库存登记门店派）" if result["mode"] == "stock" else "台账（已售，按 POS 销售门店派）"
        mode_text = (
            "增量（不动清单外的行）"
            if result.get("incremental", True)
            else "全量快照（清单外的同类行已撤下，不再推给门店）"
        )
        msg = (
            f"{kind} · {mode_text}：读 {result['rows']} 行，入库 {result['written']} 台，"
            f"数据截至 {result['asof']}"
        )
        if result["bad_imei"]:
            msg += f"；{result['bad_imei']} 行没有串码，已跳过"
        if result["skipped_city"]:
            msg += f"；{result['skipped_city']} 行不是南通门店，已跳过"
        flash(msg, "ok")
        if result["unmatched"]:
            names = "、".join(f"{name}（{n}）" for name, n in result["unmatched"][:5])
            flash(
                f"{len(result['unmatched'])} 个店名门店档案里没有（在营但没建账号），"
                f"这些机器先挂在「待建店」里，只有管理员看得到：{names}",
                "error",
            )
        return redirect(url_for("imei_page", show="all"))

    @app.route("/imei/pool")
    @login_required
    def imei_pool():
        """南通池子（第二阶段的入口）：池子里还有哪些顺差机器没卖。

        现在只回答「有什么」，不做调拨、不做推荐——调拨走线下审批。
        """
        group_key = (request.args.get("by") or "store").strip()
        one = (request.args.get("store") or "").strip()
        tab = _pool_tab(request.args.get("tab", "stock"))
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            if tab == "unmapped":
                # 待建店待办：不分店也能看全池（不按 scope 过滤），"按店"分组用 dispatch_store
                rows = db_imei.pool_rows(conn, own_store=one, source="unmapped")
                stores = db_imei.pool_summary(conn, source="unmapped")
            else:
                rows = db_imei.pool_rows(conn, own_store=one) if one else []
                stores = db_imei.pool_summary(conn)
            cards = db_imei.summary(conn, [s["id"] for s in accessible_stores(conn)])
            target_stores = accessible_stores(conn)
        groups: List[Dict[str, Any]] = []
        by_key: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if tab == "unmapped":
                key = (row.get("dispatch_store") or "未登记") if group_key != "model" else (row["model"] or "未登记")
            else:
                key = (row["model"] or "未登记") if group_key == "model" else ""
            group = by_key.get(key)
            if group is None:
                group = {"key": key, "rows": []}
                by_key[key] = group
                groups.append(group)
            group["rows"].append(row)
        total = sum(s["n"] for s in stores)
        return render_template(
            "imei_pool.html",
            groups=groups,
            stores=stores,
            one=one,
            cards=cards,
            by=group_key,
            total=total,
            shown=len(rows),
            tab=tab,
            target_stores=target_stores,
            actions=db_imei.ACTIONS,
            biz_choices=db_imei.BIZ_CHOICES,
            biz_other=db_imei.BIZ_OTHER,
            needs_evidence=list(db_imei.NO_SYSTEM_EVIDENCE),
            can_write=_can_write(g.user["role"]),
            is_admin=g.user["role"] == "admin",
        )

    @app.route("/uploads/imei/<path:filename>")
    @login_required
    def imei_photo(filename):
        name = Path(filename).name
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("not found", status=404)
            store_id: Optional[int] = db_imei.photo_store(conn, name)
            if store_id is None or not db.user_can_access_store(conn, g.user, store_id):
                return Response("not found", status=404)
        return send_from_directory(db_core.DATA_DIR / "uploads" / "imei", name)
