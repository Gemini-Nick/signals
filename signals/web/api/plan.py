# -*- coding: utf-8 -*-
"""
盘前计划 + 周末策略 API
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..services.engine import get_engine

router = APIRouter(prefix="/api/plan", tags=["plan"])


def _serialize_scenario(sc) -> dict:
    return {
        "name": sc.name,
        "probability_hint": sc.probability_hint,
        "trigger": sc.trigger,
        "action": sc.action,
        "target_prices": sc.target_prices,
        "stop_price": sc.stop_price,
        "rationale": sc.rationale,
    }


def _serialize_plan(plan) -> dict:
    return {
        "symbol": plan.symbol,
        "name": plan.name,
        "current_price": plan.current_price,
        "trend": plan.trend,
        "structure": plan.structure,
        "scenarios": [_serialize_scenario(s) for s in plan.scenarios],
        "key_levels": plan.key_levels,
    }


def _serialize_event(ev) -> dict:
    return {
        "event_name": ev.event_name,
        "event_date": ev.event_date,
        "scenarios": ev.scenarios,
        "affected_sectors": ev.affected_sectors,
    }


def _serialize_weekly(wp) -> dict:
    return {
        "week_label": wp.week_label,
        "market_outlook": wp.market_outlook,
        "position_suggestion": wp.position_suggestion,
        "focus_sectors": wp.focus_sectors,
        "avoid_sectors": wp.avoid_sectors,
        "events": [_serialize_event(e) for e in wp.events],
        "key_levels": wp.key_levels,
        "style_suggestion": wp.style_suggestion,
        "rotation_outlook": wp.rotation_outlook,
    }


@router.post("/generate")
async def plan_generate():
    """
    生成盘前计划 — 基于当前缓存的指数分析结果。
    对每个有数据的指数生成 3 种完全分类情景。
    """
    engine = get_engine()
    if not engine.is_ready():
        return JSONResponse(status_code=503, content={"error": "引擎未就绪，请先加载数据"})

    try:
        from signals.core.planner import generate_plan
        reports = engine.get_index_reports()
        plans = []

        # 对主要指数生成计划
        main_indices = ["沪深300", "上证50", "创业板指", "科创50", "中证500"]
        for r in reports:
            if not getattr(r, "data_available", False):
                continue
            if r.name not in main_indices:
                continue
            # 获取日线 analyzer
            analyzer = engine.get_symbol_analyzer(r.name, "daily")
            if analyzer is None:
                continue
            ma_ctx = getattr(r, "ma_context", None)
            plan = generate_plan(analyzer, ma_ctx)
            plan.name = r.name
            plans.append(_serialize_plan(plan))

        return {"ok": True, "plans": plans}

    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "error": str(e), "detail": traceback.format_exc()
        })


@router.get("/weekly")
async def weekly_strategy():
    """
    生成周末策略 — 整合指数 + 轮动 + 宏观事件。
    """
    engine = get_engine()
    try:
        from signals.core.weekly import generate_weekly
        reports = engine.get_index_reports()
        ctx = engine.get_market_context()
        rv = engine.review_state

        weekly = generate_weekly(
            index_reports=reports,
            market_context=ctx,
            rotation_stage=getattr(rv, "rotation_stage", "") or
                           getattr(ctx, "rotation_stage", "") if ctx else "",
            allocation=getattr(rv, "allocation_suggestion", "") or
                       getattr(ctx, "allocation_suggestion", "") if ctx else "",
        )
        return _serialize_weekly(weekly)

    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "error": str(e), "detail": traceback.format_exc()
        })
