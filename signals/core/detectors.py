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
    signals.extend(_detect_first_bs(czsc_obj, symbol))
    signals.extend(_detect_second_bs(czsc_obj, symbol))
    signals.extend(_detect_third_bs(czsc_obj, symbol))
    signals.extend(_detect_divergence(czsc_obj, symbol))
    signals.extend(_detect_trend(czsc_obj, symbol))
    return signals


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

    # 二买：b1↓ b3↓ b5↑，b3 低点高于 b1 低点 且 b3 高点低于 b1 高点（完整结构确认）
    if (b1.direction == Direction.Down and
            b3.direction == Direction.Down and
            b5.direction == Direction.Up and
            b3.low > b1.low and
            b3.high < b1.high):  # 高点也在下降才是真二买
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
            details=f"低点抬升 {b3.low:.2f}>{b1.low:.2f}，高点递降 {b3.high:.2f}<{b1.high:.2f}，回撤 {retracement:.1%}",
        ))

    # 二卖：b1↑ b3↑ b5↓，b3 高点低于 b1 高点 且 b3 低点高于 b1 低点
    if (b1.direction == Direction.Up and
            b3.direction == Direction.Up and
            b5.direction == Direction.Down and
            b3.high < b1.high and
            b3.low > b1.low):  # 低点也在抬升才是真二卖
        b2_range = b2.high - b2.low
        b3_range = b3.high - b3.low
        retracement = b3_range / (b2_range + 1e-9)
        conf = 0.80 if retracement < 0.618 else 0.60
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=b5.sdt, signal_type="二卖",
            confidence=conf,
            price=b5.fx_a.fx,
            details=f"高点递降 {b3.high:.2f}<{b1.high:.2f}，低点抬升 {b3.low:.2f}>{b1.low:.2f}，反弹比 {retracement:.1%}",
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

    # 三买：b4 向上离开中枢（b4.low > zs_zg 确认离开），b5↓ 回调但低点 > 中枢上沿
    if b4.direction == Direction.Up and b5.direction == Direction.Down:
        b4_left = b4.low > zs_zg  # b4 整根笔在中枢上方 = 确认离开
        if b5.low > zs_zg and b4_left:
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

    # 三卖：b4 向下离开中枢（b4.high < zs_zd 确认离开），b5↑ 反弹但高点 < 中枢下沿
    if b4.direction == Direction.Down and b5.direction == Direction.Up:
        b4_left = b4.high < zs_zd  # b4 整根笔在中枢下方 = 确认离开
        if b5.high < zs_zd and b4_left:
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
