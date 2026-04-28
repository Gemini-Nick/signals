# -*- coding: utf-8 -*-
"""Direct public minute-bar sources used by sync minute modules."""
from __future__ import annotations

import json
from typing import Iterable

import pandas as pd
import requests

from signals.sync.provider_limits import provider_call

_SINA_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
_TENCENT_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def stock_to_market_symbol(code: str) -> str:
    """Return Sina/Tencent market-prefixed A-share symbol, e.g. sh688252."""
    raw = str(code or "").strip().lower().replace(".", "")
    if raw.startswith(("sh", "sz", "bj")) and len(raw) >= 8:
        return raw

    pure = raw.replace("sh", "").replace("sz", "").replace("bj", "")
    if pure.startswith(("6", "5", "9")):
        return f"sh{pure}"
    if pure.startswith(("0", "2", "3")):
        return f"sz{pure}"
    if pure.startswith(("4", "8")):
        return f"bj{pure}"
    return pure


def _direct_get(url: str, *, params: dict, headers: dict | None = None, timeout: float = 10.0):
    session = requests.Session()
    session.trust_env = False
    provider = "tencent" if "gtimg.cn" in url else "sina" if "sina.cn" in url else "unknown"
    try:
        response = provider_call(
            provider,
            "stock_minute",
            lambda: session.get(url, params=params, headers=headers or _HEADERS, timeout=timeout),
            domain="minute",
        )
        response.raise_for_status()
        return response
    finally:
        session.close()


def _to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    result = pd.DataFrame(
        {
            "时间": pd.to_datetime(df["时间"], errors="coerce").astype(str),
            "开盘": _to_number(df["开盘"]),
            "最高": _to_number(df["最高"]),
            "最低": _to_number(df["最低"]),
            "收盘": _to_number(df["收盘"]),
            "成交量": _to_number(df["成交量"]),
            "成交额": _to_number(df.get("成交额", pd.Series(0, index=df.index))),
        }
    )
    result = result[result["时间"] != "NaT"].reset_index(drop=True)
    return result


def fetch_sina_minute(symbol: str, period: str, *, timeout: float = 10.0, datalen: int = 1970) -> pd.DataFrame:
    """Fetch 5/15/30 minute bars from Sina for stocks or indexes."""
    response = _direct_get(
        _SINA_URL,
        params={"symbol": symbol, "scale": period, "ma": "no", "datalen": str(datalen)},
        headers={**_HEADERS, "Accept": "*/*"},
        timeout=timeout,
    )
    text = response.text
    try:
        payload = text.split("=(", 1)[1].rsplit(");", 1)[0]
        rows = json.loads(payload)
    except Exception as exc:
        raise ValueError(f"Sina minute JSONP parse failed: {text[:120]}") from exc
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    rename = {
        "day": "时间",
        "open": "开盘",
        "high": "最高",
        "low": "最低",
        "close": "收盘",
        "volume": "成交量",
        "amount": "成交额",
    }
    missing = [key for key in ("day", "open", "high", "low", "close", "volume") if key not in df.columns]
    if missing:
        raise ValueError(f"Sina minute missing columns: {missing}")
    return _normalize_columns(df.rename(columns=rename))


def fetch_tencent_minute(symbol: str, period: str, *, timeout: float = 10.0, count: int = 320) -> pd.DataFrame:
    """Fetch 5/15/30 minute bars from Tencent for stocks or indexes."""
    key = f"m{period}"
    response = _direct_get(
        _TENCENT_URL,
        params={"param": f"{symbol},{key},,{count}"},
        headers={**_HEADERS, "Referer": "https://gu.qq.com/"},
        timeout=timeout,
    )
    data = response.json()
    rows = data.get("data", {}).get(symbol, {}).get(key, [])
    if not rows:
        return pd.DataFrame()

    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        amount = 0
        if len(row) > 7:
            # Tencent's last field is displayed in million-CNY units for stocks.
            amount = float(row[7] or 0) * 1_000_000
        parsed.append(
            {
                "时间": pd.to_datetime(row[0], format="%Y%m%d%H%M", errors="coerce"),
                "开盘": row[1],
                "收盘": row[2],
                "最高": row[3],
                "最低": row[4],
                "成交量": row[5],
                "成交额": amount,
            }
        )
    return _normalize_columns(pd.DataFrame(parsed))


def fetch_public_minute(
    symbol: str,
    period: str,
    *,
    providers: Iterable[str] = ("sina", "tencent"),
    timeout: float = 10.0,
    datalen: int | None = None,
    count: int | None = None,
) -> tuple[pd.DataFrame, str]:
    """Fetch public minute bars and return (dataframe, provider)."""
    errors: list[str] = []
    for provider in providers:
        try:
            if provider == "sina":
                df = fetch_sina_minute(symbol, period, timeout=timeout, datalen=datalen or 1970)
            elif provider == "tencent":
                df = fetch_tencent_minute(symbol, period, timeout=timeout, count=count or 320)
            else:
                raise ValueError(f"unknown minute provider: {provider}")
            if df is not None and not df.empty:
                return df, provider
            errors.append(f"{provider}: empty")
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {str(exc)[:160]}")
    raise RuntimeError("; ".join(errors))
