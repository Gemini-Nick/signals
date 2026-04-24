# -*- coding: utf-8 -*-
"""
Signals — 缠论三层联动分析系统

子包结构：
  core/     缠论引擎（分析器、检测器、评分）
  data/     数据源（Tushare / AKShare / Futu）
  layers/   三层联动（指数 → 行业 → 标的）
  research/ 研究笔记（多格式导入、双维度集成）
  notify/   通知（飞书推送）
"""

# Lazy imports：避免顶层加载 czsc 等重依赖，
# 让 signals.web / signals.deploy 等轻量子模块可以独立使用。
# 保持 `from signals import Xxx` 的兼容性。

_LAZY_IMPORTS = {
    # core
    "config_freq_to_czsc": "signals.core.freq_utils",
    "FREQ_MAP":            "signals.core.freq_utils",
    "SymbolAnalyzer":      "signals.core.analyzer",
    "detect_all_signals":  "signals.core.detectors",
    "SignalEvent":         "signals.core.detectors",
    "score_signals":       "signals.core.scorer",
    "ScoredSymbol":        "signals.core.scorer",
    # layers
    "ZSLevel":             "signals.layers.index_report",
    "IndexReport":         "signals.layers.index_report",
    "IndexAnalyzer":       "signals.layers.index_analyzer",
    "MarketContext":       "signals.layers.market_context",
    "build_market_context":"signals.layers.market_context",
    "infer_strong_sectors":"signals.layers.market_context",
    "SentimentPhase":      "signals.layers.market_context",
    "calc_divergence":     "signals.layers.market_context",
    "detect_sentiment_phase":"signals.layers.market_context",
    "IndexScreener":       "signals.layers.index_screener",
    "get_industry_list":   "signals.layers.industry",
    "get_industry_stocks": "signals.layers.industry",
    "IndustryScore":       "signals.layers.industry",
    "score_industry":      "signals.layers.industry",
    "get_industry_representatives": "signals.layers.industry",
    "ConceptRanking":      "signals.layers.industry",
    "IntraDayScreener":    "signals.layers.screener",
    "review_stock_daily":  "signals.layers.review_screener",
    # domain pack
    "SignalsPack":         "signals.domain_pack",
}


def __getattr__(name):
    if name in _LAZY_IMPORTS:
        import importlib
        module = importlib.import_module(_LAZY_IMPORTS[name])
        return getattr(module, name)
    raise AttributeError(f"module 'signals' has no attribute {name!r}")
