# -*- coding: utf-8 -*-
"""
WeChat Agent — weclaw + Claude Code 集成模块

架构: 微信 → weclaw → Claude Code CLI (Max Plan) → signals 分析引擎 → 回复

- skills.py: 9 个内置分析技能（个股/大盘/行业/回测/舆情/热点/计划/周策略/帮助）
- scripts/wechat_run.py: Claude Code 调用入口（匹配技能 → 执行 → 输出文本）
- deploy/weclaw/config.example.json: weclaw CLI 模式配置模板
"""
