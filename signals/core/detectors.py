# -*- coding: utf-8 -*-
"""
信号检测模块 —— 缠论买卖点识别

5 类信号：
  一买 / 一卖  —— 内置函数 cxt_first_buy_V221126 / cxt_first_sell_V221126
  二买 / 二卖  —— 自定义笔结构分析（回调不破前低/前高）
  三买 / 三卖  —— 自定义中枢分析（回调不破中枢上沿/下沿）
  背驰买 / 背驰卖 —— 同向笔 power_price 对比
  趋势买 / 趋势卖 —— 内置函数 cxt_bs_V240526
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

from czsc import CZSC, Direction


@dataclass
class SignalEvent:
    symbol: str
    freq: str           # e.g. "15分钟"
    dt: datetime
    signal_type: str    # "一买"/"二买"/"三买"/"背驰买"/"趋势买" 及对应卖点
    confidence: float   # 0.0 ~ 1.0
    price: float
    details: str = ""


def detect_all_signals(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    """对一个 CZSC 对象运行全部检测器，返回 SignalEvent 列表。"""
    signals: List[SignalEvent] = []
    signals.extend(detect_structural_signals(czsc_obj, symbol))
    signals.extend(_detect_macd_convergence(czsc_obj, symbol))
    signals.extend(_detect_gaps(czsc_obj, symbol))
    return signals


def detect_structural_signals(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    """Run only CZSC structure detectors, excluding auxiliary MACD/gap scans."""
    signals: List[SignalEvent] = []
    signals.extend(_detect_first_bs(czsc_obj, symbol))
    signals.extend(_detect_second_bs(czsc_obj, symbol))
    signals.extend(_detect_third_bs(czsc_obj, symbol))
    signals.extend(_detect_divergence(czsc_obj, symbol))
    signals.extend(_detect_trend(czsc_obj, symbol))
    signals.extend(_detect_patterns(czsc_obj, symbol))
    return signals


def _detect_macd_convergence(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    """MACD 绿柱极端状态信号（从 CZSC bars_raw 提取 OHLC 数据）"""
    try:
        from signals.core.macd_detector import detect_macd_signals
        import pandas as pd

        bars = czsc_obj.bars_raw
        if not bars or len(bars) < 35:
            return []

        freq_val = czsc_obj.freq.value
        records = [{"open": b.open, "high": b.high, "low": b.low, "close": b.close}
                   for b in bars]
        df = pd.DataFrame(records)
        df.index = pd.DatetimeIndex([b.dt for b in bars])

        macd_sigs = detect_macd_signals(df, symbol, freq_val, lookback=10)

        # 转换为 SignalEvent
        events = []
        for sig in macd_sigs:
            if "A_" in sig.pattern:
                sig_type = "MACD绿柱扩大_零上"
            else:
                sig_type = "MACD绿柱缩小_零下"

            events.append(SignalEvent(
                symbol=symbol,
                freq=freq_val,
                dt=sig.dt,
                signal_type=sig_type,
                confidence=sig.confidence,
                price=sig.price,
                details=sig.details,
            ))
        return events
    except Exception:
        return []


def _detect_gaps(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    """跳空缺口信号检测（从 CZSC bars_raw 提取）"""
    try:
        from signals.core.gap_detector import detect_gap_signals

        bars = czsc_obj.bars_raw
        if not bars or len(bars) < 25:
            return []
        return detect_gap_signals(bars, symbol, czsc_obj.freq.value)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────
# 一买 / 一卖  —— 调用内置函数
# ─────────────────────────────────────────────────────────
def _detect_first_bs(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    signals = []
    if len(czsc_obj.bi_list) < 5:
        return signals

    try:
        from czsc.signals import cxt_first_buy_V221126, cxt_first_sell_V221126
    except ImportError:
        return signals

    freq_val = czsc_obj.freq.value

    result = cxt_first_buy_V221126(czsc_obj, di=1)
    for key, value in result.items():
        if "一买" in str(value):
            last_bi = czsc_obj.bi_list[-1]
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=last_bi.edt, signal_type="一买",
                confidence=0.7,
                price=last_bi.low,
                details=f"内置信号: {key}={value}",
            ))

    result = cxt_first_sell_V221126(czsc_obj, di=1)
    for key, value in result.items():
        if "一卖" in str(value):
            last_bi = czsc_obj.bi_list[-1]
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=last_bi.edt, signal_type="一卖",
                confidence=0.7,
                price=last_bi.high,
                details=f"内置信号: {key}={value}",
            ))

    return signals


# ─────────────────────────────────────────────────────────
# 二买 / 二卖  —— 回调不破前低 / 反弹不破前高
# ─────────────────────────────────────────────────────────
def _detect_second_bs(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    bis = czsc_obj.finished_bis
    if len(bis) < 5:
        return []

    signals = []
    b1, b2, b3, b4, b5 = bis[-5], bis[-4], bis[-3], bis[-2], bis[-1]
    freq_val = czsc_obj.freq.value

    # 二买：回调低点抬升。兼容两种触发形态：
    # 1) b5 已经向上确认；2) b5 仍是向下回调笔但低点继续抬升。
    if (b1.direction == Direction.Down and
            b3.direction == Direction.Down and
            b5.direction == Direction.Up and
            b3.low > b1.low):
        # 回撤比例：b3 回调幅度 / b2 上涨幅度
        b2_range = b2.high - b2.low
        b3_range = b3.high - b3.low
        retracement = b3_range / (b2_range + 1e-9)
        conf = 0.80 if retracement < 0.618 else 0.60
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=b5.sdt, signal_type="二买",
            confidence=conf,
            price=b5.fx_a.fx,
            details=f"低点抬升 {b3.low:.2f}>{b1.low:.2f}，右侧向上确认，回撤 {retracement:.1%}",
        ))

    if (b1.direction == Direction.Down and
            b3.direction == Direction.Down and
            b5.direction == Direction.Down and
            b3.low > b1.low and
            b5.low > b3.low):
        b4_range = b4.high - b4.low
        b5_range = b5.high - b5.low
        retracement = b5_range / (b4_range + 1e-9)
        conf = 0.78 if retracement < 0.618 else 0.62
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=b5.edt, signal_type="二买",
            confidence=conf,
            price=b5.low,
            details=f"回调低点连续抬升 {b1.low:.2f}->{b3.low:.2f}->{b5.low:.2f}，回撤 {retracement:.1%}",
        ))

    # 二卖：反弹高点递降。兼容右侧向下确认和仍在向上反弹笔两种形态。
    if (b1.direction == Direction.Up and
            b3.direction == Direction.Up and
            b5.direction == Direction.Down and
            b3.high < b1.high):
        b2_range = b2.high - b2.low
        b3_range = b3.high - b3.low
        retracement = b3_range / (b2_range + 1e-9)
        conf = 0.80 if retracement < 0.618 else 0.60
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=b5.sdt, signal_type="二卖",
            confidence=conf,
            price=b5.fx_a.fx,
            details=f"高点递降 {b3.high:.2f}<{b1.high:.2f}，右侧向下确认，反弹比 {retracement:.1%}",
        ))

    if (b1.direction == Direction.Up and
            b3.direction == Direction.Up and
            b5.direction == Direction.Up and
            b3.high < b1.high and
            b5.high < b3.high):
        b4_range = b4.high - b4.low
        b5_range = b5.high - b5.low
        retracement = b5_range / (b4_range + 1e-9)
        conf = 0.78 if retracement < 0.618 else 0.62
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=b5.edt, signal_type="二卖",
            confidence=conf,
            price=b5.high,
            details=f"反弹高点连续递降 {b1.high:.2f}->{b3.high:.2f}->{b5.high:.2f}，反弹比 {retracement:.1%}",
        ))

    return signals


# ─────────────────────────────────────────────────────────
# 三买 / 三卖  —— 中枢分析
# ─────────────────────────────────────────────────────────
def _detect_third_bs(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    bis = czsc_obj.finished_bis
    if len(bis) < 5:
        return []

    signals = []
    b1, b2, b3, b4, b5 = bis[-5], bis[-4], bis[-3], bis[-2], bis[-1]
    freq_val = czsc_obj.freq.value

    # 中枢：由 b1 b2 b3 三笔重叠区间确定
    zs_zd = max(min(b1.high, b1.low), min(b3.high, b3.low))  # 中枢底
    zs_zg = min(max(b1.high, b1.low), max(b3.high, b3.low))  # 中枢顶
    # 等价简化：重叠区间
    zs_zd = max(b1.low, b3.low)
    zs_zg = min(b1.high, b3.high)

    if zs_zd >= zs_zg:
        return []  # 无有效中枢

    # 三买：b4 向上离开中枢，b5↓ 回调但低点不回中枢。
    if b4.direction == Direction.Up and b5.direction == Direction.Down:
        if b4.high > zs_zg and b5.low > zs_zg:
            # 离开幅度越大、回调越浅 → 置信度越高
            leave_pct = (b4.high - zs_zg) / (zs_zg + 1e-9) * 100
            pullback_pct = (b4.high - b5.low) / (b4.high - zs_zg + 1e-9) * 100
            conf = 0.80 if pullback_pct < 50 else 0.65
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b5.edt, signal_type="三买",
                confidence=conf,
                price=b5.low,
                details=f"回调低 {b5.low:.2f}>中枢上沿 {zs_zg:.2f}，"
                        f"离开 {leave_pct:.1f}%，回撤 {pullback_pct:.0f}%",
            ))

    # 三卖：b4 向下离开中枢，b5↑ 反弹但高点不回中枢。
    if b4.direction == Direction.Down and b5.direction == Direction.Up:
        if b4.low < zs_zd and b5.high < zs_zd:
            leave_pct = (zs_zd - b4.low) / (zs_zd + 1e-9) * 100
            pullback_pct = (b5.high - b4.low) / (zs_zd - b4.low + 1e-9) * 100
            conf = 0.80 if pullback_pct < 50 else 0.65
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b5.edt, signal_type="三卖",
                confidence=conf,
                price=b5.high,
                details=f"反弹高 {b5.high:.2f}<中枢下沿 {zs_zd:.2f}，"
                        f"离开 {leave_pct:.1f}%，反弹 {pullback_pct:.0f}%",
            ))

    return signals


# ─────────────────────────────────────────────────────────
# 背驰买 / 背驰卖  —— 同向笔 power_price 对比
# ─────────────────────────────────────────────────────────
def _detect_divergence(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    bis = czsc_obj.finished_bis
    if len(bis) < 3:
        return []

    signals = []
    last = bis[-1]
    freq_val = czsc_obj.freq.value

    # 找上一根同向笔
    prev_same = None
    for bi in reversed(bis[:-1]):
        if bi.direction == last.direction:
            prev_same = bi
            break

    if prev_same is None:
        return []

    # 背驰卖：都是向上笔，创新高但力度减弱（力度差 >= 10% 才触发）
    if last.direction == Direction.Up:
        if last.high > prev_same.high and last.power_price < prev_same.power_price:
            # 力度减弱必须达到 10% 阈值，避免微小差异误触发
            power_drop_pct = (prev_same.power_price - last.power_price) / (prev_same.power_price + 1e-9) * 100
            if power_drop_pct >= 10:
                vol_div = last.power_volume < prev_same.power_volume
                conf = 0.75 if vol_div else 0.60
                signals.append(SignalEvent(
                    symbol=symbol, freq=freq_val,
                    dt=last.edt, signal_type="背驰卖",
                    confidence=conf,
                    price=last.high,
                    details=(f"价创新高 {last.high:.2f}>{prev_same.high:.2f}，"
                             f"力度降 {power_drop_pct:.0f}%"
                             + ("，量能也背驰" if vol_div else "")),
                ))

    # 背驰买：都是向下笔，创新低但力度减弱（力度差 >= 10% 才触发）
    if last.direction == Direction.Down:
        if last.low < prev_same.low and last.power_price < prev_same.power_price:
            power_drop_pct = (prev_same.power_price - last.power_price) / (prev_same.power_price + 1e-9) * 100
            if power_drop_pct >= 10:
                vol_div = last.power_volume < prev_same.power_volume
                conf = 0.75 if vol_div else 0.60
                signals.append(SignalEvent(
                    symbol=symbol, freq=freq_val,
                    dt=last.edt, signal_type="背驰买",
                    confidence=conf,
                    price=last.low,
                    details=(f"价创新低 {last.low:.2f}<{prev_same.low:.2f}，"
                             f"力度降 {power_drop_pct:.0f}%"
                             + ("，量能也背驰" if vol_div else "")),
                ))

    return signals


# ─────────────────────────────────────────────────────────
# 趋势买 / 趋势卖  —— 内置 cxt_bs_V240526
# ─────────────────────────────────────────────────────────
def _detect_trend(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    if len(czsc_obj.bi_list) < 3:
        return []

    signals = []
    try:
        from czsc.signals import cxt_bs_V240526
    except ImportError:
        return signals

    freq_val = czsc_obj.freq.value
    last_bi = czsc_obj.bi_list[-1]

    result = cxt_bs_V240526(czsc_obj)
    for key, value in result.items():
        val_str = str(value)
        if "买点" in val_str:
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=last_bi.edt, signal_type="趋势买",
                confidence=0.7,
                price=last_bi.low,
                details=f"内置趋势信号: {key}={value}",
            ))
        elif "卖点" in val_str:
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=last_bi.edt, signal_type="趋势卖",
                confidence=0.7,
                price=last_bi.high,
                details=f"内置趋势信号: {key}={value}",
            ))

    return signals


# ─────────────────────────────────────────────────────────
# 经典形态识别（1期：双头双底 + 头肩 + 三角形）
# ─────────────────────────────────────────────────────────

def _near(a: float, b: float, tol_pct: float = 2.0) -> bool:
    """判断两个价格是否接近（容差 tol_pct%）"""
    return abs(a - b) / (max(abs(a), abs(b)) + 1e-9) * 100 < tol_pct


def _detect_patterns(czsc_obj: CZSC, symbol: str) -> List[SignalEvent]:
    """
    经典形态识别（Phase 1）：
    - 双头(M顶) / 双底(W底)：5+ 笔
    - 头肩顶 / 头肩底：7+ 笔
    - 上升三角 / 下降三角：7+ 笔
    """
    bis = czsc_obj.finished_bis
    signals: List[SignalEvent] = []
    freq_val = czsc_obj.freq.value

    # ── 双头 / 双底（需要 5+ 笔）──
    if len(bis) >= 5:
        signals.extend(_detect_double_top_bottom(bis, symbol, freq_val))

    # ── 头肩顶 / 头肩底（需要 7+ 笔）──
    if len(bis) >= 7:
        signals.extend(_detect_head_shoulders(bis, symbol, freq_val))

    # ── 上升/下降三角（需要 7+ 笔）──
    if len(bis) >= 7:
        signals.extend(_detect_triangle(bis, symbol, freq_val))

    return signals


def _detect_double_top_bottom(bis, symbol: str, freq_val: str) -> List[SignalEvent]:
    """
    双头(M顶)：两次上攻高点近似，第二次力度衰减后回落。
    双底(W底)：两次下跌低点近似，第二次力度衰减后反弹。

    结构（5笔）：
    双头: b1↑ b2↓ b3↑ b4↓ b5  — b1.high ≈ b3.high（<2%），b3力度 < b1
    双底: b1↓ b2↑ b3↓ b4↑ b5  — b1.low ≈ b3.low（<2%），b3力度 < b1
    """
    signals = []
    b1, b2, b3, b4, b5 = bis[-5], bis[-4], bis[-3], bis[-2], bis[-1]

    # ── 双头(M顶) ──
    if (b1.direction == Direction.Up and
            b3.direction == Direction.Up and
            b5.direction == Direction.Down and
            _near(b1.high, b3.high, 2.0)):
        # 力度衰减：b3 力度 < b1 力度
        power_weaken = b3.power_price < b1.power_price
        # 颈线：b2 低点
        neckline = b2.low
        # 确认：b4 跌破颈线
        broken = b4.low < neckline
        if power_weaken and broken:
            drop_pct = (b3.high - b4.low) / (b3.high + 1e-9) * 100
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b5.sdt, signal_type="形态:双头",
                confidence=0.70,
                price=b4.low,
                details=f"双峰 {b1.high:.2f}≈{b3.high:.2f}，"
                        f"颈线 {neckline:.2f} 已破，跌 {drop_pct:.1f}%",
            ))

    # ── 双底(W底) ──
    if (b1.direction == Direction.Down and
            b3.direction == Direction.Down and
            b5.direction == Direction.Up and
            _near(b1.low, b3.low, 2.0)):
        power_weaken = b3.power_price < b1.power_price
        neckline = b2.high
        broken = b4.high > neckline
        if power_weaken and broken:
            rise_pct = (b4.high - b3.low) / (b3.low + 1e-9) * 100
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b5.sdt, signal_type="形态:双底",
                confidence=0.70,
                price=b4.high,
                details=f"双谷 {b1.low:.2f}≈{b3.low:.2f}，"
                        f"颈线 {neckline:.2f} 已破，涨 {rise_pct:.1f}%",
            ))

    return signals


def _detect_head_shoulders(bis, symbol: str, freq_val: str) -> List[SignalEvent]:
    """
    头肩顶：三峰结构，中间峰（头部）最高，两侧肩部近似。
    头肩底：三谷结构，中间谷（头部）最低，两侧肩部近似。

    结构（7笔）：
    头肩顶: b1↑ b2↓ b3↑ b4↓ b5↑ b6↓ b7
            b3.high > b1.high（头>左肩），b5.high < b3.high（右肩<头）
            b1.high ≈ b5.high（左右肩近似，容差5%）
    头肩底: 对称镜像
    """
    signals = []
    b1, b2, b3, b4, b5, b6, b7 = bis[-7], bis[-6], bis[-5], bis[-4], bis[-3], bis[-2], bis[-1]

    # ── 头肩顶 ──
    if (b1.direction == Direction.Up and
            b3.direction == Direction.Up and
            b5.direction == Direction.Up and
            b3.high > b1.high and           # 头 > 左肩
            b5.high < b3.high and           # 右肩 < 头
            _near(b1.high, b5.high, 5.0)):  # 左右肩近似
        neckline = min(b2.low, b4.low)
        power_decay = b5.power_price < b3.power_price
        broken = b6.low < neckline or b7.low < neckline
        if power_decay and broken:
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b7.sdt, signal_type="形态:头肩顶",
                confidence=0.75,
                price=neckline,
                details=f"左肩 {b1.high:.2f} 头 {b3.high:.2f} "
                        f"右肩 {b5.high:.2f}，颈线 {neckline:.2f} 已破",
            ))

    # ── 头肩底 ──
    if (b1.direction == Direction.Down and
            b3.direction == Direction.Down and
            b5.direction == Direction.Down and
            b3.low < b1.low and             # 头 < 左肩
            b5.low > b3.low and             # 右肩 > 头
            _near(b1.low, b5.low, 5.0)):    # 左右肩近似
        neckline = max(b2.high, b4.high)
        power_decay = b5.power_price < b3.power_price
        broken = b6.high > neckline or b7.high > neckline
        if power_decay and broken:
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b7.sdt, signal_type="形态:头肩底",
                confidence=0.75,
                price=neckline,
                details=f"左肩 {b1.low:.2f} 头 {b3.low:.2f} "
                        f"右肩 {b5.low:.2f}，颈线 {neckline:.2f} 已破",
            ))

    return signals


def _detect_triangle(bis, symbol: str, freq_val: str) -> List[SignalEvent]:
    """
    上升三角：顶部平坦（阻力），底部递升（支撑上移）。
    下降三角：底部平坦（支撑），顶部递降（阻力下移）。

    使用最近7笔中的向上/向下笔分别提取高低点分析。
    """
    signals = []
    recent = bis[-7:]

    up_bis = [b for b in recent if b.direction == Direction.Up]
    down_bis = [b for b in recent if b.direction == Direction.Down]

    if len(up_bis) < 3 or len(down_bis) < 3:
        return signals

    up_highs = [b.high for b in up_bis]
    down_lows = [b.low for b in down_bis]

    # ── 上升三角 ──
    flat_top = all(_near(h, up_highs[0], 2.0) for h in up_highs[1:])
    rising_lows = all(down_lows[i] >= down_lows[i - 1] * 0.99
                      for i in range(1, len(down_lows)))
    meaningful_rise = (down_lows[-1] - down_lows[0]) / (down_lows[0] + 1e-9) * 100 > 1.0

    if flat_top and rising_lows and meaningful_rise:
        resistance = max(up_highs)
        last_bi = bis[-1]
        near_breakout = last_bi.direction == Direction.Up and last_bi.high >= resistance * 0.98
        conf = 0.75 if near_breakout else 0.60
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=last_bi.edt, signal_type="形态:上升三角",
            confidence=conf,
            price=resistance,
            details=f"阻力 {resistance:.2f}（平顶），"
                    f"低点递升 {down_lows[0]:.2f}→{down_lows[-1]:.2f}"
                    + ("，接近突破" if near_breakout else ""),
        ))

    # ── 下降三角 ──
    flat_bottom = all(_near(l, down_lows[0], 2.0) for l in down_lows[1:])
    falling_highs = all(up_highs[i] <= up_highs[i - 1] * 1.01
                        for i in range(1, len(up_highs)))
    meaningful_fall = (up_highs[0] - up_highs[-1]) / (up_highs[0] + 1e-9) * 100 > 1.0

    if flat_bottom and falling_highs and meaningful_fall:
        support = min(down_lows)
        last_bi = bis[-1]
        near_breakdown = last_bi.direction == Direction.Down and last_bi.low <= support * 1.02
        conf = 0.75 if near_breakdown else 0.60
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=last_bi.edt, signal_type="形态:下降三角",
            confidence=conf,
            price=support,
            details=f"支撑 {support:.2f}（平底），"
                    f"高点递降 {up_highs[0]:.2f}→{up_highs[-1]:.2f}"
                    + ("，接近破位" if near_breakdown else ""),
        ))

    return signals
