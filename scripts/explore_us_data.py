#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股数据源探索脚本

逐个探测可用的美股数据渠道，验证数据质量和 RawBar 兼容性。
Phase 1: 无需 API key 的 5 个源（AKShare×2, Futu, yfinance, Stooq）

Usage:
    python scripts/explore_us_data.py              # 全部探测
    python scripts/explore_us_data.py --skip-futu  # 跳过 Futu（节省额度）
"""

import sys
import os
import time
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

# 让 import 找到项目根目录
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from czsc import RawBar, Freq

# 复用项目的 _to_raw_bars
from monitor.data_fetcher import _to_raw_bars


# ─────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────
@dataclass
class ProbeResult:
    source: str
    data_type: str          # "daily" / "5min" / "15min" ...
    symbol: str             # 统一 US.AAPL 格式
    success: bool
    bar_count: int = 0
    date_start: str = ""
    date_end: str = ""
    nan_count: int = 0
    latency_ms: float = 0.0
    rawbar_ok: bool = False
    czsc_ok: bool = False
    czsc_bi_count: int = 0
    error: str = ""
    notes: str = ""


# ─────────────────────────────────────────────────────────
# 测试标的
# ─────────────────────────────────────────────────────────
TEST_SYMBOLS = {
    # futu_code: (eastmoney_prefix, exchange_desc)
    "US.AAPL":  ("105", "NASDAQ"),
    "US.MSFT":  ("105", "NASDAQ"),
    "US.NVDA":  ("105", "NASDAQ"),
    "US.SPY":   ("107", "ARCA ETF"),
    "US.QQQ":   ("105", "NASDAQ ETF"),
    "US.DIA":   ("107", "ARCA ETF"),
    "US.BABA":  ("106", "NYSE ADR"),
}

# Futu 只测这些（节省额度）
FUTU_DAILY_SYMBOLS = ["US.AAPL", "US.SPY"]
FUTU_MINUTE_SYMBOLS = ["US.AAPL"]


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────
def _ticker(futu_code: str) -> str:
    """US.AAPL → AAPL"""
    return futu_code.split(".")[1]


def _check_nan(df: pd.DataFrame, cols: list) -> int:
    """统计 OHLCV 列的 NaN 数量"""
    count = 0
    for c in cols:
        if c in df.columns:
            count += df[c].isna().sum()
    return int(count)


def _validate_rawbars(bars: List[RawBar], symbol: str) -> tuple:
    """基础 RawBar 质量检查 → (ok, msg)"""
    if not bars:
        return False, "empty"
    for i, b in enumerate(bars[:3] + bars[-3:]):
        if b.high < b.low:
            return False, f"bar[{i}] high < low"
        if b.close <= 0 or b.open <= 0:
            return False, f"bar[{i}] price <= 0"
    # 单调递增检查
    for i in range(1, min(len(bars), 50)):
        if bars[i].dt <= bars[i - 1].dt:
            return False, f"dt not monotonic at {i}"
    return True, "ok"


def _try_czsc(bars: List[RawBar]) -> tuple:
    """尝试喂入 CZSC → (ok, bi_count)"""
    if len(bars) < 100:
        return False, 0
    try:
        from czsc import CZSC
        c = CZSC(bars, max_bi_num=50)
        return True, len(c.bi_list)
    except Exception as e:
        return False, 0


def _print_result(r: ProbeResult):
    """单行打印探测结果"""
    status = "OK  " if r.success else "FAIL"
    rawbar = "RawBar=OK" if r.rawbar_ok else "RawBar=FAIL"
    czsc_info = ""
    if r.czsc_ok:
        czsc_info = f"  CZSC={r.czsc_bi_count}笔"
    elif r.bar_count >= 100 and r.rawbar_ok:
        czsc_info = "  CZSC=FAIL"

    if r.success:
        print(f"  {r.symbol:<10} {r.data_type:<8} {status} "
              f"{r.bar_count:>6} bars  {r.date_start} ~ {r.date_end}  "
              f"NaN={r.nan_count}  {r.latency_ms:.0f}ms  {rawbar}{czsc_info}"
              f"{'  ' + r.notes if r.notes else ''}")
    else:
        err_short = r.error[:60] if r.error else "unknown"
        print(f"  {r.symbol:<10} {r.data_type:<8} {status} {err_short}"
              f"{'  ' + r.notes if r.notes else ''}")


# ─────────────────────────────────────────────────────────
# Probe 1: AKShare-Sina (stock_us_daily)
# ─────────────────────────────────────────────────────────
def probe_akshare_sina(futu_code: str) -> ProbeResult:
    r = ProbeResult(source="AKShare-Sina", data_type="daily", symbol=futu_code, success=False)
    try:
        import akshare as ak
        ticker = _ticker(futu_code)
        t0 = time.time()
        # 用不复权避免前复权导致远古数据变负数（AAPL 1984 年 qfq 后价格 -10）
        df = ak.stock_us_daily(symbol=ticker, adjust="")
        r.latency_ms = (time.time() - t0) * 1000

        if df is None or df.empty:
            r.error = "empty DataFrame"
            return r

        r.bar_count = len(df)
        r.date_start = str(df["date"].min())[:10]
        r.date_end = str(df["date"].max())[:10]
        neg_count = int((df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
        r.nan_count = _check_nan(df, ["open", "high", "low", "close", "volume"])
        r.success = True
        if neg_count:
            r.notes = f"warn: {neg_count} rows with price<=0 (pre-adj artifact)"

        # RawBar 转换 — 截取近 5 年避免远古脏数据
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=5 * 365)
        df_recent = df[pd.to_datetime(df["date"]) >= cutoff].copy()
        r.notes = (r.notes + "  " if r.notes else "") + f"recent={len(df_recent)}bars(5yr)"

        df2 = df_recent.rename(columns={"date": "dt", "volume": "vol"})
        df2["amount"] = 0
        bars = _to_raw_bars(df2, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, msg = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


# ─────────────────────────────────────────────────────────
# Probe 2: AKShare-东财 (stock_us_hist / stock_us_hist_min_em)
# ─────────────────────────────────────────────────────────
def probe_akshare_eastmoney_daily(futu_code: str, em_prefix: str) -> ProbeResult:
    r = ProbeResult(source="AKShare-东财", data_type="daily", symbol=futu_code, success=False)
    try:
        import akshare as ak
        ticker = _ticker(futu_code)
        em_symbol = f"{em_prefix}.{ticker}"
        t0 = time.time()
        df = ak.stock_us_hist(symbol=em_symbol, period="daily", adjust="qfq")
        r.latency_ms = (time.time() - t0) * 1000

        if df is None or df.empty:
            r.error = "empty DataFrame"
            return r

        r.bar_count = len(df)
        r.date_start = str(df["日期"].min())[:10]
        r.date_end = str(df["日期"].max())[:10]
        r.nan_count = _check_nan(df, ["开盘", "最高", "最低", "收盘", "成交量"])
        r.success = True
        r.notes = f"有成交额字段"

        # RawBar 转换
        df2 = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                                  "最低": "low", "收盘": "close",
                                  "成交量": "vol", "成交额": "amount"})
        bars = _to_raw_bars(df2, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, msg = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


def probe_akshare_eastmoney_minute(futu_code: str, em_prefix: str) -> ProbeResult:
    r = ProbeResult(source="AKShare-东财", data_type="minute", symbol=futu_code, success=False)
    try:
        import akshare as ak
        ticker = _ticker(futu_code)
        em_symbol = f"{em_prefix}.{ticker}"
        t0 = time.time()
        # stock_us_hist_min_em 签名: (symbol, start_date, end_date)，无 period 参数
        df = ak.stock_us_hist_min_em(symbol=em_symbol)
        r.latency_ms = (time.time() - t0) * 1000

        if df is None or df.empty:
            r.error = "empty DataFrame"
            return r

        r.bar_count = len(df)
        print(f"    [debug] columns: {list(df.columns)}")
        # 自动检测列名
        dt_col = next((c for c in df.columns if c in ("时间", "time", "datetime")), df.columns[0])
        r.date_start = str(df[dt_col].min())[:16]
        r.date_end = str(df[dt_col].max())[:16]
        r.success = True

        # 尝试 RawBar 转换
        col_map = {}
        for cn, en in [("时间", "dt"), ("开盘", "open"), ("收盘", "close"),
                        ("最高", "high"), ("最低", "low"), ("成交量", "vol"), ("成交额", "amount")]:
            if cn in df.columns:
                col_map[cn] = en
        df2 = df.rename(columns=col_map)
        if "amount" not in df2.columns:
            df2["amount"] = 0
        # 推测频率：看时间间隔
        freq_guess = Freq.F5  # 默认5分钟
        bars = _to_raw_bars(df2, futu_code, freq_guess,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, msg = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


# ─────────────────────────────────────────────────────────
# Probe 3: Futu (request_history_kline)
# ─────────────────────────────────────────────────────────
def probe_futu_daily(futu_code: str, ctx) -> ProbeResult:
    r = ProbeResult(source="Futu", data_type="daily", symbol=futu_code, success=False, notes="1 quota")
    try:
        from futu import KLType, AuType, RET_OK
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        t0 = time.time()
        ret, df, _ = ctx.request_history_kline(
            futu_code, start=start, ktype=KLType.K_DAY,
            autype=AuType.QFQ, max_count=2000
        )
        r.latency_ms = (time.time() - t0) * 1000

        if ret != 0:
            # df contains error message when ret != 0
            r.error = f"ret={ret}: {df}" if isinstance(df, str) else f"ret={ret}"
            return r
        if df is None or df.empty:
            r.error = "empty"
            return r

        r.bar_count = len(df)
        r.date_start = str(df["time_key"].min())[:10]
        r.date_end = str(df["time_key"].max())[:10]
        r.nan_count = _check_nan(df, ["open", "high", "low", "close", "volume"])
        r.success = True

        df2 = df.rename(columns={"time_key": "dt", "volume": "vol", "turnover": "amount"})
        df2["amount"] = df2["amount"].fillna(0)
        bars = _to_raw_bars(df2, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, _ = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


def probe_futu_minute(futu_code: str, ctx) -> ProbeResult:
    r = ProbeResult(source="Futu", data_type="5min", symbol=futu_code, success=False, notes="1 quota")
    try:
        from futu import KLType, AuType, RET_OK
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        t0 = time.time()
        ret, df, _ = ctx.request_history_kline(
            futu_code, start=start, ktype=KLType.K_5M,
            autype=AuType.QFQ, max_count=2000
        )
        r.latency_ms = (time.time() - t0) * 1000

        if ret != 0:
            r.error = f"ret={ret}: {df}" if isinstance(df, str) else f"ret={ret}"
            return r
        if df is None or df.empty:
            r.error = "empty"
            return r

        r.bar_count = len(df)
        r.date_start = str(df["time_key"].min())[:16]
        r.date_end = str(df["time_key"].max())[:16]
        r.nan_count = _check_nan(df, ["open", "high", "low", "close", "volume"])
        r.success = True

        df2 = df.rename(columns={"time_key": "dt", "volume": "vol", "turnover": "amount"})
        df2["amount"] = df2["amount"].fillna(0)
        bars = _to_raw_bars(df2, futu_code, Freq.F5,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, _ = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


# ─────────────────────────────────────────────────────────
# Probe 4: yfinance
# ─────────────────────────────────────────────────────────
def probe_yfinance_daily(futu_code: str) -> ProbeResult:
    r = ProbeResult(source="yfinance", data_type="daily", symbol=futu_code, success=False)
    try:
        import yfinance as yf
    except ImportError:
        r.error = "NOT INSTALLED: pip install yfinance"
        return r
    try:
        ticker = _ticker(futu_code)
        t0 = time.time()
        tk = yf.Ticker(ticker)
        df = tk.history(period="1y")
        r.latency_ms = (time.time() - t0) * 1000

        if df is None or df.empty:
            r.error = "empty DataFrame"
            return r

        r.bar_count = len(df)
        r.date_start = str(df.index.min())[:10]
        r.date_end = str(df.index.max())[:10]
        r.nan_count = _check_nan(df, ["Open", "High", "Low", "Close", "Volume"])
        r.success = True

        # RawBar 转换
        df2 = df.reset_index()
        dt_col = "Date" if "Date" in df2.columns else "Datetime"
        df2 = df2.rename(columns={dt_col: "dt", "Open": "open", "High": "high",
                                   "Low": "low", "Close": "close",
                                   "Volume": "vol"})
        df2["amount"] = 0
        bars = _to_raw_bars(df2, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, _ = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


def probe_yfinance_minute(futu_code: str) -> ProbeResult:
    r = ProbeResult(source="yfinance", data_type="5min", symbol=futu_code, success=False)
    try:
        import yfinance as yf
    except ImportError:
        r.error = "NOT INSTALLED: pip install yfinance"
        return r
    try:
        ticker = _ticker(futu_code)
        t0 = time.time()
        tk = yf.Ticker(ticker)
        df = tk.history(period="5d", interval="5m")
        r.latency_ms = (time.time() - t0) * 1000

        if df is None or df.empty:
            r.error = "empty DataFrame"
            return r

        r.bar_count = len(df)
        r.date_start = str(df.index.min())[:16]
        r.date_end = str(df.index.max())[:16]
        r.nan_count = _check_nan(df, ["Open", "High", "Low", "Close", "Volume"])
        r.success = True

        df2 = df.reset_index()
        dt_col = "Datetime" if "Datetime" in df2.columns else "Date"
        df2 = df2.rename(columns={dt_col: "dt", "Open": "open", "High": "high",
                                   "Low": "low", "Close": "close",
                                   "Volume": "vol"})
        df2["amount"] = 0
        bars = _to_raw_bars(df2, futu_code, Freq.F5,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, _ = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


# ─────────────────────────────────────────────────────────
# Probe 5: Stooq (via pandas-datareader)
# ─────────────────────────────────────────────────────────
def probe_stooq(futu_code: str) -> ProbeResult:
    r = ProbeResult(source="Stooq", data_type="daily", symbol=futu_code, success=False)
    try:
        import pandas_datareader.data as web
    except ImportError:
        r.error = "NOT INSTALLED: pip install pandas-datareader"
        return r
    try:
        ticker = _ticker(futu_code)
        t0 = time.time()
        df = web.DataReader(ticker, "stooq")
        r.latency_ms = (time.time() - t0) * 1000

        if df is None or df.empty:
            r.error = "empty DataFrame"
            return r

        r.bar_count = len(df)
        r.date_start = str(df.index.min())[:10]
        r.date_end = str(df.index.max())[:10]
        r.nan_count = _check_nan(df, ["Open", "High", "Low", "Close", "Volume"])
        r.success = True

        df2 = df.reset_index()
        df2 = df2.rename(columns={"Date": "dt", "Open": "open", "High": "high",
                                   "Low": "low", "Close": "close",
                                   "Volume": "vol"})
        df2["amount"] = 0
        bars = _to_raw_bars(df2, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        ok, _ = _validate_rawbars(bars, futu_code)
        r.rawbar_ok = ok
        if ok:
            r.czsc_ok, r.czsc_bi_count = _try_czsc(bars)
    except Exception as e:
        r.error = str(e)
    return r


# ─────────────────────────────────────────────────────────
# 汇总对比表
# ─────────────────────────────────────────────────────────
def print_summary(results: List[ProbeResult]):
    print("\n" + "=" * 72)
    print("  汇总对比")
    print("=" * 72)

    # 按 source 聚合
    from collections import defaultdict
    agg = defaultdict(lambda: {"daily_ok": 0, "daily_total": 0,
                                "minute_ok": 0, "minute_total": 0,
                                "avg_latency": [], "depth": "",
                                "rawbar_ok": 0, "czsc_ok": 0,
                                "cost": "Free"})

    for r in results:
        key = r.source
        is_minute = r.data_type != "daily"
        if is_minute:
            agg[key]["minute_total"] += 1
            if r.success:
                agg[key]["minute_ok"] += 1
        else:
            agg[key]["daily_total"] += 1
            if r.success:
                agg[key]["daily_ok"] += 1
                # 记录最大深度
                if r.date_start and (not agg[key]["depth"] or r.date_start < agg[key]["depth"]):
                    agg[key]["depth"] = r.date_start

        if r.success:
            agg[key]["avg_latency"].append(r.latency_ms)
        if r.rawbar_ok:
            agg[key]["rawbar_ok"] += 1
        if r.czsc_ok:
            agg[key]["czsc_ok"] += 1

        if r.source == "Futu":
            agg[key]["cost"] = "Quota"

    # 表头
    print(f"\n{'Source':<18} {'Daily':>8} {'Minute':>8} {'Depth':>12} "
          f"{'Latency':>10} {'RawBar':>8} {'CZSC':>6} {'Cost':>8}")
    print("-" * 90)

    for src, d in sorted(agg.items()):
        daily = f"{d['daily_ok']}/{d['daily_total']}" if d['daily_total'] else "-"
        minute = f"{d['minute_ok']}/{d['minute_total']}" if d['minute_total'] else "-"
        depth = d["depth"] if d["depth"] else "-"
        lat = f"~{sum(d['avg_latency'])/len(d['avg_latency']):.0f}ms" if d["avg_latency"] else "-"
        rb = f"{d['rawbar_ok']}" if d['rawbar_ok'] else "0"
        czsc = f"{d['czsc_ok']}" if d['czsc_ok'] else "0"

        print(f"{src:<18} {daily:>8} {minute:>8} {depth:>12} "
              f"{lat:>10} {rb:>8} {czsc:>6} {d['cost']:>8}")

    print()


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="US Stock Data Source Exploration")
    parser.add_argument("--skip-futu", action="store_true", help="Skip Futu probes (save quota)")
    args = parser.parse_args()

    print("=" * 72)
    print("  美股数据源探索 (Phase 1: No API Key Required)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    # 检查已安装的包
    installed = []
    missing = []
    for pkg in ["akshare", "futu", "yfinance", "pandas_datareader"]:
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "?")
            installed.append(f"{pkg}={ver}")
        except ImportError:
            install_name = {"pandas_datareader": "pandas-datareader"}.get(pkg, pkg)
            missing.append(f"{pkg} (pip install {install_name})")

    print(f"\n  Installed: {', '.join(installed) if installed else 'none'}")
    if missing:
        print(f"  Missing:   {', '.join(missing)}")
    print()

    all_results: List[ProbeResult] = []

    # ── 1. AKShare-Sina ──────────────────────────────────
    print("── AKShare-Sina (stock_us_daily) " + "─" * 38)
    for sym in TEST_SYMBOLS:
        r = probe_akshare_sina(sym)
        all_results.append(r)
        _print_result(r)
    print()

    # ── 2. AKShare-东财 ──────────────────────────────────
    print("── AKShare-东财 (stock_us_hist / stock_us_hist_min_em) " + "─" * 17)
    for sym, (em_prefix, _) in TEST_SYMBOLS.items():
        r = probe_akshare_eastmoney_daily(sym, em_prefix)
        all_results.append(r)
        _print_result(r)
    # 分钟线只测 AAPL（最可能成功）
    print("  --- minute test (AAPL only, may timeout) ---")
    r = probe_akshare_eastmoney_minute("US.AAPL", "105")
    all_results.append(r)
    _print_result(r)
    print()

    # ── 3. Futu ──────────────────────────────────────────
    print("── Futu (request_history_kline) " + "─" * 39)
    if args.skip_futu:
        print("  [SKIPPED] --skip-futu flag")
    else:
        futu_ctx = None
        try:
            from futu import OpenQuoteContext
            import config
            futu_ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
            for sym in FUTU_DAILY_SYMBOLS:
                r = probe_futu_daily(sym, futu_ctx)
                all_results.append(r)
                _print_result(r)
                time.sleep(1)
            for sym in FUTU_MINUTE_SYMBOLS:
                r = probe_futu_minute(sym, futu_ctx)
                all_results.append(r)
                _print_result(r)
                time.sleep(1)
        except Exception as e:
            print(f"  [FAIL] Futu connection: {e}")
        finally:
            if futu_ctx:
                futu_ctx.close()
    print()

    # ── 4. yfinance ──────────────────────────────────────
    print("── yfinance " + "─" * 59)
    # 先测一个看看是否安装
    test_r = probe_yfinance_daily("US.AAPL")
    if "NOT INSTALLED" in test_r.error:
        print(f"  [NOT INSTALLED] pip install yfinance")
        all_results.append(test_r)
    else:
        all_results.append(test_r)
        _print_result(test_r)
        for sym in list(TEST_SYMBOLS.keys())[1:]:  # 跳过已测的 AAPL
            r = probe_yfinance_daily(sym)
            all_results.append(r)
            _print_result(r)
        # 分钟线
        print("  --- minute test ---")
        for sym in ["US.AAPL", "US.SPY"]:
            r = probe_yfinance_minute(sym)
            all_results.append(r)
            _print_result(r)
    print()

    # ── 5. Stooq ─────────────────────────────────────────
    print("── Stooq (pandas-datareader) " + "─" * 42)
    test_r = probe_stooq("US.AAPL")
    if "NOT INSTALLED" in test_r.error:
        print(f"  [NOT INSTALLED] pip install pandas-datareader")
        all_results.append(test_r)
    else:
        all_results.append(test_r)
        _print_result(test_r)
        for sym in list(TEST_SYMBOLS.keys())[1:]:
            r = probe_stooq(sym)
            all_results.append(r)
            _print_result(r)
    print()

    # ── 汇总 ─────────────────────────────────────────────
    print_summary(all_results)

    # ── 建议 ─────────────────────────────────────────────
    print("=" * 72)
    print("  建议")
    print("=" * 72)
    # 动态生成建议
    daily_sources = []
    minute_sources = []
    issues = []

    for r in all_results:
        if r.success and r.rawbar_ok:
            entry = f"{r.source} ({r.bar_count} bars, {r.date_start}~{r.date_end})"
            if r.data_type == "daily":
                daily_sources.append(entry)
            else:
                minute_sources.append(entry)
        elif not r.success and r.error and "NOT INSTALLED" not in r.error:
            issues.append(f"{r.source} {r.data_type}: {r.error[:70]}")

    daily_sources = list(dict.fromkeys(daily_sources))  # dedupe
    minute_sources = list(dict.fromkeys(minute_sources))

    print("\n  可用 Daily 源:")
    for s in daily_sources[:5]:
        print(f"    [OK] {s}")
    print("\n  可用 Minute 源:")
    for s in minute_sources[:5]:
        print(f"    [OK] {s}")
    if not minute_sources:
        print("    (无可用分钟线源)")

    if issues:
        print("\n  问题:")
        for issue in issues[:8]:
            print(f"    [!] {issue}")

    print("""
  推荐方案:
    Daily:  AKShare-Sina (不复权, 截取近5年) + Stooq (5年复权数据)
    Minute: yfinance (免费, 5天5min) — 足够缠论日内分析
    备选:   Futu (需开通美股行情权限) — 更深分钟线历史

  Next steps:
    1. 确认上述源足够后, 可在 monitor/data_fetcher.py 新增 US 数据方法
    2. Futu: 在牛牛APP开通美股行情权限 (Level 1 免费)
    3. Phase 2: 注册 Alpha Vantage / Twelve Data 获取免费 API key
""")


if __name__ == "__main__":
    main()
