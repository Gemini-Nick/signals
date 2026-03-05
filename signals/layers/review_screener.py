# -*- coding: utf-8 -*-
"""
盘后复盘工具函数

提供个股日线加载和复盘分析功能，供 run.py 的 run_review() 调用。
支持 A股（AKShare → Tushare SSL降级）和 美股（USDataSource）。
"""
from typing import List, Optional

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
# 个股日线复盘（独立函数）
# ─────────────────────────────────────────────────────────

def review_stock_daily(symbols: List[str], start_date: str) -> list:
    """
    对指定个股做日线级别缠论分析（盘后复盘）。

    :param symbols: Futu格式代码列表
    :param start_date: 'YYYY-MM-DD'
    :return: List[ScoredSymbol] 按评分降序
    """
    from signals.core.analyzer import SymbolAnalyzer
    from signals.core.detectors import detect_all_signals
    from signals.core.scorer import score_signals

    if not symbols:
        print("  跳过个股复盘（未指定标的）", flush=True)
        return []

    print(f"\n>>> Layer 3 个股复盘（日线）：{len(symbols)} 只 ...", flush=True)
    scored: list = []
    for sym in symbols:
        bars = _load_stock_daily_bars(sym, start_date)
        if not bars:
            print(f"  [✗] {sym}: 无数据", flush=True)
            continue
        try:
            az = SymbolAnalyzer(sym, Freq.D, bars, max_bi_num=200)
            signals = detect_all_signals(az.czsc, sym)
            sc = score_signals(sym, signals)
            scored.append(sc)
            sig_str = f"得分={sc.total_score:+.0f}" if sc.total_score != 0 else "无信号"
            print(f"  [✓] {sym}: {len(bars)}根日线  {len(az.finished_bis)}笔  {sig_str}",
                  flush=True)
        except Exception as e:
            print(f"  [✗] {sym}: 分析失败 {e}", flush=True)

    scored.sort(key=lambda x: -x.total_score)
    _print_stock_report(scored)

    # 信号存档（回测验证用，异常不影响主流程）
    try:
        from signals.core.backtest import archive_signals
        archive_signals(scored)
    except Exception:
        pass

    return scored


def _print_stock_report(scored: list):
    """输出个股复盘排名表。"""
    if not scored:
        print("  无个股评分结果", flush=True)
        return
    print("\n" + "─" * 52)
    print("  个股复盘排名（日线级别）")
    print("─" * 52)
    for i, sc in enumerate(scored, 1):
        sigs = ", ".join(f"{s.signal_type}({s.freq})" for s in sc.signals[:3])
        print(f"  {i:2d}. {sc.symbol:15s}  分={sc.total_score:+6.1f}  {sigs}")
    print("─" * 52 + "\n")
