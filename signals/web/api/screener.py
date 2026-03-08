# -*- coding: utf-8 -*-
"""Layer 3 信号列表 API"""
from fastapi import APIRouter

from ..services.engine import get_engine
from ..services.serializers import serialize_scored_symbol

router = APIRouter(prefix="/api/screener", tags=["screener"])


@router.get("/results")
def get_screener_results():
    """
    获取评分排序的标的列表（ScoredSymbol[]）。
    Phase 0-1 阶段，此端点返回空列表（L3 尚未集成到 Web 引擎）。
    """
    engine = get_engine()
    scored = engine.get_scored_symbols()
    return [serialize_scored_symbol(s) for s in scored]
