# -*- coding: utf-8 -*-
"""
盘后复盘工具函数

提供个股日线加载和复盘分析功能，供 run.py 的 run_review() 调用。
支持 A股（AKShare 东财→Sina 降级链）和 美股（USDataSource）。
"""
import threading
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd
from czsc import Freq, RawBar

from signals.data.bar_cache import get_cache, DiskBarCache


# ─────────────────────────────────────────────────────────
# Dashboard-aware logging
# ─────────────────────────────────────────────────────────

import logging as _logging
_file_log = _logging.getLogger("signals.review_screener")

def _log(msg: str):
    from signals.dashboard import get_dashboard
    dash = get_dashboard()
    if dash:
        dash.log(msg)
    else:
        print(msg, flush=True)
    _file_log.info(msg)


# ─────────────────────────────────────────────────────────
# Thread-safe 降级计数器（模块级）
# ─────────────────────────────────────────────────────────

_counter_lock = threading.Lock()

# 东财 SSL 连续失败计数器
_em_ssl_fails: int = 0
_EM_SKIP_THRESHOLD: int = 3
_em_switched: bool = False

# Sina 连续失败计数器
_sina_fails: int = 0
_SINA_SKIP_THRESHOLD: int = 3
_sina_switched: bool = False


def _is_ssl_error(e: Exception) -> bool:
    """判断是否为 SSL 相关错误"""
    err_str = f"{type(e).__name__}: {e}"
    return "SSL" in err_str or "SSLError" in type(e).__name__


def _reset_em_counter():
    """东财请求成功时重置计数器"""
    global _em_ssl_fails, _em_switched
    with _counter_lock:
        _em_ssl_fails = 0
        _em_switched = False


def _bump_em_fail(context: str = "日线"):
    """东财 SSL 失败时递增计数，达阈值时输出一次切换提示"""
    global _em_ssl_fails, _em_switched
    with _counter_lock:
        _em_ssl_fails += 1
        if _em_ssl_fails >= _EM_SKIP_THRESHOLD and not _em_switched:
            _em_switched = True
            _log(f"  [!] 东财{context}接口连续{_em_ssl_fails}次SSL失败，"
                 f"自动切换 Sina 源")


def _reset_sina_counter():
    """Sina 请求成功时重置计数器"""
    global _sina_fails, _sina_switched
    with _counter_lock:
        _sina_fails = 0
        _sina_switched = False


def _bump_sina_fail():
    """Sina 失败时递增计数，达阈值时输出一次提示"""
    global _sina_fails, _sina_switched
    with _counter_lock:
        _sina_fails += 1
        if _sina_fails >= _SINA_SKIP_THRESHOLD and not _sina_switched:
            _sina_switched = True
            _log(f"  [!] Sina接口连续{_sina_fails}次失败，跳过 Sina 源")


def _reset_all_counters():
    """重置所有熔断计数器（每次 review 入口调用，防多轮残留）"""
    global _em_ssl_fails, _em_switched, _sina_fails, _sina_switched
    with _counter_lock:
        _em_ssl_fails = 0
        _em_switched = False
        _sina_fails = 0
        _sina_switched = False


# ─────────────────────────────────────────────────────────
# 缓存辅助：records ↔ RawBar 转换
# ─────────────────────────────────────────────────────────

def _bars_to_records(bars: List[RawBar]) -> list:
    """RawBar 列表 → 可 JSON 序列化的 dict 列表。"""
    return [{"dt": str(b.dt), "open": b.open, "high": b.high,
             "low": b.low, "close": b.close,
             "vol": b.vol, "amount": b.amount} for b in bars]


def _records_to_rawbars(records: list, symbol: str) -> List[RawBar]:
    """JSON dict 列表 → RawBar 列表。"""
    bars = []
    for i, r in enumerate(records):
        bars.append(RawBar(
            symbol=symbol, dt=pd.to_datetime(r["dt"]), id=i, freq=Freq.D,
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            vol=int(r["vol"]), amount=int(r.get("amount", 0)),
        ))
    return bars


# ─────────────────────────────────────────────────────────
# 个股日线加载工具（缓存 → 东财 → Sina 降级链）
# ─────────────────────────────────────────────────────────

def _load_stock_daily_bars(futu_code: str, start_date: str,
                            end_date: str = None) -> Tuple[List[RawBar], str]:
    """
    加载个股日线，供盘后复盘使用。
    返回 (bars, error_type): bars 为空时 error_type 标识失败原因。

    Runtime path is cache-only. sync/backfill owns external provider refresh.
    """
    from signals.data.fetcher import detect_market
    from signals.data.gateway import get_kline
    from signals.data.models import DataRequest

    market = detect_market(futu_code)
    resp = get_kline(DataRequest(
        domain="kline",
        mode="historical",
        market=market,
        symbol=futu_code,
        freq="daily",
        as_of=end_date,
        purpose="review",
        allow_stale=True,
    ))
    df = resp.data
    if df is None or df.empty:
        return [], "cache_miss"
    df = df.reset_index().rename(columns={"index": "dt"})
    if start_date:
        df = df[pd.to_datetime(df["dt"]) >= pd.to_datetime(start_date)]
    records = df.to_dict("records")
    bars = _records_to_rawbars(records, futu_code)
    return (bars, "") if bars else ([], "cache_miss")


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
# 个股分钟线加载（Sina → 东财降级，双熔断控制）
# ─────────────────────────────────────────────────────────

def _load_stock_minute_bars(futu_code: str, freq: Freq) -> List[RawBar]:
    """
    加载个股分钟线（最近5天），供盘后复盘的"当下补充"。
    Runtime path is cache-only.
    """
    from signals.data.fetcher import detect_market
    from signals.data.gateway import get_kline
    from signals.data.models import DataRequest

    market = detect_market(futu_code)
    if market != "A":
        return []  # 暂只支持A股
    freq_value = "30m" if freq == Freq.F30 else "15m"
    resp = get_kline(DataRequest(
        domain="kline",
        mode="historical",
        market=market,
        symbol=futu_code,
        freq=freq_value,
        purpose="review",
        allow_stale=True,
    ))
    df = resp.data
    if df is None or df.empty:
        return []
    df = df.reset_index().rename(columns={"index": "dt"})
    records = df.to_dict("records")
    bars = _records_to_rawbars(records, futu_code)
    for i, bar in enumerate(bars):
        bars[i] = RawBar(
            symbol=bar.symbol,
            dt=bar.dt,
            id=bar.id,
            freq=freq,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            vol=bar.vol,
            amount=bar.amount,
        )
    return bars


# ─────────────────────────────────────────────────────────
# 个股日线+周线+30M复盘（并发版）
# ─────────────────────────────────────────────────────────

def review_stock_daily(symbols: List[str], start_date: str,
                       with_minute: bool = True,
                       l2_stats: Optional[dict] = None,
                       market_ctx: object = None) -> list:
    """
    对指定个股做多级别缠论分析（盘后复盘）。

    三级联动：周线(趋势背景) + 日线(结构确认) + 30min(当下定位)
    信号自带时间衰减，老信号自动降权。

    使用 ThreadPoolExecutor(8) 并发处理，提速 4-7 倍。

    :param symbols: Futu格式代码列表
    :param start_date: 'YYYY-MM-DD'
    :param with_minute: 是否追加30min分析（默认True）
    :param l2_stats: Layer 2 统计数据（用于 regime_mult 计算）
    :param market_ctx: 市场上下文（用于 regime_mult 计算）
    :return: List[ScoredSymbol] 按评分降序
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from signals.core.analyzer import SymbolAnalyzer
    from signals.core.detectors import detect_all_signals
    from signals.core.scorer import score_signals
    from signals.dashboard import get_dashboard

    if not symbols:
        _log("  跳过个股复盘（未指定标的）")
        return []

    # 每次 review 入口重置熔断计数器 + 清理过期缓存
    _reset_all_counters()
    _cache = get_cache()
    if isinstance(_cache, DiskBarCache):
        _cache.cleanup_old(datetime.now().strftime("%Y%m%d"))

    import time as _time
    _t_l3_start = _time.monotonic()

    level_desc = "周线+日线+30min" if with_minute else "日线+周线"
    _log(f"\n>>> Layer 3 个股复盘（{level_desc}）：{len(symbols)} 只 ...")

    # Dashboard 阶段跟踪
    dash = get_dashboard()
    if dash:
        dash.phase_start("L3.review", total=len(symbols))

    scored: list = []
    ok_count = 0
    fail_count = 0
    _done = 0
    _total = len(symbols)
    _result_lock = threading.Lock()

    # 耗时统计（线程安全）
    _timing_lock = threading.Lock()
    _load_times: list = []   # 每只股票的数据加载耗时
    _anal_times: list = []   # 每只股票的分析耗时
    _slow_stocks: list = []  # 耗时 >5s 的慢股

    def _review_one(sym: str):
        """处理单只股票（在线程池中执行）。"""
        nonlocal ok_count, fail_count, _done
        _t_stock = _time.monotonic()

        # ── 并行加载日线 + 分钟线 ──
        _t_load = _time.monotonic()
        if with_minute:
            from concurrent.futures import ThreadPoolExecutor as _MiniPool
            with _MiniPool(max_workers=2) as mini:
                fut_d = mini.submit(_load_stock_daily_bars, sym, start_date)
                fut_m = mini.submit(_load_stock_minute_bars, sym, Freq.F30)
                bars, err_type = fut_d.result()
                bars_30 = fut_m.result()
        else:
            bars, err_type = _load_stock_daily_bars(sym, start_date)
            bars_30 = []
        load_sec = _time.monotonic() - _t_load

        if not bars:
            with _result_lock:
                if err_type != "no_data" or _em_ssl_fails == 0:
                    _log(f"  [✗] {sym}: 无数据 (加载{load_sec:.1f}s)")
                fail_count += 1
                _done += 1
            if dash:
                dash.task_error("L3.review", sym, "无数据")
            return None

        try:
            _t_anal = _time.monotonic()

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

            # 30min 分析（数据已并行加载完毕）
            sigs_30 = []
            m_bi_cnt = 0
            m_bar_cnt = 0
            if bars_30:
                m_bar_cnt = len(bars_30)
                az_30 = SymbolAnalyzer(sym, Freq.F30, bars_30)
                sigs_30 = detect_all_signals(az_30.czsc, sym)
                m_bi_cnt = len(az_30.finished_bis)

            # 合并全部信号（含时间衰减）
            all_signals = sigs_d + sigs_w + sigs_30
            sc = score_signals(sym, all_signals)

            # 笔动力学分析（对齐盘中 screener 逻辑）
            try:
                from signals.core.bi_dynamics import (
                    analyze_multi_freq_dynamics,
                    merge_dynamics_score,
                    get_best_sell_warning,
                )
                sym_analyzers = {"日线": az_d}
                if bars_30:
                    sym_analyzers["30分钟"] = az_30
                profiles = analyze_multi_freq_dynamics(sym_analyzers)
                sc.dynamics_merged_score = merge_dynamics_score(profiles)
                sc.sell_warning = get_best_sell_warning(profiles)
                sc.dynamics_profile = profiles.get("日线") or next(
                    iter(profiles.values()), None)
                # 临时存储供后续融合使用
                sc._dynamics = sc.dynamics_profile
            except Exception:
                sc._dynamics = None

            anal_sec = _time.monotonic() - _t_anal
            total_sec = _time.monotonic() - _t_stock

            sig_str = f"得分={sc.total_score:+.0f}" if sc.total_score != 0 else "无信号"
            dir_tag = f" [{sc.direction}]" if sc.direction else ""
            w_info = f"  {len(bars_w)}根周线 {w_bi_cnt}笔" if bars_w else ""
            m_info = f"  {m_bar_cnt}根30M {m_bi_cnt}笔" if m_bar_cnt else ""

            with _result_lock:
                scored.append(sc)
                ok_count += 1
                _done += 1
                _log(f"  [✓] {sym}: {len(bars)}根日线 {len(az_d.finished_bis)}笔"
                     f"{w_info}{m_info}  {sig_str}{dir_tag}")
            with _timing_lock:
                _load_times.append(load_sec)
                _anal_times.append(anal_sec)
                if total_sec > 5.0:
                    _slow_stocks.append((sym, total_sec, load_sec, anal_sec))

            if dash:
                dash.task_done("L3.review", sym)
            return sc

        except Exception as e:
            with _result_lock:
                _log(f"  [✗] {sym}: 分析失败 {e}")
                fail_count += 1
                _done += 1
            if dash:
                dash.task_error("L3.review", sym, str(e))
            return None

    # ── 并发执行 ─────────────────────────────────────────
    max_workers = min(12, len(symbols))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_review_one, sym): sym for sym in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                fut.result()  # 异常已在 _review_one 内处理
            except Exception as e:
                with _result_lock:
                    _log(f"  [✗] {sym}: 线程异常 {e}")
                    fail_count += 1
                    _done += 1

            # 进度报告（每5只或结束时）
            with _result_lock:
                current_done = _done
            if current_done % 5 == 0 or current_done == _total:
                _log(f"  ── 进度: {current_done}/{_total}"
                     f" ({current_done * 100 // _total}%)")

    # Dashboard 阶段结束
    if dash:
        dash.phase_end("L3.review",
                       detail=f"成功{ok_count} 失败{fail_count}")

    # 降级统计
    if _em_switched:
        _log(f"  [i] 东财→Sina 降级: SSL失败{_em_ssl_fails}次, "
             f"成功{ok_count}只, 失败{fail_count}只")
    if _sina_switched:
        _log(f"  [i] Sina 降级: 连续失败{_sina_fails}次, "
             f"成功{ok_count}只, 失败{fail_count}只")

    # ── 异常检测 + 信号融合（对齐盘中 screener 完整流水线）──
    try:
        from signals.core.anomaly import compute_anomaly_profile
        from signals.core.fusion import fuse_scores
        for sc in scored:
            cache_key = f"{sc.symbol.replace('.', '_')}_{datetime.now().strftime('%Y%m%d')}"
            cached = get_cache().get(cache_key)
            anomaly = None
            if cached and len(cached) >= 25:
                daily_bars = _records_to_rawbars(cached, sc.symbol)
                anomaly = compute_anomaly_profile(sc.symbol, daily_bars)

            # 取出 _review_one 中计算的 dynamics
            dynamics = getattr(sc, '_dynamics', None)

            fused = fuse_scores(
                sc, anomaly,
                dynamics=dynamics,
                l2_stats=l2_stats,
                market_ctx=market_ctx,
            )
            if anomaly:
                sc.anomaly_profile = anomaly
            sc.fused_score = fused
            sc.fused_total = fused.fused_total
    except Exception:
        pass  # 异常检测失败不影响主流程

    # 排序: 有融合分用融合分，否则用缠论原始分
    scored.sort(key=lambda x: x.fused_total if x.fused_total else x.total_score,
                reverse=True)

    # ── 耗时统计 ──
    _l3_elapsed = _time.monotonic() - _t_l3_start
    avg_load = sum(_load_times) / len(_load_times) if _load_times else 0
    avg_anal = sum(_anal_times) / len(_anal_times) if _anal_times else 0
    _log(f"\n  [⏱] L3 耗时统计:")
    _log(f"       总耗时: {_l3_elapsed:.1f}s ({len(symbols)}只, "
         f"成功{ok_count} 失败{fail_count})")
    _log(f"       平均加载: {avg_load:.2f}s  平均分析: {avg_anal:.2f}s")
    if _slow_stocks:
        _slow_stocks.sort(key=lambda x: -x[1])
        _log(f"       慢股 (>5s): {len(_slow_stocks)}只")
        for sym, total, load, anal in _slow_stocks[:5]:
            _log(f"         {sym}: {total:.1f}s (加载{load:.1f}s 分析{anal:.1f}s)")

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
        _log("  无个股评分结果")
        return
    _log("\n" + "─" * 60)
    _log("  个股复盘排名（周线+日线+30min 三级联动）")
    _log("─" * 60)
    for i, sc in enumerate(scored, 1):
        sigs = ", ".join(f"{s.signal_type}({s.freq})" for s in sc.signals[:4])
        dir_tag = f" [{sc.direction}]" if sc.direction else ""
        # 风控信息
        risk_str = ""
        try:
            from signals.core.risk import enrich_with_risk
            risk_line = enrich_with_risk(sc)
            if risk_line:
                risk_str = f"\n      {risk_line.strip()}"
        except Exception:
            pass
        _log(f"  {i:2d}. {sc.symbol:15s}  分={sc.total_score:+6.1f}{dir_tag}  {sigs}{risk_str}")
    _log("─" * 60 + "\n")
