# -*- coding: utf-8 -*-
"""同步模块注册"""

from .stock_daily import sync_stock_daily
from .index_daily import sync_index_daily
from .stock_minute import sync_stock_minute
from .index_minute import sync_index_minute
from .board_ranking import sync_board_ranking
from .board_cons import sync_board_cons
from .cache_preheat import sync_cache_preheat
from .market_pools import sync_market_pools
from .quote_snapshots import sync_quote_snapshots
from .signal_pool import sync_signal_pool
from .strategy_snapshot import sync_strategy_snapshot

ALL_MODULES = [
    ("cache_preheat", sync_cache_preheat, "00:05 weekday"),
    ("signal_pool",   sync_signal_pool,   "00:10 weekday"),
    ("market_pools",  sync_market_pools,  "09:05 weekday"),
    ("quote_snapshots", sync_quote_snapshots, "09:10 weekday"),
    ("stock_daily",   sync_stock_daily,   "16:30 weekday"),
    ("index_daily",   sync_index_daily,   "16:30 weekday"),
    ("stock_minute",  sync_stock_minute,  "16:00 weekday"),
    ("index_minute",  sync_index_minute,  "16:00 weekday"),
    ("board_ranking", sync_board_ranking, "16:30 weekday"),
    ("strategy_snapshot", sync_strategy_snapshot, "16:40 weekday"),
    ("board_cons",    sync_board_cons,    "Sunday 10:00"),
]
