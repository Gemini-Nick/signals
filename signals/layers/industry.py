# -*- coding: utf-8 -*-
"""
行业工具：
- 获取行业列表 + 成分股（AKShare）
- 行业强度研判（两级降级方案）
  方法 A：行业板块 CZSC（东财接口，间歇性超时 → 降级到方法 B）
  方法 B：成分股聚合评分（始终可用）
"""
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd


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

    df = ak.stock_board_industry_cons_em(symbol=industry)
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
        return self.avg_score > 0 or self.buy_ratio > 0.3

    @property
    def summary(self) -> str:
        if self.method == "czsc":
            sig = f" | {self.latest_signal}" if self.latest_signal != "无" else ""
            return f"{self.name} [{self.trend}{sig}]（CZSC方法）"
        elif self.method == "members":
            return (f"{self.name} 平均分={self.avg_score:.1f} "
                    f"买信号占比={self.buy_ratio:.0%}（成分股聚合）")
        return f"{self.name} 数据不可用"


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
