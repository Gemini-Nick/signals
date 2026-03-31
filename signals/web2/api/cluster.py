# -*- coding: utf-8 -*-
"""行业聚类 API — 行业板块(东财) + 概念板块(THS) 双维聚类 + 盘中定时器"""
import logging
import threading
from datetime import datetime

from fastapi import APIRouter

from signals.core.clustering import cluster_industries, cluster_concepts
from signals.core.cluster_store import save_result, load_result, load_latest, load_week

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
    global _latest_industry
    logger.info("聚类定时器启动（每 %d 分钟）", _INTERVAL // 60)
    # 先从历史缓存预加载，确保启动即有数据
    if not _latest_industry:
        stored = load_latest()
        if stored:
            _latest_industry = stored
            logger.info("从历史缓存预加载聚类数据: %s", stored.get("meta", {}).get("date"))
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
        # 尝试加载最近交易日历史
        from signals.data.mongo_fallback import get_last_trading_day
        trading_day = get_last_trading_day()
        stored = load_result(trading_day)
        if not stored:
            stored = load_latest()  # 兜底：取最近的文件
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

    # 数据过期检测：比对实际数据日期与最近交易日
    data_warning = None
    try:
        from signals.data.mongo_fallback import get_last_trading_day
        expected_day = get_last_trading_day()
        actual_day = None
        if industry and industry.get("meta", {}).get("date"):
            actual_day = industry["meta"]["date"]
        if actual_day and actual_day < expected_day:
            data_warning = f"数据日期为 {actual_day}，最近交易日为 {expected_day}，数据未更新"
    except Exception:
        pass

    result = {
        "industry": industry or {"top": [], "all_clusters": [], "meta": {"error": "行业数据加载中"}},
        "concept": concept or {"top": [], "all_clusters": [], "meta": {}},
        "market_status": market_status,
    }
    if data_warning:
        result["data_warning"] = data_warning
    return result


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


# ── 观察池 + 成分股 ─────────────────────────────────────

@router.get("/watchlist")
def get_watchlist(direction: str = "", mode: str = "belief", top: int = 30):
    """
    观察池扫描 — 扫描指定方向的成分股信号。

    :param direction: 方向名称（如"上证50"、"半导体"）
    :param mode: "belief"(信念) / "panic"(恐慌抄底)
    :param top: 最多扫描前N只
    """
    if not direction:
        return {"error": "请指定方向（direction 参数）", "results": []}

    try:
        from signals.core.signal_filter import scan_direction, results_to_dict
        results = scan_direction(direction=direction, mode=mode, top_n=top)
        return {
            "direction": direction,
            "mode": mode,
            "total": len(results),
            "grade_a": len([r for r in results if r.grade == "A"]),
            "grade_b": len([r for r in results if r.grade == "B"]),
            "grade_c": len([r for r in results if r.grade == "C"]),
            "results": results_to_dict(results),
        }
    except Exception as e:
        logger.error("观察池扫描失败: %s", e)
        return {"error": str(e), "results": []}


@router.get("/stocks")
def get_board_stocks(board: str = ""):
    """
    获取板块成分股涨跌幅列表（按需加载，不做 CZSC 分析）。

    :param board: 板块/行业/概念名称
    """
    if not board:
        return {"error": "请指定板块名（board 参数）", "stocks": []}

    try:
        from signals.layers.industry import get_industry_stocks
        symbols = get_industry_stocks(board)
        if not symbols:
            return {"board": board, "stocks": [], "error": f"未找到 {board} 的成分股"}

        # 获取实时快照（涨跌幅）
        stocks = []
        try:
            import akshare as ak
            # 尝试获取实时行情
            for sym in symbols[:50]:  # 最多50只
                code = sym.replace("SH.", "").replace("SZ.", "")
                stocks.append({
                    "symbol": sym,
                    "code": code,
                    "name": code,  # 后续可从缓存获取名称
                })
        except Exception:
            # 降级: 仅返回代码列表
            stocks = [{"symbol": s, "code": s.replace("SH.", "").replace("SZ.", ""), "name": ""} for s in symbols[:50]]

        return {
            "board": board,
            "total": len(symbols),
            "showing": len(stocks),
            "stocks": stocks,
        }
    except Exception as e:
        logger.error("获取板块成分股失败: %s", e)
        return {"error": str(e), "stocks": []}
