# -*- coding: utf-8 -*-
"""Shared macro watchlist and A-share index minute-cache universe."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

import config


MINGDAO_INDEX_THEMES: dict[str, list[str]] = {
    "上证指数": ["全市场", "权重", "政策温度"],
    "上证50": ["权重", "大金融", "消费"],
    "沪深300": ["核心资产", "权重", "大金融"],
    "深证成指": ["成长", "先进制造", "消费电子"],
    "创业板指": ["CPO", "电新", "成长链"],
    "科创50": ["芯片", "半导体", "硬科技"],
    "科创综指": ["硬科技", "半导体", "创新成长"],
    "超大盘": ["央国企", "红利", "权重"],
    "中证500": ["中盘成长", "制造业", "弹性成长"],
    "中证1000": ["小盘成长", "主题弹性", "交易活跃"],
    "中证银行": ["大金融", "红利", "顺周期"],
    "国证2000": ["小微盘", "题材弹性", "市场广度"],
    "恒生科技ETF": ["港股科技", "互联网", "风险偏好"],
    "30年国债ETF": ["利率", "避险", "股债跷跷板"],
    "中国石油": ["资源", "央企", "红利"],
}


MINGDAO_MACRO_WATCHLIST: list[dict[str, str]] = [
    {"name": "上证指数", "symbol": "sh000001", "kind": "index"},
    {"name": "深证成指", "symbol": "sz399001", "kind": "index"},
    {"name": "沪深300", "symbol": "sh000300", "kind": "index"},
    {"name": "创业板指", "symbol": "sz399006", "kind": "index"},
    {"name": "科创50", "symbol": "sh000688", "kind": "index"},
    {"name": "科创综指", "symbol": "sh000680", "kind": "index"},
    {"name": "上证50", "symbol": "sh000016", "kind": "index"},
    {"name": "超大盘", "symbol": "sh000043", "kind": "index"},
    {"name": "中证500", "symbol": "sh000905", "kind": "index"},
    {"name": "中证1000", "symbol": "sh000852", "kind": "index"},
    {"name": "中证银行", "symbol": "sz399986", "kind": "index"},
    {"name": "国证2000", "symbol": "sz399303", "kind": "index"},
    {"name": "恒生科技ETF", "symbol": "SH.513130", "kind": "stock"},
    {"name": "30年国债ETF", "symbol": "SH.511090", "kind": "stock"},
    {"name": "中国石油", "symbol": "SH.601857", "kind": "stock"},
]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _a_index_symbol(value: Any) -> str:
    symbol = _text(value).lower().replace(".", "")
    if symbol.startswith(("sh", "sz")) and len(symbol) == 8 and symbol[2:].isdigit():
        return symbol
    return ""


def macro_watchlist() -> list[dict[str, str]]:
    rows = [dict(item) for item in MINGDAO_MACRO_WATCHLIST]
    seen_names = {_text(item.get("name")) for item in rows}
    for name, symbol in getattr(config, "INDEX_AK_CODES", {}).items():
        if name and name not in seen_names:
            rows.append({"name": str(name), "symbol": str(symbol), "kind": "index"})
            seen_names.add(str(name))
    return rows


def macro_index_themes() -> dict[str, list[str]]:
    return {name: list(tags) for name, tags in MINGDAO_INDEX_THEMES.items()}


def macro_a_index_codes() -> dict[str, str]:
    rows: "OrderedDict[str, str]" = OrderedDict()
    for item in macro_watchlist():
        if _text(item.get("kind")) != "index":
            continue
        symbol = _a_index_symbol(item.get("symbol"))
        name = _text(item.get("name"))
        if name and symbol:
            rows[name] = symbol
    return dict(rows)


def macro_a_index_symbols() -> list[str]:
    return list(macro_a_index_codes().values())


def macro_a_index_pure_codes() -> set[str]:
    return {symbol[2:] for symbol in macro_a_index_symbols()}


def supports_a_index_minute_cache(symbol: Any) -> bool:
    return bool(_a_index_symbol(symbol))
