# -*- coding: utf-8 -*-
"""
A股日线同步 — ~5000 只股票增量同步

数据源: AKShare stock_zh_a_hist（东财接口）
策略: 增量同步，从 sync_log.last_dt 开始拉增量
频率: 工作日 16:30
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
from pymongo import UpdateOne
from pymongo.database import Database

from ..proxy import em_proxy
from ..retry import sync_retry

logger = logging.getLogger("signals.sync.stock_daily")

# 每批次并行拉取股票数
_BATCH_WORKERS = 8
# 每只股票间隔（秒），避免被东财限速
_CALL_INTERVAL = 0.3


def _get_all_stock_codes() -> list:
    """获取全量 A 股代码列表"""
    try:
        df = ak.stock_info_a_code_name()
        return df["code"].tolist()
    except Exception as e:
        logger.warning(f"获取股票列表失败: {e}，使用 stock_zh_a_spot_em 兜底")
        df = ak.stock_zh_a_spot_em()
        return df["代码"].tolist()


def _sync_one_stock(code: str, last_dt: str, end_date: str,
                    proxy_url: str = None) -> list:
    """同步单只股票日线，返回文档列表"""
    start = last_dt or "19900101"
    with em_proxy(proxy_url):
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start, end_date=end_date,
            adjust="qfq",
        )
    if df is None or df.empty:
        return []

    docs = []
    for _, row in df.iterrows():
        docs.append({
            "dt": pd.to_datetime(row["日期"]),
            "meta": {"symbol": code, "freq": "日线"},
            "open": float(row["开盘"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "close": float(row["收盘"]),
            "vol": int(row["成交量"]) if pd.notna(row["成交量"]) else 0,
            "amount": int(float(row["成交额"])) if pd.notna(row["成交额"]) else 0,
        })
    return docs


@sync_retry
def sync_stock_daily(db: Database, proxy_url: str = None) -> dict:
    """
    A 股日线全量增量同步。

    1. 获取全量股票列表
    2. 查 sync_log 获取每只股票的 last_dt
    3. 并行拉取增量数据
    4. bulk_write 到 bars collection
    5. 更新 sync_log
    """
    bars_col = db["bars"]
    sync_col = db["sync_log"]
    end_date = datetime.now().strftime("%Y%m%d")

    # 获取股票列表
    codes = _get_all_stock_codes()
    logger.info(f"A股日线同步: {len(codes)} 只股票")

    # 批量查询 sync_log
    sync_docs = {
        doc["symbol"]: doc.get("last_dt")
        for doc in sync_col.find(
            {"module": "stock_daily"},
            {"symbol": 1, "last_dt": 1}
        )
    }

    total_inserted = 0
    total_skipped = 0
    errors = []

    def _process(code):
        last_dt_raw = sync_docs.get(code)
        if last_dt_raw:
            # 增量：从 last_dt 下一天开始
            if isinstance(last_dt_raw, datetime):
                inc_start = (last_dt_raw + timedelta(days=1)).strftime("%Y%m%d")
            else:
                inc_start = (datetime.strptime(str(last_dt_raw)[:10], "%Y-%m-%d")
                             + timedelta(days=1)).strftime("%Y%m%d")
            if inc_start > end_date:
                return code, [], "skip"
        else:
            # 全量：近 2 年
            inc_start = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")

        try:
            docs = _sync_one_stock(code, inc_start, end_date, proxy_url)
            time.sleep(_CALL_INTERVAL)
            return code, docs, "ok"
        except Exception as e:
            return code, [], str(e)

    # 并行拉取
    with ThreadPoolExecutor(max_workers=_BATCH_WORKERS) as executor:
        futures = {executor.submit(_process, c): c for c in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                code, docs, status = future.result()
                if status == "skip":
                    total_skipped += 1
                elif status == "ok" and docs:
                    # 写入 MongoDB
                    bars_col.insert_many(docs, ordered=False)
                    total_inserted += len(docs)
                    # 更新 sync_log
                    last = docs[-1]["dt"]
                    sync_col.update_one(
                        {"_id": f"stock_daily:{code}"},
                        {"$set": {
                            "module": "stock_daily",
                            "symbol": code,
                            "last_dt": last,
                            "last_run": datetime.now(),
                            "status": "ok",
                            "bar_count": len(docs),
                        }},
                        upsert=True,
                    )
                elif status != "ok":
                    errors.append((code, status))
            except Exception as e:
                errors.append((code, str(e)))

    logger.info(f"A股日线完成: +{total_inserted} bars, "
                f"{total_skipped} 已最新, {len(errors)} 失败")
    if errors[:5]:
        logger.warning(f"前 5 个错误: {errors[:5]}")

    return {
        "inserted": total_inserted,
        "skipped": total_skipped,
        "errors": len(errors),
    }
