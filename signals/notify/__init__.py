# -*- coding: utf-8 -*-
"""通知：飞书 + 微信双通道推送（懒导入，缺依赖时不影响主流程）"""
import logging

logger = logging.getLogger(__name__)


def send_text(text: str, chat_id: str = ""):
    """双通道发送纯文本：飞书 + 微信。"""
    # 飞书
    try:
        from .feishu import send_text as _feishu
        _feishu(text, chat_id)
    except Exception as e:
        logger.debug("飞书推送跳过: %s", e)
    # 微信
    try:
        from .weclaw import send_text as _weclaw
        _weclaw(text)
    except Exception as e:
        logger.debug("微信推送跳过: %s", e)


def send_card(card_json: dict, chat_id: str = ""):
    """双通道发送卡片：飞书原生卡片 + 微信文本摘要。"""
    # 飞书
    try:
        from .feishu import send_card as _feishu
        _feishu(card_json, chat_id)
    except Exception as e:
        logger.debug("飞书卡片推送跳过: %s", e)
    # 微信（卡片转文本摘要）
    try:
        from .weclaw import send_card as _weclaw
        _weclaw(card_json)
    except Exception as e:
        logger.debug("微信卡片推送跳过: %s", e)
