# -*- coding: utf-8 -*-
"""
🐲 隆小侠 LONG CLAW — 全局配置

凭证从 .env 文件读取（不入库），参考 .env.example 创建本地 .env。
"""
import os
from dotenv import load_dotenv
load_dotenv()

# ── 部署模式 ─────────────────────────────────────────────
# "local" → 本地开发（现有行为，所有数据源直连）
# "cloud" → 中国云部署（跳过 yfinance/IB/Alpaca，用 Futu+AKShare 覆盖美股）
DEPLOY_MODE = os.getenv("DEPLOY_MODE", "local")

# ── 云端数据库（MongoDB）─────────────────────────────────
# 设置后自动启用 MongoDB 作为数据降级链首选源
# 格式: mongodb://user:pass@host:27017/dbname?authSource=admin
MONGO_URL = os.getenv("MONGO_URL", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "signals")
DB_ENABLED = bool(MONGO_URL)

# ── 东财隧道代理（cheapproxy.net）────────────────────────
# 隧道代理每次请求自动换 IP，解决东财频繁访问封禁
# 格式: http://user:pass@tunnel.cheapproxy.net:port
EM_PROXY_URL = os.getenv("EM_PROXY_URL", "")
EM_PROXY_ENABLED = os.getenv("EM_PROXY_ENABLED", "false").lower() == "true"

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

# ── WeClaw 微信推送 ──────────────────────────────────────
WECLAW_API_URL = os.getenv("WECLAW_API_URL", "http://127.0.0.1:18011")
WECLAW_SEND_TO = os.getenv("WECLAW_SEND_TO", "")  # 微信接收者 ID（filehelper=文件传输助手）
WECLAW_ENABLED = os.getenv("WECLAW_ENABLED", "false").lower() == "true"

# ── 标的池配置 ────────────────────────────────────────────
MAX_POOL_SIZE = 50          # 标的池上限
SCORE_THRESHOLD = 60        # 评分低于此值淘汰

# ── 监控级别 ──────────────────────────────────────────────
MONITOR_FREQS = ["15min", "30min"]

# ── 用户初始白名单（Futu 格式：市场.代码）────────────────
# A股: SH.600000 / SZ.000001
# 港股: HK.00700
# 美股: US.AAPL
WHITELIST_MAP = {
    "SH.601958": "金钼股份",
}
WHITELIST = list(WHITELIST_MAP.keys())

# L3 智能入池上限
L3_MAX_SYMBOLS: int = 20

# ── 指数配置（Layer 1）────────────────────────────────────
# A股指数（AKShare格式：sh/sz + 代码）
INDEX_AK_CODES = {
    "上证指数": "sh000001",
    "上证50":   "sh000016",
    "沪深300":  "sh000300",
    "深证成指": "sz399001",
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

# 均线计算需要更多历史数据（10月线 ≈ MA200 需 ~200交易日）
# 300自然日 ≈ 200交易日，满足所有均线计算需求
INDEX_MA_LOOKBACK_DAYS = 300

# ── 行业配置（Layer 2）────────────────────────────────────
# 默认关注板块，可随时修改；命令行 --industries 可临时覆盖
# 空列表 = 跳过 Layer 2，只跑指数 + 白名单
WATCH_INDUSTRIES: list = []

# ── 行业双榜配置（Layer 2）──────────────────────────────
# 涨幅榜 + 综合强度榜各取前 N 名行业，取并集
# 0 = 禁用此功能
RANK_TOP_N: int = 10
RANK_MAX_STOCKS_PER_IND: int = 5    # 每个行业最多入池股票数

# 综合强度评分权重（满分100 = 各权重之和，含2个领先指标）
RANK_COMPOSITE_WEIGHTS: dict = {
    "gain": 15,              # 涨幅得分 (滞后)
    "inflow": 15,            # 资金流入得分 (滞后)
    "zt_density": 15,        # 涨停密度 (滞后)
    "lianban": 5,            # 连板高度 (滞后，越高越危险)
    "strong_density": 10,    # 强势股密度 (滞后)
    "continue": 5,           # 涨停持续性 (滞后)
    "dt_penalty": 10,        # 跌停惩罚 (加大)
    "inflow_momentum": 15,   # 资金动量=流入/涨幅 (领先: 资金先行)
    "startup_ratio": 10,     # 启动率=涨停+强势数 (领先: 个股先动)
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
    # tier: "major"=外部驱动大事件(默认显示), "sector"=板块/结构性事件(可展开)
    # 选取原则：只保留有明确外部驱动力的事件（政策/地缘/重大数据），不选纯技术面点位标签
    # ── 2024 里程碑（本轮牛市起点）──
    "924":       {"date": "2024-09-24", "label": "924新政 — 央行三箭齐发"},
    # ── 2025 ──
    "deepseek":  {"date": "2025-01-23", "label": "DeepSeek行情 — AI新纪元"},
    "tariff":    {"date": "2025-04-07", "label": "关税暴跌 — 对华加征145%"},
    "zhongmei":  {"date": "2025-05-12", "label": "中美日内瓦声明 — 关税对砍91%"},
    # ── 2026 ──
    "jiangwen":  {"date": "2026-01-14", "label": "监管降温 — 融资保证金100%+严防大起大落", "tier": "major"},
    "iran":      {"date": "2026-02-28", "label": "美以打击伊朗 — 哈梅内伊遇袭+霍尔木兹海峡封锁", "tier": "major"},
    "lianghui":  {"date": "2026-03-04", "label": "两会开幕 — 十五五规划+政策预期", "tier": "major"},
}

# ── 回测验证（信号自我进化）──────────────────────────────────
# ── 分钟线缓存（解决 AKShare 5 天窗口限制）───────────────
MINUTE_CACHE_DB_PATH = ".data/minute_cache.db"
MINUTE_CACHE_MAX_DAYS = 60                 # 缓存保留天数

# ── 风控参数（止损 + 仓位建议）──────────────────────────────
RISK_PER_TRADE_PCT = 2.0                   # 单笔最大亏损占账户 %
MAX_POSITION_PCT = 25.0                    # 单标的最大仓位 %

# ── 恐慌波浪参数（最后一跌检测）────────────────────────────
PANIC_WAVE_GAP_DAYS = 5                    # 恐慌间隔超过此天数重置波浪计数
PANIC_EXHAUSTION_DECAY = 0.8               # velocity 衰减比例阈值（<前一波×0.8=衰竭）
BOTTOM_SIGNAL_MIN_WAVES = 2                # 最少恐慌波数才触发抄底信号
BOTTOM_SIGNAL_MIN_PANIC = 60               # 恐慌分数门槛
BOTTOM_SIGNAL_BASE_CONFIDENCE = 0.60       # 抄底信号基础置信度

BACKTEST_DB_PATH = ".data/backtest.db"     # 信号存档 SQLite 路径
BACKTEST_EVAL_WINDOWS = [5, 10, 20]        # 前瞻评估窗口（交易日）
BACKTEST_MIN_AGE_DAYS = 20                 # 信号满多少天后才评估
BACKTEST_NEUTRAL_PCT = 2.0                 # 方向判定中性带 ±%
BACKTEST_TARGET_PCT = 5.0                  # 目标收益率 %

# ── AutoResearch 自主研究 ──────────────────────────────────
AUTORESEARCH_DIR = ".data/autoresearch"
AUTORESEARCH_LOG = ".data/autoresearch/experiments.tsv"
AUTORESEARCH_MIN_SAMPLES = 20              # 至少 N 条已评估信号才能调参
AUTORESEARCH_MAX_DELTA_PCT = 10            # 单次变异不超过当前值 10%

# ── 板块属性分类（Layer 2 标签系统）─────────────────────────
# 东财行业名 → 属性分类
# 防守：高股息/低波动/刚需   进攻：高弹性/科技成长
# 周期：跟经济周期走          中性：兼具多重属性
SECTOR_TYPE_MAP: dict = {
    # ── 防守（高股息/刚需/低beta）──
    "银行": "防守", "保险": "防守", "多元金融": "防守",
    "电力": "防守", "水务": "防守", "燃气II": "防守",
    "白酒": "防守", "饮料乳品": "防守",
    "航运港口": "防守", "航空运输": "防守",
    "证券": "防守",
    # ── 进攻（高弹性/科技成长）──
    "半导体": "进攻", "消费电子": "进攻", "元件": "进攻",
    "印制电路板": "进攻", "光学光电子": "进攻", "其他电子": "进攻",
    "计算机设备": "进攻", "软件开发": "进攻", "IT设备": "进攻",
    "互联网服务": "进攻", "通信设备": "进攻", "通信服务": "进攻",
    "电网设备": "进攻", "电机": "进攻", "其他电源设备": "进攻",
    "光伏设备": "进攻", "风电设备": "进攻",
    "军工装备": "进攻", "军工电子": "进攻", "航空航天装备": "进攻",
    "船舶制造": "进攻",
    "游戏": "进攻", "影视院线": "进攻", "广告营销": "进攻",
    # ── 周期（跟经济周期走）──
    "煤炭": "周期", "石油石化": "周期",
    "工业金属": "周期", "小金属": "周期", "能源金属": "周期",
    "金属新材料": "周期", "钢铁": "周期",
    "化学原料": "周期", "化学制品": "周期", "农化制品": "周期",
    "电子化学品": "周期",
    "建筑材料": "周期", "建筑装饰": "周期",
    "房地产开发": "周期", "房地产服务": "周期",
    "造纸印刷": "周期", "橡胶": "周期", "塑料": "周期",
    "通用设备": "周期", "专用设备": "周期", "工程机械": "周期",
    "汽车整车": "周期", "汽车零部件": "周期",
    # ── 中性（兼具多重属性）──
    "化学制药": "中性", "生物制药": "中性", "中药": "中性",
    "医疗器械": "中性", "医疗服务": "中性", "医药商业": "中性",
    "食品加工制造": "中性", "家用电器": "中性", "照明设备": "中性",
    "种植业与林业": "中性", "养殖业": "中性", "水产": "中性",
    "仪器仪表": "中性", "轨道交通装备": "中性", "摩托车": "中性",
    "环保设备": "中性", "环保服务": "中性",
    "旅游景区": "中性", "酒店餐饮": "中性", "教育": "中性",
}

# ── 轮动线分类（科技/顺周期/消费 三线轮动）──────────────
# 与 SECTOR_TYPE_MAP（防守/进攻/周期/中性）并存，两个维度独立使用：
#   - 风险维度(SECTOR_TYPE_MAP): 情绪周期仓位建议
#   - 轮动维度(ROTATION_LINE_MAP): 轮动阶段识别
ROTATION_LINE_MAP: dict = {
    # ── 科技线（技术创新驱动）──
    "半导体": "科技", "消费电子": "科技", "元件": "科技",
    "印制电路板": "科技", "光学光电子": "科技", "其他电子": "科技",
    "计算机设备": "科技", "软件开发": "科技", "IT设备": "科技",
    "互联网服务": "科技", "通信设备": "科技", "通信服务": "科技",
    "游戏": "科技",
    # ── 顺周期线（经济复苏驱动）──
    "银行": "顺周期", "保险": "顺周期", "多元金融": "顺周期",
    "证券": "顺周期",
    "煤炭": "顺周期", "石油石化": "顺周期",
    "工业金属": "顺周期", "小金属": "顺周期", "能源金属": "顺周期",
    "金属新材料": "顺周期", "钢铁": "顺周期",
    "化学原料": "顺周期", "化学制品": "顺周期", "农化制品": "顺周期",
    "电子化学品": "顺周期",
    "建筑材料": "顺周期", "建筑装饰": "顺周期",
    "房地产开发": "顺周期", "房地产服务": "顺周期",
    "通用设备": "顺周期", "专用设备": "顺周期", "工程机械": "顺周期",
    "汽车整车": "顺周期", "汽车零部件": "顺周期",
    "航运港口": "顺周期", "航空运输": "顺周期",
    "造纸印刷": "顺周期", "橡胶": "顺周期", "塑料": "顺周期",
    # ── 消费线（消费复苏驱动）──
    "白酒": "消费", "饮料乳品": "消费",
    "食品加工制造": "消费", "家用电器": "消费", "照明设备": "消费",
    "化学制药": "消费", "生物制药": "消费", "中药": "消费",
    "医疗器械": "消费", "医疗服务": "消费", "医药商业": "消费",
    "旅游景区": "消费", "酒店餐饮": "消费", "教育": "消费",
    "种植业与林业": "消费", "养殖业": "消费", "水产": "消费",
    # ── 新能源（独立于三线轮动）──
    "光伏设备": "新能源", "风电设备": "新能源",
    "电网设备": "新能源", "电机": "新能源", "其他电源设备": "新能源",
    # ── 主题（政策/事件驱动）──
    "军工装备": "主题", "军工电子": "主题", "航空航天装备": "主题",
    "船舶制造": "主题",
    "影视院线": "主题", "广告营销": "主题",
    # ── 公用（弱周期/防御）──
    "电力": "公用", "水务": "公用", "燃气II": "公用",
    "环保设备": "公用", "环保服务": "公用",
    "仪器仪表": "公用", "轨道交通装备": "公用", "摩托车": "公用",
}

# 概念名关键词 → 属性（用于自动分类概念板块）
CONCEPT_TYPE_KEYWORDS: dict = {
    "防守": ["银行", "红利", "高股息", "公用事业", "电力", "水务", "央企"],
    "进攻": ["AI", "芯片", "算力", "机器人", "低空", "无人驾驶",
             "量子", "脑机", "卫星", "6G", "DeepSeek", "半导体",
             "鸿蒙", "华为", "光刻", "存储", "CPO",
             "十五五", "军工", "航天", "商业航天"],
    "周期": ["有色", "煤炭", "钢铁", "化工", "稀土", "锂电",
             "光伏", "风电", "储能"],
}

# 概念板块展示 Top N
CONCEPT_TOP_N: int = 15

# 噪音概念过滤（非主题性概念，按子串匹配排除）
CONCEPT_NOISE_PATTERNS: list = [
    "昨日首板", "昨日涨停", "昨日连板", "昨日触板",
    "百元股", "次新股", "ST板块", "B股",
    "破净股", "新进指数", "MSCI", "富时",
    "基金重仓", "社保重仓", "券商重仓",
    "预亏预减", "预盈预增", "东方财富热股",
    "高校背景", "国企改革", "参股新三板",
    "最近多板", "融资融券", "转融券标的",
]

# 关注主题（恐慌抄底时匹配，CLI --themes 覆盖）
# 示例: ["储能", "算力", "CLAW", "化工"]
WATCH_THEMES: list = []

# 指标股（情绪周期检测用，仅拉实时快照，不做CZSC分析）
INDICATOR_STOCKS: dict = {
    "四大行": ["SH.601398", "SH.601939", "SH.601288", "SH.601988"],
    "能源权重": ["SH.601857", "SH.600028"],
}

# ── 仿真环境（历史回放）──────────────────────────────────
SIM_DIR = ".data/sim"
SIM_SESSION_DIR = ".data/sim/sessions"
SIM_BACKTEST_DB = ".data/sim/backtest.db"
SIM_MINUTE_CACHE_DB = ".data/sim/minute_cache.db"
SIM_WAREHOUSE_DB = ".data/sim/warehouse.db"

# ── 自定义关键价位（P3-2 情景分叉）──────────────────────────
# 手动指定的关键价位（指数名 → {价位名: 价格}），与均线计算结果合并
# 示例: {"上证50": {"2917关口": 2917}, "沪深300": {"前高": 3950}}
CUSTOM_KEY_LEVELS: dict = {}

# ── 历史形态匹配配置（P3-6）──────────────────────────────────
ANALOG_LOOKBACK_DAYS = 3000     # 历史回溯天数（约12年，覆盖完整牛熊周期）
ANALOG_WINDOW = 30              # 默认匹配窗口长度（交易日）
ANALOG_TOP_K = 5                # 返回 Top K 匹配
ANALOG_MIN_SIMILARITY = 0.40    # 最低相似度阈值（0.4=中等正相关）
ANALOG_INDICES = ["沪深300", "深证成指", "创业板指", "上证50"]  # 默认匹配指数

# ── 异常检测配置（P0: sigma 异常）──────────────────────────
ANOMALY_ROLLING_WINDOW = 20              # 滚动统计窗口（交易日）
ANOMALY_THRESHOLDS = {
    "volume":         {"high": 2.0, "low": -1.5},   # 放量/缩量
    "range":          {"high": 2.5},                  # 振幅异常
    "gap":            {"high": 2.0},                  # 跳空异常
    "body":           {"high": 2.0},                  # 实体异常
}

# ── 割肉指标权重（散户止损检测）────────────────────────────
CAPITULATION_WEIGHTS = {
    "volume_spike": 30,    # 异常放量 (恐慌抛售)
    "lower_shadow": 25,    # 长下影线 (抛后有承接)
    "vol_breakout": 25,    # 缩量后突然放量 (最后的投降)
    "close_at_low": 20,    # 收盘靠近最低 (下跌环境)
}

# ── 信号融合权重（预测导向 — 动力学60% / 结构25% / 事后确认15%）───
FUSION_WEIGHTS = {
    # ─── 事后确认维度（降权，受市场环境系数调节）───
    "anomaly_volume_boost": 8,     # 异常放量加分 (15→8)
    "anomaly_volume_penalty": -5,  # 异常缩量减分 (-10→-5)
    "anomaly_gap_boost": 5,        # 异常跳空加分 (10→5)
    "anomaly_range_boost": 3,      # 异常波动加分 (5→3)
    "anomaly_body_boost": 6,       # 异常大阳/大阴加分 (12→6)
    "convergence_3dim": 20,        # ≥3维收敛加分
    "convergence_2dim": 12,        # 2维收敛加分
    "convergence_1dim": 6,         # 1维异常加分
    "capitulation_extreme": 24,    # 极度割肉加分
    "capitulation_high": 16,       # 恐慌割肉加分
    "capitulation_medium": 8,      # 偏弱割肉加分
    # ─── 卖点预警折扣 ───
    "sell_warning_extreme": 25,    # sell_warning≥80 扣分
    "sell_warning_high": 15,       # sell_warning≥60 扣分
    # ─── 动力学预测（主导权重 60%）───
    "dynamics_accel_bonus": 45,    # 笔加速 → 启动点信号（最强）
    "dynamics_exhaust_bonus": 40,  # 笔衰竭见底 → 抄底点信号
    "dynamics_ubi_strong": 35,     # 未完成笔强势延续
    "dynamics_consecutive": 25,    # 动量区间阳线占比高
    "dynamics_volume_expand": 20,  # 量能递增确认
    # ─── 板块动量（预测维度）───
    "sector_momentum_strong": 30,  # 板块动量强 → 板块级启动
    "sector_momentum_medium": 15,  # 板块动量中
}

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
