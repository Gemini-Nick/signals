#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐲 隆小侠 LONG CLAW — 实线虚线分析框架

三层联动总入口：指数研判 → 行业研判 → 标的筛选

用法：
  python run.py                                    # 盘中监测（默认）
  python run.py --mode intraday                    # 盘中监测
  python run.py --mode intraday --industries ""    # 盘中，跳过行业分析
  python run.py --mode intraday --industries 有色金属,半导体  # 盘中，指定行业
  python run.py --mode review                      # 盘后复盘（默认今年以来）
  python run.py --mode review --start 924          # 盘后复盘（924新政）
  python run.py --mode review --start deepseek     # 盘后复盘（DeepSeek行情）
  python run.py --mode review --start 924,deepseek,tariff  # 多日期对比
  python run.py --mode index                       # 仅看指数报告（快速）
  python run.py --list-dates                       # 列出所有日期预设
  python run.py --mode import --file 锂电池深度.pdf          # 导入研究笔记（自动归档到 notes/YYYY/MM/）
  python run.py --mode import --file 锂电池深度.pdf --source 中信证券 --author 张三
  python run.py --mode intraday --market us              # 强制美股（盘中）
  python run.py --mode intraday --market a,hk            # 强制 A+H
  python run.py --mode intraday --market all              # 全市场（不做时段过滤）
  python run.py --mode backtest                           # 回测验证（评估历史信号）
  python run.py --mode backtest --signal-type 二买         # 仅看二买信号表现
  python run.py --mode backtest --freq-filter 日线         # 仅看日线信号
"""
import sys
import subprocess
import argparse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config


# ─────────────────────────────────────────────────────────
# 日期解析工具
# ─────────────────────────────────────────────────────────

def _resolve_start_date(raw: str) -> str:
    """
    将日期别名解析为 'YYYY-MM-DD' 格式。
    支持：预设别名 / YYYYMMDD / YYYY-MM-DD
    """
    preset = config.DATE_PRESETS.get(raw.lower())
    if preset:
        if "date" in preset:
            return preset["date"]
        offset = preset["offset"]
        if offset == "ytd":
            return f"{datetime.now().year}-01-01"
        return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
    # 支持 YYYYMMDD 格式自动转换
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw  # 已经是 YYYY-MM-DD


def _get_date_label(raw: str) -> str:
    """获取日期的标签说明。"""
    preset = config.DATE_PRESETS.get(raw.lower())
    if preset:
        return preset["label"]
    return ""


def _print_date_presets():
    """打印所有可用的日期预设。"""
    print("\n可用日期预设：")
    print("─" * 60)
    print(f"  {'别名':<12} {'日期':<14} {'说明'}")
    print("─" * 60)
    for key, info in config.DATE_PRESETS.items():
        if "date" in info:
            date_str = info["date"]
        elif info["offset"] == "ytd":
            date_str = f"{datetime.now().year}-01-01"
        else:
            date_str = f"(T-{info['offset']}天)"
        print(f"  {key:<12} {date_str:<14} {info['label']}")
    print("─" * 60)
    print("\n用法：python run.py --mode review --start <别名>")
    print("      python run.py --mode review --start 924,deepseek  (多日期)")


# ─────────────────────────────────────────────────────────
# 盘中模式：三层联动实时扫描
# ─────────────────────────────────────────────────────────

def _git_pull_notes():
    """从 GitHub 拉取最新研报文件"""
    r = subprocess.run(
        ["git", "pull", "--ff-only"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"  研报同步: {r.stdout.strip() or '已是最新'}")
    else:
        print(f"  [!] git pull 失败（本地优先继续）: {r.stderr.strip()}")


def _auto_import_new_notes(notes_dir: str):
    """对没有 .meta.yaml 的原始文件自动运行 import_note()"""
    from signals.research import import_note
    exts = {'.md', '.pdf', '.png', '.jpg', '.jpeg', '.txt'}
    for f in Path(notes_dir).rglob('*'):
        if f.suffix.lower() in exts:
            meta = f.parent / (f.stem + '.meta.yaml')
            if not meta.exists():
                print(f"  自动导入: {f.name}")
                try:
                    import_note(str(f))
                except Exception as e:
                    print(f"  [!] {f.name} 导入失败: {e}")


def _load_notes(args):
    """
    加载研究笔记，支持交互式选择。
    - 默认列出所有可用笔记，用户输入编号选择
    - 直接回车 = 全部加载
    - 输入 0 = 不加载任何笔记
    - 输入 1,3,5 = 只加载指定编号
    """
    from signals.research import load_all_notes, print_notes_summary
    notes_dir = getattr(args, 'notes', None) or config.NOTES_DIR

    # 启动时同步 + 懒加载新研报
    _git_pull_notes()
    _auto_import_new_notes(notes_dir)

    all_notes = load_all_notes(notes_dir)

    if not all_notes:
        print("  研报: 无有效研究笔记")
        return []

    # 列出可用笔记
    print(f"\n{'─'*50}")
    print(f"  可用研究笔记（{len(all_notes)} 篇）")
    print(f"{'─'*50}")
    for i, note in enumerate(all_notes, 1):
        sentiment_mark = {"看多": "+", "看空": "-", "中性": "~"}.get(note.sentiment, "?")
        sectors = "、".join(note.sectors[:2]) or "未分类"
        print(f"  [{i}] {note.date}  {note.title}")
        print(f"      {note.source_label}  |  {sectors}  |  {sentiment_mark}{note.sentiment}")
    print(f"{'─'*50}")

    # 交互选择
    try:
        choice = input("  选择笔记 (回车=全部, 0=跳过, 1,3=指定编号): ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""

    if choice == "0":
        print("  → 跳过研究笔记")
        return []
    elif choice == "":
        notes = all_notes
        print(f"  → 加载全部 {len(notes)} 篇")
    else:
        indices = []
        for part in choice.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part)
                if 1 <= idx <= len(all_notes):
                    indices.append(idx - 1)
        if indices:
            notes = [all_notes[i] for i in indices]
            print(f"  → 加载 {len(notes)} 篇: {', '.join(n.title for n in notes)}")
        else:
            print("  → 输入无效，加载全部")
            notes = all_notes

    if notes:
        print_notes_summary(notes)
    return notes


def run_intraday(args):
    """
    盘中模式：
    Layer 1 → 指数研判（MarketContext）
    Layer 2 → 双榜行业筛选（自动） + --industries 补充
    Layer 3 → 标的筛选（白名单 + 双榜代表股 + 研报标的）
    """
    from signals.core.market_hours import Market, filter_index_codes, filter_symbols
    from signals.layers.index_screener import IndexScreener
    from signals.layers.screener import IntraDayScreener
    from signals.research import (
        get_noted_industries, get_noted_stocks,
        match_notes_for_symbol, check_resonance,
    )

    # ── 市场时段路由 ─────────────────────────────────────
    active = _get_active_markets(args)
    ak_codes, futu_codes, us_codes = filter_index_codes(
        active, config.INDEX_AK_CODES, config.INDEX_FUTU_CODES, config.INDEX_US_CODES,
    )

    # ── 加载研究笔记 ─────────────────────────────────────
    notes = _load_notes(args)
    noted_industries = get_noted_industries(notes)
    noted_stocks = get_noted_stocks(notes)

    # ── Layer 1：指数研判 ──────────────────────────────────
    screener_l1 = IndexScreener(
        ak_codes=ak_codes, futu_codes=futu_codes, us_codes=us_codes,
    )
    ctx = screener_l1.run()

    if not ctx.gate_industry_scan:
        print("⚠️  市场偏空，建议观望，仅扫描白名单。")

    # ── Layer 2：双榜行业筛选 + 多维度个股入池 ─────────────
    ranking_stocks: list = []
    if Market.A not in active:
        print(">>> A股未开盘，跳过 Layer 2 行业筛选")
    elif config.RANK_TOP_N > 0:
        from signals.layers.industry import get_industry_representatives

        print(f"\n>>> Layer 2 双榜行业筛选（各取前{config.RANK_TOP_N}名）")
        print("  " + "─" * 60)

        try:
            gain_list, composite_list, merged_list = get_industry_representatives(
                config.RANK_TOP_N)
        except Exception as e:
            print(f"  [!] 行业筛选异常（{e}），跳过 Layer 2", flush=True)
            gain_list, composite_list, merged_list = [], [], []

        # ── Layer 2A：涨幅排行榜 ─────────────────────────
        if gain_list:
            print(f"\n  >>> Layer 2A 行业涨幅排行（今日前{len(gain_list)}名）")
            print(f"  {'排名':<4} {'行业':<12} {'涨幅':>7}  {'净流入(亿)':>10}")
            print("  " + "─" * 55)
            for r in gain_list:
                sign = "+" if r.gain_pct >= 0 else ""
                print(f"  {r.gain_rank:<4} {r.name:<12} "
                      f"{sign}{r.gain_pct:.2f}%  {r.net_inflow:>10.2f}")
                for c in r.candidates:
                    print(f"       {c.role}: {c.name}({c.code}"
                          f"{', ' + c.detail if c.detail else ''})")
                if r.pool_codes:
                    print(f"       → 入池: {', '.join(r.pool_codes)}")

        # ── Layer 2B：综合强度排行榜 ─────────────────────
        if composite_list:
            print(f"\n  >>> Layer 2B 行业综合强度排行（今日前{len(composite_list)}名）")
            print(f"  {'排名':<4} {'行业':<10} {'综合分':>6} {'涨幅':>7} "
                  f"{'流入(亿)':>9} {'涨停':>4} {'强势':>4} {'续板':>4}")
            print("  " + "─" * 60)
            for r in composite_list:
                sign = "+" if r.gain_pct >= 0 else ""
                tag = " ★" if r.source == "both" else ""
                print(f"  {r.composite_rank:<4} {r.name:<10} "
                      f"{r.composite_score:>6.1f} {sign}{r.gain_pct:>6.2f}% "
                      f"{r.net_inflow:>9.2f} {r.zt_count:>4} "
                      f"{r.strong_count:>4} {r.zbgc_count:>4}{tag}")
                for c in r.candidates:
                    already = " [涨幅榜已入池]" if r.source == "both" else ""
                    print(f"       {c.role}: {c.name}({c.code}"
                          f"{', ' + c.detail if c.detail else ''}){already}")
                if r.pool_codes and r.source != "both":
                    print(f"       → 入池: {', '.join(r.pool_codes)}")

        # ── 汇总 ─────────────────────────────────────────
        for r in merged_list:
            ranking_stocks.extend(r.pool_codes)
        ranking_stocks = list(dict.fromkeys(ranking_stocks))

        if merged_list:
            gain_only = sum(1 for r in merged_list if r.source == "gain")
            comp_only = sum(1 for r in merged_list if r.source == "composite")
            both_cnt = sum(1 for r in merged_list if r.source == "both")
            print(f"\n  >>> Layer 2 合计: 涨幅榜 {len(gain_list)} 行业 + "
                  f"综合榜 {comp_only} 新增行业"
                  f"{f' + {both_cnt} 重叠' if both_cnt else ''}"
                  f" = {len(merged_list)} 行业")
            print(f"  >>> Layer 2 共 {len(ranking_stocks)} 只代表股纳入筛选池")

    # ── --industries 补充行业（直接入池，不做CZSC研判）──────
    named_stocks: list = []
    if Market.A in active:
        industry_names = _parse_industries(args)
        # 研报中涉及的行业也自动加入补充池
        if noted_industries:
            for ni in noted_industries:
                if ni not in industry_names:
                    industry_names.append(ni)
            print(f"  研报行业已加入扫描池: {', '.join(noted_industries)}")
        if industry_names:
            from signals.layers.industry import get_industry_stocks as _get_ind_stocks
            max_per = getattr(config, "RANK_MAX_STOCKS_PER_IND", 5)
            print(f"\n>>> Layer 2 补充行业：{', '.join(industry_names)}")
            for ind in industry_names:
                stocks = _get_ind_stocks(ind)
                named_stocks.extend(stocks[:max_per])
                print(f"  [{ind}] 取 {min(max_per, len(stocks))} 只")
            named_stocks = list(dict.fromkeys(named_stocks))

    # ── Layer 3：标的筛选 ──────────────────────────────────
    print("\n>>> Layer 3 标的筛选 ...")
    extra_stocks = list(dict.fromkeys(ranking_stocks + named_stocks))
    all_symbols = list(dict.fromkeys(
        config.WHITELIST + noted_stocks + extra_stocks
    ))  # 去重保序
    all_symbols = filter_symbols(active, all_symbols)

    screener_l3 = IntraDayScreener(
        symbols=all_symbols, freqs=config.MONITOR_FREQS, notes=notes,
    )
    if extra_stocks:
        wl_results = screener_l3.run_whitelist()
        screener_l3.initialize(extra_stocks)
        extra_results = screener_l3.scan_once(extra_stocks)
        screener_l3.print_results(extra_results, title="行业成分股筛选结果")

        combined = {}
        for r in wl_results + extra_results:
            if r.symbol not in combined or r.total_score > combined[r.symbol].total_score:
                combined[r.symbol] = r
        merged = sorted(combined.values(), key=lambda x: -x.total_score)
        screener_l3.print_results(merged, title="综合筛选结果（三层联动）")
    else:
        screener_l3.run_whitelist()

    # ── 飞书推送（卡片模式，含折叠面板）────────────────────
    try:
        from signals.notify.feishu import send_card
        card = ctx.to_feishu_card()
        send_card(card)
    except Exception as e:
        print(f"  [!] 飞书推送异常: {e}")


# ─────────────────────────────────────────────────────────
# 导入模式：研究笔记 → 结构化元数据
# ─────────────────────────────────────────────────────────

def run_import(args):
    """
    导入研究笔记：提取文本 → 自动识别 → 生成 .meta.yaml。
    支持 .md / .pdf / .png / .jpg / .txt 格式。
    文件自动归档到 notes/YYYY/MM/ 子目录（按导入日期）。
    """
    from signals.research import import_note
    import os
    import shutil

    if not args.file:
        print("错误：--mode import 必须指定 --file 参数")
        print("示例：python run.py --mode import --file 锂电池深度.pdf")
        return

    if not os.path.exists(args.file):
        print(f"错误：文件不存在: {args.file}")
        return

    # 自动归档：如果文件不在 notes/YYYY/MM/ 下，复制过去
    src_path = os.path.abspath(args.file)
    month_dir = config.notes_month_dir()
    dest_path = os.path.join(month_dir, os.path.basename(args.file))

    if os.path.abspath(os.path.dirname(src_path)) != os.path.abspath(month_dir):
        if os.path.exists(dest_path):
            stem, ext = os.path.splitext(os.path.basename(args.file))
            import time as _time
            dest_path = os.path.join(month_dir, f"{stem}_{int(_time.time())}{ext}")
        shutil.copy2(src_path, dest_path)
        print(f"  已归档到: {dest_path}")
        file_to_import = dest_path
    else:
        file_to_import = src_path

    print(f"\n>>> 导入研究笔记")
    note = import_note(
        file_path=file_to_import,
        source=args.source or "",
        author=args.author or "",
    )

    print(f"\n  导入完成:")
    print(f"  标题:   {note.title}")
    print(f"  来源:   {note.source_label}")
    print(f"  日期:   {note.date}")
    print(f"  行业:   {'、'.join(note.sectors) or '未识别（请手动编辑 .meta.yaml）'}")
    print(f"  标的:   {'、'.join(note.stocks[:5]) or '未识别（请手动编辑 .meta.yaml）'}")
    print(f"  观点:   {note.sentiment}")
    if note.catalysts:
        print(f"  催化:   {'、'.join(note.catalysts[:3])}")
    print(f"\n  元数据: {note.meta_path}")
    print(f"  ↑ 可手动编辑此文件修正自动识别结果\n")


# ─────────────────────────────────────────────────────────
# 仅指数模式：快速查看大市方向
# ─────────────────────────────────────────────────────────

def run_backtest(args):
    """
    回测验证模式：评估历史信号的前瞻表现，输出统计报告。

    信号存档由 screener / review_screener 自动完成（每次运行时存入 SQLite）。
    本模式负责：评估到期信号 → 买卖配对 → 生成双视角报告 → 输出权重建议。
    """
    from signals.core.backtest import run_backtest as _run_backtest

    print(f"\n{'═'*52}")
    print(f"  回测验证模式 — 信号自我进化")
    print(f"{'═'*52}")

    signal_type = getattr(args, "signal_type", "") or ""
    freq_filter = getattr(args, "freq_filter", "") or ""
    _run_backtest(signal_type=signal_type, freq_filter=freq_filter)


def run_index_only(args):
    """仅运行 Layer 1，快速输出指数报告。"""
    from signals.core.market_hours import filter_index_codes
    from signals.layers.index_screener import IndexScreener

    active = _get_active_markets(args)
    ak_codes, futu_codes, us_codes = filter_index_codes(
        active, config.INDEX_AK_CODES, config.INDEX_FUTU_CODES, config.INDEX_US_CODES,
    )
    screener = IndexScreener(
        ak_codes=ak_codes, futu_codes=futu_codes, us_codes=us_codes,
    )
    ctx = screener.run()

    # ── 飞书推送（卡片模式）────────────────────────────────
    if ctx:
        try:
            from signals.notify.feishu import send_card
            card = ctx.to_feishu_card()
            send_card(card)
        except Exception as e:
            print(f"  [!] 飞书推送异常: {e}")


# ─────────────────────────────────────────────────────────
# 盘后复盘模式
# ─────────────────────────────────────────────────────────

def run_review(args):
    """
    盘后复盘模式：从指定关键时间节点加载完整历史结构。
    支持单日期和多日期（逗号分隔）两种模式。
    """
    raw_dates = args.start.split(",")
    dates = [(_resolve_start_date(d.strip()), d.strip()) for d in raw_dates]

    if len(dates) == 1:
        _run_single_review(dates[0][0], dates[0][1], args)
    else:
        _run_multi_review(dates, args)


def _run_single_review(start_date: str, raw_alias: str, args):
    """
    单日期盘后复盘：
    Layer 1 → 指数历史结构
    Layer 2 → 双榜行业筛选（历史模式）
    Layer 3 → 个股日线复盘（白名单 + 代表股 + 补充行业）
    """
    from signals.layers.index_screener import IndexScreener
    from signals.layers.review_screener import review_stock_daily

    label = _get_date_label(raw_alias)
    label_str = f"（{label}）" if label else ""

    print(f"\n{'═'*52}")
    print(f"  盘后复盘模式  起始：{start_date}{label_str}")
    print(f"{'═'*52}")

    # ── Layer 1：指数复盘 ──────────────────────────────────
    screener_l1 = IndexScreener()
    ctx = screener_l1.run_review(start_date)

    # ── Layer 2：双榜行业筛选（使用最近交易日Pool数据）────
    ranking_stocks: list = []
    if config.RANK_TOP_N > 0:
        from signals.layers.industry import get_industry_representatives

        # Pool API仅保留近期数据（~3-4周），始终用最近交易日
        pool_date = datetime.now().strftime("%Y%m%d")
        print(f"\n>>> Layer 2 双榜行业筛选（Pool日期：{pool_date}）")
        print("  " + "─" * 60)

        try:
            gain_list, composite_list, merged_list = get_industry_representatives(
                config.RANK_TOP_N, date_str=pool_date)
        except Exception as e:
            print(f"  [!] 行业筛选异常（{e}），跳过 Layer 2", flush=True)
            gain_list, composite_list, merged_list = [], [], []

        # ── Layer 2A：涨停密度排行（盘后替代涨幅排行）─────
        if gain_list:
            print(f"\n  >>> Layer 2A 行业涨停密度排行（前{len(gain_list)}名）")
            print(f"  {'排名':<4} {'行业':<12} {'涨停':>4}")
            print("  " + "─" * 40)
            for i, r in enumerate(gain_list, 1):
                print(f"  {i:<4} {r.name:<12} {r.zt_count:>4}只")
                for c in r.candidates:
                    print(f"       {c.role}: {c.name}({c.code}"
                          f"{', ' + c.detail if c.detail else ''})")
                if r.pool_codes:
                    print(f"       → 入池: {', '.join(r.pool_codes)}")
        else:
            print("  [!] 涨停池无数据（可能为非交易时段），Layer 2A 跳过")

        # ── Layer 2B：综合强度排行（5维评分）───────────────
        if composite_list:
            print(f"\n  >>> Layer 2B 行业综合强度排行（前{len(composite_list)}名）")
            print(f"  {'排名':<4} {'行业':<10} {'综合分':>6} "
                  f"{'涨停':>4} {'强势':>4} {'续板':>4}")
            print("  " + "─" * 50)
            for r in composite_list:
                tag = " ★" if r.source == "both" else ""
                print(f"  {r.composite_rank:<4} {r.name:<10} "
                      f"{r.composite_score:>6.1f} {r.zt_count:>4} "
                      f"{r.strong_count:>4} {r.zbgc_count:>4}{tag}")
                for c in r.candidates:
                    already = " [密度榜已入池]" if r.source == "both" else ""
                    print(f"       {c.role}: {c.name}({c.code}"
                          f"{', ' + c.detail if c.detail else ''}){already}")
                if r.pool_codes and r.source != "both":
                    print(f"       → 入池: {', '.join(r.pool_codes)}")

        # ── 汇总 ─────────────────────────────────────────
        for r in merged_list:
            ranking_stocks.extend(r.pool_codes)
        ranking_stocks = list(dict.fromkeys(ranking_stocks))

        if merged_list:
            gain_only = sum(1 for r in merged_list if r.source == "gain")
            comp_only = sum(1 for r in merged_list if r.source == "composite")
            both_cnt = sum(1 for r in merged_list if r.source == "both")
            print(f"\n  >>> Layer 2 合计: 涨停榜 {len(gain_list)} 行业 + "
                  f"综合榜 {comp_only} 新增行业"
                  f"{f' + {both_cnt} 重叠' if both_cnt else ''}"
                  f" = {len(merged_list)} 行业")
            print(f"  >>> Layer 2 共 {len(ranking_stocks)} 只代表股纳入复盘池")
        else:
            print("\n  >>> Layer 2 无行业数据，仅白名单复盘")

    # ── --industries 补充行业 ──────────────────────────────
    named_stocks: list = []
    industry_names = _parse_industries(args)
    if industry_names:
        from signals.layers.industry import get_industry_stocks as _get_ind_stocks
        max_per = getattr(config, "RANK_MAX_STOCKS_PER_IND", 5)
        print(f"\n>>> Layer 2 补充行业：{', '.join(industry_names)}")
        for ind in industry_names:
            stocks = _get_ind_stocks(ind)
            named_stocks.extend(stocks[:max_per])
            print(f"  [{ind}] 取 {min(max_per, len(stocks))} 只")
        named_stocks = list(dict.fromkeys(named_stocks))

    # ── Layer 3：个股日线复盘 ──────────────────────────────
    extra_stocks = list(dict.fromkeys(ranking_stocks + named_stocks))
    all_symbols = list(dict.fromkeys(config.WHITELIST + extra_stocks))
    review_stock_daily(all_symbols, start_date)


def _run_multi_review(dates: list, args):
    """
    多日期批量对比模式。

    :param dates: [(resolved_date, raw_alias), ...]
    """
    from signals.layers.index_screener import IndexScreener
    from signals.layers.industry import get_industry_representatives
    from signals.layers.review_screener import review_stock_daily

    # 日期标签
    date_labels = []
    for resolved, raw in dates:
        label = _get_date_label(raw) or resolved
        date_labels.append((resolved, raw, label))

    dates_display = ", ".join(f"{d[0]}({d[2]})" for d in date_labels)
    print(f"\n{'═'*60}")
    print(f"  盘后复盘模式（多日期对比）")
    print(f"  日期：{dates_display}")
    print(f"{'═'*60}")

    # ── Layer 1：指数复盘（共享，取最早日期）──────────────
    earliest = min(d[0] for d in date_labels)
    screener_l1 = IndexScreener()
    ctx = screener_l1.run_review(earliest)

    # ── Layer 2：每日期独立 ────────────────────────────────
    all_ranking_stocks: list = []
    per_date_results: list = []  # [(date, label, gain_list, comp_list, merged_list)]

    if config.RANK_TOP_N > 0:
        for resolved, raw, label in date_labels:
            date_yyyymmdd = resolved.replace("-", "")
            print(f"\n>>> Layer 2 行业筛选：{resolved}（{label}）")
            print("  " + "─" * 50)

            try:
                gain_list, composite_list, merged_list = get_industry_representatives(
                    config.RANK_TOP_N, date_str=date_yyyymmdd)
            except Exception as e:
                print(f"  [!] {resolved} 行业筛选异常（{e}），跳过", flush=True)
                gain_list, composite_list, merged_list = [], [], []

            per_date_results.append((resolved, label, gain_list, composite_list, merged_list))

            # 简要输出
            if gain_list:
                top3 = ", ".join(f"{r.name}({r.zt_count}只)" for r in gain_list[:3])
                print(f"  涨停密度 Top 3: {top3}")
            if composite_list:
                top3 = ", ".join(f"{r.name}({r.composite_score:.0f}分)" for r in composite_list[:3])
                print(f"  综合强度 Top 3: {top3}")
            if not gain_list and not composite_list:
                print(f"  [!] {resolved} 无Pool数据（东财仅保留近3-4周），跳过")

            for r in merged_list:
                all_ranking_stocks.extend(r.pool_codes)

        all_ranking_stocks = list(dict.fromkeys(all_ranking_stocks))

        # ── 行业×日期矩阵 ─────────────────────────────────
        _print_industry_date_matrix(per_date_results)

        print(f"\n  >>> Layer 2 多日期合计: {len(all_ranking_stocks)} 只代表股纳入复盘池")

    # ── --industries 补充行业 ──────────────────────────────
    named_stocks: list = []
    industry_names = _parse_industries(args)
    if industry_names:
        from signals.layers.industry import get_industry_stocks as _get_ind_stocks
        max_per = getattr(config, "RANK_MAX_STOCKS_PER_IND", 5)
        print(f"\n>>> Layer 2 补充行业：{', '.join(industry_names)}")
        for ind in industry_names:
            stocks = _get_ind_stocks(ind)
            named_stocks.extend(stocks[:max_per])
            print(f"  [{ind}] 取 {min(max_per, len(stocks))} 只")
        named_stocks = list(dict.fromkeys(named_stocks))

    # ── Layer 3：合并去重，统一复盘 ────────────────────────
    extra_stocks = list(dict.fromkeys(all_ranking_stocks + named_stocks))
    all_symbols = list(dict.fromkeys(config.WHITELIST + extra_stocks))
    review_stock_daily(all_symbols, earliest)


def _print_industry_date_matrix(per_date_results: list):
    """
    输出行业×日期矩阵。
    :param per_date_results: [(date, label, gain_list, comp_list, merged_list), ...]
    """
    if not per_date_results:
        return

    # 收集所有出现过的行业
    industry_dates: dict = {}  # {行业名: {date: (zt_count, is_comp_top)}}
    for resolved, label, gain_list, comp_list, merged_list in per_date_results:
        comp_names = {r.name for r in comp_list}
        for r in merged_list:
            industry_dates.setdefault(r.name, {})[resolved] = (
                r.zt_count, r.name in comp_names)

    if not industry_dates:
        return

    # 按出现次数降序排列
    dates_list = [r[0] for r in per_date_results]
    sorted_industries = sorted(
        industry_dates.items(),
        key=lambda x: (-len(x[1]), x[0])
    )

    # 短标签
    short_labels = {}
    for resolved, label in [(r[0], r[1]) for r in per_date_results]:
        short = label[:8] if len(label) > 8 else label
        short_labels[resolved] = short

    print(f"\n>>> Layer 2 多日期行业矩阵（{len(dates_list)}个日期）")
    header = f"  {'行业':<10}"
    for d in dates_list:
        header += f" {short_labels[d]:>10}"
    header += f"  {'出现':>6}"
    print(header)
    print("  " + "─" * (12 + 11 * len(dates_list) + 8))

    for ind_name, date_map in sorted_industries[:20]:
        row = f"  {ind_name:<10}"
        count = 0
        for d in dates_list:
            if d in date_map:
                zt_cnt, is_comp = date_map[d]
                marker = "★" if is_comp else " "
                row += f" {marker}涨停{zt_cnt:<2}{'':<3}"
                count += 1
            else:
                row += f" {'—':>10}"
        row += f"  {count}/{len(dates_list)}"
        if count == len(dates_list):
            row += " ★"
        print(row)
    print("  " + "─" * (12 + 11 * len(dates_list) + 8))
    print("  ★ = 该日期综合榜 Top 10")


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

def _parse_market_override(raw: str):
    """解析 --market 参数为 Market 集合。"""
    from signals.core.market_hours import Market
    _map = {"a": Market.A, "hk": Market.HK, "us": Market.US}
    if raw.lower() == "all":
        return {Market.A, Market.HK, Market.US}
    return {_map[m.strip().lower()] for m in raw.split(",")
            if m.strip().lower() in _map}


def _get_active_markets(args):
    """根据 --market 参数或当前时间返回活跃市场集合。"""
    from signals.core.market_hours import (
        Market, get_active_markets, describe_sessions,
    )
    market_arg = getattr(args, "market", None)
    if market_arg:
        active = _parse_market_override(market_arg)
        print(f">>> 手动指定市场: {', '.join(m.value for m in sorted(active))}")
    else:
        active = get_active_markets()
        print(f">>> 市场时段检测: {describe_sessions()}")

    if not active:
        print(">>> 当前无市场开盘，运行全量分析")
        active = {Market.A, Market.HK, Market.US}
    return active


def _parse_industries(args) -> list:
    """
    解析行业列表：
    - --industries 有色金属,半导体  → ["有色金属", "半导体"]
    - --industries ""               → []（跳过）
    - 未传参                         → config.WATCH_INDUSTRIES
    """
    if args.industries is None:
        return list(config.WATCH_INDUSTRIES)
    val = args.industries.strip()
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


# ─────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────

def main():
    # 构建预设列表字符串
    preset_keys = ", ".join(config.DATE_PRESETS.keys())

    parser = argparse.ArgumentParser(
        description="🐲 隆小侠 LONG CLAW — 实线虚线分析框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
示例：
  python run.py                                    # 盘中监测（默认）
  python run.py --mode intraday                    # 盘中监测
  python run.py --mode index                       # 仅看指数（快速）
  python run.py --mode intraday --industries ""    # 盘中，跳过行业
  python run.py --mode intraday --industries 有色金属,半导体
  python run.py --mode review                      # 盘后复盘（今年以来）
  python run.py --mode review --start 924          # 盘后复盘（924新政）
  python run.py --mode review --start deepseek     # 盘后复盘（DeepSeek行情）
  python run.py --mode review --start 924,deepseek # 多日期对比
  python run.py --mode intraday --market us         # 强制美股
  python run.py --mode intraday --market all        # 全市场（不做时段过滤）
  python run.py --mode import --file 锂电池.pdf --source 中信证券 --author 张三
  python run.py --list-dates                       # 列出所有日期预设

可用日期预设：{preset_keys}
        """
    )
    parser.add_argument(
        "--mode",
        default="intraday",
        choices=["intraday", "review", "index", "import", "backtest"],
        help="运行模式：intraday / review / index / import / backtest"
    )
    parser.add_argument(
        "--start",
        default="ytd",
        metavar="日期或别名",
        help=f"盘后复盘起始日期（--mode review 时使用），默认 ytd（今年以来）。"
             f"可用预设：{preset_keys}。支持逗号分隔多日期对比。"
    )
    parser.add_argument(
        "--industries",
        default=None,
        metavar="行业1,行业2",
        help="覆盖 config.WATCH_INDUSTRIES，逗号分隔。传空字符串=跳过行业分析"
    )
    parser.add_argument(
        "--notes",
        default=None,
        metavar="PATH",
        help="研究笔记目录路径，默认 config.NOTES_DIR"
    )
    # import 模式专用参数
    parser.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="导入研究笔记的文件路径（--mode import 时必须）"
    )
    parser.add_argument(
        "--source",
        default=None,
        metavar="NAME",
        help="研究笔记来源机构（如 中信证券、华泰研究所）"
    )
    parser.add_argument(
        "--author",
        default=None,
        metavar="NAME",
        help="研究笔记作者/分析师"
    )
    parser.add_argument(
        "--market",
        default=None,
        metavar="MARKET",
        help="强制指定市场：a / hk / us / a,hk / all。"
             "默认自动检测当前开盘市场。仅 intraday/index 模式生效。"
    )
    # backtest 模式专用参数
    parser.add_argument(
        "--signal-type",
        default=None,
        metavar="TYPE",
        help="回测筛选：仅分析指定信号类型（如 二买、三买）"
    )
    parser.add_argument(
        "--freq-filter",
        default=None,
        metavar="FREQ",
        help="回测筛选：仅分析指定频率（如 日线、30分钟）"
    )
    parser.add_argument(
        "--list-dates",
        action="store_true",
        help="列出所有可用的日期预设并退出"
    )

    args = parser.parse_args()

    # --list-dates 优先处理
    if args.list_dates:
        _print_date_presets()
        return

    dispatch = {
        "intraday": run_intraday,
        "review":   run_review,
        "index":    run_index_only,
        "import":   run_import,
        "backtest": run_backtest,
    }
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
