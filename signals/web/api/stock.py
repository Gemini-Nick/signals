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
        from signals.core.stock_names import get_resolver
        resolver = get_resolver()
        result = {"symbol": clean, "name": resolver.get_name(clean), "errors": dive._errors}

        # 评分
        if dive.scored:
            s = dive.scored
            result["scored"] = {
                "total_score": round(s.total_score, 1),
                "fused_total": round(s.fused_total, 1) if s.fused_total else None,
                "direction": s.direction,
                "signal_count": s.signal_count,
                "ma_confirmation": getattr(s, "ma_confirmation", ""),
                "confidence_level": getattr(s.fused_score, "confidence_level", None) if s.fused_score else None,
            }

        # 异常检测
        anomaly = getattr(dive, "anomaly", None)
        if anomaly:
            result["anomaly"] = {
                "items": [
                    {"name": item.name, "z_score": round(item.z_score, 2),
                     "raw_value": round(item.raw_value, 4),
                     "is_anomaly": item.is_anomaly, "label": item.label}
                    for item in anomaly.items.values()
                ],
                "anomaly_count": anomaly.anomaly_count,
                "convergence": anomaly.convergence,
                "capitulation_score": round(anomaly.capitulation_score, 1),
                "capitulation_detail": anomaly.capitulation_detail,
                "summary": anomaly.summary,
            }
        else:
            result["anomaly"] = None

        # 融合评分明细
        fused = getattr(dive, "fused", None)
        if fused:
            result["fused"] = {
                "raw_czsc_score": round(fused.raw_czsc_score, 1),
                "anomaly_boost": round(fused.anomaly_boost, 1),
                "convergence_bonus": round(fused.convergence_bonus, 1),
                "capitulation_bonus": round(fused.capitulation_bonus, 1),
                "fused_total": round(fused.fused_total, 1),
                "dimension_count": fused.dimension_count,
                "confidence_level": fused.confidence_level,
                "detail": fused.detail,
            }
        else:
            result["fused"] = None

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

        # 社交热度
        try:
            from signals.data.social_fetcher import fetch_social_heat
            soc = fetch_social_heat(clean)
            if soc:
                result["social"] = {
                    "heat_score": round(soc.heat_score, 1),
                    "heat_grade": soc.heat_grade,
                    "comment_score": round(soc.comment_score, 1),
                    "comment_rank": soc.comment_rank,
                    "focus_index": round(soc.focus_index, 1),
                    "institution_pct": round(soc.institution_pct, 3),
                    "concepts": soc.concepts,
                    "tag": soc.tag,
                }
            else:
                result["social"] = None
        except Exception:
            result["social"] = None

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
