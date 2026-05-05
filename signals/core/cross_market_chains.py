# -*- coding: utf-8 -*-
"""Cross-market industry-chain ontology for AI hardware factor research."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

CONFIG_PATH = Path(__file__).with_name("cross_market_ai_hardware_chains.yaml")
TARGET_CHAIN_ALIASES = {
    "computing_infrastructure": "ai_compute",
    "terminal_capex": "ai_compute",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return _text(value).replace(".", "").replace("-", "").replace(" ", "").upper()


def _unique(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        item = _text(value)
        if item and item not in output:
            output.append(item)
    return output


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_rep(rep: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _text(rep.get("symbol")).upper(),
        "name": _text(rep.get("name")),
        "role": _text(rep.get("role")),
        "evidence_type": _text(rep.get("evidence_type")),
        "priority": int(_float(rep.get("priority"), 0)),
    }


def _normalize_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        "group": _text(mapping.get("group")),
        "target_chain_id": _text(mapping.get("target_chain_id")),
        "concepts": _unique(mapping.get("concepts") or []),
        "core_representatives": _unique(mapping.get("core_representatives") or []),
        "elastic_representatives": _unique(mapping.get("elastic_representatives") or []),
        "mapping_rule": _text(mapping.get("mapping_rule")),
        "lag_rule": _text(mapping.get("lag_rule")),
        "confirmation_rule": _text(mapping.get("confirmation_rule")),
        "confidence": _float(mapping.get("confidence"), 0.0),
    }


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": _text(node.get("node_id")),
        "name": _text(node.get("name")),
        "layer": _text(node.get("layer")),
        "terminal_evidence_only": bool(node.get("terminal_evidence_only")),
        "keywords": _unique(node.get("keywords") or []),
        "us_representatives": [
            _normalize_rep(rep)
            for rep in node.get("us_representatives") or []
            if isinstance(rep, dict)
        ],
        "a_share_mapping": _normalize_mapping(node.get("a_share_mapping") or {}),
    }


@lru_cache(maxsize=4)
def load_cross_market_chains(config_path: str | None = None) -> dict[str, dict[str, Any]]:
    path = Path(config_path) if config_path else CONFIG_PATH
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    chains: dict[str, dict[str, Any]] = {}
    for chain in raw.get("chains") or []:
        if not isinstance(chain, dict):
            continue
        chain_id = _text(chain.get("chain_id"))
        if not chain_id:
            continue
        nodes = [_normalize_node(node) for node in chain.get("nodes") or [] if isinstance(node, dict)]
        chains[chain_id] = {
            "chain_id": chain_id,
            "name": _text(chain.get("name")),
            "aliases": _unique(chain.get("aliases") or []),
            "source_market": _text(chain.get("source_market")),
            "target_market": _text(chain.get("target_market")),
            "benchmark_symbols": _unique(chain.get("benchmark_symbols") or []),
            "default_nodes": _unique(chain.get("default_nodes") or []),
            "nodes": nodes,
            "nodes_by_id": {node["node_id"]: node for node in nodes if node.get("node_id")},
        }
    return chains


def match_cross_market_nodes(idea_text: str, *, chain_id: str = "us_ai_hardware") -> list[dict[str, Any]]:
    """Return nodes whose keywords/reps are mentioned by the trader idea."""

    chain = load_cross_market_chains().get(chain_id) or {}
    text = _norm(idea_text)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, node in enumerate(chain.get("nodes") or []):
        score = 0
        for keyword in node.get("keywords") or []:
            token = _norm(keyword)
            if token and token in text:
                score += 50 + min(len(token), 16)
        for rep in node.get("us_representatives") or []:
            symbol = _norm(rep.get("symbol"))
            name = _norm(rep.get("name"))
            if symbol and symbol in text:
                score += 80 + int(rep.get("priority") or 0)
            elif name and name in text:
                score += 70 + int(rep.get("priority") or 0)
        if not node.get("terminal_evidence_only"):
            mapping = node.get("a_share_mapping") or {}
            for token in [mapping.get("group"), *(mapping.get("concepts") or [])]:
                normalized = _norm(token)
                if normalized and normalized in text:
                    score += 45 + min(len(normalized), 16)
        if score > 0:
            scored.append((score, -index, node))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [node for _, _, node in scored]


def build_ai_hardware_portfolio(idea_text: str, *, chain_id: str = "us_ai_hardware") -> dict[str, Any]:
    """Build trader-readable US trigger basket and A-share reaction basket."""

    chain = load_cross_market_chains().get(chain_id) or {}
    nodes_by_id = chain.get("nodes_by_id") or {}
    matched = match_cross_market_nodes(idea_text, chain_id=chain_id)
    selected: list[dict[str, Any]] = []
    for node in matched:
        if node.get("node_id") not in {item.get("node_id") for item in selected}:
            selected.append(node)
    terminal_only = bool(selected) and all(node.get("terminal_evidence_only") for node in selected)
    if not terminal_only:
        for node_id in chain.get("default_nodes") or []:
            if len(selected) >= 8:
                break
            node = nodes_by_id.get(node_id)
            if node and node.get("node_id") not in {item.get("node_id") for item in selected}:
                selected.append(node)

    selected = _dedupe_nodes(_order_nodes_for_trading_idea(idea_text, selected))
    direct_nodes = [node for node in selected if not node.get("terminal_evidence_only")]
    terminal_nodes = [node for node in selected if node.get("terminal_evidence_only")]
    weights = _node_weights(direct_nodes)

    us_trigger_basket = [_us_basket_item(node, weights.get(node["node_id"], 0.0)) for node in direct_nodes]
    cn_reaction_basket = [_cn_basket_item(node, weights.get(node["node_id"], 0.0)) for node in direct_nodes]
    terminal_evidence = [_terminal_evidence_item(node) for node in terminal_nodes]
    us_driver_nodes = [
        _us_driver_node(node, weights.get(node["node_id"], 0.0), index)
        for index, node in enumerate(direct_nodes)
    ]
    cn_mapping_nodes = [
        _cn_mapping_node(item, us_driver_nodes[index] if index < len(us_driver_nodes) else {})
        for index, item in enumerate(cn_reaction_basket)
    ]

    return {
        "ontology_id": chain_id,
        "ontology_name": chain.get("name") or "",
        "source_market": chain.get("source_market") or "US",
        "target_market": chain.get("target_market") or "A",
        "benchmark_symbols": chain.get("benchmark_symbols") or ["SOX", "QQQ"],
        "selected_nodes": [node.get("node_id") for node in selected],
        "us_trigger_basket": us_trigger_basket,
        "cn_reaction_basket": cn_reaction_basket,
        "terminal_evidence": terminal_evidence,
        "us_driver_nodes": us_driver_nodes,
        "cn_mapping_nodes": cn_mapping_nodes,
        "rhythm_windows": _rhythm_windows(),
        "multi_timeframe_map": _multi_timeframe_map(),
        "selection_score": {
            "formula": "mapping_confidence * us_driver_strength * cn_acceptance_strength * historical_lead_lag * liquidity - overheating_or_failed_acceptance_penalty",
            "status": "draft_mapping_pending_kline",
            "note": "草稿阶段只给映射优先级；运行节奏融合或历史验证后，A股承接强度和 lead-lag 证据才会写入。",
        },
        "trigger_basket": us_trigger_basket,
        "reaction_basket": cn_reaction_basket,
        "mapping_rule": _mapping_rule(direct_nodes, terminal_nodes),
        "signal_formula": "us_strength = sum(node_weight * node_excess_vs_SOX_QQQ) + breadth + order_news_strength; cn_score = exposure_weight * T+1_acceptance * volume_breadth - overheating_penalty.",
        "rebalance": "日频；US T 日收盘定格，美股信号只允许影响 A股 T+1 及之后。",
        "portfolio_role": "美股篮子只做触发源和解释变量；A股反应篮子只进入观察/盘前池，不自动下单。",
        "reproducibility_boundary": {
            "as_of": "US T close -> A-share T+1 open and later only",
            "lookahead_guard": "No US T+1 or A-share intraday future information is allowed when forming the T+1 pre-market pool.",
            "benchmarks": chain.get("benchmark_symbols") or ["SOX", "QQQ"],
            "cost_model": "reuse Signals validation cost/slippage config",
        },
    }


def _dedupe_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for node in nodes:
        node_id = _text(node.get("node_id"))
        if not node_id or node_id in seen:
            continue
        output.append(node)
        seen.add(node_id)
    return output


def _order_nodes_for_trading_idea(idea_text: str, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = idea_text.lower()
    priority: list[str] = []
    has_networking_anchor = any(token in text for token in ("avgo", "broadcom", "博通", "anet", "arista", "mrvl", "marvell", "网络", "交换机", "asic"))
    if has_networking_anchor:
        priority.append("networking_switch_asic")
    if any(token in text for token in ("lumentum", "lite", "coherent", "cohr", "fabrinet", "fn", "光模块", "光器件", "cpo")):
        priority.append("optical_interconnect")
    elif "硅光" in text and not has_networking_anchor:
        priority.append("optical_interconnect")
    if any(token in text for token in ("vertiv", "vrt", "eaton", "etn", "nvent", "nvt", "液冷", "热管理", "散热", "cdu")):
        priority.append("thermal_liquid_cooling")
    if any(token in text for token in ("amphenol", "aph", "te connectivity", "tel", "铜连接", "铜缆", "高速连接")):
        priority.append("copper_interconnect")
    if any(token in text for token in ("ttm", "ttmi", "pcb", "ccl", "覆铜板")):
        priority.append("pcb_ccl_materials")
    if any(token in text for token in ("mu", "micron", "hbm", "存储")):
        priority.append("hbm_memory")
    priority.extend(["accelerator_compute", "networking_switch_asic", "ai_server_oem_odm", "foundry_packaging", "hyperscaler_capex_terminal"])
    rank: dict[str, int] = {}
    for index, node_id in enumerate(priority):
        rank.setdefault(node_id, index)
    return sorted(nodes, key=lambda node: rank.get(_text(node.get("node_id")), 999))


def _node_weights(nodes: list[dict[str, Any]]) -> dict[str, float]:
    if not nodes:
        return {}
    base = [max(float(len(nodes) - index), 1.0) for index, _ in enumerate(nodes)]
    total = sum(base)
    return {
        node["node_id"]: round(weight / total, 4)
        for node, weight in zip(nodes, base, strict=False)
        if node.get("node_id")
    }


def _is_conditional_rep(rep: dict[str, Any]) -> bool:
    role = _text(rep.get("role")).lower()
    evidence_type = _text(rep.get("evidence_type")).lower()
    return "conditional" in role or "condition" in evidence_type


def _symbols_for_node(node: dict[str, Any], *, conditional: bool | None = None) -> list[str]:
    reps = sorted(node.get("us_representatives") or [], key=lambda rep: int(rep.get("priority") or 0), reverse=True)
    if conditional is not None:
        reps = [rep for rep in reps if _is_conditional_rep(rep) is conditional]
    symbols = _unique(rep.get("symbol") for rep in reps if rep.get("symbol"))
    if conditional is False and not symbols:
        return _unique(rep.get("symbol") for rep in sorted(node.get("us_representatives") or [], key=lambda rep: int(rep.get("priority") or 0), reverse=True) if rep.get("symbol"))
    return symbols


def _us_basket_item(node: dict[str, Any], weight: float) -> dict[str, Any]:
    reps = sorted(node.get("us_representatives") or [], key=lambda rep: int(rep.get("priority") or 0), reverse=True)
    conditional_symbols = _symbols_for_node(node, conditional=True)
    return {
        "node_id": node.get("node_id"),
        "group": node.get("name"),
        "symbols": _symbols_for_node(node, conditional=False),
        "conditional_symbols": conditional_symbols,
        "weight": weight,
        "role": _us_role(node),
        "evidence": [
            {
                "symbol": rep.get("symbol"),
                "name": rep.get("name"),
                "role": rep.get("role"),
                "evidence_type": rep.get("evidence_type"),
            }
            for rep in reps
        ],
    }


def _cn_basket_item(node: dict[str, Any], weight: float) -> dict[str, Any]:
    mapping = node.get("a_share_mapping") or {}
    concepts = mapping.get("concepts") or []
    candidates = _a_share_candidates_for_mapping(mapping, weight=weight)
    core_candidates = [item for item in candidates if item.get("role") == "core"]
    elastic_candidates = [item for item in candidates if item.get("role") == "elastic"]
    return {
        "source_node_id": node.get("node_id"),
        "group": mapping.get("group") or node.get("name"),
        "target_chain_id": _resolved_target_chain_id(mapping),
        "concepts": concepts,
        "symbols": _unique(item.get("symbol") for item in candidates),
        "core_representatives": _unique(
            [item.get("name") for item in core_candidates] or (mapping.get("core_representatives") or [])
        ),
        "elastic_representatives": _unique(
            [item.get("name") for item in elastic_candidates] or (mapping.get("elastic_representatives") or [])
        ),
        "core_candidates": core_candidates,
        "elastic_candidates": elastic_candidates,
        "candidates": candidates,
        "weight": weight,
        "role": mapping.get("mapping_rule") or "",
        "lag_rule": mapping.get("lag_rule") or "US_T_close_to_A_T_plus_1",
        "confirmation_rule": mapping.get("confirmation_rule") or "",
        "confidence": mapping.get("confidence") or 0.0,
        "selection_status": "draft_mapping_pending_kline",
    }


def _us_driver_node(node: dict[str, Any], weight: float, index: int) -> dict[str, Any]:
    role = "primary_driver" if index == 0 else "confirming_driver"
    return {
        "node_id": node.get("node_id"),
        "name": node.get("name"),
        "role": role,
        "layer": node.get("layer"),
        "symbols": _symbols_for_node(node, conditional=False),
        "conditional_symbols": _symbols_for_node(node, conditional=True),
        "weight": weight,
        "benchmark_symbols": ["SOX", "QQQ"],
        "kline_timeframes": [
            {"market": "US", "freq": "daily", "purpose": "昨夜主趋势和跳空方向", "status": "pending_data"},
            {"market": "US", "freq": "60m", "purpose": "确认美股收盘前是否加速或回落", "status": "pending_data"},
            {"market": "US", "freq": "15m", "purpose": "拆出尾盘抢筹/回落反证", "status": "pending_data"},
        ],
        "driver_rule": _us_role(node),
    }


def _cn_mapping_node(item: dict[str, Any], driver: dict[str, Any]) -> dict[str, Any]:
    candidates = item.get("candidates") or []
    return {
        "source_node_id": item.get("source_node_id"),
        "source_driver": driver.get("name") or "",
        "group": item.get("group"),
        "target_chain_id": item.get("target_chain_id") or "",
        "symbols": item.get("symbols") or [],
        "core_candidates": item.get("core_candidates") or [],
        "elastic_candidates": item.get("elastic_candidates") or [],
        "top_candidates": candidates[:6],
        "mapping_reason": item.get("role") or "",
        "confirmation_rule": item.get("confirmation_rule") or "",
        "lag_rule": item.get("lag_rule") or "US_T_close_to_A_T_plus_1",
        "confidence": item.get("confidence") or 0.0,
        "selection_status": item.get("selection_status") or "draft_mapping_pending_kline",
        "selection_score_formula": "mapping_confidence * us_driver_strength * cn_acceptance_strength * historical_lead_lag * liquidity - penalties",
    }


def _resolved_target_chain_id(mapping: dict[str, Any]) -> str:
    target = _text(mapping.get("target_chain_id"))
    return TARGET_CHAIN_ALIASES.get(target, target)


def _load_industry_chains_safe() -> dict[str, dict[str, Any]]:
    try:
        from signals.core.concept_carriers import load_industry_chains

        return load_industry_chains()
    except Exception:
        return {}


def _a_share_candidates_for_mapping(mapping: dict[str, Any], *, weight: float) -> list[dict[str, Any]]:
    chains = _load_industry_chains_safe()
    target_chain_id = _resolved_target_chain_id(mapping)
    chain = chains.get(target_chain_id) or _best_chain_for_mapping(chains, mapping)
    if not chain:
        return _fallback_name_candidates(mapping, weight=weight)

    nodes = _matching_industry_nodes(chain, mapping)
    candidates: list[dict[str, Any]] = []
    for node in nodes:
        for role, key in (("core", "core_representatives"), ("elastic", "elastic_representatives")):
            for rep in sorted(node.get(key) or [], key=lambda item: int(item.get("priority") or 0), reverse=True):
                if not isinstance(rep, dict):
                    continue
                symbol = _text(rep.get("symbol"))
                name = _text(rep.get("name"))
                if not symbol or not name:
                    continue
                priority = int(_float(rep.get("priority"), 0))
                candidates.append({
                    "symbol": symbol,
                    "name": name,
                    "role": role,
                    "node_id": node.get("node_id"),
                    "node_name": node.get("name"),
                    "relation": _text(rep.get("relation")),
                    "source_note": _text(rep.get("source_note")),
                    "priority": priority,
                    "mapping_reason": mapping.get("mapping_rule") or "",
                    "selection_score": _selection_score(mapping, weight=weight, priority=priority, role=role),
                    "score_status": "draft_mapping_pending_kline",
                    "exclusion_reason": "",
                })
    return _dedupe_candidates(candidates) or _fallback_name_candidates(mapping, weight=weight)


def _best_chain_for_mapping(chains: dict[str, dict[str, Any]], mapping: dict[str, Any]) -> dict[str, Any]:
    tokens = [_norm(mapping.get("group")), *[_norm(item) for item in mapping.get("concepts") or []]]
    best_score = 0
    best_chain: dict[str, Any] = {}
    for chain in chains.values():
        score = 0
        for token in tokens:
            if not token:
                continue
            for value in [chain.get("name"), *(chain.get("aliases") or []), *(chain.get("industries") or [])]:
                candidate = _norm(value)
                if candidate and (candidate in token or token in candidate):
                    score += 10
        if score > best_score:
            best_score = score
            best_chain = chain
    return best_chain


def _matching_industry_nodes(chain: dict[str, Any], mapping: dict[str, Any]) -> list[dict[str, Any]]:
    tokens = [
        _norm(mapping.get("group")),
        *[_norm(item) for item in mapping.get("concepts") or []],
        *[_norm(item) for item in mapping.get("core_representatives") or []],
        *[_norm(item) for item in mapping.get("elastic_representatives") or []],
    ]
    scored: list[tuple[int, dict[str, Any]]] = []
    for node in chain.get("nodes") or []:
        score = 0
        node_terms = [
            node.get("name"),
            *(node.get("keywords") or []),
            *[rep.get("name") for rep in node.get("core_representatives") or [] if isinstance(rep, dict)],
            *[rep.get("name") for rep in node.get("elastic_representatives") or [] if isinstance(rep, dict)],
        ]
        normalized_terms = [_norm(item) for item in node_terms]
        for token in tokens:
            if not token:
                continue
            for term in normalized_terms:
                if term and (term in token or token in term):
                    score += 10 + min(len(term), 8)
        if score:
            scored.append((score, node))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored:
        return [node for _, node in scored[:3]]
    default_id = _text(chain.get("default_node_id"))
    default_node = (chain.get("nodes_by_id") or {}).get(default_id)
    if default_node:
        return [default_node]
    nodes = chain.get("nodes") or []
    return nodes[:1]


def _selection_score(mapping: dict[str, Any], *, weight: float, priority: int, role: str) -> float:
    confidence = _float(mapping.get("confidence"), 0.0)
    role_bonus = 5.0 if role == "core" else 0.0
    score = confidence * 45.0 + weight * 25.0 + min(max(priority, 0), 100) * 0.25 + role_bonus
    return round(min(score, 99.0), 2)


def _fallback_name_candidates(mapping: dict[str, Any], *, weight: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for role, key in (("core", "core_representatives"), ("elastic", "elastic_representatives")):
        for index, name in enumerate(mapping.get(key) or []):
            candidates.append({
                "symbol": "",
                "name": _text(name),
                "role": role,
                "node_id": "",
                "node_name": mapping.get("group") or "",
                "relation": "",
                "source_note": "cross_market_mapping_name_only",
                "priority": max(100 - index * 4, 1),
                "mapping_reason": mapping.get("mapping_rule") or "",
                "selection_score": _selection_score(mapping, weight=weight, priority=max(100 - index * 4, 1), role=role),
                "score_status": "draft_mapping_missing_a_share_code",
                "exclusion_reason": "本地产业链未找到代码，不能进入实盘候选。",
            })
    return candidates


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = _text(item.get("symbol")) or _text(item.get("name"))
        if not key:
            continue
        current = best.get(key)
        if current is None or _float(item.get("selection_score"), 0) > _float(current.get("selection_score"), 0):
            best[key] = item
    return sorted(best.values(), key=lambda item: _float(item.get("selection_score"), 0), reverse=True)


def _rhythm_windows() -> list[dict[str, Any]]:
    return [
        {
            "window_id": "us_overnight_close",
            "label": "昨夜美股",
            "market": "US",
            "timeframe": "daily/60m/15m",
            "question": "美股到底在炒哪条AI硬件支链，尾盘是加速还是回落？",
            "required_evidence": ["relative_vs_SOX_QQQ", "breadth", "late_session_confirmation"],
            "status": "pending_data",
        },
        {
            "window_id": "cn_call_auction",
            "label": "今日竞价",
            "market": "A",
            "timeframe": "集合竞价",
            "question": "A股映射池是否高开过热，龙头/弹性是否同步？",
            "required_evidence": ["gap_not_overheated", "mapped_basket_breadth"],
            "status": "pending_data",
        },
        {
            "window_id": "cn_open_30m",
            "label": "开盘30分钟",
            "market": "A",
            "timeframe": "5m/30m",
            "question": "高开后是否回踩承接，还是直接高开低走？",
            "required_evidence": ["pullback_support", "volume_acceptance"],
            "status": "pending_data",
        },
        {
            "window_id": "cn_intraday_confirm",
            "label": "盘中确认",
            "market": "A",
            "timeframe": "5m/30m",
            "question": "同链条是否扩散，低位补涨是否接上？",
            "required_evidence": ["theme_breadth_expansion", "leader_elastic_sync"],
            "status": "pending_data",
        },
        {
            "window_id": "cn_close_review",
            "label": "收盘复盘",
            "market": "A",
            "timeframe": "daily/30m",
            "question": "当天样本应进入成功、失败还是边界修正？",
            "required_evidence": ["close_above_acceptance", "failure_reason_if_any"],
            "status": "pending_data",
        },
    ]


def _multi_timeframe_map() -> dict[str, Any]:
    return {
        "us": ["daily", "60m", "15m"],
        "a_share": ["daily", "30m", "5m"],
        "as_of": "US T close -> A-share T+1 call auction/open/intraday/close only",
        "lookahead_guard": "A股 T+1 盘前池生成时不能读取美股 T+1 或 A股 T+1 盘中未来数据。",
    }


def _terminal_evidence_item(node: dict[str, Any]) -> dict[str, Any]:
    mapping = node.get("a_share_mapping") or {}
    return {
        "node_id": node.get("node_id"),
        "group": node.get("name"),
        "symbols": _symbols_for_node(node),
        "role": mapping.get("mapping_rule") or "terminal evidence only",
        "direct_a_share_candidates": False,
    }


def _us_role(node: dict[str, Any]) -> str:
    mapping = node.get("a_share_mapping") or {}
    rule = mapping.get("mapping_rule") or ""
    if rule:
        return rule
    return f"{node.get('name')} cross-market trigger source."


def _mapping_rule(direct_nodes: list[dict[str, Any]], terminal_nodes: list[dict[str, Any]]) -> str:
    groups = _unique((node.get("a_share_mapping") or {}).get("group") for node in direct_nodes)
    terminal = _unique(node.get("name") for node in terminal_nodes)
    rule = f"美股触发节点先按产业链分层计算相对 SOX/QQQ 强度，再映射到 A股 {'、'.join(groups)} 反应池；A股必须用 T+1 开盘承接或盘中量价确认二次过滤。"
    if terminal:
        rule += f" {'、'.join(terminal)} 只作为终端 capex 背景证据，不直接生成 A股候选。"
    return rule
