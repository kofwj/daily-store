"""企业微信群机器人：保存日报/触客后自动发群。"""

from __future__ import annotations

import json
import logging
from urllib.request import Request, urlopen

from . import db_core

logger = logging.getLogger("wecom")

WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
TIMEOUT = 8
MAX_CONTENT = 4000  # 企微 text 消息上限 4096 字节，留余量


def _is_valid_webhook(url: str) -> bool:
    return url.startswith(WEBHOOK_PREFIX) and "key=" in url


def get_webhook(conn, store) -> str:
    """取该店该发的 webhook：地市优先，没有走全局，全局没有返回空。"""
    city = ""
    if store is not None and "city" in (store.keys() if hasattr(store, "keys") else []):
        city = (store["city"] or "").strip()
    if city:
        city_url = db_core.get_setting(conn, f"wecom_city_{city}", "")
        if city_url and _is_valid_webhook(city_url):
            return city_url
    global_url = db_core.get_setting(conn, "wecom_global", "")
    if global_url and _is_valid_webhook(global_url):
        return global_url
    return ""


def send_text(conn, store, text: str, *, source: str = "") -> bool:
    """发一条 text 到该店对应的群。失败记日志，不抛异常。"""
    url = get_webhook(conn, store)
    if not url:
        return False
    content = (text or "").strip()
    if not content:
        return False
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT - 3] + "..."
    payload = {"msgtype": "text", "text": {"content": content}}
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        result = json.loads(body) if body else {}
        if result.get("errcode", 0) != 0:
            logger.warning("wecom send failed: %s source=%s", result, source)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("wecom send error: %s source=%s", exc, source)
        return False


def send_test(conn, url: str) -> tuple[bool, str]:
    """用指定 URL 试发一条测试消息。"""
    if not _is_valid_webhook(url):
        return False, "不是有效的企微机器人地址"
    payload = {
        "msgtype": "text",
        "text": {"content": "门店日报机器人测试：收到这条说明配置成功。"},
    }
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        result = json.loads(body) if body else {}
        if result.get("errcode", 0) != 0:
            return False, f"企微返回错误：{result}"
        return True, "测试消息已发送，请到群里确认。"
    except Exception as exc:  # noqa: BLE001
        return False, f"发送失败：{exc}"
