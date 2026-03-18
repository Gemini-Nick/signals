# -*- coding: utf-8 -*-
"""
跳空缺口信号检测 —— 独立于缠论的价格行为信号

4 种缺口类型：
  突破缺口 (breakaway)  — 盘整区突破，放量确认 → 强信号
  持续缺口 (runaway)    — 趋势中途，量能放大 → 趋势延续
  衰竭缺口 (exhaustion) — 趋势末端，量能萎缩 → 反转警告
  普通缺口 (common)     — 不符合以上条件 → 弱信号

用法:
    from signals.core.gap_detector import detect_gap_signals
    signals = detect_gap_signals(bars, "SZ.300750", "日线")
"""
from typing import List, Optional, Tuple

from .detectors import SignalEvent


def detect_gap_signals(
    bars: list,
    symbol: str,
    freq_str: str,
    gap_threshold_pct: float = 2.0,
    max_gap_pct: float = 9.5,
    consolidation_lookback: int = 20,
    vol_confirm_ratio: float = 1.5,
) -> List[SignalEvent]:
    """扫描 K 线数据，检测跳空缺口并生成买卖信号。

    Args:
        bars: RawBar 列表（需 >= consolidation_lookback + 5 根）
        symbol: 标的代码
        freq_str: 级别字符串，如 "日线" / "30分钟"
        gap_threshold_pct: 最小缺口幅度 %（默认 2.0）
        max_gap_pct: 最大缺口幅度 %，排除涨跌停（默认 9.5）
        consolidation_lookback: 盘整检测回看根数（默认 20）
        vol_confirm_ratio: 放量确认倍数（默认 1.5）

    Returns:
        List[SignalEvent]
    """
    if not bars or len(bars) < consolidation_lookback + 5:
        return []

    # 只检测最近 5 根 K 线的缺口（更早的不可操作）
    scan_start = max(1, len(bars) - 5)
    gaps = _find_gaps(bars, scan_start, gap_threshold_pct, max_gap_pct)

    signals: List[SignalEvent] = []
    for gap in gaps:
        gap_type, confidence, detail = _classify_gap(
            bars, gap, consolidation_lookback, vol_confirm_ratio
        )

        # 已回补的缺口降级为普通
        filled, fill_bars = _check_gap_fill(bars, gap)
        if filled and gap_type != "普通":
            gap_type = "普通"
            confidence = 0.35
            detail += f" [已回补({fill_bars}根)]"

        # 映射信号类型
        signal_type = _map_signal_type(gap["direction"], gap_type)
        vol_ratio = _calc_volume_ratio(bars, gap["index"])

        signals.append(SignalEvent(
            symbol=symbol,
            freq=freq_str,
            dt=gap["bar"].dt,
            signal_type=signal_type,
            confidence=confidence,
            price=gap["bar"].close,
            details=(
                f"缺口{gap['gap_pct']:.1f}% "
                f"{'↑' if gap['direction'] == 'up' else '↓'} "
                f"类型={gap_type} 量比={vol_ratio:.1f} {detail}"
            ),
        ))
    return signals


# ── 内部函数 ──────────────────────────────────────────────


def _find_gaps(
    bars: list, scan_start: int, threshold_pct: float, max_pct: float
) -> List[dict]:
    """扫描 bars[scan_start:] 中的真实缺口。

    真实缺口定义：价格空间存在空白区域
      向上缺口: bar.low > prev.high  (当日最低 > 前日最高)
      向下缺口: bar.high < prev.low  (当日最高 < 前日最低)
    """
    results = []
    for i in range(scan_start, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]
        if prev.high <= 0 or prev.low <= 0:
            continue

        if bar.low > prev.high:
            # 向上缺口
            gap_pct = (bar.low - prev.high) / prev.close * 100
            if threshold_pct <= gap_pct <= max_pct:
                results.append({
                    "index": i,
                    "direction": "up",
                    "gap_pct": gap_pct,
                    "gap_top": bar.low,
                    "gap_bottom": prev.high,
                    "bar": bar,
                    "prev_bar": prev,
                })
        elif bar.high < prev.low:
            # 向下缺口
            gap_pct = (prev.low - bar.high) / prev.close * 100
            if threshold_pct <= gap_pct <= max_pct:
                results.append({
                    "index": i,
                    "direction": "down",
                    "gap_pct": gap_pct,
                    "gap_top": prev.low,
                    "gap_bottom": bar.high,
                    "bar": bar,
                    "prev_bar": prev,
                })
    return results


def _classify_gap(
    bars: list, gap: dict, lookback: int, vol_confirm_ratio: float
) -> Tuple[str, float, str]:
    """分类缺口类型，返回 (类型, 置信度, 细节描述)。"""
    idx = gap["index"]
    direction = gap["direction"]
    vol_ratio = _calc_volume_ratio(bars, idx)

    is_consol = _is_consolidating(bars, idx - 1, lookback)
    trend = _detect_trend_direction(bars, idx - 1, lookback)

    # 1. 突破缺口: 盘整后跳空突破 + 放量
    if is_consol:
        has_vol = vol_ratio >= vol_confirm_ratio
        conf = 0.80 if has_vol else 0.65
        detail = f"盘整突破{'放量' if has_vol else '量能一般'}"
        return "突破", conf, detail

    # 2&3. 趋势中的缺口: 区分持续 vs 衰竭
    trend_aligned = (
        (direction == "up" and trend == "up")
        or (direction == "down" and trend == "down")
    )

    if trend_aligned:
        # 衰竭缺口: 趋势方向一致但量能萎缩
        if vol_ratio < 0.8:
            detail = f"趋势末端量缩(量比{vol_ratio:.1f})"
            return "衰竭", 0.65, detail
        # 持续缺口: 趋势方向一致且量能正常或放大
        detail = f"趋势延续{'放量' if vol_ratio >= vol_confirm_ratio else ''}"
        conf = 0.70 if vol_ratio >= vol_confirm_ratio else 0.55
        return "持续", conf, detail

    # 4. 普通缺口
    return "普通", 0.40, "方向与趋势不一致或无明确趋势"


def _check_gap_fill(bars: list, gap: dict, max_check: int = 10) -> Tuple[bool, int]:
    """检查缺口是否被后续 K 线回补。

    Returns:
        (已回补, 回补用了几根K线)
    """
    idx = gap["index"]
    direction = gap["direction"]
    gap_bottom = gap["gap_bottom"]
    gap_top = gap["gap_top"]

    end = min(idx + 1 + max_check, len(bars))
    for k in range(idx + 1, end):
        if direction == "up" and bars[k].low <= gap_bottom:
            return True, k - idx
        if direction == "down" and bars[k].high >= gap_top:
            return True, k - idx
    return False, 0


def _calc_volume_ratio(bars: list, index: int, period: int = 5) -> float:
    """计算 bars[index] 成交量 / 前 period 日均量。"""
    if index < period or bars[index].vol <= 0:
        return 0.0
    avg_vol = sum(b.vol for b in bars[index - period : index]) / period
    if avg_vol <= 0:
        return 0.0
    return bars[index].vol / avg_vol


def _is_consolidating(
    bars: list, end_idx: int, lookback: int, max_range_pct: float = 10.0
) -> bool:
    """判断 bars[end_idx-lookback+1 : end_idx+1] 是否处于盘整。

    阈值 10%：真正的盘整区间振幅通常 <10%，
    超过 10% 大概率已有趋势（尤其是单边上涨/下跌）。
    """
    start = max(0, end_idx - lookback + 1)
    if start >= end_idx:
        return False
    highs = [b.high for b in bars[start : end_idx + 1]]
    lows = [b.low for b in bars[start : end_idx + 1]]
    range_high = max(highs)
    range_low = min(lows)
    if range_low <= 0:
        return False
    range_pct = (range_high - range_low) / range_low * 100
    return range_pct < max_range_pct


def _detect_trend_direction(bars: list, end_idx: int, lookback: int) -> str:
    """判断趋势方向: "up" / "down" / "flat"。"""
    start = max(0, end_idx - lookback + 1)
    if start >= end_idx:
        return "flat"
    c_start = bars[start].close
    c_end = bars[end_idx].close
    if c_start <= 0:
        return "flat"
    change_pct = (c_end - c_start) / c_start * 100
    if change_pct > 3.0:
        return "up"
    if change_pct < -3.0:
        return "down"
    return "flat"


def _map_signal_type(direction: str, gap_type: str) -> str:
    """将缺口方向 + 类型映射为信号类型字符串。

    衰竭缺口是反转信号 → 方向取反:
      向上衰竭 → 缺口卖:衰竭（涨势衰竭，看空）
      向下衰竭 → 缺口买:衰竭（跌势衰竭，看多）
    """
    if gap_type == "衰竭":
        # 反转信号：方向取反
        return "缺口买:衰竭" if direction == "down" else "缺口卖:衰竭"

    if direction == "up":
        return f"缺口买:{gap_type}"
    return f"缺口卖:{gap_type}"
