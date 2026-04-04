# -*- coding: utf-8 -*-
"""
盘中恐慌检测器 (Intraday Panic Detector)

四维度打分，用盘中可观测指标（非涨跌停等滞后指标）动态识别恐慌情绪。

维度:
1. 跌幅 (30分): 基于下跌速率(velocity)动态计算，非绝对跌幅
2. 广度 (25分): 全行业红盘比例（普跌→恐慌）
3. 量能 (20分): 焦点指数今日成交量 vs 历史同时段均量
4. 支撑 (25分): 焦点指数是否跌破/接近 MA 关键支撑位

核心设计: 恐慌是情绪驱动的，基于下跌速率(velocity)而非绝对水平。
- 急跌 → velocity 高 → 恐慌高
- 市场企稳 → velocity 趋零 + stability 低 → 恐慌自然衰减
- 闪崩 → velocity 突然飙高 → 恐慌立即上升
- 全天慢跌 → velocity 低 → 偏弱但非恐慌

所有数据来自 L1+L2 已加载的数据，不需要额外 API 调用。
"""
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import pandas as pd

from signals.layers.index_report import IndexReport
from config import (PANIC_WAVE_GAP_DAYS, PANIC_EXHAUSTION_DECAY,
                    BOTTOM_SIGNAL_MIN_WAVES, BOTTOM_SIGNAL_MIN_PANIC,
                    BOTTOM_SIGNAL_BASE_CONFIDENCE)


# 焦点指数：代表市场风险偏好的成长型指数
_FOCUS_INDICES = {"科创50", "创业板指", "沪深300"}

# velocity 计算窗口（bar 数量，15M bar → 6根 ≈ 90分钟滑动窗口）
_VELOCITY_WINDOW = 6
# 稳定性检查窗口（最近 N 根 bar）
_STABILITY_WINDOW = 3


@dataclass
class PanicAssessment:
    """盘中恐慌评估结果"""
    score: float          # 0-100 恐慌指数
    level: str            # "恐慌"(>60) / "偏弱"(40-60) / "正常"(<40)
    detail: str           # 分项明细（一行文字）
    # 四个维度得分
    decline_score: float  # 跌幅维度 (0-30)
    breadth_score: float  # 广度维度 (0-25)
    volume_score: float   # 量能维度 (0-20)
    support_score: float  # 支撑维度 (0-25)
    # 补充信息
    focus_declines: dict = None  # {指数名: 跌幅%}
    red_ratio: float = 0.0      # 红盘比例
    # 速率驱动指标（Issue 2）
    velocity: float = 0.0       # 下跌速率 (%/bar)，正值=下跌
    acceleration: float = 0.0   # 速率变化 (正=加速下跌, 负=减速)
    is_stabilizing: bool = False  # 是否正在企稳
    market_state: str = "平稳"    # "急跌"/"缓跌"/"企稳"/"反弹"/"平稳"
    # 波浪追踪 + 抄底信号（assess_intraday_panic 末尾填充）
    wave_state: Optional["PanicWaveState"] = None
    bottom_signal: Optional["BottomSignal"] = None


@dataclass
class PanicWaveState:
    """跨调用的恐慌波浪状态（模块级单例）"""
    wave_count: int = 0
    last_panic_date: str = ""
    peak_velocity: float = 0.0
    consecutive_panic_days: int = 0
    velocities: list = field(default_factory=list)
    is_exhausting: bool = False


@dataclass
class BottomSignal:
    """最后一跌抄底信号"""
    triggered: bool = False
    confidence: float = 0.0
    wave_count: int = 0
    is_exhausting: bool = False
    daily_er_mai: list = field(default_factory=list)
    detail: str = ""


# 模块级单例，ACP 长驻进程内跨调用保持状态
_wave_state = PanicWaveState()


def _update_wave_tracking(score: float, velocity: float, market_state: str):
    """更新恐慌波浪追踪状态"""
    global _wave_state
    today = datetime.now().strftime("%Y-%m-%d")

    if score >= BOTTOM_SIGNAL_MIN_PANIC:
        if today != _wave_state.last_panic_date:
            if _wave_state.last_panic_date:
                try:
                    last = datetime.strptime(_wave_state.last_panic_date, "%Y-%m-%d")
                    gap = (datetime.strptime(today, "%Y-%m-%d") - last).days
                    if gap > PANIC_WAVE_GAP_DAYS:
                        _wave_state.wave_count = 0
                        _wave_state.velocities = []
                except ValueError:
                    pass
            _wave_state.wave_count += 1
            _wave_state.consecutive_panic_days += 1
            _wave_state.last_panic_date = today
            _wave_state.velocities.append(velocity)
        _wave_state.peak_velocity = max(_wave_state.peak_velocity, velocity)
    else:
        if today != _wave_state.last_panic_date:
            _wave_state.consecutive_panic_days = 0
            _wave_state.peak_velocity = 0.0

    vels = _wave_state.velocities
    _wave_state.is_exhausting = (
        len(vels) >= 2
        and vels[-1] < vels[-2] * PANIC_EXHAUSTION_DECAY
        and market_state in ("企稳", "缓跌")
    )


def detect_bottom_signal(
    panic: PanicAssessment,
    index_reports: List[IndexReport],
) -> BottomSignal:
    """
    最后一跌检测：恐慌 + 日线二买共振。

    触发条件（ALL required）:
    1. panic_score >= BOTTOM_SIGNAL_MIN_PANIC
    2. 至少1个焦点指数出现日线二买
    3. wave_count >= BOTTOM_SIGNAL_MIN_WAVES
    """
    er_mai_indices = []
    for r in index_reports:
        if r.name in _FOCUS_INDICES:
            sig = getattr(r, 'daily_latest_signal', '')
            if sig and '二买' in sig:
                er_mai_indices.append(r.name)

    if (panic.score < BOTTOM_SIGNAL_MIN_PANIC
            or not er_mai_indices
            or _wave_state.wave_count < BOTTOM_SIGNAL_MIN_WAVES):
        return BottomSignal()

    conf = BOTTOM_SIGNAL_BASE_CONFIDENCE
    if _wave_state.is_exhausting:
        conf += 0.15
    if _wave_state.wave_count >= 3:
        conf += 0.10
    if panic.is_stabilizing:
        conf += 0.05
    conf = min(conf, 0.95)

    detail = (f"第{_wave_state.wave_count}波恐慌"
              f"{'[衰竭]' if _wave_state.is_exhausting else ''}"
              f" + 日线二买({','.join(er_mai_indices)})"
              f" → 置信度{conf:.0%}")

    return BottomSignal(
        triggered=True, confidence=conf,
        wave_count=_wave_state.wave_count,
        is_exhausting=_wave_state.is_exhausting,
        daily_er_mai=er_mai_indices,
        detail=detail,
    )


def _get_today_bars(analyzers: dict):
    """提取焦点指数的当日 15M bars。返回 {name: [bars]}"""
    today = datetime.now().date()
    result = {}
    for name in _FOCUS_INDICES:
        az = analyzers.get(name)
        if az is None:
            continue
        f15 = getattr(az, '_f15', None)
        if f15 is None:
            continue
        bars = f15.bars_raw
        if not bars:
            continue
        today_bars = [b for b in bars if b.dt.date() == today]
        if not today_bars:
            last_date = bars[-1].dt.date()
            today_bars = [b for b in bars if b.dt.date() == last_date]
        if today_bars:
            result[name] = today_bars
    return result


def _calc_velocity_metrics(bars: list) -> dict:
    """
    从一组连续 bars 计算速率指标。

    返回:
        velocity: 下跌速率 (%/bar)，正值=下跌方向
        acceleration: 速率变化率
        stability: 最近 N bar 收益率的标准差
        returns: 逐 bar 收益率列表
    """
    if len(bars) < 2:
        return {"velocity": 0.0, "acceleration": 0.0,
                "stability": 0.0, "returns": []}

    # 逐 bar 收益率
    returns = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        if prev_close > 0:
            ret = (bars[i].close - prev_close) / prev_close * 100
            returns.append(ret)

    if not returns:
        return {"velocity": 0.0, "acceleration": 0.0,
                "stability": 0.0, "returns": []}

    # velocity = 负收益率之和 / N（正值=下跌方向）
    n = len(returns)
    neg_sum = sum(r for r in returns if r < 0)
    velocity = abs(neg_sum) / n  # 正值表示下跌速率

    # 整体方向（如果正收益占主导，velocity 应该很低或 0）
    total_return = sum(returns)
    if total_return > 0:
        # 上涨方向 → velocity 设为负值（反弹）
        velocity = -abs(sum(r for r in returns if r > 0)) / n

    # acceleration: 前半窗口 vs 后半窗口的速率变化
    mid = n // 2
    if mid >= 1 and n - mid >= 1:
        first_half_neg = sum(r for r in returns[:mid] if r < 0)
        first_vel = abs(first_half_neg) / mid
        second_half_neg = sum(r for r in returns[mid:] if r < 0)
        second_vel = abs(second_half_neg) / (n - mid)

        if first_vel > 0.001:
            acceleration = (second_vel - first_vel) / first_vel
        elif second_vel > 0.001:
            acceleration = 1.0  # 从无下跌到有下跌
        else:
            acceleration = 0.0
    else:
        acceleration = 0.0

    # stability: 最近几根 bar 收益率的标准差（越低=越稳定）
    recent = returns[-_STABILITY_WINDOW:] if len(returns) >= _STABILITY_WINDOW else returns
    mean_r = sum(recent) / len(recent)
    variance = sum((r - mean_r) ** 2 for r in recent) / len(recent)
    stability = math.sqrt(variance)

    return {
        "velocity": round(velocity, 4),
        "acceleration": round(acceleration, 3),
        "stability": round(stability, 4),
        "returns": returns,
    }


def _calc_decline_score(analyzers: dict) -> tuple:
    """
    跌幅维度 (满分30): 基于下跌速率(velocity)动态计算。

    核心指标:
    - velocity: 下跌速率 (%/bar)，从最近 N 根 15M bar 的负收益率求得
    - acceleration: 速率变化率（加速下跌 / 减速企稳）
    - stability: 最近 bar 的波动率（低=已企稳）

    velocity → score 映射 (满分30):
        ≥ 0.3%/bar → 30 (急速下跌)
        0.1~0.3%   → 15~30 线性
        < 0.1%     → 0~15 线性
    acceleration 调节 (±10):
        加速下跌 (accel > 0.15) → +10
        减速企稳 (accel < -0.15) → -8
    stability 衰减 (×0.5~1.0):
        波动率 < 0.08% → 市场已企稳，分数 ×0.5~0.8

    返回: (score, declines_dict, velocity, acceleration, is_stabilizing, market_state)
    """
    today_bars_map = _get_today_bars(analyzers)
    if not today_bars_map:
        return 0.0, {}, 0.0, 0.0, False, "平稳"

    # 对每个焦点指数计算速率指标
    all_metrics = {}
    declines = {}
    for name, bars in today_bars_map.items():
        # 绝对跌幅（仍保留作为辅助显示）
        if bars[0].open > 0:
            change_pct = (bars[-1].close - bars[0].open) / bars[0].open * 100
            declines[name] = round(change_pct, 2)

        # 取最近 N 根 bar 计算速率
        recent = bars[-_VELOCITY_WINDOW:] if len(bars) >= _VELOCITY_WINDOW else bars
        metrics = _calc_velocity_metrics(recent)
        all_metrics[name] = metrics

    if not all_metrics:
        return 0.0, declines, 0.0, 0.0, False, "平稳"

    # 取焦点指数中最大速率（最恐慌的那个）
    worst_name = max(all_metrics, key=lambda n: all_metrics[n]["velocity"])
    m = all_metrics[worst_name]
    velocity = m["velocity"]
    acceleration = m["acceleration"]
    stability = m["stability"]

    # velocity → base score
    if velocity >= 0.3:
        score = 30.0
    elif velocity >= 0.1:
        score = 15.0 + (velocity - 0.1) / 0.2 * 15.0
    elif velocity > 0:
        score = velocity / 0.1 * 15.0
    else:
        # velocity <= 0: 上涨/反弹方向
        score = 0.0

    # acceleration 调节
    if acceleration > 0.15:
        score += 10.0  # 加速下跌 → 恐慌加剧
    elif acceleration < -0.15:
        score -= 8.0   # 减速/企稳 → 恐慌消退

    # stability 衰减：市场已企稳时降低分数
    is_stabilizing = False
    if stability < 0.08 and velocity > 0:
        # 低波动 + 仍有下跌 → 市场正在企稳
        decay_mult = max(0.5, stability / 0.08)
        score *= decay_mult
        is_stabilizing = True

    score = max(0.0, min(30.0, score))

    # 市场状态判断
    if velocity >= 0.2:
        market_state = "急跌"
    elif velocity >= 0.05:
        market_state = "缓跌"
    elif velocity > -0.02:
        if is_stabilizing:
            market_state = "企稳"
        else:
            market_state = "平稳"
    else:
        market_state = "反弹"

    return (round(score, 1), declines, round(velocity, 4),
            round(acceleration, 3), is_stabilizing, market_state)


def _calc_breadth_score(name_df) -> tuple:
    """
    广度维度 (满分25): 全行业红盘比例。

    红盘 < 30%: 满分25 (普跌)
    30% ~ 50%: 线性 (25 → 0)
    > 50%: 0分

    :param name_df: 东财行业板块数据 DataFrame，需包含涨跌幅列
    :return: (score, red_ratio)
    """
    if name_df is None or name_df.empty:
        return 0.0, 0.5

    # 尝试找涨跌幅列
    change_col = None
    for col in ['涨跌幅', '涨跌幅(%)', '涨幅', '涨幅(%)', '最新涨跌幅']:
        if col in name_df.columns:
            change_col = col
            break

    if change_col is None:
        return 0.0, 0.5

    changes = pd.to_numeric(name_df[change_col], errors='coerce').dropna()
    if changes.empty:
        return 0.0, 0.5

    red_count = (changes > 0).sum()
    total = len(changes)
    red_ratio = red_count / total if total > 0 else 0.5

    if red_ratio < 0.30:
        score = 25.0
    elif red_ratio < 0.50:
        # 30%→25, 50%→0，线性插值
        score = 25.0 * (0.50 - red_ratio) / 0.20
    else:
        score = 0.0

    return round(score, 1), round(red_ratio, 3)


def _calc_volume_score(analyzers: dict) -> float:
    """
    量能维度 (满分20): 焦点指数今日成交量 vs 历史同时段均量。

    放量下跌 = 恐慌性抛售。
    量比 >= 1.5: 满分20
    1.0 ~ 1.5: 线性 0~20
    < 1.0: 0分（缩量下跌属于阴跌，非恐慌）
    """
    today = datetime.now().date()
    vol_ratios = []

    for name in _FOCUS_INDICES:
        az = analyzers.get(name)
        if az is None:
            continue
        f15 = getattr(az, '_f15', None)
        if f15 is None:
            continue
        bars = f15.bars_raw
        if not bars:
            continue

        # 分出今日和历史 bars
        today_bars = [b for b in bars if b.dt.date() == today]
        if not today_bars:
            last_date = bars[-1].dt.date()
            today_bars = [b for b in bars if b.dt.date() == last_date]
        if not today_bars:
            continue

        target_date = today_bars[0].dt.date()
        hist_bars = [b for b in bars if b.dt.date() != target_date]
        if not hist_bars:
            continue

        # 今日已有 bars 的时段区间
        today_times = {b.dt.time() for b in today_bars}
        # 历史同时段 bars
        hist_same_time = [b for b in hist_bars if b.dt.time() in today_times]
        if not hist_same_time:
            # 退化: 用所有历史 bars 的平均
            hist_same_time = hist_bars

        today_avg_vol = sum(b.vol for b in today_bars) / len(today_bars)
        hist_avg_vol = sum(b.vol for b in hist_same_time) / len(hist_same_time)

        if hist_avg_vol > 0:
            vol_ratios.append(today_avg_vol / hist_avg_vol)

    if not vol_ratios:
        return 0.0

    avg_ratio = sum(vol_ratios) / len(vol_ratios)

    if avg_ratio >= 1.5:
        score = 20.0
    elif avg_ratio >= 1.0:
        score = (avg_ratio - 1.0) * 40.0  # 1.0->0, 1.5->20
    else:
        score = 0.0

    return round(score, 1)


def _calc_support_score(reports: List[IndexReport]) -> float:
    """
    支撑位维度 (满分25): 焦点指数是否跌破/接近 MA 关键支撑位。

    跌破关键支撑 (distance_pct < -0.5%): 25分
    接近支撑 (-0.5% ~ 0.5%): 15分
    距支撑较远 (>0.5%): 0分

    取焦点指数中最严重的一个打分。
    """
    worst_score = 0.0

    for r in reports:
        if r.name not in _FOCUS_INDICES:
            continue
        if r.ma_context is None:
            continue

        # MAContext.support_levels: 价下方的均线，按距离升序
        # MAContext.key_levels: 提炼的关键2-3个
        ma_ctx = r.ma_context

        # 检查最近的支撑位
        supports = getattr(ma_ctx, 'support_levels', [])
        key_levels = getattr(ma_ctx, 'key_levels', [])

        # 优先看 key_levels 中的支撑位
        check_levels = key_levels if key_levels else supports
        if not check_levels:
            continue

        for level in check_levels:
            dist = level.distance_pct  # 正=价格在上方, 负=价格在下方(跌破)
            if dist < -0.5:
                # 已跌破支撑
                worst_score = max(worst_score, 25.0)
            elif -0.5 <= dist <= 0.5:
                # 接近支撑（贴合区域）
                worst_score = max(worst_score, 15.0)
            # dist > 0.5: 距支撑还远，不加分
            break  # 只看最近的一个关键位

    return worst_score


def assess_intraday_panic(
    index_reports: List[IndexReport],
    analyzers: dict,
    industry_name_df=None,
) -> PanicAssessment:
    """
    盘中恐慌评估（四维度打分）。

    :param index_reports: L1 生成的 IndexReport 列表
    :param analyzers: IndexScreener.analyzers dict (name -> IndexAnalyzer)
    :param industry_name_df: L2 行业涨跌幅快照 DataFrame
    :return: PanicAssessment
    """
    # 四维度分别计算（跌幅维度现在返回速率指标）
    (decline_score, focus_declines, velocity,
     acceleration, is_stabilizing, market_state) = _calc_decline_score(analyzers)
    breadth_score, red_ratio = _calc_breadth_score(industry_name_df)
    volume_score = _calc_volume_score(analyzers)
    support_score = _calc_support_score(index_reports)

    total = decline_score + breadth_score + volume_score + support_score

    # 级别判定
    if total >= 60:
        level = "恐慌"
    elif total >= 40:
        level = "偏弱"
    else:
        level = "正常"

    # 生成详情文本
    detail_parts = []
    # 市场状态标签
    state_emoji = {
        "急跌": "🔴", "缓跌": "🟡", "企稳": "🟢",
        "反弹": "📈", "平稳": "⚪",
    }
    detail_parts.append(f"{state_emoji.get(market_state, '')}[{market_state}]")
    if focus_declines:
        decline_strs = [f"{n}{v:+.1f}%" for n, v in
                        sorted(focus_declines.items(), key=lambda x: x[1])]
        detail_parts.append(f"跌幅: {', '.join(decline_strs)}")
    if velocity != 0:
        vel_dir = "↓" if velocity > 0 else "↑"
        detail_parts.append(f"速率: {vel_dir}{abs(velocity):.2f}%/bar")
    detail_parts.append(f"广度: {red_ratio*100:.0f}%行业红盘")
    detail_parts.append(f"量能: {volume_score:.0f}分")
    if support_score > 0:
        detail_parts.append(f"支撑: {'接近' if support_score < 25 else '跌破'}关键位")
    if is_stabilizing:
        detail_parts.append("📊企稳中")
    detail = "  |  ".join(detail_parts)

    # 波浪追踪 + 抄底检测
    _update_wave_tracking(total, velocity, market_state)
    result = PanicAssessment(
        score=round(total, 1),
        level=level,
        detail=detail,
        decline_score=decline_score,
        breadth_score=breadth_score,
        volume_score=volume_score,
        support_score=support_score,
        focus_declines=focus_declines,
        red_ratio=red_ratio,
        velocity=velocity,
        acceleration=acceleration,
        is_stabilizing=is_stabilizing,
        market_state=market_state,
        wave_state=_wave_state,
        bottom_signal=detect_bottom_signal(
            PanicAssessment(
                score=round(total, 1), level=level, detail=detail,
                decline_score=decline_score, breadth_score=breadth_score,
                volume_score=volume_score, support_score=support_score,
                is_stabilizing=is_stabilizing, market_state=market_state,
            ),
            index_reports,
        ),
    )
    return result
