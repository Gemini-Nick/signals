# -*- coding: utf-8 -*-
"""Layer 2 行业分析 API"""
import logging
from fastapi import APIRouter, HTTPException

from ..services.engine import get_engine
from ..services.serializers import (
    serialize_bars, serialize_bi_list, serialize_fx_list,
    serialize_zhongshu, serialize_signals,
    compute_ma_lines, compute_macd,
)

logger = logging.getLogger(__name__)

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
        "phase": ind.rhythm_phase,
        "phase_hint": ind.rhythm_hint,
        "candidate_count": len(ind.candidates),
        "candidates": [
            {"code": c.code, "name": c.name, "role": c.role,
             "priority": c.priority, "detail": c.detail}
            for c in ind.candidates[:10]
        ],
    }



@router.get("/ranking")
def get_industry_ranking():
    """
    获取行业全景排行 + 概念排行 + 超跌行业 + 统计摘要。
    L2 未完成时返回 loading 状态。
    """
    engine = get_engine()
    status = engine.get_status()
    if status.get("loading_phase") in ("L1", "L2"):
        return {"loading": True, "phase": status.get("loading_phase", "L2")}

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
        "oversold_list": [_serialize_industry(i) for i in data["oversold_list"]],
        "stats": stats,
    }


@router.get("/concept-ranking")
def get_concept_ranking():
    """
    获取概念板块综合排行（过滤噪音、多维评分、关联行业）。
    L2 未完成时返回 loading 状态。
    """
    engine = get_engine()
    status = engine.get_status()
    if status.get("loading_phase") in ("L1", "L2"):
        return {"loading": True, "phase": status.get("loading_phase", "L2")}

    concepts = engine.get_concepts()
    concept_list = []
    for c in concepts:
        total = c.up_count + c.down_count
        up_ratio = round(c.up_count / total * 100, 1) if total > 0 else 0
        concept_list.append({
            "name": c.name,
            "code": c.code,
            "composite_score": round(c.composite_score, 1),
            "gain_pct": round(c.gain_pct, 2),
            "up_count": c.up_count,
            "down_count": c.down_count,
            "up_ratio": up_ratio,
            "turnover_rate": round(c.turnover_rate, 2),
            "leading_stock": c.leading_stock,
            "leading_gain": round(c.leading_gain, 2),
            "sector_type": c.sector_type,
            "related_industries": c.related_industries,
        })
    return {"concept_list": concept_list}


@router.get("/detail/{name}")
def get_industry_detail(name: str):
    """
    获取行业 CZSC 分析数据（K 线 + 笔 + 中枢 + 信号 + MA + MACD）。
    复用 IndexAnalyzer 进行缠论分析。
    """
    from signals.layers.industry import get_industry_bars
    from signals.layers.index_analyzer import IndexAnalyzer
    from signals.core.detectors import detect_all_signals
    import config as _cfg

    # 获取行业 K 线
    bars = get_industry_bars(name, lookback_days=180)
    if not bars:
        raise HTTPException(status_code=404, detail=f"无法获取 {name} K线数据")

    # CZSC 分析
    az = IndexAnalyzer(name=name, symbol=name, daily_bars=bars)
    daily = az._daily

    ohlcv = serialize_bars(daily)
    bi_list = serialize_bi_list(daily)
    fx_list = serialize_fx_list(daily)
    zhongshu = serialize_zhongshu(daily)

    # 信号检测
    signals_data = []
    try:
        detected = detect_all_signals(daily.czsc, name)
        signals_data = serialize_signals(detected)
    except Exception as e:
        logger.warning("行业信号检测失败 %s: %s", name, e)

    # MA + MACD
    ma_lines = []
    macd_data = []
    try:
        ma_lines = compute_ma_lines(daily.bars_raw)
    except Exception as e:
        logger.warning("行业MA计算失败 %s: %s", name, e)
    try:
        macd_data = compute_macd(daily.bars_raw)
    except Exception as e:
        logger.warning("行业MACD计算失败 %s: %s", name, e)

    # 行业属性信息
    engine = get_engine()
    ranking = engine.get_industry_ranking_by_name(name)
    industry_info = {}
    if ranking:
        industry_info = {
            "rotation_line": ranking.rotation_line,
            "sector_type": ranking.sector_type,
            "gain_pct": round(ranking.gain_pct, 2),
            "composite_score": round(ranking.composite_score, 1),
            "zt_count": ranking.zt_count,
            "phase": ranking.rhythm_phase,
            "phase_hint": ranking.rhythm_hint,
            "concept_tags": ranking.concept_tags[:5],
        }
    else:
        # 无排行数据时从配置获取基本信息
        rotation_map = getattr(_cfg, "ROTATION_LINE_MAP", {})
        sector_map = getattr(_cfg, "SECTOR_TYPE_MAP", {})
        industry_info = {
            "rotation_line": rotation_map.get(name, ""),
            "sector_type": sector_map.get(name, "中性"),
        }

    # 关联行业（同轮动线）
    related = []
    rot_line = industry_info.get("rotation_line", "")
    if rot_line:
        rotation_map = getattr(_cfg, "ROTATION_LINE_MAP", {})
        for ind, line in rotation_map.items():
            if line == rot_line and ind != name:
                related.append(ind)

    # CZSC 报告摘要
    report = az.report()
    report_summary = {
        "daily_trend": report.daily_trend,
        "daily_latest_signal": report.daily_latest_signal,
        "daily_bi_count": report.daily_bi_count,
        "is_bullish": report.is_bullish,
        "has_buy_signal": report.has_buy_signal,
        "has_sell_signal": report.has_sell_signal,
    }

    return {
        "ohlcv": ohlcv,
        "bi_list": bi_list,
        "fx_list": fx_list,
        "zhongshu": zhongshu,
        "signals": signals_data,
        "ma_lines": ma_lines,
        "macd": macd_data,
        "industry_info": industry_info,
        "related_industries": related[:8],
        "report": report_summary,
        "meta": {
            "name": name,
            "type": "industry",
            "freq": "daily",
        },
    }


@router.get("/resolve/{query}")
def resolve_sector(query: str):
    """
    智能板块解析：自然语言 → 行业列表 + 概念列表。
    如 "电气新能源" → 光伏设备/风电设备/电网设备/电机/其他电源设备 + 新能源车概念等
    """
    engine = get_engine()
    return engine.resolve_sector(query)
