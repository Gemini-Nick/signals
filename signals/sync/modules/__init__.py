# -*- coding: utf-8 -*-
"""同步模块注册"""

from .stock_daily import sync_stock_daily
from .index_daily import sync_index_daily
from .stock_minute import sync_stock_minute
from .index_minute import sync_index_minute
from .board_ranking import sync_board_ranking
from .board_cons import sync_board_cons

ALL_MODULES = [
    ("stock_daily",   sync_stock_daily,   "16:30 weekday"),
    ("index_daily",   sync_index_daily,   "16:30 weekday"),
    ("stock_minute",  sync_stock_minute,  "16:00 weekday"),
    ("index_minute",  sync_index_minute,  "16:00 weekday"),
    ("board_ranking", sync_board_ranking, "16:30 weekday"),
    ("board_cons",    sync_board_cons,    "Sunday 10:00"),
]
