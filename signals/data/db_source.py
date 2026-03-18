# -*- coding: utf-8 -*-
"""
MongoDB 读取后端 — 降级链首选数据源

实现与 AKShareSource 相同的方法签名，作为 drop-in 替代。
当 MONGO_URL 配置后，自动成为数据降级链的第一层。

用法:
    from signals.data.db_source import get_mongo_source
    src = get_mongo_source()
    if src:
        bars = src.get_a_daily("SH.600519", "20260101", "20260316")
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
from czsc import Freq, RawBar

logger = logging.getLogger("signals.data.db_source")

_mongo_source = None
_lock = threading.Lock()


def get_mongo_source():
    """
    获取 MongoSource 单例（DB_ENABLED=False 时返回 None）。
    """
    global _mongo_source
    if _mongo_source is not None:
        return _mongo_source

    with _lock:
        if _mongo_source is not None:
            return _mongo_source

        try:
            import config
            if not config.DB_ENABLED:
                return None
            _mongo_source = MongoSource(config.MONGO_URL, config.MONGO_DB_NAME)
            logger.info("MongoDB 数据源已连接")
            return _mongo_source
        except Exception as e:
            logger.warning(f"MongoDB 数据源初始化失败: {e}")
            return None


def _doc_to_rawbar(doc: dict, symbol: str, freq: Freq,
                   idx: int = 0) -> RawBar:
    """MongoDB 文档 → czsc.RawBar"""
    return RawBar(
        symbol=symbol,
        dt=doc["dt"] if isinstance(doc["dt"], datetime) else pd.to_datetime(doc["dt"]),
        id=idx,
        freq=freq,
        open=float(doc["open"]),
        high=float(doc["high"]),
        low=float(doc["low"]),
        close=float(doc["close"]),
        vol=int(doc.get("vol", 0)),
        amount=int(doc.get("amount", 0)),
    )


# Freq.value → MongoDB meta.freq 映射
_FREQ_MAP = {
    "日线": "日线",
    "周线": "周线",
    "30分钟": "30分钟",
    "15分钟": "15分钟",
    "60分钟": "60分钟",
}


class MongoSource:
    """
    MongoDB 读取后端。

    实现与 AKShareSource / FutuSource 相同的方法签名，
    供 fetcher.py 和 industry.py 作为降级链首选源。
    """

    def __init__(self, mongo_url: str, db_name: str = "signals"):
        from pymongo import MongoClient
        self._client = MongoClient(
            mongo_url,
            maxPoolSize=5,
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
        )
        self._db = self._client[db_name]

    def ping(self) -> bool:
        """检测连接是否可用"""
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False

    # ─── K 线查询 ───────────────────────────────────

    def get_a_daily(self, futu_code: str, sdt: str = None,
                    edt: str = None, **kwargs) -> List[RawBar]:
        """A 股日线查询"""
        # futu_code: SH.600519 → MongoDB 存 6 位代码
        code = futu_code.split(".")[-1] if "." in futu_code else futu_code
        return self._query_bars(code, "日线", Freq.D,
                                futu_code, sdt, edt)

    def get_index_daily(self, symbol: str,
                        lookback_days: int = 180,
                        start_date: str = None,
                        **kwargs) -> List[RawBar]:
        """指数日线查询（AKShare 格式: sh000016）"""
        if start_date:
            sdt = start_date.replace("-", "")
        else:
            sdt = (datetime.now() - timedelta(days=lookback_days)
                   ).strftime("%Y%m%d")
        return self._query_bars(symbol, "日线", Freq.D, symbol, sdt)

    def get_a_minute(self, futu_code: str, freq: Freq,
                     **kwargs) -> List[RawBar]:
        """A 股分钟线查询"""
        code = futu_code.split(".")[-1] if "." in futu_code else futu_code
        freq_str = _FREQ_MAP.get(freq.value, freq.value)
        # 分钟线只取最近 10 天
        sdt = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        return self._query_bars(code, freq_str, freq, futu_code, sdt)

    def get_index_minute(self, symbol: str, freq: Freq,
                         **kwargs) -> List[RawBar]:
        """指数分钟线查询"""
        freq_str = _FREQ_MAP.get(freq.value, freq.value)
        return self._query_bars(symbol, freq_str, freq, symbol)

    def _query_bars(self, symbol: str, freq_str: str, freq: Freq,
                    output_symbol: str,
                    sdt: str = None, edt: str = None) -> List[RawBar]:
        """通用 bars 查询"""
        query = {
            "meta.symbol": symbol,
            "meta.freq": freq_str,
        }
        if sdt or edt:
            dt_filter = {}
            if sdt:
                dt_filter["$gte"] = pd.to_datetime(sdt)
            if edt:
                dt_filter["$lte"] = pd.to_datetime(edt)
            query["dt"] = dt_filter

        try:
            cursor = self._db.bars.find(query).sort("dt", 1)
            bars = []
            for i, doc in enumerate(cursor):
                bars.append(_doc_to_rawbar(doc, output_symbol, freq, i))
            return bars
        except Exception as e:
            logger.debug(f"MongoDB 查询失败 {symbol}/{freq_str}: {e}")
            return []

    # ─── 行业数据查询 ───────────────────────────────

    def get_board_ranking(self, date: datetime = None,
                          source: str = None) -> Optional[pd.DataFrame]:
        """
        行业排行查询。

        :param date: 日期，默认今天
        :param source: 数据源过滤 ("ths"/"em")，默认返回所有
        """
        if date is None:
            date = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0)

        query = {"dt": date}
        if source:
            query["source"] = source

        try:
            docs = list(self._db.board_ranking.find(
                query, {"_id": 0}).sort("rank_idx", 1))
            if not docs:
                return None
            return pd.DataFrame(docs)
        except Exception as e:
            logger.debug(f"MongoDB 排行查询失败: {e}")
            return None

    def get_board_constituents(self, board_name: str) -> List[str]:
        """行业成分股查询"""
        try:
            doc = self._db.board_constituents.find_one({"_id": board_name})
            if doc:
                return doc.get("symbols", [])
        except Exception as e:
            logger.debug(f"MongoDB 成分股查询失败 {board_name}: {e}")
        return []

    def get_board_stock_names(self, board_name: str) -> Dict[str, str]:
        """行业成分股名称映射"""
        try:
            doc = self._db.board_constituents.find_one({"_id": board_name})
            if doc:
                return doc.get("stock_names", {})
        except Exception:
            pass
        return {}

    def get_concept_ranking(self, date: datetime = None,
                            source: str = None) -> Optional[pd.DataFrame]:
        """概念排行查询"""
        if date is None:
            date = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0)

        query = {"dt": date}
        if source:
            query["source"] = source

        try:
            docs = list(self._db.concept_ranking.find(
                query, {"_id": 0}).sort("rank_idx", 1))
            if not docs:
                return None
            return pd.DataFrame(docs)
        except Exception as e:
            logger.debug(f"MongoDB 概念查询失败: {e}")
            return None

    # ─── Sync 状态查询 ──────────────────────────────

    def get_sync_status(self) -> List[dict]:
        """获取所有模块的同步状态"""
        try:
            return list(self._db.sync_log.find(
                {"_id": {"$regex": ":_meta$"}},
                {"_id": 0, "module": 1, "status": 1,
                 "last_run": 1, "elapsed_seconds": 1, "error_msg": 1},
            ))
        except Exception:
            return []
