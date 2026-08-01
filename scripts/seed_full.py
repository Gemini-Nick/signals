#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐲 一键全量数据灌入 + 历史排名回填

在网络畅通的环境执行，一次性完成所有数据灌入：

  Phase 1: 指数日线 (BaoStock, ~9只, 无需东财)
  Phase 2: 核心股票日线 (BaoStock, ~800只, 无需东财)
  Phase 3: 行业K线 (THS, ~90行业)
  Phase 4: 行业分类+成分股 (BaoStock)
  Phase 5: 概念K线 (THS, ~175概念) ← 新增
  Phase 6: 行业排行快照 (THS/东财/新浪)
  Phase 7: 概念排行快照 (新浪/东财)
  Phase 8: 行业历史排名回填 (从K线计算，纯本地)
  Phase 9: 概念历史排名回填 (从K线计算，纯本地)

用法:
    python scripts/seed_full.py                  # 全部 Phase
    python scripts/seed_full.py --phase 5        # 只跑概念K线
    python scripts/seed_full.py --phase 8 9      # 只回填排名
    python scripts/seed_full.py --check          # 检查所有集合状态
    python scripts/seed_full.py --date 2024-09-24 # 查看某天行业+概念排名

环境要求:
    - MongoDB 运行中 (.env 配置 MONGO_URL)
    - Python: akshare, baostock, pymongo, pandas
    - 网络: Phase 1/2/4 用 BaoStock (证券之星), Phase 3/5 用 THS (同花顺)
            Phase 6/7 用 THS/东财/新浪
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta

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
START_COMPACT = START_DATE.replace("-", "")
END_COMPACT = END_DATE.replace("-", "")


def get_db():
    from pymongo import MongoClient
    try:
        import config
        url = config.MONGO_URL or "mongodb://localhost:27017/signals"
        name = config.MONGO_DB_NAME or "signals"
    except Exception:
        url = "mongodb://localhost:27017/signals"
        name = "signals"
    client = MongoClient(url, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client[name]


def detect_real_trading_day():
    """用新浪K线检测真实最后交易日"""
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_zh_a_daily(symbol="sz399300", adjust="qfq")
        if df is not None and not df.empty:
            return pd.to_datetime(df["date"].iloc[-1]).strftime("%Y-%m-%d")
    except Exception:
        pass
    # 本地推算
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════
# Phase 1: 指数日线 (BaoStock)
# ═══════════════════════════════════════════════════════

INDICES_BS = {
    "sh.000016": "上证50", "sh.000300": "沪深300", "sz.399006": "创业板指",
    "sh.000905": "中证500", "sh.000852": "中证1000", "sh.000043": "超大盘",
    "sz.399001": "深证成指", "sz.399303": "国证2000",
}

def _check_incremental(col, key_field, key_val, end_date):
    """
    智能增量检查：
    返回 (mode, start_date)
      - ("skip", None)     → 数据已最新，不需要更新
      - ("incremental", "2026-03-18") → 从该日期开始增量拉取
      - ("full", "2024-01-01")        → 无数据，全量拉取
    """
    count = col.count_documents({key_field: key_val})
    if count == 0:
        return "full", START_DATE

    last = col.find_one({key_field: key_val}, sort=[("dt", -1)])
    first = col.find_one({key_field: key_val}, sort=[("dt", 1)])
    last_dt = str(last["dt"])[:10]
    first_dt = str(first["dt"])[:10]

    # 检查数据完整性：起始日期应该在 START_DATE 附近
    if first_dt > "2024-02-01":
        # 起始日期太晚，可能是不完整数据，全量重拉
        return "full", START_DATE

    # 检查是否已包含最新交易日
    # end_date 可能是周末/假日，用上一个工作日比较
    d = datetime.strptime(end_date, "%Y-%m-%d")
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    latest_trading = d.strftime("%Y-%m-%d")

    if last_dt >= latest_trading:
        return "skip", None

    # 有数据但不是最新 → 增量
    next_day = (datetime.strptime(last_dt, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return "incremental", next_day


def phase1_indices(db):
    import baostock as bs
    bs.login()
    col = db["index_daily"]
    total = 0
    for bs_code, name in INDICES_BS.items():
        symbol = bs_code.replace(".", "")
        mode, start = _check_incremental(col, "symbol", symbol, END_DATE)
        if mode == "skip":
            continue
        if mode == "full":
            col.delete_many({"symbol": symbol})
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=start, end_date=END_DATE, frequency="d",
        )
        docs = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if not row[1]:
                continue
            docs.append({
                "symbol": symbol, "name": name, "dt": row[0],
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "vol": int(float(row[5])) if row[5] else 0,
                "amount": float(row[6]) if row[6] else 0,
            })
        if docs:
            # 增量模式不删旧数据，用 upsert 避免重复
            from pymongo import UpdateOne
            ops = [UpdateOne({"symbol": symbol, "dt": d["dt"]}, {"$set": d}, upsert=True) for d in docs]
            col.bulk_write(ops, ordered=False)
            total += len(docs)
            logger.info(f"  ✓ {symbol} ({name}): +{len(docs)} bars ({mode})")

    # 科创50 补充
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_zh_index_daily(symbol="sh000688")
        if df is not None and not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df = df[df["date"] >= pd.to_datetime(START_DATE)]
            docs = [{"symbol": "sh000688", "name": "科创50", "dt": r["date"].strftime("%Y-%m-%d"),
                      "open": float(r["open"]), "high": float(r["high"]),
                      "low": float(r["low"]), "close": float(r["close"]),
                      "vol": int(r["volume"]) if "volume" in r else 0}
                     for _, r in df.iterrows()]
            if docs:
                col.delete_many({"symbol": "sh000688"})
                col.insert_many(docs, ordered=False)
                total += len(docs)
                logger.info(f"  ✓ sh000688 (科创50): {len(docs)} bars")
    except Exception as e:
        logger.warning(f"  ✗ 科创50: {e}")

    col.create_index([("symbol", 1), ("dt", 1)], unique=True, background=True)
    bs.logout()
    logger.info(f"Phase 1 完成: index_daily {total} 条")
    return total


# ═══════════════════════════════════════════════════════
# Phase 2: 核心股票日线 (BaoStock)
# ═══════════════════════════════════════════════════════

def phase2_stocks(db):
    import baostock as bs
    bs.login()

    all_codes = {}
    for fn in [bs.query_hs300_stocks, bs.query_sz50_stocks, bs.query_zz500_stocks]:
        rs = fn()
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            all_codes[row[1]] = row[2]
    logger.info(f"核心股票: {len(all_codes)} 只")

    col = db["kline_daily"]
    total, failed = 0, []
    t0 = time.time()

    for i, (bs_code, name) in enumerate(all_codes.items()):
        code = bs_code.split(".")[1]
        mode, start = _check_incremental(col, "code", code, END_DATE)
        if mode == "skip":
            if (i + 1) % 200 == 0:
                logger.info(f"  [{i+1}/{len(all_codes)}] 已跳过（数据已最新）")
            continue
        if mode == "full":
            col.delete_many({"code": code})
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume,amount",
                start_date=start, end_date=END_DATE,
                frequency="d", adjustflag="2",
            )
            docs = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if not row[1]:
                    continue
                docs.append({
                    "code": code, "dt": row[0],
                    "open": float(row[1]), "high": float(row[2]),
                    "low": float(row[3]), "close": float(row[4]),
                    "vol": int(float(row[5])) if row[5] else 0,
                    "amount": float(row[6]) if row[6] else 0,
                })
            if docs:
                from pymongo import UpdateOne
                ops = [UpdateOne({"code": code, "dt": d["dt"]}, {"$set": d}, upsert=True) for d in docs]
                col.bulk_write(ops, ordered=False)
                total += len(docs)
            if (i + 1) % 100 == 0:
                elapsed = time.time() - t0
                eta = elapsed / (i + 1) * (len(all_codes) - i - 1)
                logger.info(f"  [{i+1}/{len(all_codes)}] 累计 {total:,} 条 | ETA {eta/60:.0f}min")
        except Exception as e:
            failed.append(code)

    col.create_index([("code", 1), ("dt", 1)], unique=True, background=True)
    bs.logout()
    logger.info(f"Phase 2 完成: kline_daily {total:,} 条 | 失败 {len(failed)}")
    return total


# ═══════════════════════════════════════════════════════
# Phase 3: 行业K线 (THS)
# ═══════════════════════════════════════════════════════

def phase3_industry_kline(db):
    import akshare as ak
    try:
        df_list = ak.stock_board_industry_summary_ths()
        industries = df_list["板块"].tolist()
    except Exception as e:
        logger.warning(f"THS 行业列表失败: {e}")
        return 0

    col = db["industry_kline"]
    # 检查每个行业是否需要更新
    todo = []
    for name in industries:
        mode, start = _check_incremental(col, "board_name", name, END_DATE)
        if mode != "skip":
            todo.append((name, mode, start))
    if not todo:
        logger.info("Phase 3: 全部行业数据已最新，跳过")
        return 0
    new_count = sum(1 for _, m, _ in todo if m == "full")
    inc_count = sum(1 for _, m, _ in todo if m == "incremental")
    logger.info(f"  待处理: {len(todo)}/{len(industries)} (新增{new_count} + 增量{inc_count})")

    total, failed = 0, []
    for i, (name, mode, start) in enumerate(todo):
        try:
            start_c = start.replace("-", "")
            df = ak.stock_board_industry_index_ths(
                symbol=name, start_date=start_c, end_date=END_COMPACT)
            if df is None or df.empty:
                continue
            docs = [{"board_name": name, "dt": str(r["日期"]),
                      "open": float(r["开盘价"]), "high": float(r["最高价"]),
                      "low": float(r["最低价"]), "close": float(r["收盘价"]),
                      "vol": int(r["成交量"]) if "成交量" in r else 0,
                      "amount": float(r["成交额"]) if "成交额" in r else 0}
                     for _, r in df.iterrows()]
            if docs:
                if mode == "full":
                    col.delete_many({"board_name": name})
                    col.insert_many(docs, ordered=False)
                else:
                    from pymongo import UpdateOne
                    ops = [UpdateOne({"board_name": name, "dt": d["dt"]}, {"$set": d}, upsert=True) for d in docs]
                    col.bulk_write(ops, ordered=False)
                total += len(docs)
            logger.info(f"  [{i+1}/{len(todo)}] {name}: +{len(docs)} bars ({mode}) | 累计 {total:,}")
        except Exception as e:
            failed.append(name)
            if "Connection" in str(e) or "Remote" in str(e):
                logger.warning(f"  ✗ {name}: 网络中断，等 5s...")
                time.sleep(5)
            else:
                logger.warning(f"  ✗ {name}: {str(e)[:60]}")

    col.create_index([("board_name", 1), ("dt", 1)], unique=True, background=True)
    logger.info(f"Phase 3 完成: industry_kline +{total:,} 条 | 失败 {len(failed)}")
    if failed:
        logger.info(f"  失败列表: {failed[:10]}")
    return total


# ═══════════════════════════════════════════════════════
# Phase 4: 行业分类+成分股 (BaoStock)
# ═══════════════════════════════════════════════════════

def phase4_constituents(db):
    import baostock as bs
    bs.login()

    col = db["board_constituents"]
    if col.count_documents({}) > 50:
        logger.info("Phase 4: 已有成分股数据，跳过")
        bs.logout()
        return 0

    rs = bs.query_stock_industry()
    industry_map = {}
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        industry = row[3]
        if not industry:
            continue
        code = row[1].split(".")[1] if "." in row[1] else row[1]
        if industry not in industry_map:
            industry_map[industry] = []
        industry_map[industry].append({"code": code, "code_name": row[2]})

    col.drop()
    docs = [{"board_name": ind, "source": "baostock", "dt": END_DATE,
              "stocks": [s["code"] for s in stks], "stock_details": stks, "count": len(stks)}
             for ind, stks in industry_map.items()]
    if docs:
        col.insert_many(docs, ordered=False)
    col.create_index("board_name", background=True)

    # 指数成分股
    idx_col = db["index_constituents"]
    # Append-only versioned snapshots; never erase prior effective dates.
    from scripts.versioned_index_constituents import append_index_snapshot
    for fn, name in [(bs.query_hs300_stocks, "沪深300"),
                     (bs.query_sz50_stocks, "上证50"),
                     (bs.query_zz500_stocks, "中证500")]:
        rs = fn()
        stks = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            code = row[1].split(".")[1] if "." in row[1] else row[1]
            stks.append({"code": code, "code_name": row[2]})
        if stks:
            append_index_snapshot(
                idx_col,
                index_name=name,
                effective_date=END_DATE,
                stocks=stks,
                source="baostock",
            )
            logger.info(f"  ✓ {name}: {len(stks)} 只")

    bs.logout()
    total = sum(d["count"] for d in docs)
    logger.info(f"Phase 4 完成: {len(docs)} 行业, {total} 只股票")
    return total


# ═══════════════════════════════════════════════════════
# Phase 5: 概念K线 (THS) ← 新增
# ═══════════════════════════════════════════════════════

def phase5_concept_kline(db):
    """概念板块K线（THS 概念指数历史）"""
    import akshare as ak

    # 获取概念列表
    try:
        df_list = ak.stock_board_concept_name_ths()
        if df_list is None or df_list.empty:
            logger.warning("THS 概念列表获取失败")
            return 0
        # THS概念列表的列名
        name_col = "概念名称" if "概念名称" in df_list.columns else df_list.columns[1]
        concepts = df_list[name_col].tolist()
        logger.info(f"THS 概念总数: {len(concepts)}")
    except Exception as e:
        logger.warning(f"THS 概念列表失败: {e}")
        return 0

    col = db["concept_kline"]
    # 检查每个概念是否需要更新
    todo = []
    for name in concepts:
        mode, start = _check_incremental(col, "concept_name", name, END_DATE)
        if mode != "skip":
            todo.append((name, mode, start))
    if not todo:
        logger.info("Phase 5: 全部概念数据已最新，跳过")
        return 0
    new_count = sum(1 for _, m, _ in todo if m == "full")
    inc_count = sum(1 for _, m, _ in todo if m == "incremental")
    logger.info(f"  待处理: {len(todo)}/{len(concepts)} (新增{new_count} + 增量{inc_count})")

    total, failed = 0, []
    for i, (name, mode, start) in enumerate(todo):
        try:
            start_c = start.replace("-", "")
            df = ak.stock_board_concept_index_ths(
                symbol=name, start_date=start_c, end_date=END_COMPACT)
            if df is None or df.empty:
                failed.append((name, "empty"))
                continue
            docs = [{"concept_name": name, "dt": str(r["日期"]),
                      "open": float(r["开盘价"]), "high": float(r["最高价"]),
                      "low": float(r["最低价"]), "close": float(r["收盘价"]),
                      "vol": int(r["成交量"]) if "成交量" in r else 0,
                      "amount": float(r["成交额"]) if "成交额" in r else 0}
                     for _, r in df.iterrows()]
            if docs:
                if mode == "full":
                    col.delete_many({"concept_name": name})
                    col.insert_many(docs, ordered=False)
                else:
                    from pymongo import UpdateOne
                    ops = [UpdateOne({"concept_name": name, "dt": d["dt"]}, {"$set": d}, upsert=True) for d in docs]
                    col.bulk_write(ops, ordered=False)
                total += len(docs)
            if (i + 1) % 10 == 0 or i == len(todo) - 1:
                logger.info(f"  [{i+1}/{len(todo)}] {name}: +{len(docs)} bars ({mode}) | 累计 {total:,}")
        except Exception as e:
            failed.append((name, str(e)[:40]))
            if "Connection" in str(e) or "Remote" in str(e):
                logger.warning(f"  ✗ {name}: 网络中断，等 5s...")
                time.sleep(5)

    col.create_index([("concept_name", 1), ("dt", 1)], unique=True, background=True)
    logger.info(f"Phase 5 完成: concept_kline +{total:,} 条 | 失败 {len(failed)}")
    if failed:
        logger.info(f"  失败: {[f[0] for f in failed[:10]]}")
    return total


# ═══════════════════════════════════════════════════════
# Phase 6: 行业排行快照 (THS/东财/新浪)
# ═══════════════════════════════════════════════════════

def phase6_board_ranking(db):
    import akshare as ak
    import pandas as pd

    trading_day = detect_real_trading_day()
    logger.info(f"  真实交易日: {trading_day}")
    dt_val = datetime.strptime(trading_day, "%Y-%m-%d")

    col = db["board_ranking"]
    total = 0

    fetchers = [
        ("ths", lambda: ak.stock_board_industry_summary_ths()),
        ("em", lambda: ak.stock_board_industry_name_em()),
        ("sina", lambda: ak.stock_sector_spot(indicator="新浪行业")),
    ]

    for source, fn in fetchers:
        try:
            logger.info(f"  拉取 [{source}]...")
            df = fn()
            if df is None or df.empty:
                continue
            docs = []
            for _, row in df.iterrows():
                doc = {"dt": dt_val, "trading_day": trading_day, "source": source}
                for c in df.columns:
                    v = row[c]
                    if pd.notna(v) and not (isinstance(v, float) and abs(v) == float("inf")):
                        doc[c] = v
                docs.append(doc)
            col.delete_many({"source": source})
            col.insert_many(docs, ordered=False)
            total += len(docs)
            logger.info(f"  ✓ [{source}]: {len(docs)} 条")
        except Exception as e:
            logger.warning(f"  ✗ [{source}]: {str(e)[:60]}")

    # 清理脏数据
    col.delete_many({"trading_day": {"$exists": False}})
    logger.info(f"Phase 6 完成: board_ranking {total} 条")
    return total


# ═══════════════════════════════════════════════════════
# Phase 7: 概念排行快照 (新浪/东财)
# ═══════════════════════════════════════════════════════

def phase7_concept_ranking(db):
    import akshare as ak
    import pandas as pd

    trading_day = detect_real_trading_day()
    dt_val = datetime.strptime(trading_day, "%Y-%m-%d")

    col = db["concept_ranking"]
    total = 0

    fetchers = [
        ("sina", lambda: ak.stock_sector_spot(indicator="概念")),
        ("em", lambda: ak.stock_board_concept_name_em()),
    ]

    for source, fn in fetchers:
        try:
            logger.info(f"  拉取 [{source}]...")
            df = fn()
            if df is None or df.empty:
                continue
            docs = []
            for _, row in df.iterrows():
                doc = {"dt": dt_val, "trading_day": trading_day, "source": source}
                for c in df.columns:
                    v = row[c]
                    if pd.notna(v) and not (isinstance(v, float) and abs(v) == float("inf")):
                        doc[c] = v
                docs.append(doc)
            col.delete_many({"source": source})
            col.insert_many(docs, ordered=False)
            total += len(docs)
            logger.info(f"  ✓ [{source}]: {len(docs)} 条")
        except Exception as e:
            logger.warning(f"  ✗ [{source}]: {str(e)[:60]}")

    col.delete_many({"trading_day": {"$exists": False}})
    logger.info(f"Phase 7 完成: concept_ranking {total} 条")
    return total


# ═══════════════════════════════════════════════════════
# Phase 8: 行业历史排名回填 (纯本地计算)
# ═══════════════════════════════════════════════════════

def phase8_industry_ranking_history(db):
    import pandas as pd
    col_src = db["industry_kline"]
    col_dst = db["board_ranking_history"]

    boards = col_src.distinct("board_name")
    if not boards:
        logger.warning("industry_kline 无数据，跳过")
        return 0

    all_docs = list(col_src.find({}, {"_id": 0, "board_name": 1, "dt": 1, "close": 1, "vol": 1}).sort("dt", 1))
    df = pd.DataFrame(all_docs)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0)
    df["prev_close"] = df.groupby("board_name")["close"].shift(1)
    df["change_pct"] = ((df["close"] - df["prev_close"]) / df["prev_close"] * 100).round(2)
    df = df.dropna(subset=["change_pct"])
    df["rank"] = df.groupby("dt")["change_pct"].rank(ascending=False, method="min").astype(int)

    dates = sorted(df["dt"].unique())
    logger.info(f"  行业: {len(boards)} 个, 交易日: {len(dates)} 天")

    col_dst.drop()
    docs = [{"trading_day": r["dt"], "board_name": r["board_name"],
              "close": float(r["close"]), "change_pct": float(r["change_pct"]),
              "rank": int(r["rank"]), "vol": int(r["vol"]) if pd.notna(r["vol"]) else 0,
              "source": "kline_backfill"}
             for _, r in df.iterrows()]

    BATCH = 5000
    for i in range(0, len(docs), BATCH):
        col_dst.insert_many(docs[i:i + BATCH], ordered=False)

    col_dst.create_index([("trading_day", 1), ("rank", 1)], background=True)
    col_dst.create_index([("board_name", 1), ("trading_day", 1)], unique=True, background=True)
    logger.info(f"Phase 8 完成: board_ranking_history {len(docs):,} 条")
    return len(docs)


# ═══════════════════════════════════════════════════════
# Phase 9: 概念历史排名回填 (纯本地计算)
# ═══════════════════════════════════════════════════════

def phase9_concept_ranking_history(db):
    import pandas as pd
    col_src = db["concept_kline"]
    col_dst = db["concept_ranking_history"]

    concepts = col_src.distinct("concept_name")
    if not concepts:
        logger.warning("concept_kline 无数据，跳过（先跑 Phase 5）")
        return 0

    all_docs = list(col_src.find({}, {"_id": 0, "concept_name": 1, "dt": 1, "close": 1, "vol": 1}).sort("dt", 1))
    df = pd.DataFrame(all_docs)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0)
    df["prev_close"] = df.groupby("concept_name")["close"].shift(1)
    df["change_pct"] = ((df["close"] - df["prev_close"]) / df["prev_close"] * 100).round(2)
    df = df.dropna(subset=["change_pct"])
    df["rank"] = df.groupby("dt")["change_pct"].rank(ascending=False, method="min").astype(int)

    dates = sorted(df["dt"].unique())
    logger.info(f"  概念: {len(concepts)} 个, 交易日: {len(dates)} 天")

    col_dst.drop()
    docs = [{"trading_day": r["dt"], "concept_name": r["concept_name"],
              "close": float(r["close"]), "change_pct": float(r["change_pct"]),
              "rank": int(r["rank"]), "vol": int(r["vol"]) if pd.notna(r["vol"]) else 0,
              "source": "kline_backfill"}
             for _, r in df.iterrows()]

    BATCH = 5000
    for i in range(0, len(docs), BATCH):
        col_dst.insert_many(docs[i:i + BATCH], ordered=False)

    col_dst.create_index([("trading_day", 1), ("rank", 1)], background=True)
    col_dst.create_index([("concept_name", 1), ("trading_day", 1)], unique=True, background=True)
    logger.info(f"Phase 9 完成: concept_ranking_history {len(docs):,} 条")
    return len(docs)


# ═══════════════════════════════════════════════════════
# 检查 & 查询
# ═══════════════════════════════════════════════════════

def check_all(db):
    logger.info("MongoDB 数据状态:")
    for name in sorted(db.list_collection_names()):
        cnt = db[name].count_documents({})
        extra = ""
        if name == "kline_daily":
            codes = len(db[name].distinct("code"))
            extra = f" ({codes} 只股票)"
        elif name in ("industry_kline", "board_ranking_history"):
            boards = len(db[name].distinct("board_name"))
            extra = f" ({boards} 行业)"
        elif name in ("concept_kline", "concept_ranking_history"):
            concepts = len(db[name].distinct("concept_name"))
            extra = f" ({concepts} 概念)"
        logger.info(f"  {name}: {cnt:,} 条{extra}")


def show_date(db, date_str):
    print(f"\n{'='*60}")
    print(f"{date_str} 行业排名")
    print(f"{'='*60}")
    docs = list(db["board_ranking_history"].find(
        {"trading_day": date_str}, {"_id": 0}).sort("rank", 1))
    if docs:
        up = sum(1 for d in docs if d["change_pct"] > 0)
        print(f"{len(docs)} 行业 | 涨{up}跌{len(docs)-up}")
        for d in docs[:10]:
            mark = "🟢" if d["change_pct"] > 3 else "🔴" if d["change_pct"] < -3 else "  "
            print(f"  {d['rank']:>3} {mark}{d['board_name']:<12} {d['change_pct']:>+7.2f}%")
        if len(docs) > 10:
            print(f"  ... 共 {len(docs)} 行业")
    else:
        print("  无行业数据")

    print(f"\n{date_str} 概念排名")
    print("-" * 40)
    cdocs = list(db["concept_ranking_history"].find(
        {"trading_day": date_str}, {"_id": 0}).sort("rank", 1))
    if cdocs:
        up = sum(1 for d in cdocs if d["change_pct"] > 0)
        print(f"{len(cdocs)} 概念 | 涨{up}跌{len(cdocs)-up}")
        for d in cdocs[:10]:
            mark = "🟢" if d["change_pct"] > 3 else "🔴" if d["change_pct"] < -3 else "  "
            print(f"  {d['rank']:>3} {mark}{d['concept_name']:<14} {d['change_pct']:>+7.2f}%")
        if len(cdocs) > 10:
            print(f"  ... 共 {len(cdocs)} 概念")
    else:
        print("  无概念数据（先跑 Phase 5+9）")


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════

PHASE_MAP = {
    1: ("指数日线(BaoStock)", phase1_indices),
    2: ("核心股票日线(BaoStock)", phase2_stocks),
    3: ("行业K线(THS)", phase3_industry_kline),
    4: ("行业分类+成分股(BaoStock)", phase4_constituents),
    5: ("概念K线(THS)", phase5_concept_kline),
    6: ("行业排行快照(THS/东财/新浪)", phase6_board_ranking),
    7: ("概念排行快照(新浪/东财)", phase7_concept_ranking),
    8: ("行业历史排名回填(本地计算)", phase8_industry_ranking_history),
    9: ("概念历史排名回填(本地计算)", phase9_concept_ranking_history),
}

def main():
    parser = argparse.ArgumentParser(description="🐲 一键全量数据灌入")
    parser.add_argument("--phase", type=int, nargs="+", help="指定 phase (1-9)")
    parser.add_argument("--check", action="store_true", help="检查数据状态")
    parser.add_argument("--date", type=str, help="查看某天排名 (YYYY-MM-DD)")
    args = parser.parse_args()

    db = get_db()
    logger.info(f"MongoDB 已连接: {db.name}")

    if args.check:
        check_all(db)
        return
    if args.date:
        show_date(db, args.date)
        return

    phases = args.phase if args.phase else list(range(1, 10))
    logger.info(f"执行 Phase: {phases}")
    logger.info(f"时间范围: {START_DATE} ~ {END_DATE}")

    t0 = time.time()
    for p in phases:
        if p not in PHASE_MAP:
            logger.warning(f"未知 Phase {p}，跳过")
            continue
        name, fn = PHASE_MAP[p]
        logger.info(f"\n{'='*50}")
        logger.info(f"Phase {p}: {name}")
        logger.info(f"{'='*50}")
        fn(db)

    elapsed = time.time() - t0
    logger.info(f"\n{'='*50}")
    logger.info(f"全部完成! 耗时 {elapsed/60:.1f} 分钟")
    logger.info(f"{'='*50}")
    check_all(db)


if __name__ == "__main__":
    main()
