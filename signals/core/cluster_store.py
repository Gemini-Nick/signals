# -*- coding: utf-8 -*-
"""聚类结果历史存储 — JSON 文件按日归档，支持按周回顾"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from signals.core.trading_dates import is_trading_day, trading_day_key

_HISTORY_DIR = Path(".data/cache/cluster_history")


def save_result(result: dict):
    """存储当日聚类结果。"""
    date = result.get("meta", {}).get("date") or trading_day_key("A")
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = _HISTORY_DIR / f"{date}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)


def load_result(date: str) -> dict | None:
    """加载指定日期的聚类结果。"""
    path = _HISTORY_DIR / f"{date}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_week(ref_date: str = None) -> list:
    """
    加载本周一到 ref_date（默认今天）的所有聚类结果。
    返回 [{date, result}, ...] 按日期排序。
    """
    if ref_date:
        end = datetime.strptime(ref_date, "%Y-%m-%d")
    else:
        end = datetime.strptime(trading_day_key("A"), "%Y-%m-%d")
    # 本周一
    monday = end - timedelta(days=end.weekday())

    results = []
    d = monday
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        r = load_result(ds)
        if r:
            results.append({"date": ds, "result": r})
        d += timedelta(days=1)
    return results


def load_latest() -> dict | None:
    """加载最近一次的聚类结果（按文件名日期排序）。"""
    if not _HISTORY_DIR.exists():
        return None
    files = [
        path for path in sorted(_HISTORY_DIR.glob("*.json"), reverse=True)
        if _is_trading_file(path)
    ]
    if not files:
        return None
    with open(files[0], "r", encoding="utf-8") as f:
        return json.load(f)


def _is_trading_file(path: Path) -> bool:
    try:
        if path.stem[:8].isdigit() and "-" not in path.stem[:10]:
            day = datetime.strptime(path.stem[:8], "%Y%m%d").date()
        else:
            day = datetime.strptime(path.stem[:10], "%Y-%m-%d").date()
        return is_trading_day("A", day)
    except Exception:
        return False


def cleanup(keep_days: int = 30):
    """清理超过 keep_days 天的历史文件。"""
    if not _HISTORY_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    for f in _HISTORY_DIR.glob("*.json"):
        try:
            fdate = datetime.strptime(f.stem, "%Y-%m-%d")
            if fdate < cutoff:
                f.unlink()
        except ValueError:
            pass
