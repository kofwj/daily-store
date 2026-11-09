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
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from . import db, db_core, db_imei
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


def register_imei(app) -> None:
    @app.route("/imei")
    @login_required
    def imei_page():
        show = "all" if request.args.get("show") == "all" else "open"
        is_admin = g.user["role"] == "admin"
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            ids = scope["ids"]
            # 店员只盯自己店：默认不铺全站，页头直接给「还剩几天」；筛选了具体地市/区域经理时不混入待建店
            rows = db_imei.todo_rows(
                conn, ids, show=show, include_unmapped=(is_admin and not scope["active"])
            )
            cards = db_imei.summary(conn, ids)
            # 各店标了多少台异网/已激活：管理员用来发现「全标异网躲催办」
            marks = db_imei.mark_summary(conn, ids) if is_admin else []
            # 异地异网超过 30 天还没激活：管理员人工核实，门店端只显示不再催
            review_n = (
                len(db_imei.review_rows(conn, ids, include_unmapped=not scope["active"]))
                if is_admin
                else 0
            )
        groups: List[Dict[str, Any]] = []
        by_store: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            # store_id 为空的 = 在营但档案里没有的门店（待建店），只在管理员视图出现
            key: Any = int(row["store_id"]) if row["store_id"] else "unmapped"
            group = by_store.get(key)
            if group is None:
                group = {
                    "id": key,
                    "name": row["store_name"] or row.get("dispatch_store") or "门店档案里没有",
                    "short": row["store_short"] or row.get("dispatch_store") or "待建店",
                    "unmapped": not row["store_id"],
                    "rows": [],
                    "pending": [],
                }
                by_store[key] = group
                groups.append(group)
            group["pending" if row["act_state"] == "待激活" else "rows"].append(row)
        return render_template(
            "imei.html",
            groups=groups,
            cards=cards,
            marks=marks,
            review_n=review_n,
            today_iso=db.today_local().isoformat(),
            user_types=db_imei.USER_TYPES,
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

    @app.route("/imei/report", methods=["POST"])
    @login_required
    def imei_report():
        imei = (request.form.get("imei") or "").strip()
        action = (request.form.get("action") or "").strip()
        biz_type = (request.form.get("biz_type") or "").strip()
        note = (request.form.get("note") or "").strip()
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
                "SELECT store_id FROM imei_todo WHERE imei=?", (imei,)
            ).fetchone()
            if row is None:
                flash("这条串码不在清单里，刷新后再试", "error")
                return redirect(back)
            if row["store_id"] is None:
                flash("这家店还没建账号，先补门店档案；这条不用门店报", "error")
                return redirect(back)
            store_id = int(row["store_id"])
            if not db.user_can_access_store(conn, g.user, store_id):
                # 写路径不静默落到别家店：宁可拒绝，也不能把结论记到别的店
                return Response("forbidden", status=403)
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
            photo = ""
            if has_photo:
                try:
                    photo = _save_photo(upload, store_id)
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(back)
            try:
                db_imei.add_report(
                    conn,
                    imei=imei,
                    store_id=store_id,
                    action=action,
                    biz_type=biz_type,
                    note=note,
                    photo=photo,
                    user_id=g.user["id"],
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(back)
        norm_biz = db_imei.norm_biz_type(biz_type) if action == "已办" else ""
        suffix = f" · {norm_biz}" if norm_biz else ""
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
        with db.get_db() as conn:
            try:
                result = db_imei.import_rows(conn, upload.read(), asof)
            except ValueError as exc:
                flash(f"导入失败：{exc}", "error")
                return redirect(url_for("imei_page"))
        kind = "库存（未售，按库存登记门店派）" if result["mode"] == "stock" else "台账（已售，按 POS 销售门店派）"
        msg = (
            f"{kind}：读 {result['rows']} 行，入库 {result['written']} 台，"
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
        with db.get_db() as conn:
            if not db_imei.user_in_imei_scope(conn, g.user):
                return Response("forbidden", status=403)
            rows = db_imei.pool_rows(conn, own_store=one) if one else []
            stores = db_imei.pool_summary(conn)
            cards = db_imei.summary(conn, [s["id"] for s in accessible_stores(conn)])
        groups: List[Dict[str, Any]] = []
        by_key: Dict[str, Dict[str, Any]] = {}
        for row in rows:
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
