# -*- coding: utf-8 -*-
"""
行业排行快照同步 — THS + 东财双源

数据源:
  Primary: 同花顺 stock_board_industry_summary_ths（90 行业）
  Fallback: 东财 stock_board_industry_name_em
策略: 每日快照，不同源独立存储
频率: 工作日 16:30
"""
import logging
from datetime import datetime

import akshare as ak
import pandas as pd
from pymongo.database import Database

from signals.data.board_normalizer import (
    merge_industry_sources,
    normalize_em_concept,
    normalize_em_industry,
    normalize_sina_concept,
    normalize_sina_industry,
    normalize_ths_concept,
    normalize_ths_industry,
)

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.board_ranking")


def _today():
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _replace_docs(db: Database, collection: str, docs: list[dict],
                  dedup: dict) -> int:
    if not docs:
        return 0
    col = db[collection]
    col.delete_many(dedup)
    col.insert_many(docs, ordered=False)
    return len(docs)


def _save_source_df(db: Database, collection: str, df: pd.DataFrame,
                    source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    normalized = df.copy()
    normalized["dt"] = _today()
    docs = normalized.to_dict("records")
    _replace_docs(db, collection, docs, {"dt": _today()})
    db["data_freshness"].update_one(
        {"domain": collection.split("_")[0], "market": "A",
         "mode": "realtime", "collection": collection},
        {"$set": {
            "domain": collection.split("_")[0],
            "market": "A",
            "mode": "realtime",
            "as_of": _today(),
            "collection": collection,
            "latest_dt": _today(),
            "stale_reason": "",
            "updated_at": datetime.now(),
            "source": source,
        }},
        upsert=True,
    )
    return normalized


def _health(db: Database, provider: str, endpoint: str, domain: str,
            ok: bool, error: str = ""):
    db["provider_health"].update_one(
        {"provider": provider, "endpoint": endpoint, "domain": domain},
        {"$set": {
            "provider": provider,
            "endpoint": endpoint,
            "domain": domain,
            "status": "ok" if ok else "degraded",
            "last_success_at": datetime.now() if ok else None,
            "last_error_at": None if ok else datetime.now(),
            "last_error_type": None if ok else error[:200],
        }},
        upsert=True,
    )


def _sync_ths_ranking(db: Database) -> int:
    """同花顺行业排行（无需代理，10jqka.com 不封 IP）"""
    col = db["board_ranking"]
    today = _today()

    try:
        df = ak.stock_board_industry_summary_ths()
        if df is None or df.empty:
            _health(db, "ths", "stock_board_industry_summary_ths", "board", False, "empty")
            return 0
        _save_source_df(db, "board_ths", normalize_ths_industry(df), "ths")

        docs = []
        for idx, row in df.iterrows():
            doc = {
                "dt": today,
                "source": "ths",
                "board_name": row.get("板块", row.get("行业", "")),
                "rank_idx": idx,
                "change_pct": float(row.get("涨跌幅", 0)),
            }
            # THS 有额外字段：净流入等
            for extra_col in ["净流入", "领涨股", "领涨股-涨跌幅",
                              "涨跌家数", "总市值"]:
                if extra_col in row and pd.notna(row[extra_col]):
                    doc[extra_col] = row[extra_col]
            docs.append(doc)

        if docs:
            # 删除今日同源旧数据
            col.delete_many({"dt": today, "source": "ths"})
            col.insert_many(docs)
            _health(db, "ths", "stock_board_industry_summary_ths", "board", True)
            logger.info(f"  ✓ THS 行业排行: {len(docs)} 板块")
            return len(docs)

    except Exception as e:
        _health(db, "ths", "stock_board_industry_summary_ths", "board", False, str(e))
        logger.error(f"  ✗ THS 排行失败: {e}")
    return 0


def _sync_em_ranking(db: Database, proxy_url: str = None) -> int:
    """东财行业排行（可能需要代理）"""
    col = db["board_ranking"]
    today = _today()

    try:
        with em_proxy(proxy_url):
            df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            _health(db, "em", "stock_board_industry_name_em", "board", False, "empty")
            return 0
        _save_source_df(db, "board_em", normalize_em_industry(df), "em")

        docs = []
        for idx, row in df.iterrows():
            doc = {
                "dt": today,
                "source": "em",
                "board_name": row.get("板块名称", ""),
                "rank_idx": idx,
                "change_pct": float(row.get("涨跌幅", 0)),
            }
            for extra_col in ["总市值", "换手率", "上涨家数", "下跌家数",
                              "领涨股票", "涨跌幅.1"]:
                if extra_col in row and pd.notna(row[extra_col]):
                    doc[extra_col] = row[extra_col]
            docs.append(doc)

        if docs:
            col.delete_many({"dt": today, "source": "em"})
            col.insert_many(docs)
            _health(db, "em", "stock_board_industry_name_em", "board", True)
            logger.info(f"  ✓ 东财行业排行: {len(docs)} 板块")
            return len(docs)

    except Exception as e:
        _health(db, "em", "stock_board_industry_name_em", "board", False, str(e))
        logger.error(f"  ✗ 东财排行失败: {e}")
    return 0


def _sync_sina_industry(db: Database) -> int:
    """新浪行业排行 source snapshot."""
    try:
        df = ak.stock_sector_spot(indicator="行业")
        if df is None or df.empty:
            _health(db, "sina", "stock_sector_spot_industry", "board", False, "empty")
            return 0
        normalized = _save_source_df(db, "board_sina", normalize_sina_industry(df), "sina")
        _health(db, "sina", "stock_sector_spot_industry", "board", True)
        logger.info(f"  ✓ 新浪行业排行: {len(normalized)} 板块")
        return len(normalized)
    except Exception as e:
        _health(db, "sina", "stock_sector_spot_industry", "board", False, str(e))
        logger.warning(f"  ✗ 新浪行业失败: {e}")
        return 0


def _sync_concept_ranking(db: Database, proxy_url: str = None) -> int:
    """概念排行快照"""
    col = db["concept_ranking"]
    today = _today()
    total = 0

    # 新浪实时源
    try:
        df = ak.stock_sector_spot(indicator="概念")
        if df is not None and not df.empty:
            _save_source_df(db, "concept_sina", normalize_sina_concept(df), "sina")
            docs = []
            for idx, row in df.iterrows():
                docs.append({
                    "dt": today,
                    "source": "sina",
                    "concept": row.get("板块", ""),
                    "rank_idx": idx,
                    "change_pct": float(row.get("涨跌幅", 0)),
                })
            if docs:
                col.delete_many({"dt": today, "source": "sina"})
                col.insert_many(docs)
                _health(db, "sina", "stock_sector_spot_concept", "concept", True)
                logger.info(f"  ✓ 新浪概念排行: {len(docs)} 概念")
                total += len(docs)
        else:
            _health(db, "sina", "stock_sector_spot_concept", "concept", False, "empty")
    except Exception as e:
        _health(db, "sina", "stock_sector_spot_concept", "concept", False, str(e))
        logger.warning(f"  ✗ 新浪概念失败: {e}")

    # 东财实时源，独立写 source snapshot；失败不影响新浪结果
    try:
        with em_proxy(proxy_url):
            df = ak.stock_board_concept_name_em()
        if df is not None and not df.empty:
            _save_source_df(db, "concept_em", normalize_em_concept(df), "em")
            docs = []
            for idx, row in df.iterrows():
                docs.append({
                    "dt": today,
                    "source": "em",
                    "concept": row.get("板块名称", ""),
                    "rank_idx": idx,
                    "change_pct": float(row.get("涨跌幅", 0)),
                })
            if docs:
                col.delete_many({"dt": today, "source": "em"})
                col.insert_many(docs)
                _health(db, "em", "stock_board_concept_name_em", "concept", True)
                logger.info(f"  ✓ 东财概念排行: {len(docs)} 概念")
                total += len(docs)
        else:
            _health(db, "em", "stock_board_concept_name_em", "concept", False, "empty")
    except Exception as e:
        _health(db, "em", "stock_board_concept_name_em", "concept", False, str(e))
        logger.error(f"  ✗ 东财概念失败: {e}")

    return total


def _sync_ths_concept(db: Database) -> int:
    try:
        df = ak.stock_board_concept_name_ths()
        if df is None or df.empty:
            _health(db, "ths", "stock_board_concept_name_ths", "concept", False, "empty")
            return 0
        normalized = _save_source_df(db, "concept_ths", normalize_ths_concept(df), "ths")
        _health(db, "ths", "stock_board_concept_name_ths", "concept", True)
        logger.info(f"  ✓ THS 概念列表: {len(normalized)} 概念")
        return len(normalized)
    except Exception as e:
        _health(db, "ths", "stock_board_concept_name_ths", "concept", False, str(e))
        logger.warning(f"  ✗ THS 概念失败: {e}")
        return 0


def _rebuild_board_canonical(db: Database) -> int:
    dfs = {}
    for src, col_name in [("em", "board_em"), ("ths", "board_ths"), ("sina", "board_sina")]:
        docs = list(db[col_name].find({"dt": _today()}, {"_id": 0}))
        if docs:
            dfs[src] = pd.DataFrame(docs)
    merged = merge_industry_sources(dfs)
    if merged is None or merged.empty:
        return 0
    merged = merged.sort_values("change_pct", ascending=False).reset_index(drop=True)
    merged["rank_idx"] = range(len(merged))
    merged["source"] = "canonical"
    docs = merged.to_dict("records")
    _replace_docs(db, "board_ranking", docs, {"dt": _today(), "source": "canonical"})
    db["data_freshness"].update_one(
        {"domain": "board", "market": "A", "mode": "historical",
         "collection": "board_ranking"},
        {"$set": {
            "domain": "board", "market": "A", "mode": "historical",
            "as_of": _today(), "collection": "board_ranking",
            "latest_dt": _today(), "stale_reason": "",
            "updated_at": datetime.now(),
        }},
        upsert=True,
    )
    return len(docs)


def _rebuild_concept_canonical(db: Database) -> int:
    # Priority: Sina broad coverage, then Eastmoney rich fields, then THS names.
    frames = []
    for col_name in ["concept_sina", "concept_em", "concept_ths"]:
        docs = list(db[col_name].find({"dt": _today()}, {"_id": 0}))
        if docs:
            frames.append(pd.DataFrame(docs))
    if not frames:
        return 0
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.dropna(subset=["board_name"])
    merged = merged.drop_duplicates(subset=["board_name"], keep="first")
    if "change_pct" in merged.columns:
        merged = merged.sort_values("change_pct", ascending=False)
    merged = merged.reset_index(drop=True)
    merged["rank_idx"] = range(len(merged))
    merged["source"] = "canonical"
    docs = merged.to_dict("records")
    _replace_docs(db, "concept_ranking", docs, {"dt": _today(), "source": "canonical"})
    db["data_freshness"].update_one(
        {"domain": "concept", "market": "A", "mode": "historical",
         "collection": "concept_ranking"},
        {"$set": {
            "domain": "concept", "market": "A", "mode": "historical",
            "as_of": _today(), "collection": "concept_ranking",
            "latest_dt": _today(), "stale_reason": "",
            "updated_at": datetime.now(),
        }},
        upsert=True,
    )
    return len(docs)


@sync_retry(max_attempts=5)
def sync_board_ranking(db: Database, proxy_url: str = None) -> dict:
    """行业 + 概念排行快照同步"""
    logger.info("行业排行同步")

    ths_count = _sync_ths_ranking(db)
    em_count = _sync_em_ranking(db, proxy_url)
    sina_count = _sync_sina_industry(db)
    concept_count = _sync_concept_ranking(db, proxy_url)
    ths_concept_count = _sync_ths_concept(db)
    board_canonical = _rebuild_board_canonical(db)
    concept_canonical = _rebuild_concept_canonical(db)

    total = ths_count + em_count + sina_count + concept_count + ths_concept_count
    logger.info(f"排行同步完成: THS={ths_count}, EM={em_count}, SINA={sina_count}, "
                f"概念={concept_count}, THS概念={ths_concept_count}, "
                f"canonical(board={board_canonical}, concept={concept_canonical})")
    return {
        "ths": ths_count,
        "em": em_count,
        "sina": sina_count,
        "concept": concept_count,
        "concept_ths": ths_concept_count,
        "board_canonical": board_canonical,
        "concept_canonical": concept_canonical,
        "total": total,
    }
