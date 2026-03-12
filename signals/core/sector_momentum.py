# -*- coding: utf-8 -*-
"""
板块动量检测 (Sector Momentum)

扫描概念板块成分股的多维动量信号，预测板块级爆发/见顶。
使用自适应动量窗口（从最近 K 线往回找"动量起点"），避免固定窗口过拟合。

用法:
    from signals.core.sector_momentum import detect_sector_momentum, scan_hot_sectors
    signal = detect_sector_momentum("储能")
    hot = scan_hot_sectors(top_n=10)
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

_log = logging.getLogger(__name__)


@dataclass
class StockMomentum:
    """个股动量画像"""
    code: str = ""
    name: str = ""
    momentum_days: int = 0              # 动量区间天数（自适应）
    daily_avg_gain: float = 0.0         # 日均涨幅%
    bullish_ratio: float = 0.0          # 阳线占比
    volume_trend_slope: float = 0.0     # 量能斜率（>0递增）
    avg_body_ratio: float = 0.0         # 平均实体比
    volume_expanding: bool = False      # 量能递增
    momentum_score: float = 0.0         # 个股动量综合分 [0, 100]
    change_pct: float = 0.0            # 最新涨跌幅%


@dataclass
class SectorMomentumSignal:
    """板块动量信号"""
    concept_name: str = ""
    concept_code: str = ""
    total_stocks: int = 0
    momentum_stock_count: int = 0       # 动量分>=40的股票数
    momentum_ratio: float = 0.0         # momentum_stock_count / total
    avg_momentum: float = 0.0           # 平均个股动量分
    momentum_score: float = 0.0         # 板块动量综合分 [0, 100]
    signal_level: str = ""              # "强"/"中"/"弱"/""
    bearish_ratio: float = 0.0          # 阴线/下跌股票占比 (用于卖点预警)
    top_movers: List[StockMomentum] = field(default_factory=list)
    detail: str = ""


def _fetch_stock_daily(code: str, days: int = 15) -> List[Dict]:
    """
    获取个股近 N 日日线数据。
    返回 [{date, open, close, high, low, volume, change_pct}, ...]
    """
    import akshare as ak
    from signals.data.social_fetcher import no_proxy
    from signals.data.date_utils import recent_trade_date

    end = recent_trade_date()
    # 多拿几天以覆盖非交易日
    from datetime import timedelta
    start = (end - timedelta(days=days + 10)).strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    try:
        with no_proxy():
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start, end_date=end_str,
                adjust="qfq",
            )
        if df is None or df.empty:
            return []

        bars = []
        for _, row in df.iterrows():
            bars.append({
                "date": str(row["日期"]),
                "open": float(row["开盘"]),
                "close": float(row["收盘"]),
                "high": float(row["最高"]),
                "low": float(row["最低"]),
                "volume": float(row["成交量"]),
                "change_pct": float(row["涨跌幅"]),
            })
        return bars[-days:] if len(bars) > days else bars
    except Exception as e:
        _log.debug(f"获取 {code} 日线失败: {e}")
        return []


def _calc_stock_momentum(code: str, name: str, bars: List[Dict]) -> StockMomentum:
    """
    计算单股动量（自适应窗口）。
    从最新 bar 往回找"动量起点"（断裂 bar），动态确定动量区间。
    """
    sm = StockMomentum(code=code, name=name)
    if len(bars) < 2:
        return sm

    sm.change_pct = bars[-1].get("change_pct", 0.0)

    # ── 从最新 bar 往回找动量起点 ──
    momentum_start = 0
    for i in range(len(bars) - 1, 0, -1):
        b = bars[i]
        # 断裂条件: 阴线且跌幅>1% 或 大幅缩量(<前一根50%)
        is_bearish_break = b["close"] < b["open"] and b["change_pct"] < -1.0
        is_vol_collapse = (
            i > 0 and bars[i - 1]["volume"] > 0
            and b["volume"] < bars[i - 1]["volume"] * 0.5
        )
        if is_bearish_break or is_vol_collapse:
            momentum_start = i + 1
            break

    momentum_bars = bars[momentum_start:]
    if len(momentum_bars) < 1:
        return sm

    sm.momentum_days = len(momentum_bars)

    # ── 日均涨幅 ──
    if len(momentum_bars) >= 2:
        total_gain = (
            (momentum_bars[-1]["close"] - momentum_bars[0]["open"])
            / max(momentum_bars[0]["open"], 1e-9) * 100
        )
        sm.daily_avg_gain = round(total_gain / max(sm.momentum_days, 1), 3)
    elif len(momentum_bars) == 1:
        sm.daily_avg_gain = round(momentum_bars[0]["change_pct"], 3)

    # ── 阳线占比 ──
    bullish_count = sum(1 for b in momentum_bars if b["close"] > b["open"])
    sm.bullish_ratio = round(bullish_count / max(len(momentum_bars), 1), 3)

    # ── 量能斜率 (线性回归) ──
    if len(momentum_bars) >= 3:
        vols = [b["volume"] for b in momentum_bars]
        x = np.arange(len(vols))
        try:
            slope = np.polyfit(x, vols, 1)[0]
            avg_vol = np.mean(vols) if np.mean(vols) > 0 else 1
            sm.volume_trend_slope = round(slope / avg_vol, 4)
            sm.volume_expanding = sm.volume_trend_slope > 0.05
        except Exception:
            pass

    # ── 平均实体比 ──
    body_ratios = []
    for b in momentum_bars:
        hl = b["high"] - b["low"]
        if hl > 0:
            body_ratios.append(abs(b["close"] - b["open"]) / hl)
    sm.avg_body_ratio = round(np.mean(body_ratios) if body_ratios else 0, 3)

    # ── 个股动量分 (加权) ──
    # 日均涨幅 35% (归一化: [0, 3%] → [0, 100])
    gain_score = max(0, min(100, sm.daily_avg_gain / 3.0 * 100))
    # 阳线占比 25% ([0, 1] → [0, 100])
    bull_score = sm.bullish_ratio * 100
    # 量能斜率 20% (归一化: [-0.3, 0.3] → [0, 100])
    vol_score = max(0, min(100, (sm.volume_trend_slope + 0.3) / 0.6 * 100))
    # 动量天数 10% ([1, 7] → [14, 100])
    day_score = max(14, min(100, sm.momentum_days / 7 * 100))
    # 实体比 10% ([0, 1] → [0, 100])
    body_score = sm.avg_body_ratio * 100

    sm.momentum_score = round(
        gain_score * 0.35 + bull_score * 0.25 + vol_score * 0.20
        + day_score * 0.10 + body_score * 0.10,
        1,
    )

    return sm


def detect_sector_momentum(
    concept_name: str,
    top_n_stocks: int = 30,
    concept_code: str = "",
) -> SectorMomentumSignal:
    """
    检测单个概念板块的动量信号。

    1. 获取成分股 TOP N (按涨幅)
    2. 批量获取近 10 日日线
    3. 计算各股动量分
    4. 聚合为板块级信号

    :param concept_name: 概念名称
    :param top_n_stocks: 取前 N 只成分股
    :param concept_code: 概念代码(可选)
    :return: SectorMomentumSignal
    """
    from signals.data.social_fetcher import fetch_concept_stocks

    signal = SectorMomentumSignal(
        concept_name=concept_name,
        concept_code=concept_code,
    )

    # 获取成分股
    theme = fetch_concept_stocks(concept_name)
    if not theme.stocks:
        signal.detail = "无成分股数据"
        return signal

    # 取 TOP N (按涨跌幅降序)
    stocks = sorted(theme.stocks, key=lambda s: s.get("change_pct", 0), reverse=True)
    stocks = stocks[:top_n_stocks]
    signal.total_stocks = len(stocks)

    # 并发获取日线 + 计算动量
    momentums: List[StockMomentum] = []

    def _process_stock(s):
        code = s.get("code", "")
        name = s.get("name", "")
        bars = _fetch_stock_daily(code, days=15)
        if len(bars) >= 3:
            return _calc_stock_momentum(code, name, bars)
        return StockMomentum(code=code, name=name, change_pct=s.get("change_pct", 0))

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_process_stock, s): s for s in stocks}
        for fut in as_completed(futures):
            try:
                sm = fut.result(timeout=10)
                momentums.append(sm)
            except Exception as e:
                _log.debug(f"动量计算失败: {e}")

    if not momentums:
        signal.detail = "无有效动量数据"
        return signal

    # ── 聚合板块级指标 ──
    momentum_stocks = [m for m in momentums if m.momentum_score >= 40]
    signal.momentum_stock_count = len(momentum_stocks)
    signal.momentum_ratio = round(
        signal.momentum_stock_count / max(len(momentums), 1), 3
    )
    scores = [m.momentum_score for m in momentums]
    signal.avg_momentum = round(np.mean(scores), 1)

    # 阴线/下跌比例（卖点预警用）
    bearish = sum(1 for m in momentums if m.change_pct < -0.5)
    signal.bearish_ratio = round(bearish / max(len(momentums), 1), 3)

    # TOP5 movers
    momentums.sort(key=lambda m: m.momentum_score, reverse=True)
    signal.top_movers = momentums[:5]

    # ── 板块 momentum_score ──
    ratio_part = signal.momentum_ratio * 100 * 0.4
    avg_part = signal.avg_momentum * 0.3
    top_part = (momentums[0].momentum_score if momentums else 0) * 0.3
    signal.momentum_score = round(ratio_part + avg_part + top_part, 1)

    # 信号分级
    if signal.momentum_score >= 60:
        signal.signal_level = "强"
    elif signal.momentum_score >= 40:
        signal.signal_level = "中"
    elif signal.momentum_score >= 20:
        signal.signal_level = "弱"
    else:
        signal.signal_level = ""

    # 明细
    top_names = ", ".join(f"{m.name}({m.momentum_days}d)" for m in signal.top_movers[:3])
    signal.detail = (
        f"动量股{signal.momentum_stock_count}/{len(momentums)} "
        f"均分{signal.avg_momentum:.0f} "
        f"TOP: {top_names}"
    )

    return signal


def scan_hot_sectors(
    concept_names: Optional[List[str]] = None,
    top_n: int = 10,
) -> List[SectorMomentumSignal]:
    """
    并发扫描概念板块动量，返回 TOP N 信号。

    :param concept_names: 指定概念列表。为 None 时从概念排行取 TOP30。
    :param top_n: 返回前 N 个有信号的板块
    :return: 按 momentum_score 降序排列
    """
    if concept_names is None:
        concept_names = _get_top_concepts(30)

    if not concept_names:
        _log.warning("scan_hot_sectors: 无概念列表")
        return []

    _log.info(f"板块动量扫描: {len(concept_names)} 个概念")
    t0 = time.time()
    signals: List[SectorMomentumSignal] = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(detect_sector_momentum, name): name
            for name in concept_names
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                sig = fut.result(timeout=30)
                if sig.momentum_score > 0:
                    signals.append(sig)
            except Exception as e:
                _log.debug(f"板块 [{name}] 动量扫描失败: {e}")

    signals.sort(key=lambda s: s.momentum_score, reverse=True)
    elapsed = time.time() - t0
    _log.info(f"板块动量扫描完成: {len(signals)} 个有效信号, 耗时 {elapsed:.1f}s")

    return signals[:top_n]


def _get_top_concepts(n: int = 30) -> List[str]:
    """从概念排行或已缓存数据中取 TOP N 概念名称"""
    try:
        from signals.data.social_fetcher import fetch_concept_list
        df = fetch_concept_list()
        if df is not None and not df.empty:
            # 按涨跌幅排序取 TOP
            if "涨跌幅" in df.columns:
                df = df.sort_values("涨跌幅", ascending=False)
            return df["板块名称"].head(n).tolist()
    except Exception as e:
        _log.warning(f"获取概念排行失败: {e}")
    return []
