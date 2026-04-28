# -*- coding: utf-8 -*-
"""
行业成分股同步 — 东财 + THS 双源

数据源:
  Primary: 东财 stock_board_industry_cons_em
  Fallback: 同花顺 stock_board_industry_cons_ths
策略: 每周日全量覆盖
频率: Sunday 10:00
"""
import logging
import os
import atexit
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from functools import lru_cache

import akshare as ak
import pandas as pd
import requests
from pymongo.database import Database

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.board_cons")

_CALL_INTERVAL = 1.0  # 成分股接口限速更严格
_PROVIDER_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="board-cons-provider")
atexit.register(_PROVIDER_TIMEOUT_POOL.shutdown, wait=False, cancel_futures=True)
_SOURCE_UNMAPPED_PREFIX = "source_unmapped"


def _provider_timeout() -> float:
    return float(os.getenv("BOARD_CONS_PROVIDER_TIMEOUT", "12"))


def _call_provider(fn):
    # Keep provider calls in the main worker thread by default. AKShare may load
    # libmini_racer, which has native-crashed when initialized from timeout
    # helper threads on this workstation.
    if os.getenv("BOARD_CONS_PROVIDER_TIMEOUT_MODE", "direct").lower() != "thread":
        return fn()
    future = _PROVIDER_TIMEOUT_POOL.submit(fn)
    try:
        return future.result(timeout=_provider_timeout())
    except FutureTimeout as exc:
        future.cancel()
        raise TimeoutError(f"provider_timeout>{_provider_timeout()}s") from exc


def _batch_size() -> int:
    return max(1, int(os.getenv("BOARD_CONS_BATCH_SIZE", "80")))


def _max_runtime_seconds() -> float:
    return max(30.0, float(os.getenv("BOARD_CONS_MAX_RUNTIME_SECONDS", "720")))


def _progress_interval() -> int:
    return max(1, int(os.getenv("BOARD_CONS_PROGRESS_INTERVAL", "5")))


def _eastmoney_map_max_pages() -> int:
    return max(1, int(os.getenv("BOARD_CONS_EM_MAP_MAX_PAGES", "12")))


def _write_board_cons_progress(
    sync_col,
    *,
    status: str,
    cursor: int,
    start_cursor: int,
    total_count: int,
    processed_groups: int,
    total_stocks: int,
    errors: list,
    unmapped: int,
    source_counts: dict[str, int],
) -> None:
    remaining = max(0, total_count - cursor)
    next_cursor = cursor if remaining else 0
    error_msg = f"{len(errors)} errors; remaining={remaining}" if errors or remaining else ""
    sync_col.update_one(
        {"_id": "board_cons:_meta"},
        {"$set": {
            "module": "board_cons",
            "last_run": datetime.now(),
            "heartbeat_at": datetime.now(),
            "status": status,
            "bar_count": total_stocks,
            "error_msg": error_msg,
            "processed": cursor - start_cursor,
            "processed_groups": processed_groups,
            "remaining": remaining,
            "next_cursor": next_cursor,
            "total_groups": total_count,
            "batch_size": _batch_size(),
            "sample_errors": errors[:10],
            "unmapped": unmapped,
            "source_counts": source_counts,
        }},
        upsert=True,
    )


def _text(value) -> str:
    return str(value or "").strip()


def _unique(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        item = _text(value)
        if item and item not in output:
            output.append(item)
    return output


def _ranking_names(db: Database, collection: str, fields: tuple[str, ...]) -> list[str]:
    latest = db[collection].find_one(
        {"source": "canonical"}, {"dt": 1}, sort=[("dt", -1)])
    query = {"source": "canonical"}
    if latest and latest.get("dt"):
        query["dt"] = latest["dt"]
    names: list[str] = []
    docs = db[collection].find(query, {field: 1 for field in fields}).sort("rank_idx", 1)
    for doc in docs:
        for field in fields:
            value = _text(doc.get(field))
            if value:
                names.append(value)
                break
    return _unique(names)


def _get_board_list(db: Database) -> list:
    """获取行业列表（优先最新 canonical board_ranking）。"""

    boards = _ranking_names(db, "board_ranking", ("board_name", "name", "label"))
    if boards:
        return boards

    # 兼容旧数据：取最新 board_ths source snapshot。
    latest = db["board_ths"].find_one({}, {"dt": 1}, sort=[("dt", -1)])
    if latest and latest.get("dt"):
        docs = db["board_ths"].find({"dt": latest["dt"]}, {"board_name": 1})
        boards = [d["board_name"] for d in docs if d.get("board_name")]
        if boards:
            return boards

    # 兜底：从 AKShare 获取
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None and not df.empty:
            return df["板块名称"].tolist()
    except Exception:
        pass

    return []


def _get_concept_list(db: Database) -> list[str]:
    concepts = _ranking_names(db, "concept_ranking", ("concept_name", "board_name", "concept", "name", "label"))
    if concepts:
        return concepts
    for collection in ("concept_sina", "concept_em", "concept_ths"):
        docs = db[collection].find({}, {"concept_name": 1, "board_name": 1, "concept": 1, "name": 1}).sort("dt", -1).limit(300)
        values: list[str] = []
        for doc in docs:
            for field in ("concept_name", "board_name", "concept", "name"):
                value = _text(doc.get(field))
                if value:
                    values.append(value)
                    break
        concepts = _unique(values)
        if concepts:
            return concepts
    try:
        df = _call_provider(ak.stock_board_concept_name_em)
        if df is not None and not df.empty:
            for column in ("板块名称", "概念名称", "名称"):
                if column in df.columns:
                    return _unique([_text(value) for value in df[column].tolist()])
    except Exception:
        pass
    return []


def _extract_constituents(df) -> tuple[list[str], dict[str, str]]:
    if df is None or df.empty:
        return [], {}
    code_col = "代码" if "代码" in df.columns else "股票代码" if "股票代码" in df.columns else ""
    name_col = "名称" if "名称" in df.columns else "股票简称" if "股票简称" in df.columns else ""
    if not code_col:
        return [], {}
    symbols = [_text(value) for value in df[code_col].tolist()]
    symbols = [value for value in symbols if value]
    stock_names: dict[str, str] = {}
    if name_col:
        stock_names = {
            _text(code): _text(name)
            for code, name in zip(df[code_col].tolist(), df[name_col].tolist())
            if _text(code)
        }
    return symbols, stock_names


def _eastmoney_delay_clist(params: dict) -> list[dict]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        params=params,
        timeout=float(os.getenv("BOARD_CONS_EM_DELAY_TIMEOUT", "8")),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/center/boardlist.html",
        },
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    return data.get("diff") or []


@lru_cache(maxsize=4)
def _eastmoney_board_code_map(kind: str) -> dict[str, str]:
    if kind == "concept":
        fs = "m:90 t:3 f:!50"
        fid = "f12"
    else:
        fs = "m:90 t:2 f:!50"
        fid = "f3"
    mapping: dict[str, str] = {}
    for page in range(1, _eastmoney_map_max_pages() + 1):
        rows = _eastmoney_delay_clist({
            "pn": str(page),
            "pz": "100",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": fid,
            "fs": fs,
            "fields": "f12,f14",
        })
        if not rows:
            break
        for row in rows:
            name = _text(row.get("f14"))
            code = _text(row.get("f12"))
            if name and code:
                mapping[name] = code
        if len(rows) < 100:
            break
    return mapping


def _eastmoney_delay_cons(name: str, *, kind: str):
    code = name if name.startswith("BK") else _eastmoney_board_code_map(kind).get(name, "")
    if not code:
        raise LookupError(f"{_SOURCE_UNMAPPED_PREFIX}:{kind}:{name}")
    rows = _eastmoney_delay_clist({
        "pn": "1",
        "pz": "5000",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12" if kind == "concept" else "f3",
        "fs": f"b:{code} f:!50",
        "fields": "f12,f14",
    })
    return pd.DataFrame([{"代码": row.get("f12"), "名称": row.get("f14")} for row in rows])


def _fetch_board_cons(board_name: str, proxy_url: str = None):
    try:
        return _eastmoney_delay_cons(board_name, kind="board"), "em_delay"
    except Exception as delay_exc:
        if _is_source_unmapped(delay_exc):
            raise
        logger.debug("eastmoney delay board cons failed %s: %s", board_name, delay_exc)
    source = "em"
    try:
        with em_proxy(proxy_url):
            return _call_provider(lambda: ak.stock_board_industry_cons_em(symbol=board_name)), source
    except Exception as em_exc:
        fetch_ths = getattr(ak, "stock_board_industry_cons_ths", None)
        if fetch_ths is None:
            raise RuntimeError(f"em={str(em_exc)[:120]}; ths_unavailable=stock_board_industry_cons_ths") from em_exc
        try:
            source = "ths"
            return _call_provider(lambda: fetch_ths(symbol=board_name)), source
        except Exception as ths_exc:
            raise RuntimeError(f"em={str(em_exc)[:120]}; ths={str(ths_exc)[:120]}") from ths_exc


def _fetch_concept_cons(concept_name: str, proxy_url: str = None):
    try:
        return _eastmoney_delay_cons(concept_name, kind="concept"), "em_delay"
    except Exception as delay_exc:
        if _is_source_unmapped(delay_exc):
            raise
        logger.debug("eastmoney delay concept cons failed %s: %s", concept_name, delay_exc)
    source = "em"
    try:
        with em_proxy(proxy_url):
            return _call_provider(lambda: ak.stock_board_concept_cons_em(symbol=concept_name)), source
    except Exception as em_exc:
        fetch_ths = getattr(ak, "stock_board_concept_cons_ths", None)
        if fetch_ths is None:
            raise RuntimeError(f"em={str(em_exc)[:120]}; ths_unavailable=stock_board_concept_cons_ths") from em_exc
        try:
            source = "ths"
            return _call_provider(lambda: fetch_ths(symbol=concept_name)), source
        except Exception as ths_exc:
            raise RuntimeError(f"em={str(em_exc)[:120]}; ths={str(ths_exc)[:120]}") from ths_exc


def _is_source_unmapped(exc: Exception) -> bool:
    return str(exc).strip("'").startswith(_SOURCE_UNMAPPED_PREFIX)


def _sync_one_group(db: Database, *, kind: str, name: str, proxy_url: str = None) -> tuple[int, str]:
    try:
        df, source = _fetch_concept_cons(name, proxy_url) if kind == "concept" else _fetch_board_cons(name, proxy_url)
    except LookupError as exc:
        if not _is_source_unmapped(exc):
            raise
        col_name = "concept_constituents" if kind == "concept" else "board_constituents"
        name_field = "concept_name" if kind == "concept" else "board_name"
        db[col_name].update_one(
            {"_id": name},
            {"$set": {
                name_field: name,
                "source": "source_unmapped",
                "updated_at": datetime.now(),
                "status": "source_unmapped",
                "error_msg": str(exc),
            }},
            upsert=True,
        )
        return 0, "source_unmapped"
    symbols, stock_names = _extract_constituents(df)
    if not symbols:
        return 0, source
    col_name = "concept_constituents" if kind == "concept" else "board_constituents"
    name_field = "concept_name" if kind == "concept" else "board_name"
    db[col_name].update_one(
        {"_id": name},
        {"$set": {
            name_field: name,
            "symbols": symbols,
            "stock_names": stock_names,
            "source": source,
            "stock_count": len(symbols),
            "updated_at": datetime.now(),
            "status": "ok",
        }},
        upsert=True,
    )
    return len(symbols), source


@sync_retry(max_attempts=3, min_wait=10)
def sync_board_cons(db: Database, proxy_url: str = None) -> dict:
    """
    行业成分股全量同步。

    遍历所有行业板块，拉取成分股列表存入 board_constituents。
    """
    sync_col = db["sync_log"]
    meta = sync_col.find_one({"_id": "board_cons:_meta"}, {"next_cursor": 1}) or {}

    boards = _get_board_list(db)
    concepts = _get_concept_list(db)
    groups = [("board", name) for name in boards] + [("concept", name) for name in concepts]
    logger.info("成分股同步: %d 行业, %d 概念", len(boards), len(concepts))

    start_cursor = 0 if os.getenv("BOARD_CONS_RESET_CURSOR", "false").lower() == "true" else int(meta.get("next_cursor") or 0)
    if start_cursor >= len(groups):
        start_cursor = 0
    batch_limit = min(len(groups), start_cursor + _batch_size())
    deadline = time.monotonic() + _max_runtime_seconds()

    total_groups = 0
    total_stocks = 0
    errors = []
    unmapped = 0
    source_counts: dict[str, int] = {}
    cursor = start_cursor
    progress_interval = _progress_interval()
    _write_board_cons_progress(
        sync_col,
        status="running",
        cursor=cursor,
        start_cursor=start_cursor,
        total_count=len(groups),
        processed_groups=0,
        total_stocks=0,
        errors=[],
        unmapped=0,
        source_counts={},
    )

    while cursor < batch_limit and time.monotonic() < deadline:
        kind, name = groups[cursor]
        try:
            stock_count, source = _sync_one_group(db, kind=kind, name=name, proxy_url=proxy_url)
            source_counts[source] = source_counts.get(source, 0) + 1
            if source == "source_unmapped":
                unmapped += 1
                logger.debug("  - %s %s: source_unmapped", kind, name)
            elif stock_count:
                total_groups += 1
                total_stocks += stock_count
                logger.debug("  ✓ %s %s: %d 只 (%s)", kind, name, stock_count, source)
            else:
                errors.append((kind, name, "无数据"))
            time.sleep(_CALL_INTERVAL)

        except Exception as e:
            errors.append((kind, name, str(e)[:240]))
        finally:
            cursor += 1
            if (cursor - start_cursor) % progress_interval == 0 or cursor >= batch_limit:
                _write_board_cons_progress(
                    sync_col,
                    status="running",
                    cursor=cursor,
                    start_cursor=start_cursor,
                    total_count=len(groups),
                    processed_groups=total_groups,
                    total_stocks=total_stocks,
                    errors=errors,
                    unmapped=unmapped,
                    source_counts=source_counts,
                )

    remaining = max(0, len(groups) - cursor)
    next_cursor = cursor if remaining else 0
    status = "ok"
    if remaining or errors:
        status = "partial" if total_groups > 0 or cursor > start_cursor else "degraded"
    error_msg = f"{len(errors)} errors; remaining={remaining}" if errors or remaining else ""

    _write_board_cons_progress(
        sync_col,
        status=status,
        cursor=cursor,
        start_cursor=start_cursor,
        total_count=len(groups),
        processed_groups=total_groups,
        total_stocks=total_stocks,
        errors=errors,
        unmapped=unmapped,
        source_counts=source_counts,
    )

    logger.info("成分股完成: %s, %d 组, %d 只股票, %d 失败, remaining=%d",
                status, total_groups, total_stocks, len(errors), remaining)
    return {
        "status": status,
        "groups": total_groups,
        "processed": cursor - start_cursor,
        "remaining": remaining,
        "next_cursor": next_cursor,
        "total_groups": len(groups),
        "stocks": total_stocks,
        "errors": len(errors),
        "error_msg": error_msg,
        "sample_errors": errors[:10],
        "unmapped": unmapped,
        "source_counts": source_counts,
    }
