# -*- coding: utf-8 -*-
"""Compatibility adapter for stock -> industry-chain position lookups.

`industry_chains.yaml` is the source of truth. This module keeps the older
`CHAIN_MAP`/`get_chain_position` API alive without maintaining a second hardcoded
industry-chain table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .concept_carriers import load_industry_chains


@dataclass
class ChainNode:
    """产业链节点"""

    position: str
    role: str
    symbols: List[str] = field(default_factory=list)


@dataclass
class ChainPosition:
    """个股在产业链中的位置"""

    chain_name: str
    position: str
    role: str
    related_chains: List[str] = field(default_factory=list)


def _clean_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    return value.split(".", 1)[-1] if "." in value else value


def _build_chain_map() -> tuple[Dict[str, Dict[str, ChainNode]], Dict[str, str]]:
    chain_map: Dict[str, Dict[str, ChainNode]] = {}
    alias_map: Dict[str, str] = {}
    for chain in load_industry_chains().values():
        chain_name = str(chain.get("name") or "")
        if not chain_name:
            continue
        nodes_by_key: Dict[str, ChainNode] = {}
        for node in chain.get("nodes") or []:
            position = str(node.get("stage") or node.get("layer") or "")
            role = str(node.get("name") or "")
            node_key = str(node.get("node_id") or role or position)
            symbols: list[str] = []
            representatives = [
                *(node.get("core_representatives") or []),
                *(node.get("elastic_representatives") or []),
            ]
            for rep in representatives:
                symbol = _clean_symbol(rep.get("symbol", ""))
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
            if not symbols:
                continue
            nodes_by_key[node_key] = ChainNode(position=position, role=role, symbols=symbols)
        chain_map[chain_name] = nodes_by_key
        for alias in [chain.get("chain_id"), *(chain.get("aliases") or [])]:
            alias_text = str(alias or "").strip()
            if alias_text:
                alias_map[alias_text] = chain_name
    return chain_map, alias_map


CHAIN_MAP, _CHAIN_ALIASES = _build_chain_map()
_SYMBOL_INDEX: Dict[str, List[ChainPosition]] = {}


def _build_index() -> None:
    if _SYMBOL_INDEX:
        return
    related_by_symbol: dict[str, list[str]] = {}
    for chain_name, nodes in CHAIN_MAP.items():
        for node in nodes.values():
            for sym in node.symbols:
                related_by_symbol.setdefault(sym, [])
                if chain_name not in related_by_symbol[sym]:
                    related_by_symbol[sym].append(chain_name)

    for chain_name, nodes in CHAIN_MAP.items():
        for node in nodes.values():
            for sym in node.symbols:
                _SYMBOL_INDEX.setdefault(sym, []).append(ChainPosition(
                    chain_name=chain_name,
                    position=node.position,
                    role=node.role,
                    related_chains=[
                        item for item in related_by_symbol.get(sym, [])
                        if item != chain_name
                    ],
                ))


def get_chain_position(symbol: str) -> Optional[ChainPosition]:
    """查询个股所属产业链及位置。"""
    _build_index()
    positions = _SYMBOL_INDEX.get(_clean_symbol(symbol))
    return positions[0] if positions else None


def get_all_chain_positions(symbol: str) -> List[ChainPosition]:
    """查询个股所有关联的产业链位置。"""
    _build_index()
    return _SYMBOL_INDEX.get(_clean_symbol(symbol), [])


def get_chain(chain_name: str) -> Optional[Dict[str, ChainNode]]:
    """获取完整产业链定义，兼容 chain name / chain_id / alias。"""
    direct = CHAIN_MAP.get(chain_name)
    if direct is not None:
        return direct
    canonical = _CHAIN_ALIASES.get(str(chain_name or "").strip())
    return CHAIN_MAP.get(canonical) if canonical else None


def list_chains() -> List[str]:
    """列出所有产业链名称。"""
    return list(CHAIN_MAP.keys())


def get_chain_symbols(chain_name: str, position: str = None) -> List[str]:
    """获取产业链中某位置（或所有位置）的代表标的。"""
    chain = get_chain(chain_name)
    if not chain:
        return []
    symbols: list[str] = []
    for node in chain.values():
        if position is None or node.position == position:
            for symbol in node.symbols:
                if symbol not in symbols:
                    symbols.append(symbol)
    return symbols
