#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
🐲 MongoDB 核心数据灌入脚本

Usage:
    python scripts/seed_mongodb.py              # 全部 Phase
    python scripts/seed_mongodb.py --phase 1    # 只灌指数
    python scripts/seed_mongodb.py --phase 2    # 只灌个股（最慢，可 nohup 后台跑）
    python scripts/seed_mongodb.py --phase 3    # 只灌行业K线
    python scripts/seed_mongodb.py --phase 4    # 只灌行业分类
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seed")

START_DATE = "2024-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
BS_START = START_DATE  # BaoStock 用 YYYY-MM-DD 格式
BS_END = END_DATE
BS_START_COMPACT = START_DATE.replace("-", "")  # AKShare/THS 用 YYYYMMDD
BS_END_COMPACT = END_DATE.replace("-", "")


def get_db():
    from pymongo import MongoClient
    import config
    if not config.MONGO_URL:
        logger.error("MONGO_URL 未配置，请在 .env 中设置")
        sys.exit(1)
    client = MongoClient(config.MONGO_URL, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client[config.MONGO_DB_NAME]


# ═══════════════════════════════════════════════════════
# Phase 1: 指数日线
# ═══════════════════════════════════════════════════════

INDICES_BS = {
    "sh.000016": "上证50",
    "sh.000300": "沪深300",
    "sz.399006": "创业板指",
    "sh.000905": "中证500",
    "sh.000852": "中证1000",
    "sh.000043": "超大盘",
    "sz.399001": "深证成指",
    "sz.399303": "国证2000",
}


def phase1_indices(db):
    """Phase 1: 指数日线 (BaoStock)"""
    import baostock as bs
    bs.login()

    col = db["index_daily"]
    total = 0

    for bs_code, name in INDICES_BS.items():
        # 转为 AKShare 格式: sh.000016 → sh000016
        symbol = bs_code.replace(".", "")

        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=BS_START, end_date=BS_END, frequency="d",
        )
        docs = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if not row[1]:  # open 为空跳过
                continue
            docs.append({
                "symbol": symbol,
                "name": name,
                "dt": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "vol": int(float(row[5])) if row[5] else 0,
                "amount": float(row[6]) if row[6] else 0,
            })

        if docs:
            col.delete_many({"symbol": symbol})
            col.insert_many(docs, ordered=False)
            total += len(docs)
            logger.info(f"  ✓ {symbol} ({name}): {len(docs)} bars")

    # 科创50 用 AKShare 补充
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000688")
        if df is not None and not df.empty:
            import pandas as pd
            df["date"] = pd.to_datetime(df["date"])
            cutoff = pd.to_datetime(START_DATE)
            df = df[df["date"] >= cutoff]
            docs = []
            for _, row in df.iterrows():
                docs.append({
                    "symbol": "sh000688",
                    "name": "科创50",
                    "dt": row["date"].strftime("%Y-%m-%d"),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "vol": int(row["volume"]) if "volume" in row else 0,
                })
            if docs:
                col.delete_many({"symbol": "sh000688"})
                col.insert_many(docs, ordered=False)
                total += len(docs)
                logger.info(f"  ✓ sh000688 (科创50/AKShare): {len(docs)} bars")
    except Exception as e:
        logger.warning(f"  ✗ 科创50 AKShare 失败: {e}")

    # 建索引
    col.create_index([("symbol", 1), ("dt", 1)], unique=True, background=True)
    bs.logout()
    logger.info(f"Phase 1 完成: index_daily {total} 条")
    return total


# ═══════════════════════════════════════════════════════
# Phase 2: 核心股票日线
# ═══════════════════════════════════════════════════════

def _get_core_stocks():
    """获取核心股票列表: 沪深300 + 上证50 + 中证500 去重"""
    import baostock as bs
    bs.login()

    all_codes = {}  # code → name (去重)

    # 沪深300
    rs = bs.query_hs300_stocks()
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        all_codes[row[1]] = row[2]  # code, code_name

    # 上证50
    rs = bs.query_sz50_stocks()
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        all_codes[row[1]] = row[2]

    # 中证500
    rs = bs.query_zz500_stocks()
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        all_codes[row[1]] = row[2]

    bs.logout()
    return all_codes


def phase2_stocks(db, max_stocks=0):
    """Phase 2: 核心股票日线 (BaoStock)"""
    import baostock as bs

    stocks = _get_core_stocks()
    logger.info(f"核心股票去重: {len(stocks)} 只")

    if max_stocks > 0:
        codes = list(stocks.items())[:max_stocks]
    else:
        codes = list(stocks.items())

    col = db["kline_daily"]
    total = 0
    failed = []
    t0 = time.time()

    bs.login()
    for i, (bs_code, name) in enumerate(codes):
        # bs_code: sh.600519 → code: 600519
        code = bs_code.split(".")[1]

        # 断点续传: 检查是否已有足够数据
        existing = col.count_documents({"code": code})
        if existing > 400:  # 已有足够数据，跳过
            if (i + 1) % 100 == 0:
                logger.info(f"  [{i+1}/{len(codes)}] {code} 已有 {existing} 条，跳过")
            continue

        try:
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume,amount",
                start_date=BS_START, end_date=BS_END,
                frequency="d", adjustflag="2",  # 前复权
            )
            docs = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if not row[1]:
                    continue
                docs.append({
                    "code": code,
                    "dt": row[0],
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "vol": int(float(row[5])) if row[5] else 0,
                    "amount": float(row[6]) if row[6] else 0,
                })

            if docs:
                col.delete_many({"code": code})
                col.insert_many(docs, ordered=False)
                total += len(docs)

            if (i + 1) % 50 == 0:
                elapsed = time.time() - t0
                per = elapsed / (i + 1)
                eta = per * (len(codes) - i - 1)
                logger.info(
                    f"  [{i+1}/{len(codes)}] {code} ({name}): {len(docs)} bars | "
                    f"累计 {total:,} 条 | ETA {eta/60:.0f}min"
                )

        except Exception as e:
            failed.append((code, str(e)[:60]))
            if len(failed) <= 5:
                logger.warning(f"  ✗ {code}: {e}")

    bs.logout()

    # 建索引
    col.create_index([("code", 1), ("dt", 1)], unique=True, background=True)

    elapsed = time.time() - t0
    logger.info(
        f"Phase 2 完成: kline_daily {total:,} 条 | "
        f"{elapsed/60:.1f} 分钟 | 失败 {len(failed)} 只"
    )
    if failed:
        logger.info(f"  失败股票: {[f[0] for f in failed[:10]]}")
    return total


# ═══════════════════════════════════════════════════════
# Phase 3: 行业 K 线
# ═══════════════════════════════════════════════════════

def phase3_industry_kline(db):
    """Phase 3: 行业板块 K 线 (AKShare/THS)"""
    import akshare as ak

    # 获取行业列表
    try:
        df_list = ak.stock_board_industry_summary_ths()
        if df_list is None or df_list.empty:
            logger.warning("THS 行业列表获取失败")
            return 0
        industries = df_list["板块"].tolist()
    except Exception as e:
        logger.warning(f"THS 行业列表失败: {e}")
        return 0

    col = db["industry_kline"]
    total = 0
    failed = []
    t0 = time.time()

    # 断点续传：跳过已入库的行业
    existing = set(col.distinct("board_name"))
    if existing:
        skipped = [n for n in industries if n in existing]
        industries = [n for n in industries if n not in existing]
        logger.info(f"  断点续传: 跳过 {len(skipped)} 个已入库行业, 待拉取 {len(industries)} 个")
        if not industries:
            logger.info("  所有行业已入库, 跳过 Phase 3")
            return 0

    for i, name in enumerate(industries):
        try:
            df = ak.stock_board_industry_index_ths(
                symbol=name,
                start_date=BS_START_COMPACT,
                end_date=BS_END_COMPACT,
            )
            if df is None or df.empty:
                continue

            docs = []
            for _, row in df.iterrows():
                docs.append({
                    "board_name": name,
                    "dt": str(row["日期"]),
                    "open": float(row["开盘价"]),
                    "high": float(row["最高价"]),
                    "low": float(row["最低价"]),
                    "close": float(row["收盘价"]),
                    "vol": int(row["成交量"]) if "成交量" in row else 0,
                    "amount": float(row["成交额"]) if "成交额" in row else 0,
                })

            if docs:
                col.delete_many({"board_name": name})
                col.insert_many(docs, ordered=False)
                total += len(docs)

            logger.info(f"  [{i+1}/{len(industries)}] {name}: {len(docs)} bars | 累计 {total:,}")

        except Exception as e:
            failed.append((name, str(e)[:60]))
            if len(failed) <= 3:
                logger.warning(f"  ✗ {name}: {str(e)[:60]}")
            if "Connection" in str(e) or "Remote" in str(e):
                logger.warning("  网络中断，等待 5 秒后继续...")
                time.sleep(5)

    # 建索引
    col.create_index([("board_name", 1), ("dt", 1)], unique=True, background=True)

    elapsed = time.time() - t0
    logger.info(
        f"Phase 3 完成: industry_kline {total:,} 条 | "
        f"{elapsed/60:.1f} 分钟 | 失败 {len(failed)}"
    )
    return total


# ═══════════════════════════════════════════════════════
# Phase 4: 行业分类 + 成分股
# ═══════════════════════════════════════════════════════

def phase4_constituents(db):
    """Phase 4: 行业分类 + 指数成分股 (BaoStock)"""
    import baostock as bs
    bs.login()

    col = db["board_constituents"]
    total = 0

    # 行业分类
    rs = bs.query_stock_industry()
    industry_map = {}  # industry → [codes]
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        # row: [updateDate, code, code_name, industry, industryClassification]
        industry = row[3]
        if not industry:
            continue
        code = row[1].split(".")[1] if "." in row[1] else row[1]
        if industry not in industry_map:
            industry_map[industry] = []
        industry_map[industry].append({
            "code": code,
            "code_name": row[2],
        })

    # 写入 MongoDB
    col.drop()
    docs = []
    for industry, stocks in industry_map.items():
        docs.append({
            "board_name": industry,
            "source": "baostock",
            "dt": datetime.now().strftime("%Y-%m-%d"),
            "stocks": [s["code"] for s in stocks],
            "stock_details": stocks,
            "count": len(stocks),
        })

    if docs:
        col.insert_many(docs, ordered=False)
        total = sum(d["count"] for d in docs)
        logger.info(f"  ✓ 行业分类: {len(docs)} 个行业, {total} 只股票")

    # 建索引
    col.create_index("board_name", background=True)

    # 指数成分股
    idx_col = db["index_constituents"]
    idx_col.drop()
    idx_docs = []

    for query_fn, idx_name in [
        (bs.query_hs300_stocks, "沪深300"),
        (bs.query_sz50_stocks, "上证50"),
        (bs.query_zz500_stocks, "中证500"),
    ]:
        rs = query_fn()
        stocks = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            code = row[1].split(".")[1] if "." in row[1] else row[1]
            stocks.append({"code": code, "code_name": row[2]})
        if stocks:
            idx_docs.append({
                "index_name": idx_name,
                "dt": datetime.now().strftime("%Y-%m-%d"),
                "stocks": [s["code"] for s in stocks],
                "count": len(stocks),
            })
            logger.info(f"  ✓ {idx_name}: {len(stocks)} 只")

    if idx_docs:
        idx_col.insert_many(idx_docs, ordered=False)

    bs.logout()
    logger.info(f"Phase 4 完成: {len(docs)} 行业, {len(idx_docs)} 指数成分股")
    return total


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MongoDB 核心数据灌入")
    parser.add_argument("--phase", type=int, default=0, help="指定 phase (1-4), 0=全部")
    parser.add_argument("--max-stocks", type=int, default=0, help="Phase 2 限制股票数量")
    args = parser.parse_args()

    db = get_db()
    logger.info(f"MongoDB 已连接, 数据库: {db.name}")
    logger.info(f"时间范围: {START_DATE} ~ {END_DATE}")

    t0 = time.time()
    phases = [args.phase] if args.phase > 0 else [1, 2, 3, 4]

    for p in phases:
        logger.info(f"\n{'='*50}")
        logger.info(f"Phase {p} 开始")
        logger.info(f"{'='*50}")

        if p == 1:
            phase1_indices(db)
        elif p == 2:
            phase2_stocks(db, args.max_stocks)
        elif p == 3:
            phase3_industry_kline(db)
        elif p == 4:
            phase4_constituents(db)

    elapsed = time.time() - t0
    logger.info(f"\n{'='*50}")
    logger.info(f"全部完成! 耗时 {elapsed/60:.1f} 分钟")
    logger.info(f"{'='*50}")

    # 汇总
    for col_name in sorted(db.list_collection_names()):
        count = db[col_name].count_documents({})
        logger.info(f"  {col_name}: {count:,} 条")


if __name__ == "__main__":
    main()
