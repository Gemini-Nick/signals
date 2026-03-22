# -*- coding: utf-8 -*-
"""行业聚类 API — 行业板块(东财) + 概念板块(THS) 双维聚类 + 盘中定时器"""
import logging
import threading
from datetime import datetime

from fastapi import APIRouter

from signals.core.clustering import cluster_industries, cluster_concepts
from signals.core.cluster_store import save_result, load_result, load_week

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cluster", tags=["cluster"])

# ── 内存缓存 ───────────────────────────────────────────────
_latest_industry: dict | None = None
_latest_concept: dict | None = None
_timer: threading.Timer | None = None
_INTERVAL = 30 * 60  # 30 分钟


def _run_cluster():
    """执行行业+概念双聚类并存储。"""
    global _latest_industry, _latest_concept

    # 行业板块聚类（东财 → CSV 缓存）
    try:
        result = cluster_industries()
        if result.get("top"):
            _latest_industry = result
            save_result(result)
            logger.info("行业聚类完成: %d 簇, Top1=%s",
                        result["meta"]["valid_clusters"],
                        result["top"][0]["label"] if result["top"] else "N/A")
    except Exception as e:
        logger.error("行业聚类失败: %s", e)

    # 概念板块聚类（新浪/东财/THS 降级链）
    try:
        concept = cluster_concepts()
        if concept.get("top"):
            _latest_concept = concept
            logger.info("概念聚类完成: %d 簇, Top1=%s",
                        concept["meta"]["valid_clusters"],
                        concept["top"][0]["label"] if concept["top"] else "N/A")
    except Exception as e:
        logger.error("概念聚类失败: %s", e)


def _schedule_next():
    """调度下一次聚类（盘中 9:30-15:00 工作日）。"""
    global _timer
    try:
        from signals.core.market_hours import get_session_mode
        session = get_session_mode()
        if session.a_live:
            _run_cluster()
    except Exception as e:
        logger.error("定时聚类异常: %s", e)
    # 无论成功与否，30 分钟后再检查
    _timer = threading.Timer(_INTERVAL, _schedule_next)
    _timer.daemon = True
    _timer.start()


def start_scheduler():
    """启动盘中定时聚类（由 app lifespan 调用）。"""
    logger.info("聚类定时器启动（每 %d 分钟）", _INTERVAL // 60)
    # 首次在后台线程执行（不阻塞 app 启动）
    t = threading.Thread(target=_run_cluster, daemon=True)
    t.start()
    # 调度后续
    global _timer
    _timer = threading.Timer(_INTERVAL, _schedule_next)
    _timer.daemon = True
    _timer.start()


def stop_scheduler():
    """停止定时器。"""
    global _timer
    if _timer:
        _timer.cancel()
        _timer = None


# ── 路由 ───────────────────────────────────────────────────

@router.get("/latest")
def get_latest(top: int = 3):
    """获取最新聚类结果（行业 + 概念双维度）。"""
    global _latest_industry

    industry = None
    # 内存缓存
    if _latest_industry:
        industry = _latest_industry.copy()
        industry["top"] = industry["all_clusters"][:top]
    else:
        # 尝试加载今日历史
        today = datetime.now().strftime("%Y-%m-%d")
        stored = load_result(today)
        if stored:
            _latest_industry = stored
            industry = stored.copy()
            industry["top"] = industry["all_clusters"][:top]

    # 概念聚类
    concept = None
    if _latest_concept:
        concept = _latest_concept.copy()
        concept["top"] = concept["all_clusters"][:top]

    if not industry and not concept:
        return {
            "industry": {"top": [], "all_clusters": [], "meta": {"error": "暂无数据，请等待或手动刷新"}},
            "concept": {"top": [], "all_clusters": [], "meta": {}},
        }

    # 市场状态（精细到盘前/午休/盘后/期货）
    try:
        from signals.core.market_hours import get_session_mode, get_market_detail
        session = get_session_mode()
        detail = get_market_detail()
        market_status = {
            "session_name": session.name,
            "session_label": session.label,
            "a_live": session.a_live,
            "hk_live": session.hk_live,
            "us_live": session.us_live,
            "markets": detail,  # 每个市场精细状态
        }
    except Exception:
        market_status = {"session_label": "未知", "a_live": False, "markets": {}}

    return {
        "industry": industry or {"top": [], "all_clusters": [], "meta": {"error": "行业数据加载中"}},
        "concept": concept or {"top": [], "all_clusters": [], "meta": {}},
        "market_status": market_status,
    }


@router.get("/history")
def get_history(date: str = ""):
    """获取历史聚类结果（默认本周）。"""
    if date:
        result = load_result(date)
        if result:
            return {"date": date, "result": result}
        return {"date": date, "result": None, "error": f"无 {date} 数据"}
    # 默认本周
    week = load_week()
    return {"week": week, "count": len(week)}


@router.get("/refresh")
def refresh(top: int = 3):
    """手动触发聚类刷新。"""
    _run_cluster()
    return get_latest(top)
