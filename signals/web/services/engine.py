# -*- coding: utf-8 -*-
"""
Engine Bridge — 桥接现有分析引擎和 Web API

包装 IndexScreener、MarketContext 等，缓存分析结果供 API 读取。
不包含任何分析逻辑，只做包装和缓存。
"""
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import threading
import time


@dataclass
class EngineState:
    """Web 引擎缓存状态"""
    # L1 分析结果
    market_context: Optional[object] = None     # MarketContext
    index_reports: List[object] = field(default_factory=list)  # IndexReport[]

    # L3 信号结果
    scored_symbols: List[object] = field(default_factory=list)  # ScoredSymbol[]

    # 底层分析器引用 (用于图表数据)
    index_screener: Optional[object] = None     # IndexScreener
    analyzers: Dict[str, object] = field(default_factory=dict)  # name -> IndexAnalyzer

    # 状态
    last_update: float = 0.0
    is_running: bool = False
    error: str = ""


class WebEngine:
    """
    Web UI 的分析引擎桥接层。

    用法:
        engine = WebEngine()
        engine.run_l1()  # 运行 L1 指数分析
        ctx = engine.get_market_context()
        reports = engine.get_index_reports()
        analyzer = engine.get_index_analyzer("沪深300", "daily")
    """

    def __init__(self):
        self._state = EngineState()
        self._lock = threading.Lock()

    @property
    def state(self) -> EngineState:
        return self._state

    def run_l1(self):
        """运行 Layer 1 指数分析（同步，阻塞）"""
        with self._lock:
            if self._state.is_running:
                return
            self._state.is_running = True
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

        except Exception as e:
            with self._lock:
                self._state.error = str(e)
            raise
        finally:
            with self._lock:
                self._state.is_running = False

    def get_market_context(self):
        """获取缓存的 MarketContext"""
        return self._state.market_context

    def get_index_reports(self) -> list:
        """获取缓存的 IndexReport 列表"""
        return self._state.index_reports or []

    def get_index_analyzer(self, name: str):
        """
        按指数名称获取 IndexAnalyzer。
        返回 IndexAnalyzer 或 None。
        """
        return self._state.analyzers.get(name)

    def get_symbol_analyzer(self, name: str, freq: str):
        """
        获取特定指数+特定周期的 SymbolAnalyzer。

        :param name: 指数名称，如 "沪深300"
        :param freq: "daily" / "30min" / "15min"
        :return: SymbolAnalyzer 或 None
        """
        idx_az = self._state.analyzers.get(name)
        if idx_az is None:
            return None

        freq_map = {
            "daily": "_daily",
            "30min": "_f30",
            "15min": "_f15",
        }
        attr = freq_map.get(freq)
        if attr is None:
            return None

        return getattr(idx_az, attr, None)

    def find_index_name(self, symbol: str) -> Optional[str]:
        """根据 symbol 代码查找指数名称"""
        for report in self.get_index_reports():
            if report.symbol == symbol:
                return report.name
        # 也尝试从 analyzers 中查找
        for name, az in self._state.analyzers.items():
            if az.symbol == symbol:
                return name
        return None

    def get_scored_symbols(self) -> list:
        """获取缓存的 ScoredSymbol 列表"""
        return self._state.scored_symbols or []

    def is_ready(self) -> bool:
        """是否已完成至少一次分析"""
        return self._state.last_update > 0

    def get_status(self) -> dict:
        """返回引擎状态摘要"""
        return {
            "ready": self.is_ready(),
            "running": self._state.is_running,
            "last_update": self._state.last_update,
            "error": self._state.error,
            "index_count": len(self._state.analyzers),
            "signal_count": len(self._state.scored_symbols),
        }


# 全局单例
_engine: Optional[WebEngine] = None


def get_engine() -> WebEngine:
    """获取全局 WebEngine 单例"""
    global _engine
    if _engine is None:
        _engine = WebEngine()
    return _engine
