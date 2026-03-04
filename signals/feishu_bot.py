# -*- coding: utf-8 -*-
"""
飞书研报助手 — 长连接模式

在飞书群聊中上传文件/图片/发送文本，自动导入为结构化研究笔记。

工作流程：
1. 用户在群聊上传 PDF/图片/Markdown/文本
2. Bot 自动下载 → 调用 import_note() 提取文本 → 识别行业/标的/观点
3. 在群聊中回复结构化分析结果
4. 文件保存至 notes/ 目录，生成 .meta.yaml

启动方式：
  python run.py --mode bot

前置条件：
  pip install lark-oapi
  在 config.py 中填入 FEISHU_APP_ID 和 FEISHU_APP_SECRET
"""
import json
import os
import time
import threading
from pathlib import Path

import config

# 支持的文件扩展名
_SUPPORTED_EXTS = {".md", ".pdf", ".png", ".jpg", ".jpeg", ".txt"}

# 全局客户端
_client = None


# ─────────────────────────────────────────────────────────
# 文件下载
# ─────────────────────────────────────────────────────────

def _current_month_dir():
    """返回当月笔记目录并确保存在"""
    return config.notes_month_dir()


def _download_file(message_id: str, file_key: str, file_name: str) -> str:
    """下载消息中的文件附件，存入当月子目录"""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    month_dir = _current_month_dir()

    request = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(file_key) \
        .type("file") \
        .build()

    response = _client.im.v1.message_resource.get(request)
    if not response.success():
        print(f"  [!] 下载文件失败: code={response.code} msg={response.msg}")
        return ""

    # 避免覆盖同名文件
    save_path = os.path.join(month_dir, file_name)
    if os.path.exists(save_path):
        stem = Path(file_name).stem
        ext = Path(file_name).suffix
        save_path = os.path.join(month_dir, f"{stem}_{int(time.time())}{ext}")

    with open(save_path, "wb") as f:
        f.write(response.file.read())

    print(f"  已下载: {save_path}")
    return save_path


def _download_image(message_id: str, image_key: str) -> str:
    """下载消息中的图片，存入当月子目录"""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    month_dir = _current_month_dir()

    request = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(image_key) \
        .type("image") \
        .build()

    response = _client.im.v1.message_resource.get(request)
    if not response.success():
        print(f"  [!] 下载图片失败: code={response.code} msg={response.msg}")
        return ""

    file_name = f"feishu_{int(time.time())}.png"
    save_path = os.path.join(month_dir, file_name)

    with open(save_path, "wb") as f:
        f.write(response.file.read())

    print(f"  已下载图片: {save_path}")
    return save_path


def _save_text_as_file(text: str) -> str:
    """将纯文本消息保存为 .txt 文件，存入当月子目录"""
    month_dir = _current_month_dir()
    file_name = f"feishu_{int(time.time())}.txt"
    save_path = os.path.join(month_dir, file_name)

    with open(save_path, "w", encoding="utf-8") as f:
        f.write(text)

    print(f"  已保存文本: {save_path}")
    return save_path


# ─────────────────────────────────────────────────────────
# 消息回复
# ─────────────────────────────────────────────────────────

def _reply_text(chat_id: str, text: str):
    """发送文本消息到群聊"""
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest, CreateMessageRequestBody,
    )

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .content(json.dumps({"text": text}))
            .msg_type("text")
            .build()
        ).build()

    response = _client.im.v1.message.create(request)
    if not response.success():
        print(f"  [!] 发送消息失败: code={response.code} msg={response.msg}")
    else:
        print(f"  已回复群聊 {chat_id}")


def _format_note_reply(note) -> str:
    """将 ResearchNote 格式化为飞书回复文本"""
    lines = [
        "--- 研报导入完成 ---",
        "",
        f"标题: {note.title}",
        f"来源: {note.source_label}",
        f"日期: {note.date}",
        "",
        f"行业: {'、'.join(note.sectors) or '未识别'}",
        f"标的: {'、'.join(note.stocks[:8]) or '未识别'}",
        f"观点: {note.sentiment}",
    ]
    if note.catalysts:
        lines.append("")
        lines.append("催化剂:")
        for i, c in enumerate(note.catalysts[:3], 1):
            lines.append(f"  {i}. {c}")
    lines.append("")
    lines.append("(可手动编辑 .meta.yaml 修正自动识别结果)")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# 核心处理逻辑
# ─────────────────────────────────────────────────────────

def _process_and_reply(chat_id: str, file_path: str, sender_name: str = ""):
    """导入笔记 → 格式化 → 回复群聊"""
    from signals.research import import_note

    try:
        note = import_note(file_path=file_path, source=sender_name)
        reply = _format_note_reply(note)
        _reply_text(chat_id, reply)
    except Exception as e:
        print(f"  [!] 处理失败: {e}")
        _reply_text(chat_id, f"[导入失败] {e}")


def _handle_message(data) -> None:
    """
    im.message.receive_v1 事件回调。
    根据消息类型分发：file → 下载并导入，image → 下载并 OCR，text → 保存并导入。
    """
    msg = data.event.message
    if not msg:
        return

    # 跳过机器人自己发的消息
    sender = data.event.sender
    if sender and sender.sender_type == "app":
        return

    chat_id = msg.chat_id
    message_id = msg.message_id
    message_type = msg.message_type

    # 获取发送者信息
    sender_name = ""
    if sender and sender.sender_id:
        sender_name = sender.sender_id.open_id or ""

    print(f"\n>>> 收到消息 [{message_type}] chat={chat_id}")

    try:
        content = json.loads(msg.content) if msg.content else {}
    except json.JSONDecodeError:
        print(f"  [!] 无法解析消息内容: {msg.content}")
        return

    # 在独立线程中处理，避免阻塞长连接事件循环
    if message_type == "file":
        file_key = content.get("file_key", "")
        file_name = content.get("file_name", "unknown")

        if not _is_supported_file(file_name):
            ext = Path(file_name).suffix
            _reply_text(
                chat_id,
                f"[不支持的格式] {ext}\n支持: .md / .pdf / .png / .jpg / .txt",
            )
            return

        def process():
            path = _download_file(message_id, file_key, file_name)
            if path:
                _process_and_reply(chat_id, path, sender_name)

        threading.Thread(target=process, daemon=True).start()

    elif message_type == "image":
        image_key = content.get("image_key", "")

        def process():
            path = _download_image(message_id, image_key)
            if path:
                _process_and_reply(chat_id, path, sender_name)

        threading.Thread(target=process, daemon=True).start()

    elif message_type == "text":
        text = content.get("text", "").strip()
        # 忽略太短的文本（可能是闲聊而非研报）
        if len(text) < 20:
            return

        def process():
            path = _save_text_as_file(text)
            _process_and_reply(chat_id, path, sender_name)

        threading.Thread(target=process, daemon=True).start()


def _is_supported_file(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in _SUPPORTED_EXTS


# ─────────────────────────────────────────────────────────
# 启动入口
# ─────────────────────────────────────────────────────────

def start():
    """
    启动飞书 Bot（WebSocket 长连接模式）。

    长连接模式优势：
    - 不需要公网 IP / ngrok
    - 不需要 Flask / HTTP 服务器
    - 只需 APP_ID + APP_SECRET
    """
    import lark_oapi as lark

    global _client

    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        print("错误: 请在 config.py 中填入 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        print()
        print("操作步骤:")
        print("  1. 打开 https://open.feishu.cn → 开发者后台")
        print("  2. 创建企业自建应用 → 凭证与基础信息 → 复制 App ID / App Secret")
        print("  3. 添加应用能力 → 机器人")
        print("  4. 权限管理 → 开通 im:message / im:resource 等权限")
        print("  5. 事件与回调 → 订阅方式选「长连接」→ 添加 im.message.receive_v1")
        print("  6. 版本管理与发布 → 创建版本 → 发布")
        print("  7. 将机器人添加到目标群聊")
        return

    # 创建 API 客户端（用于下载文件、发送消息）
    _client = lark.Client.builder() \
        .app_id(config.FEISHU_APP_ID) \
        .app_secret(config.FEISHU_APP_SECRET) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    # 注册事件处理器
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(_handle_message) \
        .build()

    # 创建 WebSocket 长连接客户端
    ws_client = lark.ws.Client(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    print("=" * 50)
    print("  飞书研报助手已启动（长连接模式）")
    print("=" * 50)
    print()
    print("  在群聊中上传文件或发送文本，自动导入分析")
    print("  支持格式: .md / .pdf / .png / .jpg / .txt")
    print("  文本消息: 超过 20 字自动视为研报导入")
    print()
    print("  按 Ctrl+C 停止")
    print()

    ws_client.start()
