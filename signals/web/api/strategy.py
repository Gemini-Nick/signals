# -*- coding: utf-8 -*-
"""Strategy and AI factor factory endpoints for Signals workbenches and agents."""
from typing import Any

from fastapi import APIRouter, Body, Query
from starlette.concurrency import run_in_threadpool

from signals.strategy.ai_factor_factory import (
    build_ai_factor_factory,
    create_factor_draft,
    disable_factor,
    publish_factor,
    run_signal_first_environment_validation,
    run_factor_rhythm_demo,
    run_factor_validation,
)
from signals.strategy.snapshot import get_strategy_snapshot

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


@router.get("/snapshot")
async def strategy_snapshot(
    persist: bool = Query(False, description="Persist the generated snapshot to Mongo"),
):
    return await run_in_threadpool(get_strategy_snapshot, persist=persist)


@router.get("/ai-factor-factory")
async def ai_factor_factory():
    return await run_in_threadpool(build_ai_factor_factory)


@router.post("/ai-factor-factory/draft")
async def ai_factor_factory_draft(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return await run_in_threadpool(
        create_factor_draft,
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
    mode = str(payload.get("mode") or "").lower()
    environment_id = str(payload.get("environment_id") or "")
    if mode == "signal_first" or environment_id:
        return await run_in_threadpool(
            run_signal_first_environment_validation,
            environment_id=environment_id or str(payload.get("factor_id") or ""),
            observations=observations,
            persist=bool(payload.get("persist", True)),
        )
    demo_mode = _payload_bool(payload.get("demo_mode"), default=observations is None)
    if mode == "demo":
        demo_mode = True
    return await run_in_threadpool(
        run_factor_validation,
        factor_id=str(payload.get("factor_id") or ""),
        idea=str(payload.get("idea") or payload.get("hypothesis") or ""),
        observations=observations,
        persist=bool(payload.get("persist", True)),
        demo_mode=demo_mode,
    )


@router.post("/ai-factor-factory/rhythm-demo")
async def ai_factor_factory_rhythm_demo(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return await run_in_threadpool(
        run_factor_rhythm_demo,
        factor_id=str(payload.get("factor_id") or ""),
        idea=str(payload.get("idea") or payload.get("hypothesis") or ""),
        persist=bool(payload.get("persist", True)),
    )


@router.post("/ai-factor-factory/publish")
async def ai_factor_factory_publish(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return await run_in_threadpool(
        publish_factor,
        factor_id=str(payload.get("factor_id") or ""),
        live_enabled=bool(payload.get("live_enabled", True)),
        approved_by=str(payload.get("approved_by") or "trader"),
    )


@router.post("/ai-factor-factory/disable")
async def ai_factor_factory_disable(
    payload: dict[str, Any] = Body(default_factory=dict),
):
    return await run_in_threadpool(
        disable_factor,
        factor_id=str(payload.get("factor_id") or ""),
        reason=str(payload.get("reason") or ""),
    )


def _payload_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "demo"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default
