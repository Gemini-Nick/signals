#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Skill Runner — 仅运行需要 Web API 的技能

仅匹配:
  - 行业/板块/排行 → 调 Web API /api/industry/*
  - 复盘/盘后       → 调 Web API /api/review/*
  - 帮助            → 显示指令列表

其他所有消息返回退出码 1，由 Claude Code 自行回答。

用法:
    python scripts/wechat_run.py "行业排行"
    python scripts/wechat_run.py "盘后复盘"
    python scripts/wechat_run.py "帮助"
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(message: str) -> str:
    """匹配并执行 Web API 技能，返回结果文本"""
    from signals.wechat.skills import get_all_skills

    for skill in get_all_skills():
        matched, params = skill.match(message)
        if matched:
            result = skill.execute(message, params)
            if result.ok:
                return result.text
            return f"⚠️ {result.error}"

    return ""


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/wechat_run.py <消息>", file=sys.stderr)
        sys.exit(1)

    message = " ".join(sys.argv[1:])
    output = run(message)

    if output:
        print(output)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
