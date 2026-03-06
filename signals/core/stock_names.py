# -*- coding: utf-8 -*-
"""股票名称解析器 — Futu 代码 → 公司名 + 行业"""


class StockNameResolver:
    """
    将 Futu 格式代码（SZ.001400）解析为公司名和行业。

    数据来源（优先级从高到低）：
    1. inject_from_rankings() 注入的 L2 IndustryRanking 数据
    2. _INDUSTRY_LEADERS 静态字典（90+ 行业龙头）
    3. _build_name_to_code_map() 的反向映射（name→code6 反转）
    """

    def __init__(self):
        self._code_to_name: dict = {}
        self._code_to_industry: dict = {}
        self._fallback_loaded = False

    def inject_from_rankings(self, merged_list):
        """从 L2 IndustryRanking 注入 code→name 和 code→industry。"""
        for ranking in merged_list:
            for c in ranking.candidates:
                if c.code:
                    self._code_to_name[c.code] = c.name
                    self._code_to_industry[c.code] = ranking.name

    def inject_from_whitelist(self, whitelist_map: dict):
        """手动注入白名单名称：{"SH.601958": "金钼股份"}"""
        self._code_to_name.update(whitelist_map)

    def _lazy_load_fallback(self):
        """加载静态数据源（首次查询 miss 时触发）"""
        if self._fallback_loaded:
            return
        self._fallback_loaded = True

        # 来源 2: _INDUSTRY_LEADERS
        try:
            from signals.layers.industry import _INDUSTRY_LEADERS
            for industry, (futu_code, name) in _INDUSTRY_LEADERS.items():
                if futu_code not in self._code_to_name:
                    self._code_to_name[futu_code] = name
                if futu_code not in self._code_to_industry:
                    self._code_to_industry[futu_code] = industry
        except Exception:
            pass

        # 来源 3: 反向映射 name→code6 → code6→name
        try:
            from signals.layers.industry import (
                _build_name_to_code_map, _code6_to_futu,
            )
            name_to_code = _build_name_to_code_map()
            for name, code6 in name_to_code.items():
                futu_code = _code6_to_futu(code6)
                if futu_code and futu_code not in self._code_to_name:
                    self._code_to_name[futu_code] = name
        except Exception:
            pass

    def get_name(self, futu_code: str) -> str:
        """返回公司名。未知则返回代码后缀（如 SPY）。"""
        if futu_code in self._code_to_name:
            return self._code_to_name[futu_code]
        self._lazy_load_fallback()
        return self._code_to_name.get(futu_code, futu_code.split(".")[-1])

    def get_industry(self, futu_code: str) -> str:
        """返回行业名。未知则返回空字符串。"""
        if futu_code in self._code_to_industry:
            return self._code_to_industry[futu_code]
        self._lazy_load_fallback()
        return self._code_to_industry.get(futu_code, "")


_resolver: StockNameResolver | None = None


def get_resolver() -> StockNameResolver:
    global _resolver
    if _resolver is None:
        _resolver = StockNameResolver()
    return _resolver
