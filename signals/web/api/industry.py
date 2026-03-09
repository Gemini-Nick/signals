# -*- coding: utf-8 -*-
"""Layer 2 行业分析 API"""
from fastapi import APIRouter

from ..services.engine import get_engine

router = APIRouter(prefix="/api/industry", tags=["industry"])


def _serialize_industry(ind) -> dict:
    """IndustryRanking → JSON"""
    return {
        "name": ind.name,
        "display_name": ind.display_name,
        "gain_rank": ind.gain_rank,
        "composite_rank": ind.composite_rank,
        "gain_pct": round(ind.gain_pct, 2),
        "net_inflow": round(ind.net_inflow, 2),
        "composite_score": round(ind.composite_score, 1),
        "source": ind.source,
        "sector_type": ind.sector_type,
        "rotation_line": ind.rotation_line,
        "oversold_score": round(ind.oversold_score, 1),
        "oversold_detail": ind.oversold_detail,
        "zt_count": ind.zt_count,
        "strong_count": ind.strong_count,
        "concept_tags": ind.concept_tags[:5],
        "candidate_count": len(ind.candidates),
        "candidates": [
            {"code": c.code, "name": c.name, "role": c.role,
             "priority": c.priority, "detail": c.detail}
            for c in ind.candidates[:10]
        ],
    }


def _serialize_concept(con) -> dict:
    """ConceptRanking → JSON"""
    return {
        "name": getattr(con, "name", ""),
        "gain_pct": round(getattr(con, "gain_pct", 0), 2),
        "tag": getattr(con, "tag", ""),
        "leading_stock": getattr(con, "leading_stock", ""),
        "leading_gain": round(getattr(con, "leading_gain", 0), 2),
        "sector_type": getattr(con, "sector_type", ""),
    }


@router.get("/ranking")
def get_industry_ranking():
    """
    获取行业全景排行 + 概念排行 + 超跌行业 + 统计摘要。
    """
    engine = get_engine()
    data = engine.get_industry_data()
    l2_stats = engine.get_l2_stats()

    # 统计摘要
    stats = {
        "zt_total": l2_stats.get("zt_total", 0),
        "dt_total": l2_stats.get("dt_total", 0),
        "lianban_max": l2_stats.get("lianban_max", 0),
        "red_pct": 0,
    }
    # 计算红盘行业比例
    name_df = l2_stats.get("name_df")
    if name_df is not None and not name_df.empty:
        try:
            import pandas as pd
            change_col = None
            for col in ['涨跌幅', '涨跌幅(%)', '涨幅', '涨幅(%)', '最新涨跌幅']:
                if col in name_df.columns:
                    change_col = col
                    break
            if change_col:
                vals = pd.to_numeric(name_df[change_col], errors='coerce')
                total = vals.count()
                red = (vals > 0).sum()
                stats["red_pct"] = round(red / total * 100, 0) if total > 0 else 0
        except Exception:
            pass

    return {
        "gain_list": [_serialize_industry(i) for i in data["gain_list"]],
        "composite_list": [_serialize_industry(i) for i in data["composite_list"]],
        "concepts": [_serialize_concept(c) for c in data["concepts"][:10]],
        "oversold_list": [_serialize_industry(i) for i in data["oversold_list"]],
        "stats": stats,
    }
