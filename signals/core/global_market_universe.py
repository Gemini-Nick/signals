# -*- coding: utf-8 -*-
"""Versioned HK/US replay universe and cross-listing identities."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from signals.core.cross_market_chains import load_cross_market_chains

CONFIG_PATH = Path(__file__).with_name("global_market_universe.yaml")
SUPPORTED_MARKETS = ("A", "HK", "US")


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_market(value: Any) -> str:
    market = _text(value).upper()
    aliases = {"CN": "A", "SH": "A", "SZ": "A", "H": "HK", "NYSE": "US", "NASDAQ": "US"}
    market = aliases.get(market, market)
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"unsupported market: {value}")
    return market


def normalize_markets(values: Iterable[Any] | None) -> list[str]:
    requested = list(values or ["A"])
    output: list[str] = []
    for value in requested:
        market = normalize_market(value)
        if market not in output:
            output.append(market)
    return output or ["A"]


def _normalize_security(item: dict[str, Any], *, market: str, role: str) -> dict[str, Any]:
    symbol = _text(item.get("symbol")).upper()
    return {
        "symbol": symbol,
        "raw_code": symbol.split(".", 1)[-1],
        "name": _text(item.get("name")) or symbol,
        "market": market,
        "exchange": _text(item.get("exchange")) or ("HKEX" if market == "HK" else "NASDAQ"),
        "role": _text(item.get("role")) or role,
        "group": _text(item.get("group")),
        "instrument_kind": _text(item.get("instrument_kind")) or ("index" if role == "index" else "stock"),
        "proxy_for": _text(item.get("proxy_for")),
        "priority": int(item.get("priority") or 0),
    }


def _ai_chain_securities() -> list[dict[str, Any]]:
    securities: list[dict[str, Any]] = []
    chain = load_cross_market_chains().get("us_ai_hardware") or {}
    for node in chain.get("nodes") or []:
        for rep in node.get("us_representatives") or []:
            ticker = _text(rep.get("symbol")).upper()
            if not ticker:
                continue
            securities.append(
                {
                    "symbol": f"US.{ticker}",
                    "raw_code": ticker,
                    "name": _text(rep.get("name")) or ticker,
                    "market": "US",
                    "exchange": "NASDAQ",
                    "role": "ai_chain",
                    "group": _text(node.get("node_id")),
                    "instrument_kind": "stock",
                    "proxy_for": "",
                    "priority": int(rep.get("priority") or 0),
                }
            )
    return securities


@lru_cache(maxsize=4)
def load_global_market_universe(config_path: str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else CONFIG_PATH
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    markets: dict[str, dict[str, Any]] = {}
    for raw_market, config in (raw.get("markets") or {}).items():
        market = normalize_market(raw_market)
        indices = [_normalize_security(item, market=market, role="index") for item in config.get("indices") or []]
        anchors = [_normalize_security(item, market=market, role="anchor") for item in config.get("anchors") or []]
        markets[market] = {
            "market": market,
            "timezone": _text(config.get("timezone")),
            "currency": _text(config.get("currency")),
            "coverage_scope": _text(config.get("coverage_scope")),
            "indices": indices,
            "anchors": anchors,
        }

    us = markets.setdefault(
        "US",
        {"market": "US", "timezone": "America/New_York", "currency": "USD", "coverage_scope": "core_universe", "indices": [], "anchors": []},
    )
    merged: dict[str, dict[str, Any]] = {}
    for item in [*us["anchors"], *_ai_chain_securities()]:
        previous = merged.get(item["symbol"])
        if previous:
            groups = [group for group in (previous.get("group"), item.get("group")) if group]
            previous["group"] = ",".join(dict.fromkeys(",".join(groups).split(",")))
            previous["priority"] = max(int(previous.get("priority") or 0), int(item.get("priority") or 0))
            if previous.get("role") != "anchor":
                previous["role"] = item.get("role")
        else:
            merged[item["symbol"]] = dict(item)
    us["anchors"] = list(merged.values())

    pairs: list[dict[str, str]] = []
    for item in raw.get("a_h_pairs") or []:
        pairs.append(
            {
                "issuer_id": _text(item.get("issuer_id")),
                "name": _text(item.get("name")),
                "a_symbol": _text(item.get("a_symbol")).upper(),
                "h_symbol": _text(item.get("h_symbol")).upper(),
            }
        )
    return {"version": _text(raw.get("version")) or "1", "markets": markets, "a_h_pairs": pairs}


def market_universe(market: str) -> list[dict[str, Any]]:
    config = load_global_market_universe()
    market_config = config["markets"].get(normalize_market(market)) or {}
    items = [*(market_config.get("indices") or []), *(market_config.get("anchors") or [])]
    if market == "HK":
        pair_names = {item["h_symbol"]: item for item in config["a_h_pairs"]}
        for symbol, pair in pair_names.items():
            items.append(
                _normalize_security(
                    {"symbol": symbol, "name": pair["name"], "exchange": "HKEX", "group": "a_h_tech"},
                    market="HK",
                    role="anchor",
                )
            )
    return list({item["symbol"]: item for item in items}.values())


def market_metadata(market: str) -> dict[str, str]:
    market = normalize_market(market)
    if market == "A":
        return {"market": "A", "timezone": "Asia/Shanghai", "currency": "CNY", "coverage_scope": "full_market"}
    config = load_global_market_universe()["markets"].get(market) or {}
    return {
        "market": market,
        "timezone": _text(config.get("timezone")),
        "currency": _text(config.get("currency")),
        "coverage_scope": _text(config.get("coverage_scope")),
    }
