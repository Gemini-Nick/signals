# -*- coding: utf-8 -*-
"""
全局配置文件
"""

# ── Tushare ──────────────────────────────────────────────
TUSHARE_TOKEN = "35671ecb7ef3524dfcb8f352ad8eabbc9c139b26224b7072363da502"

# ── Futu OpenD ───────────────────────────────────────────
FUTU_HOST = "127.0.0.1"
FUTU_PORT = 11111

# ── 飞书机器人 ────────────────────────────────────────────
# 在飞书开放平台创建机器人后填入
FEISHU_APP_ID = ""
FEISHU_APP_SECRET = ""
FEISHU_RECEIVE_ID = ""      # 接收人 open_id 或群 chat_id
FEISHU_RECEIVE_TYPE = "chat_id"  # "open_id" 或 "chat_id"

# ── 标的池配置 ────────────────────────────────────────────
MAX_POOL_SIZE = 50          # 标的池上限
SCORE_THRESHOLD = 60        # 评分低于此值淘汰

# ── 监控级别 ──────────────────────────────────────────────
MONITOR_FREQS = ["15min", "30min"]

# ── 用户初始白名单（Futu 格式：市场.代码）────────────────
# A股: SH.600000 / SZ.000001
# 港股: HK.00700
# 美股: US.AAPL
WHITELIST = [
    "SH.601958",   # 金钼股份
]

# ── 指数配置（Layer 1）────────────────────────────────────
# A股指数（AKShare格式：sh/sz + 代码）
INDEX_AK_CODES = {
    "上证50":   "sh000016",
    "沪深300":  "sh000300",
    "创业板指": "sz399006",
    "科创50":   "sh000688",
    "超大盘":   "sh000043",
    "中证500":  "sh000905",
    "中证1000": "sh000852",
}
# HK指数（Futu格式，AKShare超时故走Futu）
INDEX_FUTU_CODES = {
    "恒生科技": "HK.800700",
}
# 合并索引，供统一遍历
INDEX_CODES = {**INDEX_AK_CODES, **INDEX_FUTU_CODES}

# 指数分析周期（盘中三级联动：日线背景 + 30min中枢 + 15min买卖点）
INDEX_FREQS = ["daily", "30min", "15min"]

# 指数日线滚动窗口（盘中模式，自然日）
# 180自然日 ≈ 120交易日，可形成 15~25 笔，足够识别中枢和三买卖点
INDEX_LOOKBACK_DAYS = 180

# ── 行业配置（Layer 2）────────────────────────────────────
# 默认关注板块，可随时修改；命令行 --industries 可临时覆盖
# 空列表 = 跳过 Layer 2，只跑指数 + 白名单
WATCH_INDUSTRIES: list = []

# ── 研究笔记（双维度集成）──────────────────────────────────
# 笔记目录，存放 .md/.pdf/.png/.txt 原始文件及生成的 .meta.yaml
NOTES_DIR = "notes"
