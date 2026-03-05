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

# ── 行业双榜配置（Layer 2）──────────────────────────────
# 涨幅榜 + 综合强度榜各取前 N 名行业，取并集
# 0 = 禁用此功能
RANK_TOP_N: int = 10
RANK_MAX_STOCKS_PER_IND: int = 5    # 每个行业最多入池股票数

# 综合强度评分权重（满分100 = 各权重之和，可调）
RANK_COMPOSITE_WEIGHTS: dict = {
    "gain": 20,             # 涨幅得分
    "inflow": 20,           # 资金流入得分
    "zt_density": 20,       # 涨停密度
    "lianban": 10,          # 连板高度
    "strong_density": 15,   # 强势股密度
    "continue": 10,         # 涨停持续性（昨涨停续板）
    "dt_penalty": 5,        # 跌停惩罚
}

# 盘后模式历史评分权重（5维，去掉 gain/inflow，权重重分配）
RANK_HISTORICAL_WEIGHTS: dict = {
    "zt_density": 30,       # 涨停密度（权重提升）
    "lianban": 20,          # 连板高度（权重提升）
    "strong_density": 25,   # 强势股密度（权重提升）
    "continue": 15,         # 涨停持续性（权重提升）
    "dt_penalty": 10,       # 跌停惩罚（权重提升）
}

# ── 盘后复盘日期预设 ────────────────────────────────────────
# 用法：python run.py --mode review --start 924
# 相对日期在运行时计算；历史日期为固定值
DATE_PRESETS: dict = {
    # ── 相对日期 ──
    "ytd":       {"offset": "ytd",   "label": "今年以来"},
    "1w":        {"offset": 7,       "label": "最近一周"},
    "1m":        {"offset": 30,      "label": "最近一个月"},
    "3m":        {"offset": 90,      "label": "最近三个月"},
    # ── 2024 里程碑（本轮牛市起点）──
    "924":       {"date": "2024-09-24", "label": "924新政 — 央行三箭齐发"},
    "1006":      {"date": "2024-10-08", "label": "国庆后高开 — 疯牛顶部"},
    # ── 2025 ──
    "deepseek":  {"date": "2025-01-23", "label": "DeepSeek行情 — AI新纪元"},
    "spring":    {"date": "2025-02-17", "label": "春季躁动 — 两会预期"},
    "tariff":    {"date": "2025-04-07", "label": "关税暴跌 — 沪指跌7.3%"},
    "zhongmei":  {"date": "2025-05-12", "label": "中美日内瓦声明 — 关税对砍91%"},
    "625":       {"date": "2025-06-25", "label": "6月拉升 — 沪指创年内新高"},
    "17yang":    {"date": "2025-12-17", "label": "17连阳启动 — 沪指从3890连续上涨"},
    # ── 2026 ──
    "4100":      {"date": "2026-01-12", "label": "沪指破4100 — 17连阳+成交3.6万亿天量"},
    "jiangwen":  {"date": "2026-01-14", "label": "监管降温 — 融资保证金100%+严防大起大落"},
}

# ── 回测验证（信号自我进化）──────────────────────────────────
# ── 分钟线缓存（解决 AKShare 5 天窗口限制）───────────────
MINUTE_CACHE_DB_PATH = ".data/minute_cache.db"
MINUTE_CACHE_MAX_DAYS = 60                 # 缓存保留天数

# ── 风控参数（止损 + 仓位建议）──────────────────────────────
RISK_PER_TRADE_PCT = 2.0                   # 单笔最大亏损占账户 %
MAX_POSITION_PCT = 25.0                    # 单标的最大仓位 %

BACKTEST_DB_PATH = ".data/backtest.db"     # 信号存档 SQLite 路径
BACKTEST_EVAL_WINDOWS = [5, 10, 20]        # 前瞻评估窗口（交易日）
BACKTEST_MIN_AGE_DAYS = 20                 # 信号满多少天后才评估
BACKTEST_NEUTRAL_PCT = 2.0                 # 方向判定中性带 ±%
BACKTEST_TARGET_PCT = 5.0                  # 目标收益率 %

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
