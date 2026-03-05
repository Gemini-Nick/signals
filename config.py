# -*- coding: utf-8 -*-
"""
🐲 隆小侠 LONG CLAW — 全局配置

凭证从 .env 文件读取（不入库），参考 .env.example 创建本地 .env。
"""
import os
from dotenv import load_dotenv
load_dotenv()

# ── Tushare ──────────────────────────────────────────────
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")

# ── Futu OpenD ───────────────────────────────────────────
FUTU_HOST = os.getenv("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.getenv("FUTU_PORT", "11111"))

# ── IB Gateway (美股盘中优先) ────────────────────────────
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4001"))       # 4001=live, 4002=paper
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))

# ── Alpaca (美股盘后优先) ────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

# ── 飞书机器人 ────────────────────────────────────────────
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_RECEIVE_ID = os.getenv("FEISHU_RECEIVE_ID", "")
FEISHU_RECEIVE_TYPE = os.getenv("FEISHU_RECEIVE_TYPE", "chat_id")

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
# 美股指数 ETF（Futu格式，Layer 1 三级联动）
INDEX_US_CODES = {
    "标普500":  "US.SPY",
    "纳斯达克": "US.QQQ",
    "道琼斯":   "US.DIA",
}
# 合并索引，供统一遍历
INDEX_CODES = {**INDEX_AK_CODES, **INDEX_FUTU_CODES, **INDEX_US_CODES}

# 指数分析周期（实线虚线框架三级联动：日线背景 + 30min中枢 + 15min买卖点）
INDEX_FREQS = ["daily", "30min", "15min"]

# 指数日线滚动窗口（盘中模式，自然日）
# 180自然日 ≈ 120交易日，可形成 15~25 笔，足够识别中枢和三买卖点
INDEX_LOOKBACK_DAYS = 180

# ── 行业配置（Layer 2）────────────────────────────────────
# 默认关注板块，可随时修改；命令行 --industries 可临时覆盖
# 空列表 = 跳过 Layer 2，只跑指数 + 白名单
WATCH_INDUSTRIES: list = []

# ── 研究笔记（双维度集成）──────────────────────────────────
# 笔记根目录，按 年/月 子目录存储：notes/2026/03/xxx.pdf
NOTES_DIR = "notes"


def notes_month_dir(date_str: str = "") -> str:
    """
    返回按年月组织的笔记子目录路径，如 notes/2026/03。
    date_str 格式 YYYY-MM-DD，为空则使用当天日期。
    """
    import os
    from datetime import datetime
    if date_str:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    else:
        dt = datetime.now()
    month_dir = os.path.join(NOTES_DIR, dt.strftime("%Y"), dt.strftime("%m"))
    os.makedirs(month_dir, exist_ok=True)
    return month_dir
