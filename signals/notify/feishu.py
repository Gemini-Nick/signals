# -*- coding: utf-8 -*-
"""飞书消息推送（仅发送，不接收）"""
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

import config

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = lark.Client.builder() \
            .app_id(config.FEISHU_APP_ID) \
            .app_secret(config.FEISHU_APP_SECRET) \
            .build()
    return _client


def send_text(text: str, chat_id: str = ""):
    """发送纯文本到飞书群聊"""
    chat_id = chat_id or config.FEISHU_RECEIVE_ID
    if not chat_id:
        print("  [跳过] 飞书推送：未配置 FEISHU_RECEIVE_ID")
        return
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .content(json.dumps({"text": text}))
            .msg_type("text")
            .build()
        ).build()
    resp = _get_client().im.v1.message.create(req)
    if resp.success():
        print("  已推送到飞书群聊")
    else:
        print(f"  [!] 飞书推送失败: {resp.code} {resp.msg}")


def send_card(card_json: dict, chat_id: str = ""):
    """发送交互式卡片到飞书群聊（支持 collapsible_panel 折叠）"""
    chat_id = chat_id or config.FEISHU_RECEIVE_ID
    if not chat_id:
        print("  [跳过] 飞书推送：未配置 FEISHU_RECEIVE_ID")
        return
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .content(json.dumps(card_json))
            .msg_type("interactive")
            .build()
        ).build()
    resp = _get_client().im.v1.message.create(req)
    if resp.success():
        print("  已推送飞书卡片")
    else:
        print(f"  [!] 飞书卡片推送失败: {resp.code} {resp.msg}")
