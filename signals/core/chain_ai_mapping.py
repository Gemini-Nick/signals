# -*- coding: utf-8 -*-
"""Optional AI adjudication for industry-chain mapping.

Rules recall candidate chains. The AI layer can only accept, reject, or mark
those candidates ambiguous; it cannot invent a chain outside the taxonomy.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from pymongo.database import Database

_PROVIDER_COOLDOWN_UNTIL = 0.0
_PROVIDER_COOLDOWN_REASON = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _env_bool(name: str, default: str = "auto") -> str:
    value = _text(os.getenv(name, default)).lower()
    if value in {"1", "true", "yes", "on"}:
        return "true"
    if value in {"0", "false", "no", "off"}:
        return "false"
    return "auto"


def _weclaw_config_path() -> Path:
    return Path(_text(os.getenv("SIGNALS_CHAIN_AI_WECLAW_CONFIG")) or "~/.weclaw/config.json").expanduser()


def _weclaw_agent_name() -> str:
    return _text(os.getenv("SIGNALS_CHAIN_AI_WECLAW_AGENT")) or "openclaw"


def _weclaw_http_agent() -> dict[str, Any]:
    path = _weclaw_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    agent = ((data.get("agents") or {}).get(_weclaw_agent_name()) or {})
    if _text(agent.get("type")) != "http":
        return {}
    endpoint = _text(agent.get("endpoint"))
    model = _text(agent.get("model"))
    if not endpoint or not model:
        return {}
    return {
        "source": f"weclaw:{_weclaw_agent_name()}",
        "endpoint": endpoint,
        "api_key": _text(agent.get("api_key")),
        "model": model,
        "headers": agent.get("headers") if isinstance(agent.get("headers"), dict) else {},
    }


def _provider_config() -> dict[str, Any]:
    endpoint = _text(os.getenv("SIGNALS_CHAIN_AI_ENDPOINT"))
    base_url = (
        _text(os.getenv("SIGNALS_CHAIN_AI_BASE_URL"))
        or _text(os.getenv("OPENAI_BASE_URL"))
        or _text(os.getenv("DEEPSEEK_BASE_URL"))
    ).rstrip("/")
    model = (
        _text(os.getenv("SIGNALS_CHAIN_AI_MODEL"))
        or _text(os.getenv("OPENAI_MODEL"))
        or _text(os.getenv("DEEPSEEK_MODEL"))
    )
    api_key = (
        _text(os.getenv("SIGNALS_CHAIN_AI_API_KEY"))
        or _text(os.getenv("OPENAI_API_KEY"))
        or _text(os.getenv("DEEPSEEK_API_KEY"))
    )
    if (endpoint or base_url) and model:
        return {
            "source": "env",
            "endpoint": endpoint,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "headers": {},
        }
    return _weclaw_http_agent()


def _endpoint() -> str:
    config = _provider_config()
    endpoint = _text(config.get("endpoint"))
    if endpoint:
        return endpoint
    base_url = _text(config.get("base_url")).rstrip("/")
    return f"{base_url}/chat/completions" if base_url else ""


def _model() -> str:
    return _text(_provider_config().get("model"))


def _headers() -> dict[str, str]:
    config = _provider_config()
    headers = {
        str(key): str(value)
        for key, value in (config.get("headers") or {}).items()
        if _text(key) and _text(value)
    }
    headers["Content-Type"] = "application/json"
    api_key = _text(config.get("api_key"))
    if api_key:
        headers.setdefault("Authorization", f"Bearer {api_key}")
    return headers


def _timeout_seconds() -> float:
    try:
        return max(1.0, min(30.0, float(os.getenv("SIGNALS_CHAIN_AI_TIMEOUT_SECONDS", "8"))))
    except Exception:
        return 8.0


def _cache_ttl_seconds() -> int:
    try:
        return max(60, min(30 * 24 * 3600, int(os.getenv("SIGNALS_CHAIN_AI_CACHE_TTL_SECONDS", "604800"))))
    except Exception:
        return 604800


def ai_mapping_status() -> dict[str, Any]:
    enabled = _env_bool("SIGNALS_CHAIN_AI_ENABLED")
    config = _provider_config()
    key = _text(config.get("api_key"))
    endpoint = _endpoint()
    model = _text(config.get("model"))
    configured = bool(endpoint and model)
    active = configured and enabled != "false"
    return {
        "enabled": enabled,
        "active": active,
        "configured": configured,
        "provider": _text(config.get("source")) or "",
        "endpoint_configured": bool(endpoint),
        "model": model,
        "api_key_configured": bool(key),
        "weclaw_config_path": str(_weclaw_config_path()),
        "weclaw_agent": _weclaw_agent_name(),
    }


def _provider_cooldown_reason() -> str:
    if _PROVIDER_COOLDOWN_UNTIL > time.monotonic():
        return _PROVIDER_COOLDOWN_REASON or "provider_in_cooldown"
    return ""


def _mark_provider_cooldown(exc: Exception) -> None:
    global _PROVIDER_COOLDOWN_UNTIL, _PROVIDER_COOLDOWN_REASON
    reason = ""
    if isinstance(exc, requests.HTTPError):
        status_code = getattr(exc.response, "status_code", None)
        if status_code in {401, 403, 404}:
            reason = f"http_{status_code}"
    elif isinstance(exc, requests.RequestException):
        reason = exc.__class__.__name__
    if not reason:
        return
    _PROVIDER_COOLDOWN_REASON = reason
    _PROVIDER_COOLDOWN_UNTIL = time.monotonic() + 600.0


def _candidate_key(match: dict[str, Any]) -> str:
    return f"{_text(match.get('chain_id'))}:{_text(match.get('node_id'))}"


def _candidate_signature(matches: list[dict[str, Any]]) -> str:
    payload = [
        {
            "chain_id": _text(match.get("chain_id")),
            "node_id": _text(match.get("node_id")),
            "score": _int(match.get("score")),
            "hit_terms": match.get("hit_terms") or [],
            "evidence_sources": match.get("evidence_sources") or [],
        }
        for match in matches
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_key(source: dict[str, Any], matches: list[dict[str, Any]]) -> str:
    raw = "|".join([
        _text(source.get("kind")),
        _text(source.get("name")),
        _text(source.get("code")),
        _candidate_signature(matches),
        _model(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _candidate_payload(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for match in matches[:12]:
        output.append({
            "candidate_id": _candidate_key(match),
            "chain_id": _text(match.get("chain_id")),
            "chain_name": _text(match.get("chain_name")),
            "node_id": _text(match.get("node_id")),
            "node_name": _text(match.get("node_name")),
            "rule_confidence": _int(match.get("confidence") or match.get("score")),
            "rule_hit_terms": match.get("hit_terms") or [],
            "rule_evidence_sources": match.get("evidence_sources") or [],
            "industries": match.get("industries") or [],
        })
    return output


def _prompt_payload(source: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task": "judge_a_share_board_to_industry_chain_mapping",
        "source_board": {
            "kind": _text(source.get("kind")),
            "name": _text(source.get("name")),
            "code": _text(source.get("code")),
            "rank": source.get("rank"),
            "change_pct": source.get("change_pct"),
            "leader_name": _text(source.get("leader_name")),
        },
        "candidate_chains": _candidate_payload(matches),
        "rules": [
            "Only select from candidate_chains; never invent chain_id or node_id.",
            "Prefer exact semantic board-to-chain fit over broad industry overlap.",
            "If the board is a broad bucket that could support multiple chains, return ambiguous.",
            "If none of the candidates is semantically appropriate, return unmapped.",
            "Return compact JSON only.",
        ],
        "output_schema": {
            "status": "mapped | unmapped | ambiguous",
            "decisions": [
                {
                    "candidate_id": "chain_id:node_id",
                    "confidence": "0-100 integer",
                    "reason": "short Chinese reason",
                    "matched_terms": ["terms that justify the decision"],
                }
            ],
            "reason": "short Chinese reason for unmapped/ambiguous",
        },
    }


def _chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    status = ai_mapping_status()
    if not status["active"]:
        return {"status": "disabled", "reason": "ai_mapping_not_configured", "provider_status": status}

    url = _endpoint()
    headers = _headers()
    body = {
        "model": _model(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是A股产业链映射审核器。你只能基于输入候选链做判断，"
                    "不能发明候选之外的产业链。输出必须是严格JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
        ],
    }
    response = requests.post(url, headers=headers, json=body, timeout=_timeout_seconds())
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_json_content(content)


def _parse_json_content(content: Any) -> dict[str, Any]:
    text = _text(content)
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI mapping response is not a JSON object")
    return parsed


def _normalize_decision(raw: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = {_candidate_key(match): match for match in matches}
    status = _text(raw.get("status")).lower()
    if status not in {"mapped", "unmapped", "ambiguous"}:
        status = "ambiguous"
    decisions: list[dict[str, Any]] = []
    for item in raw.get("decisions") or []:
        if not isinstance(item, dict):
            continue
        candidate_id = _text(item.get("candidate_id") or f"{_text(item.get('chain_id'))}:{_text(item.get('node_id'))}")
        if candidate_id not in allowed:
            continue
        confidence = max(0, min(100, _int(item.get("confidence"))))
        decisions.append({
            "candidate_id": candidate_id,
            "confidence": confidence,
            "reason": _text(item.get("reason"))[:160],
            "matched_terms": [_text(term) for term in item.get("matched_terms") or [] if _text(term)][:8],
        })
    if status == "mapped" and not decisions:
        status = "ambiguous"
    decisions.sort(key=lambda item: item["confidence"], reverse=True)
    return {
        "status": status,
        "decisions": decisions[:3],
        "reason": _text(raw.get("reason"))[:200],
    }


def _read_cache(db: Database, key: str, now: datetime) -> dict[str, Any] | None:
    doc = db["chain_ai_mapping_cache"].find_one({"_id": key}, {"_id": 0}) if db is not None else None
    if not doc:
        return None
    updated_at = doc.get("updated_at")
    if isinstance(updated_at, datetime) and updated_at < now - timedelta(seconds=_cache_ttl_seconds()):
        return None
    decision = doc.get("decision")
    return decision if isinstance(decision, dict) else None


def _write_cache(db: Database, key: str, source: dict[str, Any], matches: list[dict[str, Any]], decision: dict[str, Any], now: datetime) -> None:
    if db is None:
        return
    db["chain_ai_mapping_cache"].update_one(
        {"_id": key},
        {"$set": {
            "_id": key,
            "source_kind": _text(source.get("kind")),
            "source_name": _text(source.get("name")),
            "source_code": _text(source.get("code")),
            "candidate_signature": _candidate_signature(matches),
            "model": _model(),
            "decision": decision,
            "updated_at": now,
        }},
        upsert=True,
    )


def decide_chain_mapping(
    db: Database,
    source: dict[str, Any],
    matches: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    status = ai_mapping_status()
    if not status["active"]:
        return {"status": "disabled", "reason": "ai_mapping_not_configured", "provider_status": status}
    cooldown_reason = _provider_cooldown_reason()
    if cooldown_reason:
        return {"status": "disabled", "reason": cooldown_reason, "provider_status": status}
    key = _cache_key(source, matches)
    cached = _read_cache(db, key, now)
    if cached:
        return {**cached, "cache": "hit"}
    started = time.monotonic()
    try:
        raw = _chat_completion(_prompt_payload(source, matches))
        decision = _normalize_decision(raw, matches)
        decision.update({
            "source": "ai_semantic_mapper",
            "cache": "miss",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "provider_status": status,
        })
    except Exception as exc:
        _mark_provider_cooldown(exc)
        decision = {
            "status": "error",
            "reason": str(exc)[:200],
            "source": "ai_semantic_mapper",
            "cache": "miss",
            "provider_status": status,
        }
    if decision.get("status") != "error":
        _write_cache(db, key, source, matches, decision, now)
    return decision
