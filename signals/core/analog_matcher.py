# -*- coding: utf-8 -*-
"""
P3-6: 历史形态匹配（独立模式）

Pearson 相关系数匹配当前收益率序列与历史收益率序列，
找到最相似的历史区间，输出后续走势参考。

独立运行：python run.py --mode analog [--symbol 沪深300]
结果缓存到 .data/cache/analog_latest.json

用法：
    from signals.core.analog_matcher import find_analogs, HistoricalAnalog
    analogs = find_analogs(current_bars, history_bars, window=30, top_k=3)
"""
import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional

import config


@dataclass
class HistoricalAnalog:
    """历史形态匹配结果"""
    match_start: str         # 匹配区间起始日期
    match_end: str           # 匹配区间结束日期
    index_name: str          # 指数名称
    similarity: float        # Pearson 相关系数
    window_days: int         # 匹配窗口长度
    next_10d_return: float   # 匹配后10个交易日收益率 %
    next_30d_return: float   # 匹配后30个交易日收益率 %
    what_happened: str       # 后续发生了什么
    key_observation: str     # 关键观察


_CACHE_PATH = ".data/cache/analog_latest.json"


def _returns_from_closes(closes: list) -> list:
    """收盘价序列 → 收益率序列"""
    if len(closes) < 2:
        return []
    return [(closes[i] / closes[i - 1] - 1) * 100
            for i in range(1, len(closes))]


def _pearson_corr(x: list, y: list) -> float:
    """计算 Pearson 相关系数"""
    n = len(x)
    if n < 3:
        return 0.0

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))

    if den_x == 0 or den_y == 0:
        return 0.0

    return num / (den_x * den_y)


def _describe_outcome(next_returns: list, window: int) -> str:
    """根据后续收益率描述发生了什么"""
    if not next_returns:
        return "数据不足"

    total_return = 1.0
    for r in next_returns:
        total_return *= (1 + r / 100)
    total_return = (total_return - 1) * 100

    max_drawdown = 0.0
    peak = 1.0
    val = 1.0
    for r in next_returns:
        val *= (1 + r / 100)
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        if dd > max_drawdown:
            max_drawdown = dd

    parts = []
    if total_return > 5:
        parts.append(f"上涨{total_return:.1f}%")
    elif total_return < -5:
        parts.append(f"下跌{abs(total_return):.1f}%")
    else:
        parts.append(f"震荡(累计{total_return:+.1f}%)")

    if max_drawdown > 5:
        parts.append(f"最大回撤{max_drawdown:.1f}%")

    return ", ".join(parts)


def find_analogs(
    current_closes: list,
    current_dates: list,
    history_closes: list,
    history_dates: list,
    index_name: str = "",
    window: int = None,
    top_k: int = None,
    min_similarity: float = None,
) -> List[HistoricalAnalog]:
    """
    在历史数据中寻找与当前走势最相似的区间。

    :param current_closes: 当前最近 window 天的收盘价列表
    :param current_dates: 当前日期列表（与 closes 对应）
    :param history_closes: 完整历史收盘价列表
    :param history_dates: 历史日期列表（与 closes 对应）
    :param index_name: 指数名称
    :param window: 匹配窗口，默认 config.ANALOG_WINDOW
    :param top_k: 返回 Top K，默认 config.ANALOG_TOP_K
    :param min_similarity: 最低相似度，默认 config.ANALOG_MIN_SIMILARITY
    :return: HistoricalAnalog 列表（按相似度降序）
    """
    window = window or config.ANALOG_WINDOW
    top_k = top_k or config.ANALOG_TOP_K
    min_similarity = min_similarity or config.ANALOG_MIN_SIMILARITY

    # 当前走势的收益率序列
    if len(current_closes) < window:
        return []
    current_ret = _returns_from_closes(current_closes[-window:])
    if len(current_ret) < window - 1:
        return []

    # 在历史数据中滑动窗口匹配
    hist_ret = _returns_from_closes(history_closes)
    if len(hist_ret) < window + 30:
        return []

    candidates = []

    # 避免匹配最近60天（与当前重叠）
    search_end = len(hist_ret) - 60

    for i in range(0, search_end - window + 1):
        segment = hist_ret[i:i + window - 1]
        if len(segment) != len(current_ret):
            continue

        corr = _pearson_corr(current_ret, segment)
        if corr >= min_similarity:
            candidates.append((i, corr))

    if not candidates:
        return []

    # 按相似度排序，去重（相邻窗口只保留最佳）
    candidates.sort(key=lambda x: -x[1])
    filtered = []
    used_ranges = set()
    for idx, corr in candidates:
        # 检查是否与已选的重叠（±10天内）
        overlap = False
        for used_idx in used_ranges:
            if abs(idx - used_idx) < 10:
                overlap = True
                break
        if not overlap:
            filtered.append((idx, corr))
            used_ranges.add(idx)
        if len(filtered) >= top_k:
            break

    # 构建结果
    results = []
    for idx, corr in filtered:
        # 匹配区间日期
        match_start_idx = idx + 1  # +1 因为 returns 比 closes 少1
        match_end_idx = idx + window
        if match_end_idx >= len(history_dates):
            continue

        match_start = str(history_dates[match_start_idx])[:10]
        match_end = str(history_dates[match_end_idx])[:10]

        # 后续走势
        post_start = match_end_idx + 1
        post_10 = history_closes[post_start:post_start + 10]
        post_30 = history_closes[post_start:post_start + 30]

        next_10d = 0.0
        if len(post_10) >= 2:
            next_10d = round((post_10[-1] / post_10[0] - 1) * 100, 2)

        next_30d = 0.0
        if len(post_30) >= 2:
            next_30d = round((post_30[-1] / post_30[0] - 1) * 100, 2)

        # 后续走势描述
        post_ret = _returns_from_closes(post_30) if len(post_30) >= 2 else []
        what_happened = _describe_outcome(post_ret, 30)

        # 关键观察
        obs_parts = []
        if next_10d > 3:
            obs_parts.append(f"短期(10日)反弹{next_10d:.1f}%")
        elif next_10d < -3:
            obs_parts.append(f"短期(10日)继续下跌{abs(next_10d):.1f}%")
        if next_30d > 5:
            obs_parts.append(f"中期(30日)上涨{next_30d:.1f}%")
        elif next_30d < -5:
            obs_parts.append(f"中期(30日)下跌{abs(next_30d):.1f}%")
        key_observation = ", ".join(obs_parts) if obs_parts else "走势温和"

        results.append(HistoricalAnalog(
            match_start=match_start,
            match_end=match_end,
            index_name=index_name,
            similarity=round(corr, 4),
            window_days=window,
            next_10d_return=next_10d,
            next_30d_return=next_30d,
            what_happened=what_happened,
            key_observation=key_observation,
        ))

    return results


def save_analog_results(results: dict) -> None:
    """保存匹配结果到缓存文件"""
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    data = {
        "timestamp": datetime.now().isoformat(),
        "results": results,
    }
    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_analog_results() -> Optional[dict]:
    """加载缓存的匹配结果"""
    if not os.path.exists(_CACHE_PATH):
        return None
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def analog_to_dict(analog: HistoricalAnalog) -> dict:
    """HistoricalAnalog → dict（JSON 序列化用）"""
    return asdict(analog)
