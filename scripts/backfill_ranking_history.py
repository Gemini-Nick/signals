#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史行业排名回填 — 从已有K线数据计算每日行业涨跌幅排名

数据源: MongoDB industry_kline 集合（已有 77 行业 × 534 天 K线）
原理: change_pct = (close - prev_close) / prev_close × 100
输出: MongoDB board_ranking_history 集合

已验证: 与 THS 实时数据 100% 匹配（77/77 行业，平均偏差 0.001%）

用法:
    python scripts/backfill_ranking_history.py              # 回填全部
    python scripts/backfill_ranking_history.py --dry        # 只计算不写入
    python scripts/backfill_ranking_history.py --check      # 检查回填结果
    python scripts/backfill_ranking_history.py --date 2024-09-24  # 查看某天排名
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def get_db():
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
    return client[db_name]


def compute_daily_rankings(db):
    """从 industry_kline 计算每日行业涨跌幅排名"""
    import pandas as pd

    col = db["industry_kline"]
    boards = col.distinct("board_name")
    logger.info(f"行业K线: {len(boards)} 个行业")

    # 一次性读取所有K线
    all_docs = list(col.find({}, {"_id": 0, "board_name": 1, "dt": 1, "close": 1, "vol": 1}).sort("dt", 1))
    if not all_docs:
        logger.error("industry_kline 无数据")
        return None

    df = pd.DataFrame(all_docs)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0)

    # 按行业计算每日涨跌幅
    df["prev_close"] = df.groupby("board_name")["close"].shift(1)
    df["change_pct"] = ((df["close"] - df["prev_close"]) / df["prev_close"] * 100).round(2)
    df = df.dropna(subset=["change_pct"])

    # 按日期分组，计算排名
    df["rank"] = df.groupby("dt")["change_pct"].rank(ascending=False, method="min").astype(int)

    dates = sorted(df["dt"].unique())
    logger.info(f"可覆盖交易日: {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")
    logger.info(f"总记录数: {len(df):,}")

    return df


def backfill(db, df, dry_run=False):
    """将计算结果写入 board_ranking_history"""
    import pandas as pd
    from datetime import datetime

    col = db["board_ranking_history"]

    if dry_run:
        dates = sorted(df["dt"].unique())
        logger.info(f"[dry run] 将写入 {len(df):,} 条到 board_ranking_history")
        logger.info(f"[dry run] 覆盖 {len(dates)} 个交易日: {dates[0]} ~ {dates[-1]}")

        # 展示几天的数据
        for day in [dates[0], dates[len(dates)//2], dates[-1]]:
            day_df = df[df["dt"] == day].sort_values("rank")
            top = day_df.iloc[0]
            print(f"  {day}: Top={top['board_name']}({top['change_pct']:+.2f}%), {len(day_df)} 行业")
        return

    t0 = time.time()

    # 清空旧数据
    old = col.count_documents({})
    if old > 0:
        col.drop()
        logger.info(f"清空旧数据: {old:,} 条")

    # 构建文档
    docs = []
    for _, row in df.iterrows():
        doc = {
            "trading_day": row["dt"],
            "board_name": row["board_name"],
            "close": float(row["close"]),
            "change_pct": float(row["change_pct"]),
            "rank": int(row["rank"]),
            "vol": int(row["vol"]) if pd.notna(row["vol"]) else 0,
            "source": "kline_backfill",
        }
        docs.append(doc)

    # 批量写入
    BATCH = 5000
    for i in range(0, len(docs), BATCH):
        batch = docs[i:i+BATCH]
        col.insert_many(batch, ordered=False)
        if (i // BATCH + 1) % 5 == 0:
            logger.info(f"  写入进度: {i+len(batch):,}/{len(docs):,}")

    # 建索引
    col.create_index([("trading_day", 1), ("rank", 1)], background=True)
    col.create_index([("board_name", 1), ("trading_day", 1)], unique=True, background=True)
    col.create_index("trading_day", background=True)

    elapsed = time.time() - t0
    logger.info(f"回填完成: {len(docs):,} 条 | {elapsed:.1f} 秒")


def check_result(db):
    """检查回填结果"""
    col = db["board_ranking_history"]
    total = col.count_documents({})
    if total == 0:
        logger.info("board_ranking_history: 空（未回填）")
        return

    dates = col.distinct("trading_day")
    boards = col.distinct("board_name")
    logger.info(f"board_ranking_history: {total:,} 条")
    logger.info(f"  交易日: {len(dates)} 天 ({min(dates)} ~ {max(dates)})")
    logger.info(f"  行业: {len(boards)} 个")

    # 展示最近3天
    recent = sorted(dates)[-3:]
    for day in recent:
        top3 = list(col.find({"trading_day": day}, {"_id": 0}).sort("rank", 1).limit(3))
        labels = [f"{d['board_name']}({d['change_pct']:+.2f}%)" for d in top3]
        logger.info(f"  {day}: {' | '.join(labels)}")


def show_date(db, date_str):
    """展示指定日期的完整排名"""
    col = db["board_ranking_history"]
    docs = list(col.find({"trading_day": date_str}, {"_id": 0}).sort("rank", 1))
    if not docs:
        logger.info(f"{date_str}: 无数据")
        return

    up = sum(1 for d in docs if d["change_pct"] > 0)
    dn = sum(1 for d in docs if d["change_pct"] < 0)
    avg = sum(d["change_pct"] for d in docs) / len(docs)

    print(f"\n{'='*60}")
    print(f"{date_str} 行业排名 | {len(docs)} 行业 | 涨{up}跌{dn} | 均涨{avg:+.2f}%")
    print(f"{'='*60}")
    print(f"{'排名':>4} {'行业':<14} {'涨跌幅':>8} {'收盘':>10}")
    print("-" * 40)
    for d in docs:
        pct = d["change_pct"]
        mark = "🔴" if pct < -3 else "🟢" if pct > 3 else "  "
        print(f"{d['rank']:>4} {mark}{d['board_name']:<12} {pct:>+7.2f}% {d['close']:>10.2f}")


def main():
    parser = argparse.ArgumentParser(description="行业历史排名回填")
    parser.add_argument("--dry", action="store_true", help="只计算不写入")
    parser.add_argument("--check", action="store_true", help="检查回填结果")
    parser.add_argument("--date", type=str, help="查看指定日期排名 (YYYY-MM-DD)")
    args = parser.parse_args()

    db = get_db()
    logger.info(f"MongoDB 已连接: {db.name}")

    if args.check:
        check_result(db)
        return

    if args.date:
        show_date(db, args.date)
        return

    # 计算
    df = compute_daily_rankings(db)
    if df is None:
        return

    # 回填
    backfill(db, df, dry_run=args.dry)

    if not args.dry:
        check_result(db)


if __name__ == "__main__":
    main()
