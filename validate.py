# -*- coding: utf-8 -*-
"""
端到端验证脚本

运行方式：
    python validate.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from signals.core.freq_utils import config_freq_to_czsc, FREQ_MAP
from signals.core.analyzer import SymbolAnalyzer
from signals.core.detectors import detect_all_signals
from signals.core.scorer import score_signals
from signals.layers.industry import get_industry_list, get_industry_stocks
from signals.layers.screener import IntraDayScreener


TEST_SYMBOLS = ["SH.601958", "SH.600519", "SZ.000001"]
TEST_FREQS   = ["15min", "30min"]

SEP = "=" * 70


def section(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def main():
    print(SEP)
    print("  🐲 隆小侠 LONG CLAW — 端到端验证")
    print(SEP)

    # ── Step 1: 频率映射 ───────────────────────────────────
    section("Step 1 / 频率映射")
    for name, freq in FREQ_MAP.items():
        print(f"  {name:8s} → {freq.value}")
    print("  PASS ✓")

    # ── Step 2: 数据加载 + CZSC 分析 ──────────────────────
    section("Step 2 / 数据加载 + CZSC 分析（并发）")
    screener = IntraDayScreener(symbols=TEST_SYMBOLS, freqs=TEST_FREQS, max_workers=5)
    screener.initialize(TEST_SYMBOLS)

    print("\n  Analyzer 摘要：")
    for sym in TEST_SYMBOLS:
        for freq_str, freq_obj in zip(TEST_FREQS, screener.czsc_freqs):
            az = screener.analyzers.get(sym, {}).get(freq_obj.value)
            if az:
                czsc = az.czsc
                last_bi = czsc.bi_list[-1] if czsc.bi_list else None
                bi_dir = last_bi.direction if last_bi else "N/A"
                print(
                    f"  {sym} {freq_str:6s}  "
                    f"bars={len(czsc.bars_raw):5d}  "
                    f"分型={len(czsc.fx_list):3d}  "
                    f"笔={len(czsc.bi_list):3d}  "
                    f"最后一笔={bi_dir}"
                )
            else:
                print(f"  {sym} {freq_str} — 无数据")

    # ── Step 3: 信号检测 ───────────────────────────────────
    section("Step 3 / 信号检测")
    total_signals = 0
    for sym in TEST_SYMBOLS:
        for freq_obj in screener.czsc_freqs:
            az = screener.analyzers.get(sym, {}).get(freq_obj.value)
            if not az:
                continue
            sigs = detect_all_signals(az.czsc, sym)
            total_signals += len(sigs)
            if sigs:
                print(f"\n  {sym} {freq_obj.value}:")
                for s in sigs:
                    print(f"    [{s.signal_type}] conf={s.confidence:.2f}  @ {s.price:.2f}  {s.details}")
            else:
                print(f"  {sym} {freq_obj.value}: 无信号")

    # ── Step 4: 评分排序 ───────────────────────────────────
    section("Step 4 / 评分排序")
    results = screener.scan_once(TEST_SYMBOLS)
    screener.print_results(results, title="验证用筛选结果")

    # ── Step 5: 行业接口 ───────────────────────────────────
    section("Step 5 / 行业成分股接口")
    try:
        test_industry = "有色金属"
        stocks = get_industry_stocks(test_industry)
        print(f"  '{test_industry}' 行业成分股: {len(stocks)} 只")
        print(f"  前 5 只: {stocks[:5]}")
        print("  PASS ✓")
    except Exception as e:
        print(f"  行业接口异常（不影响主管道）: {e}")

    # ── Step 6: 美股数据验证（yfinance 兜底）──────────────
    section("Step 6 / 美股数据验证（yfinance）")
    us_ok = False
    try:
        from signals.data.fetcher import YFinanceSource, detect_market
        from czsc import CZSC

        yf_source = YFinanceSource()
        us_sym = "US.AAPL"
        print(f"  detect_market('{us_sym}') = {detect_market(us_sym)}")

        # 日线
        bars_d = yf_source.get_us_daily(us_sym, period="6mo")
        print(f"  日线: {len(bars_d)} 根  ({bars_d[0].dt.date()} ~ {bars_d[-1].dt.date()})" if bars_d else "  日线: 无数据")

        # 分钟线
        from czsc import Freq
        bars_15 = yf_source.get_us_minute(us_sym, Freq.F15)
        print(f"  15min: {len(bars_15)} 根" if bars_15 else "  15min: 无数据")

        # CZSC 分析
        if bars_d and len(bars_d) >= 100:
            c = CZSC(bars_d, max_bi_num=50)
            print(f"  CZSC 日线: {len(c.bi_list)} 笔  PASS ✓")
            us_ok = True
        elif bars_d:
            print(f"  CZSC 日线: 数据不足（{len(bars_d)} < 100）跳过")
            us_ok = True
        else:
            print("  CZSC 日线: 无数据")
    except ImportError:
        print("  [!] yfinance 未安装（pip install yfinance）")
    except Exception as e:
        print(f"  [!] 美股验证异常: {e}")

    # ── 汇总 ──────────────────────────────────────────────
    section("验证汇总")
    analyzers_ok = sum(len(v) for v in screener.analyzers.values())
    print(f"  Analyzer 数量: {analyzers_ok} / {len(TEST_SYMBOLS) * len(TEST_FREQS)} 期望")
    print(f"  检测到信号总数: {total_signals}")
    print(f"  美股数据（yfinance）: {'PASS ✓' if us_ok else 'FAIL ✗'}")
    print()

    if analyzers_ok > 0:
        print("  ✅ 管道核心功能正常（数据加载、CZSC 分析、信号检测、评分排序）")
    else:
        print("  ❌ 未能创建任何 Analyzer，请检查网络或 AKShare 接口")

    if us_ok:
        print("  ✅ 美股数据通道正常（yfinance 兜底可用）")

    if total_signals == 0:
        print("  ℹ️  当前市场结构未触发买卖点（正常，信号并非随时出现）")
        print("     可通过检查笔数 > 10 来确认 CZSC 分析已正常运行")

    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
