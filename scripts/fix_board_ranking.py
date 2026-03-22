#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 MongoDB board_ranking / concept_ranking 数据

核心问题：
  灌入脚本在非交易日运行时，dt 标记为灌入时间而非实际交易日，
  导致数据日期和真实行情对不上。

修复逻辑：
  1. 用 AKShare 拉一只蓝筹（如沪深300 ETF）的最近 K 线，
     取最后一根 K 线的日期作为「真实交易日」
  2. 分源拉取行业/概念排行，每条数据标注真实交易日
  3. 清理旧的无 trading_day 标记的脏数据

用法：
  python scripts/fix_board_ranking.py              # 修复全部
  python scripts/fix_board_ranking.py --dry        # 只检查不修改
  python scripts/fix_board_ranking.py --check      # 检查 MongoDB 当前数据状态
"""
import sys
import os
import logging
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─── 核心：获取 A 股真实最后交易日 ─────────────────

def detect_real_trading_day() -> str:
    """
    通过拉取真实 K 线数据来确定 A 股最后一个交易日。

    优先级：
    1. 新浪个股日线（最快）→ 取最后一根 K 线日期
    2. 东财个股日线 → 取最后一根日期
    3. 本地推算（跳过周末）

    返回 YYYY-MM-DD 格式的字符串
    """
    import akshare as ak
    import pandas as pd

    # 方法 1: 新浪日线（快速可靠）
    try:
        df = ak.stock_zh_a_daily(symbol="sz399300", adjust="qfq")
        if df is not None and not df.empty:
            last_date = pd.to_datetime(df["date"].iloc[-1]).strftime("%Y-%m-%d")
            logger.info(f"  真实交易日（新浪 sz399300）: {last_date}")
            return last_date
    except Exception as e:
        logger.debug(f"  新浪检测失败: {e}")

    # 方法 2: 东财日线
    try:
        df = ak.stock_zh_a_hist(
            symbol="000300", period="daily",
            start_date=(datetime.now() - timedelta(days=10)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
            adjust="qfq",
        )
        if df is not None and not df.empty:
            last_date = pd.to_datetime(df["日期"].iloc[-1]).strftime("%Y-%m-%d")
            logger.info(f"  真实交易日（东财 000300）: {last_date}")
            return last_date
    except Exception as e:
        logger.debug(f"  东财检测失败: {e}")

    # 方法 3: 本地推算
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    fallback = d.strftime("%Y-%m-%d")
    logger.warning(f"  交易日检测全部失败，使用本地推算: {fallback}")
    return fallback


# ─── 数据拉取函数 ─────────────────────────────────

def fetch_ths_ranking():
    """同花顺行业排行（90 行业）"""
    import akshare as ak
    logger.info("拉取 THS 行业排行...")
    df = ak.stock_board_industry_summary_ths()
    if df is None or df.empty:
        return None
    logger.info(f"  ✓ THS: {len(df)} 个行业")
    return df, "ths"


def fetch_em_ranking():
    """东财行业排行（~500 行业）"""
    import akshare as ak
    logger.info("拉取东财行业排行...")
    df = ak.stock_board_industry_name_em()
    if df is None or df.empty:
        return None
    logger.info(f"  ✓ 东财: {len(df)} 个行业")
    return df, "em"


def fetch_sina_industry():
    """新浪行业排行"""
    import akshare as ak
    logger.info("拉取新浪行业...")
    df = ak.stock_sector_spot(indicator="新浪行业")
    if df is None or df.empty:
        return None
    logger.info(f"  ✓ 新浪: {len(df)} 个行业")
    return df, "sina"


def fetch_sina_concept():
    """新浪概念排行"""
    import akshare as ak
    logger.info("拉取新浪概念...")
    df = ak.stock_sector_spot(indicator="概念")
    if df is None or df.empty:
        return None
    logger.info(f"  ✓ 新浪概念: {len(df)} 条")
    return df, "sina_concept"


def fetch_em_concept():
    """东财概念排行"""
    import akshare as ak
    logger.info("拉取东财概念...")
    df = ak.stock_board_concept_name_em()
    if df is None or df.empty:
        return None
    logger.info(f"  ✓ 东财概念: {len(df)} 条")
    return df, "em_concept"


# ─── 数据转换 ─────────────────────────────────────

def df_to_docs(df, source: str, trading_day: str) -> list:
    """DataFrame → MongoDB 文档列表，标记真实交易日"""
    import pandas as pd
    dt_val = datetime.strptime(trading_day, "%Y-%m-%d")
    docs = []

    for _, row in df.iterrows():
        doc = {
            "dt": dt_val,
            "trading_day": trading_day,
            "source": source,
        }
        for col in df.columns:
            val = row[col]
            if pd.isna(val):
                continue
            if isinstance(val, float) and (val != val or abs(val) == float("inf")):
                continue
            doc[col] = val
        docs.append(doc)
    return docs


# ─── 检查当前数据 ─────────────────────────────────

def check_data(db):
    """检查 MongoDB 当前数据状态"""
    for col_name in ["board_ranking", "concept_ranking"]:
        col = db[col_name]
        total = col.count_documents({})
        if total == 0:
            logger.info(f"{col_name}: 空")
            continue

        sources = col.distinct("source")
        has_td = col.count_documents({"trading_day": {"$exists": True}})
        no_td = col.count_documents({"trading_day": {"$exists": False}})

        logger.info(f"\n{col_name}: {total} 条")
        logger.info(f"  数据源: {sources}")
        logger.info(f"  有 trading_day: {has_td} 条")
        logger.info(f"  无 trading_day（脏数据）: {no_td} 条")

        for src in sources:
            cnt = col.count_documents({"source": src})
            sample = col.find_one({"source": src}, {"_id": 0, "dt": 1, "trading_day": 1})
            td = sample.get("trading_day", "无")
            dt = sample.get("dt", "无")
            logger.info(f"  [{src}] {cnt} 条 | dt={dt} | trading_day={td}")


# ─── 主函数 ─────────────────────────────────────

def main():
    dry_run = "--dry" in sys.argv
    check_only = "--check" in sys.argv

    # 连接 MongoDB
    try:
        from pymongo import MongoClient
        try:
            import config
            mongo_url = config.MONGO_URL or "mongodb://localhost:27017/signals"
            db_name = config.MONGO_DB_NAME or "signals"
        except Exception:
            mongo_url = "mongodb://localhost:27017/signals"
            db_name = "signals"

        client = MongoClient(mongo_url, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        db = client[db_name]
        logger.info(f"MongoDB 已连接: {db_name}")
    except Exception as e:
        logger.error(f"MongoDB 连接失败: {e}")
        sys.exit(1)

    if check_only:
        check_data(db)
        return

    # 检测真实交易日
    logger.info("")
    logger.info("检测 A 股真实交易日...")
    trading_day = detect_real_trading_day()
    logger.info(f"真实交易日: {trading_day}")

    if dry_run:
        logger.info("(dry run 模式，不修改数据)")
        check_data(db)
        return

    # ── 修复行业排行 ──────────────────────────
    logger.info("")
    logger.info("=" * 50)
    logger.info("修复 board_ranking")
    logger.info("=" * 50)

    col = db["board_ranking"]
    old_count = col.count_documents({})

    fetchers = [fetch_ths_ranking, fetch_em_ranking, fetch_sina_industry]
    success_count = 0

    for fetcher in fetchers:
        try:
            result = fetcher()
            if result is None:
                continue
            df, source = result
            docs = df_to_docs(df, source, trading_day)
            if docs:
                deleted = col.delete_many({"source": source})
                logger.info(f"  删除旧 [{source}]: {deleted.deleted_count} 条")
                col.insert_many(docs, ordered=False)
                logger.info(f"  写入 [{source}]: {len(docs)} 条 (trading_day={trading_day})")
                success_count += 1
        except Exception as e:
            logger.warning(f"  ✗ {fetcher.__name__}: {str(e)[:80]}")

    # 清理无 trading_day 的脏数据
    dirty = col.delete_many({"trading_day": {"$exists": False}})
    if dirty.deleted_count:
        logger.info(f"  清理脏数据: {dirty.deleted_count} 条")

    new_count = col.count_documents({})
    logger.info(f"  结果: {old_count} → {new_count} 条 ({success_count} 源成功)")

    # ── 修复概念排行 ──────────────────────────
    logger.info("")
    logger.info("=" * 50)
    logger.info("修复 concept_ranking")
    logger.info("=" * 50)

    col2 = db["concept_ranking"]
    old_count2 = col2.count_documents({})

    concept_fetchers = [fetch_sina_concept, fetch_em_concept]
    success_count2 = 0

    for fetcher in concept_fetchers:
        try:
            result = fetcher()
            if result is None:
                continue
            df, source = result
            docs = df_to_docs(df, source, trading_day)
            if docs:
                deleted = col2.delete_many({"source": source})
                logger.info(f"  删除旧 [{source}]: {deleted.deleted_count} 条")
                col2.insert_many(docs, ordered=False)
                logger.info(f"  写入 [{source}]: {len(docs)} 条 (trading_day={trading_day})")
                success_count2 += 1
        except Exception as e:
            logger.warning(f"  ✗ {fetcher.__name__}: {str(e)[:80]}")

    dirty2 = col2.delete_many({"trading_day": {"$exists": False}})
    if dirty2.deleted_count:
        logger.info(f"  清理脏数据: {dirty2.deleted_count} 条")

    new_count2 = col2.count_documents({})
    logger.info(f"  结果: {old_count2} → {new_count2} 条 ({success_count2} 源成功)")

    # ── 汇总 ──────────────────────────────────
    logger.info("")
    logger.info("=" * 50)
    logger.info("修复完成")
    logger.info("=" * 50)
    logger.info(f"真实交易日: {trading_day}")
    logger.info(f"board_ranking: {new_count} 条")
    logger.info(f"concept_ranking: {new_count2} 条")
    logger.info("")
    logger.info("下一步:")
    logger.info("  1. 重启 web2: 刷新浏览器")
    logger.info("  2. 刷新聚类: curl http://localhost:8001/api/cluster/refresh")


if __name__ == "__main__":
    main()
