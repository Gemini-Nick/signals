# -*- coding: utf-8 -*-
"""
🐲 行业/概念板块数据标准化适配器

将 THS/东财/新浪/BaoStock 等不同数据源的原始 DataFrame
映射到统一字段名，供聚类分析和 MongoDB 存储使用。

统一字段:
    board_name, change_pct, volume, amount, net_inflow,
    up_count, down_count, member_count, turnover_pct, avg_price,
    leader_name, leader_code, leader_change_pct, leader_price,
    source, dt
"""
import logging
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger("signals.data.board_normalizer")

# ─── 统一列名定义 ──────────────────────────────────────

UNIFIED_COLS = [
    "board_name",         # 板块名称
    "change_pct",         # 涨跌幅 (%)
    "volume",             # 总成交量
    "amount",             # 总成交额
    "net_inflow",         # 净流入 (仅 THS)
    "up_count",           # 上涨家数
    "down_count",         # 下跌家数
    "member_count",       # 公司家数
    "turnover_pct",       # 换手率 (%)
    "avg_price",          # 均价
    "leader_name",        # 领涨股名称
    "leader_code",        # 领涨股代码
    "leader_change_pct",  # 领涨股涨跌幅
    "leader_price",       # 领涨股价格
    "source",             # 数据源标签
    "dt",                 # 交易日期
]


def _ensure_unified(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """确保 DataFrame 包含所有统一列，缺失的补 NaN。"""
    df = df.copy()
    df["source"] = source
    if "dt" not in df.columns:
        df["dt"] = datetime.now().strftime("%Y-%m-%d")
    for col in UNIFIED_COLS:
        if col not in df.columns:
            df[col] = np.nan
    # 数值列强转
    for col in ["change_pct", "volume", "amount", "net_inflow",
                 "up_count", "down_count", "member_count", "turnover_pct",
                 "avg_price", "leader_change_pct", "leader_price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[UNIFIED_COLS]


# ─── 行业板块 normalize ────────────────────────────────

def normalize_ths_industry(df: pd.DataFrame) -> pd.DataFrame:
    """
    THS 行业排行 → 统一格式
    输入列: 序号, 板块, 涨跌幅, 总成交量, 总成交额, 净流入,
           上涨家数, 下跌家数, 均价, 领涨股, 领涨股-最新价, 领涨股-涨跌幅
    """
    mapped = df.rename(columns={
        "板块": "board_name",
        "涨跌幅": "change_pct",
        "总成交量": "volume",
        "总成交额": "amount",
        "净流入": "net_inflow",
        "上涨家数": "up_count",
        "下跌家数": "down_count",
        "均价": "avg_price",
        "领涨股": "leader_name",
        "领涨股-涨跌幅": "leader_change_pct",
        "领涨股-最新价": "leader_price",
    })
    return _ensure_unified(mapped, "ths")


def normalize_em_industry(df: pd.DataFrame) -> pd.DataFrame:
    """
    东财行业排行 → 统一格式
    输入列: 排名, 板块, 板块代码, 最新价, 涨跌额, 涨跌幅, 总市值, 换手率,
           上涨家数, 下跌家数, 领涨股票, 涨跌幅.1, ...
    """
    mapped = df.rename(columns={
        "板块": "board_name",
        "板块名称": "board_name",
        "涨跌幅": "change_pct",
        "换手率": "turnover_pct",
        "上涨家数": "up_count",
        "下跌家数": "down_count",
        "总成交量": "volume",
        "总成交额": "amount",
        "领涨股票": "leader_name",
        "领涨股票-涨跌幅": "leader_change_pct",
    })
    return _ensure_unified(mapped, "em")


def normalize_sina_industry(df: pd.DataFrame) -> pd.DataFrame:
    """
    新浪行业排行 → 统一格式
    输入列: label, 板块, 公司家数, 平均价格, 涨跌额, 涨跌幅,
           总成交量, 总成交额, 股票代码, 个股-涨跌幅, 个股-当前价, 个股-涨跌额, 股票名称
    """
    mapped = df.rename(columns={
        "板块": "board_name",
        "涨跌幅": "change_pct",
        "总成交量": "volume",
        "总成交额": "amount",
        "公司家数": "member_count",
        "平均价格": "avg_price",
        "股票名称": "leader_name",
        "股票代码": "leader_code",
        "个股-涨跌幅": "leader_change_pct",
        "个股-当前价": "leader_price",
    })
    return _ensure_unified(mapped, "sina")


def normalize_fund_flow_industry(df: pd.DataFrame) -> pd.DataFrame:
    """THS 行业资金流向 → 统一格式"""
    mapped = df.rename(columns={
        "行业": "board_name",
        "板块": "board_name",
        "涨跌幅": "change_pct",
        "主力净流入-净额": "net_inflow",
    })
    return _ensure_unified(mapped, "fund_flow")


# ─── 概念板块 normalize ────────────────────────────────

def normalize_sina_concept(df: pd.DataFrame) -> pd.DataFrame:
    """新浪概念排行 → 统一格式（与新浪行业结构完全相同）"""
    mapped = df.rename(columns={
        "板块": "board_name",
        "涨跌幅": "change_pct",
        "总成交量": "volume",
        "总成交额": "amount",
        "公司家数": "member_count",
        "平均价格": "avg_price",
        "股票名称": "leader_name",
        "股票代码": "leader_code",
        "个股-涨跌幅": "leader_change_pct",
        "个股-当前价": "leader_price",
    })
    return _ensure_unified(mapped, "sina")


def normalize_em_concept(df: pd.DataFrame) -> pd.DataFrame:
    """东财概念排行 → 统一格式"""
    mapped = df.rename(columns={
        "板块名称": "board_name",
        "板块": "board_name",
        "涨跌幅": "change_pct",
        "换手率": "turnover_pct",
        "上涨家数": "up_count",
        "下跌家数": "down_count",
        "总成交量": "volume",
        "总成交额": "amount",
        "领涨股票": "leader_name",
        "领涨股票-涨跌幅": "leader_change_pct",
    })
    return _ensure_unified(mapped, "em")


def normalize_ths_concept(df: pd.DataFrame) -> pd.DataFrame:
    """THS 概念排行 → 统一格式"""
    mapped = df.rename(columns={
        "概念名称": "board_name",
        "板块": "board_name",
        "涨跌幅": "change_pct",
        "上涨家数": "up_count",
        "下跌家数": "down_count",
    })
    return _ensure_unified(mapped, "ths")


def normalize_fund_flow_concept(df: pd.DataFrame) -> pd.DataFrame:
    """THS 概念资金流向 → 统一格式"""
    mapped = df.rename(columns={
        "行业": "board_name",
        "板块": "board_name",
        "涨跌幅": "change_pct",
        "主力净流入-净额": "net_inflow",
    })
    return _ensure_unified(mapped, "fund_flow")


# ─── 多源合并 ──────────────────────────────────────────

def merge_industry_sources(dfs: dict) -> pd.DataFrame:
    """
    合并多源行业数据：以 board_name 为 key，互补字段。

    Args:
        dfs: {"ths": df_ths, "em": df_em, "sina": df_sina, ...}

    Returns:
        合并后的 DataFrame，每个行业一行，字段取各源最优值
    """
    if not dfs:
        return pd.DataFrame(columns=UNIFIED_COLS)

    # 优先级：东财（最全）> THS（有净流入/广度）> 新浪（有公司家数）
    priority = ["em", "ths", "sina", "fund_flow"]

    merged = None
    sources_used = []

    for src in priority:
        if src not in dfs or dfs[src] is None or dfs[src].empty:
            continue
        df = dfs[src].copy()
        sources_used.append(src)

        if merged is None:
            merged = df
            continue

        # 以 board_name 为 key 合并，补充缺失字段
        for _, row in df.iterrows():
            name = row["board_name"]
            mask = merged["board_name"] == name
            if mask.any():
                # 已有该行业 → 补充 NaN 字段
                idx = merged.loc[mask].index[0]
                for col in UNIFIED_COLS:
                    if col in ("board_name", "source", "dt"):
                        continue
                    if pd.isna(merged.at[idx, col]) and pd.notna(row[col]):
                        merged.at[idx, col] = row[col]
            else:
                # 新行业 → 追加
                merged = pd.concat([merged, row.to_frame().T], ignore_index=True)

    if merged is not None and sources_used:
        merged["source"] = "+".join(sources_used)

    return merged if merged is not None else pd.DataFrame(columns=UNIFIED_COLS)
