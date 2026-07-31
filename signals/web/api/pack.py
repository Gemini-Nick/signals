# -*- coding: utf-8 -*-
"""Signals domain-pack contract endpoints."""
import asyncio
from typing import Any

from fastapi import APIRouter, Body, Query
from starlette.concurrency import run_in_threadpool

from signals.domain_pack import SignalsPack

router = APIRouter(prefix="/api/pack", tags=["pack"])


@router.get("/dashboard")
async def pack_dashboard(
    recent_limit: int = Query(20, ge=1, le=100),
    backlog_limit: int = Query(10, ge=1, le=50),
    include_ai_factor_factory: bool = Query(False),
):
    def _dashboard():
        pack = SignalsPack()
        return asyncio.run(pack.dashboard(
            recent_limit=recent_limit,
            backlog_limit=backlog_limit,
            include_ai_factor_factory=include_ai_factor_factory,
        ))

    return await run_in_threadpool(_dashboard)


@router.get("/descriptor")
def pack_descriptor():
    return SignalsPack().describe()


@router.get("/cache-status")
def pack_cache_status():
    return SignalsPack().cache_status()


@router.post("/refresh")
def pack_refresh(payload: dict[str, Any] = Body(default_factory=dict)):
    force_postmarket_requested = bool(payload.get("force_postmarket", False))
    result = SignalsPack().trigger_refresh(
        reason=str(payload.get("reason") or "manual"),
        force_live=bool(payload.get("force_live", False)),
        force_postmarket=False,
        run_optional_tasks=bool(payload.get("run_optional_tasks", True)),
        wait=bool(payload.get("wait", False)),
    )
    result.setdefault("deprecated_parameters", {})["force_postmarket"] = {
        "requested": force_postmarket_requested,
        "ignored": True,
        "message": "普通刷新不触发、恢复或补跑盘后流程。",
    }
    return result
