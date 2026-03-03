#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signals 系统总入口 — 三层联动（指数 → 行业 → 标的）

用法：
  python run.py                                    # 盘中监测（默认）
  python run.py --mode intraday                    # 盘中监测
  python run.py --mode intraday --industries ""    # 盘中，跳过行业分析
  python run.py --mode intraday --industries 有色金属,半导体  # 盘中，指定行业
  python run.py --mode review --start 2024-09-24  # 盘后复盘（九月行情）
  python run.py --mode review --start 2025-01-06  # 盘后复盘（DeepSeek行情）
  python run.py --mode index                       # 仅看指数报告（快速）
  python run.py --mode import --file notes/xxx.pdf # 导入研究笔记
  python run.py --mode import --file notes/xxx.pdf --source 中信证券 --author 张三
"""
import sys
import argparse

sys.path.insert(0, "/Users/zhangqilong/Desktop/Signals")

import config


# ─────────────────────────────────────────────────────────
# 盘中模式：三层联动实时扫描
# ─────────────────────────────────────────────────────────

def _load_notes(args):
    """加载研究笔记（如果 notes/ 目录存在）"""
    from signals.research import load_all_notes, print_notes_summary
    notes_dir = getattr(args, 'notes', None) or config.NOTES_DIR
    notes = load_all_notes(notes_dir)
    if notes:
        print_notes_summary(notes)
    return notes


def run_intraday(args):
    """
    盘中模式：
    Layer 1 → 指数研判（MarketContext）
    Layer 2 → 行业强度（可选，由 --industries 或 config.WATCH_INDUSTRIES 控制）
    Layer 3 → 标的筛选（白名单 + 行业成分股）
    研报维度 → 独立展示，与技术面分数并列
    """
    from signals.index_screener import IndexScreener
    from signals.screener import IntraDayScreener
    from signals.research import (
        get_noted_industries, get_noted_stocks,
        match_notes_for_symbol, check_resonance,
    )

    # ── 加载研究笔记 ─────────────────────────────────────
    notes = _load_notes(args)
    noted_industries = get_noted_industries(notes)
    noted_stocks = get_noted_stocks(notes)

    # ── Layer 1：指数研判 ──────────────────────────────────
    screener_l1 = IndexScreener()
    ctx = screener_l1.run()

    if not ctx.gate_industry_scan:
        print("⚠️  市场偏空，建议观望，仅扫描白名单。")

    # ── Layer 2：行业分析（可选）────────────────────────────
    industry_stocks: list = []
    industry_names = _parse_industries(args)
    # 研报中涉及的行业自动加入扫描池
    if noted_industries:
        for ni in noted_industries:
            if ni not in industry_names:
                industry_names.append(ni)
        print(f"  研报行业已加入扫描池: {', '.join(noted_industries)}")

    if industry_names and ctx.gate_industry_scan:
        from signals.industry import score_industry
        print(f"\n>>> Layer 2 行业研判：{', '.join(industry_names)}")
        ind_scores = []
        for ind in industry_names:
            print(f"  分析：{ind} ...", flush=True)
            sc = score_industry(ind)
            ind_scores.append(sc)
            print(f"    {sc.summary}", flush=True)

        # 从强势行业取成分股
        from signals.industry import get_industry_stocks
        for sc in ind_scores:
            if sc.is_strong:
                stocks = get_industry_stocks(sc.name)
                industry_stocks.extend(stocks[:30])  # 每个行业最多取30只
                print(f"  [{sc.name}] 取 {min(30, len(stocks))} 只成分股进入 Layer 3")
    elif industry_names and not ctx.gate_industry_scan:
        print("  市场偏空，跳过行业分析。")

    # ── Layer 3：标的筛选 ──────────────────────────────────
    print("\n>>> Layer 3 标的筛选 ...")
    # 研报中的标的也加入候选池
    all_symbols = list(dict.fromkeys(
        config.WHITELIST + noted_stocks + industry_stocks
    ))  # 去重保序

    screener_l3 = IntraDayScreener(
        symbols=all_symbols, freqs=config.MONITOR_FREQS, notes=notes,
    )
    if industry_stocks:
        # 两轮分别输出
        wl_results = screener_l3.run_whitelist()
        if industry_stocks:
            # 直接扫描已合并的标的池
            screener_l3.initialize(industry_stocks)
            ind_results = screener_l3.scan_once(industry_stocks)
            screener_l3.print_results(ind_results, title="行业成分股筛选结果")

            # 合并排序
            combined = {}
            for r in wl_results + ind_results:
                if r.symbol not in combined or r.total_score > combined[r.symbol].total_score:
                    combined[r.symbol] = r
            merged = sorted(combined.values(), key=lambda x: -x.total_score)
            screener_l3.print_results(merged, title="综合筛选结果（三层联动）")
    else:
        screener_l3.run_whitelist()


# ─────────────────────────────────────────────────────────
# 导入模式：研究笔记 → 结构化元数据
# ─────────────────────────────────────────────────────────

def run_import(args):
    """
    导入研究笔记：提取文本 → 自动识别 → 生成 .meta.yaml。
    支持 .md / .pdf / .png / .jpg / .txt 格式。
    """
    from signals.research import import_note
    import os

    if not args.file:
        print("错误：--mode import 必须指定 --file 参数")
        print("示例：python run.py --mode import --file notes/锂电池深度.pdf")
        return

    if not os.path.exists(args.file):
        print(f"错误：文件不存在: {args.file}")
        return

    print(f"\n>>> 导入研究笔记")
    note = import_note(
        file_path=args.file,
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

def run_index_only(args):
    """仅运行 Layer 1，快速输出指数报告。"""
    from signals.index_screener import IndexScreener
    screener = IndexScreener()
    screener.run()


# ─────────────────────────────────────────────────────────
# 盘后复盘模式
# ─────────────────────────────────────────────────────────

def run_review(args):
    """
    盘后复盘模式：从指定关键时间节点加载完整历史结构。
    Layer 1 → 指数历史结构
    Layer 2 → 行业复盘（可选）
    Layer 3 → 个股日线复盘（白名单）
    """
    from signals.review_screener import ReviewScreener

    industry_names = _parse_industries(args)

    reviewer = ReviewScreener(
        start_date=args.start,
        industries=industry_names,
    )
    reviewer.run_all()


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

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
    parser = argparse.ArgumentParser(
        description="Signals — 缠论三层联动分析系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python run.py                                    # 盘中监测（默认）
  python run.py --mode intraday                    # 盘中监测
  python run.py --mode index                       # 仅看指数（快速）
  python run.py --mode intraday --industries ""    # 盘中，跳过行业
  python run.py --mode intraday --industries 有色金属,半导体
  python run.py --mode review --start 2024-09-24  # 盘后复盘（九月行情起）
  python run.py --mode review --start 2025-01-06  # 盘后复盘（DeepSeek行情起）
  python run.py --mode import --file notes/锂电池.pdf --source 中信证券 --author 张三
        """
    )
    parser.add_argument(
        "--mode",
        default="intraday",
        choices=["intraday", "review", "index", "import"],
        help="运行模式：intraday / review / index / import（导入研究笔记）"
    )
    parser.add_argument(
        "--start",
        default="2024-09-24",
        metavar="YYYY-MM-DD",
        help="盘后复盘起始日期（--mode review 时使用），默认 2024-09-24"
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

    args = parser.parse_args()

    dispatch = {
        "intraday": run_intraday,
        "review":   run_review,
        "index":    run_index_only,
        "import":   run_import,
    }
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
