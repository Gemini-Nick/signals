# -*- coding: utf-8 -*-
"""
ReviewScreener: 盘后复盘模式入口（Layer 1 + Layer 3）

用法（通过 run.py 调用）：
  python run.py --mode review --start 2024-09-24
  python run.py --mode review --start 2024-09-24 --industries 有色金属,半导体

功能：
1. 指数复盘：从 start_date 加载完整历史日线 → 输出结构报告
2. 个股复盘：对指定标的做日线级别缠论分析 → 输出历史结构
3. 行业复盘：对指定行业批量评分 → 输出行业排名
"""
import sys
from typing import List, Optional, Dict

from czsc import Freq, RawBar

from .index_screener import IndexScreener
from .market_context import MarketContext
from .industry import score_industry, IndustryScore


# ─────────────────────────────────────────────────────────
# 个股日线复盘工具
# ─────────────────────────────────────────────────────────

def _load_stock_daily_bars(futu_code: str, start_date: str,
                            end_date: str = None) -> List[RawBar]:
    """
    加载个股日线（AKShare stock_zh_a_hist），供盘后复盘使用。
    仅支持 A股（SH/SZ 前缀）。
    """
    from signals.data.fetcher import AKShareSource
    from datetime import datetime
    ak = AKShareSource()
    edt = end_date or datetime.now().strftime("%Y-%m-%d")
    try:
        return ak.get_a_daily(futu_code, sdt=start_date, edt=edt)
    except Exception as e:
        print(f"  [✗] {futu_code} 日线加载失败：{e}", flush=True)
        return []


# ─────────────────────────────────────────────────────────
# ReviewScreener
# ─────────────────────────────────────────────────────────

class ReviewScreener:
    """
    盘后复盘模式：从指定关键时间节点加载历史K线，输出完整结构报告。

    典型起始日期：
    - "2024-09-24"  九月行情启动
    - "2025-01-06"  DeepSeek 行情
    - "2024-01-01"  年初
    """

    def __init__(self,
                 start_date: str,
                 ak_codes: Optional[Dict[str, str]] = None,
                 futu_codes: Optional[Dict[str, str]] = None,
                 whitelist: Optional[List[str]] = None,
                 industries: Optional[List[str]] = None,
                 futu_host: str = "127.0.0.1",
                 futu_port: int = 11111):
        """
        :param start_date:  复盘起始日期，格式 'YYYY-MM-DD'
        :param ak_codes:    A股指数代码映射（None=使用 config 默认）
        :param futu_codes:  HK指数代码映射（None=使用 config 默认）
        :param whitelist:   个股复盘列表（None=使用 config.WHITELIST）
        :param industries:  行业复盘列表（None=跳过行业复盘）
        :param futu_host:   FutuOpenD 地址
        :param futu_port:   FutuOpenD 端口
        """
        self.start_date = start_date
        self.industries = industries or []
        self.futu_host  = futu_host
        self.futu_port  = futu_port

        import config
        self.whitelist = whitelist or config.WHITELIST
        self._index_screener = IndexScreener(
            ak_codes=ak_codes,
            futu_codes=futu_codes,
            futu_host=futu_host,
            futu_port=futu_port,
        )
        self._market_ctx: Optional[MarketContext] = None

    # ────────────────────────────────
    # Layer 1：指数复盘
    # ────────────────────────────────

    def run_index_review(self) -> MarketContext:
        """
        从 start_date 加载指数历史，生成完整结构报告。
        结果缓存在 self._market_ctx 供后续步骤使用。
        """
        ctx = self._index_screener.run_review(self.start_date)
        self._market_ctx = ctx
        return ctx

    # ────────────────────────────────
    # Layer 2：行业复盘
    # ────────────────────────────────

    def run_industry_review(self,
                             industries: Optional[List[str]] = None,
                             sample_size: int = 20) -> List[IndustryScore]:
        """
        对指定行业做强度研判（自动降级：东财CZSC → 成分股聚合）。
        按平均分从高到低排序后输出。
        """
        target = industries or self.industries
        if not target:
            print("  跳过行业复盘（未指定行业）", flush=True)
            return []

        print(f"\n>>> Layer 2 行业复盘：{', '.join(target)}", flush=True)
        results: List[IndustryScore] = []
        for ind in target:
            print(f"  分析行业：{ind} ...", flush=True)
            score = score_industry(ind, start_date=self.start_date,
                                   sample_size=sample_size)
            results.append(score)
            print(f"    {score.summary}", flush=True)

        results.sort(key=lambda x: -x.avg_score)
        self._print_industry_report(results)
        return results

    def _print_industry_report(self, scores: List[IndustryScore]):
        print("\n" + "─" * 44)
        print("  行业复盘排名")
        print("─" * 44)
        for i, s in enumerate(scores, 1):
            flag = "✓" if s.is_strong else " "
            print(f"  {i:2d}. [{flag}] {s.summary}")
        print("─" * 44 + "\n")

    # ────────────────────────────────
    # Layer 3：个股日线复盘
    # ────────────────────────────────

    def run_stock_review(self, symbols: Optional[List[str]] = None) -> list:
        """
        对指定个股做日线级别缠论分析。
        返回 List[ScoredSymbol]（日线级别评分）。
        """
        from signals.core.analyzer import SymbolAnalyzer
        from signals.core.detectors import detect_all_signals
        from signals.core.scorer import score_signals, ScoredSymbol

        target = symbols or self.whitelist
        if not target:
            print("  跳过个股复盘（未指定标的）", flush=True)
            return []

        print(f"\n>>> Layer 3 个股复盘（日线）：{len(target)} 只 ...", flush=True)
        scored: list = []
        for sym in target:
            bars = _load_stock_daily_bars(sym, self.start_date)
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
        self._print_stock_report(scored)
        return scored

    def _print_stock_report(self, scored: list):
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

    # ────────────────────────────────
    # 一键全量复盘
    # ────────────────────────────────

    def run_all(self,
                industries: Optional[List[str]] = None,
                symbols: Optional[List[str]] = None) -> dict:
        """
        一键运行全量复盘：指数 → 行业 → 个股。
        :return: {"market_ctx": MarketContext, "industry_scores": [...], "stock_scores": [...]}
        """
        print(f"\n{'═'*52}")
        print(f"  盘后复盘模式  起始日期：{self.start_date}")
        print(f"{'═'*52}")

        ctx = self.run_index_review()
        ind_scores = self.run_industry_review(industries=industries)
        stock_scores = self.run_stock_review(symbols=symbols)

        return {
            "market_ctx": ctx,
            "industry_scores": ind_scores,
            "stock_scores": stock_scores,
        }
