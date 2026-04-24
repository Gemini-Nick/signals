# -*- coding: utf-8 -*-
"""Signals domain-pack contract endpoints."""
from fastapi import APIRouter, Query

from signals.domain_pack import SignalsPack

router = APIRouter(prefix="/api/pack", tags=["pack"])


@router.get("/dashboard")
async def pack_dashboard(
    recent_limit: int = Query(20, ge=1, le=100),
    backlog_limit: int = Query(10, ge=1, le=50),
):
    pack = SignalsPack()
    return await pack.dashboard(
        recent_limit=recent_limit,
        backlog_limit=backlog_limit,
    )


@router.get("/descriptor")
def pack_descriptor():
    return SignalsPack().describe()
