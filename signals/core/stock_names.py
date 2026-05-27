# -*- coding: utf-8 -*-
"""股票名称解析器 — Futu 代码 → 公司名 + 行业"""


_DEFAULT_STOCK_ALIASES = {
    "HK.00522": {
        "name": "ASMPT",
        "aliases": ("ASMPT", "ASMPT Limited", "ASM Pacific Technology"),
    },
    "HK.03750": {
        "name": "宁德时代 H",
        "aliases": (
            "宁德时代H",
            "宁德时代 H",
            "宁德时代港股",
            "寧德時代",
            "CATL H",
            "CATL-H",
        ),
    },
}


class StockNameResolver:
    """
    将 Futu 格式代码（SZ.001400）解析为公司名和行业。

    数据来源（优先级从高到低）：
    1. inject_from_rankings() 注入的 L2 IndustryRanking 数据
    2. _build_name_to_code_map() 的反向映射（name→code6 反转）
    3. _INDUSTRY_LEADERS 静态字典（90+ 行业龙头，作为旧名/兜底别名）
    """

    def __init__(self):
        self._code_to_name: dict = {}
        self._code_to_industry: dict = {}
        self._name_to_code: dict = {}
        self._alias_to_code: dict = {}
        self._fallback_loaded = False

    @staticmethod
    def _alias_key(value: str) -> str:
        return str(value or "").strip().casefold()

    def _remember_alias(self, futu_code: str, alias: str):
        key = self._alias_key(alias)
        if key:
            self._alias_to_code[key] = futu_code

    def _remember_name(self, futu_code: str, name: str, *, override_code_name: bool = False):
        """记录双向映射；旧静态名保留为 name->code 别名。"""
        futu_code = str(futu_code or "").strip()
        name = str(name or "").strip()
        if not futu_code or not name:
            return
        self._name_to_code[name] = futu_code
        self._remember_alias(futu_code, name)
        if override_code_name or futu_code not in self._code_to_name:
            self._code_to_name[futu_code] = name

    def _load_default_aliases(self):
        for futu_code, payload in _DEFAULT_STOCK_ALIASES.items():
            display_name = str(payload.get("name") or "").strip()
            if display_name:
                self._remember_name(futu_code, display_name)
            for alias in payload.get("aliases") or ():
                self._remember_alias(futu_code, alias)

        try:
            from signals.core.macro_universe import macro_industry_etfs_by_code

            for payload in macro_industry_etfs_by_code().values():
                symbol = str(payload.get("symbol") or "").strip()
                name = str(payload.get("name") or "").strip()
                if symbol and name:
                    self._remember_name(symbol, name, override_code_name=True)
        except Exception:
            pass

    def inject_from_rankings(self, merged_list):
        """从 L2 IndustryRanking 注入 code→name 和 code→industry。"""
        for ranking in merged_list:
            for c in ranking.candidates:
                if c.code:
                    self._remember_name(c.code, c.name, override_code_name=True)
                    self._code_to_industry[c.code] = ranking.name

    def inject_from_whitelist(self, whitelist_map: dict):
        """手动注入白名单名称：{"SH.601958": "金钼股份"}"""
        for code, name in whitelist_map.items():
            self._remember_name(code, name, override_code_name=True)

    def _lazy_load_fallback(self):
        """加载静态数据源（首次查询 miss 时触发）"""
        if self._fallback_loaded:
            return
        self._fallback_loaded = True

        # 来源 3: _INDUSTRY_LEADERS。先加载为别名，后续主数据可覆盖展示名。
        try:
            from signals.layers.industry import _INDUSTRY_LEADERS
            for industry, (futu_code, name) in _INDUSTRY_LEADERS.items():
                self._remember_name(futu_code, name)
                if futu_code not in self._code_to_industry:
                    self._code_to_industry[futu_code] = industry
        except Exception:
            pass

        # 来源 2: 反向映射 name→code6 → code6→name。缓存/Mongo 比静态行业龙头更新。
        try:
            from signals.layers.industry import (
                _build_name_to_code_map, _code6_to_futu,
            )
            name_to_code = _build_name_to_code_map()
            for name, code6 in name_to_code.items():
                futu_code = _code6_to_futu(code6)
                if futu_code:
                    self._remember_name(futu_code, name, override_code_name=True)
        except Exception:
            pass

        self._load_default_aliases()

    def get_name(self, futu_code: str) -> str:
        """返回公司名。未知则返回代码后缀（如 SPY）。"""
        if futu_code in self._code_to_name:
            return self._code_to_name[futu_code]
        self._lazy_load_fallback()
        try:
            from signals.core.macro_universe import macro_industry_etf_name

            macro_name = macro_industry_etf_name(futu_code)
            if macro_name:
                return macro_name
        except Exception:
            pass
        return self._code_to_name.get(futu_code, futu_code.split(".")[-1])

    def get_code(self, name: str) -> str:
        """名称→Futu代码。支持模糊匹配（包含即命中）。未找到返回空字符串。"""
        self._lazy_load_fallback()
        name = str(name or "").strip()
        if not name:
            return ""
        alias_key = self._alias_key(name)
        if alias_key in self._alias_to_code:
            return self._alias_to_code[alias_key]
        if name in self._name_to_code:
            return self._name_to_code[name]
        # 精确匹配
        for code, n in self._code_to_name.items():
            if n == name or self._alias_key(n) == alias_key:
                return code
        # 模糊匹配（名称包含查询词）
        candidates = [
            (code, n)
            for n, code in self._name_to_code.items()
            if name in n or (alias_key and alias_key in self._alias_key(n))
        ]
        unique_codes = {code for code, _ in candidates}
        if len(unique_codes) == 1:
            return candidates[0][0]
        return ""

    def search(self, keyword: str) -> list:
        """按关键词搜索，返回 [(code, name), ...] 列表。"""
        self._lazy_load_fallback()
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        alias_key = self._alias_key(keyword)
        results = []
        seen = set()

        def add(code: str):
            if code in seen:
                return
            seen.add(code)
            results.append((code, self.get_name(code)))

        for alias, code in self._alias_to_code.items():
            if alias_key and alias_key in alias:
                add(code)
        for n, code in self._name_to_code.items():
            if keyword in n or (alias_key and alias_key in self._alias_key(n)):
                add(code)
        return results

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
