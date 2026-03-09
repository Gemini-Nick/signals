# -*- coding: utf-8 -*-
"""Layer 1 指数报告 API"""
from fastapi import APIRouter, HTTPException

from ..services.engine import get_engine
from ..services.serializers import serialize_index_report, serialize_market_context

router = APIRouter(prefix="/api/index", tags=["index"])


@router.get("/context")
def get_market_context():
    """大盘方向 / 情绪周期 / 仓位建议"""
    engine = get_engine()
    ctx = engine.get_market_context()
    if ctx is None:
        raise HTTPException(status_code=503, detail="分析尚未完成")
    return serialize_market_context(ctx)


@router.get("/reports")
def get_index_reports():
    """全部指数 IndexReport 列表"""
    engine = get_engine()
    reports = engine.get_index_reports()
    if not reports:
        raise HTTPException(status_code=503, detail="分析尚未完成")
    return [serialize_index_report(r) for r in reports]


@router.get("/summary")
def get_action_summary():
    """操作建议（道长策略）— 买卖机会 + 恐慌抄底 + 行业研判 + 主题追踪"""
    engine = get_engine()
    if not engine.is_ready():
        raise HTTPException(status_code=503, detail="分析尚未完成")
    return engine.get_action_summary()


@router.get("/brief")
def get_decision_brief():
    """P3-7: 决策简报 — 整合情景分叉 + 风格切换 + 轮动 + 节奏 + 历史匹配"""
    engine = get_engine()
    if not engine.is_ready():
        raise HTTPException(status_code=503, detail="分析尚未完成")
    brief = engine.get_decision_brief()
    if brief is None:
        raise HTTPException(status_code=503, detail="决策简报生成失败")
    return brief


@router.get("/status")
def get_engine_status():
    """引擎运行状态"""
    return get_engine().get_status()
