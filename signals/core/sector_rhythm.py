# -*- coding: utf-8 -*-
"""
P3-3: 板块节奏检测（兑现信号）

四维评分体系：
1. 连涨天数 (25分) — 连涨越多，疲劳越高
2. RSI14 超买 (25分) — >70 超买区间
3. 距低点涨幅 (25分) — 低点反弹幅度
4. 量能衰减 (25分) — 放量→缩量=动能衰竭

阶段划分：
- 启动(0-25): 刚启动，可加仓
- 加速(25-50): 趋势确认，持有
- 高潮(50-75): 高位放量，开始警惕
- 衰竭(75-100): 缩量滞涨，兑现
- 休整: 从高位回落中

用法：
    from signals.core.sector_rhythm import compute_sector_rhythm, SectorRhythm
    rhythm = compute_sector_rhythm("半导体", bars)
"""
from dataclasses import dataclass
from typing import List, Optional

from czsc import RawBar


@dataclass
class SectorRhythm:
    """板块节奏检测结果"""
    name: str
    rhythm_score: float      # 0-100，越高越疲劳
    phase: str               # "启动"/"加速"/"高潮"/"衰竭"/"休整"
    consecutive_up: int      # 连涨天数
    gain_from_low: float     # 距近期低点涨幅 %
    rsi14: float             # RSI(14)
    volume_trend: str        # "放量"/"缩量"/"持平"
    action_hint: str         # "可加仓"/"持有"/"兑现"/"回避"
    detail: str              # 人类可读描述


def _calc_rsi(closes: list, period: int = 14) -> float:
    """计算 RSI"""
    if len(closes) < period + 1:
        return 50.0  # 数据不足返回中性

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    # 初始平均
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # 平滑
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def _consecutive_up_days(bars: List[RawBar]) -> int:
    """从最近一天往前数连涨天数"""
    count = 0
    for i in range(len(bars) - 1, 0, -1):
        if bars[i].close > bars[i - 1].close:
            count += 1
        else:
            break
    return count


def _gain_from_recent_low(bars: List[RawBar], lookback: int = 20) -> float:
    """距近期低点的涨幅 %"""
    recent = bars[-lookback:] if len(bars) >= lookback else bars
    if not recent:
        return 0.0
    low = min(b.low for b in recent)
    current = bars[-1].close
    if low <= 0:
        return 0.0
    return round((current - low) / low * 100, 2)


def _volume_trend(bars: List[RawBar], recent: int = 5, compare: int = 10) -> str:
    """量能趋势：近 recent 天均量 vs 前 compare 天均量"""
    if len(bars) < recent + compare:
        return "持平"

    recent_bars = bars[-recent:]
    older_bars = bars[-(recent + compare):-recent]

    avg_recent = sum(b.vol for b in recent_bars) / len(recent_bars)
    avg_older = sum(b.vol for b in older_bars) / len(older_bars)

    if avg_older == 0:
        return "持平"

    ratio = avg_recent / avg_older
    if ratio > 1.3:
        return "放量"
    elif ratio < 0.7:
        return "缩量"
    else:
        return "持平"


def compute_sector_rhythm(
    name: str,
    bars: List[RawBar],
    lookback: int = 20,
) -> Optional[SectorRhythm]:
    """
    计算板块节奏四维评分。

    :param name: 板块名称
    :param bars: 日线 RawBar 列表（建议 >= 30 根）
    :param lookback: 低点回溯窗口
    :return: SectorRhythm，数据不足返回 None
    """
    if not bars or len(bars) < 10:
        return None

    closes = [b.close for b in bars]

    # 四维指标
    consec_up = _consecutive_up_days(bars)
    gain_low = _gain_from_recent_low(bars, lookback)
    rsi = _calc_rsi(closes)
    vol_trend = _volume_trend(bars)

    # ── 四维评分（每维 0-25 分）──

    # 1. 连涨天数 (0-25)：5天以上开始累积
    if consec_up >= 8:
        score_consec = 25
    elif consec_up >= 6:
        score_consec = 20
    elif consec_up >= 4:
        score_consec = 15
    elif consec_up >= 2:
        score_consec = 8
    else:
        score_consec = 0

    # 2. RSI 超买 (0-25)：>70 超买
    if rsi >= 85:
        score_rsi = 25
    elif rsi >= 75:
        score_rsi = 20
    elif rsi >= 65:
        score_rsi = 12
    elif rsi >= 55:
        score_rsi = 5
    else:
        score_rsi = 0

    # 3. 距低点涨幅 (0-25)：>20% 高位
    if gain_low >= 30:
        score_gain = 25
    elif gain_low >= 20:
        score_gain = 20
    elif gain_low >= 12:
        score_gain = 12
    elif gain_low >= 5:
        score_gain = 5
    else:
        score_gain = 0

    # 4. 量能衰减 (0-25)：高位缩量=衰竭信号
    if vol_trend == "缩量" and gain_low > 10:
        score_vol = 25  # 高位缩量，最危险
    elif vol_trend == "缩量":
        score_vol = 12  # 缩量但不在高位
    elif vol_trend == "放量" and gain_low > 20:
        score_vol = 15  # 高位放量，可能是高潮
    elif vol_trend == "放量":
        score_vol = 5   # 低位放量=启动
    else:
        score_vol = 8   # 持平

    rhythm_score = score_consec + score_rsi + score_gain + score_vol
    rhythm_score = min(100.0, max(0.0, float(rhythm_score)))

    # ── 阶段判定 ──
    # 特殊判定：从高位回落（RSI 从高位下来 + 近期涨幅萎缩）
    if rsi < 45 and gain_low < 0:
        phase = "休整"
        action_hint = "回避"
    elif rhythm_score >= 75:
        phase = "衰竭"
        action_hint = "兑现"
    elif rhythm_score >= 50:
        phase = "高潮"
        action_hint = "持有"
    elif rhythm_score >= 25:
        phase = "加速"
        action_hint = "持有"
    else:
        phase = "启动"
        action_hint = "可加仓"

    # 详情
    detail_parts = [
        f"连涨{consec_up}天",
        f"RSI{rsi:.0f}",
        f"距低点+{gain_low:.1f}%",
        vol_trend,
    ]
    detail = " | ".join(detail_parts)

    return SectorRhythm(
        name=name,
        rhythm_score=round(rhythm_score, 1),
        phase=phase,
        consecutive_up=consec_up,
        gain_from_low=gain_low,
        rsi14=rsi,
        volume_trend=vol_trend,
        action_hint=action_hint,
        detail=detail,
    )
