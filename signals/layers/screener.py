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

from config import WHITELIST, MONITOR_FREQS, SCORE_THRESHOLD, FUTU_HOST, FUTU_PORT, TUSHARE_TOKEN
from signals.data.fetcher import AKShareSource, TushareSource, FutuSource, USDataSource, detect_market
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
        self._ts_source: Optional[TushareSource] = None
        self._ts_failed: bool = False
        self._ts_call_count: int = 0             # Tushare 限速计数
        self._futu_source: Optional[FutuSource] = None
        self._futu_failed: bool = False
        self._us_source: Optional[USDataSource] = None
        self.notes = notes or []
        # Dict[symbol][freq.value] -> SymbolAnalyzer  (Rust Freq 不可哈希，用 str)
        self.analyzers: Dict[str, Dict[str, SymbolAnalyzer]] = {}

    # ─────────────────────────────────────────────────────
    # 初始化：并发拉取历史分钟线，创建 CZSC Analyzer
    # ─────────────────────────────────────────────────────
    def _get_tushare(self) -> Optional[TushareSource]:
        """懒初始化 Tushare（限频 2次/分钟）"""
        if self._ts_failed:
            return None
        if self._ts_source is None:
            if not TUSHARE_TOKEN:
                print("  [Tushare] 无 Token，跳过")
                self._ts_failed = True
                return None
            try:
                self._ts_source = TushareSource(TUSHARE_TOKEN)
                print("  [Tushare] 初始化成功，可用作分钟线降级源（限频 2次/分钟）")
            except Exception as e:
                print(f"  [Tushare] 初始化失败（{e}）")
                self._ts_failed = True
                return None
        return self._ts_source

    def _tushare_get_with_throttle(self, sym: str, freq: Freq) -> List:
        """Tushare 限速获取：每2次调用后 sleep 31s"""
        ts = self._get_tushare()
        if not ts:
            return []
        # 限速：每 2 次 sleep 31s
        if self._ts_call_count > 0 and self._ts_call_count % 2 == 0:
            print(f"    [Tushare限速] 已调用{self._ts_call_count}次，等待31s ...", flush=True)
            time.sleep(31)
        try:
            bars = ts.get_minute(sym, freq)
            self._ts_call_count += 1
            return bars
        except Exception as e:
            err = str(e)
            if "每分钟最多" in err:
                print(f"    [Tushare限速] 触发频率限制，等待31s ...", flush=True)
                time.sleep(31)
                try:
                    bars = ts.get_minute(sym, freq)
                    self._ts_call_count += 1
                    return bars
                except Exception as e2:
                    print(f"    [Tushare] {sym} {freq.value} 重试失败: {e2}")
            else:
                print(f"    [Tushare] {sym} {freq.value} 失败: {e}")
            return []

    def _get_futu(self) -> Optional[FutuSource]:
        """懒初始化 Futu 连接"""
        if self._futu_failed:
            return None
        if self._futu_source is None:
            try:
                self._futu_source = FutuSource(host=FUTU_HOST, port=FUTU_PORT)
                self._futu_source.connect()
                print("  [Futu] 连接成功，可用作分钟线降级源")
            except Exception as e:
                print(f"  [Futu] 连接失败（{e}），跳过")
                self._futu_failed = True
                return None
        return self._futu_source

    def _fetch_minute_bars(self, sym: str, freq: Freq) -> List:
        """
        根据市场路由到正确的数据源获取分钟线。
        A股：AKShare(Sina) → Tushare(限速) → Futu(如有A股权限)
        美股：Futu → yfinance fallback（USDataSource 内部处理）
        """
        market = detect_market(sym)
        if market == "A":
            # 主路径：AKShare (Sina, 免费无限制)
            try:
                bars = self.ak_source.get_a_minute(sym, freq)
                if bars:
                    return bars
            except Exception:
                pass
            # 降级1：Tushare (限频 2次/分钟)
            bars = self._tushare_get_with_throttle(sym, freq)
            if bars:
                return bars
            # 降级2：Futu (需A股行情权限)
            futu = self._get_futu()
            if futu:
                try:
                    bars = futu.get_a_minute(sym, freq)
                    if bars:
                        return bars
                except Exception:
                    pass
            return []
        elif market == "US":
            if not self._us_source:
                self._us_source = USDataSource()
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

        # A股预热：测试 AKShare 可用性
        ak_ok = True
        if a_tasks:
            _warm_sym, _warm_freq = a_tasks[0]
            try:
                test_bars = self.ak_source.get_a_minute(_warm_sym, _warm_freq)
                if not test_bars:
                    raise ValueError("AKShare 返回空数据")
                print(f"  AKShare(Sina) 预热成功 ✓")
            except Exception as e:
                ak_ok = False
                print(f"  [!] AKShare(Sina) 不可用（{e}）", flush=True)
                # 尝试初始化降级源
                ts = self._get_tushare()
                futu = self._get_futu()
                if ts:
                    # Tushare 限频 2次/分钟，大批量不现实
                    max_ts_stocks = 10  # 最多用 Tushare 处理 10 只
                    unique_syms = list(dict.fromkeys(s for s, _ in a_tasks))
                    if len(unique_syms) > max_ts_stocks:
                        print(f"  → Tushare 限频（2次/分钟），{len(unique_syms)}只太多，"
                              f"仅处理前{max_ts_stocks}只", flush=True)
                        keep = set(unique_syms[:max_ts_stocks])
                        a_tasks = [(s, f) for s, f in a_tasks if s in keep]
                    est_min = len(a_tasks) // 2
                    print(f"  → 使用 Tushare 降级（{len(a_tasks)}个任务，"
                          f"预计{est_min}~{est_min+1}分钟）", flush=True)
                elif futu:
                    print(f"  → 使用 Futu 降级", flush=True)
                else:
                    print(f"  → 无可用降级源，A股分钟线将全部跳过", flush=True)

        # A股获取（统一走 _fetch_minute_bars，内含 fallback）
        def _fetch_and_build(sym: str, freq: Freq):
            bars = self._fetch_minute_bars(sym, freq)
            return sym, freq, bars

        # AKShare 可用时并发；Tushare 需限速串行（workers=1）
        workers = self.max_workers if ak_ok else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
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
            new_bars = self._fetch_minute_bars(sym, freq)
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
