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

from . import db, db_imei
from .db_core import DATA_DIR
from .helpers import (
    accessible_stores,
    admin_required,
    login_required,
    request_scope,
)

_PHOTO_MAX_BYTES = 5 * 1024 * 1024
_PHOTO_MAX_EDGE = 1600
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


def _shrink(data: bytes, ext: str) -> bytes:
    """长边 > 1600 才缩，重编码顺带丢掉手机截图里的 EXIF（含定位）。"""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 20_000_000
    try:
        im: Any = Image.open(BytesIO(data))
        im.load()
    except Exception as exc:  # noqa: BLE001 — 任何解码失败都当「不是图片」
        raise ValueError("图片打不开，换一张试试") from exc
    if im.width > _PHOTO_MAX_EDGE or im.height > _PHOTO_MAX_EDGE:
        im.thumbnail((_PHOTO_MAX_EDGE, _PHOTO_MAX_EDGE), Image.Resampling.LANCZOS)
    fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[ext]
    if fmt == "JPEG" and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    out = BytesIO()
    im.save(out, format=fmt, optimize=True, quality=85)
    return out.getvalue()


def _save_photo(upload, store_id: int) -> str:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in _PHOTO_EXTS:
        raise ValueError("截图只支持 png / jpg / webp")
    data = upload.read()
    if not data or len(data) > _PHOTO_MAX_BYTES:
        raise ValueError("截图是空的或超过 5MB")
    if not _looks_like_image(data):
        raise ValueError("这个文件不是图片")
    name = f"{secrets.token_hex(12)}{ext}"
    folder = DATA_DIR / "uploads" / "imei"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(_shrink(data, ext))
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
            stores = accessible_stores(conn)
            scope = request_scope(stores)
            ids = scope["ids"]
            # 店员只盯自己店：默认不铺全站，页头直接给「还剩几天」
            rows = db_imei.todo_rows(conn, ids, show=show, include_unmapped=is_admin)
            cards = db_imei.summary(conn, ids)
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
                }
                by_store[key] = group
                groups.append(group)
            group["rows"].append(row)
        return render_template(
            "imei.html",
            groups=groups,
            cards=cards,
            scope=scope,
            show=show,
            can_write=_can_write(g.user["role"]),
            actions=db_imei.ACTIONS,
            is_admin=is_admin,
        )

    @app.route("/imei/report", methods=["POST"])
    @login_required
    def imei_report():
        imei = (request.form.get("imei") or "").strip()
        action = (request.form.get("action") or "").strip()
        note = (request.form.get("note") or "").strip()
        back = request.form.get("back") or url_for("imei_page")
        if not back.startswith("/") or back.startswith("//"):
            back = url_for("imei_page")
        if not _can_write(g.user["role"]):
            flash("只读账号只能看，不能自报", "error")
            return redirect(back)
        with db.get_db() as conn:
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
            photo = ""
            upload = request.files.get("photo")
            if upload is not None and upload.filename:
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
                    note=note,
                    photo=photo,
                    user_id=g.user["id"],
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(back)
        flash(f"已记下：{imei[-6:]} → {action}", "ok")
        return redirect(back)

    @app.route("/imei/export")
    @admin_required
    def imei_export():
        """把门店自报导出成 CSV（带 BOM，Excel 直接打开不乱码），给台账回灌。"""
        with db.get_db() as conn:
            rows = db_imei.export_reports(conn, [s['id'] for s in accessible_stores(conn)])
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["imei", "门店", "结论", "备注", "截图", "报人", "时间"])
        for row in rows:
            writer.writerow(
                [
                    row["imei"],
                    row["store_name"],
                    row["action"],
                    row["note"],
                    row["photo"],
                    row["reporter"],
                    row["created_at"],
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
            store_id: Optional[int] = db_imei.photo_store(conn, name)
            if store_id is None or not db.user_can_access_store(conn, g.user, store_id):
                return Response("not found", status=404)
        return send_from_directory(DATA_DIR / "uploads" / "imei", name)
