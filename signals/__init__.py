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

# Re-export：保持 `from signals import Xxx` 的兼容性
from signals.core.freq_utils import config_freq_to_czsc, FREQ_MAP
from signals.core.analyzer import SymbolAnalyzer
from signals.core.detectors import detect_all_signals, SignalEvent
from signals.core.scorer import score_signals, ScoredSymbol

from signals.layers.index_report import ZSLevel, IndexReport
from signals.layers.index_analyzer import IndexAnalyzer
from signals.layers.market_context import (MarketContext, build_market_context, infer_strong_sectors,
                                            SentimentPhase, calc_divergence, detect_sentiment_phase)
from signals.layers.index_screener import IndexScreener
from signals.layers.industry import get_industry_list, get_industry_stocks, IndustryScore, score_industry
from signals.layers.industry import get_industry_representatives, ConceptRanking
from signals.layers.screener import IntraDayScreener
from signals.layers.review_screener import review_stock_daily
