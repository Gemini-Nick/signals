#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Skill Runner — Claude Code 调用入口

用法:
    python scripts/wechat_run.py "分析 茅台"
    python scripts/wechat_run.py "回测 600519 日线"
    python scripts/wechat_run.py "大盘"
    python scripts/wechat_run.py "帮助"

weclaw CLI/ACP 模式下，Claude Code 读取微信消息后调用此脚本执行技能。
"""
import sys
import os

# 确保 signals 包可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(message: str) -> str:
    """匹配并执行技能，返回结果文本"""
    from signals.wechat.skills import get_all_skills

    for skill in get_all_skills():
        matched, params = skill.match(message)
        if matched:
            result = skill.execute(message, params)
            if result.ok:
                return result.text
            return f"⚠️ {result.error}"

    return ""  # 无匹配 — Claude Code 自行回答


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/wechat_run.py <消息>", file=sys.stderr)
        sys.exit(1)

    message = " ".join(sys.argv[1:])
    output = run(message)

    if output:
        print(output)
    else:
        # 无匹配技能 — 退出码 1 告知 Claude Code 需要自行回答
        sys.exit(1)


if __name__ == "__main__":
    main()
