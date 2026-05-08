# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.cache_preheat import _freq_from_name


def test_cache_preheat_imports_kline_cache_with_canonical_freqs():
    assert _freq_from_name("daily") == "日线"
    assert _freq_from_name("weekly") == "周线"
    assert _freq_from_name("monthly") == "月线"
    assert _freq_from_name("15m") == "15分钟"
    assert _freq_from_name("30m") == "30分钟"
