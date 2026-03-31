#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ── 静默 tqdm（AKShare 内部使用，会干扰 Rich Dashboard）──────
import tqdm as _tqdm_mod                              # noqa: E402
_tqdm_mod.tqdm = lambda iterable, *a, **kw: iterable  # noqa: E402
try:
    import tqdm.auto as _tqdm_auto                    # noqa: E402
    _tqdm_auto.tqdm = _tqdm_mod.tqdm
except ImportError:
    pass

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
  python run.py --mode sim --create --start 2026-01-14   # 创建仿真快照
  python run.py --mode sim --session 2026-01-14          # 执行仿真回放
  python run.py --mode sim --list-sessions               # 列出可用快照
  python run.py --mode web                               # Web UI（TradingView 风格）
  python run.py --mode web --port 9000                   # 指定端口
  python run.py --mode analog                            # 历史形态匹配（全部指数）
  python run.py --mode analog --symbol 沪深300           # 指定单个指数匹配
  python run.py --mode index --push                      # 指数分析 + 推送到 Vercel
"""
import sys
import subprocess
import argparse
import time as _time_mod
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config


# ─────────────────────────────────────────────────────────
# 进度面板（已迁移到 signals/dashboard/）
# ─────────────────────────────────────────────────────────

def _fmt_sec(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec//60)}m{int(sec%60)}s"


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

    # 默认全部加载，列出摘要
    notes = all_notes
    print(f"  研报: 自动加载全部 {len(notes)} 篇")

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
    from signals.data.fetcher import detect_market
    from signals.layers.index_screener import IndexScreener
    from signals.layers.screener import IntraDayScreener
    from signals.research import (
        get_noted_industries, get_noted_stocks,
        match_notes_for_symbol, check_resonance,
    )
    from signals.dashboard import Dashboard

    # 市场检测必须在 Dashboard 之前（raw print 不能与 Rich Live 共存）
    active = _get_active_markets(args)

    dash = Dashboard(mode="intraday")
    ak_codes, futu_codes, us_codes = filter_index_codes(
        active, config.INDEX_AK_CODES, config.INDEX_FUTU_CODES, config.INDEX_US_CODES,
    )

    # ── 加载研究笔记 ─────────────────────────────────────
    dash.phase_start("research")
    dash.pause()  # input() 交互需暂停面板
    notes = _load_notes(args)
    dash.resume()
    noted_industries = get_noted_industries(notes)
    noted_stocks = get_noted_stocks(notes)
    dash.phase_end("research", detail=f"{len(notes)} 篇" if notes else "无")

    # ── Layer 1：指数研判 ──────────────────────────────────
    screener_l1 = IndexScreener(
        ak_codes=ak_codes, futu_codes=futu_codes, us_codes=us_codes,
    )
    ctx = screener_l1.run()
    if ctx:
        dash.set_context(direction=ctx.overall_direction,
                         style=ctx.recommended_style)

    # 保存 screener 引用供 --push 使用
    args._screener = screener_l1

    if not ctx.gate_industry_scan:
        dash.log("  [yellow]⚠️  市场偏空，建议观望，仅扫描白名单。[/yellow]")

    # ── Layer 2：双榜行业筛选 + 多维度个股入池 ─────────────
    ranking_stocks: list = []
    merged_list: list = []
    if Market.A not in active:
        dash.phase_skip("L2.ranking", "A股未开盘")
        dash.log(">>> A股未开盘，跳过 Layer 2 行业筛选")
    elif config.RANK_TOP_N > 0:
        from signals.layers.industry import get_industry_representatives

        dash.phase_start("L2.ranking")

        try:
            gain_list, composite_list, merged_list, concepts, oversold_list, l2_stats = get_industry_representatives(
                config.RANK_TOP_N)
        except Exception as e:
            dash.log(f"  [!] 行业筛选异常（{e}），跳过 Layer 2")
            dash.task_error("L2.ranking", "行业筛选", str(e))
            gain_list, composite_list, merged_list, concepts, oversold_list = [], [], [], [], []
            l2_stats = {}

        # ── 情绪周期更新（L2 涨跌停数据回传 MarketContext）───
        if l2_stats:
            ctx.update_sentiment(
                zt_total=l2_stats.get("zt_total", 0),
                dt_total=l2_stats.get("dt_total", 0),
                lianban_max=l2_stats.get("lianban_max", 0),
            )

        # ── 汇总入池 ─────────────────────────────────────
        for r in merged_list:
            ranking_stocks.extend(r.pool_codes)
        ranking_stocks = list(dict.fromkeys(ranking_stocks))

        dash.phase_end("L2.ranking", detail=f"{len(ranking_stocks)} 只入池")

        # ── 轮动阶段识别 ─────────────────────────────────
        if gain_list or composite_list:
            try:
                from signals.core.rotation import detect_rotation_stage, suggest_allocation
                rot = detect_rotation_stage(gain_list, composite_list)
                ctx.rotation_stage = rot.stage
                ctx.rotation_detail = rot.format_line()
                _, alloc_str = suggest_allocation(rot, ctx.sentiment_phase)
                ctx.allocation_suggestion = alloc_str
                # P3-4: 轮动持续时间和速度
                ctx.rotation_duration = rot.duration_days
                ctx.rotation_velocity = rot.velocity
                ctx.rotation_peak_warning = rot.peak_warning
                ctx.rotation_peak_detail = rot.peak_detail
            except Exception:
                pass

        # ── 暂停面板，打印 L2 报告 ────────────────────────
        dash.pause()
        print(f"\n>>> Layer 2 双榜行业筛选（各取前{config.RANK_TOP_N}名）")
        print("  " + "─" * 60)

        if gain_list:
            print(f"\n  >>> Layer 2A 行业涨幅排行（今日前{len(gain_list)}名）")
            print(f"  {'排名':<4} {'行业':<24} {'涨幅':>7}  {'净流入(亿)':>10}")
            print("  " + "─" * 55)
            for r in gain_list:
                sign = "+" if r.gain_pct >= 0 else ""
                print(f"  {r.gain_rank:<4} {r.display_name:<24} "
                      f"{sign}{r.gain_pct:.2f}%  {r.net_inflow:>10.2f}")
                for c in r.candidates:
                    print(f"       {c.role}: {c.name}({c.code}"
                          f"{', ' + c.detail if c.detail else ''})")
                if r.pool_codes:
                    print(f"       → 入池: {', '.join(r.pool_codes)}")

        if composite_list:
            print(f"\n  >>> Layer 2B 行业综合强度排行（今日前{len(composite_list)}名）")
            print(f"  {'排名':<4} {'行业':<22} {'综合分':>6} {'涨幅':>7} "
                  f"{'流入(亿)':>9} {'涨停':>4} {'强势':>4} {'续板':>4}")
            print("  " + "─" * 60)
            for r in composite_list:
                sign = "+" if r.gain_pct >= 0 else ""
                tag = " ★" if r.source == "both" else ""
                print(f"  {r.composite_rank:<4} {r.display_name:<22} "
                      f"{r.composite_score:>6.1f} {sign}{r.gain_pct:>6.2f}% "
                      f"{r.net_inflow:>9.2f} {r.zt_count:>4} "
                      f"{r.strong_count:>4} {r.zbgc_count:>4}{tag}")
                for c in r.candidates:
                    already = " [涨幅榜已入池]" if r.source == "both" else ""
                    print(f"       {c.role}: {c.name}({c.code}"
                          f"{', ' + c.detail if c.detail else ''}){already}")
                if r.pool_codes and r.source != "both":
                    print(f"       → 入池: {', '.join(r.pool_codes)}")

        # ── 超跌关注 ─────────────────────────────────────
        if oversold_list:
            print(f"\n  >>> ⬇️  超跌关注（评分>=40）")
            print(f"  {'行业':<22} {'超跌分':>6} {'今日涨幅':>8} {'详情'}")
            print("  " + "─" * 55)
            for r in oversold_list:
                sign = "+" if r.gain_pct >= 0 else ""
                print(f"  {r.display_name:<22} {r.oversold_score:>6.1f} "
                      f"{sign}{r.gain_pct:.2f}%  {r.oversold_detail}")

        if merged_list:
            comp_only = sum(1 for r in merged_list if r.source == "composite")
            both_cnt = sum(1 for r in merged_list if r.source == "both")
            print(f"\n  >>> Layer 2 合计: 涨幅榜 {len(gain_list)} 行业 + "
                  f"综合榜 {comp_only} 新增行业"
                  f"{f' + {both_cnt} 重叠' if both_cnt else ''}"
                  f" = {len(merged_list)} 行业")
            print(f"  >>> Layer 2 共 {len(ranking_stocks)} 只代表股纳入筛选池")

        # ── 轮动阶段 + 配置建议 ────────────────────────
        if ctx.rotation_stage:
            print(f"\n  >>> {ctx.rotation_detail}")
        if ctx.allocation_suggestion:
            print(f"  >>> 📦 {ctx.allocation_suggestion}")

        # ── 概念板块 Top N ─────────────────────────────
        _CP_ICON = {"防守": "🛡", "进攻": "⚔", "周期": "🔄", "中性": ""}
        if concepts:
            print(f"\n  >>> 概念板块热度排行（前{len(concepts)}名）")
            print(f"  {'排名':<4} {'概念':<14} {'涨幅':>7}  {'属性':<6}")
            print("  " + "─" * 40)
            for i, cp in enumerate(concepts, 1):
                sign = "+" if cp.gain_pct >= 0 else ""
                cp_icon = _CP_ICON.get(cp.sector_type, "")
                print(f"  {i:<4} {cp.name:<14} "
                      f"{sign}{cp.gain_pct:.2f}%  {cp.sector_type}{cp_icon}")

        # ── 盘中恐慌评估 + 抄底候选 + 主题追踪 ──────────
        try:
            from signals.core.panic_detector import assess_intraday_panic
            name_df = l2_stats.get("name_df") if l2_stats else None
            panic = assess_intraday_panic(
                ctx.reports, screener_l1.analyzers, name_df)

            # 存入 ctx 供输出层使用
            ctx.panic_score = panic.score
            ctx.panic_level = panic.level
            ctx.panic_detail = panic.detail

            # ① 始终显示盘中情绪评估
            _p_icons = {"恐慌": "🔴", "偏弱": "🟡", "正常": "🟢"}
            _p_icon = _p_icons.get(panic.level, "⚪")
            print(f"\n  >>> {_p_icon} 盘中情绪: "
                  f"{panic.score:.0f}/100 ({panic.level})")
            print(f"      {panic.detail}")
            if panic.level == "恐慌":
                print(f"  💡 恐慌=底部信号 → 关注超跌+支撑位的反弹机会")

            # ② 偏弱/恐慌时显示抄底候选
            if panic.score >= 40:
                from signals.layers.industry import get_bottom_fishing_candidates
                _themes = [t.strip() for t in
                           (args.themes.split(",") if args.themes
                            else config.WATCH_THEMES) if t.strip()]
                bottom = get_bottom_fishing_candidates(
                    name_df, oversold_list, panic.score, _themes)
                if bottom:
                    ctx.bottom_candidates = [b.name for b in bottom]
                    print(f"\n  >>> 🎯 抄底候选板块:")
                    for _bi, b in enumerate(bottom, 1):
                        parts = [f"今日{b.gain_pct:+.1f}%"]
                        if b.oversold_score > 0:
                            parts.append(f"历史超跌{b.oversold_score:.0f}")
                        if b.sector_type != "中性":
                            parts.append(f"{b.sector_type}型")
                        if b.rotation_line:
                            parts.append(f"{b.rotation_line}线")
                        print(f"      {_bi}. {b.name} ({', '.join(parts)})")

            # ③ 主题追踪 — 独立于恐慌门控，始终显示
            _themes = [t.strip() for t in
                       (args.themes.split(",") if args.themes
                        else config.WATCH_THEMES) if t.strip()]
            if _themes:
                from signals.core.theme_tracker import match_themes, format_theme_hits
                hits = match_themes(_themes, name_df, concepts, panic.level)
                fmt = format_theme_hits(hits)
                if fmt:
                    ctx.theme_summary = fmt
                    print(f"\n  >>> 🏷 主题追踪: {fmt}")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("恐慌评估失败: %s", e)
            print(f"  [!] 恐慌评估失败: {e}")

        dash.resume()

    dash.set_l2_count(len(ranking_stocks))

    # ── --industries 补充行业（直接入池，不做CZSC研判）──────
    named_stocks: list = []
    if Market.A in active:
        industry_names = _parse_industries(args)
        # 研报中涉及的行业也自动加入补充池
        if noted_industries:
            for ni in noted_industries:
                if ni not in industry_names:
                    industry_names.append(ni)
            dash.detail(f"  研报行业已加入扫描池: {', '.join(noted_industries)}")
        if industry_names:
            dash.phase_start("L2.supplement", total=len(industry_names))
            from signals.layers.industry import get_industry_stocks as _get_ind_stocks
            max_per = getattr(config, "RANK_MAX_STOCKS_PER_IND", 5)
            for ind in industry_names:
                dash.task_start("L2.supplement", ind)
                stocks = _get_ind_stocks(ind)
                named_stocks.extend(stocks[:max_per])
                dash.detail(f"  [{ind}] 取 {min(max_per, len(stocks))} 只")
                dash.task_done("L2.supplement", ind)
            named_stocks = list(dict.fromkeys(named_stocks))
            dash.phase_end("L2.supplement",
                           detail=f"{len(named_stocks)} 只")

    # ── 名称解析器（L2 数据注入）────────────────────────────
    from signals.core.stock_names import get_resolver
    resolver = get_resolver()
    resolver.inject_from_whitelist(getattr(config, "WHITELIST_MAP", {}))
    if merged_list:
        resolver.inject_from_rankings(merged_list)

    # ── Layer 3：标的筛选 ──────────────────────────────────
    # L3 需要分钟线，非交易时段数据源不可用，自动跳过
    from signals.core.market_hours import get_active_markets as _get_live_markets, Market
    _live = _get_live_markets()
    _all_pool = (config.WHITELIST + noted_stocks + ranking_stocks + named_stocks)
    _a_symbols = [s for s in _all_pool if detect_market(s) == "A"]
    _us_symbols = [s for s in _all_pool if detect_market(s) == "US"]
    _need_a = bool(_a_symbols)
    _need_us = bool(_us_symbols)
    _skip_l3 = ((_need_a and Market.A not in _live and not _need_us)
                or (_need_a and Market.A not in _live
                    and _need_us and Market.US not in _live))

    if _skip_l3:
        dash.phase_skip("L3.init", "非交易时段")
        dash.phase_skip("L3.scan", "非交易时段")
        dash.log("\n>>> Layer 3 标的筛选 ... 跳过（非交易时段，分钟线不可用）")
    else:
        # 智能入池：行业轮选优先，白名单/研报保底
        l3_pool = _smart_pool(
            config.WHITELIST, noted_stocks, merged_list, named_stocks,
            max_total=getattr(config, "L3_MAX_SYMBOLS", 20),
        )
        l3_pool = filter_symbols(active, l3_pool)
        dash.log(f"\n>>> Layer 3 标的筛选（{len(l3_pool)} 只入池，从 {len(ranking_stocks)} 候选中智能筛选）...")

        screener_l3 = IntraDayScreener(
            symbols=l3_pool, freqs=config.MONITOR_FREQS, notes=notes,
        )

        try:
            # 一次性初始化所有标的（dashboard 活跃，显示进度）
            screener_l3.initialize(l3_pool)
            l3_results = screener_l3.scan_once(
                l3_pool,
                sentiment_phase=ctx.sentiment_phase,
                consensus_risk_level=(ctx.consensus_risk.level
                                      if ctx.consensus_risk else "低"),
            )

            above_cnt = sum(1 for r in l3_results
                           if r.total_score >= config.SCORE_THRESHOLD
                           and r.signal_count > 0)
            dash.set_l3_count(above_cnt)

            # 暂停面板，打印综合表 + 操作建议
            dash.pause()
            screener_l3.print_results(
                l3_results, title="L3 标的筛选结果（三层联动）",
                resolver=resolver)

            from signals.core.summary import print_action_summary
            print_action_summary(ctx, l3_results, resolver=resolver,
                                 notes=notes)
            dash.resume()
        finally:
            screener_l3.close()

    # ── 飞书推送（卡片模式，含折叠面板）────────────────────
    if not config.FEISHU_APP_ID:
        dash.phase_skip("feishu", "未配置")
    else:
        dash.phase_start("feishu")
        try:
            from signals.notify import send_card
            card = ctx.to_feishu_card()
            send_card(card)
        except Exception as e:
            dash.log(f"  [!] 飞书推送异常: {e}")
        dash.phase_end("feishu")

    # ── 最终汇总面板 ────────────────────────────────────
    dash.finish()


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


def run_autoresearch(args):
    """
    AutoResearch 模式 — 策略参数自主研究循环。

    借鉴 Karpathy autoresearch: 变异参数 → 回测 → 保留/回退 → 永不停止。
    """
    from signals.autoresearch.agent import AutoResearchAgent

    dry_run = getattr(args, "dry_run", False)
    experiments = getattr(args, "experiments", None)

    agent = AutoResearchAgent(dry_run=dry_run)
    try:
        if experiments:
            agent.run_n(experiments)
        else:
            agent.run_forever()
    finally:
        agent.close()


def run_index_only(args):
    """仅运行 Layer 1，快速输出指数报告。"""
    from signals.core.market_hours import filter_index_codes
    from signals.layers.index_screener import IndexScreener
    from signals.dashboard import Dashboard

    # 市场检测必须在 Dashboard 之前（raw print 不能与 Rich Live 共存）
    active = _get_active_markets(args)

    dash = Dashboard(mode="index")

    ak_codes, futu_codes, us_codes = filter_index_codes(
        active, config.INDEX_AK_CODES, config.INDEX_FUTU_CODES, config.INDEX_US_CODES,
    )
    screener = IndexScreener(
        ak_codes=ak_codes, futu_codes=futu_codes, us_codes=us_codes,
    )
    ctx = screener.run()

    # 保存 screener 引用供 --push 使用
    args._screener = screener

    # ── 飞书推送（卡片模式）────────────────────────────────
    if not config.FEISHU_APP_ID:
        dash.phase_skip("feishu", "未配置")
    else:
        dash.phase_start("feishu")
        if ctx:
            try:
                from signals.notify import send_card
                card = ctx.to_feishu_card()
                send_card(card)
            except Exception as e:
                dash.log(f"  [!] 飞书推送异常: {e}")
        dash.phase_end("feishu")

    dash.finish()


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
    from signals.dashboard import Dashboard

    label = _get_date_label(raw_alias)
    label_str = f"（{label}）" if label else ""

    print(f"\n{'═'*52}")
    print(f"  盘后复盘模式  起始：{start_date}{label_str}")
    print(f"{'═'*52}")

    dash = Dashboard(mode="review")

    # ── Layer 1：指数复盘 ──────────────────────────────────
    screener_l1 = IndexScreener()
    ctx = screener_l1.run_review(start_date)
    if ctx:
        dash.set_context(direction=ctx.overall_direction,
                         style=ctx.recommended_style)

    # ── Layer 2：双榜行业筛选（使用最近交易日Pool数据）────
    ranking_stocks: list = []
    merged_list: list = []
    concepts: list = []
    if config.RANK_TOP_N > 0:
        from signals.layers.industry import get_industry_representatives

        dash.phase_start("L2.ranking")

        # Pool API仅保留近期数据（~3-4周），始终用最近交易日
        pool_date = datetime.now().strftime("%Y%m%d")

        try:
            gain_list, composite_list, merged_list, concepts, oversold_list, _ = get_industry_representatives(
                config.RANK_TOP_N, date_str=pool_date)
        except Exception as e:
            dash.log(f"  [!] 行业筛选异常（{e}），跳过 Layer 2")
            dash.task_error("L2.ranking", "行业筛选", str(e))
            gain_list, composite_list, merged_list, concepts, oversold_list = [], [], [], [], []

        # ── 汇总入池 ─────────────────────────────────────
        for r in merged_list:
            ranking_stocks.extend(r.pool_codes)
        ranking_stocks = list(dict.fromkeys(ranking_stocks))

        dash.phase_end("L2.ranking", detail=f"{len(ranking_stocks)} 只入池")

        # ── 轮动阶段识别 ─────────────────────────────────
        if gain_list or composite_list:
            try:
                from signals.core.rotation import detect_rotation_stage, suggest_allocation
                rot = detect_rotation_stage(gain_list, composite_list)
                ctx.rotation_stage = rot.stage
                ctx.rotation_detail = rot.format_line()
                _, alloc_str = suggest_allocation(rot, ctx.sentiment_phase)
                ctx.allocation_suggestion = alloc_str
                # P3-4: 轮动持续时间和速度
                ctx.rotation_duration = rot.duration_days
                ctx.rotation_velocity = rot.velocity
                ctx.rotation_peak_warning = rot.peak_warning
                ctx.rotation_peak_detail = rot.peak_detail
            except Exception:
                pass

        # ── 暂停面板，打印 L2 报告 ────────────────────────
        dash.pause()
        print(f"\n>>> Layer 2 双榜行业筛选（Pool日期：{pool_date}）")
        print("  " + "─" * 60)

        # ── Layer 2A：涨停密度排行（盘后替代涨幅排行）─────
        if gain_list:
            print(f"\n  >>> Layer 2A 行业涨停密度排行（前{len(gain_list)}名）")
            print(f"  {'排名':<4} {'行业':<22} {'涨停':>4}")
            print("  " + "─" * 45)
            for i, r in enumerate(gain_list, 1):
                print(f"  {i:<4} {r.display_name:<22} {r.zt_count:>4}只")
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
            print(f"  {'排名':<4} {'行业':<22} {'综合分':>6} "
                  f"{'涨停':>4} {'强势':>4} {'续板':>4}")
            print("  " + "─" * 58)
            for r in composite_list:
                tag = " ★" if r.source == "both" else ""
                print(f"  {r.composite_rank:<4} {r.display_name:<22} "
                      f"{r.composite_score:>6.1f} {r.zt_count:>4} "
                      f"{r.strong_count:>4} {r.zbgc_count:>4}{tag}")
                for c in r.candidates:
                    already = " [密度榜已入池]" if r.source == "both" else ""
                    print(f"       {c.role}: {c.name}({c.code}"
                          f"{', ' + c.detail if c.detail else ''}){already}")
                if r.pool_codes and r.source != "both":
                    print(f"       → 入池: {', '.join(r.pool_codes)}")

        # ── 汇总 ─────────────────────────────────────────
        if merged_list:
            comp_only = sum(1 for r in merged_list if r.source == "composite")
            both_cnt = sum(1 for r in merged_list if r.source == "both")
            print(f"\n  >>> Layer 2 合计: 涨停榜 {len(gain_list)} 行业 + "
                  f"综合榜 {comp_only} 新增行业"
                  f"{f' + {both_cnt} 重叠' if both_cnt else ''}"
                  f" = {len(merged_list)} 行业")
            print(f"  >>> Layer 2 共 {len(ranking_stocks)} 只代表股纳入复盘池")
        else:
            print("\n  >>> Layer 2 无行业数据，仅白名单复盘")

        # ── 轮动阶段 + 配置建议 ────────────────────────
        if ctx.rotation_stage:
            print(f"\n  >>> {ctx.rotation_detail}")
        if ctx.allocation_suggestion:
            print(f"  >>> 📦 {ctx.allocation_suggestion}")

        # ── 概念板块 Top N ─────────────────────────────
        _CP_ICON = {"防守": "🛡", "进攻": "⚔", "周期": "🔄", "中性": ""}
        if concepts:
            print(f"\n  >>> 概念板块热度排行（前{len(concepts)}名）")
            print(f"  {'排名':<4} {'概念':<14} {'涨幅':>7}  {'属性':<6}")
            print("  " + "─" * 40)
            for i, cp in enumerate(concepts, 1):
                sign = "+" if cp.gain_pct >= 0 else ""
                cp_icon = _CP_ICON.get(cp.sector_type, "")
                print(f"  {i:<4} {cp.name:<14} "
                      f"{sign}{cp.gain_pct:.2f}%  {cp.sector_type}{cp_icon}")

        # L2 输出后保持 paused（不 resume），L3 纯文本滚动

    dash.set_l2_count(len(ranking_stocks))

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

    # ── 最终汇总面板 ────────────────────────────────────
    dash.finish()


def _run_multi_review(dates: list, args):
    """
    多日期批量对比模式。

    :param dates: [(resolved_date, raw_alias), ...]
    """
    from signals.layers.index_screener import IndexScreener
    from signals.layers.industry import get_industry_representatives
    from signals.layers.review_screener import review_stock_daily
    from signals.dashboard import Dashboard

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

    dash = Dashboard(mode="review")

    # ── Layer 1：指数复盘（共享，取最早日期）──────────────
    earliest = min(d[0] for d in date_labels)
    screener_l1 = IndexScreener()
    ctx = screener_l1.run_review(earliest)
    if ctx:
        dash.set_context(direction=ctx.overall_direction,
                         style=ctx.recommended_style)

    # ── Layer 2：每日期独立 ────────────────────────────────
    all_ranking_stocks: list = []
    all_concepts: list = []
    per_date_results: list = []  # [(date, label, gain_list, comp_list, merged_list)]

    if config.RANK_TOP_N > 0:
        dash.phase_start("L2.ranking", total=len(date_labels))

        for resolved, raw, label in date_labels:
            date_yyyymmdd = resolved.replace("-", "")
            dash.task_start("L2.ranking", f"{resolved}({label})")

            try:
                gain_list, composite_list, merged_list, concepts, _, _ = get_industry_representatives(
                    config.RANK_TOP_N, date_str=date_yyyymmdd)
            except Exception as e:
                dash.log(f"  [!] {resolved} 行业筛选异常（{e}），跳过")
                gain_list, composite_list, merged_list, concepts = [], [], [], []

            per_date_results.append((resolved, label, gain_list, composite_list, merged_list))
            if concepts and not all_concepts:
                all_concepts = concepts  # 保留第一组有效概念

            for r in merged_list:
                all_ranking_stocks.extend(r.pool_codes)

            dash.task_done("L2.ranking", f"{resolved}")

        all_ranking_stocks = list(dict.fromkeys(all_ranking_stocks))
        dash.phase_end("L2.ranking", detail=f"{len(all_ranking_stocks)} 只入池")

        # ── 暂停面板，打印 L2 报告 ────────────────────────
        dash.pause()

        for resolved, label, gain_list, composite_list, merged_list in per_date_results:
            print(f"\n>>> Layer 2 行业筛选：{resolved}（{label}）")
            print("  " + "─" * 50)

            if gain_list:
                top3 = ", ".join(f"{r.display_name}({r.zt_count}只)" for r in gain_list[:3])
                print(f"  涨停密度 Top 3: {top3}")
            if composite_list:
                top3 = ", ".join(f"{r.display_name}({r.composite_score:.0f}分)" for r in composite_list[:3])
                print(f"  综合强度 Top 3: {top3}")
            if not gain_list and not composite_list:
                print(f"  [!] {resolved} 无Pool数据（东财仅保留近3-4周），跳过")

        # ── 行业×日期矩阵 ─────────────────────────────────
        _print_industry_date_matrix(per_date_results)

        print(f"\n  >>> Layer 2 多日期合计: {len(all_ranking_stocks)} 只代表股纳入复盘池")

        # ── 概念板块 Top N ─────────────────────────────
        _CP_ICON = {"防守": "🛡", "进攻": "⚔", "周期": "🔄", "中性": ""}
        if all_concepts:
            print(f"\n  >>> 概念板块热度排行（前{len(all_concepts)}名）")
            print(f"  {'排名':<4} {'概念':<14} {'涨幅':>7}  {'属性':<6}")
            print("  " + "─" * 40)
            for i, cp in enumerate(all_concepts, 1):
                sign = "+" if cp.gain_pct >= 0 else ""
                cp_icon = _CP_ICON.get(cp.sector_type, "")
                print(f"  {i:<4} {cp.name:<14} "
                      f"{sign}{cp.gain_pct:.2f}%  {cp.sector_type}{cp_icon}")

        # L2 输出后保持 paused（不 resume），L3 纯文本滚动

    dash.set_l2_count(len(all_ranking_stocks))

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

    # ── 最终汇总面板 ────────────────────────────────────
    dash.finish()


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


def _smart_pool(whitelist, noted_stocks, merged_list, named_stocks,
                max_total=20):
    """
    智能入池：行业轮选 + 信号优先，保证行业分散。

    填充顺序：
    1. 行业轮选（L2 数据驱动）— 每轮从每个行业取 1 只最高优先级候选
    2. --industries 补充标的
    3. 白名单 + 研报标的（保底）
    """
    pool: list = []
    seen: set = set()

    # 第一优先：行业轮选
    ind_queues = []
    for ranking in (merged_list or []):
        q = [c.code for c in ranking.candidates if c.code]
        ind_queues.append(q)

    round_idx = 0
    while len(pool) < max_total and ind_queues:
        found_any = False
        for q in ind_queues:
            if len(pool) >= max_total:
                break
            if round_idx < len(q):
                code = q[round_idx]
                if code not in seen:
                    pool.append(code)
                    seen.add(code)
                found_any = True
        round_idx += 1
        if not found_any:
            break

    # 第二优先：--industries 补充标的
    for s in (named_stocks or []):
        if len(pool) >= max_total:
            break
        if s not in seen:
            pool.append(s)
            seen.add(s)

    # 第三优先：白名单 + 研报标的（保底）
    for s in list(whitelist or []) + list(noted_stocks or []):
        if len(pool) >= max_total:
            break
        if s not in seen:
            pool.append(s)
            seen.add(s)

    return pool


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
# 仿真模式：历史数据快照回放
# ─────────────────────────────────────────────────────────

def _list_sim_sessions():
    """列出所有可用的仿真快照。"""
    from signals.data.sim_source import list_sim_sessions
    sessions = list_sim_sessions()
    if not sessions:
        print("\n  无可用仿真快照。")
        print(f"  创建：python run.py --mode sim --create --start YYYY-MM-DD\n")
        return
    print(f"\n  可用仿真快照（{len(sessions)} 个）：")
    print(f"  {'名称':<16} {'日期范围':<26} {'标的':>4} {'bar数':>10} {'大小':>8}")
    print("  " + "─" * 70)
    for s in sessions:
        date_range = f"{s['start_date']} → {s['end_date']}"
        print(f"  {s['name']:<16} {date_range:<26} "
              f"{s['symbol_count']:>4} {s['bar_count']:>10,} "
              f"{s['size_mb']:>7.1f}M")
    print()


def run_analog(args):
    """
    历史形态匹配模式：独立运行，对比当前走势与历史走势。

    用法：python run.py --mode analog [--symbol 沪深300]
    """
    from signals.data.fetcher import DataFetcher
    from signals.core.analog_matcher import (
        find_analogs, save_analog_results, analog_to_dict,
    )
    from dataclasses import asdict

    indices = config.ANALOG_INDICES
    symbol_filter = getattr(args, "symbol", None)
    if symbol_filter:
        if symbol_filter not in config.INDEX_AK_CODES:
            print(f"  [!] 未知指数: {symbol_filter}")
            print(f"      可选: {', '.join(config.INDEX_AK_CODES.keys())}")
            return
        indices = [symbol_filter]

    print(f"\n{'═'*52}")
    print(f"  📊 历史形态匹配  窗口={config.ANALOG_WINDOW}天  Top{config.ANALOG_TOP_K}")
    print(f"  匹配指数: {', '.join(indices)}")
    print(f"{'═'*52}\n")

    fetcher = DataFetcher()
    all_results = {}

    for name in indices:
        code = config.INDEX_AK_CODES.get(name)
        if not code:
            print(f"  [!] {name}: 无 AKShare 代码，跳过")
            continue

        print(f"  ▶ {name} ({code})")
        try:
            bars = fetcher.get_index_daily(code, lookback_days=config.ANALOG_LOOKBACK_DAYS)
            if len(bars) < config.ANALOG_WINDOW + 60:
                print(f"    数据不足 ({len(bars)}根K线)，需要至少 {config.ANALOG_WINDOW + 60} 根")
                continue

            closes = [b.close for b in bars]
            dates = [str(b.dt)[:10] for b in bars]

            # 当前走势 = 最近 window 天；历史 = 全部（去掉最近60天）
            analogs = find_analogs(
                current_closes=closes,
                current_dates=dates,
                history_closes=closes,
                history_dates=dates,
                index_name=name,
                window=config.ANALOG_WINDOW,
                top_k=config.ANALOG_TOP_K,
                min_similarity=config.ANALOG_MIN_SIMILARITY,
            )

            if analogs:
                all_results[name] = [analog_to_dict(a) for a in analogs]
                print(f"    找到 {len(analogs)} 个匹配:")
                for i, a in enumerate(analogs, 1):
                    print(f"    [{i}] 相似度={a.similarity:.2%}  "
                          f"{a.match_start}~{a.match_end}  "
                          f"后10日={a.next_10d_return:+.1f}%  "
                          f"后30日={a.next_30d_return:+.1f}%")
                    print(f"        {a.what_happened}")
            else:
                print(f"    未找到相似度≥{config.ANALOG_MIN_SIMILARITY:.0%}的匹配")

        except Exception as e:
            print(f"    [!] 异常: {e}")

    if all_results:
        save_analog_results(all_results)
        print(f"\n  ✅ 结果已缓存 → .data/cache/analog_latest.json")
    else:
        print(f"\n  未产生任何匹配结果")


def run_plan(args):
    """
    盘前计划模式：分析主要指数，生成完全分类的 3 种情景。

    用法：python run.py --mode plan
    """
    print("\n🐲 隆小侠 — 盘前计划\n")

    from signals.layers.index_screener import IndexScreener
    from signals.core.planner import generate_plan

    print("  加载指数数据...")
    screener = IndexScreener()
    screener.initialize()
    ctx = screener.analyze()

    main_indices = ["沪深300", "上证50", "创业板指", "科创50", "中证500"]
    for name in main_indices:
        az = screener.analyzers.get(name)
        if az is None:
            continue
        daily = getattr(az, "_daily", None)
        if daily is None:
            continue
        # 获取 MA 上下文
        report = next((r for r in ctx.reports if r.name == name), None)
        ma_ctx = getattr(report, "ma_context", None) if report else None

        plan = generate_plan(daily, ma_ctx)
        plan.name = name

        print(f"\n  {'='*50}")
        print(f"  {plan.name}  现价 {plan.current_price}  {plan.trend}")
        print(f"  {plan.structure}")
        if plan.key_levels:
            lvs = " | ".join(f"{lv['name']} {lv['price']}" for lv in plan.key_levels)
            print(f"  关键位: {lvs}")
        for sc in plan.scenarios:
            print(f"\n  {sc.name} [{sc.probability_hint}]")
            print(f"    触发: {sc.trigger}")
            print(f"    操作: {sc.action}")
            if sc.target_prices:
                print(f"    目标: {' / '.join(str(p) for p in sc.target_prices)}")
            if sc.stop_price:
                print(f"    止损: {sc.stop_price}")
            if sc.rationale:
                print(f"    逻辑: {sc.rationale}")

    print(f"\n  {'='*50}")
    print(f"  大盘方向: {ctx.overall_direction} | 情绪: {ctx.sentiment_phase}")
    print(f"  建议仓位: {ctx.position_suggestion}")
    print()


def run_weekly(args):
    """
    周末策略模式：整合指数 + 轮动 + 宏观事件，生成下周操作策略。

    用法：python run.py --mode weekly
    """
    print("\n🐲 隆小侠 — 周末策略\n")

    from signals.layers.index_screener import IndexScreener
    from signals.core.weekly import generate_weekly

    print("  加载指数数据...")
    screener = IndexScreener()
    screener.initialize()
    ctx = screener.analyze()

    weekly = generate_weekly(
        index_reports=ctx.reports,
        market_context=ctx,
        rotation_stage=ctx.rotation_stage,
        allocation=ctx.allocation_suggestion,
    )

    print(f"  {weekly.week_label}")
    print(f"\n  大盘展望: {weekly.market_outlook}")
    print(f"  仓位建议: {weekly.position_suggestion}")
    if weekly.style_suggestion:
        print(f"  风格建议: {weekly.style_suggestion}")
    if weekly.rotation_outlook:
        print(f"  轮动阶段: {weekly.rotation_outlook}")

    if weekly.focus_sectors:
        print(f"\n  关注板块: {', '.join(weekly.focus_sectors)}")
    if weekly.avoid_sectors:
        print(f"  回避板块: {', '.join(weekly.avoid_sectors)}")

    if weekly.events:
        print(f"\n  宏观事件:")
        for ev in weekly.events:
            print(f"    {ev.event_name} {ev.event_date}")
            for k, v in ev.scenarios.items():
                print(f"      {k}: {v}")

    if weekly.key_levels:
        print(f"\n  关键价位:")
        for lv in weekly.key_levels:
            print(f"    {lv['index']} {lv['name']} {lv['price']} ({lv['distance_pct']:+.1f}%)")

    print()


def run_web(args):
    """
    Web UI 模式：先启动 Web 服务器（秒开），L1→L2→L3 后台异步加载。

    用法：python run.py --mode web [--port 8000]
    """
    port = getattr(args, "port", 8000)

    # ── 文件日志初始化 ──
    import logging, os
    from datetime import datetime
    log_dir = os.path.join(os.path.dirname(__file__), ".data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"web_{datetime.now():%Y%m%d_%H%M%S}.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S"))
    # 同时输出到控制台
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    print(f"\n🐲 隆小侠 Web UI")
    print(f"   🌐 http://localhost:{port}")
    print(f"   📋 日志: {log_file}")
    print(f"   数据后台加载中...\n")

    from signals.web.services.engine import get_engine
    engine = get_engine()

    # 后台异步加载 L1 → L2 → L3
    engine.run_all_async()

    # 立即启动 FastAPI（不等数据加载完成）
    import uvicorn
    from signals.web.app import create_app
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def run_web2(args):
    """Web2 精简版：行业聚类 + MACD 回测"""
    import uvicorn
    port = getattr(args, "port", None) or 8001
    from signals.web2.app import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def run_sim(args):
    """
    仿真模式：全自动 — 检查仓库 → 补全数据 → 创建快照 → 执行回放。

    用户只需一条命令：python run.py --mode sim --start 2026-01-14
    """
    import os
    from signals.core.market_hours import Market

    # ── 管理命令 ─────────────────────────────────────────
    if getattr(args, "list_sessions", False):
        _list_sim_sessions()
        return

    # ── 手动同步（可选）──────────────────────────────────
    if getattr(args, "sync", False):
        from signals.data.warehouse import DataWarehouse
        wh = DataWarehouse()
        start = args.start if args.start != "ytd" else "2026-01-01"
        extra = None
        if getattr(args, "symbols", None):
            extra = [s.strip() for s in args.symbols.split(",") if s.strip()]
        wh.sync(start, extra)
        wh.print_info()
        wh.close()
        return

    # ── 参数校验 ──────────────────────────────────────────
    start_date = args.start
    if not start_date or start_date == "ytd":
        print("错误：--mode sim 必须指定 --start YYYY-MM-DD")
        print("  示例：python run.py --mode sim --start 2026-01-14")
        return

    session_name = getattr(args, "session", None) or start_date
    end_date = getattr(args, "end", None)
    session_path = f"{config.SIM_SESSION_DIR}/{session_name}.db"

    extra_symbols = None
    if getattr(args, "symbols", None):
        extra_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # ── 步骤 1：快照已存在？直接回放 ─────────────────────
    if os.path.exists(session_path) and not getattr(args, "create", False):
        print(f"\n  快照已存在: {session_name}.db，直接回放")
    else:
        # ── 步骤 2+3：仓库检查 + 自动补全 + 提取快照 ────
        from signals.data.warehouse import DataWarehouse
        wh = DataWarehouse()

        all_symbols = list(config.WHITELIST)
        if extra_symbols:
            for s in extra_symbols:
                if s not in all_symbols:
                    all_symbols.append(s)

        # 检查覆盖率
        coverage = wh.check_coverage(
            all_symbols, start_date, end_date or "9999-12-31",
            index_codes=config.INDEX_AK_CODES,
        )

        if coverage["missing"]:
            n_missing = len(coverage["missing"])
            print(f"\n>>> 仓库缺失 {n_missing} 项数据，自动补全...")
            wh.sync(start_date, extra_symbols)
        else:
            print(f"\n>>> 仓库数据完整（{coverage['total_bars']:,} bars），提取快照...")

        # 从仓库提取快照
        wh.extract_session(session_name, start_date, end_date, extra_symbols)
        wh.close()

    # ── 步骤 4：执行回放（L1 + L3）──────────────────────
    from signals.data.sim_source import SimDataSource
    from signals.layers.index_screener import IndexScreener
    from signals.layers.screener import IntraDayScreener

    sim = SimDataSource(session_path)
    sim.print_info()

    print(f"\n{'═'*52}")
    print(f"  仿真回放模式  快照：{session_name}")
    print(f"{'═'*52}")

    # 强制所有市场活跃（绕过时段检查）
    active = {Market.A, Market.HK, Market.US}

    # ── Layer 1：指数研判（注入仿真数据源）────────────────
    print(f"\n>>> Layer 1 指数研判（仿真数据）")
    screener_l1 = IndexScreener(
        ak_codes=config.INDEX_AK_CODES,
        futu_codes=config.INDEX_FUTU_CODES,
        us_codes=config.INDEX_US_CODES,
        data_source=sim,
    )
    ctx = screener_l1.run()

    # ── Layer 2：跳过（行业实时数据无法从历史 API 还原）───
    print(f"\n>>> [仿真模式] Layer 2 跳过（无实时行业数据）")

    # ── Layer 3：标的筛选（注入仿真数据源）────────────────
    l3_pool = list(config.WHITELIST)
    if getattr(args, "symbols", None):
        for s in args.symbols.split(","):
            s = s.strip()
            if s and s not in l3_pool:
                l3_pool.append(s)

    # 检查快照中实际有哪些标的的分钟线
    available = sim._available_symbols("15分钟") + sim._available_symbols("30分钟")
    available_set = set(available)
    l3_pool = [s for s in l3_pool if s in available_set]

    if not l3_pool:
        print(f"\n>>> Layer 3 跳过（快照中无匹配标的的分钟线数据）")
        print(f"  快照可用标的: {', '.join(sorted(available_set)[:10])}...")
    else:
        print(f"\n>>> Layer 3 标的筛选（仿真，{len(l3_pool)} 只入池）")
        screener_l3 = IntraDayScreener(
            symbols=l3_pool, freqs=config.MONITOR_FREQS,
            data_source=sim,
        )

        try:
            screener_l3.initialize(l3_pool)
            results = screener_l3.scan_once(l3_pool)
            screener_l3.print_results(results, title="[仿真] L3 标的筛选结果")

            # 操作建议
            if ctx:
                from signals.core.summary import print_action_summary
                print_action_summary(ctx, results)
        finally:
            screener_l3.close()

    sim.close()
    print(f"\n  仿真回放完成。\n")


# ─────────────────────────────────────────────────────────
# RSS 资讯
# ─────────────────────────────────────────────────────────

def run_rss(args):
    """RSS 资讯订阅：抓取 + 推送美股市场分析。"""
    from signals.data.rss_fetcher import run_rss as _run_rss
    _run_rss(args)


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
  python run.py --mode sim --start 2026-01-14                    # 全自动：仓库→快照→回放
  python run.py --mode sim --start 2026-01-14 --symbols SH.600519  # 指定额外标的
  python run.py --mode sim --sync --start 2026-01-01            # 手动同步仓库
  python run.py --mode sim --list-sessions                      # 列出可用快照
  python run.py --mode web                                     # Web UI（TradingView 风格）
  python run.py --mode web --port 9000                         # 指定端口
  python run.py --mode analog                                    # 历史形态匹配（全部指数）
  python run.py --mode analog --symbol 沪深300                   # 指定单个指数匹配
  python run.py --mode index --push                              # 分析 + 推送到 Vercel
  python run.py --mode intraday --push                           # 盘中 + 推送到 Vercel
  python run.py --mode rss                                       # RSS 美股资讯
  python run.py --mode rss --push                                # RSS + 推送到飞书/微信
  python run.py --list-dates                       # 列出所有日期预设

可用日期预设：{preset_keys}
        """
    )
    parser.add_argument(
        "--mode",
        default="intraday",
        choices=["intraday", "review", "index", "import", "backtest", "sim", "web", "web2", "analog", "plan", "weekly", "rss"],
        help="运行模式：intraday / review / index / import / backtest / sim / web / web2 / analog / plan / weekly / rss"
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
    # sim 模式专用参数
    parser.add_argument(
        "--create",
        action="store_true",
        help="[sim] 创建仿真快照（从历史 API 下载数据）"
    )
    parser.add_argument(
        "--session",
        default=None,
        metavar="NAME",
        help="[sim] 仿真快照名称（即日期，如 2026-01-14）"
    )
    parser.add_argument(
        "--end",
        default=None,
        metavar="YYYY-MM-DD",
        help="[sim] 仿真结束日期（默认今天）"
    )
    parser.add_argument(
        "--symbols",
        default=None,
        metavar="SYM1,SYM2",
        help="[sim] 额外仿真标的（逗号分隔，Futu 格式）"
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="[sim] 列出所有可用仿真快照"
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="[sim] 手动触发数据仓库全量同步"
    )
    # web 模式专用参数
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="[web] Web UI 端口号，默认 8000"
    )
    # 云端推送参数
    parser.add_argument(
        "--push",
        action="store_true",
        help="分析完成后推送结果到 Upstash Redis（供 Vercel 前端读取）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="配合 --push 使用：mock Redis + mock 数据，验证推送逻辑（不连网）"
    )
    parser.add_argument(
        "--themes",
        type=str, default="",
        help="关注主题（逗号分隔），如 --themes '储能,算力,CLAW'（覆盖 config.WATCH_THEMES）"
    )
    # analog 模式专用参数
    parser.add_argument(
        "--symbol",
        default=None,
        metavar="NAME",
        help="[analog] 指定匹配的指数名称（如 沪深300），默认匹配全部 ANALOG_INDICES"
    )

    args = parser.parse_args()

    # --list-dates 优先处理
    if args.list_dates:
        _print_date_presets()
        return

    # --list-sessions 优先处理
    if getattr(args, "list_sessions", False):
        _list_sim_sessions()
        return

    dispatch = {
        "intraday": run_intraday,
        "review":   run_review,
        "index":    run_index_only,
        "import":   run_import,
        "backtest": run_backtest,
        "sim":      run_sim,
        "web":      run_web,
        "web2":     run_web2,
        "analog":   run_analog,
        "plan":     run_plan,
        "weekly":   run_weekly,
        "rss":      run_rss,
    }
    dispatch[args.mode](args)

    # --push: 分析完成后推送到 Upstash Redis
    if getattr(args, "push", False) and args.mode in ("index", "intraday", "web"):
        dry_run = getattr(args, "dry_run", False)
        label = "[dry-run] " if dry_run else ""
        print(f"\n  {label}推送分析结果到 Upstash Redis...")
        from signals.deploy.push_to_kv import push_from_screener
        screener = getattr(args, "_screener", None)
        push_from_screener(screener=screener, dry_run=dry_run)


if __name__ == "__main__":
    main()
