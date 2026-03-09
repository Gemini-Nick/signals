# -*- coding: utf-8 -*-
"""个股深度分析 API"""
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/stock", tags=["stock"])


def _serialize_tf(tf) -> dict:
    """TimeframeAnalysis → JSON"""
    return {
        "freq": tf.freq,
        "bi_count": tf.bi_count,
        "last_direction": tf.last_direction,
        "trend": tf.trend,
        "signal_count": len(tf.signals),
        "signals": [
            {"signal_type": s.signal_type, "freq": s.freq,
             "confidence": round(s.confidence, 1),
             "description": getattr(s, "description", "")}
            for s in tf.signals
        ],
        "zs_range": list(tf.zs_range) if tf.zs_range else None,
        "latest_bis": [
            {"sdt": b[0], "edt": b[1], "dir": b[2],
             "low": round(b[3], 2), "high": round(b[4], 2),
             "power": round(b[5], 2)}
            for b in tf.latest_bis
        ],
    }


def _serialize_volume(vol) -> dict:
    """VolumeProfile → JSON"""
    if not vol:
        return {}
    return {
        "trend": vol.trend,
        "ratio": round(vol.ratio, 2),
        "price_vol_match": vol.price_vol_match,
        "detail": vol.detail,
    }


def _serialize_scenario(sc) -> dict:
    """Scenario → JSON"""
    return {
        "name": sc.name,
        "trigger": sc.trigger,
        "probability_hint": sc.probability_hint,
        "action": sc.action,
        "target_prices": [round(p, 2) for p in sc.target_prices],
        "rationale": sc.rationale,
    }


def _serialize_ma(ma) -> dict:
    """MAContext → JSON"""
    if not ma:
        return {}
    return {
        "trend_summary": ma.trend_summary,
        "key_levels": [
            {"name": lv.name, "value": round(lv.value, 2),
             "position": lv.position, "distance_pct": round(lv.distance_pct, 2)}
            for lv in (ma.key_levels or [])
        ],
    }


@router.get("/analyze/{symbol}")
def analyze_stock(symbol: str):
    """
    深度分析单只股票。
    symbol: A股代码（如 SZ.002759 或 600519）
    """
    # 规范化代码格式
    clean = symbol.strip()
    if clean.isdigit() and len(clean) == 6:
        if clean.startswith("6"):
            clean = f"SH.{clean}"
        elif clean.startswith(("0", "3")):
            clean = f"SZ.{clean}"
        elif clean.startswith(("8", "4")):
            clean = f"BJ.{clean}"

    try:
        from signals.layers.stock_deep_dive import StockDeepDive
        dive = StockDeepDive(clean)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析失败: {e}")

    try:
        result = {"symbol": clean, "errors": dive._errors}

        # 评分
        if dive.scored:
            s = dive.scored
            result["scored"] = {
                "total_score": round(s.total_score, 1),
                "direction": s.direction,
                "signal_count": s.signal_count,
                "ma_confirmation": getattr(s, "ma_confirmation", ""),
            }

        # MA 均线
        result["ma_context"] = _serialize_ma(dive.ma_context)

        # 量价
        result["volume"] = _serialize_volume(dive.volume)

        # 多级别分析
        result["tf_analyses"] = {
            k: _serialize_tf(v) for k, v in dive.tf_analyses.items()
        }

        # 完全分类
        result["scenarios"] = [_serialize_scenario(sc) for sc in dive.scenarios]

        # 风控
        risk = {}
        if dive.risk_info:
            ri = dive.risk_info
            risk["stop_loss"] = round(ri.stop_loss, 2)
            risk["risk_reward"] = round(ri.risk_reward, 2) if ri.risk_reward else None
            risk["position_pct"] = round(ri.position_pct, 1) if ri.position_pct else None
            risk["description"] = getattr(ri, "description", "")
        result["risk"] = risk

        # 分层仓位
        layered = {}
        if dive.layered_pos:
            lp = dive.layered_pos
            layered["base_pct"] = round(lp.base_pct, 1)
            layered["flex_pct"] = round(lp.flex_pct, 1)
            layered["flex_buy_ref"] = lp.flex_buy_ref
            layered["flex_sell_ref"] = lp.flex_sell_ref
            layered["rationale"] = lp.rationale
        result["layered_position"] = layered

        # 关键高低点
        result["pivots"] = [
            {"price": round(p.price, 2), "type": p.pivot_type,
             "role": p.role, "significance": round(p.significance, 1),
             "dt": p.dt.strftime("%Y-%m-%d") if p.dt else ""}
            for p in dive.pivots[:8]
        ]

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"序列化失败: {e}")
