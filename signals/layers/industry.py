# -*- coding: utf-8 -*-
"""
行业工具：
- 获取行业列表 + 成分股（AKShare）
- 行业强度研判（两级降级方案）
  方法 A：行业板块 CZSC（东财接口，间歇性超时 → 降级到方法 B）
  方法 B：成分股聚合评分（始终可用）
- 双榜行业排行：涨幅榜 + 综合强度榜
- 多维度个股入池：涨停/异动/领涨/强势/龙头/融资
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pandas as pd


# ─────────────────────────────────────────────────────────
# 基础辅助函数
# ─────────────────────────────────────────────────────────

def _code6_to_futu(code: str) -> str:
    """6位A股代码 → Futu格式（SH.600000 / SZ.000001 / BJ.830799）。"""
    code = str(code).strip().zfill(6)
    if code.startswith("6"):
        return f"SH.{code}"
    elif code.startswith(("0", "3")):
        return f"SZ.{code}"
    elif code.startswith(("8", "4")):
        return f"BJ.{code}"
    return ""


_NAME_TO_CODE: dict = {}

def _build_name_to_code_map() -> dict:
    """股票名称→6位代码映射（缓存），用于将领涨股名称转换为代码。"""
    global _NAME_TO_CODE
    if _NAME_TO_CODE:
        return _NAME_TO_CODE
    import akshare as ak
    try:
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("name", "")).strip()
                code = str(row.get("code", "")).strip().zfill(6)
                if name and code:
                    _NAME_TO_CODE[name] = code
            print(f"  [✓] 名称映射已加载（{len(_NAME_TO_CODE)} 只）", flush=True)
    except Exception as e:
        print(f"  [!] 股票名称映射加载失败（{e.__class__.__name__}）", flush=True)
    return _NAME_TO_CODE


# ─────────────────────────────────────────────────────────
# 行业龙头映射（静态：行业名 → (Futu代码, 股票名称)）
# ─────────────────────────────────────────────────────────

_INDUSTRY_LEADERS: dict = {
    "煤炭": ("SH.601088", "中国神华"),
    "石油石化": ("SH.600028", "中国石化"),
    "电力": ("SH.600900", "长江电力"),
    "燃气II": ("SH.600333", "长春燃气"),
    "钢铁": ("SH.600019", "宝钢股份"),
    "工业金属": ("SH.601600", "中国铝业"),
    "小金属": ("SZ.002460", "赣锋锂业"),
    "能源金属": ("SZ.002466", "天齐锂业"),
    "金属新材料": ("SZ.002182", "云海金属"),
    "化学原料": ("SH.600309", "万华化学"),
    "化学制品": ("SZ.002648", "卫星化学"),
    "农化制品": ("SZ.000902", "新洋丰"),
    "电子化学品": ("SZ.300236", "上海新阳"),
    "白酒": ("SH.600519", "贵州茅台"),
    "啤酒": ("SH.600600", "青岛啤酒"),
    "饮料乳品": ("SH.600887", "伊利股份"),
    "食品加工制造": ("SZ.300999", "金龙鱼"),
    "银行": ("SH.601398", "工商银行"),
    "证券": ("SH.601211", "国泰君安"),
    "保险": ("SH.601318", "中国平安"),
    "多元金融": ("SH.600030", "中信证券"),
    "房地产开发": ("SZ.000002", "万科A"),
    "房地产服务": ("SZ.002285", "世联行"),
    "建筑装饰": ("SH.601668", "中国建筑"),
    "建筑材料": ("SH.600585", "海螺水泥"),
    "半导体": ("SZ.002049", "紫光国微"),
    "消费电子": ("SZ.002475", "立讯精密"),
    "元件": ("SZ.300408", "三环集团"),
    "印制电路板": ("SZ.002036", "联创电子"),
    "光学光电子": ("SZ.000725", "京东方A"),
    "其他电子": ("SZ.002371", "北方华创"),
    "计算机设备": ("SZ.000977", "浪潮信息"),
    "软件开发": ("SZ.002230", "科大讯飞"),
    "互联网服务": ("SZ.002555", "三七互娱"),
    "IT设备": ("SZ.000977", "浪潮信息"),
    "通信设备": ("SH.600050", "中国联通"),
    "通信服务": ("SH.600050", "中国联通"),
    "汽车整车": ("SZ.002594", "比亚迪"),
    "汽车零部件": ("SH.600741", "华域汽车"),
    "摩托车": ("SH.603776", "春风动力"),
    "家用电器": ("SZ.000333", "美的集团"),
    "照明设备": ("SZ.300625", "三雄极光"),
    "化学制药": ("SH.600276", "恒瑞医药"),
    "生物制药": ("SZ.300760", "迈瑞医疗"),
    "中药": ("SZ.000538", "云南白药"),
    "医疗器械": ("SZ.300760", "迈瑞医疗"),
    "医疗服务": ("SZ.300015", "爱尔眼科"),
    "医药商业": ("SH.601607", "上海医药"),
    "电网设备": ("SH.600089", "特变电工"),
    "电机": ("SZ.002074", "国轩高科"),
    "风电设备": ("SH.600905", "三峡能源"),
    "光伏设备": ("SH.601012", "隆基绿能"),
    "其他电源设备": ("SZ.300014", "亿纬锂能"),
    "军工装备": ("SH.600893", "航发动力"),
    "军工电子": ("SH.600760", "中航沈飞"),
    "航空航天装备": ("SH.600893", "航发动力"),
    "船舶制造": ("SH.600150", "中国船舶"),
    "轨道交通装备": ("SH.601766", "中国中车"),
    "通用设备": ("SH.600031", "三一重工"),
    "专用设备": ("SZ.300124", "汇川技术"),
    "工程机械": ("SH.600031", "三一重工"),
    "仪器仪表": ("SH.600590", "泰豪科技"),
    "种植业与林业": ("SZ.000998", "隆平高科"),
    "养殖业": ("SZ.002714", "牧原股份"),
    "水产": ("SZ.002069", "獐子岛"),
    "旅游景区": ("SH.600138", "中青旅"),
    "酒店餐饮": ("SH.600754", "锦江酒店"),
    "航空运输": ("SH.600029", "南方航空"),
    "航运港口": ("SH.601919", "中远海控"),
    "环保设备": ("SZ.300070", "碧水源"),
    "环保服务": ("SZ.300070", "碧水源"),
    "游戏": ("SZ.002602", "世纪华通"),
    "影视院线": ("SZ.300251", "光线传媒"),
    "广告营销": ("SZ.002027", "分众传媒"),
    "教育": ("SZ.003032", "传智教育"),
    "造纸印刷": ("SZ.000488", "晨鸣纸业"),
    "橡胶": ("SH.601966", "玲珑轮胎"),
    "塑料": ("SZ.000973", "佛塑科技"),
    "水务": ("SH.600323", "瀚蓝环境"),
    "有色金属": ("SH.601899", "紫金矿业"),
}


# ─────────────────────────────────────────────────────────
# 基础接口（已有）
# ─────────────────────────────────────────────────────────

def get_industry_list() -> pd.DataFrame:
    """返回 A 股所有行业名称列表。"""
    import akshare as ak
    df = ak.stock_board_industry_name_em()
    return df


def get_industry_stocks(industry: str) -> List[str]:
    """
    获取指定行业的成分股，返回 Futu 格式代码列表。

    :param industry: 行业名称，如 "有色金属"、"半导体"（需与 AKShare 行业名称一致）
    :return: ["SH.600489", "SZ.002460", ...]
    """
    import akshare as ak

    try:
        df = ak.stock_board_industry_cons_em(symbol=industry)
    except Exception as e:
        print(f"  [!] {industry} 成分股接口失败（{e.__class__.__name__}），返回空列表", flush=True)
        return []
    if df is None or df.empty:
        return []

    # AKShare 返回的代码列（通常是 "代码" 列，6 位数字）
    code_col = None
    for col in ["代码", "code", "股票代码"]:
        if col in df.columns:
            code_col = col
            break
    if code_col is None:
        return []

    futu_codes = []
    for code in df[code_col].astype(str):
        code = code.zfill(6)
        if code.startswith("6"):
            futu_codes.append(f"SH.{code}")
        elif code.startswith(("0", "3")):
            futu_codes.append(f"SZ.{code}")
        elif code.startswith("8") or code.startswith("4"):
            futu_codes.append(f"BJ.{code}")
    return futu_codes


# ─────────────────────────────────────────────────────────
# IndustryScore 数据类
# ─────────────────────────────────────────────────────────

@dataclass
class IndustryScore:
    """行业强度评分结果"""
    name: str                          # 行业名称
    method: str = "unknown"            # "czsc" / "members" / "unavailable"
    avg_score: float = 0.0             # 平均评分
    buy_ratio: float = 0.0             # 成分股中有买信号的比例
    bullish_ratio: float = 0.0         # 上涨趋势成分股比例（czsc方法）
    bi_count: int = 0                  # 笔数（czsc方法）
    trend: str = "未知"                # 板块趋势（czsc方法）
    latest_signal: str = "无"          # 板块最新信号（czsc方法）
    top_stocks: List[str] = field(default_factory=list)  # 最强成分股代码列表
    error: str = ""                    # 异常信息

    @property
    def is_strong(self) -> bool:
        """是否为强势行业（综合判断）"""
        if self.method == "czsc":
            return self.trend == "上涨趋势" or "买" in self.latest_signal
        return self.buy_ratio > 0.3

    @property
    def summary(self) -> str:
        if self.method == "czsc":
            sig = f" | {self.latest_signal}" if self.latest_signal != "无" else ""
            return f"{self.name} [{self.trend}{sig}]（CZSC方法）"
        elif self.method == "members":
            top_str = ", ".join(self.top_stocks[:3]) if self.top_stocks else "无"
            return (f"{self.name} 平均分={self.avg_score:.1f} "
                    f"买信号占比={self.buy_ratio:.0%} "
                    f"强势股: {top_str}（成分股聚合）")
        return f"{self.name} 数据不可用"


# ─────────────────────────────────────────────────────────
# 新增数据类：多维度选股
# ─────────────────────────────────────────────────────────

@dataclass
class StockCandidate:
    """个股候选（来自某一维度）"""
    code: str          # Futu格式代码 (SH.600000)
    name: str          # 股票名称
    role: str          # "涨停" / "异动" / "领涨" / "强势" / "龙头"
    priority: int      # 优先级（1最高：涨停>异动>领涨>强势>龙头）
    detail: str = ""   # 附加信息（连板数、量比等）


@dataclass
class IndustryRanking:
    """行业排名结果（双榜 + 代表股）"""
    name: str                                          # 行业名称
    gain_rank: int = 0                                 # 涨幅榜排名（0=未上榜）
    composite_rank: int = 0                            # 综合榜排名（0=未上榜）
    gain_pct: float = 0.0                              # 今日涨幅%
    net_inflow: float = 0.0                            # 净流入（亿元）
    composite_score: float = 0.0                       # 综合评分（0-100）
    source: str = "gain"                               # "gain" / "composite" / "both"
    candidates: List[StockCandidate] = field(default_factory=list)
    zt_count: int = 0                                  # 涨停家数
    strong_count: int = 0                              # 强势股家数
    zbgc_count: int = 0                                # 昨涨停续板家数

    @property
    def pool_codes(self) -> List[str]:
        """去重后的入池代码列表"""
        seen: set = set()
        codes: list = []
        for c in self.candidates:
            if c.code and c.code not in seen:
                seen.add(c.code)
                codes.append(c.code)
        return codes


# ─────────────────────────────────────────────────────────
# 方法 A：行业板块 CZSC（东财接口）
# ─────────────────────────────────────────────────────────

def get_industry_bars(industry: str,
                      lookback_days: int = 180,
                      start_date: str = None):
    """
    通过 stock_board_industry_hist_em 获取行业板块日线 K 线。
    东财接口间歇性超时，调用方需 try/except 降级。

    :param industry:     行业名称（东财格式），如 "有色金属"
    :param lookback_days: 盘中模式：近 N 自然日（默认180）
    :param start_date:   盘后模式：固定起点，如 '2024-09-24'
    :return: List[RawBar]，失败返回空列表
    """
    import akshare as ak
    from datetime import datetime, timedelta
    from czsc import RawBar, Freq
    import pandas as pd
    from signals.data.fetcher import _to_raw_bars

    today = datetime.now()
    if start_date:
        s_date = start_date.replace("-", "")
    else:
        s_date = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    e_date = today.strftime("%Y%m%d")

    df = ak.stock_board_industry_hist_em(
        symbol=industry, period="daily",
        start_date=s_date, end_date=e_date,
        adjust="qfq"
    )
    if df is None or df.empty:
        return []

    # 东财返回的列名可能是中文或英文，尝试两种
    col_map = {}
    for src, dst in [("日期", "dt"), ("开盘", "open"), ("最高", "high"),
                     ("最低", "low"), ("收盘", "close"), ("成交量", "vol"),
                     ("成交额", "amount"), ("date", "dt"), ("volume", "vol")]:
        if src in df.columns:
            col_map[src] = dst
    df = df.rename(columns=col_map)
    if "amount" not in df.columns:
        df["amount"] = 0

    return _to_raw_bars(df, industry, Freq.D,
                        "dt", "open", "high", "low", "close", "vol", "amount")


def score_industry_czsc(industry: str,
                        lookback_days: int = 180,
                        start_date: str = None) -> IndustryScore:
    """
    方法 A：对行业板块指数做 CZSC 分析。
    东财 SSL 超时时抛出异常，调用方降级到 score_industry_by_members()。
    """
    from .index_analyzer import IndexAnalyzer

    bars = get_industry_bars(industry, lookback_days=lookback_days,
                             start_date=start_date)
    if not bars:
        return IndustryScore(name=industry, method="unavailable",
                             error="东财数据为空")

    az = IndexAnalyzer(name=industry, symbol=industry,
                       daily_bars=bars)
    r = az.report()
    return IndustryScore(
        name=industry,
        method="czsc",
        trend=r.daily_trend,
        latest_signal=r.daily_latest_signal,
        bi_count=r.daily_bi_count,
        bullish_ratio=1.0 if r.is_bullish else 0.0,
        buy_ratio=1.0 if r.has_buy_signal else 0.0,
    )


# ─────────────────────────────────────────────────────────
# 方法 B：成分股聚合评分（降级方案，始终可用）
# ─────────────────────────────────────────────────────────

def score_industry_by_members(industry: str,
                               sample_size: int = 20,
                               freqs: list = None) -> IndustryScore:
    """
    方法 B：获取行业成分股 → 对每只跑 Layer 3 → 取平均分。

    :param industry:    行业名称
    :param sample_size: 最多分析前 N 只成分股（默认 20，控制耗时）
    :param freqs:       分析频率列表，默认 [Freq.F15, Freq.F30]
    :return: IndustryScore
    """
    from czsc import Freq as CFreq
    from .screener import IntraDayScreener

    if freqs is None:
        freqs = [CFreq.F15, CFreq.F30]

    stocks = get_industry_stocks(industry)
    if not stocks:
        return IndustryScore(name=industry, method="unavailable",
                             error="成分股获取失败")

    sample = stocks[:sample_size]
    screener = IntraDayScreener(symbols=sample, freqs=freqs)
    try:
        screener.initialize()
    except Exception as e:
        return IndustryScore(name=industry, method="unavailable",
                             error=f"成分股数据加载失败：{e}")

    results = screener.scan_once()
    if not results:
        return IndustryScore(name=industry, method="members",
                             avg_score=0.0, buy_ratio=0.0,
                             top_stocks=sample[:5])

    scores = [r.total_score for r in results]
    buy_count = sum(1 for r in results if r.total_score > 0)
    avg = sum(scores) / len(scores)
    top5 = [r.symbol for r in sorted(results, key=lambda x: -x.total_score)[:5]]

    return IndustryScore(
        name=industry,
        method="members",
        avg_score=avg,
        buy_ratio=buy_count / len(results),
        top_stocks=top5,
    )


# ─────────────────────────────────────────────────────────
# 统一入口：自动降级
# ─────────────────────────────────────────────────────────

def score_industry(industry: str,
                   lookback_days: int = 180,
                   start_date: str = None,
                   sample_size: int = 20) -> IndustryScore:
    """
    行业强度研判统一入口，自动两级降级：
    方法 A（东财 CZSC）→ 方法 B（成分股聚合）。

    :param industry:      行业名称
    :param lookback_days: 盘中模式窗口（默认180自然日）
    :param start_date:    盘后模式固定起点
    :param sample_size:   方法 B 成分股抽样数量
    :return: IndustryScore
    """
    # 方法 A：东财行业K线 CZSC
    try:
        result = score_industry_czsc(industry, lookback_days=lookback_days,
                                     start_date=start_date)
        if result.method == "czsc":
            return result
    except Exception as e:
        print(f"  [!] {industry} 东财接口失败（{e}），降级到成分股聚合", flush=True)

    # 方法 B：成分股聚合评分
    try:
        return score_industry_by_members(industry, sample_size=sample_size)
    except Exception as e:
        return IndustryScore(name=industry, method="unavailable",
                             error=str(e))


# ─────────────────────────────────────────────────────────
# 行业涨幅排行（Layer 2a）
# ─────────────────────────────────────────────────────────

def get_top_industries_by_gain(top_n: int = 10, period: str = "今日") -> list:
    """
    获取行业涨幅排行前 top_n 名。

    数据源优先级：
    A. stock_board_industry_name_em()  — 东财行业板块实时行情（含涨跌幅）
    B. stock_board_change_em()         — 东财全板块异动（降级，过滤出行业名）

    :param top_n:   取前N名行业，默认10
    :param period:  暂未使用（两个接口均为实时数据）
    :return: list of dict，每项含 name/gain_pct/net_inflow/leading_stock/leading_gain
    """
    import akshare as ak

    # ── 方法 A：东财行业板块实时行情 ──────────────────────
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None and not df.empty and "涨跌幅" in df.columns and "板块名称" in df.columns:
            df = df.sort_values("涨跌幅", ascending=False).head(top_n)
            result = []
            for _, row in df.iterrows():
                leading = str(row.get("领涨股票", ""))
                leading_gain = 0.0
                try:
                    leading_gain = float(row.get("领涨股票-涨跌幅", 0) or 0)
                except (ValueError, TypeError):
                    pass
                result.append({
                    "name":          str(row["板块名称"]),
                    "gain_pct":      float(row.get("涨跌幅", 0) or 0),
                    "net_inflow":    0.0,   # 该接口无资金流向
                    "leading_stock": leading,
                    "leading_gain":  leading_gain,
                })
            return result
    except Exception as e:
        print(f"  [!] 行业板块行情接口失败（{e}），降级到全板块异动", flush=True)

    # ── 方法 B：全板块异动（过滤行业名）─────────────────
    try:
        df_all = ak.stock_board_change_em()
        if df_all is None or df_all.empty:
            return []
        # 只保留能被 stock_board_industry_cons_em 识别的行业名
        # 用已有的行业名集合过滤（懒加载 + 缓存）
        industry_names = _get_known_industry_names()
        df_ind = df_all[df_all["板块名称"].isin(industry_names)] if industry_names else df_all
        df_ind = df_ind.sort_values("涨跌幅", ascending=False).head(top_n)
        result = []
        for _, row in df_ind.iterrows():
            leading_code = str(row.get("板块异动最频繁个股及所属类型-股票代码", ""))
            result.append({
                "name":          str(row["板块名称"]),
                "gain_pct":      float(row.get("涨跌幅", 0) or 0),
                "net_inflow":    float(row.get("主力净流入", 0) or 0) / 1e8,
                "leading_stock": leading_code,
                "leading_gain":  0.0,
            })
        return result
    except Exception as e:
        print(f"  [!] 全板块异动接口也失败（{e}）", flush=True)
        return []


# 静态行业名缓存（供方法B过滤，首次调用时尝试从接口加载，失败则用内置集合）
_KNOWN_INDUSTRY_NAMES: Optional[set] = None

def _get_known_industry_names() -> set:
    """返回已知东财行业名称集合，优先从接口加载，失败则返回内置集合。"""
    global _KNOWN_INDUSTRY_NAMES
    if _KNOWN_INDUSTRY_NAMES is not None:
        return _KNOWN_INDUSTRY_NAMES

    import akshare as ak
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None and not df.empty and "板块名称" in df.columns:
            _KNOWN_INDUSTRY_NAMES = set(df["板块名称"].tolist())
            return _KNOWN_INDUSTRY_NAMES
    except Exception:
        pass

    # 内置常用东财行业名（保底）
    _KNOWN_INDUSTRY_NAMES = {
        "煤炭", "石油石化", "电力", "燃气", "水务",
        "钢铁", "工业金属", "小金属", "金属新材料", "能源金属",
        "化学原料", "化学制品", "农化制品", "电子化学品",
        "造纸印刷", "橡胶", "塑料",
        "通用设备", "专用设备", "工程机械", "仪器仪表",
        "轨道交通装备", "航空航天装备", "军工装备", "军工电子", "船舶制造",
        "电网设备", "电机", "其他电源设备", "风电设备", "光伏设备",
        "汽车整车", "汽车零部件", "摩托车",
        "家用电器", "照明设备",
        "白酒", "啤酒", "饮料乳品", "食品加工制造",
        "种植业与林业", "养殖业", "水产",
        "化学制药", "生物制药", "中药", "医疗器械", "医疗服务", "医药商业",
        "银行", "证券", "保险", "多元金融",
        "房地产开发", "房地产服务", "建筑装饰", "建筑材料",
        "半导体", "消费电子", "元件", "印制电路板",
        "光学光电子", "其他电子", "IT设备", "计算机设备", "软件开发",
        "互联网服务", "通信设备", "通信服务",
        "环保设备", "环保服务",
        "旅游景区", "酒店餐饮", "航空运输", "航运港口",
        "教育", "游戏", "影视院线", "广告营销",
    }
    return _KNOWN_INDUSTRY_NAMES


# ─────────────────────────────────────────────────────────
# 数据加载函数（涨停池、强势股、融资等）
# ─────────────────────────────────────────────────────────

def _load_zt_pool(date_str: str) -> dict:
    """
    加载涨停池，按行业归组。
    :param date_str: YYYYMMDD
    :return: {行业名: [(6位代码, 名称, 连板数, 首封时间), ...]}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if not ind:
                continue
            code = str(row.get("代码", "")).strip().zfill(6)
            name = str(row.get("名称", ""))
            lianban = 1
            try:
                lianban = int(row.get("连板数", 1) or 1)
            except (ValueError, TypeError):
                pass
            first_time = str(row.get("首次封板时间", ""))
            result.setdefault(ind, []).append((code, name, lianban, first_time))
        print(f"  [✓] 涨停池 {sum(len(v) for v in result.values())} 只，"
              f"覆盖 {len(result)} 个行业", flush=True)
    except Exception as e:
        print(f"  [!] 涨停池加载失败（{e.__class__.__name__}）", flush=True)
    return result


def _load_strong_pool(date_str: str) -> dict:
    """
    加载强势股池，按行业归组。
    :param date_str: YYYYMMDD
    :return: {行业名: [(6位代码, 名称, 量比, 入选理由), ...]}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_strong_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if not ind:
                continue
            code = str(row.get("代码", "")).strip().zfill(6)
            name = str(row.get("名称", ""))
            liangbi = 0.0
            try:
                liangbi = float(row.get("量比", 0) or 0)
            except (ValueError, TypeError):
                pass
            reason = str(row.get("入选理由", ""))
            result.setdefault(ind, []).append((code, name, liangbi, reason))
        print(f"  [✓] 强势股池 {sum(len(v) for v in result.values())} 只，"
              f"覆盖 {len(result)} 个行业", flush=True)
    except Exception as e:
        print(f"  [!] 强势股池加载失败（{e.__class__.__name__}）", flush=True)
    return result


def _load_margin_map(date_str: str) -> dict:
    """
    加载融资融券数据（上交所），返回 {6位代码: 融资买入额}。
    注意：融资数据通常为 T-1，自动尝试前一交易日。
    """
    import akshare as ak
    from datetime import datetime, timedelta
    result: dict = {}
    # 尝试当日和前3天（覆盖周末）
    base = datetime.strptime(date_str, "%Y%m%d")
    for delta in range(0, 4):
        d = (base - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            df = ak.stock_margin_detail_sse(date=d)
            if df is not None and not df.empty:
                code_col = None
                for c in ["股票代码", "标的证券代码", "代码"]:
                    if c in df.columns:
                        code_col = c
                        break
                buy_col = None
                for c in ["融资买入额", "融资买入额(元)", "融资买入"]:
                    if c in df.columns:
                        buy_col = c
                        break
                if code_col and buy_col:
                    for _, row in df.iterrows():
                        code = str(row[code_col]).strip().zfill(6)
                        try:
                            amt = float(row[buy_col] or 0)
                        except (ValueError, TypeError):
                            amt = 0.0
                        if amt > 0:
                            result[code] = amt
                print(f"  [✓] 融资数据（{d}）{len(result)} 只", flush=True)
                return result
        except Exception:
            continue
    print("  [!] 融资数据加载失败（最近4天均无数据）", flush=True)
    return result


def _load_dt_pool(date_str: str) -> dict:
    """
    加载跌停股池，按行业计数。
    :return: {行业名: 跌停家数}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if ind:
                result[ind] = result.get(ind, 0) + 1
        print(f"  [✓] 跌停池 {sum(result.values())} 只，"
              f"覆盖 {len(result)} 个行业", flush=True)
    except Exception as e:
        print(f"  [!] 跌停池加载失败（{e.__class__.__name__}）", flush=True)
    return result


def _load_zbgc_pool(date_str: str) -> dict:
    """
    加载昨日涨停今日表现（续板）池，按行业计数。
    :return: {行业名: 续板家数}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if ind:
                result[ind] = result.get(ind, 0) + 1
        print(f"  [✓] 昨涨停跟踪 {sum(result.values())} 只，"
              f"覆盖 {len(result)} 个行业", flush=True)
    except Exception as e:
        print(f"  [!] 昨涨停跟踪加载失败（{e.__class__.__name__}）", flush=True)
    return result


# ─────────────────────────────────────────────────────────
# 行业综合强度评分
# ─────────────────────────────────────────────────────────

def compute_industry_composite_scores(
    change_df: pd.DataFrame,
    zt_pool: dict,
    strong_pool: dict,
    dt_count: dict,
    zbgc_count: dict,
    weights: dict = None,
) -> List[dict]:
    """
    计算所有行业的综合强度评分（7维度加权）。

    :param change_df:   stock_board_change_em() 结果
    :param zt_pool:     _load_zt_pool() 结果
    :param strong_pool: _load_strong_pool() 结果
    :param dt_count:    _load_dt_pool() 结果
    :param zbgc_count:  _load_zbgc_pool() 结果
    :param weights:     评分权重 dict
    :return: 按综合分降序排列的 list[dict]
    """
    import config as _cfg

    if weights is None:
        weights = getattr(_cfg, "RANK_COMPOSITE_WEIGHTS", {
            "gain": 20, "inflow": 20, "zt_density": 20,
            "lianban": 10, "strong_density": 15,
            "continue": 10, "dt_penalty": 5,
        })

    if change_df is None or change_df.empty:
        return []

    # 过滤只保留行业板块
    industry_names = _get_known_industry_names()
    df = (change_df[change_df["板块名称"].isin(industry_names)].copy()
          if industry_names else change_df.copy())

    # 提取每行业的原始维度
    rows = []
    for _, r in df.iterrows():
        name = str(r["板块名称"])
        gain = float(r.get("涨跌幅", 0) or 0)
        inflow = float(r.get("主力净流入", 0) or 0)
        zt_list = zt_pool.get(name, [])
        zt_cnt = len(zt_list)
        max_lianban = max((x[2] for x in zt_list), default=0)
        strong_cnt = len(strong_pool.get(name, []))
        zbgc = zbgc_count.get(name, 0)
        dt = dt_count.get(name, 0)
        rows.append({
            "name": name, "gain": gain, "inflow": inflow,
            "zt_count": zt_cnt, "max_lianban": max_lianban,
            "strong_count": strong_cnt, "zbgc": zbgc, "dt": dt,
        })

    if not rows:
        return []

    # min-max 归一化
    def _norm(vals):
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [0.5] * len(vals)
        return [(v - mn) / (mx - mn) for v in vals]

    gains      = _norm([r["gain"] for r in rows])
    inflows    = _norm([r["inflow"] for r in rows])
    zt_dens    = _norm([r["zt_count"] for r in rows])
    lianbans   = _norm([r["max_lianban"] for r in rows])
    strongs    = _norm([r["strong_count"] for r in rows])
    continues  = _norm([r["zbgc"] for r in rows])
    dt_pens    = _norm([r["dt"] for r in rows])

    results = []
    for i, r in enumerate(rows):
        score = (
            gains[i]     * weights.get("gain", 20)
            + inflows[i] * weights.get("inflow", 20)
            + zt_dens[i] * weights.get("zt_density", 20)
            + lianbans[i] * weights.get("lianban", 10)
            + strongs[i] * weights.get("strong_density", 15)
            + continues[i] * weights.get("continue", 10)
            - dt_pens[i] * weights.get("dt_penalty", 5)
        )
        results.append({**r, "composite_score": round(score, 1)})

    results.sort(key=lambda x: -x["composite_score"])
    return results


def compute_historical_composite_scores(
    zt_pool: dict,
    strong_pool: dict,
    dt_count: dict,
    zbgc_count: dict,
    weights: dict = None,
) -> List[dict]:
    """
    盘后模式：仅用 pool 数据的 5 维综合评分（无 change_em）。
    以涨停池覆盖的行业为候选集。

    :return: 按综合分降序排列的 list[dict]
    """
    import config as _cfg

    if weights is None:
        weights = getattr(_cfg, "RANK_HISTORICAL_WEIGHTS", {
            "zt_density": 30, "lianban": 20, "strong_density": 25,
            "continue": 15, "dt_penalty": 10,
        })

    # 合并所有 pool 中出现过的行业名
    all_industries: set = set()
    all_industries.update(zt_pool.keys())
    all_industries.update(strong_pool.keys())
    all_industries.update(zbgc_count.keys())
    all_industries.update(dt_count.keys())

    if not all_industries:
        return []

    rows = []
    for name in sorted(all_industries):
        zt_list = zt_pool.get(name, [])
        zt_cnt = len(zt_list)
        max_lianban = max((x[2] for x in zt_list), default=0)
        strong_cnt = len(strong_pool.get(name, []))
        zbgc = zbgc_count.get(name, 0)
        dt = dt_count.get(name, 0)
        rows.append({
            "name": name, "gain": 0.0, "inflow": 0.0,
            "zt_count": zt_cnt, "max_lianban": max_lianban,
            "strong_count": strong_cnt, "zbgc": zbgc, "dt": dt,
        })

    if not rows:
        return []

    # min-max 归一化
    def _norm(vals):
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [0.5] * len(vals)
        return [(v - mn) / (mx - mn) for v in vals]

    zt_dens   = _norm([r["zt_count"] for r in rows])
    lianbans  = _norm([r["max_lianban"] for r in rows])
    strongs   = _norm([r["strong_count"] for r in rows])
    continues = _norm([r["zbgc"] for r in rows])
    dt_pens   = _norm([r["dt"] for r in rows])

    results = []
    for i, r in enumerate(rows):
        score = (
            zt_dens[i]   * weights.get("zt_density", 30)
            + lianbans[i] * weights.get("lianban", 20)
            + strongs[i]  * weights.get("strong_density", 25)
            + continues[i] * weights.get("continue", 15)
            - dt_pens[i]  * weights.get("dt_penalty", 10)
        )
        results.append({**r, "composite_score": round(score, 1)})

    results.sort(key=lambda x: -x["composite_score"])
    return results


# ─────────────────────────────────────────────────────────
# 双榜行业筛选 + 多维度个股入池（主函数）
# ─────────────────────────────────────────────────────────

def get_industry_representatives(top_n: int = None,
                                  date_str: str = None) -> Tuple[
        List[IndustryRanking], List[IndustryRanking], List[IndustryRanking]]:
    """
    双榜合并 + 每行业选出代表股。

    流程：
    1. 加载全部数据源（change_em, name_em, 涨停池, 强势池, 融资, 跌停, 续板）
    2. 涨幅榜 top N（盘后降级为涨停密度排行）
    3. 综合强度榜 top N（盘后用5维评分）
    4. 对并集中每个行业，从 6 维度选出代表股

    :param top_n:    每个榜取前N名行业
    :param date_str: YYYYMMDD格式日期，None=今天（盘中模式）；
                     传入历史日期则进入盘后模式（跳过 change_em/name_em）
    :return: (gain_list, composite_list, merged_list)
             gain_list      — 涨幅榜/涨停密度榜 top N（带代表股）
             composite_list — 综合榜 top N（带代表股）
             merged_list    — 并集（去重，带代表股）
    """
    import akshare as ak
    import config as _cfg
    from datetime import datetime

    if top_n is None:
        top_n = getattr(_cfg, "RANK_TOP_N", 10)
    max_per_ind = getattr(_cfg, "RANK_MAX_STOCKS_PER_IND", 5)
    target_date = date_str or datetime.now().strftime("%Y%m%d")
    is_historical = date_str is not None  # 盘后模式标志

    # ── 1. 加载所有数据源 ────────────────────────────────
    mode_label = f"历史（{target_date}）" if is_historical else "实时"
    print(f"  加载多维数据源（{mode_label}）...", flush=True)

    change_df = None
    name_df = None
    if not is_historical:
        # 盘中模式：加载实时接口
        try:
            change_df = ak.stock_board_change_em()
            if change_df is not None and not change_df.empty:
                print(f"  [✓] 全板块异动 {len(change_df)} 条", flush=True)
        except Exception as e:
            print(f"  [!] 全板块异动接口失败（{e.__class__.__name__}）", flush=True)

        try:
            name_df = ak.stock_board_industry_name_em()
            if name_df is not None and not name_df.empty:
                print(f"  [✓] 行业板块行情 {len(name_df)} 条", flush=True)
        except Exception as e:
            print(f"  [!] 行业板块行情接口失败（{e.__class__.__name__}）", flush=True)
    else:
        print("  [i] 盘后模式：跳过 change_em / name_em（仅实时接口）", flush=True)

    zt_pool = _load_zt_pool(target_date)
    strong_pool = _load_strong_pool(target_date)
    margin_map = _load_margin_map(target_date)
    dt_count = _load_dt_pool(target_date)
    zbgc_count = _load_zbgc_pool(target_date)
    name_to_code = _build_name_to_code_map()

    # ── 2. 榜单 A：涨幅排行（盘中）/ 涨停密度排行（盘后）──
    gain_industries: list = []   # [(行业名, {gain_pct, net_inflow, leading_name, leading_gain})]

    if is_historical:
        # 盘后模式：按涨停密度排行（替代涨幅排行）
        zt_density_rank = sorted(
            [(ind, len(stocks)) for ind, stocks in zt_pool.items()],
            key=lambda x: -x[1]
        )[:top_n]
        for ind_name, zt_cnt in zt_density_rank:
            gain_industries.append((ind_name, {
                "gain_pct": 0.0,
                "net_inflow": 0.0,
                "leading_name": "",
                "leading_gain": 0.0,
                "zt_count": zt_cnt,
            }))
    elif name_df is not None and not name_df.empty and "涨跌幅" in name_df.columns:
        sorted_df = name_df.sort_values("涨跌幅", ascending=False)
        for _, row in sorted_df.head(top_n).iterrows():
            ind_name = str(row.get("板块名称", "")).strip()
            if not ind_name:
                continue
            gain_industries.append((ind_name, {
                "gain_pct": float(row.get("涨跌幅", 0) or 0),
                "net_inflow": 0.0,
                "leading_name": str(row.get("领涨股票", "")),
                "leading_gain": float(row.get("领涨股票-涨跌幅", 0) or 0),
            }))
    elif change_df is not None and not change_df.empty:
        industry_names = _get_known_industry_names()
        df_ind = (change_df[change_df["板块名称"].isin(industry_names)]
                  if industry_names else change_df)
        df_ind = df_ind.sort_values("涨跌幅", ascending=False)
        for _, row in df_ind.head(top_n).iterrows():
            ind_name = str(row["板块名称"])
            gain_industries.append((ind_name, {
                "gain_pct": float(row.get("涨跌幅", 0) or 0),
                "net_inflow": float(row.get("主力净流入", 0) or 0) / 1e8,
                "leading_name": "",
                "leading_gain": 0.0,
            }))

    # ── 3. 榜单 B：综合强度排行 ──────────────────────────
    if is_historical:
        all_scores = compute_historical_composite_scores(
            zt_pool, strong_pool, dt_count, zbgc_count
        )
    else:
        all_scores = compute_industry_composite_scores(
            change_df, zt_pool, strong_pool, dt_count, zbgc_count
        )
    composite_industries = all_scores[:top_n]  # list of dict

    # ── 4. 合并双榜 → 为每行业选代表股 ──────────────────
    gain_names = {x[0] for x in gain_industries}
    comp_names = {x["name"] for x in composite_industries}
    all_ind_names = list(dict.fromkeys(
        [x[0] for x in gain_industries] + [x["name"] for x in composite_industries]
    ))

    # 辅助：从 change_df 获取行业信息
    change_map: dict = {}
    if change_df is not None and not change_df.empty:
        for _, row in change_df.iterrows():
            change_map[str(row["板块名称"])] = row

    # 辅助：gain_industries → dict
    gain_map = {x[0]: x[1] for x in gain_industries}

    # 辅助：composite → dict
    comp_map = {x["name"]: x for x in composite_industries}

    def _select_candidates(ind_name: str) -> List[StockCandidate]:
        """为单个行业从 6 个维度选出候选股。"""
        cands: list = []

        # 1. 涨停股（优先级1）
        for code6, sname, lianban, ftime in zt_pool.get(ind_name, []):
            futu = _code6_to_futu(code6)
            if futu:
                detail = f"{lianban}连板" if lianban > 1 else "涨停"
                if ftime:
                    detail += f",{ftime}"
                cands.append(StockCandidate(
                    code=futu, name=sname, role="涨停",
                    priority=1, detail=detail))
        # 涨停股按连板数降序，只保留最强的2只
        zt_cands = sorted(
            [c for c in cands if c.role == "涨停"],
            key=lambda c: -int(c.detail.split("连板")[0])
            if "连板" in c.detail else 0)
        cands = zt_cands[:2]

        # 2. 异动股（优先级2）
        if ind_name in change_map:
            row = change_map[ind_name]
            anomaly_code = str(
                row.get("板块异动最频繁个股及所属类型-股票代码", "")).strip()
            anomaly_name = str(
                row.get("板块异动最频繁个股及所属类型-股票名称", "")).strip()
            if anomaly_code and anomaly_code != "nan":
                futu = _code6_to_futu(anomaly_code)
                if futu:
                    inflow = float(row.get("主力净流入", 0) or 0) / 1e8
                    cands.append(StockCandidate(
                        code=futu, name=anomaly_name, role="异动",
                        priority=2, detail=f"净流入{inflow:.1f}亿"))

        # 3. 领涨股（优先级3，需名称→代码映射）
        g_info = gain_map.get(ind_name, {})
        leading_name = g_info.get("leading_name", "")
        if leading_name and name_to_code:
            code6 = name_to_code.get(leading_name, "")
            if code6:
                futu = _code6_to_futu(code6)
                if futu:
                    lg = g_info.get("leading_gain", 0)
                    cands.append(StockCandidate(
                        code=futu, name=leading_name, role="领涨",
                        priority=3,
                        detail=f"{'+' if lg >= 0 else ''}{lg:.1f}%"))

        # 4. 强势股（优先级4，取量比最高的1只）
        strong_list = strong_pool.get(ind_name, [])
        if strong_list:
            best = max(strong_list, key=lambda x: x[2])
            futu = _code6_to_futu(best[0])
            if futu:
                cands.append(StockCandidate(
                    code=futu, name=best[1], role="强势",
                    priority=4, detail=f"量比{best[2]:.1f}"))

        # 5. 行业龙头（优先级5，静态）
        if ind_name in _INDUSTRY_LEADERS:
            leader_code, leader_name = _INDUSTRY_LEADERS[ind_name]
            cands.append(StockCandidate(
                code=leader_code, name=leader_name, role="龙头",
                priority=5, detail="权重股"))

        # 去重（按代码），保留优先级最高的
        seen: set = set()
        unique: list = []
        for c in sorted(cands, key=lambda x: x.priority):
            if c.code not in seen:
                seen.add(c.code)
                unique.append(c)

        # 融资标注（补充信息，不影响入池）
        for c in unique:
            code6 = c.code.split(".")[-1] if "." in c.code else ""
            if code6 in margin_map:
                margin_amt = margin_map[code6]
                c.detail += f",融资{margin_amt / 1e8:.1f}亿"

        return unique[:max_per_ind]

    # ── 构建三个列表 ─────────────────────────────────────
    def _build_ranking(ind_name: str, candidates: List[StockCandidate]) -> IndustryRanking:
        g = gain_map.get(ind_name, {})
        c = comp_map.get(ind_name, {})
        source = "both" if (ind_name in gain_names and ind_name in comp_names) \
            else ("gain" if ind_name in gain_names else "composite")

        g_rank = 0
        if ind_name in gain_names:
            g_names_list = [x[0] for x in gain_industries]
            g_rank = g_names_list.index(ind_name) + 1 if ind_name in g_names_list else 0

        c_rank = 0
        if ind_name in comp_names:
            c_names_list = [x["name"] for x in composite_industries]
            c_rank = c_names_list.index(ind_name) + 1 if ind_name in c_names_list else 0

        return IndustryRanking(
            name=ind_name,
            gain_rank=g_rank,
            composite_rank=c_rank,
            gain_pct=g.get("gain_pct", c.get("gain", 0.0)),
            net_inflow=g.get("net_inflow", 0.0) or (c.get("inflow", 0) / 1e8 if c else 0.0),
            composite_score=c.get("composite_score", 0.0),
            source=source,
            candidates=candidates,
            zt_count=c.get("zt_count", len(zt_pool.get(ind_name, []))),
            strong_count=c.get("strong_count", len(strong_pool.get(ind_name, []))),
            zbgc_count=c.get("zbgc", zbgc_count.get(ind_name, 0)),
        )

    # 选股（所有上榜行业）
    all_cands_map = {name: _select_candidates(name) for name in all_ind_names}

    # 涨幅榜列表
    gain_list = [_build_ranking(x[0], all_cands_map.get(x[0], []))
                 for x in gain_industries]

    # 综合榜列表
    composite_list = [_build_ranking(x["name"], all_cands_map.get(x["name"], []))
                      for x in composite_industries]

    # 合并列表（去重，保持顺序：先涨幅榜、再综合榜独有）
    merged_list: list = []
    seen_names: set = set()
    for r in gain_list:
        seen_names.add(r.name)
        merged_list.append(r)
    for r in composite_list:
        if r.name not in seen_names:
            seen_names.add(r.name)
            merged_list.append(r)

    return gain_list, composite_list, merged_list
