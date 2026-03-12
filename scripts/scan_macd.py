# -*- coding: utf-8 -*-
"""
MACD 信号全量筛查脚本 — A股+港股日线/周线

功能：
  1. 获取全A股/港股主要标的列表
  2. 并发拉取日线+周线数据
  3. MACD 信号检测（Pattern A 零上回踩 + Pattern B 零下企稳）
  4. 按行业板块 + 概念板块分布输出结果

用法：
  python scripts/scan_macd.py                    # 默认：A股日线+周线
  python scripts/scan_macd.py --hk               # 含港股
  python scripts/scan_macd.py --concepts          # 含概念板块分布
  python scripts/scan_macd.py --workers 12        # 调整并发数
  python scripts/scan_macd.py --test 50           # 测试模式：只扫前50只
"""
import sys
import os
import json
import time
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from signals.data.fetcher import no_proxy
from signals.core.macd_detector import detect_macd_signals, MACDSignal

# 缓存目录
CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────
# 数据获取
# ─────────────────────────────────────────────────────

def get_all_a_stocks():
    """获取全A股列表，返回 [(code6, name), ...]"""
    import akshare as ak
    with no_proxy():
        df = ak.stock_info_a_code_name()
    stocks = []
    for _, row in df.iterrows():
        code = str(row["code"]).zfill(6)
        name = row["name"]
        # 过滤 ST、退市
        if "ST" in name or "退" in name:
            continue
        stocks.append((code, name))
    return stocks


def get_hk_main_stocks():
    """港股主要标的（恒生科技+蓝筹）"""
    # 常见港股代码 (5位)
    hk_stocks = [
        ("09988", "阿里巴巴"), ("00700", "腾讯控股"), ("03690", "美团"),
        ("09618", "京东集团"), ("09999", "网易"), ("01024", "快手"),
        ("09888", "百度集团"), ("02015", "理想汽车"), ("09868", "小鹏汽车"),
        ("01211", "比亚迪"), ("02382", "舜宇光学"), ("00981", "中芯国际"),
        ("09961", "携程集团"), ("06060", "众安在线"), ("01810", "小米集团"),
        ("00005", "汇丰控股"), ("00388", "香港交易所"), ("02318", "中国平安"),
        ("00941", "中国移动"), ("00883", "中国海油"), ("01398", "工商银行"),
        ("03968", "招商银行"), ("02628", "中国人寿"), ("00857", "中国石油"),
    ]
    return hk_stocks


def fetch_a_daily(code6, days=365):
    """获取A股日线"""
    import akshare as ak
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    edt = datetime.now().strftime("%Y%m%d")
    with no_proxy():
        df = ak.stock_zh_a_hist(symbol=code6, period="daily",
                                 start_date=sdt, end_date=edt, adjust="qfq")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                             "最低": "low", "收盘": "close", "成交量": "vol"})
    df["dt"] = pd.to_datetime(df["dt"])
    return df.set_index("dt")


def fetch_a_weekly(code6, days=730):
    """获取A股周线"""
    import akshare as ak
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    edt = datetime.now().strftime("%Y%m%d")
    with no_proxy():
        df = ak.stock_zh_a_hist(symbol=code6, period="weekly",
                                 start_date=sdt, end_date=edt, adjust="qfq")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                             "最低": "low", "收盘": "close", "成交量": "vol"})
    df["dt"] = pd.to_datetime(df["dt"])
    return df.set_index("dt")


def fetch_hk_daily(code, days=365):
    """获取港股日线"""
    import akshare as ak
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    edt = datetime.now().strftime("%Y%m%d")
    with no_proxy():
        df = ak.stock_hk_hist(symbol=code, period="daily",
                               start_date=sdt, end_date=edt, adjust="qfq")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                             "最低": "low", "收盘": "close", "成交量": "vol"})
    df["dt"] = pd.to_datetime(df["dt"])
    return df.set_index("dt")


def fetch_hk_weekly(code, days=730):
    """获取港股周线"""
    import akshare as ak
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    edt = datetime.now().strftime("%Y%m%d")
    with no_proxy():
        df = ak.stock_hk_hist(symbol=code, period="weekly",
                               start_date=sdt, end_date=edt, adjust="qfq")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                             "最低": "low", "收盘": "close", "成交量": "vol"})
    df["dt"] = pd.to_datetime(df["dt"])
    return df.set_index("dt")


# ─────────────────────────────────────────────────────
# 行业 / 概念 映射
# ─────────────────────────────────────────────────────

def build_industry_map():
    """构建 code6 → 行业名 反向映射"""
    import akshare as ak
    cache_file = CACHE_DIR / "stock_industry_map.json"

    # 当日缓存
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        if data.get("date") == datetime.now().strftime("%Y%m%d"):
            return data["mapping"]

    print("  构建行业映射... (约30-60秒)")
    mapping = {}
    with no_proxy():
        df_boards = ak.stock_board_industry_name_em()
    names = df_boards["板块名称"].tolist()

    for i, name in enumerate(names):
        try:
            with no_proxy():
                cons = ak.stock_board_industry_cons_em(symbol=name)
            if cons is None or cons.empty:
                continue
            code_col = next((c for c in ["代码", "code", "股票代码"] if c in cons.columns), None)
            if not code_col:
                continue
            for code in cons[code_col].astype(str):
                mapping[str(code).zfill(6)] = name
        except Exception:
            continue
        if (i + 1) % 20 == 0:
            print(f"    进度: {i+1}/{len(names)}")

    # 保存缓存
    cache_file.write_text(json.dumps({
        "date": datetime.now().strftime("%Y%m%d"),
        "mapping": mapping
    }, ensure_ascii=False))
    print(f"  行业映射完成: {len(mapping)} 只股票")
    return mapping


def build_concept_map():
    """构建 code6 → [概念名, ...] 反向映射"""
    import akshare as ak
    cache_file = CACHE_DIR / "stock_concept_map.json"

    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        if data.get("date") == datetime.now().strftime("%Y%m%d"):
            return data["mapping"]

    print("  构建概念映射... (约3-5分钟)")
    mapping = defaultdict(list)  # code6 → [concept1, ...]

    # 噪音概念过滤
    try:
        from config import CONCEPT_NOISE_PATTERNS
        noise = set(CONCEPT_NOISE_PATTERNS)
    except Exception:
        noise = set()

    with no_proxy():
        df_concepts = ak.stock_board_concept_name_em()
    concept_names = df_concepts["板块名称"].tolist()

    for i, name in enumerate(concept_names):
        if name in noise:
            continue
        try:
            with no_proxy():
                cons = ak.stock_board_concept_cons_em(symbol=name)
            if cons is None or cons.empty:
                continue
            code_col = next((c for c in ["代码", "code", "股票代码"] if c in cons.columns), None)
            if not code_col:
                continue
            for code in cons[code_col].astype(str):
                mapping[str(code).zfill(6)].append(name)
        except Exception:
            continue
        if (i + 1) % 50 == 0:
            print(f"    进度: {i+1}/{len(concept_names)}")

    # 保存
    result = {k: v for k, v in mapping.items()}
    cache_file.write_text(json.dumps({
        "date": datetime.now().strftime("%Y%m%d"),
        "mapping": result
    }, ensure_ascii=False))
    print(f"  概念映射完成: {len(result)} 只股票")
    return result


# ─────────────────────────────────────────────────────
# 扫描逻辑
# ─────────────────────────────────────────────────────

def scan_stock(code6, name, market="A", lookback=15):
    """
    扫描单只股票的日线+周线 MACD 信号。
    返回 {"code6", "name", "market", "daily_signals": [...], "weekly_signals": [...]}
    """
    result = {"code6": code6, "name": name, "market": market,
              "daily_signals": [], "weekly_signals": []}

    if market == "A":
        symbol = f"SH.{code6}" if code6.startswith(("6", "5")) else f"SZ.{code6}"
    else:
        symbol = f"HK.{code6}"

    try:
        if market == "A":
            df_daily = fetch_a_daily(code6)
        else:
            df_daily = fetch_hk_daily(code6)
        if not df_daily.empty:
            sigs = detect_macd_signals(df_daily, symbol, "日线", lookback=lookback)
            result["daily_signals"] = sigs
    except Exception:
        pass

    try:
        if market == "A":
            df_weekly = fetch_a_weekly(code6)
        else:
            df_weekly = fetch_hk_weekly(code6)
        if not df_weekly.empty:
            sigs = detect_macd_signals(df_weekly, symbol, "周线", lookback=lookback)
            result["weekly_signals"] = sigs
    except Exception:
        pass

    return result


def scan_all(stocks, market="A", workers=8, lookback=15):
    """并发扫描所有股票"""
    results = []
    total = len(stocks)
    done = 0
    errors = 0

    print(f"\n  扫描 {total} 只{market}股 (workers={workers})...")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for code6, name in stocks:
            fut = pool.submit(scan_stock, code6, name, market, lookback)
            futs[fut] = (code6, name)

        for fut in as_completed(futs):
            done += 1
            try:
                result = fut.result()
                if result["daily_signals"] or result["weekly_signals"]:
                    results.append(result)
            except Exception:
                errors += 1

            if done % 100 == 0 or done == total:
                hit = len(results)
                print(f"    进度: {done}/{total} ({done*100//total}%)  "
                      f"命中: {hit}  错误: {errors}")

    return results


# ─────────────────────────────────────────────────────
# 输出报告
# ─────────────────────────────────────────────────────

def print_report(results, industry_map, concept_map=None):
    """打印分组报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 分类
    daily_a = []  # Pattern A 日线
    daily_b = []  # Pattern B 日线
    weekly_a = []
    weekly_b = []
    resonance = []  # 日线+周线同时有信号

    for r in results:
        d_sigs = r["daily_signals"]
        w_sigs = r["weekly_signals"]

        for sig in d_sigs:
            if "A_" in sig.pattern:
                daily_a.append((r, sig))
            else:
                daily_b.append((r, sig))

        for sig in w_sigs:
            if "A_" in sig.pattern:
                weekly_a.append((r, sig))
            else:
                weekly_b.append((r, sig))

        if d_sigs and w_sigs:
            resonance.append(r)

    print(f"\n{'═'*85}")
    print(f"  MACD 信号筛查报告  |  {now}")
    print(f"{'═'*85}")

    total_hits = len(results)
    total_daily = len(daily_a) + len(daily_b)
    total_weekly = len(weekly_a) + len(weekly_b)
    print(f"\n  总命中: {total_hits} 只  日线信号: {total_daily}  "
          f"周线信号: {total_weekly}  共振: {len(resonance)}")

    # 日线 Pattern A
    _print_section("日线 Pattern A (零上回踩)", daily_a, industry_map, concept_map)
    _print_section("日线 Pattern B (零下企稳)", daily_b, industry_map, concept_map)
    _print_section("周线 Pattern A (零上回踩)", weekly_a, industry_map, concept_map)
    _print_section("周线 Pattern B (零下企稳)", weekly_b, industry_map, concept_map)

    # 共振
    if resonance:
        print(f"\n  ── 日线+周线共振（最强信号）{'─'*40}")
        for r in resonance:
            code6 = r["code6"]
            name = r["name"]
            industry = industry_map.get(code6, "未知")
            d_pat = r["daily_signals"][0].pattern if r["daily_signals"] else "-"
            w_pat = r["weekly_signals"][0].pattern if r["weekly_signals"] else "-"
            d_conf = r["daily_signals"][0].confidence if r["daily_signals"] else 0
            w_conf = r["weekly_signals"][0].confidence if r["weekly_signals"] else 0
            print(f"    {code6}  {name:<8}  {industry:<8}  "
                  f"日:{d_pat}({d_conf:.2f})  周:{w_pat}({w_conf:.2f})")

    print(f"\n{'═'*85}")


def _print_section(title, items, industry_map, concept_map):
    """打印一个信号类型的分组"""
    if not items:
        return

    print(f"\n  ── {title} ({len(items)}只) {'─'*40}")

    # 行业分布
    ind_count = defaultdict(int)
    for r, sig in items:
        ind = industry_map.get(r["code6"], "未知")
        ind_count[ind] += 1

    sorted_inds = sorted(ind_count.items(), key=lambda x: -x[1])
    ind_str = "  ".join(f"{k}({v})" for k, v in sorted_inds[:10])
    print(f"  行业: {ind_str}")

    # 概念分布（如有）
    if concept_map:
        con_count = defaultdict(int)
        for r, sig in items:
            concepts = concept_map.get(r["code6"], [])
            for c in concepts:
                con_count[c] += 1
        if con_count:
            sorted_cons = sorted(con_count.items(), key=lambda x: -x[1])
            con_str = "  ".join(f"{k}({v})" for k, v in sorted_cons[:10])
            print(f"  概念: {con_str}")

    # 按置信度排序输出
    items_sorted = sorted(items, key=lambda x: -x[1].confidence)

    print(f"    {'代码':<10} {'名称':<8} {'行业':<8} {'置信度':>6} "
          f"{'价格':>8} {'DEA':>8} {'HIST':>8}  详情")
    print(f"    {'─'*78}")

    for r, sig in items_sorted[:30]:  # 最多显示30只
        code6 = r["code6"]
        name = r["name"][:6]
        industry = industry_map.get(code6, "未知")[:6]
        print(f"    {code6:<10} {name:<8} {industry:<8} {sig.confidence:>6.2f} "
              f"{sig.price:>8.2f} {sig.dea:>8.3f} {sig.hist:>8.3f}  {sig.details}")

    if len(items_sorted) > 30:
        print(f"    ... 还有 {len(items_sorted) - 30} 只")


# ─────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MACD 信号全量筛查")
    parser.add_argument("--hk", action="store_true", help="包含港股主要标的")
    parser.add_argument("--concepts", action="store_true", help="包含概念板块分布")
    parser.add_argument("--workers", type=int, default=8, help="并发线程数 (默认8)")
    parser.add_argument("--test", type=int, default=0, help="测试模式：只扫前N只")
    parser.add_argument("--lookback", type=int, default=15, help="信号回看窗口 (默认15)")
    args = parser.parse_args()

    print("═" * 85)
    print("  MACD 信号全量筛查")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  配置: workers={args.workers}, lookback={args.lookback}")
    print("═" * 85)

    # 1. 获取股票列表
    print("\n[1] 获取股票列表...")
    a_stocks = get_all_a_stocks()
    if args.test:
        a_stocks = a_stocks[:args.test]
    print(f"  A股: {len(a_stocks)} 只")

    hk_stocks = []
    if args.hk:
        hk_stocks = get_hk_main_stocks()
        print(f"  港股: {len(hk_stocks)} 只")

    # 2. 构建行业映射
    print("\n[2] 构建行业/概念映射...")
    industry_map = build_industry_map()

    concept_map = None
    if args.concepts:
        concept_map = build_concept_map()

    # 3. 扫描
    print("\n[3] 开始扫描...")
    t0 = time.time()

    all_results = []
    if a_stocks:
        a_results = scan_all(a_stocks, market="A", workers=args.workers,
                             lookback=args.lookback)
        all_results.extend(a_results)

    if hk_stocks:
        hk_results = scan_all(hk_stocks, market="HK", workers=1,
                              lookback=args.lookback)
        all_results.extend(hk_results)

    elapsed = time.time() - t0
    print(f"\n  扫描完成 ({elapsed:.1f}秒)，共 {len(all_results)} 只命中")

    # 4. 输出报告
    print_report(all_results, industry_map, concept_map)


if __name__ == "__main__":
    main()
