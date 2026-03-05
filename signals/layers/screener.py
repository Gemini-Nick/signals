# -*- coding: utf-8 -*-
"""
主筛选器 —— 两轮模式 + 并发拉取

第 1 轮（白名单快扫）：screener.run_whitelist()           ~1 分钟
第 2 轮（行业批扫）：  screener.run_industry("有色金属")  ~5 分钟
两轮合并：            screener.run_full("有色金属")        ~6 分钟
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional

from config import WHITELIST, MONITOR_FREQS, SCORE_THRESHOLD
from signals.data.fetcher import AKShareSource, USDataSource, detect_market
from czsc import Freq

from signals.core.freq_utils import config_freq_to_czsc
from signals.core.analyzer import SymbolAnalyzer
from signals.core.detectors import detect_all_signals
from signals.core.scorer import score_signals, ScoredSymbol
from .industry import get_industry_stocks


class IntraDayScreener:
    """盘中实时筛选器。"""

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        freqs: Optional[List[str]] = None,
        max_workers: int = 5,
        notes: Optional[List] = None,
    ):
        self.symbols: List[str] = list(symbols or WHITELIST)
        self.freqs: List[str] = list(freqs or MONITOR_FREQS)
        self.czsc_freqs: List[Freq] = [config_freq_to_czsc(f) for f in self.freqs]
        self.max_workers = max_workers
        self.ak_source = AKShareSource()
        self._us_source: Optional[USDataSource] = None
        self.notes = notes or []
        # Dict[symbol][freq.value] -> SymbolAnalyzer  (Rust Freq 不可哈希，用 str)
        self.analyzers: Dict[str, Dict[str, SymbolAnalyzer]] = {}

    # ─────────────────────────────────────────────────────
    # 初始化：并发拉取历史分钟线，创建 CZSC Analyzer
    # ─────────────────────────────────────────────────────
    def _fetch_minute_bars(self, sym: str, freq: Freq) -> List:
        """根据市场路由到正确的数据源获取分钟线"""
        from czsc import RawBar
        market = detect_market(sym)
        if market == "A":
            return self.ak_source.get_a_minute(sym, freq)
        elif market == "US":
            if not self._us_source:
                from signals.data.us_factory import create_us_source
                self._us_source = create_us_source("intraday")
            return self._us_source.get_us_minute(sym, freq)
        return []

    def initialize(self, symbols: Optional[List[str]] = None):
        """并发拉取分钟线，为所有标的 × 所有级别创建 SymbolAnalyzer。"""
        target = symbols or self.symbols
        # 按市场分组：A股可并发，US股顺序获取
        a_tasks = [(sym, freq) for sym in target for freq in self.czsc_freqs
                   if detect_market(sym) == "A"]
        us_tasks = [(sym, freq) for sym in target for freq in self.czsc_freqs
                    if detect_market(sym) == "US"]

        total = len(a_tasks) + len(us_tasks)
        print(f"[{_ts()}] 初始化 {len(target)} 只 × {len(self.czsc_freqs)} 级别 = {total} 个 analyzer ...")
        if a_tasks:
            print(f"  A股: {len(a_tasks)} 个（并发），美股: {len(us_tasks)} 个（顺序）")

        # A股预热 + 并发获取
        if a_tasks:
            _warm_sym, _warm_freq = a_tasks[0]
            self.ak_source.get_a_minute(_warm_sym, _warm_freq)

        def _fetch_and_build(sym: str, freq: Freq):
            bars = self.ak_source.get_a_minute(sym, freq)
            return sym, freq, bars

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {pool.submit(_fetch_and_build, sym, freq): (sym, freq)
                    for sym, freq in a_tasks}
            for fut in as_completed(futs):
                sym, freq = futs[fut]
                try:
                    _, _, bars = fut.result()
                    self._store_analyzer(sym, freq, bars)
                except Exception as e:
                    print(f"  错误 {sym} {freq.value}: {e}")

        # 美股顺序获取（USDataSource 内部可能用 Futu 连接，非线程安全）
        for sym, freq in us_tasks:
            try:
                bars = self._fetch_minute_bars(sym, freq)
                self._store_analyzer(sym, freq, bars)
            except Exception as e:
                print(f"  错误 {sym} {freq.value}: {e}")

        print(f"[{_ts()}] 初始化完成，共 {sum(len(v) for v in self.analyzers.values())} 个 analyzer。\n")

    def _store_analyzer(self, sym: str, freq: Freq, bars):
        """存储 Analyzer 并打印状态"""
        if sym not in self.analyzers:
            self.analyzers[sym] = {}
        if bars:
            self.analyzers[sym][freq.value] = SymbolAnalyzer(sym, freq, bars)
            bi_cnt = len(self.analyzers[sym][freq.value].bi_list)
            print(f"  OK  {sym} {freq.value:6s} {len(bars):5d} bars  {bi_cnt:3d} 笔")
        else:
            print(f"  跳过 {sym} {freq.value} — 无数据")

    # ─────────────────────────────────────────────────────
    # 扫描：信号检测 + 评分
    # ─────────────────────────────────────────────────────
    def scan_once(self, symbols: Optional[List[str]] = None) -> List[ScoredSymbol]:
        """对所有（或指定）标的运行信号检测，返回按评分降序排列的结果。"""
        target = symbols or list(self.analyzers.keys())
        results = []

        for sym in target:
            all_signals = []
            for _fkey, analyzer in self.analyzers.get(sym, {}).items():
                sigs = detect_all_signals(analyzer.czsc, sym)
                all_signals.extend(sigs)
            results.append(score_signals(sym, all_signals))

        results.sort(key=lambda x: x.total_score, reverse=True)
        return results

    # ─────────────────────────────────────────────────────
    # 刷新数据（增量更新）
    # ─────────────────────────────────────────────────────
    def refresh_data(self, symbols: Optional[List[str]] = None):
        """重新拉取分钟线并增量更新各 Analyzer。"""
        target = symbols or list(self.analyzers.keys())

        # A股并发刷新
        a_targets = [(sym, freq) for sym in target for freq in self.czsc_freqs
                     if detect_market(sym) == "A"
                     and sym in self.analyzers and freq.value in self.analyzers[sym]]

        def _refresh_a(sym: str, freq: Freq):
            new_bars = self.ak_source.get_a_minute(sym, freq)
            self.analyzers[sym][freq.value].update_many(new_bars)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = [pool.submit(_refresh_a, sym, freq) for sym, freq in a_targets]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    print(f"  刷新错误: {e}")

        # 美股顺序刷新
        for sym in target:
            if detect_market(sym) != "US":
                continue
            for freq in self.czsc_freqs:
                if sym in self.analyzers and freq.value in self.analyzers[sym]:
                    try:
                        new_bars = self._fetch_minute_bars(sym, freq)
                        self.analyzers[sym][freq.value].update_many(new_bars)
                    except Exception as e:
                        print(f"  刷新错误 {sym} {freq.value}: {e}")

    # ─────────────────────────────────────────────────────
    # 输出
    # ─────────────────────────────────────────────────────
    def print_results(self, results: List[ScoredSymbol], title: str = "筛选结果"):
        from signals.research import match_notes_for_symbol, check_resonance

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'='*70}")
        print(f"  {title}  |  {now}")
        print(f"{'='*70}")

        has_signal = any(r.signal_count > 0 for r in results)
        if not has_signal:
            print("  当前无信号（市场结构尚未触发买卖点）")
            print(f"{'='*70}\n")
            return

        for r in results:
            if r.signal_count == 0:
                continue
            marker = ">>>" if r.total_score >= SCORE_THRESHOLD else "   "

            # 双维度展示：技术面 + 研报
            note_view = match_notes_for_symbol(r.symbol, self.notes)
            resonance = check_resonance(r.total_score, note_view)
            note_tag = ""
            if note_view.has_coverage:
                note_tag = f"  |  研报: {note_view.label}"
            if resonance:
                note_tag += f"  {resonance}"

            print(f"\n{marker} {r.symbol}  技术分: {r.total_score}  信号数: {r.signal_count}{note_tag}")
            print(r.details)
            if note_view.catalysts:
                print(f"  [研报催化] {'、'.join(note_view.catalysts)}")

        above = [r for r in results if r.total_score >= SCORE_THRESHOLD]
        print(f"\n--- 达到阈值 ({SCORE_THRESHOLD} 分) 的标的: {len(above)} 只 ---")
        if above:
            for r in above:
                nv = match_notes_for_symbol(r.symbol, self.notes)
                res = check_resonance(r.total_score, nv)
                extra = f"  {nv.label} {res}" if nv.has_coverage else ""
                print(f"    {r.symbol}  技术分={r.total_score}{extra}")
        print(f"{'='*70}\n")

    # ─────────────────────────────────────────────────────
    # 运行模式
    # ─────────────────────────────────────────────────────
    def run_whitelist(self) -> List[ScoredSymbol]:
        """第 1 轮：白名单快扫。"""
        print(f"\n{'─'*40}")
        print(f"第 1 轮 — 白名单快扫（{len(self.symbols)} 只）")
        print(f"{'─'*40}")
        self.initialize(self.symbols)
        results = self.scan_once(self.symbols)
        self.print_results(results, title="白名单筛选结果")
        return results

    def run_industry(self, industry: str) -> List[ScoredSymbol]:
        """第 2 轮：行业批扫。"""
        print(f"\n{'─'*40}")
        print(f"第 2 轮 — 行业批扫：{industry}")
        print(f"{'─'*40}")
        ind_symbols = get_industry_stocks(industry)
        if not ind_symbols:
            print(f"  未获取到 '{industry}' 的成分股，请检查行业名称。")
            return []
        print(f"  获取到 {len(ind_symbols)} 只成分股")
        self.initialize(ind_symbols)
        results = self.scan_once(ind_symbols)
        self.print_results(results, title=f"{industry} 行业筛选结果")
        return results

    def run_full(self, industry: str) -> List[ScoredSymbol]:
        """两轮合并：白名单 + 行业，去重后合并排序。"""
        r1 = self.run_whitelist()
        r2 = self.run_industry(industry)

        # 合并，同一标的取更高分
        combined: Dict[str, ScoredSymbol] = {}
        for r in r1 + r2:
            if r.symbol not in combined or r.total_score > combined[r.symbol].total_score:
                combined[r.symbol] = r

        merged = sorted(combined.values(), key=lambda x: x.total_score, reverse=True)
        self.print_results(merged, title=f"综合筛选结果（白名单 + {industry}）")
        return merged

    def run_loop(self, interval_seconds: int = 300):
        """持续轮询模式（默认 5 分钟刷新一次）。"""
        self.initialize()
        print(f"\n持续扫描已启动（刷新间隔 {interval_seconds}s）。Ctrl+C 停止。\n")
        try:
            while True:
                results = self.scan_once()
                self.print_results(results)
                print(f"下次刷新: {interval_seconds}s 后 ...")
                time.sleep(interval_seconds)
                self.refresh_data()
        except KeyboardInterrupt:
            print("\n已停止。")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")
