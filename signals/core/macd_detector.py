# -*- coding: utf-8 -*-
"""
MACD 信号检测 —— 绿柱极端状态筛查

两种模式：
  Pattern A（零上回踩）: DEA>0 + 绿柱扩大中 + 到达支撑位 → 回踩买点
  Pattern B（零下企稳）: DEA<0 + 绿柱从显著极值开始缩小 → 卖压衰竭企稳信号
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple


@dataclass
class MACDSignal:
    symbol: str
    freq: str           # "日线" / "周线"
    dt: datetime        # 信号触发日期
    pattern: str        # "A_零上回踩" / "B_零下企稳"
    confidence: float   # 0.0 ~ 1.0
    price: float        # 触发时收盘价
    dea: float
    dif: float
    hist: float         # 当根柱状图
    support_type: str = ""   # 支撑类型描述
    details: str = ""


def compute_macd(closes: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    """计算 MACD 指标，返回 DataFrame with dif/dea/hist columns."""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return pd.DataFrame({"dif": dif, "dea": dea, "hist": hist}, index=closes.index)


def detect_macd_signals(df: pd.DataFrame, symbol: str, freq: str,
                        lookback: int = 10) -> List[MACDSignal]:
    """
    在 OHLC DataFrame 上检测 MACD 信号。

    df 需包含: open, high, low, close 列，index 为 datetime。
    lookback: 回看窗口（检测最近 lookback 根K线内的信号）。

    返回去重后的信号：同一绿柱序列只保留置信度最高的一个。
    """
    if len(df) < 35:
        return []

    macd = compute_macd(df["close"])
    df = df.copy()
    df["dif"] = macd["dif"]
    df["dea"] = macd["dea"]
    df["hist"] = macd["hist"]

    raw_signals = []

    # 只检查最近 lookback 根已完成的K线（不含最后一根，防未来函数）
    end_idx = len(df) - 1
    start_idx = max(30, end_idx - lookback)

    for i in range(start_idx, end_idx):
        sig_a = _check_pattern_a(df, i, symbol, freq)
        if sig_a:
            raw_signals.append(sig_a)

        sig_b = _check_pattern_b(df, i, symbol, freq)
        if sig_b:
            raw_signals.append(sig_b)

    # 去重：同一 pattern 类型连续触发只保留最后一个（置信度通常最高）
    return _dedupe_signals(raw_signals)


def _dedupe_signals(signals: List[MACDSignal]) -> List[MACDSignal]:
    """同一 pattern 连续触发（间隔 ≤ 5天/周），只保留最后一个。"""
    if not signals:
        return []

    deduped = []
    prev = signals[0]

    for sig in signals[1:]:
        if sig.pattern == prev.pattern:
            # 同类型连续信号，保留更新的（通常置信度更高）
            prev = sig
        else:
            deduped.append(prev)
            prev = sig

    deduped.append(prev)
    return deduped


def _check_pattern_a(df: pd.DataFrame, idx: int, symbol: str, freq: str) -> Optional[MACDSignal]:
    """
    Pattern A: 零上回踩
    条件:
    1. DEA > 0
    2. 当前 histogram < 0（绿柱）
    3. 最近绿柱在扩大（变得更负）— 至少连续3根
    4. 价格到达支撑位（MA/前期低点/回撤位/前期平台）
    """
    row = df.iloc[idx]

    if row["dea"] <= 0:
        return None

    if row["hist"] >= 0:
        return None

    expand_count = _count_expanding_green(df, idx)
    if expand_count < 3:
        return None

    support_type, support_conf = _check_support(df, idx)
    if not support_type:
        return None

    confidence = 0.5
    confidence += min(expand_count - 3, 4) * 0.05
    confidence += support_conf
    confidence = min(confidence, 1.0)

    return MACDSignal(
        symbol=symbol, freq=freq, dt=df.index[idx],
        pattern="A_零上回踩",
        confidence=round(confidence, 2),
        price=row["close"],
        dea=round(row["dea"], 4),
        dif=round(row["dif"], 4),
        hist=round(row["hist"], 4),
        support_type=support_type,
        details=f"绿柱连续扩大{expand_count}根, {support_type}",
    )


def _check_pattern_b(df: pd.DataFrame, idx: int, symbol: str, freq: str) -> Optional[MACDSignal]:
    """
    Pattern B: 零下企稳
    条件:
    1. DEA < 0
    2. 当前 histogram < 0（绿柱）
    3. 绿柱从显著极值开始缩小 — 至少连续3根
    4. 额外条件（防假信号）:
       - 极值点的 |hist| 必须 > 收盘价的 1%（足够显著）
       - 当前 |hist| < 极值 |hist| 的 70%（缩小幅度够大）
       - 价格不再持续创新低（企稳证据）
    """
    row = df.iloc[idx]

    if row["dea"] >= 0:
        return None

    if row["hist"] >= 0:
        return None

    shrink_count, peak_hist, peak_bar_idx = _count_shrinking_green_with_peak(df, idx)
    if shrink_count < 6:  # 至少6根缩小（排除噪音）
        return None

    # 防假信号1：极值点必须足够显著
    price_ref = row["close"]
    if abs(peak_hist) < price_ref * 0.015:
        return None

    # 防假信号2：缩小幅度必须够大（当前 < 极值的60%）
    if abs(row["hist"]) > abs(peak_hist) * 0.60:
        return None

    # 防假信号3：极值点前必须有明显的扩大阶段（至少2根），
    # 确保是真正的"先扩后缩"，而非缓慢衰减
    if peak_bar_idx is not None:
        expand_before_peak = _count_expanding_green(df, peak_bar_idx)
        if expand_before_peak < 2:
            return None

    # 防假信号4：价格确认 — 当前价格不应远低于极值点价格
    if peak_bar_idx is not None and peak_bar_idx < len(df):
        peak_price = df.iloc[peak_bar_idx]["close"]
        price_drop = (row["close"] - peak_price) / peak_price * 100
        if price_drop < -20:
            return None

    confidence = 0.5
    confidence += min(shrink_count - 3, 4) * 0.05
    # 缩小幅度越大，置信度越高
    shrink_ratio = 1.0 - abs(row["hist"]) / abs(peak_hist)
    confidence += shrink_ratio * 0.2

    support_type, support_conf = _check_support(df, idx)
    if support_type:
        confidence += support_conf
    confidence = min(confidence, 1.0)

    return MACDSignal(
        symbol=symbol, freq=freq, dt=df.index[idx],
        pattern="B_零下企稳",
        confidence=round(confidence, 2),
        price=row["close"],
        dea=round(row["dea"], 4),
        dif=round(row["dif"], 4),
        hist=round(row["hist"], 4),
        support_type=support_type or "",
        details=(f"绿柱缩小{shrink_count}根(极值{peak_hist:.3f}→{row['hist']:.3f}, "
                 f"缩{shrink_ratio:.0%})"
                 + (f", {support_type}" if support_type else "")),
    )


def _count_expanding_green(df: pd.DataFrame, idx: int) -> int:
    """
    从 idx 往回数，连续多少根绿柱在扩大（变得更负）。
    允许1根小幅反复。
    """
    count = 0
    tolerance_used = False
    for i in range(idx, max(idx - 15, 0), -1):
        if i == 0:
            break
        curr_hist = df.iloc[i]["hist"]
        prev_hist = df.iloc[i - 1]["hist"]

        if curr_hist >= 0 or prev_hist >= 0:
            break

        if curr_hist <= prev_hist:
            count += 1
        else:
            if not tolerance_used and (curr_hist - prev_hist) < abs(prev_hist) * 0.3:
                tolerance_used = True
                count += 1
            else:
                break

    return count


def _count_shrinking_green_with_peak(df: pd.DataFrame, idx: int) -> Tuple[int, float, Optional[int]]:
    """
    从 idx 往回找绿柱缩小序列。
    允许短暂翻红（≤1根）后继续计入绿柱链（底部横盘常见）。
    返回 (缩小根数, 极值点hist, 极值点在df中的index)。
    """
    # 先收集最近的负柱序列（允许1根正值穿插）
    neg_bars = []  # [(df_idx, hist_val)]
    pos_skip = 0
    max_pos_skips = 1

    for i in range(idx, max(idx - 30, 0), -1):
        h = df.iloc[i]["hist"]
        if h < 0:
            neg_bars.append((i, h))
        elif pos_skip < max_pos_skips:
            pos_skip += 1
            continue
        else:
            break

    if len(neg_bars) < 3:
        return 0, 0.0, None

    # 找极值点
    peak_entry = min(neg_bars, key=lambda b: b[1])
    peak_hist = peak_entry[1]
    peak_df_idx = peak_entry[0]
    peak_idx_in_list = neg_bars.index(peak_entry)

    if peak_idx_in_list == 0:
        return 0, peak_hist, peak_df_idx

    shrink_count = 0
    tolerance_used = False
    for j in range(peak_idx_in_list):
        curr_abs = abs(neg_bars[j][1])
        next_abs = abs(neg_bars[j + 1][1])
        if curr_abs <= next_abs:
            shrink_count += 1
        else:
            if not tolerance_used and (curr_abs - next_abs) < next_abs * 0.3:
                tolerance_used = True
                shrink_count += 1
            else:
                break

    return shrink_count, peak_hist, peak_df_idx


def _check_support(df: pd.DataFrame, idx: int) -> Tuple[str, float]:
    """
    检测当前价格是否到达支撑位。
    返回 (支撑类型描述, 置信度加分)。
    只使用 idx 及之前的数据，避免未来函数。

    支撑来源:
    1. MA 均线支撑（MA20/60/120/250）
    2. 前期摆动低点支撑（60根内显著低点）
    3. Fibonacci 回撤位支撑（38.2%/50%/61.8%）
    4. 前期横盘平台支撑（密集成交区）
    """
    if idx < 30:
        return ("", 0.0)

    close = df.iloc[idx]["close"]
    low = df.iloc[idx]["low"]
    closes_before = df["close"].iloc[:idx + 1]

    supports_found = []

    # ── 1. MA 均线支撑 ──
    for period, name in [(20, "MA20"), (60, "MA60"), (120, "MA120"), (250, "MA250")]:
        if len(closes_before) < period:
            continue
        ma_val = closes_before.iloc[-period:].mean()
        distance_pct = (close - ma_val) / ma_val * 100
        if -5 <= distance_pct <= 3:  # 在MA附近（下方5%到上方3%）
            supports_found.append((name, abs(distance_pct), 0.1))

    # ── 2. 前期摆动低点支撑 ──
    swing_lows = _find_swing_lows(df, idx, lookback=60)
    for swing_idx, swing_price in swing_lows:
        dist_pct = (low - swing_price) / swing_price * 100
        if -3 <= dist_pct <= 3:
            days_ago = idx - swing_idx
            supports_found.append((f"前低({days_ago}根前)", abs(dist_pct), 0.15))
            break  # 只取最近一个

    # ── 3. Fibonacci 回撤位 ──
    fib_support = _check_fibonacci_support(df, idx)
    if fib_support:
        supports_found.append(fib_support)

    # ── 4. 前期横盘平台 ──
    platform = _find_platform_support(df, idx, lookback=80)
    if platform:
        supports_found.append(platform)

    if not supports_found:
        return ("", 0.0)

    # 汇总
    if len(supports_found) >= 3:
        names = "+".join(s[0] for s in supports_found[:3])
        return (f"多重支撑({names})", 0.3)
    elif len(supports_found) >= 2:
        names = "+".join(s[0] for s in supports_found[:2])
        return (f"双重支撑({names})", 0.2)
    else:
        return (supports_found[0][0], supports_found[0][2])


def _find_swing_lows(df: pd.DataFrame, idx: int, lookback: int = 60) -> List[Tuple[int, float]]:
    """
    在 idx 之前 lookback 根内寻找摆动低点（局部极小值）。
    摆动低点定义：某根K线的 low 低于前后各2根的 low。
    """
    swing_lows = []
    start = max(2, idx - lookback)
    end = idx - 2  # 需要后面2根来确认

    for i in range(start, end):
        low_i = df.iloc[i]["low"]
        is_swing_low = True
        for offset in [-2, -1, 1, 2]:
            if df.iloc[i + offset]["low"] < low_i:
                is_swing_low = False
                break
        if is_swing_low:
            swing_lows.append((i, low_i))

    return swing_lows


def _check_fibonacci_support(df: pd.DataFrame, idx: int) -> Optional[Tuple[str, float, float]]:
    """
    检查当前价格是否在前一波上涨的 Fibonacci 回撤位附近。
    找最近的显著上涨波段（从摆动低点到摆动高点），计算回撤比例。
    """
    if idx < 30:
        return None

    # 找最近的高点（前30根内最高价）
    recent_highs = df["high"].iloc[max(0, idx - 30):idx]
    if recent_highs.empty:
        return None
    peak_idx_relative = recent_highs.idxmax()
    peak_price = recent_highs.max()

    # 找高点之前的低点（再往前30根的最低价）
    peak_abs_idx = df.index.get_loc(peak_idx_relative)
    prior_lows = df["low"].iloc[max(0, peak_abs_idx - 40):peak_abs_idx]
    if prior_lows.empty:
        return None
    trough_price = prior_lows.min()

    # 波幅太小则忽略
    wave_pct = (peak_price - trough_price) / trough_price * 100
    if wave_pct < 15:  # 涨幅至少15%才有意义
        return None

    close = df.iloc[idx]["close"]
    retracement = (peak_price - close) / (peak_price - trough_price)

    fib_levels = {0.382: "Fib38.2%", 0.5: "Fib50%", 0.618: "Fib61.8%"}
    for level, name in fib_levels.items():
        if abs(retracement - level) < 0.05:  # 5%容差
            return (name, abs(retracement - level) * 100, 0.15)

    return None


def _find_platform_support(df: pd.DataFrame, idx: int,
                           lookback: int = 80) -> Optional[Tuple[str, float, float]]:
    """
    寻找前期横盘平台支撑。
    方法：在历史区间内找价格密集区（K线实体集中的价格带），
    检查当前价格是否接近该密集区。
    """
    if idx < lookback:
        return None

    close = df.iloc[idx]["close"]
    # 取历史收盘价（排除最近回调阶段）
    hist_closes = df["close"].iloc[max(0, idx - lookback):max(0, idx - 10)]
    if len(hist_closes) < 20:
        return None

    # 用直方图找密集区
    price_min, price_max = hist_closes.min(), hist_closes.max()
    if price_max == price_min:
        return None

    n_bins = 20
    counts, bin_edges = np.histogram(hist_closes, bins=n_bins)

    # 找最密集的 bin
    max_bin_idx = counts.argmax()
    if counts[max_bin_idx] < len(hist_closes) * 0.15:
        return None  # 不够密集

    platform_low = bin_edges[max_bin_idx]
    platform_high = bin_edges[max_bin_idx + 1]
    platform_mid = (platform_low + platform_high) / 2

    dist_pct = abs(close - platform_mid) / platform_mid * 100
    if dist_pct < 5:
        return (f"平台支撑({platform_mid:.1f})", dist_pct, 0.15)

    return None


# ─────────────────────────────────────────────────────
# 便捷函数：从 RawBar 列表直接检测
# ─────────────────────────────────────────────────────
def detect_from_raw_bars(bars, symbol: str, freq: str, lookback: int = 10) -> List[MACDSignal]:
    """从 czsc.RawBar 列表直接检测 MACD 信号。"""
    if not bars or len(bars) < 35:
        return []

    records = []
    for bar in bars:
        records.append({
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "vol": bar.vol,
        })
    df = pd.DataFrame(records)
    df.index = pd.DatetimeIndex([bar.dt for bar in bars])

    return detect_macd_signals(df, symbol, freq, lookback=lookback)
