# -*- coding: utf-8 -*-
"""同步模块注册"""

from .stock_daily import sync_stock_daily
from .hk_stock_daily import sync_hk_stock_daily
from .stock_30m_fullmarket import sync_stock_30m_fullmarket
from .index_daily import sync_index_daily
from .stock_minute import sync_stock_minute
from .index_minute import sync_index_minute
from .board_ranking import sync_board_ranking
from .board_heat_minute import sync_board_heat_minute, sync_concept_heat_minute
from .chain_heat import sync_chain_heat_snapshots
from .board_cons import sync_board_cons
from .cache_preheat import sync_cache_preheat
from .market_pools import sync_market_pools
from .market_limit_pools import sync_market_limit_pools
from .fullmarket_spot_snapshot import sync_fullmarket_spot_snapshot
from .etf_spot_snapshot import sync_etf_spot_snapshot
from .quote_snapshots import sync_eastmoney_ulist_quote, sync_quote_snapshots
from .signal_pool import sync_signal_pool
from .strategy_snapshot import sync_strategy_snapshot
from .minute_readiness import sync_minute_readiness_probe
from .weekly_rollup import sync_weekly_rollup
from .terminal_pool import sync_terminal_realtime_pool
from .hot_rank_clues import sync_hot_rank_clues
from .ma_climb_scan import sync_ma_climb_scan
from .technical_signal_scan import sync_intraday_technical_signal_scan, sync_technical_signal_scan
from .knowledge_market_views import sync_knowledge_market_views
from .concept_relationship_graph import sync_concept_relationship_graph
from .calendar_validate import sync_calendar_validate
from .postmarket_chain_rebuild import sync_postmarket_chain_rebuild
from .security_business_facts import sync_security_business_facts
from .global_market_foundation import sync_global_market_foundation
from .sector_transition import sync_sector_transition_rollup, sync_sector_transition_scan

ALL_MODULES = [
    ("calendar_validate", sync_calendar_validate, "08:30 weekday"),
    ("fullmarket_spot_snapshot", sync_fullmarket_spot_snapshot, "15:35-23:50 weekday"),
    ("etf_spot_snapshot", sync_etf_spot_snapshot, "15:35-23:50 weekday"),
    ("market_pools",  sync_market_pools,  "09:05 weekday"),
    ("market_limit_pools", sync_market_limit_pools, "09:25-15:05 weekday"),
    ("eastmoney_ulist_quote", sync_eastmoney_ulist_quote, "09:10 weekday"),
    ("quote_snapshots", sync_quote_snapshots, "09:10 weekday"),
    ("stock_minute",  sync_stock_minute,  "15:05 weekday"),
    ("index_minute",  sync_index_minute,  "15:05 weekday"),
    ("board_heat_minute", sync_board_heat_minute, "15:05 weekday"),
    ("concept_heat_minute", sync_concept_heat_minute, "15:05 weekday"),
    ("chain_heat_snapshots", sync_chain_heat_snapshots, "15:06 weekday"),
    ("sector_transition_scan", sync_sector_transition_scan, "live only"),
    ("minute_readiness_probe", sync_minute_readiness_probe, "15:10 weekday"),
    ("intraday_technical_signal_scan", sync_intraday_technical_signal_scan, "live only"),
    ("stock_daily",   sync_stock_daily,   "16:00-17:30 weekday"),
    ("hk_stock_daily", sync_hk_stock_daily, "16:15-19:30 weekday"),
    ("stock_30m_fullmarket", sync_stock_30m_fullmarket, "17:00-20:30 weekday"),
    ("index_daily",   sync_index_daily,   "16:00-17:30 weekday"),
    ("global_market_foundation", sync_global_market_foundation, "after HK/US daily close"),
    ("weekly_rollup", sync_weekly_rollup, "17:30-18:00 weekday"),
    ("board_ranking", sync_board_ranking, "18:00-21:00 weekday"),
    ("board_cons",    sync_board_cons,    "18:00-21:00 weekday"),
    ("security_business_facts", sync_security_business_facts, "20:00-22:00 weekday"),
    ("postmarket_chain_rebuild", sync_postmarket_chain_rebuild, "20:15-22:15 weekday"),
    ("ma_climb_scan", sync_ma_climb_scan, "20:25-22:25 weekday"),
    ("technical_signal_scan", sync_technical_signal_scan, "20:30-22:30 weekday"),
    ("sector_transition_rollup", sync_sector_transition_rollup, "after technical scans"),
    ("knowledge_market_views", sync_knowledge_market_views, "20:30-22:30 weekday"),
    ("concept_relationship_graph", sync_concept_relationship_graph, "20:45-22:45 weekday"),
    ("signal_pool",   sync_signal_pool,   "21:00 weekday"),
    ("strategy_snapshot", sync_strategy_snapshot, "21:10 weekday"),
    ("hot_rank_clues", sync_hot_rank_clues, "postmarket only"),
    ("terminal_realtime_pool", sync_terminal_realtime_pool, "postmarket only"),
    ("cache_preheat", sync_cache_preheat, "21:20 weekday"),
]
