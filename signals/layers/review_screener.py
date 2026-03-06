# -*- coding: utf-8 -*-
"""
盘后复盘工具函数

提供个股日线加载和复盘分析功能，供 run.py 的 run_review() 调用。
支持 A股（AKShare → Tushare SSL降级）和 美股（USDataSource）。
"""
from typing import List, Optional

import pandas as pd
from czsc import Freq, RawBar


# ─────────────────────────────────────────────────────────
# 个股日线加载工具
# ─────────────────────────────────────────────────────────

# TODO: Tushare 充值后恢复
# def _futu_to_tushare(futu_code: str) -> str:
#     """SH.601958 → 601958.SH"""
#     mkt, code = futu_code.split(".")
#     return f"{code}.{mkt}"


def _load_stock_daily_bars(futu_code: str, start_date: str,
                            end_date: str = None) -> List[RawBar]:
    """
    加载个股日线，供盘后复盘使用。
    支持 A股（SH/SZ/BJ）和 美股（US）。

    A股路径：AKShare（免费）。
    美股路径：USDataSource（降级链）。
    """
    from signals.data.fetcher import AKShareSource, USDataSource, detect_market
    from datetime import datetime
    edt = end_date or datetime.now().strftime("%Y-%m-%d")
    market = detect_market(futu_code)

    # ── 美股路径 ──
    if market == "US":
        try:
            us = USDataSource()
            return us.get_us_daily(futu_code)
        except Exception as e:
            print(f"  [✗] {futu_code} 美股日线加载失败：{e}", flush=True)
            return []

    # ── A股路径：AKShare ──
    ak_src = AKShareSource()
    try:
        bars = ak_src.get_a_daily(futu_code, sdt=start_date, edt=edt)
        if bars:
            return bars
    except Exception as e:
        print(f"  [✗] {futu_code} AKShare日线失败：{type(e).__name__}: {e}", flush=True)

    # TODO: Tushare 充值后恢复日线降级
    # 当前 token 等级限制严格，暂时跳过
    print(f"  [✗] {futu_code} 无可用日线数据", flush=True)
    return []


# ─────────────────────────────────────────────────────────
# 日线 → 周线合成
# ─────────────────────────────────────────────────────────

def resample_daily_to_weekly(bars: List[RawBar], symbol: str) -> List[RawBar]:
    """
    从日线 RawBar 列表合成周线 RawBar 列表。
    按周五收盘聚合，open=首日, high=最高, low=最低, close=末日, vol=累计。
    """
    if len(bars) < 5:
        return []

    df = pd.DataFrame([{
        "dt": b.dt, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "vol": b.vol, "amount": b.amount,
    } for b in bars])
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.set_index("dt")

    weekly = df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "vol": "sum", "amount": "sum",
    }).dropna()

    result = []
    for i, (dt, row) in enumerate(weekly.iterrows()):
        result.append(RawBar(
            symbol=symbol, dt=dt, id=i, freq=Freq.W,
            open=round(row["open"], 2),
            high=round(row["high"], 2),
            low=round(row["low"], 2),
            close=round(row["close"], 2),
            vol=int(row["vol"]),
            amount=int(row["amount"]),
        ))
    return result


# ─────────────────────────────────────────────────────────
# 个股日线+周线复盘（独立函数）
# ─────────────────────────────────────────────────────────

def _load_stock_minute_bars(futu_code: str, freq: Freq) -> List[RawBar]:
    """
    加载个股分钟线（最近5天），供盘后复盘的"当下补充"。
    A股路径：AKShare(Sina) → AKShare(东财)。
    """
    from signals.data.fetcher import AKShareSource, detect_market
    market = detect_market(futu_code)
    if market != "A":
        return []  # 暂只支持A股
    ak_src = AKShareSource()
    try:
        bars = ak_src.get_a_minute(futu_code, freq)
        if bars:
            return bars
    except Exception:
        pass
    # 降级：东财分钟线
    try:
        bars = ak_src.get_a_minute_em(futu_code, freq, max_retries=1)
        if bars:
            return bars
    except Exception:
        pass
    return []


def review_stock_daily(symbols: List[str], start_date: str,
                       with_minute: bool = True) -> list:
    """
    对指定个股做多级别缠论分析（盘后复盘）。

    三级联动：周线(趋势背景) + 日线(结构确认) + 30min(当下定位)
    信号自带时间衰减，老信号自动降权。

    :param symbols: Futu格式代码列表
    :param start_date: 'YYYY-MM-DD'
    :param with_minute: 是否追加30min分析（默认True）
    :return: List[ScoredSymbol] 按评分降序
    """
    from signals.core.analyzer import SymbolAnalyzer
    from signals.core.detectors import detect_all_signals
    from signals.core.scorer import score_signals

    if not symbols:
        print("  跳过个股复盘（未指定标的）", flush=True)
        return []

    level_desc = "周线+日线+30min" if with_minute else "日线+周线"
    print(f"\n>>> Layer 3 个股复盘（{level_desc}）：{len(symbols)} 只 ...", flush=True)
    scored: list = []
    for sym in symbols:
        bars = _load_stock_daily_bars(sym, start_date)
        if not bars:
            print(f"  [✗] {sym}: 无数据", flush=True)
            continue
        try:
            # 日线分析
            az_d = SymbolAnalyzer(sym, Freq.D, bars, max_bi_num=200)
            sigs_d = detect_all_signals(az_d.czsc, sym)

            # 周线分析（从日线合成）
            bars_w = resample_daily_to_weekly(bars, sym)
            sigs_w = []
            w_bi_cnt = 0
            if bars_w and len(bars_w) >= 10:
                az_w = SymbolAnalyzer(sym, Freq.W, bars_w, max_bi_num=100)
                sigs_w = detect_all_signals(az_w.czsc, sym)
                w_bi_cnt = len(az_w.finished_bis)

            # 30min 当下补充（最近5天）
            sigs_30 = []
            m_bi_cnt = 0
            m_bar_cnt = 0
            if with_minute:
                bars_30 = _load_stock_minute_bars(sym, Freq.F30)
                if bars_30:
                    m_bar_cnt = len(bars_30)
                    az_30 = SymbolAnalyzer(sym, Freq.F30, bars_30)
                    sigs_30 = detect_all_signals(az_30.czsc, sym)
                    m_bi_cnt = len(az_30.finished_bis)

            # 合并全部信号（含时间衰减）
            all_signals = sigs_d + sigs_w + sigs_30
            sc = score_signals(sym, all_signals)
            scored.append(sc)

            sig_str = f"得分={sc.total_score:+.0f}" if sc.total_score != 0 else "无信号"
            dir_tag = f" [{sc.direction}]" if sc.direction else ""
            w_info = f"  {len(bars_w)}根周线 {w_bi_cnt}笔" if bars_w else ""
            m_info = f"  {m_bar_cnt}根30M {m_bi_cnt}笔" if m_bar_cnt else ""
            print(f"  [✓] {sym}: {len(bars)}根日线 {len(az_d.finished_bis)}笔"
                  f"{w_info}{m_info}  {sig_str}{dir_tag}", flush=True)
        except Exception as e:
            print(f"  [✗] {sym}: 分析失败 {e}", flush=True)

    scored.sort(key=lambda x: -x.total_score)
    _print_stock_report(scored)
    return scored


def _print_stock_report(scored: list):
    """输出个股复盘排名表。"""
    if not scored:
        print("  无个股评分结果", flush=True)
        return
    print("\n" + "─" * 60)
    print("  个股复盘排名（周线+日线+30min 三级联动）")
    print("─" * 60)
    for i, sc in enumerate(scored, 1):
        sigs = ", ".join(f"{s.signal_type}({s.freq})" for s in sc.signals[:4])
        dir_tag = f" [{sc.direction}]" if sc.direction else ""
        print(f"  {i:2d}. {sc.symbol:15s}  分={sc.total_score:+6.1f}{dir_tag}  {sigs}")
    print("─" * 60 + "\n")
