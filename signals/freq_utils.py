# -*- coding: utf-8 -*-
from czsc import Freq

FREQ_MAP = {
    "1min":  Freq.F1,
    "5min":  Freq.F5,
    "15min": Freq.F15,
    "30min": Freq.F30,
    "60min": Freq.F60,
    "daily": Freq.D,
}


def config_freq_to_czsc(freq_str: str) -> Freq:
    """将 config.MONITOR_FREQS 字符串转换为 czsc.Freq 枚举。"""
    if freq_str not in FREQ_MAP:
        raise KeyError(f"未知频率 '{freq_str}'，支持: {list(FREQ_MAP.keys())}")
    return FREQ_MAP[freq_str]
