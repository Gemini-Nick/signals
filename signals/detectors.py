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

    # 二买：b1↓ b3↓ b5↑，b3 低点高于 b1 低点
    if (b1.direction == Direction.Down and
            b3.direction == Direction.Down and
            b5.direction == Direction.Up and
            b3.low > b1.low):
        # 回撤比例：b3.low 相对 b1.high 的位置
        retracement = (b3.high - b3.low) / (b3.high - b1.low + 1e-9)
        conf = 0.85 if retracement < 0.618 else 0.65
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=b5.sdt, signal_type="二买",
            confidence=conf,
            price=b5.fx_a.fx,
            details=f"回调低点 {b3.low:.2f} > 前低 {b1.low:.2f}，回撤 {retracement:.1%}",
        ))

    # 二卖：b1↑ b3↑ b5↓，b3 高点低于 b1 高点
    if (b1.direction == Direction.Up and
            b3.direction == Direction.Up and
            b5.direction == Direction.Down and
            b3.high < b1.high):
        retracement = (b3.high - b3.low) / (b1.high - b3.low + 1e-9)
        conf = 0.85 if retracement < 0.618 else 0.65
        signals.append(SignalEvent(
            symbol=symbol, freq=freq_val,
            dt=b5.sdt, signal_type="二卖",
            confidence=conf,
            price=b5.fx_a.fx,
            details=f"反弹高点 {b3.high:.2f} < 前高 {b1.high:.2f}，反弹比 {retracement:.1%}",
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

    # 三买：b4 向上离开中枢，b5↓ 回调但低点 > 中枢上沿
    if b4.direction == Direction.Up and b5.direction == Direction.Down:
        if b5.low > zs_zg:
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b5.edt, signal_type="三买",
                confidence=0.85,
                price=b5.low,
                details=f"回调低点 {b5.low:.2f} > 中枢上沿 {zs_zg:.2f}",
            ))

    # 三卖：b4 向下离开中枢，b5↑ 反弹但高点 < 中枢下沿
    if b4.direction == Direction.Down and b5.direction == Direction.Up:
        if b5.high < zs_zd:
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=b5.edt, signal_type="三卖",
                confidence=0.85,
                price=b5.high,
                details=f"反弹高点 {b5.high:.2f} < 中枢下沿 {zs_zd:.2f}",
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

    # 背驰卖：都是向上笔，创新高但力度减弱
    if last.direction == Direction.Up:
        if last.high > prev_same.high and last.power_price < prev_same.power_price:
            vol_div = last.power_volume < prev_same.power_volume
            conf = 0.75 if vol_div else 0.6
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=last.edt, signal_type="背驰卖",
                confidence=conf,
                price=last.high,
                details=(f"价创新高 {last.high:.2f}>{prev_same.high:.2f}，"
                         f"力度 {last.power_price:.2f}<{prev_same.power_price:.2f}"
                         + ("，量能也背驰" if vol_div else "")),
            ))

    # 背驰买：都是向下笔，创新低但力度减弱
    if last.direction == Direction.Down:
        if last.low < prev_same.low and last.power_price < prev_same.power_price:
            vol_div = last.power_volume < prev_same.power_volume
            conf = 0.75 if vol_div else 0.6
            signals.append(SignalEvent(
                symbol=symbol, freq=freq_val,
                dt=last.edt, signal_type="背驰买",
                confidence=conf,
                price=last.low,
                details=(f"价创新低 {last.low:.2f}<{prev_same.low:.2f}，"
                         f"力度 {last.power_price:.2f}<{prev_same.power_price:.2f}"
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
