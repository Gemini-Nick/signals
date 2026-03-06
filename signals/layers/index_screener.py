# -*- coding: utf-8 -*-
"""
IndexScreener: 指数层入口（Layer 1）
支持两种模式：
  - 盘中模式 initialize(lookback_days)：滚动窗口，近 N 自然日
  - 盘后模式 initialize_with_start(start_date)：固定起点历史数据

实线虚线框架三级联动：日线（趋势背景）+ 30分钟（中枢结构）+ 15分钟（买卖点）

数据源分工：
  - A股7只指数：
      日线：AKShare stock_zh_index_daily（免费，无额度）
      30min/15min：AKShare stock_zh_a_minute（近5日，免费）
  - 恒生科技（HK.800700）：
      三个周期均由 Futu request_history_kline 提供（消耗历史K线额度 3/run）
  - 美股指数ETF（US.SPY/QQQ/DIA）：
      Futu 优先（需美股行情权限），降级 yfinance（免费兜底）
  - Futu 不可用时：港股/美股指数降级或跳过
"""
from typing import Dict, List, Optional

from czsc import Freq

from .index_report import IndexReport
from .index_analyzer import IndexAnalyzer
from .market_context import MarketContext, build_market_context


class IndexScreener:
    """
    指数层分析器，支持盘中 / 盘后两种初始化模式。
    """

    def __init__(self,
                 ak_codes: Optional[Dict[str, str]] = None,
                 futu_codes: Optional[Dict[str, str]] = None,
                 us_codes: Optional[Dict[str, str]] = None,
                 futu_host: str = "127.0.0.1",
                 futu_port: int = 11111):
        import config
        self.ak_codes   = ak_codes   if ak_codes   is not None else config.INDEX_AK_CODES
        self.futu_codes = futu_codes if futu_codes is not None else config.INDEX_FUTU_CODES
        self.us_codes   = us_codes   if us_codes   is not None else getattr(config, "INDEX_US_CODES", {})
        self.futu_host  = futu_host
        self.futu_port  = futu_port
        self.analyzers: Dict[str, IndexAnalyzer] = {}
        self._futu_available = False

    # ────────────────────────────────
    # 盘中初始化（滚动窗口）
    # ────────────────────────────────

    def initialize(self, lookback_days: int = None):
        """
        盘中模式：拉取近 lookback_days 自然日数据（三个周期）。
        默认从 config.INDEX_LOOKBACK_DAYS 读取（180天≈120交易日）。
        注意：30min/15min 来自 AKShare stock_zh_a_minute，只有近5日数据。
        """
        import config
        lb = lookback_days or getattr(config, "INDEX_LOOKBACK_DAYS", 180)
        self._load_ak_indices(lookback_days=lb)
        self._load_futu_indices(lookback_days=lb)
        self._load_us_indices(lookback_days=lb)

    # ────────────────────────────────
    # 盘后初始化（固定起点）
    # ────────────────────────────────

    def initialize_with_start(self, start_date: str):
        """
        盘后复盘模式：从指定日期加载完整历史。
        日线：从 start_date 起全量；30min/15min：AKShare 仍限近5日（限制）。
        :param start_date: 格式 'YYYY-MM-DD'
        """
        self._load_ak_indices(start_date=start_date)
        self._load_futu_indices(start_date=start_date)
        self._load_us_indices(start_date=start_date)

    # ────────────────────────────────
    # 私有：AKShare 指数加载（三级）
    # ────────────────────────────────

    def _load_ak_indices(self, lookback_days: int = None, start_date: str = None):
        """
        加载 A股指数三个周期（并行，IO密集型）：
        - 日线：stock_zh_index_daily（任意历史，带日级缓存）
        - 30min/15min：stock_zh_a_minute（近5日，~40/80根）
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from signals.data.fetcher import AKShareSource
        from signals.dashboard import get_dashboard

        dash = get_dashboard()

        if dash:
            dash.phase_start("L1.ak_load", total=len(self.ak_codes))

        def _load_one(name, sym):
            """加载单只指数的三个周期，返回 (name, IndexAnalyzer)。"""
            src = AKShareSource()
            if dash:
                dash.task_start("L1.ak_load", f"{name} ({sym})")
            try:
                bars_d = src.get_index_daily(sym,
                                              lookback_days=lookback_days or 180,
                                              start_date=start_date)
                if not bars_d:
                    msg = f"  [✗] {name} ({sym}): 日线数据为空"
                    if dash:
                        dash.log(msg)
                        dash.task_error("L1.ak_load", name, "日线数据为空")
                    else:
                        print(msg, flush=True)
                    return name, IndexAnalyzer(name, sym, [])

                bars_30 = _safe_load(
                    lambda: src.get_index_minute(sym, Freq.F30),
                    label=f"{name} 30min"
                )
                bars_15 = _safe_load(
                    lambda: src.get_index_minute(sym, Freq.F15),
                    label=f"{name} 15min"
                )

                parts = [f"{len(bars_d)}根日线"]
                if bars_30: parts.append(f"{len(bars_30)}根30M")
                if bars_15: parts.append(f"{len(bars_15)}根15M")
                msg = f"  [✓] {name} ({sym}): {'  '.join(parts)}"
                if dash:
                    dash.detail(msg)
                    dash.task_done("L1.ak_load", name, "  ".join(parts))
                else:
                    print(msg, flush=True)

                return name, IndexAnalyzer(
                    name, sym, bars_d,
                    f30_bars=bars_30 or None,
                    f15_bars=bars_15 or None,
                )
            except Exception as e:
                msg = f"  [✗] {name} ({sym}): 加载失败 {e}"
                if dash:
                    dash.log(msg)
                    dash.task_error("L1.ak_load", name, str(e))
                else:
                    print(msg, flush=True)
                return name, IndexAnalyzer(name, sym, [])

        # V8 预热：主线程强制初始化 mini_racer，之后并行安全
        try:
            from py_mini_racer import MiniRacer
            _v8 = MiniRacer(); _v8.eval("1"); del _v8
        except ImportError:
            pass

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_load_one, n, s): n
                       for n, s in self.ak_codes.items()}
            for f in as_completed(futures):
                name, analyzer = f.result()
                self.analyzers[name] = analyzer

        if dash:
            dash.phase_end("L1.ak_load")
        else:
            print(f"  ── A股指数加载完成 ({len(self.ak_codes)}/{len(self.ak_codes)})", flush=True)

    # ────────────────────────────────
    # 私有：Futu 指数加载（三级）
    # ────────────────────────────────

    def _load_futu_indices(self, lookback_days: int = None, start_date: str = None):
        """
        加载 HK 指数三个周期（需要 FutuOpenD 在线）。
        消耗历史K线额度：日线1 + 30min1 + 15min1 = 3/run/指数。
        """
        from signals.dashboard import get_dashboard
        dash = get_dashboard()

        if not self.futu_codes:
            if dash:
                dash.phase_skip("L1.futu", "无港股指数")
            return

        if dash:
            dash.phase_start("L1.futu", total=len(self.futu_codes))

        from signals.data.fetcher import FutuSource
        futu = FutuSource(self.futu_host, self.futu_port)
        try:
            futu.connect()
            self._futu_available = True
        except Exception as e:
            msg = f"  [!] Futu OpenD 连接失败，港股指数跳过：{e}"
            if dash:
                dash.log(msg)
                dash.degradation("Futu", "跳过", str(e))
                dash.phase_end("L1.futu", detail="连接失败")
            else:
                print(msg, flush=True)
            for name, sym in self.futu_codes.items():
                self.analyzers[name] = IndexAnalyzer(name, sym, [])
            return

        lb = lookback_days or 180
        try:
            for name, sym in self.futu_codes.items():
                if dash:
                    dash.task_start("L1.futu", f"{name} ({sym})")
                try:
                    daily  = futu.get_index_kline(sym, Freq.D,
                                                   lookback_days=lb, start=start_date)
                    bars_30 = _safe_load(
                        lambda: futu.get_index_kline(sym, Freq.F30,
                                                      lookback_days=lb, start=start_date),
                        label=f"{name} 30min"
                    )
                    bars_15 = _safe_load(
                        lambda: futu.get_index_kline(sym, Freq.F15,
                                                      lookback_days=lb, start=start_date),
                        label=f"{name} 15min"
                    )

                    if daily:
                        self.analyzers[name] = IndexAnalyzer(
                            name, sym, daily,
                            f30_bars=bars_30 or None,
                            f15_bars=bars_15 or None,
                        )
                        parts = [f"{len(daily)}根日线"]
                        if bars_30: parts.append(f"{len(bars_30)}根30M")
                        if bars_15: parts.append(f"{len(bars_15)}根15M")
                        msg = f"  [✓] {name} ({sym}): {'  '.join(parts)}"
                        if dash:
                            dash.detail(msg)
                            dash.task_done("L1.futu", name)
                        else:
                            print(msg, flush=True)
                    else:
                        self.analyzers[name] = IndexAnalyzer(name, sym, [])
                        msg = f"  [✗] {name} ({sym}): 日线数据为空"
                        if dash:
                            dash.log(msg)
                            dash.task_error("L1.futu", name, "日线数据为空")
                        else:
                            print(msg, flush=True)
                except Exception as e:
                    self.analyzers[name] = IndexAnalyzer(name, sym, [])
                    msg = f"  [✗] {name} ({sym}): 加载失败 {e}"
                    if dash:
                        dash.log(msg)
                        dash.task_error("L1.futu", name, str(e))
                    else:
                        print(msg, flush=True)
        finally:
            futu.close()

        if dash:
            dash.phase_end("L1.futu")

    # ────────────────────────────────
    # 私有：美股指数加载（三级）
    # ────────────────────────────────

    def _load_us_indices(self, lookback_days: int = None, start_date: str = None):
        """
        加载美股指数 ETF（SPY/QQQ/DIA）三个周期。
        Futu 优先（需美股行情权限），降级 yfinance（免费兜底）。
        """
        from signals.dashboard import get_dashboard
        dash = get_dashboard()

        if not self.us_codes:
            if dash:
                dash.phase_skip("L1.us", "无美股指数")
            return

        if dash:
            dash.phase_start("L1.us", total=len(self.us_codes))

        from signals.data.fetcher import FutuSource
        from signals.data.us_factory import create_us_source

        # 尝试复用已验证的 Futu 连接（盘中模式下作为 IB 的备选）
        futu = None
        if self._futu_available:
            try:
                futu = FutuSource(self.futu_host, self.futu_port)
                futu.connect()
            except Exception:
                futu = None

        mode = "review" if start_date else "intraday"
        us_source = create_us_source(mode, futu_source=futu)
        lb = lookback_days or 180

        try:
            for name, sym in self.us_codes.items():
                if dash:
                    dash.task_start("L1.us", f"{name} ({sym})")
                try:
                    daily = us_source.get_us_index_kline(
                        sym, Freq.D, lookback_days=lb, start=start_date)
                    if not daily:
                        self.analyzers[name] = IndexAnalyzer(name, sym, [])
                        msg = f"  [✗] {name} ({sym}): 日线数据为空"
                        if dash:
                            dash.log(msg)
                            dash.task_error("L1.us", name, "日线数据为空")
                        else:
                            print(msg, flush=True)
                        continue

                    bars_30 = _safe_load(
                        lambda s=sym: us_source.get_us_index_kline(
                            s, Freq.F30, lookback_days=lb, start=start_date),
                        label=f"{name} 30min"
                    )
                    bars_15 = _safe_load(
                        lambda s=sym: us_source.get_us_index_kline(
                            s, Freq.F15, lookback_days=lb, start=start_date),
                        label=f"{name} 15min"
                    )

                    self.analyzers[name] = IndexAnalyzer(
                        name, sym, daily,
                        f30_bars=bars_30 or None,
                        f15_bars=bars_15 or None,
                    )
                    parts = [f"{len(daily)}根日线"]
                    if bars_30: parts.append(f"{len(bars_30)}根30M")
                    if bars_15: parts.append(f"{len(bars_15)}根15M")
                    msg = f"  [✓] {name} ({sym}): {'  '.join(parts)}"
                    if dash:
                        dash.detail(msg)
                        dash.task_done("L1.us", name)
                    else:
                        print(msg, flush=True)

                except Exception as e:
                    self.analyzers[name] = IndexAnalyzer(name, sym, [])
                    msg = f"  [✗] {name} ({sym}): 加载失败 {e}"
                    if dash:
                        dash.log(msg)
                        dash.task_error("L1.us", name, str(e))
                    else:
                        print(msg, flush=True)
        finally:
            us_source.close()

        if dash:
            dash.phase_end("L1.us")

    # ────────────────────────────────
    # 分析 + 输出
    # ────────────────────────────────

    def analyze(self) -> MarketContext:
        """生成所有指数报告，聚合为 MarketContext"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()

        if dash:
            dash.phase_start("L1.analyze", total=len(self.analyzers))

        reports: List[IndexReport] = []
        for name, az in self.analyzers.items():
            if dash:
                dash.task_start("L1.analyze", name)
            try:
                reports.append(az.report())
                if dash:
                    dash.task_done("L1.analyze", name)
            except Exception as e:
                reports.append(IndexReport(name=name, symbol=az.symbol,
                                           data_available=False))
                msg = f"  [!] {name} report() 失败：{e}"
                if dash:
                    dash.log(msg)
                    dash.task_error("L1.analyze", name, str(e))
                else:
                    print(msg, flush=True)

        if dash:
            dash.phase_end("L1.analyze")

        return build_market_context(reports)

    def run(self, lookback_days: int = None) -> MarketContext:
        """盘中模式一键运行：initialize → analyze → print_report"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()
        if dash:
            dash.log("\n>>> Layer 1 指数研判（加载中）...")
        else:
            print("\n>>> Layer 1 指数研判（加载中）...", flush=True)
        self.initialize(lookback_days=lookback_days)
        ctx = self.analyze()
        if dash:
            dash.pause()
        ctx.print_report()
        if dash:
            dash.resume()
        return ctx

    def run_review(self, start_date: str) -> MarketContext:
        """盘后复盘一键运行：initialize_with_start → analyze → print_report"""
        from signals.dashboard import get_dashboard
        dash = get_dashboard()
        if dash:
            dash.log(f"\n>>> Layer 1 指数复盘（起始：{start_date}）...")
        else:
            print(f"\n>>> Layer 1 指数复盘（起始：{start_date}）...", flush=True)
        self.initialize_with_start(start_date)
        ctx = self.analyze()
        if dash:
            dash.pause()
        ctx.print_report()
        if dash:
            dash.resume()
        return ctx


# ─────────────────────────────────────────────────────────
# 工具：安全加载，失败时返回空列表
# ─────────────────────────────────────────────────────────

def _safe_load(fn, label: str = ""):
    try:
        result = fn()
        return result or []
    except Exception as e:
        from signals.dashboard import get_dashboard
        dash = get_dashboard()
        err_type = type(e).__name__
        brief = str(e).split('(')[0].strip()[:40]
        msg = f"    [!] {label} 跳过 ({err_type}: {brief})"
        if dash:
            dash.detail(msg)
        else:
            print(msg, flush=True)
        return []
