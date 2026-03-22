# -*- coding: utf-8 -*-
"""
一次性迁移: SQLite backtest.db → MongoDB signals + trade_pairs

用法:
    python scripts/migrate_signals_to_mongo.py
    python scripts/migrate_signals_to_mongo.py --dry-run    # 预览不写入
    python scripts/migrate_signals_to_mongo.py --verify     # 仅验证
"""
import argparse
import os
import sqlite3
import sys

# 项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


def _read_sqlite(db_path: str, table: str) -> list[dict]:
    """读取 SQLite 表的所有记录。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    result = [dict(row) for row in rows]
    conn.close()
    return result


def _get_mongo_db():
    """获取 MongoDB 数据库实例。"""
    from signals.sync.db import get_db
    db = get_db()
    db.command("ping")
    return db


def migrate_signals(db, records: list[dict], dry_run: bool = False) -> int:
    """迁移 signal_records → MongoDB signals collection。"""
    if not records:
        print("  没有信号记录需要迁移")
        return 0

    col = db["signals"]
    docs = []
    for r in records:
        doc = {k: v for k, v in r.items() if k != "id"}
        # SQLite 的 NULL 映射为 None，MongoDB 原生支持
        docs.append(doc)

    if dry_run:
        print(f"  [DRY RUN] 将迁移 {len(docs)} 条信号记录")
        return len(docs)

    from pymongo.errors import BulkWriteError
    try:
        result = col.insert_many(docs, ordered=False)
        inserted = len(result.inserted_ids)
    except BulkWriteError as e:
        inserted = e.details.get("nInserted", 0)
        dupes = len(e.details.get("writeErrors", []))
        print(f"  跳过 {dupes} 条重复记录")

    print(f"  已迁移 {inserted} 条信号记录")
    return inserted


def migrate_trade_pairs(db, records: list[dict], dry_run: bool = False) -> int:
    """迁移 trade_pairs → MongoDB trade_pairs collection。"""
    if not records:
        print("  没有交易配对需要迁移")
        return 0

    col = db["trade_pairs"]
    docs = []
    for r in records:
        doc = {k: v for k, v in r.items() if k != "id"}
        docs.append(doc)

    if dry_run:
        print(f"  [DRY RUN] 将迁移 {len(docs)} 条交易配对")
        return len(docs)

    from pymongo.errors import BulkWriteError
    try:
        result = col.insert_many(docs, ordered=False)
        inserted = len(result.inserted_ids)
    except BulkWriteError as e:
        inserted = e.details.get("nInserted", 0)

    print(f"  已迁移 {inserted} 条交易配对")
    return inserted


def verify(db, db_path: str):
    """验证迁移结果：对比 MongoDB 和 SQLite 的记录数。"""
    conn = sqlite3.connect(db_path)

    # 信号记录
    sqlite_total = conn.execute("SELECT COUNT(*) FROM signal_records").fetchone()[0]
    sqlite_eval = conn.execute(
        "SELECT COUNT(*) FROM signal_records WHERE evaluated=1"
    ).fetchone()[0]
    mongo_total = db["signals"].count_documents({})
    mongo_eval = db["signals"].count_documents({"evaluated": 1})

    print(f"\n  信号记录对比:")
    print(f"    SQLite: 总计 {sqlite_total} | 已评估 {sqlite_eval}")
    print(f"    MongoDB: 总计 {mongo_total} | 已评估 {mongo_eval}")
    match = "OK" if sqlite_total == mongo_total else "MISMATCH"
    print(f"    状态: {match}")

    # 交易配对
    sqlite_pairs = conn.execute("SELECT COUNT(*) FROM trade_pairs").fetchone()[0]
    mongo_pairs = db["trade_pairs"].count_documents({})
    print(f"\n  交易配对对比:")
    print(f"    SQLite: {sqlite_pairs} | MongoDB: {mongo_pairs}")
    match = "OK" if sqlite_pairs == mongo_pairs else "MISMATCH"
    print(f"    状态: {match}")

    # 抽样对比前 3 条
    sqlite_sample = conn.execute(
        "SELECT symbol, signal_date, signal_type FROM signal_records "
        "ORDER BY signal_date LIMIT 3"
    ).fetchall()
    mongo_sample = list(db["signals"].find(
        {}, {"_id": 0, "symbol": 1, "signal_date": 1, "signal_type": 1}
    ).sort("signal_date", 1).limit(3))

    print(f"\n  前3条记录抽样:")
    for i, (s, m) in enumerate(zip(sqlite_sample, mongo_sample)):
        s_str = f"{s[0]} {s[1]} {s[2]}"
        m_str = f"{m['symbol']} {m['signal_date']} {m['signal_type']}"
        ok = "OK" if s_str == m_str else "DIFF"
        print(f"    [{i+1}] {ok}: SQLite({s_str}) MongoDB({m_str})")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="迁移信号数据到 MongoDB")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--verify", action="store_true", help="仅验证")
    args = parser.parse_args()

    db_path = config.BACKTEST_DB_PATH
    print(f"{'═' * 60}")
    print(f"  信号数据迁移: SQLite → MongoDB")
    print(f"  SQLite: {db_path}")
    print(f"  MongoDB: {config.MONGO_URL[:30]}... / {config.MONGO_DB_NAME}")
    print(f"{'═' * 60}")

    if not os.path.exists(db_path):
        print(f"\n  错误: SQLite 数据库不存在: {db_path}")
        sys.exit(1)

    if not config.DB_ENABLED:
        print(f"\n  错误: MongoDB 未启用 (MONGO_URL 未配置)")
        sys.exit(1)

    try:
        db = _get_mongo_db()
    except Exception as e:
        print(f"\n  错误: MongoDB 连接失败: {e}")
        sys.exit(1)

    if args.verify:
        verify(db, db_path)
        return

    # 读取 SQLite 数据
    print(f"\n  读取 SQLite 数据...")
    signals = _read_sqlite(db_path, "signal_records")
    pairs = _read_sqlite(db_path, "trade_pairs")
    print(f"  信号记录: {len(signals)} 条 | 交易配对: {len(pairs)} 条")

    # 迁移
    print(f"\n  迁移信号记录...")
    migrate_signals(db, signals, args.dry_run)

    print(f"\n  迁移交易配对...")
    migrate_trade_pairs(db, pairs, args.dry_run)

    # 验证
    if not args.dry_run:
        print(f"\n  验证迁移结果...")
        verify(db, db_path)

    print(f"\n{'═' * 60}")
    print(f"  迁移完成!")
    print(f"{'═' * 60}")


if __name__ == "__main__":
    main()
