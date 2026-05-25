# -*- coding: utf-8 -*-
"""
信号融合过滤器 — 观点驱动的宽松确认

原则: "模糊的正确，精确的错误"
- 信号本质: 均线(价格在哪) + 量价(资金在做什么)
- 不要求全部条件满足，1-2 个维度支持即推荐
- 用户观点是最高权重信号，系统做辅助确认

两种模式:
  belief — 信念方向(找右侧买点)
  panic  — 恐慌抄底(找超跌反弹)

用法:
    from signals.core.signal_filter import scan_direction
    results = scan_direction("上证50", mode="belief")
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    symbol: str
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0

    # 三个维度
    ma_status: str = ""          # "MA21支撑(+1.2%)" / "多头排列" / ""
    ma_near_support: bool = False
    czsc_signals: List[str] = field(default_factory=list)  # ["二买(conf=75%)"]
    has_buy_signal: bool = False
    volume_status: str = ""      # "放量(2.1σ)" / "缩量" / "正常"
    volume_confirmed: bool = False

    # 恐慌模式专用
    cap_score: int = 0           # 割肉指标 0-100
    oversold: bool = False

    # 综合
    confirmations: int = 0       # 确认维度数
    grade: str = "C"             # A/B/C
    action: str = "等待"         # "推荐关注" / "可观察" / "等待"
    target_price: float = 0.0    # panic模式: 建议兑现价


def _get_index_constituents(index_name: str) -> List[str]:
    """获取指数成分股（Futu格式）"""
    try:
        import akshare as ak
        from config import INDEX_AK_CODES

        ak_code = INDEX_AK_CODES.get(index_name)
        if ak_code:
            code = ak_code.replace("sh", "").replace("sz", "")
            df = ak.index_stock_cons(symbol=code)
            codes = df["品种代码"].tolist()
            return [f"SH.{c}" if c.startswith("6") else f"SZ.{c}" for c in codes]
    except Exception as e:
        logger.warning("获取%s成分股失败(指数): %s", index_name, e)

    # 尝试作为行业名
    try:
        from signals.layers.industry import get_industry_stocks
        stocks = get_industry_stocks(index_name)
        if stocks:
            return stocks
    except Exception as e:
        logger.warning("获取%s成分股失败(行业): %s", index_name, e)

    return []


def _analyze_one(symbol: str, bars, mode: str = "belief") -> ScanResult:
    """分析单只股票: MA + 缠论 + 量能三维度"""
    from czsc import Freq
    from signals.core.analyzer import SymbolAnalyzer
    from signals.core.detectors import detect_all_signals
    from signals.core.ma_levels import compute_ma_levels
    from signals.core.anomaly import compute_anomaly_profile

    r = ScanResult(symbol=symbol)

    if not bars or len(bars) < 60:
        r.action = "数据不足"
        return r

    latest = bars[-1]
    prev = bars[-2] if len(bars) > 1 else latest
    r.price = latest.close
    r.change_pct = round((latest.close - prev.close) / prev.close * 100, 2)

    # ── 维度1: MA 位置 ──
    ma_ctx = compute_ma_levels(bars, symbol)
    if ma_ctx:
        r.ma_status = ma_ctx.trend_summary
        for lvl in ma_ctx.support_levels:
            if abs(lvl.distance_pct) <= 5.0:
                r.ma_status = f"{lvl.name}支撑({lvl.distance_pct:+.1f}%)"
                r.ma_near_support = True
                r.confirmations += 1
                break
        if not r.ma_near_support and ma_ctx.key_levels:
            kl = ma_ctx.key_levels[0]
            r.ma_status = f"{kl.name}({kl.distance_pct:+.1f}%)"

    # ── 维度2: 缠论信号 ──
    try:
        analyzer = SymbolAnalyzer(symbol, Freq.D, bars)
        signals = detect_all_signals(analyzer.czsc, symbol)
        buy_sigs = [s for s in signals if "买" in s.signal_type]
        if buy_sigs:
            latest_buy = buy_sigs[-1]
            r.czsc_signals.append(f"{latest_buy.signal_type}(conf={latest_buy.confidence:.0%})")
            r.has_buy_signal = True
            r.confirmations += 1
        sell_sigs = [s for s in signals if "卖" in s.signal_type]
        if sell_sigs:
            r.czsc_signals.append(f"{sell_sigs[-1].signal_type}")
    except Exception as e:
        logger.debug("CZSC分析异常 %s: %s", symbol, e)

    # ── 维度3: 量能 ──
    anomaly = compute_anomaly_profile(symbol, bars)
    if anomaly:
        r.cap_score = int(anomaly.capitulation_score)
        vol_item = anomaly.items.get("volume")
        if vol_item and vol_item.z_score is not None:
            if vol_item.z_score >= 2.0:
                r.volume_status = f"放量({vol_item.z_score:.1f}σ)"
                r.volume_confirmed = True
                r.confirmations += 1
            elif vol_item.z_score <= -1.5:
                r.volume_status = f"缩量({vol_item.z_score:.1f}σ)"
            else:
                r.volume_status = "正常"
        else:
            r.volume_status = "正常"

        # panic模式: 割肉指标高 = 额外确认
        if mode == "panic" and anomaly.capitulation_score >= 60:
            r.oversold = True
            r.confirmations += 1
            r.target_price = round(r.price * 1.08, 2)  # 反弹8%兑现
    else:
        r.volume_status = "无数据"

    # ── 分级 ──
    if r.confirmations >= 2:
        r.grade = "A"
        r.action = "推荐关注"
    elif r.confirmations >= 1:
        r.grade = "B"
        r.action = "可观察"
    else:
        r.grade = "C"
        r.action = "等待"

    return r


def scan_direction(direction: str, mode: str = "belief",
                   codes: List[str] = None, top_n: int = 50) -> List[ScanResult]:
    """
    扫描一个方向的成分股信号。

    :param direction: 方向名（如"上证50"、"半导体"）
    :param mode: "belief" / "panic"
    :param codes: 直接指定代码列表
    :param top_n: 最多扫描前N只
    :return: ScanResult列表，按确认维度排序
    """
    from signals.data.fetcher import AKShareSource

    if codes:
        symbols = codes
    else:
        symbols = _get_index_constituents(direction)
        if not symbols:
            logger.error("无法获取 %s 的成分股", direction)
            return []

    symbols = symbols[:top_n]
    logger.info("方向扫描: %s (%d只), 模式=%s", direction or "自定义", len(symbols), mode)

    ak_source = AKShareSource()
    edt = datetime.now().strftime("%Y%m%d")
    sdt = (datetime.now() - timedelta(days=300)).strftime("%Y%m%d")

    results = []
    for sym in symbols:
        try:
            bars = ak_source.get_a_daily(sym, sdt, edt)
            if not bars:
                continue
            r = _analyze_one(sym, bars, mode)
            results.append(r)
        except Exception as e:
            logger.debug("跳过 %s: %s", sym, e)

    results.sort(key=lambda x: (-x.confirmations, -x.change_pct))
    return results


def results_to_dict(results: List[ScanResult]) -> List[dict]:
    """ScanResult 列表转 dict（用于 API 返回）"""
    return [
        {
            "symbol": r.symbol,
            "name": r.name,
            "price": r.price,
            "change_pct": r.change_pct,
            "ma_status": r.ma_status,
            "ma_near_support": r.ma_near_support,
            "czsc_signals": r.czsc_signals,
            "has_buy_signal": r.has_buy_signal,
            "volume_status": r.volume_status,
            "volume_confirmed": r.volume_confirmed,
            "cap_score": r.cap_score,
            "oversold": r.oversold,
            "confirmations": r.confirmations,
            "grade": r.grade,
            "action": r.action,
            "target_price": r.target_price,
        }
        for r in results
    ]
