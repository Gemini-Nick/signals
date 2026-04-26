# -*- coding: utf-8 -*-
"""Strategy snapshot endpoints for Signals workbenches and agents."""
from fastapi import APIRouter, Query

from signals.strategy.snapshot import get_strategy_snapshot

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


@router.get("/snapshot")
async def strategy_snapshot(
    persist: bool = Query(False, description="Persist the generated snapshot to Mongo"),
):
    return get_strategy_snapshot(persist=persist)
