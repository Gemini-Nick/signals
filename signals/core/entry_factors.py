# -*- coding: utf-8 -*-
"""
入场因子检测 — Phase 2: Gap / Trend Breakout / Volatility Contraction

每个因子返回与 MACD/CZSC 信号相同格式的信号列表:
  {dt, date_str, type, group, price, confidence, details}
"""
from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _robust_volume_ratio(vols: np.ndarray, index: int, period: int) -> float:
    current = float(vols[index]) if index < len(vols) else 0.0
    if current <= 0:
        return 0.0
    start = max(0, index - period)
    baseline_values = [float(value) for value in vols[start:index] if float(value) > 0]
    if not baseline_values:
        return 0.0
    comparable = [
        value for value in baseline_values
        if 0.05 <= value / current <= 20.0
    ]
    if len(comparable) >= min(3, len(baseline_values)):
        baseline_values = comparable
    baseline = float(np.mean(baseline_values)) if baseline_values else 0.0
    return current / baseline if baseline > 0 else 0.0


def detect_gap_entries(
    df: pd.DataFrame,
    gap_pct_min: float = 2.0,
    volume_ratio_min: float = 1.5,
    lookback: int = 999,
) -> list[dict]:
    """
    跳空缺口入场因子:
    - 开盘价相对前收盘跳空 ≥ gap_pct_min%
    - 当日量比 ≥ volume_ratio_min (相对 20 日均量)
    - 收阳线确认 (close > open)
    """
    if len(df) < 22:
        return []

    signals = []
    vol_col = "vol" if "vol" in df.columns else "volume"
    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    vols = df[vol_col].values.astype(float)

    # 20 日均量
    vol_ma = pd.Series(vols).rolling(20).mean().values

    start = max(21, len(df) - lookback)
    for i in range(start, len(df)):
        prev_close = closes[i - 1]
        if prev_close <= 0:
            continue

        gap_pct = (opens[i] - prev_close) / prev_close * 100
        if gap_pct < gap_pct_min:
            continue

        # 量比
        if vol_ma[i] > 0 and vols[i] / vol_ma[i] < volume_ratio_min:
            continue

        # 收阳线确认
        if closes[i] <= opens[i]:
            continue

        dt_idx = df.index[i]
        confidence = min(gap_pct / 5.0, 1.0)  # 跳空越大置信度越高

        signals.append({
            "dt": int(pd.Timestamp(dt_idx).timestamp()),
            "date_str": dt_idx.strftime("%Y-%m-%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10],
            "type": f"Gap_{gap_pct:.1f}%",
            "group": "gap",
            "price": round(float(closes[i]), 4),
            "confidence": round(confidence, 2),
            "details": f"跳空{gap_pct:.1f}%, 量比{vols[i]/vol_ma[i]:.1f}" if vol_ma[i] > 0 else f"跳空{gap_pct:.1f}%",
        })

    return signals


def detect_trend_breakout_entries(
    df: pd.DataFrame,
    lookback_days: int = 20,
    volume_ratio_min: float = 1.3,
    lookback: int = 999,
) -> list[dict]:
    """
    趋势突破入场因子:
    - 收盘价突破 N 日最高价
    - 当日量比 ≥ volume_ratio_min
    """
    if len(df) < lookback_days + 5:
        return []

    signals = []
    vol_col = "vol" if "vol" in df.columns else "volume"
    closes = df["close"].values
    highs = df["high"].values
    vols = df[vol_col].values.astype(float)
    vol_ma = pd.Series(vols).rolling(20).mean().values

    start = max(lookback_days + 1, len(df) - lookback)
    for i in range(start, len(df)):
        # N 日最高价 (不含当日)
        period_high = np.max(highs[i - lookback_days:i])

        if closes[i] <= period_high:
            continue

        # 量比
        if vol_ma[i] > 0 and vols[i] / vol_ma[i] < volume_ratio_min:
            continue

        dt_idx = df.index[i]
        breakout_pct = (closes[i] - period_high) / period_high * 100
        confidence = min(breakout_pct / 3.0, 1.0)

        signals.append({
            "dt": int(pd.Timestamp(dt_idx).timestamp()),
            "date_str": dt_idx.strftime("%Y-%m-%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10],
            "type": f"Breakout_{lookback_days}D",
            "group": "trend",
            "price": round(float(closes[i]), 4),
            "confidence": round(confidence, 2),
            "details": f"突破{lookback_days}日高{period_high:.2f}, +{breakout_pct:.1f}%",
        })

    return signals


def detect_200d_new_high_entries(
    df: pd.DataFrame,
    lookback_days: int = 199,
    volume_ratio_min: float = 0.0,
    volume_ma_days: int = 20,
    five_day_window: int = 5,
    lookback: int = 999,
) -> list[dict]:
    """
    200 日新高突破入场因子:
    - 当日最高价严格突破此前 lookback_days 个交易日最高价
    - 记录过去 five_day_window 日涨幅与量比，默认不强制过滤量比
    """
    min_len = max(lookback_days + 1, volume_ma_days + 1, five_day_window + 1)
    if len(df) < min_len:
        return []

    signals = []
    vol_col = "vol" if "vol" in df.columns else "volume"
    closes = df["close"].values.astype(float)
    highs = df["high"].values.astype(float)
    vols = df[vol_col].values.astype(float) if vol_col in df.columns else np.zeros(len(df), dtype=float)
    start = max(lookback_days, len(df) - lookback)
    for i in range(start, len(df)):
        previous_high = np.max(highs[i - lookback_days:i])
        today_high = highs[i]
        if previous_high <= 0 or today_high <= previous_high:
            continue

        volume_ratio = _robust_volume_ratio(vols, i, volume_ma_days)
        if volume_ratio_min > 0 and volume_ratio < volume_ratio_min:
            continue

        close_n_days_ago = closes[i - five_day_window] if i >= five_day_window else 0.0
        five_day_gain_pct = (
            (closes[i] - close_n_days_ago) / close_n_days_ago * 100
            if close_n_days_ago > 0 else 0.0
        )
        breakout_pct = (today_high - previous_high) / previous_high * 100
        confidence = 0.45
        confidence += min(max(breakout_pct, 0.0) / 10.0, 0.25)
        confidence += min(max(five_day_gain_pct, 0.0) / 50.0, 0.20)
        confidence += min(max(volume_ratio - 1.0, 0.0) / 5.0, 0.10)

        dt_idx = df.index[i]
        signals.append({
            "dt": int(pd.Timestamp(dt_idx).timestamp()),
            "date_str": dt_idx.strftime("%Y-%m-%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10],
            "type": "200日新高突破",
            "group": "200d_new_high_breakout",
            "price": round(float(closes[i]), 4),
            "confidence": round(min(confidence, 1.0), 2),
            "details": (
                f"200日新高, 最高{today_high:.2f} > 前高{previous_high:.2f}, "
                f"突破{breakout_pct:.1f}%, 5日涨幅{five_day_gain_pct:.1f}%, "
                f"量比{volume_ratio:.2f}"
            ),
            "today_high": round(float(today_high), 4),
            "previous_high": round(float(previous_high), 4),
            "breakout_pct": round(float(breakout_pct), 4),
            "five_day_gain_pct": round(float(five_day_gain_pct), 4),
            "volume_ratio": round(float(volume_ratio), 4),
            "lookback_days": int(lookback_days + 1),
            "volume_ma_days": int(volume_ma_days),
        })

    return signals


def detect_volatility_contraction_entries(
    df: pd.DataFrame,
    bb_period: int = 20,
    squeeze_threshold: float = 0.05,
    lookback: int = 999,
) -> list[dict]:
    """
    波动收缩突破入场因子 (布林带挤压后突破):
    - 布林带宽度收缩到 squeeze_threshold 以下
    - 随后价格突破布林上轨
    """
    if len(df) < bb_period + 10:
        return []

    signals = []
    closes = df["close"].values

    # 计算布林带
    sma = pd.Series(closes).rolling(bb_period).mean().values
    std = pd.Series(closes).rolling(bb_period).std().values

    start = max(bb_period + 5, len(df) - lookback)

    in_squeeze = False
    squeeze_start = 0

    for i in range(start, len(df)):
        if sma[i] <= 0 or np.isnan(sma[i]) or np.isnan(std[i]):
            continue

        bb_width = (2 * std[i]) / sma[i]  # 归一化带宽
        upper_band = sma[i] + 2 * std[i]

        if bb_width < squeeze_threshold:
            if not in_squeeze:
                in_squeeze = True
                squeeze_start = i
        else:
            if in_squeeze and closes[i] > upper_band:
                # 挤压结束后突破上轨
                squeeze_bars = i - squeeze_start
                dt_idx = df.index[i]
                confidence = min(squeeze_bars / 10.0, 1.0)

                signals.append({
                    "dt": int(pd.Timestamp(dt_idx).timestamp()),
                    "date_str": dt_idx.strftime("%Y-%m-%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10],
                    "type": f"VolSqueeze_{squeeze_bars}B",
                    "group": "vol",
                    "price": round(float(closes[i]), 4),
                    "confidence": round(confidence, 2),
                    "details": f"BB挤压{squeeze_bars}根后突破上轨, 带宽{bb_width:.3f}",
                })

            in_squeeze = False

    return signals


def detect_candle_run_entries(
    df: pd.DataFrame,
    run_count: int = 3,
    min_body_ratio: float = 0.5,
    lookback: int = 999,
) -> list[dict]:
    """
    连续K线入场因子 (参考 trend-backtest candle_run):
    - N 根连续同向阳线（close > open）
    - 每根K线实体占比 ≥ min_body_ratio（实体 / 振幅）
    """
    if len(df) < run_count + 5:
        return []

    signals = []
    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values

    start = max(run_count, len(df) - lookback)
    for i in range(start, len(df)):
        # 检查连续 run_count 根阳线
        valid = True
        for j in range(run_count):
            idx = i - run_count + 1 + j
            if idx < 0:
                valid = False
                break
            c, o, h, l = closes[idx], opens[idx], highs[idx], lows[idx]
            # 必须是阳线
            if c <= o:
                valid = False
                break
            # 实体占比检查
            amplitude = h - l
            if amplitude <= 0:
                valid = False
                break
            body = c - o
            if body / amplitude < min_body_ratio:
                valid = False
                break

        if not valid:
            continue

        # 确保前一根不是阳线（避免连续触发）
        prev_idx = i - run_count
        if prev_idx >= 0 and closes[prev_idx] > opens[prev_idx]:
            continue

        dt_idx = df.index[i]
        total_gain = (closes[i] - opens[i - run_count + 1]) / opens[i - run_count + 1] * 100
        confidence = min(total_gain / 8.0, 1.0)

        signals.append({
            "dt": int(pd.Timestamp(dt_idx).timestamp()),
            "date_str": dt_idx.strftime("%Y-%m-%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10],
            "type": f"CandleRun_{run_count}",
            "group": "candle_run",
            "price": round(float(closes[i]), 4),
            "confidence": round(max(confidence, 0.1), 2),
            "details": f"{run_count}根连续阳线, 涨幅{total_gain:.1f}%, 实体比≥{min_body_ratio}",
        })

    return signals


def detect_candle_accel_entries(
    df: pd.DataFrame,
    run_count: int = 3,
    lookback: int = 999,
) -> list[dict]:
    """
    加速K线入场因子 (参考 trend-backtest candle_accel):
    - N 根连续同向阳线
    - 实体不递减（后一根实体 ≥ 前一根实体）
    """
    if len(df) < run_count + 5:
        return []

    signals = []
    closes = df["close"].values
    opens = df["open"].values

    start = max(run_count, len(df) - lookback)
    for i in range(start, len(df)):
        valid = True
        prev_body = 0.0
        for j in range(run_count):
            idx = i - run_count + 1 + j
            if idx < 0:
                valid = False
                break
            c, o = closes[idx], opens[idx]
            if c <= o:
                valid = False
                break
            body = c - o
            if j > 0 and body < prev_body:
                valid = False
                break
            prev_body = body

        if not valid:
            continue

        # 避免连续触发
        prev_idx = i - run_count
        if prev_idx >= 0 and closes[prev_idx] > opens[prev_idx]:
            continue

        dt_idx = df.index[i]
        total_gain = (closes[i] - opens[i - run_count + 1]) / opens[i - run_count + 1] * 100
        last_body = closes[i] - opens[i]
        first_body = closes[i - run_count + 1] - opens[i - run_count + 1]
        accel_ratio = last_body / first_body if first_body > 0 else 1.0
        confidence = min(accel_ratio / 3.0, 1.0)

        signals.append({
            "dt": int(pd.Timestamp(dt_idx).timestamp()),
            "date_str": dt_idx.strftime("%Y-%m-%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10],
            "type": f"CandleAccel_{run_count}",
            "group": "candle_accel",
            "price": round(float(closes[i]), 4),
            "confidence": round(max(confidence, 0.1), 2),
            "details": f"{run_count}根加速阳线, 涨幅{total_gain:.1f}%, 加速比{accel_ratio:.1f}x",
        })

    return signals
