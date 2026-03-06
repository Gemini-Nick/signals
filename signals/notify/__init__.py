# -*- coding: utf-8 -*-
"""通知：飞书推送（懒导入，缺 lark_oapi 时不影响主流程）"""


def send_text(text: str, chat_id: str = ""):
    from .feishu import send_text as _send
    _send(text, chat_id)


def send_card(card_json: dict, chat_id: str = ""):
    from .feishu import send_card as _send
    _send(card_json, chat_id)
