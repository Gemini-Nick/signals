# -*- coding: utf-8 -*-
"""
信号回测验证 API — 可视化信号回测（MACD + 缠论 + 可扩展）

输入股票代码 + 频率 + 信号组，返回 K线 + MACD + MA + 信号标记 + 前瞻评估。
"""
import logging
import math
import os
import traceback
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

import config
from signals.core.backtest_history import BacktestHistoryStore, SCHEMA_VERSION
from signals.core.backtest_terminal import build_backtest_terminal, build_batch_terminal, build_scan_terminal
from signals.core.market_time import to_unix_seconds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


def _history_store() -> BacktestHistoryStore:
    return BacktestHistoryStore()


@router.get("/history")
async def backtest_history(limit: int = Query(50, ge=1, le=200)):
    return {
        "schema_version": SCHEMA_VERSION,
        "items": _history_store().list(limit=limit),
        "limit": limit,
    }


@router.get("/history/{history_id}")
async def backtest_history_item(history_id: str):
    try:
        item = _history_store().get(history_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="backtest history entry not found")
    return item


@router.post("/history")
async def save_backtest_history(request: Request):
    try:
        payload = await request.json()
        return _history_store().save(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/history/{history_id}")
async def delete_backtest_history(history_id: str):
    try:
        item = _history_store().delete(history_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": item.get("id"), "deletedAt": item.get("deletedAt")}


# ─────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────

def _detect_market(code: str) -> str:
    """根据代码长度/格式判断市场: 'A' or 'HK'"""
    code = code.strip()
    if len(code) == 5:
        return "HK"
    return "A"


def _plain_stock_code(code: str) -> str:
    value = str(code or "").strip().upper()
    for prefix in ("SZ.", "SH.", "HK.", "US.", "BJ."):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _is_unsupported_backtest_code(code: str) -> bool:
    """Current kline backtest path does not yet support BJ/New Third Board symbols."""
    normalized = _plain_stock_code(code)
    return len(normalized) == 6 and normalized.startswith(("4", "8", "920"))


def _build_symbol(code: str, market: str) -> str:
    """构造 Futu 格式代码"""
    if market == "HK":
        return f"HK.{code}"
    if code.startswith(("6", "5")):
        return f"SH.{code}"
    return f"SZ.{code}"


def _normalize_freq(freq: str) -> str:
    value = (freq or "daily").strip().lower()
    if value in {"d", "day", "daily", "日线"}:
        return "daily"
    if value in {"w", "week", "weekly", "周线"}:
        return "weekly"
    if value in {"m", "month", "monthly", "月线"}:
        return "monthly"
    if value in {"30", "30m", "30min", "30分钟"}:
        return "30m"
    return value


def _freq_label(freq: str) -> str:
    return {
        "daily": "日线",
        "weekly": "周线",
        "monthly": "月线",
        "30m": "30分钟",
    }.get(freq, freq)


def _kline_cache_path(code: str, freq: str) -> Path:
    """K线缓存文件路径"""
    cache_dir = Path(__file__).resolve().parent.parent.parent.parent / ".data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    freq_norm = _normalize_freq(freq)
    return cache_dir / f"kline_{code}_{freq_norm}.json"


def _save_kline_cache(df: pd.DataFrame, code: str, freq: str):
    """保存K线到磁盘缓存"""
    import json
    try:
        path = _kline_cache_path(code, freq)
        records = []
        for dt_idx, row in df.iterrows():
            records.append({
                "dt": dt_idx.strftime("%Y-%m-%d"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "vol": int(row.get("vol", 0)),
            })
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        logger.info("K线缓存已保存: %s (%d 根)", path.name, len(records))
    except Exception as e:
        logger.warning("K线缓存保存失败: %s", e)


def _load_kline_cache(code: str, freq: str) -> pd.DataFrame:
    """从磁盘缓存加载K线"""
    import json
    path = _kline_cache_path(code, freq)
    logger.info("K线缓存路径: %s (exists=%s)", path, path.exists())
    if not path.exists():
        return pd.DataFrame()
    try:
        # 24h 过期
        import os
        age = datetime.now().timestamp() - os.path.getmtime(path)
        if age > 86400:
            logger.info("K线缓存已过期(%dh): %s", int(age / 3600), path.name)
            return pd.DataFrame()
        records = json.loads(path.read_text(encoding="utf-8"))
        df = pd.DataFrame(records)
        df["dt"] = pd.to_datetime(df["dt"])
        future_mask = df["dt"].dt.date > datetime.now().date()
        if future_mask.any():
            logger.info("K线缓存含未来日期，忽略并重建: %s", path.name)
            return pd.DataFrame()
        df = df.set_index("dt")
        logger.info("K线从磁盘缓存加载: %s (%d 根)", path.name, len(df))
        return df
    except Exception as e:
        logger.warning("K线缓存加载失败: %s", e)
        return pd.DataFrame()


def _resample_daily(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """用日线缓存聚合周线/月线，作为上游不可用时的本地兜底。"""
    if df.empty:
        return pd.DataFrame()
    required = {"open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()
    period_freq = {"weekly": "W", "monthly": "M"}.get(freq)
    if not period_freq:
        return pd.DataFrame()
    resampled = df.copy()
    if not isinstance(resampled.index, pd.DatetimeIndex):
        resampled.index = pd.to_datetime(resampled.index)
    for col in ["open", "high", "low", "close", "vol"]:
        if col in resampled.columns:
            resampled[col] = pd.to_numeric(resampled[col], errors="coerce")
    if "vol" not in resampled.columns:
        resampled["vol"] = 0
    resampled = resampled.sort_index()
    groups = resampled.groupby(resampled.index.to_period(period_freq), sort=True)
    rows = []
    indexes = []
    for _, group in groups:
        group = group.dropna(subset=["open", "high", "low", "close"])
        if group.empty:
            continue
        rows.append({
            "open": group["open"].iloc[0],
            "high": group["high"].max(),
            "low": group["low"].min(),
            "close": group["close"].iloc[-1],
            "vol": group["vol"].sum(),
        })
        indexes.append(group.index[-1])
    if not rows:
        return pd.DataFrame()
    resampled = pd.DataFrame(rows, index=pd.DatetimeIndex(indexes))
    return resampled.dropna(subset=["open", "high", "low", "close"])


def _resample_daily_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    return _resample_daily(df, "weekly")


def _records_to_df(records: list[dict[str, Any]]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    if "dt" not in df.columns:
        return pd.DataFrame()
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.set_index("dt").sort_index()
    for col in ["open", "high", "low", "close", "vol"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "vol" not in df.columns:
        df["vol"] = 0
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df[~df.index.duplicated(keep="last")]


def _load_mongo_bars(code: str, market: str, freq: str, limit: int = 1600) -> pd.DataFrame:
    from signals.data.mongo_fallback import get_db

    db = get_db()
    if db is None:
        return pd.DataFrame()
    symbol = _build_symbol(code, market)
    freq_candidates = {
        "daily": ["daily", "D", "日线"],
        "weekly": ["weekly", "W", "周线"],
        "monthly": ["monthly", "M", "月线"],
        "30m": ["30m", "30分钟"],
    }.get(freq, [freq])
    symbol_candidates = [code, symbol, symbol.lower(), symbol.replace(".", "").lower()]
    try:
        meta_query = {"meta.symbol": {"$in": symbol_candidates}, "meta.freq": {"$in": freq_candidates}}
        cursor = db.bars.find(meta_query, {"_id": 0}).sort("dt", -1)
        if limit > 0:
            cursor = cursor.limit(limit)
        docs = list(cursor)
        if not docs:
            legacy_query = {"symbol": {"$in": symbol_candidates}, "freq": {"$in": freq_candidates}}
            cursor = db.bars.find(legacy_query, {"_id": 0}).sort("dt", -1).max_time_ms(3000)
            if limit > 0:
                cursor = cursor.limit(limit)
            docs = list(cursor)
        docs.reverse()
    except Exception as e:
        logger.warning("Mongo bars 读取失败: %s %s — %s", code, freq, str(e)[:80])
        return pd.DataFrame()
    df = _records_to_df(docs)
    if df.empty:
        return pd.DataFrame()
    df.attrs["data_source"] = "mongodb_bars"
    df.attrs["data_source_detail"] = f"{code} {freq} MongoDB bars"
    return df


def _load_mongo_direct_or_derived(code: str, market: str, freq: str, limit: int = 1600) -> pd.DataFrame:
    direct = _load_mongo_bars(code, market, freq, limit=limit)
    if not direct.empty:
        return direct
    if freq in {"weekly", "monthly"}:
        daily = _load_mongo_bars(code, market, "daily", limit=limit)
        derived = _resample_daily(daily, freq)
        if not derived.empty:
            derived.attrs["data_source"] = f"mongodb_daily_resampled_{freq}"
            derived.attrs["data_source_detail"] = f"{code} {freq} 由 MongoDB 日线聚合"
            derived.attrs["derived_from"] = "mongodb_bars_daily"
            return derived
    return pd.DataFrame()


def _load_cached_kline_or_daily_derived(code: str, freq: str) -> pd.DataFrame:
    cached = _load_kline_cache(code, freq)
    if not cached.empty:
        logger.info("K线使用磁盘缓存: %s %s (%d 根)", code, freq, len(cached))
        cached.attrs["data_source"] = "disk_cache"
        cached.attrs["data_source_detail"] = f"{code} {freq} 磁盘缓存"
        return cached
    if freq in {"weekly", "monthly"}:
        daily = _load_kline_cache(code, "daily")
        derived = _resample_daily(daily, freq)
        if not derived.empty:
            logger.info("%s 由日线缓存聚合: %s (%d 根)", freq, code, len(derived))
            derived.attrs["data_source"] = f"daily_cache_resampled_{freq}"
            derived.attrs["data_source_detail"] = f"{code} {freq} 由日线磁盘缓存聚合"
            derived.attrs["derived_from"] = "disk_cache_daily"
            _save_kline_cache(derived, code, freq)
            return derived
    return pd.DataFrame()


def _attach_kline_meta(df: pd.DataFrame, source: str, detail: str, **meta: Any) -> pd.DataFrame:
    df.attrs["data_source"] = source
    df.attrs["data_source_detail"] = detail
    for key, value in meta.items():
        if value is not None:
            df.attrs[key] = value
    return df


def _eastmoney_secid(code: str, market: str) -> str:
    if market == "HK":
        return f"116.{code}"
    prefix = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{prefix}.{code}"


def _fetch_eastmoney_push2his(
    code: str,
    market: str,
    freq: str,
    start_date: str,
    end_date: str,
    timeout: int = 8,
) -> pd.DataFrame:
    """
    Fetch Eastmoney push2his K lines without AKShare.

    AKShare's wrapper is unstable on the local Clash Verge TUN/fake-ip route.
    This native request preserves the working system proxy path and only parses
    the K-line fields Signals needs.
    """
    import requests

    klt = {"daily": "101", "weekly": "102", "monthly": "103"}.get(freq)
    if not klt:
        return pd.DataFrame()

    session = requests.Session()
    session.trust_env = True
    response = session.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": _eastmoney_secid(code, market),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt,
            "fqt": "1",
            "beg": start_date,
            "end": end_date,
        },
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    klines = (payload.get("data") or {}).get("klines") or []
    rows = []
    for item in klines:
        parts = item.split(",")
        if len(parts) < 6:
            continue
        rows.append({
            "dt": parts[0],
            "open": parts[1],
            "close": parts[2],
            "high": parts[3],
            "low": parts[4],
            "vol": parts[5],
        })
    df = _records_to_df(rows)
    if not df.empty:
        df.attrs["eastmoney_rc"] = payload.get("rc")
    return df


def _kline_meta(df: pd.DataFrame, freq: str) -> dict[str, Any]:
    as_of = None
    if not df.empty:
        try:
            as_of = pd.Timestamp(df.index.max()).strftime("%Y-%m-%d")
        except Exception:
            as_of = None
    freshness = "empty"
    if as_of:
        try:
            age_days = (pd.Timestamp(datetime.now().date()) - pd.Timestamp(as_of)).days
            freshness = "fresh" if age_days <= 3 else "stale"
        except Exception:
            freshness = "unknown"
    partial = False
    if as_of and freq in {"weekly", "monthly"}:
        now = pd.Timestamp(datetime.now().date())
        last = pd.Timestamp(as_of)
        partial = (freq == "weekly" and last.isocalendar().week == now.isocalendar().week and last.date() < now.date()) or (
            freq == "monthly" and last.year == now.year and last.month == now.month and last.date() < now.date()
        )
    return {
        "data_source": df.attrs.get("data_source", ""),
        "data_source_detail": df.attrs.get("data_source_detail", ""),
        "as_of": as_of,
        "bar_count": int(len(df)),
        "freshness": freshness,
        "derived_from": df.attrs.get("derived_from", ""),
        "partial": partial,
        "last_upstream_error": df.attrs.get("last_upstream_error", ""),
    }


def _parse_backtest_boundary(value: Any, *, end: bool = False) -> pd.Timestamp | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"日期格式无效: {raw}")
    ts = pd.Timestamp(parsed)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    ts = ts.normalize()
    if end:
        ts = ts + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return ts


def _resolve_backtest_date_scope(
    df: pd.DataFrame,
    *,
    start_date: Any = "",
    end_date: Any = "",
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    start_ts = _parse_backtest_boundary(start_date)
    end_ts = _parse_backtest_boundary(end_date, end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("start_date 不能晚于 end_date")

    analysis_df = df
    if end_ts is not None:
        analysis_df = analysis_df[analysis_df.index <= end_ts].copy()
    visible_df = analysis_df
    if start_ts is not None:
        visible_df = visible_df[visible_df.index >= start_ts].copy()
    if visible_df is analysis_df:
        visible_df = visible_df.copy()

    return analysis_df, visible_df, {
        "requested_start_date": start_ts.strftime("%Y-%m-%d") if start_ts is not None else "",
        "requested_end_date": end_ts.strftime("%Y-%m-%d") if end_ts is not None else "",
        "start_date": visible_df.index[0].strftime("%Y-%m-%d") if not visible_df.empty else "",
        "end_date": visible_df.index[-1].strftime("%Y-%m-%d") if not visible_df.empty else "",
    }


def _signal_timestamp(signal: dict[str, Any]) -> pd.Timestamp | None:
    date_text = str(signal.get("date_str") or signal.get("date") or "").strip()
    if date_text:
        parsed = pd.to_datetime(date_text[:10], errors="coerce")
        return None if pd.isna(parsed) else pd.Timestamp(parsed).normalize()
    raw_time = signal.get("dt") or signal.get("time")
    if raw_time is None:
        return None
    try:
        timestamp = int(raw_time)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        return pd.to_datetime(timestamp, unit="ms").normalize()
    return pd.to_datetime(timestamp, unit="s").normalize()


def _filter_signals_by_date_scope(
    signals: list[dict[str, Any]],
    *,
    start_date: Any = "",
    end_date: Any = "",
) -> list[dict[str, Any]]:
    start_ts = _parse_backtest_boundary(start_date)
    end_ts = _parse_backtest_boundary(end_date, end=True)
    if start_ts is None and end_ts is None:
        return signals
    filtered = []
    for signal in signals:
        ts = _signal_timestamp(signal)
        if ts is None:
            continue
        if start_ts is not None and ts < start_ts:
            continue
        if end_ts is not None and ts > end_ts:
            continue
        filtered.append(signal)
    return filtered


def _fetch_kline_legacy(code: str, market: str, freq: str) -> pd.DataFrame:
    """
    拉取 K 线数据，降级链：
      Mongo bars → 磁盘缓存 → 东财 push2his 原生请求 → 新浪 → AKShare/MongoDB 兜底
    返回带 datetime index 的 DataFrame。
    """
    import akshare as ak
    from signals.data.fetcher import _no_proxy
    from signals.data.mongo_fallback import get_kline_docs, save_kline

    _NETWORK_ERRORS = (
        "RemoteDisconnected", "ConnectionReset", "Connection aborted",
        "ConnectionError", "timeout", "Max retries exceeded",
        "SSLError", "SSL",
    )

    freq = _normalize_freq(freq)
    errors = []

    mongo_cached = _load_mongo_direct_or_derived(code, market, freq)
    if not mongo_cached.empty:
        return mongo_cached

    df = None
    sina_attempted = False
    cached = _load_cached_kline_or_daily_derived(code, freq)
    if not cached.empty:
        return cached

    # ── 30 分钟 K 线（东财分钟接口）────────────────────
    if freq == "30m":
        days = 60
        sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d 09:30:00")
        edt = datetime.now().strftime("%Y%m%d 15:00:00")
        try:
            with _no_proxy():
                if market == "A":
                    df = ak.stock_zh_a_hist_min_em(
                        symbol=code, period="30",
                        start_date=sdt, end_date=edt, adjust="qfq")
                else:
                    logger.warning("30分钟K线暂不支持港股: %s", code)
                    return pd.DataFrame()
            if df is not None and not df.empty:
                df = df.rename(columns={
                    "时间": "dt", "开盘": "open", "最高": "high",
                    "最低": "low", "收盘": "close", "成交量": "vol",
                })
                df["dt"] = pd.to_datetime(df["dt"])
                df = df.set_index("dt")
                return _attach_kline_meta(df, "eastmoney_minute", f"{code} 30m 东财分钟线")
        except Exception as e:
            err_msg = str(e)
            errors.append(f"eastmoney_minute: {err_msg[:120]}")
            is_network = any(k in err_msg for k in _NETWORK_ERRORS)
            logger.warning("30分钟K线失败: %s — %s", code, err_msg[:80])
            if not is_network:
                return pd.DataFrame()
        return pd.DataFrame()

    # ── 日线 / 周线 ──────────────────────────────────
    days = 365 * 5
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    edt = datetime.now().strftime("%Y%m%d")
    period = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}.get(freq, "weekly")

    # ── 源 1: 东财 push2his 原生请求 ─────────────────
    try:
        if market in {"A", "HK"} and freq in {"daily", "weekly", "monthly"}:
            df_em = _fetch_eastmoney_push2his(code, market, freq, sdt, edt)
            if not df_em.empty:
                _attach_kline_meta(df_em, "eastmoney_push2his", f"{code} {freq} 东财 push2his")
                logger.info("东财 push2his K线成功: %s %s (%d 根)", code, freq, len(df_em))
                _save_kline_cache(df_em, code, freq)
                save_kline("kline_cache", code, freq, _df_to_records(df_em))
                return df_em
    except Exception as e:
        err_msg = str(e)
        errors.append(f"eastmoney_push2his: {err_msg[:120]}")
        logger.warning("东财 push2his 原生请求失败: %s %s — %s", code, freq, err_msg[:120])

    # ── 源 2: 新浪日线 + 本地聚合 ─────────────────────
    if market == "A" and freq in {"daily", "weekly", "monthly"}:
        sina_attempted = True
        try:
            sina_code = f"sz{code}" if code.startswith(("0", "3")) else f"sh{code}"
            with _no_proxy():
                df2 = ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq")
            if df2 is not None and not df2.empty:
                cutoff = datetime.now() - timedelta(days=days)
                df2["date"] = pd.to_datetime(df2["date"])
                df2 = df2[df2["date"] >= cutoff]
                df2 = df2.rename(columns={
                    "date": "dt", "volume": "vol",
                })
                df2 = df2[["dt", "open", "high", "low", "close", "vol"]]
                df2 = df2.set_index("dt")
                for col in ["open", "high", "low", "close", "vol"]:
                    df2[col] = pd.to_numeric(df2[col], errors="coerce")
                if freq == "daily":
                    _attach_kline_meta(df2, "sina", f"{code} daily 新浪")
                    logger.info("新浪K线成功: %s daily (%d 根)", code, len(df2))
                    _save_kline_cache(df2, code, "daily")
                    save_kline("kline_cache", code, "daily", _df_to_records(df2))
                    return df2
                derived = _resample_daily(df2, freq)
                if not derived.empty:
                    _attach_kline_meta(
                        derived,
                        f"sina_daily_resampled_{freq}",
                        f"{code} {freq} 由新浪日线聚合",
                        derived_from="sina_daily",
                    )
                    logger.info("新浪日线聚合成功: %s %s (%d 根)", code, freq, len(derived))
                    _save_kline_cache(df2, code, "daily")
                    _save_kline_cache(derived, code, freq)
                    save_kline("kline_cache", code, "daily", _df_to_records(df2))
                    save_kline("kline_cache", code, freq, _df_to_records(derived))
                    return derived
        except Exception as e:
            errors.append(f"sina: {str(e)[:120]}")
            logger.warning("新浪优先K线失败: %s %s — %s", code, freq, str(e)[:80])

    # ── 源 3: AKShare 东财封装兜底 ──────────────────
    try:
        with _no_proxy():
            if market == "A":
                df = ak.stock_zh_a_hist(
                    symbol=code, period=period,
                    start_date=sdt, end_date=edt, adjust="qfq")
            else:
                df = ak.stock_hk_hist(
                    symbol=code, period=period,
                    start_date=sdt, end_date=edt, adjust="qfq")
        if df is not None and not df.empty:
            df = df.rename(columns={
                "日期": "dt", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "vol",
            })
            df["dt"] = pd.to_datetime(df["dt"])
            df = df.set_index("dt")
            _attach_kline_meta(df, "eastmoney", f"{code} {freq} 东财")
            _save_kline_cache(df, code, freq)
            save_kline("kline_cache", code, freq, _df_to_records(df))
            return df
    except Exception as e:
        err_msg = str(e)
        errors.append(f"eastmoney: {err_msg[:120]}")
        is_network = any(k in err_msg for k in _NETWORK_ERRORS)
        if is_network:
            logger.warning("东财K线失败: %s %s — %s", code, freq, err_msg[:80])
        else:
            logger.warning("东财K线异常: %s %s — %s", code, freq, err_msg[:80])

    # ── 源 3: 新浪重试（仅 A 股日线）────────────────
    if market == "A" and freq == "daily" and not sina_attempted:
        try:
            sina_code = f"sz{code}" if code.startswith(("0", "3")) else f"sh{code}"
            with _no_proxy():
                df2 = ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq")
            if df2 is not None and not df2.empty:
                cutoff = datetime.now() - timedelta(days=days)
                df2["date"] = pd.to_datetime(df2["date"])
                df2 = df2[df2["date"] >= cutoff]
                df2 = df2.rename(columns={
                    "date": "dt", "volume": "vol",
                })
                df2 = df2[["dt", "open", "high", "low", "close", "vol"]]
                df2 = df2.set_index("dt")
                _attach_kline_meta(df2, "sina", f"{code} {freq} 新浪")
                logger.info("新浪K线成功: %s %s (%d 根)", code, freq, len(df2))
                _save_kline_cache(df2, code, freq)
                save_kline("kline_cache", code, freq, _df_to_records(df2))
                return df2
        except Exception as e:
            errors.append(f"sina: {str(e)[:120]}")
            logger.warning("新浪K线失败: %s — %s", code, str(e)[:80])

    # ── 源 4: MongoDB 历史数据 ──────────────────────
    mongo_docs = get_kline_docs("kline_cache", code, freq)
    if mongo_docs:
        df3 = pd.DataFrame(mongo_docs)
        df3["dt"] = pd.to_datetime(df3["dt"])
        df3 = df3.set_index("dt")
        for col in ["open", "high", "low", "close", "vol"]:
            if col in df3.columns:
                df3[col] = pd.to_numeric(df3[col], errors="coerce")
        _attach_kline_meta(df3, "mongodb", f"{code} {freq} MongoDB kline_cache")
        return df3

    raise ConnectionError(
        f"所有数据源均失败（Mongo bars/磁盘缓存/东财/新浪/MongoDB kline_cache），{code} 无可用数据。"
        + (f" 最近错误: {' | '.join(errors[-3:])}" if errors else "")
    )


def _fetch_kline(code: str, market: str, freq: str) -> pd.DataFrame:
    """Fetch backtest K-lines through the Data Gateway as historical data."""
    from signals.data.gateway import get_kline
    from signals.data.models import DataRequest

    freq_norm = _normalize_freq(freq)
    local_cached = _load_mongo_direct_or_derived(code, market, freq_norm)
    if not local_cached.empty:
        return local_cached
    disk_cached = _load_cached_kline_or_daily_derived(code, freq_norm)
    if not disk_cached.empty:
        return disk_cached

    request = DataRequest(
        domain="kline",
        mode="historical",
        market=market,
        freq=freq_norm,
        symbol=code,
        purpose="backtest",
    )
    response = get_kline(
        request,
        legacy_fetcher=lambda: _fetch_kline_legacy(code, market, freq),
    )
    if response.data is None or response.data.empty:
        raise ConnectionError(
            f"历史K线不可用: {code} {freq}; "
            f"source={response.source}; errors={' | '.join(response.errors)}"
        )
    response.data.attrs.setdefault("data_source", response.source)
    response.data.attrs["gateway_mode"] = response.mode_used
    response.data.attrs["gateway_freshness"] = response.freshness
    return response.data


def _df_to_records(df: pd.DataFrame) -> list:
    """DataFrame → K 线记录列表（用于存储）"""
    records = []
    for dt_idx, row in df.iterrows():
        records.append({
            "dt": dt_idx.strftime("%Y-%m-%d"),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": int(row.get("vol", 0)),
        })
    return records


def _dt_to_unix(dt) -> int:
    """datetime / Timestamp → unix seconds using market-local naive semantics."""
    return to_unix_seconds(dt, market="A")


def _position_for_index_label(index: pd.Index, label) -> int:
    loc = index.get_loc(label)
    if isinstance(loc, slice):
        start = loc.start or 0
        stop = loc.stop or start + 1
        return max(start, stop - 1)
    if isinstance(loc, (list, tuple)):
        return int(loc[-1])
    if hasattr(loc, "nonzero"):
        positions = loc.nonzero()[0]
        if len(positions):
            return int(positions[-1])
    return int(loc)


def _serialize_ohlcv(df: pd.DataFrame) -> list:
    """DataFrame → [{time, open, high, low, close, volume}]"""
    result = []
    for dt_idx, row in df.iterrows():
        result.append({
            "time": _dt_to_unix(dt_idx),
            "open": round(float(row["open"]), 4),
            "high": round(float(row["high"]), 4),
            "low": round(float(row["low"]), 4),
            "close": round(float(row["close"]), 4),
            "volume": int(row.get("vol", 0)),
        })
    return result


def _compute_macd_data(df: pd.DataFrame) -> list:
    """计算 MACD 指标，返回图表格式 [{time, dif, dea, bar}]"""
    closes = df["close"]
    if len(closes) < 26:
        return []
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    result = []
    for i, dt_idx in enumerate(df.index):
        dea_value = dea.iloc[i]
        if pd.isna(dea_value):
            continue
        result.append({
            "time": _dt_to_unix(dt_idx),
            "dif": round(float(dif.iloc[i]), 4),
            "dea": round(float(dea_value), 4),
            "bar": round(float(hist.iloc[i]), 4),
        })
    return result


def _compute_ma_lines(df: pd.DataFrame) -> list:
    """计算 MA 均线，返回 [{label, color, data: [{time, value}]}]"""
    from signals.core.ma_levels import KEY_MA_COLORS, KEY_MA_PERIODS

    closes = df["close"]
    ma_lines = []
    for period in KEY_MA_PERIODS:
        if len(closes) < period:
            continue
        ma_vals = closes.rolling(period).mean()
        line_data = []
        for dt_idx, val in ma_vals.dropna().items():
            line_data.append({
                "time": _dt_to_unix(dt_idx),
                "value": round(float(val), 4),
            })
        ma_lines.append({"label": f"MA{period}", "color": KEY_MA_COLORS.get(period, "#2962ff"), "data": line_data})
    return ma_lines


def _compute_forward_eval(df: pd.DataFrame, sig_idx: int) -> dict:
    """
    计算信号触发后的前瞻评估。

    所有信号都视为买入方向（MACD Pattern A/B 均为看多信号，
    缠论买信号看多、卖信号在此标注 direction 反转）。

    返回: {return_t5, return_t10, return_t20, mfe, mae, mfe_day, mae_day,
           direction_correct, data_sufficient}
    """
    signal_price = df.iloc[sig_idx]["close"]
    if signal_price <= 0:
        return {}

    remaining = len(df) - sig_idx - 1
    result = {
        "return_t5": None, "return_t10": None, "return_t20": None,
        "mfe": 0.0, "mae": 0.0, "mfe_day": 0, "mae_day": 0,
        "direction_correct": None, "data_sufficient": remaining >= 20,
    }

    mfe, mae = 0.0, 0.0
    for i in range(1, min(remaining + 1, 21)):
        bar = df.iloc[sig_idx + i]
        close_ret = (bar["close"] - signal_price) / signal_price * 100
        high_ret = (bar["high"] - signal_price) / signal_price * 100
        low_ret = (bar["low"] - signal_price) / signal_price * 100

        if i == 5:
            result["return_t5"] = round(close_ret, 2)
        elif i == 10:
            result["return_t10"] = round(close_ret, 2)
        elif i == 20:
            result["return_t20"] = round(close_ret, 2)

        if high_ret > mfe:
            mfe = high_ret
            result["mfe_day"] = i
        if low_ret < mae:
            mae = low_ret
            result["mae_day"] = i

    result["mfe"] = round(float(mfe), 2)
    result["mae"] = round(float(mae), 2)

    # 方向判定（基于 T+10 或 T+5）
    ref = result["return_t10"] if result["return_t10"] is not None else result["return_t5"]
    if ref is not None:
        if ref > 2.0:
            result["direction_correct"] = 1
        elif ref < -2.0:
            result["direction_correct"] = 0

    # 确保所有值是 Python 原生类型（避免 numpy.bool_ / numpy.float64 序列化失败）
    result["data_sufficient"] = bool(result["data_sufficient"])
    return result


def _compute_kpi(signal_evals: list) -> dict:
    """从信号+评估列表计算 KPI"""
    total = len(signal_evals)
    if total == 0:
        return {"total": 0}

    valid = [s for s in signal_evals if s.get("eval", {}).get("return_t10") is not None]
    wins = sum(1 for s in valid if (s.get("eval", {}).get("direction_correct") == 1))
    losses = sum(1 for s in valid if (s.get("eval", {}).get("direction_correct") == 0))
    valid_count = len(valid)

    returns_t10 = [s["eval"]["return_t10"] for s in valid if s["eval"]["return_t10"] is not None]
    mfes = [s["eval"]["mfe"] for s in valid if s["eval"].get("mfe") is not None]
    maes = [s["eval"]["mae"] for s in valid if s["eval"].get("mae") is not None]

    win_rate = round(wins / valid_count * 100, 1) if valid_count else 0
    avg_return = round(sum(returns_t10) / len(returns_t10), 2) if returns_t10 else 0
    avg_mfe = round(sum(mfes) / len(mfes), 2) if mfes else 0
    avg_mae = round(sum(maes) / len(maes), 2) if maes else 0

    # 期望 = avg_win * WR - avg_loss * LR
    pos = [r for r in returns_t10 if r > 0]
    neg = [r for r in returns_t10 if r < 0]
    avg_win = sum(pos) / len(pos) if pos else 0
    avg_loss = abs(sum(neg) / len(neg)) if neg else 0
    wr = wins / valid_count if valid_count else 0
    lr = losses / valid_count if valid_count else 0
    expectancy = round(avg_win * wr - avg_loss * lr, 2)

    # 按信号类型分组
    by_type = {}
    for s in valid:
        sig_type = _signal_breakdown_label(s.get("type", ""), s.get("group", ""))
        if sig_type not in by_type:
            by_type[sig_type] = {"count": 0, "wins": 0, "returns": []}
        by_type[sig_type]["count"] += 1
        if s["eval"].get("direction_correct") == 1:
            by_type[sig_type]["wins"] += 1
        by_type[sig_type]["returns"].append(s["eval"]["return_t10"])

    by_type_kpi = {}
    for sig_type, info in by_type.items():
        cnt = info["count"]
        wr_type = round(info["wins"] / cnt * 100, 1) if cnt else 0
        avg_r = round(sum(info["returns"]) / cnt, 2) if cnt else 0
        by_type_kpi[sig_type] = {"count": cnt, "win_rate": wr_type, "avg_return_t10": avg_r}

    # 按 MA 确认分组
    ma_confirmed = [s for s in valid if s.get("ma_confirmed")]
    ma_not = [s for s in valid if not s.get("ma_confirmed")]
    by_ma = {}
    for label, subset in [("MA确认", ma_confirmed), ("无MA锚点", ma_not)]:
        cnt = len(subset)
        if cnt == 0:
            continue
        w = sum(1 for s in subset if s.get("eval", {}).get("direction_correct") == 1)
        rets = [s["eval"]["return_t10"] for s in subset if s["eval"].get("return_t10") is not None]
        by_ma[label] = {
            "count": cnt,
            "win_rate": round(w / cnt * 100, 1),
            "avg_return_t10": round(sum(rets) / len(rets), 2) if rets else 0,
        }

    return {
        "total": total,
        "evaluated": valid_count,
        "win_rate": win_rate,
        "avg_return_t10": avg_return,
        "expectancy": expectancy,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "by_type": by_type_kpi,
        "by_ma": by_ma,
    }


def _avg(values: list[float]) -> float:
    numbers = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numbers.append(number)
    return round(sum(numbers) / len(numbers), 2) if numbers else 0


def _signal_breakdown_label(sig_type: str, sig_group: str = "") -> str:
    """Return stable, user-facing signal buckets for return breakdown tables."""
    text = str(sig_type or "").strip()
    group = str(sig_group or "").strip()
    if not text and not group:
        return "unknown"

    if group == "macd":
        if text.startswith("A_") or "零上回踩" in text or "Pattern A" in text:
            return "MACD · A零上回踩"
        if text.startswith("B_") or "零下企稳" in text or "Pattern B" in text:
            return "MACD · B零下企稳"
        return f"MACD · {text or '信号'}"

    if group == "czsc":
        if text.startswith("形态:"):
            return f"形态 · {text.split(':', 1)[1]}"
        if text.startswith("缺口"):
            if "买" in text:
                return "缺口 · 买点"
            if "卖" in text:
                return "缺口 · 卖点"
            return "缺口 · 结构"
        if "买" in text or "卖" in text or "背驰" in text or "趋势" in text:
            return f"缠论 · {text}"
        return f"缠论 · {text or '结构信号'}"

    if group == "trend" or text.startswith("Breakout_"):
        if text.startswith("Breakout_") and text.endswith("D"):
            days = text.replace("Breakout_", "").replace("D", "")
            return f"突破 · {days}日新高"
        return "突破 · 趋势突破"

    if group == "200d_new_high_breakout" or "200日新高" in text:
        return "突破 · 200日新高"

    if group == "gap" or text.startswith("Gap_"):
        return "缺口 · 跳空买点"

    if group == "vol" or text.startswith("VolSqueeze_"):
        return "波动收缩 · 布林突破"

    if group == "candle_run" or text.startswith("CandleRun_"):
        count = text.replace("CandleRun_", "") if text.startswith("CandleRun_") else ""
        return f"K线 · 连续阳线{count}" if count else "K线 · 连续阳线"

    if group == "candle_accel" or text.startswith("CandleAccel_"):
        count = text.replace("CandleAccel_", "") if text.startswith("CandleAccel_") else ""
        return f"K线 · 加速阳线{count}" if count else "K线 · 加速阳线"

    return text or group or "unknown"


def _batch_signal_breakdown(code: str, name: str, signals: list[dict], trades: list[dict]) -> list[dict]:
    """Aggregate one stock's all-signal forward returns and simulated trade returns."""
    groups: dict[str, dict] = {}

    def group_for(sig_type: str, sig_group: str = "") -> dict:
        key = _signal_breakdown_label(sig_type, sig_group)
        if key not in groups:
            groups[key] = {
                "signal_type": key,
                "signal_group": sig_group,
                "code": code,
                "name": name,
                "signal_count": 0,
                "evaluated_count": 0,
                "win_count": 0,
                "returns_t5": [],
                "returns_t10": [],
                "returns_t20": [],
                "mfes": [],
                "maes": [],
                "trade_count": 0,
                "trade_win_count": 0,
                "trade_returns": [],
            }
        return groups[key]

    for signal in signals:
        sig_type = str(signal.get("type") or signal.get("group") or "unknown")
        sig_group = str(signal.get("group") or "")
        group = group_for(sig_type, sig_group)
        group["signal_count"] += 1
        ev = signal.get("eval") or {}
        return_t10 = ev.get("return_t10")
        if return_t10 is not None:
            group["evaluated_count"] += 1
            if ev.get("direction_correct") == 1:
                group["win_count"] += 1
            group["returns_t10"].append(float(return_t10))
        if ev.get("return_t5") is not None:
            group["returns_t5"].append(float(ev["return_t5"]))
        if ev.get("return_t20") is not None:
            group["returns_t20"].append(float(ev["return_t20"]))
        if ev.get("mfe") is not None:
            group["mfes"].append(float(ev["mfe"]))
        if ev.get("mae") is not None:
            group["maes"].append(float(ev["mae"]))

    for trade in trades:
        sig_type = str(trade.get("signal_type") or trade.get("signal_group") or "unknown")
        group = group_for(sig_type, str(trade.get("signal_group") or ""))
        if trade.get("entry_price") is None or trade.get("net_return_pct") is None:
            continue
        net_return = float(trade["net_return_pct"])
        group["trade_count"] += 1
        if net_return > 0:
            group["trade_win_count"] += 1
        group["trade_returns"].append(net_return)

    rows = []
    for group in groups.values():
        evaluated = group["evaluated_count"]
        trade_count = group["trade_count"]
        rows.append({
            "signal_type": group["signal_type"],
            "signal_group": group["signal_group"],
            "code": group["code"],
            "name": group["name"],
            "signal_count": group["signal_count"],
            "evaluated_count": evaluated,
            "win_count": group["win_count"],
            "win_rate": round(group["win_count"] / evaluated * 100, 1) if evaluated else 0,
            "avg_t5_pct": _avg(group["returns_t5"]),
            "avg_t10_pct": _avg(group["returns_t10"]),
            "avg_t20_pct": _avg(group["returns_t20"]),
            "avg_mfe_pct": _avg(group["mfes"]),
            "avg_mae_pct": _avg(group["maes"]),
            "trade_count": trade_count,
            "trade_win_count": group["trade_win_count"],
            "trade_win_rate": round(group["trade_win_count"] / trade_count * 100, 1) if trade_count else 0,
            "avg_trade_return_pct": _avg(group["trade_returns"]),
        })
    return sorted(rows, key=lambda item: (item["signal_count"], item["avg_t10_pct"]), reverse=True)


_PRESET_STALE_DAYS = 14  # 最后标签超过此天数视为过期


def _get_date_presets(show_sector: bool = False) -> list:
    """将 config.DATE_PRESETS 转为前端格式。
    - 历史标签（非当年）及 tier=major 的当年标签始终显示
    - tier=sector 的当年标签仅在 show_sector=True 时显示
    - 无 tier 字段的当年标签视为 major
    """
    current_year = datetime.now().year
    presets = []

    for key, info in config.DATE_PRESETS.items():
        if "date" not in info:
            continue
        date_str = info["date"]
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        # 当年的 sector 级标签：默认不显示
        if dt.year == current_year and info.get("tier") == "sector" and not show_sector:
            continue

        presets.append({
            "key": key,
            "date": date_str,
            "time": int(dt.timestamp()),
            "label": info["label"],
            "tier": info.get("tier", "major"),
        })

    presets = sorted(presets, key=lambda x: x["time"])

    # 如果最后标签距今超过阈值，追加动态"近期行情"标签
    if presets:
        last_date = datetime.strptime(presets[-1]["date"], "%Y-%m-%d")
        gap_days = (datetime.now() - last_date).days
        if gap_days > _PRESET_STALE_DAYS:
            recent_dt = last_date + timedelta(days=1)
            recent_str = recent_dt.strftime("%Y-%m-%d")
            presets.append({
                "key": "_recent",
                "date": recent_str,
                "time": int(recent_dt.timestamp()),
                "label": f"近期行情 — 距上次标签{gap_days}天（建议更新事件标签）",
            })
            logger.info("日期标签过期 %d 天，已追加动态标签。最后标签: %s",
                        gap_days, presets[-2]["date"])
    return presets


# ─────────────────────────────────────────────────────
# MACD 信号检测
# ─────────────────────────────────────────────────────

def _detect_macd(df: pd.DataFrame, symbol: str, freq_label: str, lookback: int) -> list:
    """运行 MACD 信号检测，返回信号列表"""
    from signals.core.macd_detector import detect_macd_signals

    signals = detect_macd_signals(df, symbol, freq_label, lookback=lookback)
    result = []
    for sig in signals:
        sig_idx = _position_for_index_label(df.index, sig.dt)
        eval_data = _compute_forward_eval(df, sig_idx)

        result.append({
            "dt": _dt_to_unix(sig.dt),
            "date_str": sig.dt.strftime("%Y-%m-%d"),
            "type": sig.pattern,
            "group": "macd",
            "price": round(sig.price, 4),
            "confidence": sig.confidence,
            "details": sig.details,
            "eval": eval_data,
        })
    return result


# ─────────────────────────────────────────────────────
# 缠论信号检测
# ─────────────────────────────────────────────────────

def _detect_czsc(
    df: pd.DataFrame,
    symbol: str,
    freq_label: str,
    lookback: int = 999,
    *,
    include_auxiliary: bool = True,
) -> tuple:
    """
    运行缠论信号检测，返回 (signals_list, bi_list, zhongshu_list)。
    对历史前缀滚动构建 CZSC 对象，避免只拿到最后一个结构信号。
    """
    from czsc import CZSC, RawBar, Freq
    from signals.core.detectors import detect_all_signals, detect_structural_signals

    freq_map = {"日线": Freq.D, "周线": Freq.W, "月线": Freq.M}
    freq_enum = freq_map.get(freq_label, Freq.D)

    # DataFrame → RawBar list
    bars = []
    for i, (dt_idx, row) in enumerate(df.iterrows()):
        bars.append(RawBar(
            symbol=symbol, dt=dt_idx, id=i, freq=freq_enum,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            vol=int(row.get("vol", 0)), amount=0,
        ))

    if len(bars) < 35:
        return [], [], []

    scan_start = max(35, len(bars) - max(int(lookback or 0), 1))
    start_end = max(scan_start, 35)
    rolling_obj = CZSC(bars[:start_end], max_bi_num=200)
    detector = detect_all_signals if include_auxiliary else detect_structural_signals
    signals_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for end in range(start_end, len(bars) + 1):
        try:
            if end > start_end:
                rolling_obj.update(bars[end - 1])
            events = detector(rolling_obj, symbol)
        except Exception:
            continue

        for ev in events:
            # MACD has its own detector; keep CZSC rows focused on buy/sell/structure.
            if "MACD" in ev.signal_type:
                continue
            try:
                sig_idx = df.index.get_indexer([ev.dt], method="nearest")[0]
                if sig_idx < scan_start or sig_idx < 0 or sig_idx >= end:
                    continue
            except Exception:
                continue

            date_str = ev.dt.strftime("%Y-%m-%d") if hasattr(ev.dt, "strftime") else str(ev.dt)[:10]
            key = (date_str, ev.signal_type)
            confidence = float(ev.confidence or 0)
            existing = signals_by_key.get(key)
            if existing and confidence <= float(existing.get("confidence") or 0):
                continue
            row = {
                "dt": _dt_to_unix(ev.dt),
                "date_str": date_str,
                "type": ev.signal_type,
                "group": "czsc",
                "price": round(ev.price, 4),
                "confidence": ev.confidence,
                "details": ev.details,
                "eval": _compute_forward_eval(df, sig_idx),
            }
            signals_by_key[key] = row

    signals = sorted(signals_by_key.values(), key=lambda item: item["dt"])

    # 序列化笔线
    bi_list = []
    for bi in rolling_obj.bi_list:
        bi_list.append({
            "sdt": _dt_to_unix(bi.fx_a.dt),
            "edt": _dt_to_unix(bi.fx_b.dt),
            "high": round(bi.high, 4),
            "low": round(bi.low, 4),
            "direction": "up" if bi.direction.value == "向上" else "down",
            "power": round(bi.power_price, 4) if hasattr(bi, "power_price") else 0,
        })

    # 序列化中枢（从笔中提取）
    zhongshu = _extract_zhongshu(rolling_obj)

    return signals, bi_list, zhongshu


def _extract_zhongshu(czsc_obj) -> list:
    """从 CZSC 对象提取中枢"""
    bis = czsc_obj.bi_list
    if len(bis) < 3:
        return []

    result = []
    i = 0
    while i < len(bis) - 2:
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        zg = min(b1.high, b3.high)
        zd = max(b1.low, b3.low)
        if zg > zd:
            # 找中枢的延伸
            end_idx = i + 2
            for j in range(i + 3, len(bis)):
                if bis[j].high >= zd and bis[j].low <= zg:
                    end_idx = j
                else:
                    break
            result.append({
                "zd": round(zd, 4),
                "zg": round(zg, 4),
                "start_dt": _dt_to_unix(bis[i].fx_a.dt),
                "end_dt": _dt_to_unix(bis[end_idx].fx_b.dt),
                "bi_count": end_idx - i + 1,
            })
            i = end_idx + 1
        else:
            i += 1
    return result


# ─────────────────────────────────────────────────────
# API 端点
# ─────────────────────────────────────────────────────

def _detect_entry_factors(
    df: pd.DataFrame,
    factor: str,
    lookback: int = 999,
    **kwargs,
) -> list:
    """运行入场因子检测"""
    from signals.core.entry_factors import (
        detect_gap_entries,
        detect_trend_breakout_entries,
        detect_200d_new_high_entries,
        detect_volatility_contraction_entries,
        detect_candle_run_entries,
        detect_candle_accel_entries,
    )

    signals = []

    if factor in ("gap", "all_factors"):
        gap_sigs = detect_gap_entries(
            df,
            gap_pct_min=float(kwargs.get("gap_pct_min", 2.0)),
            volume_ratio_min=float(kwargs.get("volume_ratio_min", 1.5)),
            lookback=lookback,
        )
        # 添加前瞻评估
        for sig in gap_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(gap_sigs)

    if factor in ("trend_breakout", "all_factors"):
        trend_sigs = detect_trend_breakout_entries(
            df,
            lookback_days=int(kwargs.get("trend_lookback", 20)),
            volume_ratio_min=float(kwargs.get("volume_ratio_min", 1.3)),
            lookback=lookback,
        )
        for sig in trend_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(trend_sigs)

    if factor in ("200d_new_high_breakout", "all_factors"):
        new_high_sigs = detect_200d_new_high_entries(
            df,
            lookback_days=int(kwargs.get("new_high_lookback_days", 199)),
            volume_ratio_min=float(kwargs.get("new_high_volume_ratio_min", 0.0)),
            lookback=lookback,
        )
        for sig in new_high_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(new_high_sigs)

    if factor in ("vol_contraction", "all_factors"):
        vol_sigs = detect_volatility_contraction_entries(
            df,
            bb_period=int(kwargs.get("bb_period", 20)),
            squeeze_threshold=float(kwargs.get("squeeze_threshold", 0.05)),
            lookback=lookback,
        )
        for sig in vol_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(vol_sigs)

    if factor in ("candle_run", "all_factors"):
        run_sigs = detect_candle_run_entries(
            df,
            run_count=int(kwargs.get("run_count", 3)),
            min_body_ratio=float(kwargs.get("body_ratio", 0.5)),
            lookback=lookback,
        )
        for sig in run_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(run_sigs)

    if factor in ("candle_accel", "all_factors"):
        accel_sigs = detect_candle_accel_entries(
            df,
            run_count=int(kwargs.get("accel_count", 3)),
            lookback=lookback,
        )
        for sig in accel_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(accel_sigs)

    return signals


def _detect_all_signals(df, symbol, freq_label, signal_group, lookback, factor,
                        gap_pct_min, volume_ratio_min, trend_lookback, bb_period, squeeze_threshold,
                        run_count=3, body_ratio=0.5, accel_count=3):
    """统一信号检测 — 返回 (signals, bi_list, zhongshu, warnings)"""
    all_signals = []
    bi_list = []
    zhongshu = []
    warnings = []

    if signal_group in ("macd", "all"):
        macd_lookback = min(lookback, len(df) - 35)
        macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback)
        all_signals.extend(macd_sigs)

    if signal_group in ("czsc", "all"):
        try:
            czsc_sigs, bi_list, zhongshu = _detect_czsc(
                df,
                symbol,
                freq_label,
                lookback=lookback,
                include_auxiliary=signal_group != "all",
            )
            all_signals.extend(czsc_sigs)
        except Exception as czsc_err:
            logger.warning("缠论信号检测失败: %s", czsc_err)
            warnings.append(f"缠论信号检测失败: {str(czsc_err)[:80]}")

    effective_factor = factor if factor else ("all_factors" if signal_group == "all" else "")
    if effective_factor:
        try:
            factor_sigs = _detect_entry_factors(
                df, effective_factor, lookback,
                gap_pct_min=gap_pct_min, volume_ratio_min=volume_ratio_min,
                trend_lookback=trend_lookback, bb_period=bb_period,
                squeeze_threshold=squeeze_threshold,
                run_count=run_count, body_ratio=body_ratio, accel_count=accel_count,
            )
            all_signals.extend(factor_sigs)
        except Exception as factor_err:
            logger.warning("入场因子检测失败: %s", factor_err)
            warnings.append(f"入场因子检测失败: {str(factor_err)[:80]}")

    all_signals.sort(key=lambda s: s["dt"])
    return all_signals, bi_list, zhongshu, warnings


def _annotate_signals_ma_vol(df: pd.DataFrame, signals: list):
    """
    给每个信号补充 MA 位置和量能状态标注。

    在信号 dict 中新增:
    - ma_status: "MA21支撑(+1.2%)" / "MA13阻力" / "多头排列" / ""
    - volume_status: "放量(2.1σ)" / "缩量(-1.8σ)" / "正常"
    - ma_confirmed: bool (MA支撑/阻力确认)
    - vol_confirmed: bool (放量确认)
    """
    if not signals or df.empty:
        return

    # 预计算MA
    from signals.core.ma_levels import KEY_MA_PERIODS

    ma_periods = {f"MA{period}": period for period in KEY_MA_PERIODS}
    for name, period in ma_periods.items():
        if len(df) >= period:
            df[name] = df["close"].rolling(period).mean()

    # 预计算量能z-score (20日滚动)
    if "vol" in df.columns and len(df) >= 25:
        vol_mean = df["vol"].rolling(20).mean()
        vol_std = df["vol"].rolling(20).std()
        df["vol_z"] = (df["vol"] - vol_mean) / vol_std.replace(0, 1)

    for sig in signals:
        sig["ma_status"] = ""
        sig["volume_status"] = ""
        sig["ma_confirmed"] = False
        sig["vol_confirmed"] = False

        # 找到信号对应的 df 位置
        sig_dt = sig.get("dt")
        if sig_dt is None:
            continue

        try:
            from datetime import datetime
            if isinstance(sig_dt, (int, float)):
                sig_time = datetime.fromtimestamp(sig_dt)
            else:
                sig_time = sig_dt

            idx = df.index.get_indexer([sig_time], method="nearest")[0]
            if idx < 0 or idx >= len(df):
                continue
        except Exception:
            continue

        row = df.iloc[idx]
        price = float(row["close"])
        if not math.isfinite(price):
            continue

        # MA标注: 找最近的MA支撑
        best_ma = None
        best_dist = 999
        for name in ma_periods:
            if name not in df.columns:
                continue
            ma_val = row.get(name)
            if pd.isna(ma_val):
                continue
            ma_number = float(ma_val)
            if not math.isfinite(ma_number) or ma_number == 0:
                continue
            dist_pct = (price - ma_number) / ma_number * 100
            # 在MA附近5%以内 = 有锚点
            if abs(dist_pct) <= 5.0 and abs(dist_pct) < best_dist:
                best_dist = abs(dist_pct)
                position = "支撑" if dist_pct >= 0 else "下方"
                best_ma = f"{name}{position}({dist_pct:+.1f}%)"
                sig["ma_confirmed"] = True

        if best_ma:
            sig["ma_status"] = best_ma
        else:
            # 判断均线排列
            ma_vals = {}
            for name in ["MA5", "MA13", "MA21"]:
                if name in df.columns and not pd.isna(row.get(name)):
                    ma_vals[name] = float(row[name])
            if len(ma_vals) >= 3:
                vals = list(ma_vals.values())
                if vals[0] > vals[1] > vals[2]:
                    sig["ma_status"] = "多头排列"
                elif vals[0] < vals[1] < vals[2]:
                    sig["ma_status"] = "空头排列"
                else:
                    sig["ma_status"] = "交织"

        # 量能标注
        if "vol_z" in df.columns:
            vol_z = row.get("vol_z")
            if not pd.isna(vol_z):
                if vol_z >= 2.0:
                    sig["volume_status"] = f"放量({vol_z:.1f}σ)"
                    sig["vol_confirmed"] = True
                elif vol_z <= -1.5:
                    sig["volume_status"] = f"缩量({vol_z:.1f}σ)"
                else:
                    sig["volume_status"] = "正常"

    # 清理临时列
    for col in list(ma_periods.keys()) + ["vol_z"]:
        if col in df.columns:
            df.drop(columns=[col], inplace=True, errors="ignore")


async def analyze_backtest_payload(
    *,
    code: str,
    freq: str = "daily",
    signal_group: str = "all",
    lookback: int = 999,
    factor: str = "",
    gap_pct_min: float = 2.0,
    volume_ratio_min: float = 1.5,
    trend_lookback: int = 20,
    bb_period: int = 20,
    squeeze_threshold: float = 0.05,
    run_count: int = 3,
    body_ratio: float = 0.5,
    accel_count: int = 3,
    stop_loss: float = 5.0,
    trail_stop: float = 50.0,
    max_hold: int = 20,
    slippage: float = 0.1,
    take_profit: float = 0,
    ma_exit_period: int = 0,
    profit_drawdown: float = 0,
    batch_exit: str = "0",
    batch1_ratio: float = 50,
    batch1_target: float = 5,
    batch2_target: float = 10,
    atr_exit_period: int = 0,
    atr_exit_mult: float = 2.0,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any] | JSONResponse:
    """Pure Python backtest analysis entrypoint shared by API and services."""
    from signals.core.trade_simulator import SimConfig, simulate_trades
    import dataclasses

    try:
        code = code.strip()
        freq = _normalize_freq(freq)
        if freq not in {"daily", "weekly", "monthly", "30m"}:
            return JSONResponse(status_code=400, content={
                "error": f"不支持的周期: {freq}",
                "supported_freqs": ["daily", "weekly", "monthly", "30m"],
            })
        if _is_unsupported_backtest_code(code):
            return JSONResponse(status_code=422, content={
                "error": f"当前回测暂不支持北交所/新三板标的: {_plain_stock_code(code)}",
                "code": _plain_stock_code(code),
                "market": "BJ",
            })
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = _freq_label(freq)

        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={
                "error": f"无法获取 {code} 的{freq_label}数据",
                "data_source": df.attrs.get("data_source", ""),
                "last_upstream_error": df.attrs.get("last_upstream_error", ""),
            })
        if lookback > 0 and len(df) > lookback:
            df = df.tail(lookback).copy()
        try:
            analysis_df, visible_df, date_scope = _resolve_backtest_date_scope(
                df,
                start_date=start_date,
                end_date=end_date,
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if analysis_df.empty or visible_df.empty:
            return JSONResponse(status_code=404, content={
                "error": f"{code} 在指定日期区间内无{freq_label}数据",
                "date_range": date_scope,
            })

        meta = _kline_meta(visible_df, freq)
        data_source = meta["data_source"]
        data_source_detail = meta["data_source_detail"]

        all_signals, bi_list, zhongshu, warnings = _detect_all_signals(
            analysis_df, symbol, freq_label, signal_group, lookback, factor,
            gap_pct_min, volume_ratio_min, trend_lookback, bb_period, squeeze_threshold,
            run_count=run_count, body_ratio=body_ratio, accel_count=accel_count,
        )
        all_signals = _filter_signals_by_date_scope(
            all_signals,
            start_date=start_date,
            end_date=end_date,
        )

        _annotate_signals_ma_vol(analysis_df, all_signals)

        ohlcv = _serialize_ohlcv(visible_df)
        macd_data = _compute_macd_data(visible_df)
        ma_lines = _compute_ma_lines(visible_df)
        kpi = _compute_kpi(all_signals)

        sim_kwargs = dict(
            stop_loss_pct=stop_loss, trail_stop_pct=trail_stop,
            max_hold_days=max_hold, slippage=slippage / 100.0,
        )
        if take_profit > 0: sim_kwargs["take_profit_pct"] = take_profit
        if ma_exit_period > 0: sim_kwargs["ma_exit_period"] = ma_exit_period
        if profit_drawdown > 0: sim_kwargs["profit_drawdown_pct"] = profit_drawdown
        if atr_exit_period > 0:
            sim_kwargs["atr_exit_period"] = atr_exit_period
            sim_kwargs["atr_exit_mult"] = atr_exit_mult
        if batch_exit == "1":
            sim_kwargs["batch_exit_enabled"] = True
            sim_kwargs["batch_exit_ratios"] = [batch1_ratio / 100.0, (100 - batch1_ratio) / 100.0]
            sim_kwargs["batch_exit_targets"] = [batch1_target, batch2_target]

        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim = simulate_trades(analysis_df, all_signals, SimConfig(**sim_kwargs))

        result = {
            "symbol": symbol, "code": code, "freq": freq_label,
            **meta,
            "date_range": date_scope,
            "ohlcv": ohlcv, "macd": macd_data, "ma_lines": ma_lines,
            "signals": all_signals, "kpi": kpi,
            "date_presets": _get_date_presets(),
            "bi_list": bi_list, "zhongshu": zhongshu,
            "sim_trades": sim.trades, "sim_equity": sim.equity_curve,
            "sim_kpi": sim.kpi, "sim_config": sim.config,
            "sim_skip_reasons": sim.skip_reasons,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if data_source in {
            "disk_cache",
            "daily_cache_resampled_weekly",
            "daily_cache_resampled_monthly",
            "mongodb",
            "mongodb_bars",
            "mongodb_daily_resampled_weekly",
            "mongodb_daily_resampled_monthly",
            "sina_daily_resampled_weekly",
            "sina_daily_resampled_monthly",
        }:
            warnings.append(f"数据源: {data_source_detail or data_source}")
        if not all_signals:
            warnings.append(f"{code} {freq_label} 有K线但暂无命中信号")
        if warnings:
            result["warnings"] = warnings
        result["terminal"] = build_backtest_terminal(result)
        return result

    except Exception as e:
        logger.exception("分析失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "last_upstream_error": str(e),
            "detail": traceback.format_exc(),
        })


@router.get("/analyze")
async def backtest_analyze(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly / monthly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    # 入场因子
    factor: str = Query("", description="入场因子: gap / trend_breakout / 200d_new_high_breakout / vol_contraction / candle_run / candle_accel"),
    gap_pct_min: float = Query(2.0), volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20), bb_period: int = Query(20), squeeze_threshold: float = Query(0.05),
    run_count: int = Query(3), body_ratio: float = Query(0.5), accel_count: int = Query(3),
    # 基础风控
    stop_loss: float = Query(5.0), trail_stop: float = Query(50.0),
    max_hold: int = Query(20), slippage: float = Query(0.1),
    # 高级出场
    take_profit: float = Query(0), ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    batch_exit: str = Query("0"), batch1_ratio: float = Query(50),
    batch1_target: float = Query(5), batch2_target: float = Query(10),
    # ATR 追踪止损
    atr_exit_period: int = Query(0), atr_exit_mult: float = Query(2.0),
    start_date: str = Query("", description="可选统计起始日期 YYYY-MM-DD"),
    end_date: str = Query("", description="可选统计结束日期 YYYY-MM-DD"),
):
    """
    一体化分析 — 信号检测 + 交易模拟，一次请求返回全部数据。
    """
    return await analyze_backtest_payload(
        code=code,
        freq=freq,
        signal_group=signal_group,
        lookback=lookback,
        factor=factor,
        gap_pct_min=gap_pct_min,
        volume_ratio_min=volume_ratio_min,
        trend_lookback=trend_lookback,
        bb_period=bb_period,
        squeeze_threshold=squeeze_threshold,
        run_count=run_count,
        body_ratio=body_ratio,
        accel_count=accel_count,
        stop_loss=stop_loss,
        trail_stop=trail_stop,
        max_hold=max_hold,
        slippage=slippage,
        take_profit=take_profit,
        ma_exit_period=ma_exit_period,
        profit_drawdown=profit_drawdown,
        batch_exit=batch_exit,
        batch1_ratio=batch1_ratio,
        batch1_target=batch1_target,
        batch2_target=batch2_target,
        atr_exit_period=atr_exit_period,
        atr_exit_mult=atr_exit_mult,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/health/push2his")
async def push2his_health(
    code: str = Query("002759"),
    timeout: int = Query(8, ge=1, le=30),
    live: bool = Query(False, description="显式 live=true 时才直连东财做人工诊断"),
):
    """
    独立探测东财 push2his K 线链路。
    默认只读 provider_health，避免 web2 runtime 请求时同步打外部源。
    """
    import time

    if not live:
        try:
            from signals.data.mongo_fallback import get_db

            db = get_db()
            if db is None:
                return {
                    "status": "pending",
                    "mode": "cached",
                    "live_check": False,
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "checks": [],
                    "error": "mongo_disabled",
                }
            docs = list(db["provider_health"].find(
                {"provider": {"$in": ["eastmoney", "em"]}},
                {"_id": 0},
            ).sort("last_success_at", -1).limit(10))
            status = "healthy" if any(doc.get("status") == "ok" for doc in docs) else "pending"
            return {
                "status": status,
                "mode": "cached",
                "live_check": False,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "checks": docs,
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "mode": "cached",
                "live_check": False,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "checks": [],
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }

    checks = []
    overall_ok = True
    for freq, period in [("daily", "daily"), ("weekly", "weekly")]:
        started = time.perf_counter()
        ok = False
        rows = 0
        error = ""
        try:
            df = _fetch_eastmoney_push2his(
                code=code,
                market=_detect_market(code),
                freq=period,
                start_date=(datetime.now() - timedelta(days=120)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
                timeout=timeout,
            )
            rows = 0 if df is None else len(df)
            ok = rows > 0
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
        duration_ms = int((time.perf_counter() - started) * 1000)
        overall_ok = overall_ok and ok
        checks.append({
            "source": "eastmoney_push2his",
            "code": code,
            "freq": freq,
            "ok": ok,
            "rows": rows,
            "duration_ms": duration_ms,
            "error": error,
        })
    return {
        "status": "healthy" if overall_ok else "degraded",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }


@router.get("/scan")
async def backtest_scan(
    code: str = Query(...), freq: str = Query("daily"),
    signal_group: str = Query("all"), lookback: int = Query(999),
    factor: str = Query(""), gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5), trend_lookback: int = Query(20),
    bb_period: int = Query(20), squeeze_threshold: float = Query(0.05),
    run_count: int = Query(3), body_ratio: float = Query(0.5), accel_count: int = Query(3),
    stop_loss: float = Query(5.0), trail_stop: float = Query(50.0),
    max_hold: int = Query(20), slippage: float = Query(0.1),
    take_profit: float = Query(0), ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    atr_exit_period: int = Query(0), atr_exit_mult: float = Query(2.0),
    scan_param: str = Query(""), scan_values: str = Query(""),
    scan_param2: str = Query(""), scan_values2: str = Query(""),
    scan_metric: str = Query("sharpe"),
):
    """独立参数扫描端点 — 避免阻塞主分析请求。"""
    from signals.core.trade_simulator import SimConfig, run_parameter_scan
    import dataclasses

    try:
        code = code.strip()
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = "日线" if freq == "daily" else "周线"

        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={"error": f"无法获取 {code} 的{freq_label}数据"})

        all_signals, _, _, _ = _detect_all_signals(
            df, symbol, freq_label, signal_group, lookback, factor,
            gap_pct_min, volume_ratio_min, trend_lookback, bb_period, squeeze_threshold,
            run_count=run_count, body_ratio=body_ratio, accel_count=accel_count,
        )

        sim_kwargs = dict(
            stop_loss_pct=stop_loss, trail_stop_pct=trail_stop,
            max_hold_days=max_hold, slippage=slippage / 100.0,
        )
        if take_profit > 0: sim_kwargs["take_profit_pct"] = take_profit
        if ma_exit_period > 0: sim_kwargs["ma_exit_period"] = ma_exit_period
        if profit_drawdown > 0: sim_kwargs["profit_drawdown_pct"] = profit_drawdown
        if atr_exit_period > 0:
            sim_kwargs["atr_exit_period"] = atr_exit_period
            sim_kwargs["atr_exit_mult"] = atr_exit_mult

        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim_config = SimConfig(**sim_kwargs)

        if not scan_param or not scan_values:
            return {"error": "请指定扫描参数和取值"}

        values1 = [float(v.strip()) for v in scan_values.split(",") if v.strip()]
        values2 = None
        p2_name = None
        if scan_param2 and scan_values2:
            values2 = [float(v.strip()) for v in scan_values2.split(",") if v.strip()]
            p2_name = scan_param2

        scan_result = run_parameter_scan(
            df, all_signals, sim_config,
            param1_name=scan_param, param1_values=values1,
            param2_name=p2_name, param2_values=values2,
            metric=scan_metric,
        )
        scan_result["metric"] = scan_metric
        scan_result["terminal"] = build_scan_terminal(scan_result, context={
            "symbol": symbol,
            "code": code,
            "market": market,
            "freq": freq_label,
            "bar_count": len(df),
            "data_source": df.attrs.get("data_source", ""),
            "freshness": "fresh" if df.attrs.get("data_source") else "unknown",
        })
        return scan_result

    except Exception as e:
        logger.exception("参数扫描失败: code=%s", code)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/run")
async def backtest_run(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly / monthly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    # Phase 2: 入场因子
    factor: str = Query("", description="入场因子: gap / trend_breakout / 200d_new_high_breakout / vol_contraction"),
    gap_pct_min: float = Query(2.0, description="跳空幅度阈值%"),
    volume_ratio_min: float = Query(1.5, description="量比阈值"),
    trend_lookback: int = Query(20, description="趋势突破回看天数"),
    bb_period: int = Query(20, description="布林带周期"),
    squeeze_threshold: float = Query(0.05, description="挤压阈值"),
):
    """
    通用信号回测 — 支持 MACD / 缠论 / 入场因子。
    """
    try:
        code = code.strip()
        freq = _normalize_freq(freq)
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = _freq_label(freq)

        # 1. 拉取K线
        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={
                "error": f"无法获取 {code} 的{freq_label}数据",
                "data_source": df.attrs.get("data_source", ""),
                "last_upstream_error": df.attrs.get("last_upstream_error", ""),
            })
        meta = _kline_meta(df, freq)

        # 2. 信号检测
        all_signals = []
        bi_list = []
        zhongshu = []
        warnings = []

        if signal_group in ("macd", "all"):
            macd_lookback = min(lookback, len(df) - 35)
            macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback)
            all_signals.extend(macd_sigs)

        if signal_group in ("czsc", "all"):
            try:
                czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label)
                all_signals.extend(czsc_sigs)
            except Exception as czsc_err:
                logger.warning("缠论信号检测失败: %s", czsc_err)
                warnings.append(f"缠论信号检测失败: {str(czsc_err)[:80]}")

        # Phase 2: 入场因子（选了具体因子则只跑该因子，"全部信号"时跑所有因子）
        effective_factor = factor if factor else ("all_factors" if signal_group == "all" else "")
        if effective_factor:
            try:
                factor_sigs = _detect_entry_factors(
                    df, effective_factor, lookback,
                    gap_pct_min=gap_pct_min,
                    volume_ratio_min=volume_ratio_min,
                    trend_lookback=trend_lookback,
                    bb_period=bb_period,
                    squeeze_threshold=squeeze_threshold,
                )
                all_signals.extend(factor_sigs)
            except Exception as factor_err:
                logger.warning("入场因子检测失败: %s", factor_err)
                warnings.append(f"入场因子检测失败: {str(factor_err)[:80]}")

        # 3. 按时间排序
        all_signals.sort(key=lambda s: s["dt"])

        # 4. 序列化图表数据
        ohlcv = _serialize_ohlcv(df)
        macd_data = _compute_macd_data(df)
        ma_lines = _compute_ma_lines(df)

        # 5. KPI
        kpi = _compute_kpi(all_signals)

        # 6. 日期预设
        date_presets = _get_date_presets()

        result = {
            "symbol": symbol,
            "code": code,
            "freq": freq_label,
            **meta,
            "ohlcv": ohlcv,
            "macd": macd_data,
            "ma_lines": ma_lines,
            "signals": all_signals,
            "kpi": kpi,
            "date_presets": date_presets,
            "bi_list": bi_list,
            "zhongshu": zhongshu,
        }
        data_source = meta.get("data_source", "")
        if data_source in {
            "disk_cache",
            "daily_cache_resampled_weekly",
            "daily_cache_resampled_monthly",
            "mongodb",
            "mongodb_bars",
            "mongodb_daily_resampled_weekly",
            "mongodb_daily_resampled_monthly",
            "sina_daily_resampled_weekly",
            "sina_daily_resampled_monthly",
        }:
            warnings.append(f"数据源: {meta.get('data_source_detail') or data_source}")
        if warnings:
            result["warnings"] = warnings
        return result

    except Exception as e:
        logger.exception("回测失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "detail": traceback.format_exc(),
        })


@router.get("/simulate")
async def backtest_simulate(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    # 基础风控
    stop_loss: float = Query(5.0, description="止损百分比"),
    trail_stop: float = Query(50.0, description="移动止盈回撤百分比"),
    max_hold: int = Query(20, description="最大持仓天数"),
    slippage: float = Query(0.1, description="滑点百分比"),
    # Phase 2: 入场因子
    factor: str = Query("", description="入场因子"),
    gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20),
    bb_period: int = Query(20),
    squeeze_threshold: float = Query(0.05),
    # Phase 3: 高级出场
    take_profit: float = Query(0, description="固定止盈%"),
    ma_exit_period: int = Query(0, description="均线离场周期"),
    profit_drawdown: float = Query(0, description="利润回撤%"),
    batch_exit: str = Query("0", description="分批出场 0/1"),
    batch1_ratio: float = Query(50, description="第1批仓位%"),
    batch1_target: float = Query(5, description="第1批目标%"),
    batch2_target: float = Query(10, description="第2批目标%"),
    # Phase 4: 参数扫描 (1D/2D)
    scan_param: str = Query("", description="扫描维度1"),
    scan_values: str = Query("", description="维度1取值"),
    scan_param2: str = Query("", description="扫描维度2"),
    scan_values2: str = Query("", description="维度2取值"),
    scan_metric: str = Query("sharpe", description="优化目标"),
):
    """
    交易模拟回测 — 支持高级出场 + 分批出场 + 2D参数扫描。
    """
    from signals.core.trade_simulator import SimConfig, simulate_trades, run_parameter_scan

    try:
        code = code.strip()
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = "日线" if freq == "daily" else "周线"

        # 1. 拉取K线
        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={
                "error": f"无法获取 {code} 的{freq_label}数据"
            })

        # 2. 信号检测
        all_signals = []
        bi_list = []
        zhongshu = []
        warnings = []

        if signal_group in ("macd", "all"):
            macd_lookback = min(lookback, len(df) - 35)
            macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback)
            all_signals.extend(macd_sigs)

        if signal_group in ("czsc", "all"):
            try:
                czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label)
                all_signals.extend(czsc_sigs)
            except Exception as czsc_err:
                logger.warning("缠论信号检测失败: %s", czsc_err)
                warnings.append(f"缠论信号检测失败: {str(czsc_err)[:80]}")

        # Phase 2: 入场因子（选了具体因子则只跑该因子，"全部信号"时跑所有因子）
        effective_factor = factor if factor else ("all_factors" if signal_group == "all" else "")
        if effective_factor:
            try:
                factor_sigs = _detect_entry_factors(
                    df, effective_factor, lookback,
                    gap_pct_min=gap_pct_min,
                    volume_ratio_min=volume_ratio_min,
                    trend_lookback=trend_lookback,
                    bb_period=bb_period,
                    squeeze_threshold=squeeze_threshold,
                )
                all_signals.extend(factor_sigs)
            except Exception as factor_err:
                logger.warning("入场因子检测失败: %s", factor_err)
                warnings.append(f"入场因子检测失败: {str(factor_err)[:80]}")

        all_signals.sort(key=lambda s: s["dt"])

        # 3. 构建模拟配置
        sim_kwargs = dict(
            stop_loss_pct=stop_loss,
            trail_stop_pct=trail_stop,
            max_hold_days=max_hold,
            slippage=slippage / 100.0,
        )
        # Phase 3: 高级出场字段 (仅当 SimConfig 支持时)
        if take_profit > 0:
            sim_kwargs["take_profit_pct"] = take_profit
        if ma_exit_period > 0:
            sim_kwargs["ma_exit_period"] = ma_exit_period
        if profit_drawdown > 0:
            sim_kwargs["profit_drawdown_pct"] = profit_drawdown
        if batch_exit == "1":
            sim_kwargs["batch_exit_enabled"] = True
            sim_kwargs["batch_exit_ratios"] = [batch1_ratio / 100.0, (100 - batch1_ratio) / 100.0]
            sim_kwargs["batch_exit_targets"] = [batch1_target, batch2_target]

        # 过滤掉 SimConfig 不支持的字段
        import dataclasses
        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim_config = SimConfig(**sim_kwargs)

        # 4. 参数扫描
        scan_result = None
        if scan_param and scan_values:
            try:
                values1 = [float(v.strip()) for v in scan_values.split(",") if v.strip()]
                values2 = None
                p2_name = None
                if scan_param2 and scan_values2:
                    values2 = [float(v.strip()) for v in scan_values2.split(",") if v.strip()]
                    p2_name = scan_param2

                if values1:
                    scan_result = run_parameter_scan(
                        df, all_signals, sim_config,
                        param1_name=scan_param,
                        param1_values=values1,
                        param2_name=p2_name,
                        param2_values=values2,
                        metric=scan_metric,
                    )
            except Exception as e:
                logger.warning("参数扫描失败: %s", e)
                scan_result = {"error": str(e)}

        # 5. 单次模拟
        sim = simulate_trades(df, all_signals, sim_config)

        # 6. 序列化图表数据
        ohlcv = _serialize_ohlcv(df)
        macd_data = _compute_macd_data(df)
        ma_lines = _compute_ma_lines(df)

        # 7. 原始 KPI
        forward_kpi = _compute_kpi(all_signals)

        result = {
            "symbol": symbol,
            "code": code,
            "freq": freq_label,
            "ohlcv": ohlcv,
            "macd": macd_data,
            "ma_lines": ma_lines,
            "signals": all_signals,
            "bi_list": bi_list,
            "zhongshu": zhongshu,
            "forward_kpi": forward_kpi,
            "sim_trades": sim.trades,
            "sim_equity": sim.equity_curve,
            "sim_kpi": sim.kpi,
            "sim_config": sim.config,
            "sim_skip_reasons": sim.skip_reasons,
            "date_presets": _get_date_presets(),
        }

        if scan_result:
            result["scan"] = scan_result
        if warnings:
            result["warnings"] = warnings
        result["terminal"] = build_backtest_terminal(result, scan=scan_result)

        return result

    except Exception as e:
        logger.exception("模拟回测失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "detail": traceback.format_exc(),
        })


@router.post("/push")
async def backtest_push(request: Request):
    """将回测结果格式化后推送到微信。"""
    try:
        data = await request.json()
        from signals.notify.backtest_notify import push_backtest_report
        ok = push_backtest_report(data)
        return {"ok": ok}
    except Exception as e:
        logger.warning("回测推送失败: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/report")
async def backtest_report(request: Request, format: str = Query("html", description="html / pdf")):
    """将当前回测结果生成 HTML/PDF 报告附件。"""
    try:
        data = await request.json()
        fmt = format.lower().lstrip(".")
        from signals.core.backtest_report import render_backtest_report, report_filename

        content = render_backtest_report(data, fmt)
        media_type = "application/pdf" if fmt == "pdf" else "text/html; charset=utf-8"
        filename = report_filename(data, fmt)
        return StreamingResponse(
            BytesIO(content),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.exception("回测报告生成失败")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/export")
async def backtest_export(
    code: str = Query(...),
    freq: str = Query("daily"),
    signal_group: str = Query("all"),
    lookback: int = Query(999),
    stop_loss: float = Query(5.0),
    trail_stop: float = Query(50.0),
    max_hold: int = Query(20),
    slippage: float = Query(0.1),
    factor: str = Query(""),
    gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20),
    bb_period: int = Query(20),
    squeeze_threshold: float = Query(0.05),
    take_profit: float = Query(0),
    ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    batch_exit: str = Query("0"),
    batch1_ratio: float = Query(50),
    batch1_target: float = Query(5),
    batch2_target: float = Query(10),
):
    """Phase 5: CSV 导出"""
    from fastapi.responses import StreamingResponse
    import io
    import csv

    try:
        analysis = await analyze_backtest_payload(
            code=code,
            freq=freq,
            signal_group=signal_group,
            lookback=lookback,
            factor=factor,
            gap_pct_min=gap_pct_min,
            volume_ratio_min=volume_ratio_min,
            trend_lookback=trend_lookback,
            bb_period=bb_period,
            squeeze_threshold=squeeze_threshold,
            stop_loss=stop_loss,
            trail_stop=trail_stop,
            max_hold=max_hold,
            slippage=slippage,
            take_profit=take_profit,
            ma_exit_period=ma_exit_period,
            profit_drawdown=profit_drawdown,
            batch_exit=batch_exit,
            batch1_ratio=batch1_ratio,
            batch1_target=batch1_target,
            batch2_target=batch2_target,
        )
        if isinstance(analysis, JSONResponse):
            return analysis
        terminal = analysis.get("terminal") or build_backtest_terminal(analysis)

        # 构建 CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["section", "key", "value", "detail"])
        for key, value in (terminal.get("target") or {}).items():
            writer.writerow(["target", key, value, ""])
        for key, value in (terminal.get("market_snapshot") or {}).items():
            writer.writerow(["market_snapshot", key, value, ""])
        for key, value in (terminal.get("trade_assumptions") or {}).items():
            writer.writerow(["trade_assumptions", key, value, ""])
        metric_keys = [
            "total_return_pct", "benchmark_return_pct", "excess_return_pct", "annual_return_pct",
            "max_drawdown_pct", "volatility_pct", "sharpe", "calmar",
            "filled_trades", "win_rate", "profit_factor", "expectancy_pct",
            "avg_win_pct", "avg_loss_pct", "avg_holding_days", "max_consecutive_losses",
            "exposure_pct", "signal_count", "evaluated_count", "avg_t5_pct", "avg_t10_pct",
            "avg_mfe_pct", "avg_mae_pct",
        ]
        metrics = terminal.get("metrics") or {}
        for key in metric_keys:
            writer.writerow(["metrics", key, metrics.get(key, ""), ""])
        writer.writerow([])
        writer.writerow([
            "信号日", "类型", "组", "入场日", "入场价", "成交方式",
            "出场日", "出场价", "出场原因", "持仓天",
            "毛利%", "净利%", "成本%", "MFE%", "MAE%", "跳过原因",
        ])
        for t in ((terminal.get("panels") or {}).get("trades") or {}).get("rows", []):
            writer.writerow([
                t.get("signal_date", ""), t.get("signal_type", ""), t.get("signal_group", ""),
                t.get("entry_date", ""), t.get("entry_price", ""), t.get("fill_type", ""),
                t.get("exit_date", ""), t.get("exit_price", ""), t.get("exit_reason", ""),
                t.get("holding_days", ""),
                t.get("return_pct", ""), t.get("net_return_pct", ""), t.get("cost_pct", ""),
                t.get("mfe_pct", ""), t.get("mae_pct", ""), t.get("skip_reason", ""),
            ])

        output.seek(0)
        target = terminal.get("target") or {}
        filename = f"backtest_{target.get('code') or code}_{datetime.now().strftime('%Y%m%d')}.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        logger.exception("导出失败: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


def _batch_worker_count(total_codes: int) -> int:
    if total_codes <= 1:
        return 1
    try:
        configured = int(os.getenv("SIGNALS_BACKTEST_BATCH_WORKERS", "0"))
    except ValueError:
        configured = 0
    if configured > 0:
        return max(1, min(total_codes, configured))
    cpu_count = os.cpu_count() or 4
    return max(1, min(total_codes, max(2, min(8, cpu_count))))


def _parse_batch_codes(codes_raw: Any) -> list[str]:
    if isinstance(codes_raw, str):
        return [c.strip() for c in codes_raw.replace("\n", ",").split(",") if c.strip()]
    if isinstance(codes_raw, list):
        return [str(c).strip() for c in codes_raw if str(c).strip()]
    return []


def _int_body_value(body: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(body.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _bool_body_value(body: dict[str, Any], key: str, default: bool = False) -> bool:
    value = body.get(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_batch_codes(body: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Resolve manual batch codes or a named market universe such as all_etf."""
    from signals.core.etf_universe import all_market_etf_universe, is_all_etf_universe_token

    codes = _parse_batch_codes(body.get("codes", ""))
    universe_token = str(body.get("universe") or "").strip()
    if not universe_token and len(codes) == 1 and is_all_etf_universe_token(codes[0]):
        universe_token = codes[0]

    if is_all_etf_universe_token(universe_token):
        try:
            from signals.data.mongo_fallback import get_db

            db = get_db()
        except Exception:
            db = None
        limit = _int_body_value(body, "universe_limit", _int_env("SIGNALS_BACKTEST_ALL_ETF_LIMIT", 0))
        require_daily_bars = _bool_body_value(
            body,
            "require_daily_bars",
            _bool_env("SIGNALS_BACKTEST_ALL_ETF_REQUIRE_DAILY_BARS", True),
        )
        universe = all_market_etf_universe(
            db=db,
            include_live=True,
            limit=limit,
            require_daily_bars=require_daily_bars,
        )
        resolved_codes = [str(code) for code in universe.get("codes") or [] if str(code)]
        meta = {
            "type": "all_etf",
            "requested": universe_token,
            "total": universe.get("total", len(resolved_codes)),
            "backtest_ready_total": universe.get("backtest_ready_total", len(resolved_codes)),
            "require_daily_bars": universe.get("require_daily_bars", require_daily_bars),
            "selected": len(resolved_codes),
            "limit": universe.get("limit", limit),
            "source": universe.get("source", ""),
            "source_counts": universe.get("source_counts", {}),
            "as_of": universe.get("as_of", ""),
            "warnings": universe.get("warnings", []),
        }
        return resolved_codes, meta

    return codes, {}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_batch_stock_name(resolver: Any, symbol: str, code: str) -> str:
    if resolver is None:
        return code
    try:
        return resolver.get_name(symbol)
    except Exception:
        return code


def _batch_stock_result(
    code: str,
    *,
    freq: str,
    signal_group: str,
    lookback: int,
    factor: str,
    gap_pct_min: float,
    volume_ratio_min: float,
    trend_lookback: int,
    bb_period: int,
    squeeze_threshold: float,
    run_count: int,
    body_ratio: float,
    accel_count: int,
    sim_config: Any,
    resolver: Any,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    from signals.core.trade_simulator import simulate_trades

    code = _plain_stock_code(code)
    stock_result: dict[str, Any] = {"code": code, "status": "ok"}
    try:
        freq_norm = _normalize_freq(freq)
        freq_label = _freq_label(freq_norm)
        if _is_unsupported_backtest_code(code):
            symbol = f"BJ.{code}"
            stock_result.update({
                "status": "error",
                "symbol": symbol,
                "market": "BJ",
                "freq": freq_label,
                "error": "北交所/新三板标的暂不支持当前回测数据源",
                "name": _resolve_batch_stock_name(resolver, symbol, code),
            })
            return stock_result

        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        stock_result.update({
            "symbol": symbol,
            "market": market,
            "freq": freq_label,
            "name": _resolve_batch_stock_name(resolver, symbol, code),
        })

        df = _fetch_kline(code, market, freq_norm)
        if df.empty:
            stock_result["status"] = "error"
            stock_result["error"] = "无可用数据"
            return stock_result
        if lookback > 0 and len(df) > lookback:
            df = df.tail(lookback).copy()

        try:
            analysis_df, visible_df, date_scope = _resolve_backtest_date_scope(
                df,
                start_date=start_date,
                end_date=end_date,
            )
        except ValueError as exc:
            stock_result["status"] = "error"
            stock_result["error"] = str(exc)
            return stock_result
        if analysis_df.empty or visible_df.empty:
            stock_result["status"] = "error"
            stock_result["error"] = "指定日期区间内无可用数据"
            stock_result["date_range"] = date_scope
            return stock_result

        meta = _kline_meta(visible_df, freq_norm)
        stock_result.update(meta)
        stock_result["date_range"] = date_scope
        stock_result["bar_count"] = int(len(visible_df))
        stock_result["ohlcv_tail"] = _serialize_ohlcv(visible_df.tail(520))

        all_signals, _, _, _ = _detect_all_signals(
            analysis_df, symbol, freq_label, signal_group, lookback, factor,
            gap_pct_min, volume_ratio_min, trend_lookback, bb_period, squeeze_threshold,
            run_count=run_count, body_ratio=body_ratio, accel_count=accel_count,
        )
        all_signals = _filter_signals_by_date_scope(
            all_signals,
            start_date=start_date,
            end_date=end_date,
        )
        sim = simulate_trades(analysis_df, all_signals, sim_config)
        kpi = sim.kpi
        stock_result["signal_count"] = len(all_signals)
        stock_result["trade_count"] = kpi.get("filled_trades", 0)
        stock_result["win_rate"] = kpi.get("win_rate", 0)
        stock_result["expectancy"] = kpi.get("expectancy", 0)
        stock_result["total_return"] = kpi.get("total_return_pct", 0)
        stock_result["max_drawdown"] = kpi.get("max_drawdown_pct", 0)
        stock_result["sharpe"] = kpi.get("sharpe", 0)
        stock_result["avg_hold_days"] = kpi.get("avg_hold_days", 0)
        stock_result["signals"] = all_signals
        stock_result["sim_trades"] = sim.trades
        stock_result["signal_breakdown"] = _batch_signal_breakdown(
            code,
            stock_result.get("name") or code,
            all_signals,
            sim.trades,
        )
    except Exception as e:
        stock_result["status"] = "error"
        stock_result["error"] = str(e)[:100]
        logger.warning("批量回测 %s 失败: %s", code, e)

    return stock_result


@router.post("/batch")
async def backtest_batch(request: Request):
    """
    股票池批量回测 — 对多只股票跑同一套信号+模拟参数，返回汇总对比。

    Body JSON:
    {
        "codes": "002759,000001,600519" 或 ["002759", "000001"],
        "freq": "daily",
        "signal_group": "all",
        "factor": "",
        "stop_loss": 5, "trail_stop": 50, "max_hold": 20, "slippage": 0.1,
        ...（与 /analyze 相同的参数）
    }
    """
    from signals.core.trade_simulator import SimConfig, simulate_trades
    import dataclasses

    try:
        body = await request.json()

        codes, universe_meta = _resolve_batch_codes(body)

        if not codes:
            return JSONResponse(status_code=400, content={"error": "请提供至少一只股票代码"})
        if not universe_meta and len(codes) > 20:
            return JSONResponse(status_code=400, content={"error": "最多支持 20 只股票"})

        # 公共参数
        freq = body.get("freq", "daily")
        signal_group = body.get("signal_group", "all")
        lookback = int(body.get("lookback", 999))
        factor = body.get("factor", "")
        gap_pct_min = float(body.get("gap_pct_min", 2.0))
        volume_ratio_min = float(body.get("volume_ratio_min", 1.5))
        trend_lookback = int(body.get("trend_lookback", 20))
        bb_period = int(body.get("bb_period", 20))
        squeeze_threshold = float(body.get("squeeze_threshold", 0.05))
        run_count = int(body.get("run_count", 3))
        body_ratio_val = float(body.get("body_ratio", 0.5))
        accel_count = int(body.get("accel_count", 3))
        start_date = str(body.get("start_date", "") or "").strip()
        end_date = str(body.get("end_date", "") or "").strip()
        try:
            start_ts = _parse_backtest_boundary(start_date)
            end_ts = _parse_backtest_boundary(end_date, end=True)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if start_ts is not None and end_ts is not None and start_ts > end_ts:
            return JSONResponse(status_code=400, content={"error": "start_date 不能晚于 end_date"})

        # 模拟参数
        stop_loss = float(body.get("stop_loss", 5.0))
        trail_stop = float(body.get("trail_stop", 50.0))
        max_hold = int(body.get("max_hold", 20))
        slippage_pct = float(body.get("slippage", 0.1))
        take_profit = float(body.get("take_profit", 0))
        ma_exit_period = int(body.get("ma_exit_period", 0))
        profit_drawdown = float(body.get("profit_drawdown", 0))
        atr_exit_period_val = int(body.get("atr_exit_period", 0))
        atr_exit_mult_val = float(body.get("atr_exit_mult", 2.0))

        # 构建 SimConfig
        sim_kwargs = dict(
            stop_loss_pct=stop_loss, trail_stop_pct=trail_stop,
            max_hold_days=max_hold, slippage=slippage_pct / 100.0,
        )
        if take_profit > 0: sim_kwargs["take_profit_pct"] = take_profit
        if ma_exit_period > 0: sim_kwargs["ma_exit_period"] = ma_exit_period
        if profit_drawdown > 0: sim_kwargs["profit_drawdown_pct"] = profit_drawdown
        if atr_exit_period_val > 0:
            sim_kwargs["atr_exit_period"] = atr_exit_period_val
            sim_kwargs["atr_exit_mult"] = atr_exit_mult_val

        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim_config = SimConfig(**sim_kwargs)

        # 获取股票名称解析器
        try:
            from signals.core.stock_names import get_resolver
            resolver = get_resolver()
        except Exception:
            resolver = None

        worker_count = _batch_worker_count(len(codes))
        worker = partial(
            _batch_stock_result,
            freq=freq,
            signal_group=signal_group,
            lookback=lookback,
            factor=factor,
            gap_pct_min=gap_pct_min,
            volume_ratio_min=volume_ratio_min,
            trend_lookback=trend_lookback,
            bb_period=bb_period,
            squeeze_threshold=squeeze_threshold,
            run_count=run_count,
            body_ratio=body_ratio_val,
            accel_count=accel_count,
            sim_config=sim_config,
            resolver=resolver,
            start_date=start_date,
            end_date=end_date,
        )
        if worker_count <= 1:
            stocks = [worker(code) for code in codes]
        else:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="signals-backtest") as executor:
                stocks = await asyncio.gather(*[
                    loop.run_in_executor(executor, worker, code)
                    for code in codes
                ])

        total_signals = sum(int(stock.get("signal_count") or 0) for stock in stocks)
        total_trades = sum(int(stock.get("trade_count") or 0) for stock in stocks)
        total_wins = sum(
            int(int(stock.get("trade_count") or 0) * float(stock.get("win_rate") or 0) / 100)
            for stock in stocks
        )
        total_evaluated = total_trades

        # 汇总
        overall_win_rate = float(round(total_wins / total_evaluated * 100, 1)) if total_evaluated > 0 else 0
        ok_stocks = [s for s in stocks if s["status"] == "ok" and s.get("trade_count", 0) > 0]
        overall_expectancy = float(round(sum(float(s.get("expectancy") or 0) for s in ok_stocks) / len(ok_stocks), 2)) if ok_stocks else 0

        result = {
            "summary": {
                "total_stocks": len(codes),
                "ok_stocks": len(ok_stocks),
                "total_signals": total_signals,
                "total_trades": total_trades,
                "overall_win_rate": overall_win_rate,
                "overall_expectancy": overall_expectancy,
                "bar_count": sum(int(s.get("bar_count") or 0) for s in stocks),
            },
            "stocks": stocks,
        }
        if universe_meta:
            result["universe"] = universe_meta
        result["terminal"] = build_batch_terminal(result, context={
            "freq": _freq_label(_normalize_freq(freq)),
            "market": "A" if any(_detect_market(code) == "A" for code in codes) else "HK",
            "name": "全量ETF回测复盘" if universe_meta.get("type") == "all_etf" else "",
            "sim_config": dataclasses.asdict(sim_config),
            "benchmark_symbol": body.get("benchmark_symbol", ""),
            "benchmark_name": body.get("benchmark_name", ""),
            "benchmark_phase": body.get("benchmark_phase", ""),
            "benchmark_return_pct": body.get("benchmark_return_pct", 0),
            "date_range": {
                "requested_start_date": start_date,
                "requested_end_date": end_date,
            },
            "date_presets": _get_date_presets(),
        })
        return result

    except Exception as e:
        logger.exception("批量回测失败")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/presets")
async def backtest_presets():
    """返回日期预设列表"""
    return _get_date_presets()
