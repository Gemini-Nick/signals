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
