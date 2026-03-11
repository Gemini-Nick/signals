# -*- coding: utf-8 -*-
"""
盘后复盘 API — 日期选择、触发分析、轮询进度、读取结果
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.engine import get_engine
from ..services.date_utils import resolve_start_date, get_date_label, get_all_presets
from ..services.serializers import (
    serialize_index_report,
    serialize_market_context,
    serialize_scored_symbol,
)

router = APIRouter(prefix="/api/review", tags=["review"])


class ReviewRequest(BaseModel):
    start_date: str  # 日期别名或 YYYY-MM-DD


@router.get("/presets")
async def review_presets():
    """返回日期预设列表（供下拉菜单）"""
    return get_all_presets()


@router.post("/run")
async def review_run(req: ReviewRequest):
    """提交复盘任务（后台线程执行）"""
    engine = get_engine()
    rv = engine.review_state

    if rv.is_running:
        return {"ok": False, "message": "复盘正在运行中，请等待"}

    resolved = resolve_start_date(req.start_date)
    label = get_date_label(req.start_date)
    engine.run_review(resolved, label)
    return {"ok": True, "start_date": resolved, "label": label}


@router.get("/status")
async def review_status():
    """轮询复盘进度"""
    rv = get_engine().review_state
    return {
        "is_running": rv.is_running,
        "phase": rv.phase,
        "phase_detail": getattr(rv, "phase_detail", ""),
        "completed": rv.completed,
        "error": rv.error,
        "start_date": rv.start_date,
        "start_label": rv.start_label,
        "timing": getattr(rv, "timing", {}),
    }


@router.get("/results")
async def review_results():
    """获取复盘结果（完成后调用）"""
    rv = get_engine().review_state

    if not rv.completed:
        return JSONResponse(status_code=400, content={
            "error": "复盘尚未完成",
            "is_running": rv.is_running,
            "phase": rv.phase,
        })

    try:
        # 大盘方向横幅
        banner = {}
        if rv.market_context:
            ctx = rv.market_context
            banner = serialize_market_context(ctx)

        # 指数卡片
        index_reports = [serialize_index_report(r) for r in rv.index_reports]

        # 行业双榜
        def _serialize_industry(ind):
            candidates = []
            for c in getattr(ind, "candidates", []):
                candidates.append({
                    "name": c.name, "code": c.code,
                    "role": c.role, "detail": getattr(c, "detail", ""),
                })
            return {
                "name": ind.name,
                "display_name": getattr(ind, "display_name", ind.name),
                "gain_pct": round(getattr(ind, "gain_pct", 0), 2),
                "zt_count": getattr(ind, "zt_count", 0),
                "strong_count": getattr(ind, "strong_count", 0),
                "zbgc_count": getattr(ind, "zbgc_count", 0),
                "composite_score": round(getattr(ind, "composite_score", 0), 1),
                "composite_rank": getattr(ind, "composite_rank", 0),
                "source": getattr(ind, "source", ""),
                "pool_codes": getattr(ind, "pool_codes", []),
                "sector_type": getattr(ind, "sector_type", ""),
                "rotation_line": getattr(ind, "rotation_line", ""),
                "rhythm_phase": getattr(ind, "rhythm_phase", ""),
                "rhythm_score": getattr(ind, "rhythm_score", 0),
                "net_inflow": round(getattr(ind, "net_inflow", 0) or 0, 2),
                "candidates": candidates,
            }

        gain_list = [_serialize_industry(i) for i in rv.gain_list]
        composite_list = [_serialize_industry(i) for i in rv.composite_list]

        # 概念热度
        concept_list = []
        for cp in rv.concepts:
            concept_list.append({
                "name": cp.name,
                "gain_pct": round(getattr(cp, "gain_pct", 0), 2),
                "sector_type": getattr(cp, "sector_type", ""),
            })

        # 轮动
        rotation = {
            "stage": rv.rotation_stage,
            "detail": rv.rotation_detail,
            "allocation": rv.allocation_suggestion,
        }

        # 个股信号
        scored_symbols = [serialize_scored_symbol(s) for s in rv.scored_symbols]

        return {
            "start_date": rv.start_date,
            "start_label": rv.start_label,
            "banner": banner,
            "index_reports": index_reports,
            "gain_list": gain_list,
            "composite_list": composite_list,
            "concepts": concept_list,
            "rotation": rotation,
            "scored_symbols": scored_symbols,
            "timing": getattr(rv, "timing", {}),
        }

    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "error": str(e), "detail": traceback.format_exc()
        })
