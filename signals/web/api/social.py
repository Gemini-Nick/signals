# -*- coding: utf-8 -*-
"""社交舆情 API — 主题发现 + 热度查询 + Dashboard 简报"""
import logging
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/social", tags=["social"])
_log = logging.getLogger("signals.web.social")


@router.get("/heat/{symbol}")
def get_social_heat(symbol: str):
    """单股社交热度详情"""
    try:
        from signals.data.social_fetcher import fetch_social_heat
        heat = fetch_social_heat(symbol)
        if not heat:
            return {"symbol": symbol, "heat": None}
        return {
            "symbol": symbol,
            "heat": {
                "heat_score": round(heat.heat_score, 1),
                "heat_grade": heat.heat_grade,
                "comment_score": heat.comment_score,
                "comment_rank": heat.comment_rank,
                "focus_index": heat.focus_index,
                "institution_pct": heat.institution_pct,
                "concepts": heat.concepts,
                "tag": heat.tag,
            },
        }
    except Exception as e:
        _log.warning("社交热度查询失败 [%s]: %s", symbol, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/discover/{theme}")
def discover_theme(theme: str):
    """主题→标的发现"""
    try:
        from signals.core.theme_discovery import discover_theme as _discover
        result = _discover(theme)
        stocks = []
        for s in result.discovered_stocks[:30]:
            stocks.append({
                "symbol": s.symbol,
                "name": s.name,
                "code": s.code,
                "change_pct": round(s.change_pct, 2),
                "price": round(s.price, 2),
                "relevance_score": round(s.relevance_score, 1),
                "heat_grade": s.heat_grade,
                "comment_score": round(s.comment_score, 1),
                "concepts": s.concepts,
            })
        return {
            "theme": result.theme,
            "matched_concepts": result.matched_concepts,
            "total_stocks": result.total_stocks,
            "sentiment_summary": result.sentiment_summary,
            "stocks": stocks,
        }
    except Exception as e:
        _log.warning("主题发现失败 [%s]: %s", theme, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/themes")
def get_hot_themes():
    """当日热门概念主题"""
    try:
        from signals.core.theme_discovery import get_hot_themes as _hot
        themes = _hot(top_n=15)
        return {
            "themes": [
                {
                    "name": t.name,
                    "code": t.code,
                    "change_pct": round(t.change_pct, 2),
                    "stock_count": t.stock_count,
                }
                for t in themes
            ],
        }
    except Exception as e:
        _log.warning("热门主题获取失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/brief")
def get_social_brief():
    """
    Dashboard 社交简报 — 一个接口返回:
    1. hot_themes: 热门概念主题 Top10
    2. surge_stocks: 飙升关注标的 Top10
    """
    result = {"hot_themes": [], "surge_stocks": []}

    # 热门主题
    try:
        from signals.core.theme_discovery import get_hot_themes
        themes = get_hot_themes(top_n=10)
        result["hot_themes"] = [
            {"name": t.name, "change_pct": round(t.change_pct, 2),
             "stock_count": t.stock_count}
            for t in themes
        ]
    except Exception as e:
        _log.warning("热门主题获取失败: %s", e)

    # 飙升标的
    try:
        from signals.core.theme_discovery import get_surge_stocks
        surges = get_surge_stocks(top_n=10)
        result["surge_stocks"] = [
            {"symbol": s["symbol"], "name": s["name"],
             "score": round(s["score"], 1),
             "focus_index": round(s["focus_index"], 1),
             "change_pct": round(s["change_pct"], 2)}
            for s in surges
        ]
    except Exception as e:
        _log.warning("飙升标的获取失败: %s", e)

    return result
