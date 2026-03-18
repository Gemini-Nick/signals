# -*- coding: utf-8 -*-
"""
MongoDB 连接管理 — 线程安全单例

pymongo.MongoClient 内置连接池，无需手动管理。
"""
import threading
from typing import Optional

from pymongo import MongoClient
from pymongo.database import Database

_client: Optional[MongoClient] = None
_lock = threading.Lock()


def get_db(mongo_url: str = None, db_name: str = None) -> Database:
    """
    获取 MongoDB 数据库实例（单例，线程安全）。

    首次调用需要传入 mongo_url，后续调用可省略。
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                if not mongo_url:
                    import config
                    mongo_url = config.MONGO_URL
                    db_name = db_name or config.MONGO_DB_NAME
                _client = MongoClient(
                    mongo_url,
                    maxPoolSize=10,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                )
    if db_name is None:
        import config
        db_name = getattr(config, "MONGO_DB_NAME", "signals")
    return _client[db_name]


def close():
    """关闭连接（进程退出时调用）"""
    global _client
    if _client:
        _client.close()
        _client = None
