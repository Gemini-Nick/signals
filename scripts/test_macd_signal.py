# -*- coding: utf-8 -*-
"""
MACD 信号验证脚本 — 用模拟数据回放验证算法

基于截图中的真实 MACD 数值构造合理的价格序列，验证信号检测逻辑。

截图数据:
  天际股份 周K: 价格 5→56→36, DEA=6.096, HIST=-3.379, MA5=37.18, MA60=19.57
  阿里巴巴 周K: 价格 70→186→131, DEA=4.570, HIST=-8.564, MA5=141.5, MA60=133.6
  腾讯 周K: 价格 344→683→546, DEA=-1.224, HIST=-25.054, MA60=549.7
  腾讯 日K: 价格 662→499→546, DEA=-16.833, HIST=+13.450

用法: python scripts/test_macd_signal.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from signals.core.macd_detector import compute_macd, detect_macd_signals, MACDSignal


def generate_trend_then_pullback(n_up=40, n_down=15, start_price=10,
                                  peak_price=50, end_price=35,
                                  start_date="2024-06-01", freq="W"):
    """
    生成先涨后回调的价格序列（模拟截图中的K线走势）。
    返回 DataFrame with open/high/low/close, datetime index.
    """
    dates = pd.date_range(start=start_date, periods=n_up + n_down, freq=freq)

    # 上涨段：从 start_price 到 peak_price
    up_prices = np.linspace(start_price, peak_price, n_up)
    # 添加一些随机波动
    noise = np.random.normal(0, (peak_price - start_price) * 0.02, n_up)
    up_prices = up_prices + noise

    # 下跌段：从 peak_price 到 end_price
    down_prices = np.linspace(peak_price, end_price, n_down)
    noise = np.random.normal(0, (peak_price - end_price) * 0.03, n_down)
    down_prices = down_prices + noise

    closes = np.concatenate([up_prices, down_prices])
    highs = closes * (1 + np.random.uniform(0.005, 0.03, len(closes)))
    lows = closes * (1 - np.random.uniform(0.005, 0.03, len(closes)))
    opens = closes * (1 + np.random.uniform(-0.015, 0.015, len(closes)))

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes
    }, index=dates)
    return df


def generate_bottom_recovery(n_down=30, n_recovery=15,
                              start_price=660, bottom_price=499,
                              end_price=546, start_date="2025-09-15", freq="B"):
    """
    生成V形底部（先快速下跌后逐步回升）。
    模拟腾讯日K: 662→499→546，没有长期横盘（更符合实际走势）。
    """
    total = n_down + n_recovery
    dates = pd.date_range(start=start_date, periods=total, freq=freq)

    # 下跌段：先缓后急
    down_t = np.linspace(0, 1, n_down)
    down = start_price - (start_price - bottom_price) * (down_t ** 0.7)  # 凸函数，先慢后快

    # 回升段：先急后缓
    up_t = np.linspace(0, 1, n_recovery)
    up = bottom_price + (end_price - bottom_price) * (1 - (1 - up_t) ** 2)  # 凹函数

    closes = np.concatenate([down, up])
    noise = np.random.normal(0, abs(start_price - bottom_price) * 0.005, len(closes))
    closes = closes + noise

    highs = closes * (1 + np.random.uniform(0.003, 0.015, len(closes)))
    lows = closes * (1 - np.random.uniform(0.003, 0.015, len(closes)))
    opens = closes * (1 + np.random.uniform(-0.008, 0.008, len(closes)))

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes
    }, index=dates[:len(closes)])
    return df


def print_macd_tail(df: pd.DataFrame, label: str, last_n: int = 20):
    """打印最后N根K线的 MACD 状态"""
    macd = compute_macd(df["close"])
    df_show = df.tail(last_n).copy()
    df_show["dif"] = macd["dif"]
    df_show["dea"] = macd["dea"]
    df_show["hist"] = macd["hist"]

    print(f"\n{'='*85}")
    print(f"  {label}  最后 {last_n} 根K线 MACD 状态")
    print(f"{'='*85}")
    print(f"  {'日期':>12}  {'收盘':>8}  {'DIF':>8}  {'DEA':>8}  {'HIST':>8}  状态")
    print(f"  {'─'*75}")

    for dt, row in df_show.iterrows():
        hist = row["hist"]
        dea = row["dea"]
        bar_status = "红柱" if hist > 0 else "绿柱"
        zone = "零上" if dea > 0 else "零下"

        # 标记扩大/缩小
        trend = ""
        dt_idx = df_show.index.get_loc(dt)
        if dt_idx > 0:
            prev_hist = df_show.iloc[dt_idx - 1]["hist"]
            if hist < 0 and prev_hist < 0:
                if abs(hist) > abs(prev_hist):
                    trend = "↓扩大"
                elif abs(hist) < abs(prev_hist):
                    trend = "↑缩小"

        print(f"  {dt.strftime('%Y-%m-%d'):>12}  {row['close']:>8.2f}  "
              f"{row['dif']:>8.3f}  {row['dea']:>8.3f}  {hist:>8.3f}  "
              f"{bar_status}|{zone} {trend}")


def run_detection_test(df: pd.DataFrame, symbol: str, freq: str, label: str):
    """回放检测信号"""
    print(f"\n{'─'*85}")
    print(f"  {label}  信号检测结果")
    print(f"{'─'*85}")

    # 检测信号（lookback=30 覆盖回调阶段）
    signals = detect_macd_signals(df, symbol, freq, lookback=30)

    if not signals:
        print(f"  无信号触发")
    else:
        for sig in signals:
            print(f"  [{sig.dt.strftime('%Y-%m-%d')}] {sig.pattern}  "
                  f"置信度={sig.confidence:.2f}  价格={sig.price:.2f}  "
                  f"DEA={sig.dea:.3f}  HIST={sig.hist:.3f}")
            print(f"    → {sig.details}")
        print(f"\n  共 {len(signals)} 个信号")


def main():
    np.random.seed(42)

    print("=" * 85)
    print("  MACD 信号验证 — 模拟数据回放测试")
    print("  时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 85)

    # ── Case 1: 天际股份周K — Pattern A（零上回踩+支撑）──
    # 从~5涨到56, 回调到36, DEA仍>0, 绿柱扩大中, MA60≈19.57
    print("\n\n>>> Case 1: 天际股份周K — 期望: Pattern A（零上回踩到支撑）")
    df1 = generate_trend_then_pullback(
        n_up=50, n_down=12, start_price=5, peak_price=56,
        end_price=36, start_date="2024-01-01", freq="W"
    )
    print_macd_tail(df1, "天际股份 周线")
    run_detection_test(df1, "SZ.002759", "周线", "天际股份 周线")

    # ── Case 2: 阿里巴巴周K — Pattern A ──
    # 从70涨到186, 回调到131
    print("\n\n>>> Case 2: 阿里巴巴周K — 期望: Pattern A（零上回踩）")
    df2 = generate_trend_then_pullback(
        n_up=50, n_down=10, start_price=70, peak_price=186,
        end_price=131, start_date="2024-01-01", freq="W"
    )
    print_macd_tail(df2, "阿里巴巴 周线")
    run_detection_test(df2, "HK.09988", "周线", "阿里巴巴 周线")

    # ── Case 3: 腾讯周K — Pattern A（DEA可能已穿0轴）──
    # 从344涨到683, 回调到546, DEA=-1.224
    print("\n\n>>> Case 3: 腾讯周K — 期望: Pattern A 或 Pattern B（DEA≈0附近）")
    df3 = generate_trend_then_pullback(
        n_up=50, n_down=15, start_price=344, peak_price=683,
        end_price=546, start_date="2024-01-01", freq="W"
    )
    print_macd_tail(df3, "腾讯控股 周线")
    run_detection_test(df3, "HK.00700", "周线", "腾讯控股 周线")

    # ── Case 4: 腾讯日K — Pattern B（零下企稳）──
    # 从662跌到499, 然后V形反弹到546, DEA<0但绿柱缩小/红柱出现
    print("\n\n>>> Case 4: 腾讯日K — 期望: Pattern B（零下企稳）")
    df4 = generate_bottom_recovery(
        n_down=30, n_recovery=15,
        start_price=662, bottom_price=499, end_price=546,
        start_date="2025-09-15", freq="B"
    )
    print_macd_tail(df4, "腾讯控股 日线")
    run_detection_test(df4, "HK.00700", "日线", "腾讯控股 日线")

    # ── Case 5: 反例 — 持续上涨，不应有信号 ──
    print("\n\n>>> Case 5: 反例 — 持续上涨（不应触发信号）")
    dates = pd.date_range(start="2024-01-01", periods=60, freq="W")
    prices = np.linspace(10, 80, 60) + np.random.normal(0, 0.5, 60)
    df5 = pd.DataFrame({
        "open": prices * 0.99, "high": prices * 1.02,
        "low": prices * 0.98, "close": prices
    }, index=dates)
    print_macd_tail(df5, "反例：持续上涨")
    run_detection_test(df5, "TEST.000001", "周线", "反例")

    # ── Case 6: 反例 — 持续下跌，绿柱持续扩大，不应有 Pattern B ──
    print("\n\n>>> Case 6: 反例 — 持续下跌（Pattern B 不应触发）")
    dates = pd.date_range(start="2024-01-01", periods=60, freq="B")
    prices = np.linspace(100, 40, 60) + np.random.normal(0, 0.5, 60)
    df6 = pd.DataFrame({
        "open": prices * 1.01, "high": prices * 1.02,
        "low": prices * 0.98, "close": prices
    }, index=dates)
    print_macd_tail(df6, "反例：持续下跌")
    run_detection_test(df6, "TEST.000002", "日线", "反例")

    print("\n" + "=" * 85)
    print("  验证完成")
    print("=" * 85)


if __name__ == "__main__":
    main()
