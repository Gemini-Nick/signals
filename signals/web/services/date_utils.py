# -*- coding: utf-8 -*-
"""
日期解析工具 — 从 run.py 提取的可复用函数
"""
from datetime import datetime, timedelta

import config


def resolve_start_date(raw: str) -> str:
    """将日期别名解析为 'YYYY-MM-DD' 格式。"""
    preset = config.DATE_PRESETS.get(raw.lower())
    if preset:
        if "date" in preset:
            return preset["date"]
        offset = preset["offset"]
        if offset == "ytd":
            return f"{datetime.now().year}-01-01"
        return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def get_date_label(raw: str) -> str:
    """获取日期的标签说明。"""
    preset = config.DATE_PRESETS.get(raw.lower())
    if preset:
        return preset["label"]
    return ""


def get_all_presets() -> list:
    """返回所有日期预设（供前端下拉菜单）。"""
    result = []
    for key, info in config.DATE_PRESETS.items():
        if "date" in info:
            date_str = info["date"]
        elif info["offset"] == "ytd":
            date_str = f"{datetime.now().year}-01-01"
        else:
            date_str = f"T-{info['offset']}天"
        result.append({
            "key": key,
            "date": date_str,
            "label": info["label"],
        })
    return result
