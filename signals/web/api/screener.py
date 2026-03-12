# -*- coding: utf-8 -*-
"""Layer 3 信号列表 API"""
from fastapi import APIRouter

from ..services.engine import get_engine
from ..services.serializers import serialize_scored_symbol

router = APIRouter(prefix="/api/screener", tags=["screener"])


@router.get("/results")
def get_screener_results():
    """
    获取评分排序的标的列表（ScoredSymbol[]），含全量结果和阈值。
    """
    from config import SCORE_THRESHOLD
    engine = get_engine()
    scored = engine.get_scored_symbols()
    return {
        "threshold": SCORE_THRESHOLD,
        "results": [serialize_scored_symbol(s) for s in scored],
    }
