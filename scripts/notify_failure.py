#!/usr/bin/env python3
"""门店日报：备份告警推送到企业微信群机器人。

webhook 来源（按顺序）：
  1. 环境变量 ALERT_WEBHOOK（可在 scripts/backup.env 里设置）
  2. 生产库 app_meta.wecom_global（复用应用已有的机器人，推荐）

用法：
  notify_failure.py --status fail --message "文字" --state-file <path>
  notify_failure.py --status ok   --message "文字" --state-file <path>
  notify_failure.py --probe                    # 只验通道，不会真的发消息

去重规则（依据 --state-file）：
  ok  -> fail   立即通知
  fail -> fail  24 小时内静默，超过 24 小时提醒一次
  fail -> ok    发一条恢复通知
  ok  -> ok     静默

环境变量：
  ALERT_WEBHOOK    覆盖 webhook
  ALERT_DRY_RUN=1  只打印将要发送的内容，不发送
  STORE_DAILY_DB   生产库路径（默认 /opt/store-daily/data/store_daily.db）

告警本身失败绝不影响备份：所有异常都吞掉并打到 stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
DEFAULT_DB = "/opt/store-daily/data/store_daily.db"
REMIND_SECONDS = 24 * 3600
TIMEOUT = 8


def redact(text: str) -> str:
    """异常/输出里可能带完整 webhook URL，key 是长期凭据，先抹掉。"""
    return re.sub(r"(key=)[^&\s\"']+", r"\1***", str(text))


def webhook_from_db(db_path: str) -> str:
    if not db_path or not os.path.exists(db_path):
        print(f"警告: 找不到库 {db_path}，无法读取 wecom_global", file=sys.stderr)
        return ""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"警告: 打不开库 {db_path}: {redact(exc)}", file=sys.stderr)
        return ""
    try:
        row = con.execute("SELECT value FROM app_meta WHERE key='wecom_global'").fetchone()
        return (row[0] or "").strip() if row else ""
    except sqlite3.Error as exc:
        print(f"警告: 读 app_meta.wecom_global 失败: {redact(exc)}", file=sys.stderr)
        return ""
    finally:
        con.close()


def resolve_webhook(db_path: str) -> tuple[str, str]:
    url = (os.environ.get("ALERT_WEBHOOK") or "").strip()
    source = "ALERT_WEBHOOK"
    if not url:
        url = webhook_from_db(db_path)
        source = f"{db_path} 的 app_meta.wecom_global"
    if not url:
        return "", "未配置：ALERT_WEBHOOK 为空，且库里没有 wecom_global"
    if not url.startswith(WEBHOOK_PREFIX):
        return "", "webhook 前缀不对（应以 %s 开头）" % WEBHOOK_PREFIX
    if "key=" not in url:
        return "", "webhook 里没有 key="
    return url, source


def post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return resp.status, (json.loads(body) if body else {})


def load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError) as exc:
        print(f"警告: state 读失败 {path}: {exc}", file=sys.stderr)
        return {}


def save_state(path: str, state: dict) -> None:
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"警告: state 写失败 {path}: {exc}", file=sys.stderr)


def decide(prev: dict, status: str, now: float) -> tuple[bool, str]:
    prev_status = str(prev.get("status") or "")
    if status == "fail":
        if prev_status != "fail":
            return True, "首次失败"
        last = float(prev.get("notified_at") or 0)
        if now - last >= REMIND_SECONDS:
            return True, "持续失败已超过 24 小时"
        return False, "仍在失败中，24 小时内不重复提醒"
    if prev_status == "fail":
        return True, "已恢复"
    return False, "一切正常，静默"


def do_probe(url: str, source: str, dry: bool) -> int:
    # 用非法 msgtype 探活：企微返回错误码，但不会真的往群里投递消息。
    if dry:
        print(f"[dry-run] probe -> {source}", file=sys.stderr)
        return 0
    try:
        code, body = post(url, {"msgtype": "__probe__", "text": {"content": "probe"}})
    except Exception as exc:  # noqa: BLE001
        print(f"探活失败: {redact(exc)}", file=sys.stderr)
        return 0
    print(
        f"探活: HTTP {code} errcode={body.get('errcode')} errmsg={body.get('errmsg')} (来源: {source})",
        file=sys.stderr,
    )
    print(
        "说明: 非法 msgtype 必被企微拒绝，群里不会出现消息；有 errcode 返回即证明网络与凭据可用。",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="备份告警推送到企微群机器人")
    ap.add_argument("--status", choices=("ok", "fail"))
    ap.add_argument("--message", default="")
    ap.add_argument("--state-file", default="")
    ap.add_argument("--db", default=os.environ.get("STORE_DAILY_DB", DEFAULT_DB))
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    dry = (os.environ.get("ALERT_DRY_RUN") or "").strip() not in ("", "0")
    url, source = resolve_webhook(args.db)
    if not url:
        print(f"警告: 备份告警未发送 —— {source}", file=sys.stderr)
        return 0

    if args.probe:
        return do_probe(url, source, dry)

    if not args.status:
        print("缺少 --status（或加 --probe）", file=sys.stderr)
        return 2

    now = time.time()
    prev = load_state(args.state_file)
    should_send, reason = decide(prev, args.status, now)
    detail = (args.message or "").strip() or f"备份状态: {args.status}"

    print(f"备份告警判定: {args.status} —— {reason}", file=sys.stderr)

    state = {
        "status": args.status,
        "since": prev.get("since", now) if prev.get("status") == args.status else now,
        "notified_at": float(prev.get("notified_at") or 0),
        "detail": detail,
        "reason": reason,
    }

    if should_send:
        if dry:
            print(
                "[dry-run] 应发送: " + json.dumps({"msgtype": "text", "text": {"content": detail}}, ensure_ascii=False),
                file=sys.stderr,
            )
            state["notified_at"] = now
        else:
            try:
                code, body = post(url, {"msgtype": "text", "text": {"content": detail}})
                if body.get("errcode", 0) == 0:
                    print(f"备份告警已发送（{reason}）: HTTP {code} (来源: {source})", file=sys.stderr)
                    state["notified_at"] = now
                else:
                    print(f"备份告警被企微拒绝（{reason}）: {json.dumps(body, ensure_ascii=False)}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"备份告警发送失败（{reason}）: {redact(exc)}", file=sys.stderr)

    save_state(args.state_file, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
