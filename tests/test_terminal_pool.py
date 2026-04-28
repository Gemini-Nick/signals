# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.terminal_pool import _add_stock


def test_terminal_pool_does_not_add_index_code_as_stock():
    stocks: list[str] = []

    _add_stock(stocks, "000300", index_codes={"000300"})
    _add_stock(stocks, "688802", index_codes={"000300"})

    assert stocks == ["688802"]
