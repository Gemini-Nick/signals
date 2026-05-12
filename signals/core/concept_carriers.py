# -*- coding: utf-8 -*-
"""Industry-chain loader and scorer for concept chart routing.

The YAML file next to this module is the source of truth. Market-source leaders
from Eastmoney/THS/Sina are short-term evidence; they should not overwrite the
chain-owner representative used for the default chart.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Iterable

import yaml

CONFIG_PATH = Path(__file__).with_name("industry_chains.yaml")

LAYER_LABELS = {
    "upstream": "上游",
    "midstream": "中游",
    "downstream": "下游",
    "terminal": "终端",
}

_BROAD_TERMS_NO_CARRIER_BOOST = {
    "半导体",
    "锂",
    "电池",
    "光伏",
    "医药",
    "化工",
    "军工",
    "银行",
    "证券",
    "保险",
    "汽车",
    "消费",
    "有色",
    "煤炭",
    "石油",
    "地产",
    "农业",
    "环保",
    "电新",
}

_REVERSE_MATCH_DENY_TERMS = {
    "设备",
    "材料",
    "租赁",
    "航空",
    "电池",
    "储能",
    "电力",
    "元件",
}

NON_CHAIN_THEME_TERMS = {
    "本月解禁": "事件/时间维度主题，不对应稳定产业链。",
    "昨日涨停": "交易行为主题，不对应稳定产业链。",
    "昨日连板": "交易行为主题，不对应稳定产业链。",
    "近期强势": "交易行为主题，不对应稳定产业链。",
    "融资融券": "交易制度主题，不对应稳定产业链。",
    "转债标的": "交易工具主题，不对应稳定产业链。",
}

NON_CHAIN_THEME_KEYWORDS = {
    "科创50": "指数样本主题，不对应单一产业链。",
    "沪深300": "指数样本主题，不对应单一产业链。",
    "中证500": "指数样本主题，不对应单一产业链。",
    "上证50": "指数样本主题，不对应单一产业链。",
    "上证180": "指数样本主题，不对应单一产业链。",
    "上证380": "指数样本主题，不对应单一产业链。",
    "HS300": "指数样本主题，不对应单一产业链。",
    "MSCI": "指数/持仓主题，不对应单一产业链。",
    "QFII": "持仓主题，不对应单一产业链。",
    "AB股": "市场结构主题，不对应单一产业链。",
    "AH股": "市场结构主题，不对应单一产业链。",
    "B股": "市场结构主题，不对应单一产业链。",
    "GDR": "融资/市场结构主题，不对应单一产业链。",
    "IPO": "融资事件主题，不对应单一产业链。",
    "举牌": "交易事件主题，不对应稳定产业链。",
    "热股": "交易热度主题，不对应稳定产业链。",
    "预增": "财报预告主题，不对应单一产业链。",
    "预减": "财报预告主题，不对应单一产业链。",
    "扭亏": "财报预告主题，不对应单一产业链。",
    "年报": "财报时间主题，不对应单一产业链。",
    "季报": "财报时间主题，不对应单一产业链。",
    "低价股": "交易风格主题，不对应单一产业链。",
    "价值股": "交易风格主题，不对应单一产业链。",
    "成长股": "交易风格主题，不对应单一产业链。",
    "大盘股": "交易风格主题，不对应单一产业链。",
    "中盘股": "交易风格主题，不对应单一产业链。",
    "小盘股": "交易风格主题，不对应单一产业链。",
    "北交所": "交易场所主题，不对应单一产业链。",
    "基金重仓": "持仓主题，不对应单一产业链。",
    "养老金": "持仓主题，不对应单一产业链。",
    "富时罗素": "指数/持仓主题，不对应单一产业链。",
    "一带一路": "政策/区域主题，不对应单一产业链。",
    "上海自贸": "政策/区域主题，不对应单一产业链。",
    "东北振兴": "政策/区域主题，不对应单一产业链。",
    "中俄贸易": "政策/区域主题，不对应单一产业链。",
    "中字头": "央企风格主题，不对应单一产业链。",
    "中特估": "估值风格主题，不对应单一产业链。",
    "央国企改革": "产权/政策主题，不对应单一产业链。",
    "专精特新": "企业属性主题，不对应单一产业链。",
    "并购重组": "交易事件主题，不对应单一产业链。",
    "历史新高": "交易行为主题，不对应单一产业链。",
    "昨日": "交易行为主题，不对应稳定产业链。",
    "最近多板": "交易行为主题，不对应稳定产业链。",
    "次新股": "上市时间主题，不对应单一产业链。",
    "微盘": "交易风格主题，不对应单一产业链。",
    "权重股": "交易风格主题，不对应单一产业链。",
    "标准普尔": "指数/持仓主题，不对应单一产业链。",
    "央视50": "指数样本主题，不对应单一产业链。",
    "宁组合": "持仓风格主题，不对应单一产业链。",
    "反内卷": "政策/事件主题，不对应单一产业链。",
    "解禁": "事件/时间维度主题，不对应稳定产业链。",
    "ST": "风险状态主题，不对应稳定产业链。",
    "中盘": "交易风格主题，不对应单一产业链。",
    "大盘": "交易风格主题，不对应单一产业链。",
    "小盘": "交易风格主题，不对应单一产业链。",
    "周期股": "交易风格主题，不对应单一产业链。",
    "微利股": "交易风格主题，不对应单一产业链。",
    "百元股": "交易风格主题，不对应单一产业链。",
    "百日新高": "交易行为主题，不对应稳定产业链。",
    "破净": "交易风格主题，不对应单一产业链。",
    "破发": "交易事件主题，不对应单一产业链。",
    "红利": "分红/风格主题，不对应单一产业链。",
    "茅指数": "持仓风格主题，不对应单一产业链。",
    "行业龙头": "企业属性主题，不对应单一产业链。",
    "创业成份": "指数样本主题，不对应单一产业链。",
    "创业板综": "指数样本主题，不对应单一产业链。",
    "深成": "指数样本主题，不对应单一产业链。",
    "深证": "指数样本主题，不对应单一产业链。",
    "沪股通": "资金通道/持仓主题，不对应单一产业链。",
    "深股通": "资金通道/持仓主题，不对应单一产业链。",
    "机构重仓": "持仓主题，不对应单一产业链。",
    "社保重仓": "持仓主题，不对应单一产业链。",
    "证金持股": "持仓主题，不对应单一产业链。",
    "科创板做市": "交易制度主题，不对应单一产业链。",
    "股权激励": "公司治理/事件主题，不对应单一产业链。",
    "股权转让": "交易事件主题，不对应单一产业链。",
    "破增发价": "交易事件主题，不对应单一产业链。",
    "贬值受益": "汇率因子主题，不对应单一产业链。",
    "超级品牌": "品牌风格主题，不对应单一产业链。",
    "超跌股": "交易行为主题，不对应稳定产业链。",
    "近期新高": "交易行为主题，不对应稳定产业链。",
    "参股新三板": "市场结构主题，不对应单一产业链。",
    "独角兽": "企业属性主题，不对应单一产业链。",
    "成渝特区": "区域政策主题，不对应单一产业链。",
    "沪企改革": "区域国企改革主题，不对应单一产业链。",
    "深圳特区": "区域政策主题，不对应单一产业链。",
    "湖北自贸": "区域政策主题，不对应单一产业链。",
    "滨海新区": "区域政策主题，不对应单一产业链。",
    "粤港自贸": "区域政策主题，不对应单一产业链。",
    "西部大开发": "区域政策主题，不对应单一产业链。",
    "长江三角": "区域政策主题，不对应单一产业链。",
    "统一大市场": "政策主题，不对应单一产业链。",
    "首发经济": "消费政策/事件主题，不对应稳定产业链。",
    "内贸流通": "流通政策/渠道主题，不对应稳定产业链。",
    "共享经济": "商业模式/题材主题，不对应稳定产业链。",
    "冰雪经济": "消费事件/季节性主题，不对应稳定产业链。",
}


def non_chain_reason(name: str) -> str:
    """Return a reason when a market theme should not be forced into a chain."""

    text = _text(name)
    if not text:
        return ""
    if text in NON_CHAIN_THEME_TERMS:
        return NON_CHAIN_THEME_TERMS[text]
    for keyword, reason in NON_CHAIN_THEME_KEYWORDS.items():
        if keyword and keyword in text:
            return reason
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    normalized = (
        _text(value)
        .replace("概念", "")
        .replace("板块", "")
        .replace("产业链", "")
        .replace("行业", "")
        .replace("Ⅲ", "")
        .replace("Ⅱ", "")
        .replace("Ⅰ", "")
        .strip()
        .upper()
    )
    return re.sub(r"(?<=[\u4e00-\u9fff])(?:IV|III|II|I)$", "", normalized)


def _matches(text: str, token: str) -> bool:
    if not text or not token:
        return False
    if text == token:
        return True
    if _is_ascii_token(token):
        return _ascii_token_in_text(text, token) or _ascii_token_in_text(token, text)
    if len(token) == 1:
        return token in text
    if token in text:
        return True
    if text in _REVERSE_MATCH_DENY_TERMS:
        return False
    return text in token


def _is_ascii_token(value: str) -> bool:
    return bool(value) and bool(re.fullmatch(r"[A-Z0-9]+", value))


def _ascii_token_in_text(text: str, token: str) -> bool:
    """Avoid matching short acronyms inside unrelated English words, e.g. CRO in MICROLED."""
    if not text or not token:
        return False
    for hit in re.finditer(re.escape(token), text):
        before = text[hit.start() - 1] if hit.start() > 0 else ""
        after = text[hit.end()] if hit.end() < len(text) else ""
        if (not before or not before.isascii() or not before.isalnum()) and (
            not after or not after.isascii() or not after.isalnum()
        ):
            return True
    return False


def _score_match(key: str, token: str, *, exact: int, partial: int) -> int:
    if not _matches(key, token):
        return 0
    if key == token:
        return exact
    return partial + min(len(token), 12)


def _score_keyword_match(key: str, token: str, *, exact: int, partial: int) -> int:
    """Node keywords are directional: broad "半导体" should not hit "半导体设备"."""
    if not key or not token:
        return 0
    if key == token:
        return exact
    if _is_ascii_token(token):
        return partial + min(len(token), 12) if _ascii_token_in_text(key, token) else 0
    if token in key or (len(token) == 1 and token in key):
        return partial + min(len(token), 12)
    return 0


def _unique(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        item = _text(value)
        if item and item not in output:
            output.append(item)
    return output


def _normalize_symbol(symbol: Any) -> str:
    value = _text(symbol).upper()
    if "." in value:
        market, code = value.split(".", 1)
        return f"{market}.{code}"
    if value.isdigit() and len(value) == 6:
        if value.startswith("6"):
            return f"SH.{value}"
        if value.startswith(("0", "3")):
            return f"SZ.{value}"
        if value.startswith(("8", "4")):
            return f"BJ.{value}"
    return value


def _normalize_representative(rep: dict[str, Any], rep_type: str) -> dict[str, Any]:
    return {
        "symbol": _normalize_symbol(rep.get("symbol")),
        "name": _text(rep.get("name")),
        "relation": _text(rep.get("relation")),
        "source_note": _text(rep.get("source_note")),
        "priority": int(rep.get("priority") or 0),
        "representative_type": rep_type,
    }


def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
    layer = _text(node.get("layer"))
    return {
        "node_id": _text(node.get("node_id")),
        "name": _text(node.get("name")),
        "layer": layer,
        "stage": LAYER_LABELS.get(layer, layer),
        "keywords": _unique(node.get("keywords") or []),
        "core_representatives": [
            _normalize_representative(item, "core")
            for item in node.get("core_representatives") or []
            if isinstance(item, dict)
        ],
        "elastic_representatives": [
            _normalize_representative(item, "elastic")
            for item in node.get("elastic_representatives") or []
            if isinstance(item, dict)
        ],
        "upstream_representatives": [
            _normalize_representative(item, "upstream")
            for item in node.get("upstream_representatives") or []
            if isinstance(item, dict)
        ],
        "downstream_representatives": [
            _normalize_representative(item, "downstream")
            for item in node.get("downstream_representatives") or []
            if isinstance(item, dict)
        ],
    }


@lru_cache(maxsize=4)
def load_industry_chains(config_path: str | None = None) -> dict[str, dict[str, Any]]:
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
        nodes_by_id = {node["node_id"]: node for node in nodes if node.get("node_id")}
        chains[chain_id] = {
            "chain_id": chain_id,
            "name": _text(chain.get("name")),
            "aliases": _unique(chain.get("aliases") or []),
            "industries": _unique(chain.get("industries") or []),
            "default_node_id": _text(chain.get("default_node_id")),
            "nodes": nodes,
            "nodes_by_id": nodes_by_id,
        }
    return chains


def _default_node(chain: dict[str, Any]) -> dict[str, Any] | None:
    nodes_by_id = chain.get("nodes_by_id") or {}
    default_id = _text(chain.get("default_node_id"))
    if default_id and default_id in nodes_by_id:
        return nodes_by_id[default_id]
    nodes = chain.get("nodes") or []
    return nodes[0] if nodes else None


def match_industry_chains(
    query: str,
    *,
    aliases: Iterable[str] = (),
    related_industries: Iterable[str] = (),
) -> list[dict[str, Any]]:
    query_keys = [_norm(query)] + [_norm(item) for item in aliases]
    industry_keys = [_norm(item) for item in related_industries]
    keys = [item for item in query_keys + industry_keys if item]
    keys = [item for index, item in enumerate(keys) if item not in keys[:index]]
    matches: list[dict[str, Any]] = []

    for chain_id, chain in load_industry_chains().items():
        chain_score = 0
        chain_hits: list[str] = []
        evidence_sources: list[str] = []
        for key in keys:
            for alias in chain.get("aliases") or []:
                token = _norm(alias)
                score = _score_match(key, token, exact=92, partial=52)
                if score:
                    chain_score = max(chain_score, score)
                    chain_hits.append(alias)
                    evidence_sources.append("alias")
            for industry in chain.get("industries") or []:
                token = _norm(industry)
                score = _score_match(key, token, exact=76, partial=42)
                if score:
                    chain_score = max(chain_score, score)
                    chain_hits.append(industry)
                    evidence_sources.append("industry")

        node_matches: list[tuple[dict[str, Any], int, list[str], list[str]]] = []
        for node in chain.get("nodes") or []:
            node_score = 0
            node_hits: list[str] = []
            node_sources: list[str] = []
            for key in keys:
                for keyword in node.get("keywords") or []:
                    token = _norm(keyword)
                    score = _score_keyword_match(key, token, exact=96, partial=58)
                    if score:
                        node_score = max(node_score, score)
                        node_hits.append(keyword)
                        node_sources.append("node_keyword")
            if node_score:
                node_matches.append((node, node_score, node_hits, node_sources))

        if not node_matches and chain_score:
            node = _default_node(chain)
            if node:
                node_matches.append((node, chain_score, [], []))

        for node, node_score, node_hits, node_sources in node_matches:
            score = max(chain_score, node_score)
            if score <= 0:
                continue
            hit_terms = list(dict.fromkeys(chain_hits + node_hits))[:8]
            matches.append({
                "chain_id": chain_id,
                "chain_name": chain["name"],
                "node_id": node.get("node_id"),
                "node_name": node.get("name"),
                "layer": node.get("layer"),
                "stage": node.get("stage"),
                "score": score,
                "confidence": min(100, score),
                "hit_terms": hit_terms,
                "industries": list(chain.get("industries", [])),
                "evidence_sources": list(dict.fromkeys(evidence_sources + node_sources))[:6],
                "representatives": [
                    *(node.get("core_representatives") or []),
                    *(node.get("elastic_representatives") or []),
                    *(node.get("upstream_representatives") or []),
                    *(node.get("downstream_representatives") or []),
                ],
                "chain": chain,
                "node": node,
            })

    matches.sort(key=lambda item: (int(item["score"]), 1 if item.get("node_id") else 0), reverse=True)
    return matches


def industry_hints_for_concept(
    concept_name: str,
    *,
    aliases: Iterable[str] = (),
    limit: int = 5,
) -> list[str]:
    hints: list[str] = []
    for match in match_industry_chains(concept_name, aliases=aliases):
        for industry in match.get("industries") or []:
            if industry not in hints:
                hints.append(industry)
            if len(hints) >= limit:
                return hints
    return hints


def _carrier_boost(rep: dict[str, Any], terms: list[str], node_name: str) -> int:
    rep_terms = [
        _norm(rep.get("name")),
        _norm(rep.get("relation")),
        _norm(node_name),
    ]
    boost = 0
    for query_term in terms:
        if not query_term or query_term in _BROAD_TERMS_NO_CARRIER_BOOST:
            continue
        for rep_term in rep_terms:
            if _matches(query_term, rep_term):
                boost = max(boost, 80)
    return boost


def preferred_concept_carriers(
    concept_name: str,
    aliases: Iterable[str] = (),
    related_industries: Iterable[str] = (),
) -> list[dict[str, Any]]:
    rows_by_key: dict[str, dict[str, Any]] = {}
    boost_terms = [_norm(concept_name)] + [_norm(item) for item in aliases]
    boost_terms = [item for item in boost_terms if item]

    direct_matches = match_industry_chains(concept_name, aliases=aliases)
    matches = direct_matches or match_industry_chains(
        concept_name,
        aliases=aliases,
        related_industries=related_industries,
    )
    best_score = int(matches[0].get("score") or 0) if matches else 0
    for match in matches:
        if best_score >= 80 and int(match.get("score") or 0) < best_score - 20:
            continue
        for rep in match.get("representatives") or []:
            symbol = _normalize_symbol(rep.get("symbol"))
            if not symbol:
                continue
            representative_type = _text(rep.get("representative_type"))
            priority = (
                int(rep.get("priority") or 0)
                + int(match.get("score") or 0)
                + _carrier_boost(rep, boost_terms, _text(match.get("node_name")))
                + (20 if representative_type == "core" else 0)
            )
            row = {
                **rep,
                "symbol": symbol,
                "source": "semantic_industry_chain",
                "chain_id": match["chain_id"],
                "chain_name": match["chain_name"],
                "node_id": match.get("node_id"),
                "node_name": match.get("node_name"),
                "layer": match.get("layer"),
                "stage": match.get("stage"),
                "chain_relation_type": representative_type if representative_type in {"upstream", "downstream"} else "",
                "priority": priority,
                "base_priority": int(rep.get("priority") or 0),
                "confidence": match.get("confidence", 0),
                "hit_terms": match.get("hit_terms", []),
                "evidence_sources": match.get("evidence_sources", []),
            }
            key = f"{symbol}:{representative_type}" if representative_type in {"upstream", "downstream"} else symbol
            existing = rows_by_key.get(key)
            if not existing or int(row["priority"]) > int(existing.get("priority") or 0):
                rows_by_key[key] = row

    rows = list(rows_by_key.values())
    tier = {"core": 4, "elastic": 3, "upstream": 2, "downstream": 1}
    rows.sort(
        key=lambda item: (
            tier.get(_text(item.get("representative_type")), 0),
            int(item.get("priority") or 0),
        ),
        reverse=True,
    )
    return rows


def preferred_carrier_symbols() -> list[str]:
    symbols: list[str] = []
    for chain in load_industry_chains().values():
        for node in chain.get("nodes") or []:
            reps = [
                *(node.get("core_representatives") or []),
                *(node.get("elastic_representatives") or []),
                *(node.get("upstream_representatives") or []),
                *(node.get("downstream_representatives") or []),
            ]
            for rep in reps:
                symbol = _normalize_symbol(rep.get("symbol"))
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
    return symbols


def build_mapping_coverage(
    names: Iterable[str],
    *,
    confidence_threshold: int = 65,
) -> dict[str, Any]:
    mapped: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []
    non_chain: list[dict[str, str]] = []
    unmapped: list[str] = []
    duplicate_alias: dict[str, list[str]] = {}
    normalized_seen: dict[str, list[str]] = {}

    unique_names = _unique(names)
    for name in unique_names:
        norm = _norm(name)
        normalized_seen.setdefault(norm, []).append(name)
        reason = non_chain_reason(name)
        if reason:
            non_chain.append({"name": name, "reason": reason})
            continue
        matches = match_industry_chains(name)
        if not matches:
            unmapped.append(name)
            continue
        best = matches[0]
        row = {
            "name": name,
            "chain_id": best.get("chain_id"),
            "chain_name": best.get("chain_name"),
            "node_id": best.get("node_id"),
            "node_name": best.get("node_name"),
            "layer": best.get("layer"),
            "confidence": best.get("confidence"),
            "hit_terms": best.get("hit_terms") or [],
            "evidence_sources": best.get("evidence_sources") or [],
        }
        if int(best.get("confidence") or 0) < confidence_threshold:
            low_confidence.append(row)
        else:
            mapped.append(row)

    duplicate_alias = {
        norm: values
        for norm, values in normalized_seen.items()
        if norm and len(values) > 1
    }
    accounted = len(mapped) + len(low_confidence) + len(unmapped)
    accounted += len(non_chain)
    return {
        "counts": {
            "total": len(unique_names),
            "mapped": len(mapped),
            "low_confidence": len(low_confidence),
            "non_chain": len(non_chain),
            "unmapped": len(unmapped),
            "duplicate_alias": len(duplicate_alias),
            "accounted": accounted,
        },
        "mapped": mapped,
        "low_confidence": low_confidence,
        "non_chain": non_chain,
        "unmapped": unmapped,
        "duplicate_alias": duplicate_alias,
    }


# Backward-compatible name for older imports. Treat it as read-only.
INDUSTRY_CHAINS = load_industry_chains()
