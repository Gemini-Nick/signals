# -*- coding: utf-8 -*-
"""
WeChat Agent — weclaw + Claude Code 集成模块

架构: 微信 → weclaw → Claude Code CLI → 理解意图 → 选择工具/自行回答 → 回复

- skills.py: CC 的工具函数（industry_ranking / review），按名称调用
- scripts/wechat_run.py: 工具执行入口，CC 决定调哪个工具
- deploy/weclaw/config.example.json: weclaw CLI 模式配置模板
"""
