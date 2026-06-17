# -*- coding: utf-8 -*-
"""Signals domain-pack contract endpoints."""
import asyncio

from fastapi import APIRouter, Query
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
