# -*- coding: utf-8 -*-
"""
天际股份 (SZ.002759) 实盘测试：盘中策略 vs 盘后回测
自带重试 + 多源降级，应对东财 SSL 间歇性故障。
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from czsc import Freq, RawBar
from signals.core.analyzer import SymbolAnalyzer
from signals.core.detectors import detect_all_signals
from signals.core.scorer import score_signals, FREQ_MULTIPLIER
from signals.layers.review_screener import resample_daily_to_weekly

SYMBOL = "SZ.002759"
SYMBOL_NAME = "天际股份"


def load_daily_with_fallback(symbol: str, start: str) -> list:
    """日线加载，东财 SSL 失败自动重试 + 换源"""
    import akshare as ak
    _, pure_code = symbol.split(".")
    edt = datetime.now().strftime("%Y%m%d")
    sdt = start.replace("-", "")

    # 尝试1: 东财 stock_zh_a_hist（最多3次重试）
    for attempt in range(1, 4):
        try:
            print(f"  [日线] 东财 尝试 {attempt}/3 ...", end="", flush=True)
            df = ak.stock_zh_a_hist(symbol=pure_code, period="daily",
                                     start_date=sdt, end_date=edt, adjust="qfq")
            if df is not None and not df.empty:
                print(f" OK ({len(df)} 根)")
                df = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                                         "最低": "low", "收盘": "close",
                                         "成交量": "vol", "成交额": "amount"})
                from signals.data.fetcher import _to_raw_bars
                return _to_raw_bars(df, symbol, Freq.D,
                                    "dt", "open", "high", "low", "close", "vol", "amount")
        except Exception as e:
            print(f" 失败 ({e.__class__.__name__})")
            if attempt < 3:
                time.sleep(3 * attempt)

    # 尝试2: AKShare stock_zh_index_daily 不适用个股，尝试 Sina 日线接口
    try:
        print(f"  [日线] Sina 备选 ...", end="", flush=True)
        df = ak.stock_zh_a_daily(symbol=f"sz{pure_code}", adjust="qfq")
        if df is not None and not df.empty:
            # 过滤日期范围
            df["date"] = df["date"].astype(str)
            df = df[df["date"] >= start]
            print(f" OK ({len(df)} 根)")
            from signals.data.fetcher import _to_raw_bars
            return _to_raw_bars(df, symbol, Freq.D,
                                "date", "open", "high", "low", "close", "volume", None)
    except Exception as e:
        print(f" 失败 ({e.__class__.__name__}: {e})")

    return []


def load_minute_with_fallback(symbol: str, freq: Freq) -> list:
    """分钟线加载，Sina → 东财 降级"""
    import akshare as ak
    _, pure_code = symbol.split(".")
    ak_sym = f"sz{pure_code}"
    freq_map = {"15分钟": "15", "30分钟": "30"}
    period = freq_map.get(freq.value, "15")

    # 尝试1: Sina (stock_zh_a_minute)
    for attempt in range(1, 3):
        try:
            print(f"  [{freq.value}] Sina 尝试 {attempt}/2 ...", end="", flush=True)
            df = ak.stock_zh_a_minute(symbol=ak_sym, period=period)
            if df is not None and not df.empty:
                print(f" OK ({len(df)} 根)")
                from signals.data.fetcher import _to_raw_bars
                return _to_raw_bars(df, symbol, freq,
                                    "day", "open", "high", "low", "close", "volume", None)
        except Exception as e:
            print(f" 失败 ({e.__class__.__name__})")
            if attempt < 2:
                time.sleep(2)

    # 尝试2: 东财分钟线 (stock_zh_a_hist_min_em)
    try:
        print(f"  [{freq.value}] 东财分钟线 ...", end="", flush=True)
        klt_map = {"15分钟": "15", "30分钟": "30"}
        df = ak.stock_zh_a_hist_min_em(symbol=pure_code, period=klt_map.get(freq.value, "15"))
        if df is not None and not df.empty:
            print(f" OK ({len(df)} 根)")
            from signals.data.fetcher import _to_raw_bars
            col_map = {"时间": "dt", "开盘": "open", "最高": "high",
                       "最低": "low", "收盘": "close", "成交量": "vol", "成交额": "amount"}
            df = df.rename(columns=col_map)
            return _to_raw_bars(df, symbol, freq,
                                "dt", "open", "high", "low", "close", "vol", "amount")
    except Exception as e:
        print(f" 失败 ({e.__class__.__name__})")

    return []


def main():
    print(f"{'=' * 72}")
    print(f"  {SYMBOL_NAME} ({SYMBOL}) 实盘策略对比")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 72}")

    # ─────────────────────────────────────────────
    # 1. 加载数据
    # ─────────────────────────────────────────────
    print(f"\n>>> 加载数据 ...")

    bars_d = load_daily_with_fallback(SYMBOL, "2024-09-24")
    if bars_d:
        print(f"  日线汇总: {len(bars_d)} 根 "
              f"({bars_d[0].dt.strftime('%Y-%m-%d')} ~ {bars_d[-1].dt.strftime('%Y-%m-%d')})")
    else:
        print(f"  *** 日线加载全部失败，无法运行回测 ***")

    bars_w = resample_daily_to_weekly(bars_d, SYMBOL) if bars_d else []
    if bars_w:
        print(f"  周线合成: {len(bars_w)} 根")

    bars_30 = load_minute_with_fallback(SYMBOL, Freq.F30)
    bars_15 = load_minute_with_fallback(SYMBOL, Freq.F15)

    if not bars_d and not bars_30 and not bars_15:
        print(f"\n  *** 所有数据源不可用，请稍后重试 ***")
        return

    # ─────────────────────────────────────────────
    # 2. 盘中策略（15min + 30min）
    # ─────────────────────────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  盘中实时策略（15min + 30min 双级别）")
    print(f"{'─' * 72}")

    intra_sigs = []
    for label, freq, bars in [("15min", Freq.F15, bars_15), ("30min", Freq.F30, bars_30)]:
        if not bars:
            print(f"  [{label}] 无数据，跳过")
            continue
        az = SymbolAnalyzer(SYMBOL, freq, bars)
        sigs = detect_all_signals(az.czsc, SYMBOL)
        bi_cnt = len(az.finished_bis)
        print(f"  [{label}] {len(bars)} bars → {bi_cnt} 笔  检出 {len(sigs)} 信号")
        for s in sigs:
            mult = FREQ_MULTIPLIER.get(s.freq, 1.0)
            print(f"    {s.signal_type} conf={s.confidence:.2f} ×{mult} @ {s.price:.2f}  {s.details}")
        intra_sigs.extend(sigs)

    intra_scored = score_signals(SYMBOL, intra_sigs)
    dir_tag = f" [{intra_scored.direction}]" if intra_scored.direction else ""
    print(f"\n  盘中综合得分: {intra_scored.total_score:+.1f}{dir_tag}  信号数: {intra_scored.signal_count}")
    if intra_scored.signal_count > 0:
        print(intra_scored.details)

    # ─────────────────────────────────────────────
    # 3. 盘后回测策略（日线 + 周线）
    # ─────────────────────────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  盘后回测策略（日线 + 周线双级别，起始 2024-09-24）")
    print(f"{'─' * 72}")

    review_sigs = []

    if bars_d:
        az_d = SymbolAnalyzer(SYMBOL, Freq.D, bars_d, max_bi_num=200)
        sigs_d = detect_all_signals(az_d.czsc, SYMBOL)
        bi_cnt = len(az_d.finished_bis)
        print(f"  [日线] {len(bars_d)} bars → {bi_cnt} 笔  检出 {len(sigs_d)} 信号")
        for s in sigs_d:
            mult = FREQ_MULTIPLIER.get(s.freq, 1.0)
            print(f"    {s.signal_type} conf={s.confidence:.2f} ×{mult} @ {s.price:.2f}  {s.details}")
        review_sigs.extend(sigs_d)
    else:
        print(f"  [日线] 无数据，跳过")

    if bars_w and len(bars_w) >= 10:
        az_w = SymbolAnalyzer(SYMBOL, Freq.W, bars_w, max_bi_num=100)
        sigs_w = detect_all_signals(az_w.czsc, SYMBOL)
        bi_cnt = len(az_w.finished_bis)
        print(f"  [周线] {len(bars_w)} bars → {bi_cnt} 笔  检出 {len(sigs_w)} 信号")
        for s in sigs_w:
            mult = FREQ_MULTIPLIER.get(s.freq, 1.0)
            print(f"    {s.signal_type} conf={s.confidence:.2f} ×{mult} @ {s.price:.2f}  {s.details}")
        review_sigs.extend(sigs_w)
    elif bars_w:
        print(f"  [周线] {len(bars_w)} 根，笔数不足，跳过")
    else:
        print(f"  [周线] 无数据")

    review_scored = score_signals(SYMBOL, review_sigs)
    dir_tag = f" [{review_scored.direction}]" if review_scored.direction else ""
    print(f"\n  回测综合得分: {review_scored.total_score:+.1f}{dir_tag}  信号数: {review_scored.signal_count}")
    if review_scored.signal_count > 0:
        print(review_scored.details)

    # ─────────────────────────────────────────────
    # 4. 对比总结
    # ─────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"  对比总结  |  {SYMBOL_NAME} ({SYMBOL})")
    print(f"{'=' * 72}")
    i_dir = intra_scored.direction or "无信号"
    r_dir = review_scored.direction or "无信号"
    print(f"  盘中策略: 得分={intra_scored.total_score:+.1f}  方向={i_dir}  信号数={intra_scored.signal_count}")
    print(f"  盘后回测: 得分={review_scored.total_score:+.1f}  方向={r_dir}  信号数={review_scored.signal_count}")
    diff = intra_scored.total_score - review_scored.total_score
    print(f"  得分差: {diff:+.1f}")

    intra_types = set(s.signal_type for s in intra_scored.signals)
    review_types = set(s.signal_type for s in review_scored.signals)
    common = intra_types & review_types
    only_intra = intra_types - review_types
    only_review = review_types - intra_types
    if common:
        print(f"  共同信号: {', '.join(common)}")
    if only_intra:
        print(f"  仅盘中: {', '.join(only_intra)}")
    if only_review:
        print(f"  仅回测: {', '.join(only_review)}")

    if i_dir != r_dir and i_dir != "无信号" and r_dir != "无信号":
        print(f"\n  ⚠️  盘中({i_dir}) 与 回测({r_dir}) 方向不一致！")
    elif i_dir == r_dir and i_dir != "无信号":
        print(f"\n  ✅ 盘中与回测方向一致: {i_dir}")

    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
