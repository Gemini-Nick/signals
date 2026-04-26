# -*- coding: utf-8 -*-
"""Direct public daily-bar sources for active A-share preheat."""
from __future__ import annotations

import pandas as pd
import requests

from .minute_sources import stock_to_market_symbol

_TENCENT_DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
    "Accept": "application/json,text/plain,*/*",
}


def _number(value, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(parsed) else float(parsed)


def fetch_tencent_daily(
    code: str,
    *,
    start_date: str = "",
    end_date: str = "",
    count: int = 800,
    timeout: float = 8.0,
) -> pd.DataFrame:
    symbol = stock_to_market_symbol(code)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            _TENCENT_DAILY_URL,
            params={"param": f"{symbol},day,,,{count},qfq"},
            headers=_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        session.close()

    rows = payload.get("data", {}).get(symbol, {}).get("qfqday") or payload.get("data", {}).get(symbol, {}).get("day") or []
    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        parsed.append({
            "日期": pd.to_datetime(row[0], errors="coerce"),
            "开盘": _number(row[1]),
            "收盘": _number(row[2]),
            "最高": _number(row[3]),
            "最低": _number(row[4]),
            "成交量": _number(row[5]),
            "成交额": 0,
        })
    df = pd.DataFrame(parsed)
    if df.empty:
        return df
    df = df.dropna(subset=["日期"]).sort_values("日期").reset_index(drop=True)
    if start_date:
        start = pd.to_datetime(start_date, errors="coerce")
        if pd.notna(start):
            df = df[df["日期"] >= start]
    if end_date:
        end = pd.to_datetime(end_date, errors="coerce")
        if pd.notna(end):
            df = df[df["日期"] <= end]
    return df.reset_index(drop=True)
