# -*- coding: utf-8 -*-
"""
WeChat Tools — CC 的工具箱，按名称调用

不做意图理解，不做关键词匹配。
CC 理解用户意图后，按工具名调用对应函数。

用法:
    python scripts/wechat_run.py industry_ranking
    python scripts/wechat_run.py industry_ranking --concepts
    python scripts/wechat_run.py review [--date yesterday]
"""
import logging
import time

_log = logging.getLogger("signals.wechat.skills")


def industry_ranking(*, include_concepts: bool = False) -> str:
    """
    全市场行业排行：涨幅榜 + 综合榜 + 超跌 + 市场统计。
    可选 include_concepts=True 追加概念板块排行。
    """
    from signals.layers.industry import get_industry_representatives

    result = get_industry_representatives()
    if len(result) == 6:
        gain_list, composite_list, merged_list, concepts, oversold_list, sentiment_stats = result
    else:
        gain_list, composite_list, merged_list, concepts, oversold_list = result
        sentiment_stats = {}

    lines = []

    # 涨幅榜 Top 10
    if gain_list:
        lines.append("📊 行业涨幅榜 Top 10\n")
        for i, ind in enumerate(gain_list[:10], 1):
            pct = ind.gain_pct
            emoji = "🔴" if pct > 0 else "🟢"
            zt_str = f" 涨停{ind.zt_count}" if ind.zt_count else ""
            lines.append(f"{i}. {emoji} {ind.name}  {pct:+.2f}%{zt_str}")

    # 综合榜 Top 10
    if composite_list:
        lines.append("\n🏆 综合评分 Top 10\n")
        for i, ind in enumerate(composite_list[:10], 1):
            lines.append(f"{i}. {ind.name}  综合{ind.composite_score:.0f}  {ind.gain_pct:+.2f}%")

    # 超跌板块
    if oversold_list:
        oversold_top = [x for x in oversold_list[:5] if x.oversold_score > 0]
        if oversold_top:
            lines.append("\n🔻 超跌反弹候选\n")
            for ind in oversold_top:
                lines.append(f"  · {ind.name}  超跌分{ind.oversold_score:.0f}")

    # 市场统计
    if isinstance(sentiment_stats, dict) and sentiment_stats:
        zt = sentiment_stats.get("zt_total", 0)
        dt = sentiment_stats.get("dt_total", 0)
        red_pct = 0
        name_df = sentiment_stats.get("name_df")
        if name_df is not None:
            try:
                import pandas as pd
                for col in ['涨跌幅', '涨跌幅(%)', '涨幅', '涨幅(%)', '最新涨跌幅']:
                    if col in name_df.columns:
                        vals = pd.to_numeric(name_df[col], errors='coerce')
                        total = vals.count()
                        red = (vals > 0).sum()
                        red_pct = round(red / total * 100) if total > 0 else 0
                        break
            except Exception:
                pass
        lines.append(f"\n📈 涨停{zt}  跌停{dt}  上涨占比{red_pct:.0f}%")

    # 概念排行
    if include_concepts and concepts:
        lines.append("\n🔥 概念板块 Top 10\n")
        for i, c in enumerate(concepts[:10], 1):
            up_total = c.up_count + c.down_count
            up_ratio = c.up_count / up_total if up_total > 0 else 0
            lines.append(
                f"{i}. {c.name}  {c.gain_pct:+.2f}%  "
                f"综合{c.composite_score:.0f}  上涨{up_ratio:.0%}"
            )

    return "\n".join(lines) if lines else "未获取到行业数据"


def review(*, date: str = "yesterday") -> str:
    """
    盘后复盘：L1 指数 → L2 行业 → L3 个股，三层联动分析。
    date: 复盘日期，默认 "yesterday"。
    """
    lines = [f"📋 盘后复盘 — {date}\n"]
    timing = {}

    # ── L1: 指数复盘 ──
    ctx = None
    try:
        _log.info("[复盘] L1 开始 — 指数分析")
        t0 = time.monotonic()
        from signals.layers.index_screener import IndexScreener
        screener = IndexScreener()
        ctx = screener.run_review(date)
        timing["L1"] = round(time.monotonic() - t0, 1)
        _log.info("[复盘] L1 完成 — %.1fs", timing["L1"])

        if ctx:
            if ctx.direction:
                lines.append(f"🌍 方向: {ctx.direction}")
            if ctx.sentiment_phase:
                lines.append(f"🎭 情绪: {ctx.sentiment_phase}")

            main_names = {"沪深300", "上证50", "创业板指"}
            main_idx = [r for r in (ctx.reports or []) if r.name in main_names]
            if main_idx:
                lines.append("\n━━ 主要指数 ━━")
                for idx in main_idx:
                    change = getattr(idx, 'change_pct', 0)
                    trend = getattr(idx, 'trend', '')
                    emoji = "🔴" if change > 0 else "🟢"
                    lines.append(f"{emoji} {idx.name}  {change:+.2f}%  {trend}")
    except Exception as e:
        _log.error("[复盘] L1 失败: %s", e)
        lines.append(f"⚠️ 指数分析失败: {e}")

    # ── L2: 行业排行 + 轮动 ──
    merged_list = []
    try:
        _log.info("[复盘] L2 开始 — 行业排行")
        t0 = time.monotonic()
        from signals.layers.industry import get_industry_representatives
        from datetime import datetime
        import config
        pool_date = datetime.now().strftime("%Y%m%d")
        result = get_industry_representatives(config.RANK_TOP_N, date_str=pool_date)
        if len(result) == 6:
            gain_list, composite_list, merged_list, concepts, oversold_list, _ = result
        else:
            gain_list, composite_list, merged_list, concepts, oversold_list = result
        timing["L2"] = round(time.monotonic() - t0, 1)
        _log.info("[复盘] L2 完成 — %.1fs (%d 行业)", timing["L2"], len(merged_list))

        if gain_list:
            lines.append("\n🏭 行业涨幅 Top 5")
            for ind in gain_list[:5]:
                emoji = "🔴" if ind.gain_pct > 0 else "🟢"
                lines.append(f"  {emoji} {ind.name} {ind.gain_pct:+.2f}%")

        if ctx and (gain_list or composite_list):
            try:
                from signals.core.rotation import detect_rotation_stage, suggest_allocation
                rot = detect_rotation_stage(gain_list, composite_list)
                _, alloc_str = suggest_allocation(rot, ctx.sentiment_phase)
                if rot.stage:
                    lines.append(f"\n🔄 轮动: {rot.stage}")
                if alloc_str:
                    lines.append(f"💡 建议: {alloc_str}")
            except Exception:
                pass
    except Exception as e:
        _log.error("[复盘] L2 失败: %s", e)
        lines.append(f"\n⚠️ 行业分析失败: {e}")

    # ── L3: 个股复盘 ──
    try:
        _log.info("[复盘] L3 开始 — 个股分析")
        t0 = time.monotonic()
        import config
        ranking_stocks = []
        for r in merged_list:
            ranking_stocks.extend(r.pool_codes)
        ranking_stocks = list(dict.fromkeys(ranking_stocks))
        all_symbols = list(dict.fromkeys(config.WHITELIST + ranking_stocks))
        _log.info("[复盘] L3 标的: %d 只", len(all_symbols))

        from signals.layers.review_screener import review_stock_daily
        scored = review_stock_daily(all_symbols, date)

        try:
            from signals.core.stock_names import get_resolver
            resolver = get_resolver()
            if merged_list:
                resolver.inject_from_rankings(merged_list)
            for s in scored:
                if not s.name:
                    s.name = resolver.get_name(s.symbol)
        except Exception:
            pass

        timing["L3"] = round(time.monotonic() - t0, 1)
        _log.info("[复盘] L3 完成 — %.1fs (%d 个股)", timing["L3"], len(scored))

        if scored:
            lines.append("\n🎯 标的信号 Top 5")
            for s in scored[:5]:
                display = f"{s.name}({s.symbol})" if s.name else s.symbol
                direction = getattr(s, 'direction', '')
                total_score = getattr(s, 'total_score', 0)
                lines.append(f"  {display}  评分{total_score:.0f}  {direction}")
    except Exception as e:
        _log.error("[复盘] L3 失败: %s", e)
        lines.append(f"\n⚠️ 个股分析失败: {e}")

    total = sum(timing.values())
    lines.append(f"\n⏱ 耗时 {total:.0f}s (L1:{timing.get('L1', 0):.0f}s L2:{timing.get('L2', 0):.0f}s L3:{timing.get('L3', 0):.0f}s)")

    return "\n".join(lines)
