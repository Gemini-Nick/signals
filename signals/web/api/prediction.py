# -*- coding: utf-8 -*-
"""预测总览 API — 行业买卖预测 + 个股买卖预测 + 市场环境"""
import logging
from fastapi import APIRouter

from ..services.engine import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prediction", tags=["prediction"])


def _serialize_stock_momentum(m) -> dict:
    return {
        "code": m.code,
        "name": m.name,
        "momentum_days": m.momentum_days,
        "daily_avg_gain": round(m.daily_avg_gain, 2),
        "bullish_ratio": round(m.bullish_ratio, 2),
        "volume_expanding": m.volume_expanding,
        "momentum_score": round(m.momentum_score, 1),
        "change_pct": round(m.change_pct, 2),
    }


def _serialize_sector_momentum(sig) -> dict:
    return {
        "concept_name": sig.concept_name,
        "concept_code": sig.concept_code,
        "total_stocks": sig.total_stocks,
        "momentum_stock_count": sig.momentum_stock_count,
        "momentum_ratio": round(sig.momentum_ratio, 3),
        "avg_momentum": round(sig.avg_momentum, 1),
        "momentum_score": round(sig.momentum_score, 1),
        "signal_level": sig.signal_level,
        "bearish_ratio": round(sig.bearish_ratio, 3),
        "top_movers": [_serialize_stock_momentum(m) for m in (sig.top_movers or [])],
        "detail": sig.detail,
    }


def _serialize_stock_prediction(scored) -> dict:
    """ScoredSymbol → 预测序列化"""
    result = {
        "symbol": scored.symbol,
        "name": getattr(scored, "name", ""),
        "total_score": round(scored.total_score, 1),
        "fused_total": round(scored.fused_total, 1) if scored.fused_total else 0,
        "direction": scored.direction,
    }
    # 动力学数据
    dynamics = getattr(scored, "dynamics_profile", None)
    if dynamics:
        result["dynamics"] = {
            "freq": dynamics.freq,
            "signal": dynamics.signal,
            "dynamics_score": round(dynamics.dynamics_score, 1),
            "power_trend": dynamics.power_trend,
            "power_ratio": round(dynamics.power_ratio, 3),
            "ubi_momentum": dynamics.ubi_momentum,
            "ubi_bar_count": dynamics.ubi_bar_count,
            "consecutive_bullish": dynamics.consecutive_bullish,
            "volume_expanding": dynamics.volume_expanding,
            "fake_positive": dynamics.fake_positive,
            "detail": dynamics.detail,
        }
    result["dynamics_merged_score"] = round(
        getattr(scored, "dynamics_merged_score", 0) or 0, 1
    )
    # 卖点预警
    sell_warning = getattr(scored, "sell_warning", {})
    if sell_warning and sell_warning.get("score", 0) > 0:
        result["sell_warning"] = sell_warning
    # 融合明细
    fused = getattr(scored, "fused_score", None)
    if fused:
        result["fusion_detail"] = {
            "dynamics_boost": round(fused.dynamics_boost, 1),
            "sector_momentum": round(fused.sector_momentum, 1),
            "anomaly_boost": round(fused.anomaly_boost, 1),
            "regime_mult": fused.regime_mult,
            "confidence": fused.confidence_level,
            "detail": fused.detail,
        }
    return result


@router.get("/overview")
def get_prediction_overview():
    """
    预测首页数据:
    - sector_buy: 板块买入预测 (动量强的板块)
    - sector_sell: 板块卖出预警 (bearish_ratio 高的板块)
    - stock_buy: 个股买入预测 (dynamics_merged_score 高)
    - stock_sell: 个股卖出预警 (sell_warning_score 高)
    - market_regime: 市场环境指标
    """
    engine = get_engine()
    status = engine.get_status()

    if status.get("loading_phase") in ("L1", "L2"):
        return {"loading": True, "phase": status.get("loading_phase")}

    # ── 板块动量 → 买入/卖出预测 ──
    momentum = engine.get_momentum_signals()
    sector_buy = []
    sector_sell = []
    for sig in momentum:
        item = _serialize_sector_momentum(sig)
        if sig.signal_level in ("强", "中"):
            sector_buy.append(item)
        if sig.bearish_ratio >= 0.3:
            item["sell_signal"] = f"分化{sig.bearish_ratio:.0%}"
            sector_sell.append(item)

    # ── 个股预测 → 买入/卖出 ──
    scored_list = engine.get_scored_symbols()
    stock_buy = []
    stock_sell = []
    for scored in scored_list[:30]:
        item = _serialize_stock_prediction(scored)
        merged = getattr(scored, "dynamics_merged_score", 0) or 0
        sell_w = getattr(scored, "sell_warning", {}) or {}

        if merged > 20 or (item.get("fused_total", 0) > 50):
            stock_buy.append(item)
        if sell_w.get("score", 0) > 40:
            stock_sell.append(item)

    # 买入按 dynamics_merged_score 降序
    stock_buy.sort(key=lambda x: x.get("dynamics_merged_score", 0), reverse=True)
    # 卖出按 sell_warning_score 降序
    stock_sell.sort(
        key=lambda x: (x.get("sell_warning") or {}).get("score", 0), reverse=True
    )

    # ── 市场环境 ──
    l2_stats = engine.get_l2_stats()
    market_ctx = engine.get_market_context()
    from signals.core.fusion import _calc_market_regime_mult
    regime_mult = _calc_market_regime_mult(l2_stats, market_ctx)
    zt = l2_stats.get("zt_total", 0)
    dt = l2_stats.get("dt_total", 0)
    lianban = l2_stats.get("lianban_max", 0)

    if regime_mult > 0.8:
        regime_label = "偏增量"
    elif regime_mult > 0.5:
        regime_label = "中性"
    else:
        regime_label = "存量市"

    return {
        "loading": False,
        "sector_buy": sector_buy[:10],
        "sector_sell": sector_sell[:5],
        "stock_buy": stock_buy[:15],
        "stock_sell": stock_sell[:10],
        "market_regime": {
            "zt_total": zt,
            "dt_total": dt,
            "lianban_max": lianban,
            "regime_mult": regime_mult,
            "label": regime_label,
        },
    }
