# -*- coding: utf-8 -*-
"""社交舆情 API — 主题发现 + 热度查询 + Dashboard 简报"""
import logging
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/social", tags=["social"])
_log = logging.getLogger("signals.web.social")


def _hot_themes_from_gateway(top_n: int = 10) -> list[dict]:
    """Read dashboard themes from local canonical/snapshot data only."""
    from signals.data.gateway import get_concept_rank
    from signals.data.models import DataRequest

    resp = get_concept_rank(DataRequest(
        domain="concept",
        mode="historical",
        market="A",
        freq="daily",
        purpose="review",
        allow_stale=True,
    ))
    df = resp.data
    if df is None or df.empty:
        return []
    if "change_pct" in df.columns:
        df = df.sort_values("change_pct", ascending=False)
    out = []
    for _, row in df.head(top_n).iterrows():
        out.append({
            "name": str(row.get("board_name", row.get("concept", ""))),
            "change_pct": round(float(row.get("change_pct", 0) or 0), 2),
            "stock_count": int(row.get("stock_count", row.get("成份股数量", 0)) or 0),
            "source": resp.source,
            "freshness": resp.freshness,
            "is_stale": resp.is_stale,
        })
    return out


@router.get("/heat/{symbol}")
def get_social_heat(symbol: str):
    """单股社交热度详情"""
    try:
        from signals.data.gateway import get_social_heat as gateway_get_social_heat
        from signals.data.models import DataRequest

        response = gateway_get_social_heat(DataRequest(
            domain="social",
            mode="historical",
            market="A",
            symbol=symbol,
            purpose="review",
            allow_stale=True,
        ))
        heat = response.data
        if not heat:
            return {"symbol": symbol, "heat": None, "meta": response.to_meta()}
        return {
            "symbol": symbol,
            "heat": {
                "heat_score": round(float(heat.get("heat_score", 0) or 0), 1),
                "heat_grade": heat.get("heat_grade", ""),
                "comment_score": heat.get("comment_score"),
                "comment_rank": heat.get("comment_rank"),
                "focus_index": heat.get("focus_index"),
                "institution_pct": heat.get("institution_pct"),
                "concepts": heat.get("concepts", []),
                "tag": heat.get("tag", ""),
            },
            "meta": response.to_meta(),
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
        result["hot_themes"] = _hot_themes_from_gateway(top_n=10)
    except Exception as e:
        _log.warning("热门主题获取失败: %s", e)

    # 飙升标的
    # Avoid live external calls here; the dashboard should remain available when
    # Eastmoney/Sina are slow or blocked. A later gateway social cache can fill
    # this from canonical snapshots.

    return result
