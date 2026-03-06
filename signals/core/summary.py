# -*- coding: utf-8 -*-
"""操作建议 — 三层联动汇总输出"""
from collections import Counter
from typing import List, Optional


def _brief_signals(signals: list) -> str:
    """简要信号摘要，如 '三买(15M+30M) 趋势买(30M)'"""
    freq_abbrev = {
        "15分钟": "15M", "30分钟": "30M", "60分钟": "60M",
        "日线": "日", "周线": "周", "5分钟": "5M", "1分钟": "1M",
    }
    sig_freq_map: dict = {}
    for sig in signals:
        abbr = freq_abbrev.get(sig.freq, sig.freq)
        sig_freq_map.setdefault(sig.signal_type, []).append(abbr)

    parts = []
    for sig_type, freqs in sig_freq_map.items():
        freq_str = "+".join(freqs)
        parts.append(f"{sig_type}({freq_str})")
    return " ".join(parts[:4])


def print_action_summary(ctx, results: list, resolver=None, notes=None):
    """
    打印操作建议。

    Args:
        ctx: MarketContext (L1)
        results: List[ScoredSymbol] (L3 合并后)
        resolver: StockNameResolver
        notes: 研报列表
    """
    from config import SCORE_THRESHOLD
    from signals.research import match_notes_for_symbol, check_resonance

    notes = notes or []

    print(f"\n{'━' * 60}")
    print(f"  操作建议（三层联动汇总）")
    print(f"{'━' * 60}")

    # ── 大势判断 ──
    if ctx:
        dir_map = {"偏多": "📈", "偏空": "📉", "分化": "↔️"}
        emoji = dir_map.get(ctx.overall_direction, "")
        print(f"\n  ▶ 大势判断: {emoji} {ctx.overall_direction}"
              f"  风格: {ctx.recommended_style}")
        if ctx.buy_indices:
            print(f"    买信号指数: {'、'.join(ctx.buy_indices[:5])}")
        if ctx.sell_indices:
            print(f"    卖信号指数: {'、'.join(ctx.sell_indices[:3])}")

    if not results:
        print(f"\n  当前无 L3 信号，观望为主。")
        print(f"{'━' * 60}\n")
        return

    above = [r for r in results
             if r.total_score >= SCORE_THRESHOLD and r.signal_count > 0]

    # ── 买入机会 ──
    buys = [r for r in above if r.direction == "偏多"]
    if buys:
        print(f"\n  ▶ 买入机会 ({len(buys)} 只):")
        for r in buys[:8]:
            name = resolver.get_name(r.symbol) if resolver else r.symbol
            industry = resolver.get_industry(r.symbol) if resolver else ""
            ind_tag = f"[{industry}]" if industry else ""

            note_view = match_notes_for_symbol(r.symbol, notes)
            resonance = check_resonance(r.total_score, note_view)
            res_tag = f" {resonance}" if resonance else ""

            sigs = _brief_signals(r.signals)
            print(f"    {r.symbol} {name} {ind_tag}"
                  f"  分={r.total_score:.0f}  {sigs}{res_tag}")
        if len(buys) > 8:
            print(f"    ... 还有 {len(buys) - 8} 只")

    # ── 风险警示 ──
    sells = [r for r in results
             if r.direction == "偏空" and abs(r.total_score) >= 30
             and r.signal_count > 0]
    if sells:
        print(f"\n  ▶ 风险警示 ({len(sells)} 只):")
        for r in sells[:5]:
            name = resolver.get_name(r.symbol) if resolver else r.symbol
            print(f"    {r.symbol} {name}  分={r.total_score:.0f}  {r.direction}")

    # ── 重点关注 ──
    watch = []
    for r in above:
        note_view = match_notes_for_symbol(r.symbol, notes)
        resonance = check_resonance(r.total_score, note_view)
        buy_freqs = {s.freq for s in r.signals if "买" in s.signal_type}
        is_multi_tf = len(buy_freqs) > 1
        if resonance == "★共振" or is_multi_tf:
            tags = []
            if is_multi_tf:
                tags.append("多级别共振")
            if resonance == "★共振":
                tags.append("技术+研报共振")
            watch.append((r, " | ".join(tags)))

    if watch:
        print(f"\n  ▶ 重点关注 ({len(watch)} 只):")
        for r, tag_str in watch[:5]:
            name = resolver.get_name(r.symbol) if resolver else r.symbol
            print(f"    {r.symbol} {name}  分={r.total_score:.0f}  [{tag_str}]")

    # ── 结论 ──
    if not buys and not sells:
        print(f"\n  ▶ 结论: 暂无明确买卖机会，维持观望。")
    elif buys and not sells:
        print(f"\n  ▶ 结论: 有 {len(buys)} 只偏多标的，可关注买入机会。")
    elif sells and not buys:
        print(f"\n  ▶ 结论: 存在卖出信号，控制仓位，回避偏空标的。")
    else:
        print(f"\n  ▶ 结论: 市场分化，精选偏多标的，回避偏空品种。")

    print(f"{'━' * 60}\n")
