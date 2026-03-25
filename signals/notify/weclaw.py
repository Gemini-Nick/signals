# -*- coding: utf-8 -*-
"""WeClaw 微信消息推送（通过 weclaw HTTP API）

weclaw 运行在 127.0.0.1:18011，提供 /api/send 端点。
未运行时静默跳过，不影响主流程。
"""
import json
import logging
from urllib.request import Request, urlopen
from urllib.error import URLError

import config

logger = logging.getLogger(__name__)


def _post(payload: dict) -> bool:
    """向 weclaw HTTP API 发送 POST 请求。"""
    if not config.WECLAW_ENABLED:
        return False

    url = f"{config.WECLAW_API_URL}/api/send"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
            logger.warning("weclaw 推送失败: HTTP %d", resp.status)
            return False
    except URLError as e:
        logger.debug("weclaw 未运行或不可达: %s", e)
        return False
    except Exception as e:
        logger.warning("weclaw 推送异常: %s", e)
        return False


def send_text(text: str, to: str = ""):
    """发送纯文本到微信。

    :param text: 消息内容
    :param to: 接收者 ID（默认从 config.WECLAW_SEND_TO 读取）
    """
    to = to or config.WECLAW_SEND_TO
    if not to:
        logger.debug("[跳过] weclaw 推送：未配置 WECLAW_SEND_TO")
        return

    payload = {"to": to, "text": text}
    if _post(payload):
        print("  已推送到微信")


def send_card(card_json: dict, to: str = ""):
    """将飞书卡片格式转为纯文本，推送到微信。

    微信不支持飞书的 interactive card 格式，
    这里提取 header.title + elements 中的 markdown 内容，
    拼接为纯文本发送。

    :param card_json: 飞书卡片 JSON（header + elements 结构）
    :param to: 接收者 ID
    """
    text = _card_to_text(card_json)
    if text:
        send_text(text, to)


def _card_to_text(card: dict) -> str:
    """飞书卡片 JSON → 纯文本摘要。"""
    parts = []

    # 标题
    header = card.get("header", {})
    title = header.get("title", {})
    if isinstance(title, dict):
        parts.append(f"📊 {title.get('content', '')}")
    elif isinstance(title, str):
        parts.append(f"📊 {title}")

    # 元素
    for elem in card.get("elements", []):
        tag = elem.get("tag", "")
        if tag == "markdown":
            content = elem.get("content", "")
            # 去除飞书 markdown 中的粗体标记
            content = content.replace("**", "")
            parts.append(content)
        elif tag == "hr":
            parts.append("─" * 20)
        elif tag == "div":
            # div 内可能嵌套 text
            text_obj = elem.get("text", {})
            if isinstance(text_obj, dict):
                parts.append(text_obj.get("content", ""))

    return "\n".join(parts) if parts else ""


def send_media(media_url: str, text: str = "", to: str = ""):
    """发送图片/文件到微信。

    :param media_url: 图片或文件 URL
    :param text: 附带的文本说明
    :param to: 接收者 ID
    """
    to = to or config.WECLAW_SEND_TO
    if not to:
        return

    payload = {"to": to, "media": media_url}
    if text:
        payload["text"] = text
    _post(payload)
