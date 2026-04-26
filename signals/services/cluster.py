# -*- coding: utf-8 -*-
"""Cluster service backed by gateway snapshots and cached signal pools."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _market_status() -> dict[str, Any]:
    try:
        from signals.core.market_hours import get_market_detail, get_session_mode

        session = get_session_mode()
        return {
            "session_name": session.name,
            "session_label": session.label,
            "a_live": session.a_live,
            "hk_live": session.hk_live,
            "us_live": session.us_live,
            "markets": get_market_detail(),
        }
    except Exception:
        return {"session_label": "未知", "a_live": False, "markets": {}}


def get_latest(top: int = 3, mode: str = "realtime") -> dict[str, Any]:
    from signals.core.clustering import cluster_concepts, cluster_industries

    industry = cluster_industries(top_n=top, mode=mode)
    concept = cluster_concepts(top_n=top, mode=mode)
    result = {
        "industry": industry or {"top": [], "all_clusters": [], "meta": {"error": "行业数据加载中"}},
        "concept": concept or {"top": [], "all_clusters": [], "meta": {"error": "概念数据加载中"}},
        "market_status": _market_status(),
    }
    warnings = []
    for key in ("industry", "concept"):
        meta = (result.get(key) or {}).get("meta") or {}
        if meta.get("error"):
            warnings.append(str(meta["error"]))
    if warnings:
        result["data_warning"] = "；".join(warnings)
    return result


def get_history(date: str = "") -> dict[str, Any]:
    from signals.core.cluster_store import load_result, load_week

    if date:
        result = load_result(date)
        if result:
            return {"date": date, "result": result}
        return {"date": date, "result": None, "error": f"无 {date} 数据"}
    week = load_week()
    return {"week": week, "count": len(week)}


def get_watchlist(direction: str = "", mode: str = "belief", top: int = 30) -> dict[str, Any]:
    """Return cached signal-pool candidates without live provider scans."""
    if not direction:
        return {"error": "请指定方向（direction 参数）", "results": []}
    try:
        from signals.data.gateway import get_signal_pool
        from signals.data.models import DataRequest

        response = get_signal_pool(DataRequest(
            domain="signal",
            mode="historical",
            purpose="review",
            allow_stale=True,
        ))
        rows = response.data or []
        filtered = []
        for item in rows:
            details = str(item.get("details") or "")
            if direction not in details and direction not in str(item.get("symbol") or ""):
                continue
            filtered.append({
                "symbol": item.get("symbol"),
                "name": item.get("name") or item.get("symbol"),
                "signal_type": item.get("signal_type"),
                "signal_date": item.get("signal_date"),
                "score": item.get("total_score") or item.get("confidence") or 0,
                "grade": "A" if float(item.get("total_score") or 0) >= 80 else "B",
                "mode": mode,
                "source": response.source,
                "freshness": response.freshness,
            })
            if len(filtered) >= top:
                break
        return {
            "direction": direction,
            "mode": mode,
            "total": len(filtered),
            "grade_a": len([r for r in filtered if r.get("grade") == "A"]),
            "grade_b": len([r for r in filtered if r.get("grade") == "B"]),
            "grade_c": 0,
            "results": filtered,
            "source": response.source,
            "freshness": response.freshness,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as exc:
        return {"error": str(exc), "results": []}
