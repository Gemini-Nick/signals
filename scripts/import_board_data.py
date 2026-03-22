#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐲 行业/概念板块多源全量导入脚本

将 THS/东财/新浪/THS资金流 4 个数据源的行业和概念板块数据
导入 MongoDB，按源分开存储。支持断点续传和数据校验。

用法:
    python scripts/import_board_data.py              # 全量导入（8个任务）
    python scripts/import_board_data.py --check       # 查看各集合状态
    python scripts/import_board_data.py --source ths,sina   # 只导入指定源
    python scripts/import_board_data.py --type industry     # 只导入行业
    python scripts/import_board_data.py --type concept      # 只导入概念
    python scripts/import_board_data.py --force             # 强制覆盖（忽略断点）

依赖: pymongo, akshare, pandas
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import akshare as ak
import pandas as pd
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("import_board")

# ─── 配置 ──────────────────────────────────────────────

MONGO_URL = "mongodb://localhost:27017/signals"
DB_NAME = "signals"

# 数据校验阈值
VALIDATION = {
    "board_ths":         {"min_rows": 80,  "label": "THS 行业"},
    "board_em":          {"min_rows": 80,  "label": "东财行业"},
    "board_sina":        {"min_rows": 70,  "label": "新浪行业"},
    "board_fund_flow":   {"min_rows": 50,  "label": "THS 行业资金流"},
    "concept_sina":      {"min_rows": 150, "label": "新浪概念"},
    "concept_em":        {"min_rows": 300, "label": "东财概念"},
    "concept_ths":       {"min_rows": 100, "label": "THS 概念"},
    "concept_fund_flow": {"min_rows": 50,  "label": "THS 概念资金流"},
}


def get_last_trading_day() -> str:
    """获取最近的交易日（跳过周末）。"""
    now = datetime.now()
    d = now
    # 15:00 之后当天数据已出
    if d.weekday() < 5 and d.hour >= 15:
        return d.strftime("%Y-%m-%d")
    # 15:00 之前或周末 → 回退
    if d.hour < 15 and d.weekday() < 5:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ─── normalizer（简化版，直接内联）──────────────────────

def _normalize(df, col_map, source):
    """通用列名映射 + 补齐缺失列。"""
    mapped = df.rename(columns=col_map)
    mapped["source"] = source
    for col in ["board_name", "change_pct", "volume", "amount",
                 "net_inflow", "up_count", "down_count", "member_count",
                 "turnover_pct", "avg_price", "leader_name", "leader_code",
                 "leader_change_pct", "leader_price"]:
        if col not in mapped.columns:
            mapped[col] = None
    # 数值转换
    for col in ["change_pct", "volume", "amount", "net_inflow",
                 "up_count", "down_count", "member_count", "turnover_pct",
                 "avg_price", "leader_change_pct", "leader_price"]:
        if col in mapped.columns:
            mapped[col] = pd.to_numeric(mapped[col], errors="coerce")
    return mapped


# ─── 8 个导入任务 ──────────────────────────────────────

def fetch_board_ths():
    """THS 行业排行"""
    df = ak.stock_board_industry_summary_ths()
    return _normalize(df, {
        "板块": "board_name", "涨跌幅": "change_pct",
        "总成交量": "volume", "总成交额": "amount",
        "净流入": "net_inflow", "上涨家数": "up_count",
        "下跌家数": "down_count", "均价": "avg_price",
        "领涨股": "leader_name", "领涨股-涨跌幅": "leader_change_pct",
        "领涨股-最新价": "leader_price",
    }, "ths")


def fetch_board_em():
    """东财行业排行"""
    df = ak.stock_board_industry_name_em()
    return _normalize(df, {
        "板块名称": "board_name", "板块": "board_name",
        "涨跌幅": "change_pct", "换手率": "turnover_pct",
        "上涨家数": "up_count", "下跌家数": "down_count",
        "总成交量": "volume", "总成交额": "amount",
        "领涨股票": "leader_name",
        "领涨股票-涨跌幅": "leader_change_pct",
    }, "em")


def fetch_board_sina():
    """新浪行业排行"""
    df = ak.stock_sector_spot(indicator="行业")
    return _normalize(df, {
        "板块": "board_name", "涨跌幅": "change_pct",
        "总成交量": "volume", "总成交额": "amount",
        "公司家数": "member_count", "平均价格": "avg_price",
        "股票名称": "leader_name", "股票代码": "leader_code",
        "个股-涨跌幅": "leader_change_pct", "个股-当前价": "leader_price",
    }, "sina")


def fetch_board_fund_flow():
    """THS 行业资金流向"""
    df = ak.stock_fund_flow_industry(symbol="今日")
    return _normalize(df, {
        "行业": "board_name", "涨跌幅": "change_pct",
        "主力净流入-净额": "net_inflow",
    }, "fund_flow")


def fetch_concept_sina():
    """新浪概念排行"""
    df = ak.stock_sector_spot(indicator="概念")
    return _normalize(df, {
        "板块": "board_name", "涨跌幅": "change_pct",
        "总成交量": "volume", "总成交额": "amount",
        "公司家数": "member_count", "平均价格": "avg_price",
        "股票名称": "leader_name", "股票代码": "leader_code",
        "个股-涨跌幅": "leader_change_pct", "个股-当前价": "leader_price",
    }, "sina")


def fetch_concept_em():
    """东财概念排行"""
    df = ak.stock_board_concept_name_em()
    return _normalize(df, {
        "板块名称": "board_name", "板块": "board_name",
        "涨跌幅": "change_pct", "换手率": "turnover_pct",
        "上涨家数": "up_count", "下跌家数": "down_count",
        "总成交量": "volume", "总成交额": "amount",
        "领涨股票": "leader_name",
        "领涨股票-涨跌幅": "leader_change_pct",
    }, "em")


def fetch_concept_ths():
    """THS 概念排行"""
    df = ak.stock_board_concept_name_ths()
    return _normalize(df, {
        "概念名称": "board_name", "板块": "board_name",
        "涨跌幅": "change_pct",
        "上涨家数": "up_count", "下跌家数": "down_count",
    }, "ths")


def fetch_concept_fund_flow():
    """THS 概念资金流向"""
    df = ak.stock_fund_flow_concept(symbol="今日")
    return _normalize(df, {
        "行业": "board_name", "涨跌幅": "change_pct",
        "主力净流入-净额": "net_inflow",
    }, "fund_flow")


# ─── 任务注册表 ────────────────────────────────────────

TASKS = {
    # 行业
    "board_ths":         {"fn": fetch_board_ths,       "type": "industry", "source": "ths"},
    "board_em":          {"fn": fetch_board_em,        "type": "industry", "source": "em"},
    "board_sina":        {"fn": fetch_board_sina,       "type": "industry", "source": "sina"},
    "board_fund_flow":   {"fn": fetch_board_fund_flow,  "type": "industry", "source": "fund_flow"},
    # 概念
    "concept_sina":      {"fn": fetch_concept_sina,     "type": "concept",  "source": "sina"},
    "concept_em":        {"fn": fetch_concept_em,       "type": "concept",  "source": "em"},
    "concept_ths":       {"fn": fetch_concept_ths,      "type": "concept",  "source": "ths"},
    "concept_fund_flow": {"fn": fetch_concept_fund_flow,"type": "concept",  "source": "fund_flow"},
}


# ─── 数据校验 ──────────────────────────────────────────

def validate_data(df, collection_name, trading_day):
    """校验导入数据质量。"""
    issues = []
    cfg = VALIDATION.get(collection_name, {})
    label = cfg.get("label", collection_name)
    min_rows = cfg.get("min_rows", 10)

    # 1. 行数检查
    if len(df) < min_rows:
        issues.append(f"行数不足: {len(df)} < {min_rows}")

    # 2. 关键字段非空
    if "board_name" in df.columns:
        null_pct = df["board_name"].isna().sum() / len(df) * 100
        if null_pct > 0:
            issues.append(f"board_name 有 {null_pct:.0f}% 为空")

    if "change_pct" in df.columns:
        null_pct = df["change_pct"].isna().sum() / len(df) * 100
        if null_pct > 50:
            issues.append(f"change_pct 有 {null_pct:.0f}% 为空")

    # 3. 涨跌幅范围 (-15% ~ +15%)
    if "change_pct" in df.columns:
        valid = df["change_pct"].dropna()
        if len(valid) > 0:
            out_range = ((valid < -15) | (valid > 15)).sum()
            if out_range > len(valid) * 0.1:
                issues.append(f"涨跌幅异常: {out_range}/{len(valid)} 超出 ±15%")

    if issues:
        logger.warning(f"  ⚠️ {label} 校验问题: {'; '.join(issues)}")
        return False
    return True


def cross_validate(db, trading_day):
    """交叉验证：多源相同行业的涨跌幅偏差。"""
    sources = {}
    for col_name in ["board_ths", "board_em", "board_sina"]:
        col = db[col_name]
        docs = list(col.find({"dt": trading_day}, {"_id": 0, "board_name": 1, "change_pct": 1}))
        if docs:
            sources[col_name] = {d["board_name"]: d["change_pct"] for d in docs if d.get("change_pct") is not None}

    if len(sources) < 2:
        logger.info("  交叉验证: 不足 2 个源，跳过")
        return

    # 比对共同行业
    keys_list = [set(v.keys()) for v in sources.values()]
    common = set.intersection(*keys_list) if keys_list else set()

    if not common:
        logger.info("  交叉验证: 无共同行业")
        return

    mismatches = 0
    for name in list(common)[:10]:  # 只检查前 10 个
        vals = [sources[s].get(name) for s in sources if name in sources[s]]
        vals = [v for v in vals if v is not None]
        if len(vals) >= 2:
            diff = max(vals) - min(vals)
            if diff > 1.0:  # 偏差超过 1%
                mismatches += 1

    if mismatches:
        logger.warning(f"  交叉验证: {mismatches}/{min(10, len(common))} 个行业涨跌幅偏差 > 1%")
    else:
        logger.info(f"  交叉验证通过: {len(common)} 个共同行业，偏差 < 1%")


# ─── 主流程 ────────────────────────────────────────────

def show_status(db):
    """显示各集合当前状态。"""
    print("\n" + "=" * 60)
    print("  MongoDB 板块数据状态")
    print("=" * 60)

    for col_name, cfg in VALIDATION.items():
        col = db[col_name]
        count = col.count_documents({})
        label = cfg["label"]

        if count == 0:
            print(f"  ✗ {label:12s} ({col_name:20s}): 空")
            continue

        # 最新日期
        latest = col.find_one({}, sort=[("dt", -1)])
        dt = latest.get("dt", "?") if latest else "?"
        print(f"  {'✓' if count >= cfg['min_rows'] else '⚠'} {label:12s} ({col_name:20s}): {count:>4} 条 | 日期: {dt}")

    # 旧集合兼容
    for old_col in ["board_ranking", "concept_ranking"]:
        count = db[old_col].count_documents({})
        if count > 0:
            latest = db[old_col].find_one({}, sort=[("dt", -1)])
            dt = latest.get("dt", "?") if latest else "?"
            print(f"  📦 旧集合 {old_col:20s}: {count:>4} 条 | 日期: {dt}")

    print()


def run_import(db, tasks_to_run, trading_day, force=False):
    """执行导入任务。"""
    results = {"success": [], "skipped": [], "failed": []}

    for col_name, task_info in tasks_to_run.items():
        label = VALIDATION.get(col_name, {}).get("label", col_name)
        col = db[col_name]

        # 断点续传检查
        if not force:
            existing = col.find_one({"dt": trading_day})
            if existing:
                count = col.count_documents({"dt": trading_day})
                min_rows = VALIDATION.get(col_name, {}).get("min_rows", 10)
                if count >= min_rows:
                    logger.info(f"  ⏭ {label}: 今日已导入 ({count} 条)，跳过")
                    results["skipped"].append(col_name)
                    continue

        # 执行导入
        logger.info(f"  ⏳ {label}: 正在获取...")
        t0 = time.time()
        try:
            df = task_info["fn"]()
            elapsed = time.time() - t0

            if df is None or df.empty:
                logger.warning(f"  ✗ {label}: 返回空数据 ({elapsed:.1f}s)")
                results["failed"].append((col_name, "空数据"))
                continue

            # 添加交易日期
            df["dt"] = trading_day

            # 数据校验
            valid = validate_data(df, col_name, trading_day)

            # 写入 MongoDB（替换当天数据）
            col.delete_many({"dt": trading_day})
            docs = df.to_dict("records")
            # 清理 NaN → None（MongoDB 不支持 NaN）
            for doc in docs:
                for k, v in doc.items():
                    if pd.isna(v) if isinstance(v, (float, int)) else False:
                        doc[k] = None
            col.insert_many(docs, ordered=False)

            # 创建索引
            col.create_index([("dt", -1), ("board_name", 1)])

            status = "✓" if valid else "⚠"
            logger.info(f"  {status} {label}: {len(docs)} 条 ({elapsed:.1f}s)")
            results["success"].append(col_name)

        except Exception as e:
            elapsed = time.time() - t0
            err_msg = str(e)[:80]
            logger.error(f"  ✗ {label}: {err_msg} ({elapsed:.1f}s)")
            results["failed"].append((col_name, err_msg))

            # 网络中断等待
            if "Connection" in str(e) or "SSL" in str(e) or "Remote" in str(e):
                logger.info("    网络问题，等待 3 秒...")
                time.sleep(3)

    return results


def main():
    parser = argparse.ArgumentParser(description="行业/概念板块多源全量导入")
    parser.add_argument("--check", action="store_true", help="查看各集合状态")
    parser.add_argument("--source", type=str, default="", help="指定源: ths,em,sina,fund_flow")
    parser.add_argument("--type", type=str, default="", help="指定类型: industry,concept")
    parser.add_argument("--force", action="store_true", help="强制覆盖（忽略断点）")
    args = parser.parse_args()

    # 连接 MongoDB
    try:
        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client[DB_NAME]
        logger.info(f"MongoDB 已连接: {DB_NAME}")
    except Exception as e:
        logger.error(f"MongoDB 连接失败: {e}")
        sys.exit(1)

    if args.check:
        show_status(db)
        return

    trading_day = get_last_trading_day()
    logger.info(f"交易日: {trading_day}")

    # 过滤任务
    tasks_to_run = dict(TASKS)
    if args.source:
        allowed_sources = [s.strip() for s in args.source.split(",")]
        tasks_to_run = {k: v for k, v in tasks_to_run.items() if v["source"] in allowed_sources}
    if args.type:
        tasks_to_run = {k: v for k, v in tasks_to_run.items() if v["type"] == args.type}

    if not tasks_to_run:
        logger.warning("没有匹配的导入任务")
        return

    logger.info(f"待执行: {len(tasks_to_run)} 个任务")
    print()

    # 执行
    results = run_import(db, tasks_to_run, trading_day, force=args.force)

    # 交叉验证
    if any("board" in s for s in results["success"]):
        cross_validate(db, trading_day)

    # 汇总
    print("\n" + "=" * 60)
    print("  导入汇总")
    print("=" * 60)
    if results["success"]:
        print(f"  ✓ 成功: {', '.join(results['success'])}")
    if results["skipped"]:
        print(f"  ⏭ 跳过: {', '.join(results['skipped'])}")
    if results["failed"]:
        for name, err in results["failed"]:
            print(f"  ✗ 失败: {name} — {err}")
    print()

    # 最终状态
    show_status(db)


if __name__ == "__main__":
    main()
