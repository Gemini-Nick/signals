# -*- coding: utf-8 -*-
"""同步模块注册"""

from .stock_daily import sync_stock_daily
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
from .fullmarket_spot_snapshot import sync_fullmarket_spot_snapshot
from .quote_snapshots import sync_quote_snapshots
from .signal_pool import sync_signal_pool
from .strategy_snapshot import sync_strategy_snapshot
from .minute_readiness import sync_minute_readiness_probe
from .weekly_rollup import sync_weekly_rollup
from .terminal_pool import sync_terminal_realtime_pool
from .technical_signal_scan import sync_technical_signal_scan
from .knowledge_market_views import sync_knowledge_market_views

ALL_MODULES = [
    ("fullmarket_spot_snapshot", sync_fullmarket_spot_snapshot, "15:35-23:50 weekday"),
    ("market_pools",  sync_market_pools,  "09:05 weekday"),
    ("quote_snapshots", sync_quote_snapshots, "09:10 weekday"),
    ("stock_minute",  sync_stock_minute,  "15:05 weekday"),
    ("index_minute",  sync_index_minute,  "15:05 weekday"),
    ("board_heat_minute", sync_board_heat_minute, "15:05 weekday"),
    ("concept_heat_minute", sync_concept_heat_minute, "15:05 weekday"),
    ("chain_heat_snapshots", sync_chain_heat_snapshots, "15:06 weekday"),
    ("minute_readiness_probe", sync_minute_readiness_probe, "15:10 weekday"),
    ("stock_daily",   sync_stock_daily,   "16:00-17:30 weekday"),
    ("stock_30m_fullmarket", sync_stock_30m_fullmarket, "17:00-20:30 weekday"),
    ("index_daily",   sync_index_daily,   "16:00-17:30 weekday"),
    ("weekly_rollup", sync_weekly_rollup, "17:30-18:00 weekday"),
    ("board_ranking", sync_board_ranking, "18:00-21:00 weekday"),
    ("board_cons",    sync_board_cons,    "18:00-21:00 weekday"),
    ("technical_signal_scan", sync_technical_signal_scan, "20:30-22:30 weekday"),
    ("knowledge_market_views", sync_knowledge_market_views, "20:30-22:30 weekday"),
    ("signal_pool",   sync_signal_pool,   "21:00 weekday"),
    ("strategy_snapshot", sync_strategy_snapshot, "21:10 weekday"),
    ("terminal_realtime_pool", sync_terminal_realtime_pool, "21:15 weekday"),
    ("cache_preheat", sync_cache_preheat, "21:20 weekday"),
]
