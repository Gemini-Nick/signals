# -*- coding: utf-8 -*-
"""个股深度分析 API"""
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/stock", tags=["stock"])


def _normalize_stock_symbol(symbol: str) -> str:
    clean = str(symbol or "").strip()
    if not clean:
        return ""
    upper = clean.upper()
    if "." in upper:
        market, code = upper.split(".", 1)
        if market in {"SZ", "SH", "HK", "BJ", "US"} and code:
            return f"{market}.{code}"
    if clean.isdigit() and len(clean) == 6:
        if clean.startswith("6"):
            return f"SH.{clean}"
        if clean.startswith(("0", "3")):
            return f"SZ.{clean}"
        if clean.startswith(("8", "4")):
            return f"BJ.{clean}"
    return upper


@router.get("/resolve/{symbol}")
def resolve_stock_name(symbol: str):
    """轻量解析股票代码/名称，不触发 K 线或 CZSC 分析。"""
    try:
        from signals.core.stock_names import get_resolver
        resolver = get_resolver()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"名称解析器不可用: {e}")

    query = str(symbol or "").strip()
    normalized = _normalize_stock_symbol(query)
    resolved_symbol = normalized
    if not resolved_symbol or (not query.isdigit() and "." not in query and not normalized.startswith(("SZ.", "SH.", "BJ.", "HK.", "US."))):
        resolved_symbol = resolver.get_code(query) or normalized

    name = resolver.get_name(resolved_symbol) if resolved_symbol else ""
    industry = resolver.get_industry(resolved_symbol) if resolved_symbol else ""
    code = resolved_symbol.split(".")[-1] if resolved_symbol else query
    return {
        "query": query,
        "symbol": resolved_symbol,
        "code": code,
        "name": name if name and name != code else "",
        "industry": industry,
        "matches": [
            {"symbol": code_item, "code": code_item.split(".")[-1], "name": name_item}
            for code_item, name_item in resolver.search(query)[:8]
        ],
    }


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
             "position": lv.position, "distance_pct": round(lv.distance_pct, 2),
             "timeframe": getattr(lv, "timeframe", ""),
             "period": getattr(lv, "period", 0),
             "direction": getattr(lv, "direction", ""),
             "role": getattr(lv, "role", "")}
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
            from signals.data.gateway import get_social_heat
            from signals.data.models import DataRequest

            social_resp = get_social_heat(DataRequest(
                domain="social",
                mode="historical",
                market="A",
                symbol=clean,
                purpose="review",
                allow_stale=True,
            ))
            soc = social_resp.data
            resolved_name = result.get("name", "")
            if not soc and resolved_name and clean != resolved_name:
                social_resp = get_social_heat(DataRequest(
                    domain="social",
                    mode="historical",
                    market="A",
                    symbol=resolved_name,
                    purpose="review",
                    allow_stale=True,
                ))
                soc = social_resp.data
            if soc:
                result["social"] = {
                    "heat_score": round(float(soc.get("heat_score", 0) or 0), 1),
                    "heat_grade": soc.get("heat_grade", ""),
                    "comment_score": soc.get("comment_score"),
                    "comment_rank": soc.get("comment_rank"),
                    "focus_index": soc.get("focus_index"),
                    "institution_pct": soc.get("institution_pct"),
                    "concepts": soc.get("concepts", []),
                    "tag": soc.get("tag", ""),
                    "meta": social_resp.to_meta(),
                }
            else:
                result["social"] = {"meta": social_resp.to_meta()}
        except Exception:
            result["social"] = None

        # 关键高低点
        result["pivots"] = [
            {"price": round(p.price, 2), "type": p.pivot_type,
             "role": p.role, "significance": round(p.significance, 1),
             "dt": p.dt.strftime("%Y-%m-%d") if p.dt else ""}
            for p in dive.pivots[:8]
        ]

        # 笔动力学（多级别）
        try:
            from signals.core.bi_dynamics import (
                analyze_multi_freq_dynamics, merge_dynamics_score,
                get_best_sell_warning,
            )
            dynamics_profiles = getattr(dive, "freq_analyzers", {}) or {}
            if dynamics_profiles:
                profiles = analyze_multi_freq_dynamics(dynamics_profiles)
                merged_score = merge_dynamics_score(profiles)
                sell_warning = get_best_sell_warning(profiles)
                result["bi_dynamics"] = {
                    "merged_score": round(merged_score, 1),
                    "sell_warning": sell_warning,
                    "levels": {
                        freq: {
                            "signal": p.signal,
                            "dynamics_score": round(p.dynamics_score, 1),
                            "power_trend": p.power_trend,
                            "power_ratio": round(p.power_ratio, 3),
                            "ubi_momentum": p.ubi_momentum,
                            "ubi_bar_count": p.ubi_bar_count,
                            "consecutive_bullish": p.consecutive_bullish,
                            "volume_expanding": p.volume_expanding,
                            "fake_positive": p.fake_positive,
                            "detail": p.detail,
                        }
                        for freq, p in profiles.items()
                    },
                }
            else:
                result["bi_dynamics"] = None
        except Exception:
            result["bi_dynamics"] = None

        # 融合评分补充动力学/板块字段
        if result.get("fused") and result["bi_dynamics"]:
            result["fused"]["dynamics_boost"] = round(
                getattr(fused, "dynamics_boost", 0), 1
            )
            result["fused"]["sector_momentum"] = round(
                getattr(fused, "sector_momentum", 0), 1
            )
            result["fused"]["regime_mult"] = getattr(fused, "regime_mult", 1.0)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"序列化失败: {e}")
