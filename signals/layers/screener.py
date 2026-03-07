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

from config import WHITELIST, MONITOR_FREQS, SCORE_THRESHOLD, FUTU_HOST, FUTU_PORT
from signals.data.fetcher import AKShareSource, FutuSource, USDataSource, detect_market
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
        max_workers: int = 12,
        notes: Optional[List] = None,
        data_source=None,
    ):
        self.symbols: List[str] = list(symbols or WHITELIST)
        self.freqs: List[str] = list(freqs or MONITOR_FREQS)
        self.czsc_freqs: List[Freq] = [config_freq_to_czsc(f) for f in self.freqs]
        self.max_workers = max_workers
        self.ak_source = data_source or AKShareSource()
        self._sim_source = data_source  # 仿真模式数据源
        # TODO: Tushare 充值后恢复以下三行
        # self._ts_source: Optional[TushareSource] = None
        # self._ts_failed: bool = False
        # self._ts_call_count: int = 0
        self._futu_source: Optional[FutuSource] = None
        self._futu_failed: bool = False
        self._us_source: Optional[USDataSource] = None
        self._ak_sina_degraded: bool = False     # Sina 连续失败熔断
        self._ak_consecutive_fails: int = 0      # Sina 连续失败计数
        self._em_degraded: bool = False          # 东财连续失败熔断
        self._em_consecutive_fails: int = 0      # 东财连续失败计数
        self.notes = notes or []
        # Dict[symbol][freq.value] -> SymbolAnalyzer  (Rust Freq 不可哈希，用 str)
        self.analyzers: Dict[str, Dict[str, SymbolAnalyzer]] = {}

    # ─────────────────────────────────────────────────────
    # 初始化：并发拉取历史分钟线，创建 CZSC Analyzer
    # ─────────────────────────────────────────────────────

    # TODO: Tushare 充值后恢复 _get_tushare() 和 _tushare_get_with_throttle()
    # 当前 token 等级限制 2次/天，无法作为分钟线降级源

    def _get_futu(self) -> Optional[FutuSource]:
        """懒初始化 Futu 连接"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()

        if self._futu_failed:
            return None
        if self._futu_source is None:
            try:
                self._futu_source = FutuSource(host=FUTU_HOST, port=FUTU_PORT)
                self._futu_source.connect()
                msg = "  [Futu] 连接成功，可用作分钟线降级源"
                if dash:
                    dash.detail(msg)
                else:
                    print(msg, flush=True)
            except Exception as e:
                msg = f"  [Futu] 连接失败（{e}），跳过"
                if dash:
                    dash.log(msg)
                else:
                    print(msg, flush=True)
                self._futu_failed = True
                return None
        return self._futu_source

    def _fetch_minute_bars(self, sym: str, freq: Freq) -> List:
        """
        根据市场路由到正确的数据源获取分钟线。
        A股：AKShare(Sina) → AKShare(东财) → Futu(如有A股权限)
        美股：USDataSource 内部降级链

        集成 MinuteCache：每次把 API 数据追加到本地 SQLite 缓存，
        随时间推移分钟线窗口从 5 天自动扩展到 20-60 天。
        """
        # 从 API 获取最新分钟线
        fresh = self._fetch_minute_bars_api(sym, freq)

        # 仿真模式：不写入实盘 MinuteCache，直接返回
        if self._sim_source:
            return fresh

        # 缓存层：合并新数据 + 读取全量
        try:
            from signals.data.minute_cache import MinuteCache
            cache = MinuteCache()
            if fresh:
                cache.merge(sym, freq.value, fresh)
            all_bars = cache.get(sym, freq.value)
            cache.close()
            if all_bars:
                return all_bars
        except Exception:
            pass  # 缓存异常不影响主流程

        return fresh

    def _fetch_minute_bars_api(self, sym: str, freq: Freq) -> List:
        """原始 API 获取逻辑（不含缓存）。"""
        # 仿真模式：直接从仿真数据源读取，跳过降级链
        if self._sim_source:
            return self._sim_source.get_a_minute(sym, freq) or []

        from signals.dashboard import get_dashboard
        dash = get_dashboard()

        market = detect_market(sym)
        if market == "A":
            # 主路径：AKShare Sina（连续失败 3 次后熔断跳过）
            if not self._ak_sina_degraded:
                try:
                    bars = self.ak_source.get_a_minute(sym, freq)
                    if bars:
                        self._ak_consecutive_fails = 0
                        return bars
                except Exception as e:
                    msg = f"    [Sina] {sym} {freq.value} 失败: {e.__class__.__name__}"
                    if dash:
                        dash.detail(msg)
                    else:
                        print(msg, flush=True)
                self._ak_consecutive_fails += 1
                if self._ak_consecutive_fails >= 3 and not self._ak_sina_degraded:
                    self._ak_sina_degraded = True
                    msg = "  [!] AKShare(Sina) 连续3次失败，后续直接走东财"
                    if dash:
                        dash.log(msg)
                        dash.degradation("AKShare(Sina)", "东财", "连续3次失败")
                    else:
                        print(msg, flush=True)
            # 降级1：AKShare 东财（push2his，间歇性SSL超时，快速放弃走 Futu）
            if not self._em_degraded:
                try:
                    bars = self.ak_source.get_a_minute_em(sym, freq, max_retries=1)
                    if bars:
                        self._em_consecutive_fails = 0
                        return bars
                except Exception as e:
                    msg = f"    [东财] {sym} {freq.value} 失败: {e.__class__.__name__}"
                    if dash:
                        dash.detail(msg)
                    else:
                        print(msg, flush=True)
                self._em_consecutive_fails += 1
                if self._em_consecutive_fails >= 3:
                    self._em_degraded = True
                    msg = "  [!] AKShare(东财) 连续3次失败，后续跳过"
                    if dash:
                        dash.log(msg)
                        dash.degradation("AKShare(东财)", "跳过", "连续3次失败")
                    else:
                        print(msg, flush=True)
            # 降级2：Futu（需A股行情权限）
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
                from signals.data.us_factory import create_us_source
                self._us_source = create_us_source("intraday")
            return self._us_source.get_us_minute(sym, freq)
        return []

    def initialize(self, symbols: Optional[List[str]] = None):
        """并发拉取分钟线，为所有标的 × 所有级别创建 SymbolAnalyzer。"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()

        target = symbols or self.symbols
        # 按市场分组：A股可并发，US股顺序获取
        a_tasks = [(sym, freq) for sym in target for freq in self.czsc_freqs
                   if detect_market(sym) == "A"]
        us_tasks = [(sym, freq) for sym in target for freq in self.czsc_freqs
                    if detect_market(sym) == "US"]

        total = len(a_tasks) + len(us_tasks)
        _log = dash.log if dash else lambda m: print(m, flush=True)
        _detail = dash.detail if dash else lambda m: print(m, flush=True)
        _detail(f"[{_ts()}] 初始化 {len(target)} 只 × {len(self.czsc_freqs)} 级别 = {total} 个 analyzer ...")
        if a_tasks:
            _detail(f"  A股: {len(a_tasks)} 个（并发），美股: {len(us_tasks)} 个（顺序）")

        if dash:
            dash.phase_start("L3.init", total=total)

        # A股预热：测试 AKShare(Sina) 可用性
        ak_ok = True
        if a_tasks:
            _warm_sym, _warm_freq = a_tasks[0]
            try:
                test_bars = self.ak_source.get_a_minute(_warm_sym, _warm_freq)
                if not test_bars:
                    raise ValueError("AKShare 返回空数据")
                _detail(f"  AKShare(Sina) 预热成功 ✓")
            except Exception as e:
                ak_ok = False
                self._ak_sina_degraded = True
                _log(f"  [!] AKShare(Sina) 不可用（{e}）")
                if dash:
                    dash.degradation("AKShare(Sina)", "东财", str(e))
                # 快速测试东财可用性
                try:
                    em_test = self.ak_source.get_a_minute_em(_warm_sym, _warm_freq,
                                                             max_retries=1)
                    if em_test:
                        _log(f"  → AKShare(东财) 可用 ✓")
                    else:
                        raise ValueError("东财返回空数据")
                except Exception:
                    self._em_degraded = True
                    _log(f"  [!] AKShare(东财) 也不可用")
                    if dash:
                        dash.degradation("AKShare(东财)", "Futu/跳过", "不可用")
                futu = self._get_futu()
                if self._em_degraded and not futu:
                    _log(f"  → 所有A股分钟线数据源不可用，跳过A股任务")
                    a_tasks = []
                elif not futu:
                    _log(f"  → Futu 不可用，仅东财兜底")

        # A股获取（统一走 _fetch_minute_bars，内含 fallback）
        def _fetch_and_build(sym: str, freq: Freq):
            bars = self._fetch_minute_bars(sym, freq)
            return sym, freq, bars

        # 预热已完成 V8 初始化，后续并行安全
        workers = self.max_workers if ak_ok else 1
        _done = 0
        _total_a = len(a_tasks)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_fetch_and_build, sym, freq): (sym, freq)
                    for sym, freq in a_tasks}
            for fut in as_completed(futs):
                sym, freq = futs[fut]
                _done += 1
                try:
                    _, _, bars = fut.result()
                    self._store_analyzer(sym, freq, bars)
                    if dash:
                        dash.task_done("L3.init", f"{sym} {freq.value}")
                except Exception as e:
                    _log(f"  错误 {sym} {freq.value}: {e}")
                    if dash:
                        dash.task_error("L3.init", f"{sym} {freq.value}", str(e))
                # 无 dashboard 时保留原进度输出
                if not dash and (_done % 10 == 0 or _done == _total_a):
                    print(f"  ── 进度: {_done}/{_total_a} ({_done*100//_total_a}%)", flush=True)

        # 美股顺序获取（USDataSource 内部可能用 Futu 连接，非线程安全）
        for sym, freq in us_tasks:
            if dash:
                dash.task_start("L3.init", f"{sym} {freq.value}")
            try:
                bars = self._fetch_minute_bars(sym, freq)
                self._store_analyzer(sym, freq, bars)
                if dash:
                    dash.task_done("L3.init", f"{sym} {freq.value}")
            except Exception as e:
                _log(f"  错误 {sym} {freq.value}: {e}")
                if dash:
                    dash.task_error("L3.init", f"{sym} {freq.value}", str(e))

        if dash:
            dash.phase_end("L3.init",
                           detail=f"{sum(len(v) for v in self.analyzers.values())} analyzers")

        _detail(f"[{_ts()}] 初始化完成，共 {sum(len(v) for v in self.analyzers.values())} 个 analyzer。")

    def _store_analyzer(self, sym: str, freq: Freq, bars):
        """存储 Analyzer 并打印状态"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()
        _detail = dash.detail if dash else lambda m: print(m, flush=True)

        if sym not in self.analyzers:
            self.analyzers[sym] = {}
        if bars:
            self.analyzers[sym][freq.value] = SymbolAnalyzer(sym, freq, bars)
            bi_cnt = len(self.analyzers[sym][freq.value].bi_list)
            _detail(f"  OK  {sym} {freq.value:6s} {len(bars):5d} bars  {bi_cnt:3d} 笔")
        else:
            _detail(f"  跳过 {sym} {freq.value} — 无数据")

    # ─────────────────────────────────────────────────────
    # 扫描：信号检测 + 评分
    # ─────────────────────────────────────────────────────
    def scan_once(self, symbols: Optional[List[str]] = None,
                  market_direction: str = "分化") -> List[ScoredSymbol]:
        """对所有（或指定）标的运行信号检测，返回按评分降序排列的结果。"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()

        target = symbols or list(self.analyzers.keys())
        if dash:
            dash.phase_start("L3.scan", total=len(target))

        results = []
        for sym in target:
            if dash:
                dash.task_start("L3.scan", sym)
            all_signals = []
            for _fkey, analyzer in self.analyzers.get(sym, {}).items():
                sigs = detect_all_signals(analyzer.czsc, sym)
                all_signals.extend(sigs)
            results.append(score_signals(sym, all_signals))
            if dash:
                dash.task_done("L3.scan", sym)

        results.sort(key=lambda x: x.total_score, reverse=True)

        # 信号存档（回测验证用，异常不影响主流程）
        try:
            from signals.core.backtest import archive_signals
            archive_signals(results, market_direction=market_direction)
        except Exception:
            pass

        if dash:
            above = sum(1 for r in results if r.total_score >= SCORE_THRESHOLD)
            dash.phase_end("L3.scan", detail=f"{above} 只达标")

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
                    from signals.dashboard import get_dashboard as _gd
                    _d = _gd()
                    (_d.detail if _d else lambda m: print(m, flush=True))(f"  刷新错误: {e}")

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
                        from signals.dashboard import get_dashboard as _gd
                        _d = _gd()
                        (_d.detail if _d else lambda m: print(m, flush=True))(f"  刷新错误 {sym} {freq.value}: {e}")

    # ─────────────────────────────────────────────────────
    # 输出
    # ─────────────────────────────────────────────────────
    def print_results(self, results: List[ScoredSymbol], title: str = "筛选结果",
                      resolver=None):
        from signals.research import match_notes_for_symbol, check_resonance

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{'='*78}")
        print(f"  {title}  |  {now}")
        print(f"{'='*78}")

        has_signal = any(r.signal_count > 0 for r in results)
        if not has_signal:
            print("  当前无信号（市场结构尚未触发买卖点）")
            print(f"{'='*78}\n")
            return

        above = [r for r in results
                 if r.total_score >= SCORE_THRESHOLD and r.signal_count > 0]
        below = [r for r in results
                 if r.signal_count > 0 and r.total_score < SCORE_THRESHOLD]

        if above:
            print(f"\n  {'排名':<4} {'代码':<12} {'名称':<10} {'行业':<8} "
                  f"{'方向':<4} {'技术分':>6} {'信号':>4}  关键信号")
            print("  " + "─" * 74)

            for i, r in enumerate(above, 1):
                name = resolver.get_name(r.symbol) if resolver else r.symbol
                industry = resolver.get_industry(r.symbol) if resolver else ""
                # 截断避免对齐错位
                name = name[:8] if len(name) > 8 else name
                industry = industry[:6] if len(industry) > 6 else industry
                dir_str = r.direction or "─"
                key_sigs = _summarize_signals(r.signals)

                note_view = match_notes_for_symbol(r.symbol, self.notes)
                resonance = check_resonance(r.total_score, note_view)
                res_tag = f" {resonance}" if resonance else ""

                print(f"  {i:<4} {r.symbol:<12} {name:<10} {industry:<8} "
                      f"{dir_str:<4} {r.total_score:>6.1f} {r.signal_count:>4}  "
                      f"{key_sigs}{res_tag}")

            print("  " + "─" * 74)

        if below:
            top_below = below[0].total_score if below else 0
            print(f"  未达标 ({SCORE_THRESHOLD}分以下): {len(below)} 只"
                  f"  最高分: {top_below:.1f}")

        print(f"{'='*78}\n")

    # ─────────────────────────────────────────────────────
    # 运行模式
    # ─────────────────────────────────────────────────────
    def run_whitelist(self) -> List[ScoredSymbol]:
        """第 1 轮：白名单快扫。"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()
        _detail = dash.detail if dash else lambda m: print(m, flush=True)
        _detail(f"第 1 轮 — 白名单快扫（{len(self.symbols)} 只）")
        self.initialize(self.symbols)
        results = self.scan_once(self.symbols)
        self.print_results(results, title="白名单筛选结果")
        return results

    def run_industry(self, industry: str) -> List[ScoredSymbol]:
        """第 2 轮：行业批扫。"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()
        _detail = dash.detail if dash else lambda m: print(m, flush=True)
        _log = dash.log if dash else lambda m: print(m, flush=True)
        _detail(f"第 2 轮 — 行业批扫：{industry}")
        ind_symbols = get_industry_stocks(industry)
        if not ind_symbols:
            _log(f"  [!] 未获取到 '{industry}' 的成分股")
            return []
        _detail(f"  获取到 {len(ind_symbols)} 只成分股")
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

    def close(self):
        """释放数据源连接（Futu socket / US providers）"""
        if self._futu_source:
            try:
                self._futu_source.close()
            except Exception:
                pass
            self._futu_source = None
        if self._us_source:
            try:
                self._us_source.close()
            except Exception:
                pass
            self._us_source = None


def _summarize_signals(signals: list) -> str:
    """将信号列表压缩为一行摘要，如 '三买(15M+30M) 趋势买(30M)'"""
    freq_abbrev = {
        "15分钟": "15M", "30分钟": "30M", "60分钟": "60M",
        "日线": "日", "周线": "周", "5分钟": "5M", "1分钟": "1M",
    }
    sig_freq_map: dict = {}
    for sig in signals:
        abbr = freq_abbrev.get(sig.freq, sig.freq)
        sig_freq_map.setdefault(sig.signal_type, []).append(abbr)
    parts = []
    for sig_type, freqs in sig_freq_map.items():
        freq_str = "+".join(freqs)
        parts.append(f"{sig_type}({freq_str})")
    return " ".join(parts[:4])


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")
