# -*- coding: utf-8 -*-
"""
数据同步服务 — 借鉴 Akshare-Sync 增量同步机制

将 AKShare/THS/东财数据定时同步到 MongoDB，
供 Signals 分析引擎作为降级链首选数据源。
"""
