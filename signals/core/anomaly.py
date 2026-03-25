# -*- coding: utf-8 -*-
"""
多维统计异常检测引擎 (P0: Sigma Anomaly Detector)

基于已有日线 OHLCV 数据计算滚动统计量，输出 z-score 异常标记。
独立于缠论模块，可单独调用。

5 个异常维度:
1. 量能异常 (volume)   — 成交量 vs 20日均量
2. 振幅异常 (range)    — 日内振幅 vs 20日均振幅
3. 跳空异常 (gap)      — 跳空幅度 vs 20日均跳空
4. 实体异常 (body)     — K线实体 vs 20日均实体
5. 量价背离 (vol_price_div) — 价格新高+量缩 / 价格新低+量增

割肉指标 (capitulation_score):
多因子打分检测散户集中止损行为，是逆向买入信号。

用法:
    from signals.core.anomaly import compute_anomaly_profile
    profile = compute_anomaly_profile("SZ.300750", daily_bars)
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import (
    ANOMALY_ROLLING_WINDOW,
    ANOMALY_THRESHOLDS,
    CAPITULATION_WEIGHTS,
)


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class AnomalyItem:
    """单个异常维度"""
    name: str           # "volume" / "range" / "gap" / "body" / "vol_price_div"
    z_score: float      # 标准差倍数（正=高于均值, 负=低于均值）
    raw_value: float    # 当日原始值
    rolling_mean: float # 滚动均值
    rolling_std: float  # 滚动标准差
    is_anomaly: bool    # 是否超过阈值
    label: str          # "异常放量" / "异常缩量" / "正常" 等


@dataclass
class AnomalyProfile:
    """一只标的的异常画像"""
    symbol: str
    items: Dict[str, AnomalyItem] = field(default_factory=dict)
    anomaly_count: int = 0              # 触发异常的维度数
    convergence: bool = False           # ≥2 维度同时异常
    capitulation_score: float = 0.0     # 割肉指标 (0-100)
    capitulation_detail: str = ""       # 割肉因子明细
    summary: str = ""                   # 人类可读一行摘要


# ── 工具函数 ──────────────────────────────────────────

def _calc_z_score(values: List[float], window: int) -> tuple:
    """
    计算序列最后一个值相对于前 window 个值的 z-score。

    返回: (z_score, rolling_mean, rolling_std)
    如果数据不足或标准差为 0，返回 (0.0, mean, 0.0)
    """
    if len(values) < window + 1:
        return 0.0, 0.0, 0.0

    # 窗口: 倒数第2到倒数第(window+1), 不含最新值
    history = values[-(window + 1):-1]
    current = values[-1]

    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    std = math.sqrt(variance)

    if std < 1e-10:
        return 0.0, mean, 0.0

    z = (current - mean) / std
    return round(z, 2), round(mean, 4), round(std, 4)


def _is_extreme(closes: List[float], lookback: int = 20,
                kind: str = "high") -> bool:
    """最新收盘价是否为近 lookback 日最高/最低"""
    if len(closes) < lookback:
        return False
    recent = closes[-lookback:]
    if kind == "high":
        return closes[-1] >= max(recent)
    return closes[-1] <= min(recent)


def _is_new_high(closes: List[float], lookback: int = 20) -> bool:
    return _is_extreme(closes, lookback, "high")


def _is_new_low(closes: List[float], lookback: int = 20) -> bool:
    return _is_extreme(closes, lookback, "low")


# ── 各维度异常检测 ─────────────────────────────────────

def _detect_volume_anomaly(volumes: List[float], window: int,
                           thresholds: dict) -> AnomalyItem:
    """量能异常: 成交量 vs 滚动均量"""
    z, mean, std = _calc_z_score(volumes, window)
    th = thresholds.get("volume", {"high": 2.0, "low": -1.5})
    high_th = th.get("high", 2.0)
    low_th = th.get("low", -1.5)

    if z >= high_th:
        label = "异常放量"
        is_anomaly = True
    elif z <= low_th:
        label = "异常缩量"
        is_anomaly = True
    else:
        label = "正常"
        is_anomaly = False

    return AnomalyItem(
        name="volume", z_score=z,
        raw_value=volumes[-1] if volumes else 0,
        rolling_mean=mean, rolling_std=std,
        is_anomaly=is_anomaly, label=label,
    )


def _detect_range_anomaly(highs: List[float], lows: List[float],
                          closes: List[float], window: int,
                          thresholds: dict) -> AnomalyItem:
    """振幅异常: (high-low)/close vs 滚动均振幅"""
    if len(highs) < window + 1:
        return AnomalyItem("range", 0, 0, 0, 0, False, "数据不足")

    ranges = [(h - l) / c * 100 if c > 0 else 0
              for h, l, c in zip(highs, lows, closes)]
    z, mean, std = _calc_z_score(ranges, window)
    th = thresholds.get("range", {"high": 2.5})
    high_th = th.get("high", 2.5)

    if z >= high_th:
        label = "异常波动"
        is_anomaly = True
    else:
        label = "正常"
        is_anomaly = False

    return AnomalyItem(
        name="range", z_score=z,
        raw_value=round(ranges[-1], 2) if ranges else 0,
        rolling_mean=mean, rolling_std=std,
        is_anomaly=is_anomaly, label=label,
    )


def _detect_gap_anomaly(opens: List[float], closes: List[float],
                        window: int, thresholds: dict) -> AnomalyItem:
    """跳空异常: |open - prev_close| / prev_close vs 滚动均跳空"""
    if len(opens) < window + 2 or len(closes) < window + 2:
        return AnomalyItem("gap", 0, 0, 0, 0, False, "数据不足")

    # 跳空序列: 从第2根bar开始
    gaps = []
    for i in range(1, len(opens)):
        prev_close = closes[i - 1]
        if prev_close > 0:
            gap_pct = abs(opens[i] - prev_close) / prev_close * 100
            gaps.append(gap_pct)
        else:
            gaps.append(0)

    if len(gaps) < window + 1:
        return AnomalyItem("gap", 0, 0, 0, 0, False, "数据不足")

    z, mean, std = _calc_z_score(gaps, window)
    th = thresholds.get("gap", {"high": 2.0})
    high_th = th.get("high", 2.0)

    if z >= high_th:
        # 判断跳空方向
        last_gap = opens[-1] - closes[-2] if len(closes) >= 2 else 0
        direction = "跳空高开" if last_gap > 0 else "跳空低开"
        label = f"异常{direction}"
        is_anomaly = True
    else:
        label = "正常"
        is_anomaly = False

    return AnomalyItem(
        name="gap", z_score=z,
        raw_value=round(gaps[-1], 2) if gaps else 0,
        rolling_mean=mean, rolling_std=std,
        is_anomaly=is_anomaly, label=label,
    )


def _detect_body_anomaly(opens: List[float], closes: List[float],
                         window: int, thresholds: dict) -> AnomalyItem:
    """实体异常: |close-open|/open vs 滚动均实体"""
    if len(opens) < window + 1:
        return AnomalyItem("body", 0, 0, 0, 0, False, "数据不足")

    bodies = [abs(c - o) / o * 100 if o > 0 else 0
              for o, c in zip(opens, closes)]
    z, mean, std = _calc_z_score(bodies, window)
    th = thresholds.get("body", {"high": 2.0})
    high_th = th.get("high", 2.0)

    if z >= high_th:
        # 判断阴阳
        is_yang = closes[-1] > opens[-1]
        label = "异常大阳" if is_yang else "异常大阴"
        is_anomaly = True
    else:
        label = "正常"
        is_anomaly = False

    return AnomalyItem(
        name="body", z_score=z,
        raw_value=round(bodies[-1], 2) if bodies else 0,
        rolling_mean=mean, rolling_std=std,
        is_anomaly=is_anomaly, label=label,
    )


def _detect_vol_price_divergence(closes: List[float], volumes: List[float],
                                 vol_z: float, lookback: int = 20) -> AnomalyItem:
    """
    量价背离检测:
    - 价格创新高 + 量能 z < -1 → 顶背离（看空信号）
    - 价格创新低 + 量能 z > 1.5 → 底背离（看多信号）
    """
    if len(closes) < lookback or len(volumes) < lookback:
        return AnomalyItem("vol_price_div", 0, 0, 0, 0, False, "数据不足")

    new_high = _is_new_high(closes, lookback)
    new_low = _is_new_low(closes, lookback)

    if new_high and vol_z < -1.0:
        return AnomalyItem(
            name="vol_price_div", z_score=vol_z,
            raw_value=0, rolling_mean=0, rolling_std=0,
            is_anomaly=True, label="顶背离(价高量缩)",
        )
    elif new_low and vol_z > 1.5:
        return AnomalyItem(
            name="vol_price_div", z_score=vol_z,
            raw_value=0, rolling_mean=0, rolling_std=0,
            is_anomaly=True, label="底背离(价低量增)",
        )
    else:
        return AnomalyItem(
            name="vol_price_div", z_score=0,
            raw_value=0, rolling_mean=0, rolling_std=0,
            is_anomaly=False, label="无",
        )


# ── 割肉指标 ──────────────────────────────────────────

def _calc_capitulation_score(
    bars,
    vol_z: float,
    window: int = 20,
    weights: dict = None,
) -> tuple:
    """
    割肉指标: 检测散户集中止损行为 (0-100)。

    4 个因子:
    1. 量能异常放量 (vol_z > 2) → 恐慌抛售
    2. 长下影线 (下影 > 2× 实体) → 抛后有承接
    3. 连续缩量阴跌后突然放量 (前5日缩量 + 今日vol_z > 1.5)
    4. 收盘靠近日内最低 + 持续下跌环境 (close < MA20)

    返回: (score, detail_str)
    """
    w = weights or CAPITULATION_WEIGHTS

    if not bars or len(bars) < window:
        return 0.0, ""

    last = bars[-1]
    score = 0.0
    details = []

    # 因子1: 量能异常放量
    w1 = w.get("volume_spike", 30)
    if vol_z >= 2.5:
        score += w1
        details.append(f"放量z={vol_z:.1f}")
    elif vol_z >= 2.0:
        score += w1 * 0.7
        details.append(f"放量z={vol_z:.1f}")
    elif vol_z >= 1.5:
        score += w1 * 0.4
        details.append(f"偏放量z={vol_z:.1f}")

    # 因子2: 长下影线 (下影 > 2× 实体)
    w2 = w.get("lower_shadow", 25)
    body = abs(last.close - last.open)
    lower_shadow = min(last.open, last.close) - last.low
    if body > 0 and lower_shadow > 2 * body:
        score += w2
        details.append("长下影线")
    elif body > 0 and lower_shadow > 1.5 * body:
        score += w2 * 0.5
        details.append("下影线")

    # 因子3: 连续缩量阴跌后突然放量
    w3 = w.get("vol_breakout", 25)
    if len(bars) >= 6:
        # 前5日均在缩量（vol < 20日均量 × 0.8）
        recent_5 = bars[-6:-1]
        vols_5 = [b.vol for b in recent_5]
        avg_20_vol = sum(b.vol for b in bars[-window - 1:-1]) / window
        if avg_20_vol > 0:
            shrinking = all(v < avg_20_vol * 0.8 for v in vols_5)
            closes_5 = [b.close for b in recent_5]
            declining = all(closes_5[i] < closes_5[i - 1]
                           for i in range(1, len(closes_5)))
            if (shrinking or declining) and vol_z >= 1.5:
                score += w3
                details.append("缩量后放量")

    # 因子4: 收盘靠近日内最低 + 下跌环境
    w4 = w.get("close_at_low", 20)
    if last.high > last.low:
        close_position = (last.close - last.low) / (last.high - last.low)
        # 收盘在日内振幅的下 30%
        if close_position < 0.3:
            # 检查是否在下跌环境 (close < MA20)
            if len(bars) >= window:
                ma20 = sum(b.close for b in bars[-window:]) / window
                if last.close < ma20:
                    score += w4
                    details.append("低位收盘+下跌环境")
                else:
                    score += w4 * 0.3
                    details.append("低位收盘")

    score = min(100.0, max(0.0, score))
    detail_str = " + ".join(details) if details else ""
    return round(score, 1), detail_str


# ── 主函数 ────────────────────────────────────────────

def compute_anomaly_profile(
    symbol: str,
    bars,
    window: int = None,
    thresholds: dict = None,
) -> Optional[AnomalyProfile]:
    """
    从日线 bars 计算异常画像。

    :param symbol: 标的代码
    :param bars: 日线 RawBar 列表（需 >= window+10 根）
    :param window: 滚动窗口, 默认 ANOMALY_ROLLING_WINDOW
    :param thresholds: 异常阈值, 默认 ANOMALY_THRESHOLDS
    :return: AnomalyProfile, 数据不足返回 None
    """
    if window is None:
        window = ANOMALY_ROLLING_WINDOW
    if thresholds is None:
        thresholds = ANOMALY_THRESHOLDS

    if not bars or len(bars) < window + 5:
        return None

    # 提取序列
    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    volumes = [b.vol for b in bars]

    # 5 维度异常检测
    vol_item = _detect_volume_anomaly(volumes, window, thresholds)
    range_item = _detect_range_anomaly(highs, lows, closes, window, thresholds)
    gap_item = _detect_gap_anomaly(opens, closes, window, thresholds)
    body_item = _detect_body_anomaly(opens, closes, window, thresholds)
    div_item = _detect_vol_price_divergence(closes, volumes, vol_item.z_score)

    items = {
        "volume": vol_item,
        "range": range_item,
        "gap": gap_item,
        "body": body_item,
        "vol_price_div": div_item,
    }

    # 统计异常维度数
    anomaly_count = sum(1 for item in items.values() if item.is_anomaly)
    convergence = anomaly_count >= 2

    # 割肉指标
    cap_score, cap_detail = _calc_capitulation_score(
        bars, vol_item.z_score, window)

    # 生成摘要
    fired = [item for item in items.values() if item.is_anomaly]
    if fired:
        fired_str = "+".join(f"{item.name}" for item in fired)
        summary_parts = [f"{anomaly_count}维异常"]
        if convergence:
            summary_parts[0] += f"收敛({fired_str})"
        if cap_score >= 40:
            summary_parts.append(f"割肉{cap_score:.0f}分")
        summary = " | ".join(summary_parts)
    else:
        summary = "无异常" + (f" | 割肉{cap_score:.0f}分" if cap_score >= 40 else "")

    return AnomalyProfile(
        symbol=symbol,
        items=items,
        anomaly_count=anomaly_count,
        convergence=convergence,
        capitulation_score=cap_score,
        capitulation_detail=cap_detail,
        summary=summary,
    )
