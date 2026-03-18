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

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.board_ranking")


def _sync_ths_ranking(db: Database) -> int:
    """同花顺行业排行（无需代理，10jqka.com 不封 IP）"""
    col = db["board_ranking"]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        df = ak.stock_board_industry_summary_ths()
        if df is None or df.empty:
            return 0

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
            logger.info(f"  ✓ THS 行业排行: {len(docs)} 板块")
            return len(docs)

    except Exception as e:
        logger.error(f"  ✗ THS 排行失败: {e}")
    return 0


def _sync_em_ranking(db: Database, proxy_url: str = None) -> int:
    """东财行业排行（可能需要代理）"""
    col = db["board_ranking"]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        with em_proxy(proxy_url):
            df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return 0

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
            logger.info(f"  ✓ 东财行业排行: {len(docs)} 板块")
            return len(docs)

    except Exception as e:
        logger.error(f"  ✗ 东财排行失败: {e}")
    return 0


def _sync_concept_ranking(db: Database, proxy_url: str = None) -> int:
    """概念排行快照"""
    col = db["concept_ranking"]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # 优先新浪
    try:
        df = ak.stock_sector_spot(indicator="概念")
        if df is not None and not df.empty:
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
                logger.info(f"  ✓ 新浪概念排行: {len(docs)} 概念")
                return len(docs)
    except Exception as e:
        logger.warning(f"  ✗ 新浪概念失败: {e}")

    # 兜底东财
    try:
        with em_proxy(proxy_url):
            df = ak.stock_board_concept_name_em()
        if df is not None and not df.empty:
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
                logger.info(f"  ✓ 东财概念排行: {len(docs)} 概念")
                return len(docs)
    except Exception as e:
        logger.error(f"  ✗ 东财概念失败: {e}")

    return 0


@sync_retry(max_attempts=5)
def sync_board_ranking(db: Database, proxy_url: str = None) -> dict:
    """行业 + 概念排行快照同步"""
    logger.info("行业排行同步")

    ths_count = _sync_ths_ranking(db)
    em_count = _sync_em_ranking(db, proxy_url)
    concept_count = _sync_concept_ranking(db, proxy_url)

    total = ths_count + em_count + concept_count
    logger.info(f"排行同步完成: THS={ths_count}, EM={em_count}, "
                f"概念={concept_count}")
    return {"ths": ths_count, "em": em_count, "concept": concept_count}
