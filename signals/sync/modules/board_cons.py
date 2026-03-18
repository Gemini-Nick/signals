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
import time
from datetime import datetime

import akshare as ak
from pymongo.database import Database

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.board_cons")

_CALL_INTERVAL = 1.0  # 成分股接口限速更严格


def _get_board_list(db: Database) -> list:
    """获取行业列表（从今日 board_ranking 中提取）"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # 优先从 MongoDB 已有排行中获取
    docs = db["board_ranking"].find(
        {"source": "ths"},
        {"board_name": 1}
    ).sort("rank_idx", 1)
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


@sync_retry(max_attempts=3, min_wait=10)
def sync_board_cons(db: Database, proxy_url: str = None) -> dict:
    """
    行业成分股全量同步。

    遍历所有行业板块，拉取成分股列表存入 board_constituents。
    """
    col = db["board_constituents"]
    sync_col = db["sync_log"]

    boards = _get_board_list(db)
    logger.info(f"成分股同步: {len(boards)} 个行业")

    total_boards = 0
    total_stocks = 0
    errors = []

    for board_name in boards:
        try:
            # 优先东财
            df = None
            source = "em"
            try:
                with em_proxy(proxy_url):
                    df = ak.stock_board_industry_cons_em(symbol=board_name)
            except Exception:
                pass

            # 兜底 THS
            if df is None or df.empty:
                try:
                    df = ak.stock_board_industry_cons_ths(symbol=board_name)
                    source = "ths"
                except Exception:
                    pass

            if df is None or df.empty:
                errors.append((board_name, "无数据"))
                continue

            # 提取代码和名称
            code_col = "代码" if "代码" in df.columns else "股票代码"
            name_col = "名称" if "名称" in df.columns else "股票简称"

            symbols = df[code_col].tolist() if code_col in df.columns else []
            stock_names = {}
            if code_col in df.columns and name_col in df.columns:
                stock_names = dict(zip(df[code_col], df[name_col]))

            if symbols:
                col.update_one(
                    {"_id": board_name},
                    {"$set": {
                        "symbols": symbols,
                        "stock_names": stock_names,
                        "source": source,
                        "stock_count": len(symbols),
                        "updated_at": datetime.now(),
                    }},
                    upsert=True,
                )
                total_boards += 1
                total_stocks += len(symbols)
                logger.debug(f"  ✓ {board_name}: {len(symbols)} 只 ({source})")

            time.sleep(_CALL_INTERVAL)

        except Exception as e:
            errors.append((board_name, str(e)))

    # 更新总体 sync_log
    sync_col.update_one(
        {"_id": "board_cons:_meta"},
        {"$set": {
            "module": "board_cons",
            "last_run": datetime.now(),
            "status": "ok",
            "bar_count": total_stocks,
            "error_msg": f"{len(errors)} errors" if errors else None,
        }},
        upsert=True,
    )

    logger.info(f"成分股完成: {total_boards} 板块, {total_stocks} 只股票, "
                f"{len(errors)} 失败")
    return {
        "boards": total_boards,
        "stocks": total_stocks,
        "errors": len(errors),
    }
