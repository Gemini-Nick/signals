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
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

from signals.data.fetcher import no_proxy as _no_proxy
from signals.dashboard import get_dashboard as _get_dashboard


# ─────────────────────────────────────────────────────────
# Dashboard 辅助（detail = 细节，log = 重要状态变更）
# ─────────────────────────────────────────────────────────

def _detail(msg: str):
    """任务级详情输出（per-task status, 线程池回调等）"""
    dash = _get_dashboard()
    if dash:
        dash.detail(msg)
    else:
        print(msg, flush=True)

def _log(msg: str):
    """重要状态变更输出（熔断、模式切换等）"""
    dash = _get_dashboard()
    if dash:
        dash.log(msg)
    else:
        print(msg, flush=True)


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
_NAME_CACHE_PATH = str(Path(__file__).resolve().parent.parent.parent / ".cache" / "name_to_code.json")

def _build_name_to_code_map() -> dict:
    """
    股票名称→6位代码映射（三级缓存）：
    1. 内存（_NAME_TO_CODE 非空直接返回）
    2. 本地 JSON（.cache/name_to_code.json，7天有效）
    3. AKShare API（兜底，~60s）
    """
    global _NAME_TO_CODE
    if _NAME_TO_CODE:
        return _NAME_TO_CODE

    import json, os
    from datetime import datetime, timedelta

    # 尝试本地缓存
    try:
        if os.path.exists(_NAME_CACHE_PATH):
            mtime = datetime.fromtimestamp(os.path.getmtime(_NAME_CACHE_PATH))
            if datetime.now() - mtime < timedelta(days=7):
                with open(_NAME_CACHE_PATH, "r", encoding="utf-8") as f:
                    _NAME_TO_CODE.update(json.load(f))
                _detail(f"  [✓] 名称映射从缓存加载（{len(_NAME_TO_CODE)} 只，"
                        f"缓存日期 {mtime:%m-%d}）")
                return _NAME_TO_CODE
    except Exception:
        pass

    # API 拉取
    import akshare as ak
    try:
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("name", "")).strip()
                code = str(row.get("code", "")).strip().zfill(6)
                if name and code:
                    _NAME_TO_CODE[name] = code
            # 写入本地缓存
            try:
                os.makedirs(os.path.dirname(_NAME_CACHE_PATH), exist_ok=True)
                with open(_NAME_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(_NAME_TO_CODE, f, ensure_ascii=False)
            except Exception:
                pass
            _detail(f"  [✓] 名称映射已加载（{len(_NAME_TO_CODE)} 只，已缓存）")
    except Exception as e:
        _detail(f"  [!] 股票名称映射加载失败（{e.__class__.__name__}）")
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
# 东财 API 熔断器 — SSLError 一次即熔断，后续调用直接走缓存
# ─────────────────────────────────────────────────────────
_EM_CIRCUIT_OPEN = False          # True = 东财不可用，跳过网络调用
_EM_NAME_DF_CACHE: Optional[pd.DataFrame] = None   # 首次成功结果缓存


def _fetch_board_industry_name_em(timeout: float = 10.0) -> Optional[pd.DataFrame]:
    """
    带熔断+重试的 stock_board_industry_name_em 调用：
    - 整体超时 10s（用 ThreadPoolExecutor 强制截断）
    - 失败自动重试 1 次
    - 首次成功后缓存结果，后续直接返回
    - 两次失败后标记熔断，本次运行内不再尝试
    """
    global _EM_CIRCUIT_OPEN, _EM_NAME_DF_CACHE

    # 有缓存直接返回
    if _EM_NAME_DF_CACHE is not None:
        return _EM_NAME_DF_CACHE

    # 熔断打开 → 跳过
    if _EM_CIRCUIT_OPEN:
        return None

    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    def _call():
        return ak.stock_board_industry_name_em()

    for attempt in range(2):
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_call)
                df = future.result(timeout=timeout)
                if df is not None and not df.empty:
                    _EM_NAME_DF_CACHE = df
                    return df
                return None
        except FutureTimeout:
            if attempt == 0:
                _log(f"  [⚡] 东财行业接口超时（>{timeout}s），重试中...")
                continue
            _EM_CIRCUIT_OPEN = True
            _log(f"  [⚡] 东财行业接口超时（重试仍失败），熔断")
            return None
        except Exception as e:
            if attempt == 0:
                _log(f"  [⚡] 东财行业接口异常（{e.__class__.__name__}），重试中...")
                continue
            _EM_CIRCUIT_OPEN = True
            _log(f"  [⚡] 东财行业接口熔断（{e.__class__.__name__}），本次运行跳过后续调用")
            return None
    return None


# ─────────────────────────────────────────────────────────
# 基础接口（已有）
# ─────────────────────────────────────────────────────────

def get_industry_list() -> pd.DataFrame:
    """返回 A 股所有行业名称列表。"""
    df = _fetch_board_industry_name_em()
    if df is not None:
        return df
    # 熔断时返回空 DataFrame
    return pd.DataFrame()


def get_industry_stocks(industry: str) -> List[str]:
    """
    获取指定行业的成分股，返回 Futu 格式代码列表。

    :param industry: 行业名称，如 "有色金属"、"半导体"（需与 AKShare 行业名称一致）
    :return: ["SH.600489", "SZ.002460", ...]
    """
    import akshare as ak

    try:
        with _no_proxy():
            df = ak.stock_board_industry_cons_em(symbol=industry)
    except Exception as e:
        _detail(f"  [!] {industry} 成分股接口失败（{e.__class__.__name__}），返回空列表")
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
    sector_type: str = "中性"                           # 防守/进攻/周期/中性
    concept_tags: List[str] = field(default_factory=list)  # 关联的热门概念名
    oversold_score: float = 0.0                           # 超跌评分(0-100)
    oversold_detail: str = ""                             # "距高点-18%"
    rotation_line: str = ""                                # 轮动线: 科技/顺周期/消费/新能源/主题/公用
    # P3-3: 板块节奏
    rhythm_phase: str = ""                                 # "启动"/"加速"/"高潮"/"衰竭"/"休整"
    rhythm_score: float = 0.0                              # 0-100
    rhythm_hint: str = ""                                  # "可加仓"/"持有"/"兑现"/"回避"

    @property
    def display_name(self) -> str:
        """行业名+属性标签+概念标签（括号化显示）"""
        _STYPE_ICON = {"防守": "🛡", "进攻": "⚔", "周期": "🔄", "中性": ""}
        icon = _STYPE_ICON.get(self.sector_type, "")
        tag = f"[{self.sector_type}{icon}]" if self.sector_type != "中性" else ""
        concept = f"({'、'.join(self.concept_tags[:3])})" if self.concept_tags else ""
        return f"{self.name}{tag}{concept}"

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

    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    def _call():
        return ak.stock_board_industry_hist_em(
            symbol=industry, period="daily",
            start_date=s_date, end_date=e_date,
            adjust="qfq"
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_call)
            df = future.result(timeout=15)
    except (FutureTimeout, Exception) as e:
        _log(f"  [!] {industry} K线接口超时/失败（{e.__class__.__name__}）")
        return []

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
        _detail(f"  [!] {industry} 东财接口失败（{e}），降级到成分股聚合")

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

    # ── 方法 A：东财行业板块实时行情（带熔断）──────────────
    df = _fetch_board_industry_name_em()
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
        _detail(f"  [!] 全板块异动接口也失败（{e}）")
        return []


# 静态行业名缓存（供方法B过滤，首次调用时尝试从接口加载，失败则用内置集合）
_KNOWN_INDUSTRY_NAMES: Optional[set] = None

def _get_known_industry_names() -> set:
    """返回已知东财行业名称集合，优先从接口加载，失败则返回内置集合。"""
    global _KNOWN_INDUSTRY_NAMES
    if _KNOWN_INDUSTRY_NAMES is not None:
        return _KNOWN_INDUSTRY_NAMES

    df = _fetch_board_industry_name_em()
    if df is not None and not df.empty and "板块名称" in df.columns:
        _KNOWN_INDUSTRY_NAMES = set(df["板块名称"].tolist())
        return _KNOWN_INDUSTRY_NAMES

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
# 概念板块排行
# ─────────────────────────────────────────────────────────

@dataclass
class ConceptRanking:
    """概念板块排名"""
    name: str
    gain_pct: float = 0.0
    leading_stock: str = ""
    leading_gain: float = 0.0
    sector_type: str = "中性"      # 自动分类：防守/进攻/周期/中性
    tag: str = ""                  # 来源标记：""=实时, "static"=硬编码兜底


def _classify_concept(name: str) -> str:
    """根据概念名中的关键词自动分类。"""
    import config as _cfg
    keywords = getattr(_cfg, "CONCEPT_TYPE_KEYWORDS", {})
    for cat, kws in keywords.items():
        for kw in kws:
            if kw in name:
                return cat
    return "中性"


def _classify_industry(name: str) -> str:
    """根据 SECTOR_TYPE_MAP 分类行业。"""
    import config as _cfg
    return getattr(_cfg, "SECTOR_TYPE_MAP", {}).get(name, "中性")



def _concept_cache_path():
    """概念排行磁盘缓存路径"""
    from pathlib import Path
    cache_dir = Path(__file__).resolve().parent.parent.parent / ".data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "concept_rankings.json"


def _save_concept_cache(results: List[ConceptRanking], source: str = "em"):
    """保存概念排行到磁盘缓存（标记来源 source: em/ths）"""
    import json, time
    data = {
        "ts": time.time(),
        "source": source,
        "items": [
            {"name": c.name, "gain_pct": c.gain_pct,
             "leading_stock": c.leading_stock, "leading_gain": c.leading_gain,
             "sector_type": c.sector_type, "tag": c.tag}
            for c in results
        ],
    }
    try:
        _concept_cache_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_concept_cache(max_age: float = 86400) -> List[ConceptRanking]:
    """从磁盘缓存加载概念排行（默认24h过期）。校验数据有效性。"""
    import json, time
    path = _concept_cache_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) > max_age:
            return []
        items = data.get("items", [])
        if not items:
            return []
        # 缓存有效性校验：如果 gain_pct 全为 0 且来源是 THS，视为弱数据
        source = data.get("source", "unknown")
        gains = [abs(item.get("gain_pct", 0)) for item in items]
        if source == "ths" and all(g < 0.01 for g in gains):
            _detail("  [缓存] THS 缓存数据 gain_pct 全为 0，视为无效")
            return []
        results = []
        for item in items:
            results.append(ConceptRanking(
                name=item["name"],
                gain_pct=item.get("gain_pct", 0),
                leading_stock=item.get("leading_stock", ""),
                leading_gain=item.get("leading_gain", 0),
                sector_type=item.get("sector_type", ""),
                tag=item.get("tag", ""),
            ))
        _detail(f"  [缓存] 概念板块 {len(results)} 条（磁盘缓存, 来源:{source}）")
        return results
    except Exception:
        return []


# ── 概念板块硬编码兜底 ───────────────────────────────────
_FALLBACK_CONCEPTS = [
    ("AI算力", "进攻"), ("DeepSeek概念", "进攻"), ("机器人概念", "进攻"),
    ("半导体", "进攻"), ("消费电子", "进攻"), ("新能源车", "周期"),
    ("光伏", "周期"), ("军工", "主题"), ("中药", "防守"), ("白酒", "消费"),
]


def _fallback_concepts(top_n: int = 10) -> List[ConceptRanking]:
    """当东财+缓存都失败时的硬编码兜底概念列表"""
    _detail("  [兜底] 使用静态概念列表")
    results = []
    for name, stype in _FALLBACK_CONCEPTS[:top_n]:
        results.append(ConceptRanking(
            name=name,
            gain_pct=0.0,
            leading_stock="",
            leading_gain=0.0,
            sector_type=stype,
            tag="static",
        ))
    return results


def _get_concepts_ths(top_n: int = 10) -> List[ConceptRanking]:
    """
    同花顺概念板块降级源。
    获取概念名称列表 + 并行获取 K 线计算涨跌幅。
    """
    import akshare as ak
    from datetime import datetime, timedelta
    from concurrent.futures import ThreadPoolExecutor

    _detail("  [THS] 尝试同花顺概念板块...")
    df = ak.stock_board_concept_name_ths()
    if df is None or df.empty:
        raise ValueError("THS 概念列表为空")

    # 名称列: 尝试多种可能的列名
    name_col = None
    for col in ["概念名称", "name", "板块名称"]:
        if col in df.columns:
            name_col = col
            break
    if name_col is None and len(df.columns) > 0:
        name_col = df.columns[0]

    # THS 概念列表按拼音排序，不能只取前 N 个（会偏向 A/B 开头）
    # 策略：取全量名称 → 优先匹配已知热门关键词 → 补齐随机样本
    all_names = df[name_col].tolist()
    _HOT_KEYWORDS = ["AI", "算力", "机器人", "芯片", "半导体", "新能源", "光伏",
                     "储能", "电力", "军工", "低空", "白酒", "医药", "消费",
                     "汽车", "锂电", "光模块", "ChatGPT", "DeepSeek", "鸿蒙",
                     "华为", "特斯拉", "比亚迪", "CXO", "CPO"]
    # 优先选包含热门关键词的概念
    hot_names = [n for n in all_names if any(kw in n for kw in _HOT_KEYWORDS)]
    # 补齐：从剩余概念中均匀采样
    remaining = [n for n in all_names if n not in hot_names]
    import random
    sample_size = max(0, top_n * 3 - len(hot_names))
    if sample_size > 0 and remaining:
        step = max(1, len(remaining) // sample_size)
        sampled = remaining[::step][:sample_size]
    else:
        sampled = []
    concept_names = (hot_names + sampled)[:top_n * 3]  # 最多获取 3x 用于排序

    today = datetime.now()
    start_date = (today - timedelta(days=7)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    def _fetch_gain(cname):
        try:
            kdf = ak.stock_board_concept_index_ths(
                symbol=cname, start_date=start_date, end_date=end_date)
            if kdf is not None and len(kdf) >= 2:
                close_col = None
                for c in ["收盘价", "收盘", "close"]:
                    if c in kdf.columns:
                        close_col = c
                        break
                if close_col:
                    gain = (float(kdf.iloc[-1][close_col]) /
                            float(kdf.iloc[-2][close_col]) - 1) * 100
                    return cname, round(gain, 2)
            return cname, 0.0
        except Exception:
            return cname, 0.0

    # 并行获取涨跌幅
    results_raw = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_gain, n): n for n in concept_names}
        for fut in futures:
            try:
                name, gain = fut.result(timeout=10)
                results_raw.append((name, gain))
            except Exception:
                results_raw.append((futures[fut], 0.0))

    # 按涨幅排序取 top_n
    results_raw.sort(key=lambda x: -x[1])
    results = []
    for name, gain in results_raw[:top_n]:
        results.append(ConceptRanking(
            name=name,
            gain_pct=gain,
            leading_stock="",
            leading_gain=0.0,
            sector_type=_classify_concept(name),
            tag="ths",
        ))

    _detail(f"  [✓] THS 概念板块 top {len(results)} 条")
    return results


def get_concept_rankings(top_n: int = None) -> List[ConceptRanking]:
    """
    获取概念板块涨幅 top N。
    降级链: 东财 concept_name_em → 同花顺 THS → 磁盘缓存 → 硬编码兜底
    """
    import akshare as ak
    import config as _cfg
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    if top_n is None:
        top_n = getattr(_cfg, "CONCEPT_TOP_N", 10)

    # ── 1. 东财 ──
    def _call():
        with _no_proxy():
            return ak.stock_board_concept_name_em()

    df = None
    for attempt in range(2):
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_call)
                df = future.result(timeout=25)
            break
        except (FutureTimeout, Exception) as e:
            if attempt == 0:
                _detail(f"  [!] 概念板块接口失败（{e.__class__.__name__}），重试中...")
                continue
            _detail(f"  [!] 概念板块接口失败（{e.__class__.__name__}，重试仍失败）")
            df = None
            break

    if df is not None and not df.empty and "涨跌幅" in df.columns:
        df = df.sort_values("涨跌幅", ascending=False).head(top_n)
        results = []
        for _, row in df.iterrows():
            name = str(row.get("板块名称", "")).strip()
            if not name:
                continue
            leading = str(row.get("领涨股票", ""))
            leading_gain = 0.0
            try:
                leading_gain = float(row.get("领涨股票-涨跌幅", 0) or 0)
            except (ValueError, TypeError):
                pass
            results.append(ConceptRanking(
                name=name,
                gain_pct=float(row.get("涨跌幅", 0) or 0),
                leading_stock=leading,
                leading_gain=leading_gain,
                sector_type=_classify_concept(name),
            ))
        _detail(f"  [✓] 概念板块 top {len(results)} 条")
        _save_concept_cache(results, source="em")
        return results

    # ── 2. 同花顺降级 ──
    try:
        ths_results = _get_concepts_ths(top_n)
        if ths_results:
            _save_concept_cache(ths_results, source="ths")
            return ths_results
    except Exception as e:
        _detail(f"  [!] THS 概念板块也失败（{e.__class__.__name__}）")

    # ── 3. 磁盘缓存 ──
    cached = _load_concept_cache()
    if cached:
        return cached[:top_n]

    # ── 4. 硬编码兜底 ──
    return _fallback_concepts(top_n)


def _match_concepts_to_industries(
    concepts: List[ConceptRanking],
    industry_names: List[str],
    name_df: "pd.DataFrame | None",
) -> dict:
    """
    轻量关联：概念的领涨股如果同名出现在行业领涨股中，就把概念挂到该行业。
    返回 {行业名: [概念名, ...]}
    """
    if not concepts or name_df is None or name_df.empty:
        return {}

    # 构建行业→领涨股映射
    ind_leader_map: dict = {}  # {领涨股名: 行业名}
    for _, row in name_df.iterrows():
        ind = str(row.get("板块名称", "")).strip()
        leader = str(row.get("领涨股票", "")).strip()
        if ind and leader and ind in industry_names:
            ind_leader_map[leader] = ind

    result: dict = {}
    for c in concepts:
        if c.leading_stock and c.leading_stock in ind_leader_map:
            ind = ind_leader_map[c.leading_stock]
            result.setdefault(ind, []).append(c.name)

    return result


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
        _detail(f"  [✓] 涨停池 {sum(len(v) for v in result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 涨停池加载失败（{e.__class__.__name__}）")
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
        _detail(f"  [✓] 强势股池 {sum(len(v) for v in result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 强势股池加载失败（{e.__class__.__name__}）")
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
                _detail(f"  [✓] 融资数据（{d}）{len(result)} 只")
                return result
        except Exception:
            continue
    _detail("  [!] 融资数据加载失败（最近4天均无数据）")
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
        _detail(f"  [✓] 跌停池 {sum(result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 跌停池加载失败（{e.__class__.__name__}）")
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
        _detail(f"  [✓] 昨涨停跟踪 {sum(result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 昨涨停跟踪加载失败（{e.__class__.__name__}）")
    return result


# ─────────────────────────────────────────────────────────
# P3-3: 板块节奏检测（轻量补充）
# ─────────────────────────────────────────────────────────

def _enrich_rhythm(rankings: List["IndustryRanking"]):
    """为 Top-N 行业补充节奏检测（跳过已有 rhythm 的）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    need_rhythm = [r for r in rankings if not r.rhythm_phase]
    if not need_rhythm:
        return

    def _compute(r):
        try:
            bars = get_industry_bars(r.name, lookback_days=30)
            if not bars:
                return
            from signals.core.sector_rhythm import compute_sector_rhythm
            rhythm = compute_sector_rhythm(r.name, bars)
            if rhythm:
                r.rhythm_phase = rhythm.phase
                r.rhythm_score = rhythm.rhythm_score
                r.rhythm_hint = rhythm.action_hint
        except Exception:
            pass

    with _no_proxy(), ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_compute, r) for r in need_rhythm[:10]]
        for f in as_completed(futures):
            f.result()  # propagate exceptions silently


# 板块超跌检测
# ─────────────────────────────────────────────────────────

def compute_oversold_score(bars) -> tuple:
    """
    从行业板块日线K线计算超跌评分（满分100）。

    四维度加权：
    - 距20日高点回撤 (40分): >15%满分, 5~15%线性
    - 均线偏离度 (30分): 低于20日均线>10%满分, 3~10%线性
    - 连续下跌天数 (15分): 5天满分, 3天10分, 1~2天线性
    - RSI超卖 (15分): RSI14<30满分, 30~40线性

    :param bars: List[RawBar] 最近30日板块日线
    :return: (score, detail_str) — score=0表示不超跌
    """
    if not bars or len(bars) < 5:
        return 0.0, ""

    closes = [b.close for b in bars]
    latest = closes[-1]

    # 1. 距20日高点回撤 (40分)
    high_20 = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    drawdown = (high_20 - latest) / high_20 * 100 if high_20 > 0 else 0
    if drawdown >= 15:
        dd_score = 40
    elif drawdown >= 5:
        dd_score = (drawdown - 5) / 10 * 40
    else:
        dd_score = 0

    # 2. 均线偏离度 (30分) — 低于20日均线的百分比
    ma20 = sum(closes[-20:]) / min(len(closes), 20) if closes else 0
    ma_dev = (ma20 - latest) / ma20 * 100 if ma20 > 0 else 0
    if ma_dev >= 10:
        ma_score = 30
    elif ma_dev >= 3:
        ma_score = (ma_dev - 3) / 7 * 30
    else:
        ma_score = 0

    # 3. 连续下跌天数 (15分)
    consec_down = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            consec_down += 1
        else:
            break
    if consec_down >= 5:
        cd_score = 15
    elif consec_down >= 3:
        cd_score = 10
    elif consec_down >= 1:
        cd_score = consec_down * 3
    else:
        cd_score = 0

    # 4. RSI14 超卖 (15分)
    rsi_score = 0
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        # 使用最近14日
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            if rsi < 30:
                rsi_score = 15
            elif rsi < 40:
                rsi_score = (40 - rsi) / 10 * 15

    total = round(dd_score + ma_score + cd_score + rsi_score, 1)
    detail = f"距高点-{drawdown:.1f}%"
    if consec_down >= 3:
        detail += f", 连跌{consec_down}日"

    return total, detail


def get_oversold_industries(name_df=None, top_n: int = 5) -> List[IndustryRanking]:
    """
    检测超跌行业（取当前跌幅最大的板块，加载K线计算超跌评分）。

    策略：从 name_df 中取涨幅最低的 15 个行业，并行加载30日K线，
    计算超跌评分，返回评分 >= 40 的 top N。

    :param name_df: stock_board_industry_name_em() 结果
    :param top_n: 返回前N个超跌行业
    :return: List[IndustryRanking]（带 oversold_score/oversold_detail）
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if name_df is None or name_df.empty or "涨跌幅" not in name_df.columns:
        return []

    # 取跌幅最大的 15 个行业作为候选
    bottom = name_df.sort_values("涨跌幅", ascending=True).head(15)
    candidates = []
    for _, row in bottom.iterrows():
        ind = str(row.get("板块名称", "")).strip()
        gain = float(row.get("涨跌幅", 0) or 0)
        if ind:
            candidates.append((ind, gain))

    if not candidates:
        return []

    # 并行加载 30 日 K线
    def _compute_one(ind_name, gain_pct):
        try:
            bars = get_industry_bars(ind_name, lookback_days=30)
            score, detail = compute_oversold_score(bars)
            from signals.core.rotation import get_rotation_line
            # P3-3: 板块节奏检测
            r_phase, r_score, r_hint = "", 0.0, ""
            try:
                from signals.core.sector_rhythm import compute_sector_rhythm
                rhythm = compute_sector_rhythm(ind_name, bars)
                if rhythm:
                    r_phase, r_score, r_hint = rhythm.phase, rhythm.rhythm_score, rhythm.action_hint
            except Exception:
                pass
            return IndustryRanking(
                name=ind_name,
                gain_pct=gain_pct,
                sector_type=_classify_industry(ind_name),
                oversold_score=score,
                oversold_detail=detail,
                rotation_line=get_rotation_line(ind_name),
                rhythm_phase=r_phase,
                rhythm_score=r_score,
                rhythm_hint=r_hint,
            )
        except Exception:
            return None

    results = []
    with _no_proxy(), ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_compute_one, n, g): n for n, g in candidates}
        for f in as_completed(futures):
            r = f.result()
            if r and r.oversold_score >= 40:
                results.append(r)

    results.sort(key=lambda x: -x.oversold_score)
    if results:
        _detail(f"  [✓] 超跌检测: {len(results)} 个行业评分>=40")
    return results[:top_n]


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
        List[IndustryRanking], List[IndustryRanking],
        List[IndustryRanking], List[ConceptRanking],
        List[IndustryRanking]]:
    """
    双榜合并 + 每行业选出代表股 + 概念板块排行 + 超跌检测。

    流程：
    1. 加载全部数据源（change_em, name_em, 涨停池, 强势池, 融资, 跌停, 续板, 概念板块）
    2. 涨幅榜 top N（盘后降级为涨停密度排行）
    3. 综合强度榜 top N（盘后用5维评分）
    4. 对并集中每个行业，从 6 维度选出代表股
    5. 为每个行业设置 sector_type 和 concept_tags + 概念板块排行
    6. 超跌检测（盘中模式）

    :param top_n:    每个榜取前N名行业
    :param date_str: YYYYMMDD格式日期，None=今天（盘中模式）；
                     传入历史日期则进入盘后模式（跳过 change_em/name_em）
    :return: (gain_list, composite_list, merged_list, concepts, oversold_list)
             gain_list      — 涨幅榜/涨停密度榜 top N（带代表股+标签）
             composite_list — 综合榜 top N（带代表股+标签）
             merged_list    — 并集（去重，带代表股+标签）
             concepts       — 概念板块涨幅排行（带分类标签）
             oversold_list  — 超跌行业（评分>=40，盘后模式为空）
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
    _log(f"  加载多维数据源（{mode_label}）...")

    change_df = None
    name_df = None

    # 并行加载所有独立数据源（ThreadPool，IO密集型）
    # 盘中模式：change_em + name_em 也并入并行池
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _pool_tasks = {
        "zt_pool":      lambda: _load_zt_pool(target_date),
        "strong_pool":  lambda: _load_strong_pool(target_date),
        "margin_map":   lambda: _load_margin_map(target_date),
        "dt_count":     lambda: _load_dt_pool(target_date),
        "zbgc_count":   lambda: _load_zbgc_pool(target_date),
        "name_to_code": lambda: _build_name_to_code_map(),
    }
    # 概念板块排行（盘中/盘后均可）
    _pool_tasks["concepts"] = lambda: get_concept_rankings()

    if not is_historical:
        def _load_change_em():
            try:
                df = ak.stock_board_change_em()
                if df is not None and not df.empty:
                    _detail(f"  [✓] 全板块异动 {len(df)} 条")
                    return df
            except Exception as e:
                _detail(f"  [!] 全板块异动接口失败（{e.__class__.__name__}）")
            return None
        _pool_tasks["change_df"] = _load_change_em
        _pool_tasks["name_df"] = lambda: _fetch_board_industry_name_em()
        _pool_tasks["concepts"] = lambda: get_concept_rankings()
    else:
        _detail("  [i] 盘后模式：跳过 change_em / name_em（仅实时接口）")
    _pool_results = {}
    with _no_proxy(), ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn): key for key, fn in _pool_tasks.items()}
        for f in as_completed(futures):
            key = futures[f]
            try:
                _pool_results[key] = f.result()
            except Exception as e:
                _detail(f"  [!] {key} 加载异常（{e.__class__.__name__}）")
                _pool_results[key] = {} if "map" in key or "count" in key else None
    zt_pool     = _pool_results.get("zt_pool", {})
    strong_pool = _pool_results.get("strong_pool", {})
    margin_map  = _pool_results.get("margin_map", {})
    dt_count    = _pool_results.get("dt_count", {})
    zbgc_count  = _pool_results.get("zbgc_count", {})
    name_to_code = _pool_results.get("name_to_code", {})
    change_df   = _pool_results.get("change_df")
    name_df     = _pool_results.get("name_df")
    concepts    = _pool_results.get("concepts", [])
    if name_df is not None and not isinstance(name_df, pd.DataFrame):
        name_df = None
    if name_df is not None and not name_df.empty:
        _detail(f"  [✓] 行业板块行情 {len(name_df)} 条")
    concepts: List[ConceptRanking] = _pool_results.get("concepts") or []

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

        # 板块属性标签
        sector_type_map = getattr(_cfg, "SECTOR_TYPE_MAP", {})
        stype = sector_type_map.get(ind_name, "中性")

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
            sector_type=stype,
            concept_tags=concept_ind_map.get(ind_name, []),
            rotation_line=_cfg.ROTATION_LINE_MAP.get(ind_name, ""),
        )

    # 概念→行业关联
    concept_ind_map = _match_concepts_to_industries(
        concepts, all_ind_names, name_df)

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

    # ── 5. 设置 sector_type 和 concept_tags ──
    concept_map = _match_concepts_to_industries(
        concepts, [r.name for r in merged_list], name_df)
    for r in gain_list + composite_list + merged_list:
        r.sector_type = _classify_industry(r.name)
        if r.name in concept_map:
            r.concept_tags = concept_map[r.name]

    # ── 5.5 P3-3: 板块节奏检测（Top-N 行业）──
    _enrich_rhythm(merged_list[:10])

    # ── 6. 超跌检测（盘中模式有 name_df 时）──
    oversold_list: List[IndustryRanking] = []
    if not is_historical and name_df is not None:
        try:
            oversold_list = get_oversold_industries(name_df)
        except Exception as e:
            _detail(f"  [!] 超跌检测失败（{e.__class__.__name__}）")

    # ── 7. 情绪统计（供 MarketContext.update_sentiment 使用）──
    _zt_total = sum(len(v) for v in zt_pool.values())
    _dt_total = sum(v for v in dt_count.values())
    _lianban_max = max(
        (max((x[2] for x in v), default=0) for v in zt_pool.values()),
        default=0,
    ) if zt_pool else 0
    sentiment_stats = {
        "zt_total": _zt_total,
        "dt_total": _dt_total,
        "lianban_max": _lianban_max,
        "name_df": name_df,
    }

    return gain_list, composite_list, merged_list, concepts, oversold_list, sentiment_stats


# ─────────────────────────────────────────────────────────
# 抄底候选板块筛选（恐慌时触发）
# ─────────────────────────────────────────────────────────

def get_bottom_fishing_candidates(
    name_df,
    oversold_list: List[IndustryRanking],
    panic_score: float,
    themes: List[str] = None,
    top_n: int = 5,
) -> List[IndustryRanking]:
    """
    恐慌行情中筛选抄底候选板块。

    选择逻辑:
    1. 从 name_df 取今日跌幅最大的 15 个行业
    2. 与历史超跌 oversold_list 取交集 → "双重超跌"
    3. 评分加权:
       - 今日跌幅分 (35): 跌得越多分越高
       - 历史超跌分 (30): 复用 oversold_score
       - 轮动线加分 (20): 进攻/科技型板块恐慌后弹性最大
       - 主题命中   (15): 命中用户关注主题
    4. 排序输出 top N

    :param name_df: 东财行业板块 DataFrame (含板块名+涨跌幅)
    :param oversold_list: L2 历史超跌列表
    :param panic_score: 盘中恐慌评分 (0-100)
    :param themes: 用户关注主题列表
    :param top_n: 输出数量
    :return: IndustryRanking 列表（按抄底得分降序）
    """
    if name_df is None or name_df.empty:
        return []

    if panic_score < 40:
        return []

    import config as _cfg

    # 找列名
    name_col = None
    for col in ['板块名称', '板块', '名称', '行业']:
        if col in name_df.columns:
            name_col = col
            break
    change_col = None
    for col in ['涨跌幅', '涨跌幅(%)', '涨幅', '涨幅(%)', '最新涨跌幅']:
        if col in name_df.columns:
            change_col = col
            break
    if not name_col or not change_col:
        return []

    # 1. 取今日跌幅最大的 15 个行业
    df = name_df[[name_col, change_col]].copy()
    df[change_col] = pd.to_numeric(df[change_col], errors='coerce')
    df = df.dropna(subset=[change_col])
    df = df.sort_values(change_col, ascending=True).head(15)

    # 历史超跌名单
    oversold_map = {r.name: r.oversold_score for r in oversold_list}

    # 主题关键词
    theme_keywords = []
    if themes:
        from signals.core.theme_tracker import THEME_KEYWORD_MAP
        for t in themes:
            kws = THEME_KEYWORD_MAP.get(t.upper().strip()) or THEME_KEYWORD_MAP.get(t.strip())
            if kws:
                theme_keywords.extend(kws)
            else:
                theme_keywords.append(t.strip())

    # 板块属性和轮动线
    sector_type_map = getattr(_cfg, "SECTOR_TYPE_MAP", {})
    rotation_map = getattr(_cfg, "ROTATION_LINE_MAP", {})

    candidates = []
    for _, row in df.iterrows():
        ind_name = str(row[name_col])
        change = float(row[change_col])

        # 今日跌幅分 (满分35): 跌幅>3%满分, 1~3%线性
        drop = abs(change) if change < 0 else 0
        if drop >= 3.0:
            decline_pts = 35.0
        elif drop >= 1.0:
            decline_pts = 35.0 * (drop - 1.0) / 2.0
        else:
            decline_pts = 0.0

        # 历史超跌分 (满分30): 直接取 oversold_score 归一化
        hist_score = oversold_map.get(ind_name, 0.0)
        oversold_pts = min(30.0, hist_score * 0.3)  # oversold_score 0-100 → 0-30

        # 轮动线/板块属性加分 (满分20): 进攻型+科技线弹性最大
        stype = sector_type_map.get(ind_name, "中性")
        rot_line = rotation_map.get(ind_name, "")
        attr_pts = 0.0
        if stype == "进攻":
            attr_pts += 12.0
        elif stype == "周期":
            attr_pts += 6.0
        if rot_line == "科技":
            attr_pts += 8.0
        elif rot_line == "新能源":
            attr_pts += 5.0
        attr_pts = min(20.0, attr_pts)

        # 主题命中 (满分15)
        theme_pts = 0.0
        if theme_keywords and any(kw in ind_name for kw in theme_keywords):
            theme_pts = 15.0

        total = decline_pts + oversold_pts + attr_pts + theme_pts

        if total > 0:
            r = IndustryRanking(
                name=ind_name,
                gain_pct=change,
                composite_score=round(total, 1),
                sector_type=stype,
                rotation_line=rot_line,
                oversold_score=hist_score,
                oversold_detail=f"今日{change:+.1f}%",
            )
            candidates.append(r)

    candidates.sort(key=lambda x: x.composite_score, reverse=True)
    return candidates[:top_n]
