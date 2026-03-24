# -*- coding: utf-8 -*-
"""
Market Cache — 轻量级市场数据缓存

从 web1 的 WebEngine(1145行) 提取核心缓存逻辑。
仅缓存 L1/L2 分析结果供 API 读取，不包含 L3 标的筛选。
"""
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)


@dataclass
class ReviewState:
    """复盘模式缓存"""
    start_date: str = ""
    start_label: str = ""
    market_context: Optional[object] = None
    index_reports: List[object] = field(default_factory=list)
    analyzers: Dict[str, object] = field(default_factory=dict)
    gain_list: List[object] = field(default_factory=list)
    composite_list: List[object] = field(default_factory=list)
    merged_list: List[object] = field(default_factory=list)
    concepts: List[object] = field(default_factory=list)
    oversold_list: List[object] = field(default_factory=list)
    scored_symbols: List[object] = field(default_factory=list)
    rotation_stage: str = ""
    rotation_detail: str = ""
    allocation_suggestion: str = ""
    # Status
    is_running: bool = False
    phase: str = ""
    error: str = ""
    completed: bool = False
    timing: Dict[str, float] = field(default_factory=dict)
    phase_detail: str = ""
    replay_timelines: Dict[str, list] = field(default_factory=dict)


@dataclass
class CacheState:
    """市场数据缓存"""
    market_context: Optional[object] = None
    index_reports: List[object] = field(default_factory=list)
    index_screener: Optional[object] = None
    analyzers: Dict[str, object] = field(default_factory=dict)
    # L2
    gain_list: List[object] = field(default_factory=list)
    composite_list: List[object] = field(default_factory=list)
    merged_list: List[object] = field(default_factory=list)
    concepts: List[object] = field(default_factory=list)
    oversold_list: List[object] = field(default_factory=list)
    l2_stats: Dict = field(default_factory=dict)
    # Status
    last_update: float = 0.0
    is_running: bool = False
    error: str = ""
    loading_phase: str = ""


class MarketCache:
    """
    轻量级市场缓存，供 Chart/Review/Dashboard API 使用。

    用法:
        cache = MarketCache()
        await cache.refresh_l1()  # 后台刷新 L1
        ctx = cache.get_market_context()
    """

    def __init__(self):
        self._state = CacheState()
        self._review = ReviewState()
        self._lock = threading.Lock()

    @property
    def state(self) -> CacheState:
        return self._state

    @property
    def review(self) -> ReviewState:
        return self._review

    # ── L1 指数分析 ──────────────────────────────────

    def run_l1(self):
        """同步运行 L1 指数分析"""
        with self._lock:
            if self._state.is_running:
                return
            self._state.is_running = True
            self._state.loading_phase = "L1"
            self._state.error = ""

        try:
            from signals.layers.index_screener import IndexScreener
            screener = IndexScreener()
            screener.initialize()
            ctx = screener.analyze()

            with self._lock:
                self._state.index_screener = screener
                self._state.market_context = ctx
                self._state.index_reports = ctx.reports
                self._state.analyzers = screener.analyzers
                self._state.last_update = time.time()
                self._state.loading_phase = ""
        except Exception as e:
            logger.error("L1 分析失败: %s", e)
            with self._lock:
                self._state.error = str(e)
        finally:
            with self._lock:
                self._state.is_running = False

    async def refresh_l1(self):
        """异步运行 L1"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.run_l1)

    # ── L2 行业分析 ──────────────────────────────────

    def run_l2(self):
        """同步运行 L2 行业分析"""
        with self._lock:
            self._state.loading_phase = "L2"

        try:
            from signals.layers.industry import get_industry_representatives
            gain, composite, merged, concepts, oversold, stats = \
                get_industry_representatives()

            with self._lock:
                self._state.gain_list = gain
                self._state.composite_list = composite
                self._state.merged_list = merged
                self._state.concepts = concepts
                self._state.oversold_list = oversold
                self._state.l2_stats = stats or {}
                self._state.loading_phase = ""
        except Exception as e:
            logger.warning("L2 行业分析失败: %s", e)

    async def refresh_l2(self):
        """异步运行 L2"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.run_l2)

    # ── 复盘模式 ─────────────────────────────────────

    def run_review(self, start_date: str, start_label: str = ""):
        """同步运行完整复盘 (L1 → L2 → L3)"""
        with self._lock:
            if self._review.is_running:
                return
            self._review = ReviewState(
                start_date=start_date,
                start_label=start_label,
                is_running=True,
                phase="L1",
            )

        try:
            t0 = time.time()

            # L1
            self._review.phase_detail = "加载指数分析..."
            from signals.layers.index_screener import IndexScreener
            screener = IndexScreener()
            screener.initialize(start_date=start_date)
            ctx = screener.analyze()
            self._review.market_context = ctx
            self._review.index_reports = ctx.reports
            self._review.analyzers = screener.analyzers
            self._review.timing["L1"] = time.time() - t0

            # L2
            self._review.phase = "L2"
            self._review.phase_detail = "加载行业分析..."
            t1 = time.time()
            from signals.layers.industry import get_industry_representatives
            gain, composite, merged, concepts, oversold, _ = \
                get_industry_representatives()
            self._review.gain_list = gain
            self._review.composite_list = composite
            self._review.merged_list = merged
            self._review.concepts = concepts
            self._review.oversold_list = oversold
            self._review.timing["L2"] = time.time() - t1

            # 轮动研判
            if gain and composite:
                from signals.core.rotation import detect_rotation_stage
                stage = detect_rotation_stage(gain, composite)
                self._review.rotation_stage = stage.stage
                self._review.rotation_detail = stage.detail
                self._review.allocation_suggestion = stage.allocation

            # L3
            self._review.phase = "L3"
            self._review.phase_detail = "加载标的筛选..."
            t2 = time.time()
            from signals.layers.review_screener import review_stock_daily
            symbols_to_review = _extract_symbols(merged, concepts, oversold)
            scored = review_stock_daily(
                symbols=symbols_to_review,
                start_date=start_date,
            )
            self._review.scored_symbols = scored
            self._review.timing["L3"] = time.time() - t2

            self._review.completed = True
            self._review.phase = ""
            self._review.phase_detail = ""
            logger.info("复盘完成: L1=%.1fs L2=%.1fs L3=%.1fs",
                        self._review.timing.get("L1", 0),
                        self._review.timing.get("L2", 0),
                        self._review.timing.get("L3", 0))

        except Exception as e:
            logger.error("复盘失败: %s", e)
            self._review.error = str(e)
        finally:
            self._review.is_running = False

    async def refresh_review(self, start_date: str, start_label: str = ""):
        """异步运行复盘"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.run_review, start_date, start_label)

    # ── 查询接口 ─────────────────────────────────────

    def get_market_context(self):
        return self._state.market_context

    def get_index_reports(self):
        return self._state.index_reports

    def get_index_analyzer(self, name: str, freq: str = "daily"):
        """获取指数分析器"""
        key = f"{name}_{freq}" if freq != "daily" else name
        return self._state.analyzers.get(key)

    def get_review_status(self) -> dict:
        """返回复盘进度"""
        r = self._review
        return {
            "is_running": r.is_running,
            "phase": r.phase,
            "phase_detail": r.phase_detail,
            "completed": r.completed,
            "error": r.error,
            "timing": r.timing,
            "start_date": r.start_date,
            "start_label": r.start_label,
        }


def _extract_symbols(merged, concepts, oversold, max_total=30) -> list:
    """从 L2 结果中提取待复盘的股票代码"""
    symbols = []
    seen = set()

    # 综合榜前 5 行业的成分股
    for ind in (merged or [])[:5]:
        for member in getattr(ind, "members", [])[:3]:
            code = getattr(member, "code", None) or getattr(member, "symbol", None)
            if code and code not in seen:
                seen.add(code)
                symbols.append(code)

    # 概念榜前 3 概念的龙头
    for con in (concepts or [])[:3]:
        for member in getattr(con, "members", [])[:2]:
            code = getattr(member, "code", None) or getattr(member, "symbol", None)
            if code and code not in seen:
                seen.add(code)
                symbols.append(code)

    # 超跌榜前 3
    for ind in (oversold or [])[:3]:
        for member in getattr(ind, "members", [])[:2]:
            code = getattr(member, "code", None) or getattr(member, "symbol", None)
            if code and code not in seen:
                seen.add(code)
                symbols.append(code)

    return symbols[:max_total]


# ── 全局单例 ─────────────────────────────────────────
_cache: Optional[MarketCache] = None


def get_cache() -> MarketCache:
    """获取全局 MarketCache 实例"""
    global _cache
    if _cache is None:
        _cache = MarketCache()
    return _cache
