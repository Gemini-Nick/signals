# -*- coding: utf-8 -*-
"""Strategy and AI factor factory endpoints for Signals workbenches and agents."""
from typing import Any

from fastapi import APIRouter, Body, Query

from signals.strategy.ai_factor_factory import (
    build_ai_factor_factory,
    create_factor_draft,
    disable_factor,
    publish_factor,
    run_factor_validation,
)
from signals.strategy.snapshot import get_strategy_snapshot

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


@router.get("/snapshot")
async def strategy_snapshot(
    persist: bool = Query(False, description="Persist the generated snapshot to Mongo"),
):
    return get_strategy_snapshot(persist=persist)


@router.get("/ai-factor-factory")
async def ai_factor_factory():
    return build_ai_factor_factory()


@router.post("/ai-factor-factory/draft")
async def ai_factor_factory_draft(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return create_factor_draft(
        idea=str(payload.get("idea") or payload.get("hypothesis") or ""),
        factor_id=str(payload.get("factor_id") or ""),
        persist=bool(payload.get("persist", True)),
    )


@router.post("/ai-factor-factory/validate")
async def ai_factor_factory_validate(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    observations = payload.get("observations")
    if not isinstance(observations, list):
        observations = None
    return run_factor_validation(
        factor_id=str(payload.get("factor_id") or ""),
        observations=observations,
        persist=bool(payload.get("persist", True)),
    )


@router.post("/ai-factor-factory/publish")
async def ai_factor_factory_publish(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return publish_factor(
        factor_id=str(payload.get("factor_id") or ""),
        live_enabled=bool(payload.get("live_enabled", True)),
        approved_by=str(payload.get("approved_by") or "trader"),
    )


@router.post("/ai-factor-factory/disable")
async def ai_factor_factory_disable(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return disable_factor(
        factor_id=str(payload.get("factor_id") or ""),
        reason=str(payload.get("reason") or ""),
    )
