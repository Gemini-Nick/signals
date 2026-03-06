# -*- coding: utf-8 -*-
"""
模拟数据测试：盘中实时 vs 盘后回测 策略对比（含优化前后对比）

用构造的 K 线数据绕过外部数据源依赖，直接对比核心引擎的行为差异。
优化内容：
  1. 级别归一化权重 — 日线×1.5, 15min×0.7
  2. 回测增加周线级别 — 日+周双级别共振
  3. 买卖互斥判断 — "偏多"/"偏空"/"分歧"
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from typing import List, Tuple
from dataclasses import dataclass

from czsc import Freq, RawBar
from signals.core.analyzer import SymbolAnalyzer
from signals.core.detectors import detect_all_signals, SignalEvent
from signals.core.scorer import score_signals, ScoredSymbol, FREQ_MULTIPLIER
from signals.layers.review_screener import resample_daily_to_weekly


# ═══════════════════════════════════════════════════════════
# 旧版评分函数（优化前，用于对比）
# ═══════════════════════════════════════════════════════════

SIGNAL_WEIGHTS_OLD = {
    "二买": 55, "三买": 50, "一买": 40, "背驰买": 35, "趋势买": 30,
    "二卖": -55, "三卖": -50, "一卖": -40, "背驰卖": -35, "趋势卖": -30,
}

@dataclass
class ScoredSymbolOld:
    symbol: str
    total_score: float
    signal_count: int
    signals: List[SignalEvent]
    details: str

def score_signals_old(symbol: str, signals: List[SignalEvent]) -> ScoredSymbolOld:
    """旧版评分：无级别系数、无方向判断"""
    if not signals:
        return ScoredSymbolOld(symbol=symbol, total_score=0.0, signal_count=0, signals=[], details="无信号")
    total = 0.0
    for sig in signals:
        base = SIGNAL_WEIGHTS_OLD.get(sig.signal_type, 0)
        total += base * sig.confidence
    buy_freqs = {s.freq for s in signals if "买" in s.signal_type}
    sell_freqs = {s.freq for s in signals if "卖" in s.signal_type}
    if len(buy_freqs) > 1:
        total += 15
    if len(sell_freqs) > 1:
        total -= 15
    return ScoredSymbolOld(
        symbol=symbol, total_score=round(total, 1),
        signal_count=len(signals), signals=signals, details="",
    )


# ═══════════════════════════════════════════════════════════
# 模拟 K 线生成器
# ═══════════════════════════════════════════════════════════

def _gen_zigzag(
    base: float,
    moves: List[Tuple[float, int]],
    freq: Freq,
    symbol: str,
    start_dt: datetime,
    bar_minutes: int = 0,
    bar_days: int = 0,
) -> List[RawBar]:
    """生成锯齿形 K 线序列。"""
    bars: List[RawBar] = []
    price = base
    dt = start_dt
    bar_id = 0

    for target, count in moves:
        step = (target - price) / count
        for i in range(count):
            o = price
            c = price + step
            noise = abs(step) * 0.3
            h = max(o, c) + noise
            l = min(o, c) - noise
            vol = int(1e6 + abs(step) * 1e5)

            bars.append(RawBar(
                symbol=symbol, dt=dt, id=bar_id, freq=freq,
                open=round(o, 2), high=round(h, 2),
                low=round(l, 2), close=round(c, 2),
                vol=vol, amount=int(vol * (o + c) / 2),
            ))
            price = c
            bar_id += 1
            if bar_days:
                dt += timedelta(days=bar_days)
            else:
                dt += timedelta(minutes=bar_minutes)

    return bars


# ═══════════════════════════════════════════════════════════
# 4 个场景
# ═══════════════════════════════════════════════════════════

def gen_scenario_A(symbol: str = "SZ.000001") -> dict:
    """场景 A — 标准二买结构"""
    base_dt_d = datetime(2025, 6, 1, 15, 0)
    base_dt_30 = datetime(2026, 2, 25, 9, 30)
    base_dt_15 = datetime(2026, 2, 25, 9, 30)

    daily_moves = [
        (18.0, 5), (12.0, 20), (16.0, 15), (13.0, 15),
        (17.5, 15), (14.5, 12), (19.0, 15), (16.0, 10), (20.0, 13),
    ]
    bars_d = _gen_zigzag(20.0, daily_moves, Freq.D, symbol, base_dt_d, bar_days=1)

    f30_moves = [
        (18.5, 10), (14.0, 30), (17.5, 25), (15.0, 20),
        (19.0, 25), (16.0, 18), (20.5, 25), (18.0, 15), (22.0, 20), (19.5, 12),
    ]
    bars_30 = _gen_zigzag(20.0, f30_moves, Freq.F30, symbol, base_dt_30, bar_minutes=30)

    f15_moves = [
        (19.0, 15), (14.5, 50), (17.0, 40), (15.5, 35),
        (18.5, 40), (16.0, 30), (20.0, 45), (17.5, 25),
        (21.5, 40), (19.0, 20), (23.0, 35), (20.5, 25),
    ]
    bars_15 = _gen_zigzag(20.0, f15_moves, Freq.F15, symbol, base_dt_15, bar_minutes=15)

    return {"symbol": symbol, "label": "标准二买结构",
            "daily": bars_d, "f30": bars_30, "f15": bars_15}


def gen_scenario_B(symbol: str = "SZ.000002") -> dict:
    """场景 B — 标准三买结构"""
    base_dt_d = datetime(2025, 6, 1, 15, 0)
    base_dt_30 = datetime(2026, 2, 25, 9, 30)
    base_dt_15 = datetime(2026, 2, 25, 9, 30)

    daily_moves = [
        (22.0, 8), (18.0, 12), (21.5, 10), (19.0, 10),
        (24.0, 12), (22.0, 8), (26.5, 15), (24.0, 10),
        (28.0, 15), (25.5, 10), (29.0, 10),
    ]
    bars_d = _gen_zigzag(20.0, daily_moves, Freq.D, symbol, base_dt_d, bar_days=1)

    f30_moves = [
        (22.0, 12), (18.5, 20), (21.0, 18), (19.5, 15),
        (24.0, 20), (21.5, 14), (26.0, 25), (23.5, 15),
        (27.5, 20), (25.0, 12), (28.0, 15), (26.0, 10),
    ]
    bars_30 = _gen_zigzag(20.0, f30_moves, Freq.F30, symbol, base_dt_30, bar_minutes=30)

    f15_moves = [
        (22.0, 20), (18.5, 35), (21.0, 30), (19.5, 25),
        (24.0, 35), (21.5, 22), (26.0, 40), (23.5, 25),
        (27.5, 35), (25.0, 20), (28.5, 30), (26.5, 20), (30.0, 25), (28.0, 18),
    ]
    bars_15 = _gen_zigzag(20.0, f15_moves, Freq.F15, symbol, base_dt_15, bar_minutes=15)

    return {"symbol": symbol, "label": "标准三买结构",
            "daily": bars_d, "f30": bars_30, "f15": bars_15}


def gen_scenario_C(symbol: str = "SZ.000003") -> dict:
    """场景 C — 背驰结构（顶背驰卖出信号为主）"""
    base_dt_d = datetime(2025, 6, 1, 15, 0)
    base_dt_30 = datetime(2026, 2, 25, 9, 30)
    base_dt_15 = datetime(2026, 2, 25, 9, 30)

    daily_moves = [
        (15.0, 8), (22.0, 18), (18.0, 12), (23.5, 20),
        (20.0, 12), (24.0, 18), (21.0, 10), (22.5, 10),
        (19.5, 12), (21.0, 10),
    ]
    bars_d = _gen_zigzag(17.0, daily_moves, Freq.D, symbol, base_dt_d, bar_days=1)

    f30_moves = [
        (15.0, 12), (21.5, 28), (18.0, 18), (22.5, 30),
        (19.0, 16), (23.0, 25), (20.5, 15), (21.5, 12),
        (19.0, 15), (20.5, 15),
    ]
    bars_30 = _gen_zigzag(17.0, f30_moves, Freq.F30, symbol, base_dt_30, bar_minutes=30)

    f15_moves = [
        (15.5, 18), (21.0, 45), (18.5, 30), (22.0, 50),
        (19.5, 25), (22.5, 40), (20.0, 22), (21.0, 18),
        (19.0, 22), (20.5, 20), (18.5, 18), (19.5, 15),
    ]
    bars_15 = _gen_zigzag(17.0, f15_moves, Freq.F15, symbol, base_dt_15, bar_minutes=15)

    return {"symbol": symbol, "label": "顶背驰结构",
            "daily": bars_d, "f30": bars_30, "f15": bars_15}


def gen_scenario_D(symbol: str = "SZ.000004") -> dict:
    """场景 D — 底部震荡（无明显信号）"""
    base_dt_d = datetime(2025, 6, 1, 15, 0)
    base_dt_30 = datetime(2026, 2, 25, 9, 30)
    base_dt_15 = datetime(2026, 2, 25, 9, 30)

    daily_moves = [
        (14.0, 10), (12.0, 15), (13.5, 12), (11.5, 15),
        (13.0, 12), (12.0, 10), (13.2, 12), (11.8, 10),
        (13.0, 12), (12.5, 12),
    ]
    bars_d = _gen_zigzag(13.0, daily_moves, Freq.D, symbol, base_dt_d, bar_days=1)

    f30_moves = [
        (14.0, 15), (12.5, 20), (13.5, 18), (12.0, 18),
        (13.2, 15), (12.3, 15), (13.0, 18), (12.5, 15),
        (13.3, 18), (12.8, 15), (13.5, 15),
    ]
    bars_30 = _gen_zigzag(13.0, f30_moves, Freq.F30, symbol, base_dt_30, bar_minutes=30)

    f15_moves = [
        (14.0, 22), (12.5, 30), (13.5, 28), (12.0, 28),
        (13.2, 25), (12.3, 22), (13.0, 25), (12.5, 22),
        (13.3, 25), (12.8, 22), (13.5, 22), (13.0, 18),
        (13.8, 22), (13.2, 18),
    ]
    bars_15 = _gen_zigzag(13.0, f15_moves, Freq.F15, symbol, base_dt_15, bar_minutes=15)

    return {"symbol": symbol, "label": "底部横盘震荡",
            "daily": bars_d, "f30": bars_30, "f15": bars_15}


# ═══════════════════════════════════════════════════════════
# 分析引擎
# ═══════════════════════════════════════════════════════════

def run_intraday(symbol: str, bars_15: List[RawBar], bars_30: List[RawBar],
                 use_old_scorer: bool = False) -> Tuple:
    """模拟盘中策略：15min + 30min 双级别分析"""
    info = {"15min": {}, "30min": {}}

    az_15 = SymbolAnalyzer(symbol, Freq.F15, bars_15)
    sigs_15 = detect_all_signals(az_15.czsc, symbol)
    info["15min"] = {"bars": len(bars_15), "bis": len(az_15.finished_bis), "signals": sigs_15}

    az_30 = SymbolAnalyzer(symbol, Freq.F30, bars_30)
    sigs_30 = detect_all_signals(az_30.czsc, symbol)
    info["30min"] = {"bars": len(bars_30), "bis": len(az_30.finished_bis), "signals": sigs_30}

    all_sigs = sigs_15 + sigs_30
    if use_old_scorer:
        scored = score_signals_old(symbol, all_sigs)
    else:
        scored = score_signals(symbol, all_sigs)
    return scored, info


def run_review(symbol: str, bars_daily: List[RawBar],
               use_old_scorer: bool = False, use_weekly: bool = True) -> Tuple:
    """模拟盘后策略：日线（+周线）分析"""
    az_d = SymbolAnalyzer(symbol, Freq.D, bars_daily, max_bi_num=200)
    sigs_d = detect_all_signals(az_d.czsc, symbol)
    info = {"daily": {"bars": len(bars_daily), "bis": len(az_d.finished_bis), "signals": sigs_d}}

    all_sigs = list(sigs_d)

    # 周线合成
    if use_weekly:
        bars_w = resample_daily_to_weekly(bars_daily, symbol)
        sigs_w = []
        w_bi_cnt = 0
        if bars_w and len(bars_w) >= 10:
            az_w = SymbolAnalyzer(symbol, Freq.W, bars_w, max_bi_num=100)
            sigs_w = detect_all_signals(az_w.czsc, symbol)
            w_bi_cnt = len(az_w.finished_bis)
        info["weekly"] = {"bars": len(bars_w), "bis": w_bi_cnt, "signals": sigs_w}
        all_sigs.extend(sigs_w)

    if use_old_scorer:
        scored = score_signals_old(symbol, all_sigs)
    else:
        scored = score_signals(symbol, all_sigs)
    return scored, info


# ═══════════════════════════════════════════════════════════
# 输出格式化
# ═══════════════════════════════════════════════════════════

def fmt_signals(sigs: List[SignalEvent]) -> str:
    if not sigs:
        return "无信号"
    return ", ".join(f"{s.signal_type}({s.freq})" for s in sigs)


def fmt_score(scored) -> str:
    """统一格式化得分（兼容新旧 ScoredSymbol）"""
    direction = getattr(scored, 'direction', '')
    dir_tag = f" [{direction}]" if direction else ""
    return f"{scored.total_score:+.1f}{dir_tag}"


# ═══════════════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════════════

def main():
    scenarios = [gen_scenario_A(), gen_scenario_B(), gen_scenario_C(), gen_scenario_D()]

    # ═══════════════════════════════════════════════════════
    # PART 1: 旧版评分对比（无级别系数、回测无周线）
    # ═══════════════════════════════════════════════════════
    print("=" * 76)
    print("  PART 1: 优化前（旧版评分 — 无级别系数、回测仅日线）")
    print("=" * 76)

    old_results = []
    for sc in scenarios:
        sym = sc["symbol"]
        intra_old, intra_info = run_intraday(sym, sc["f15"], sc["f30"], use_old_scorer=True)
        review_old, review_info = run_review(sym, sc["daily"], use_old_scorer=True, use_weekly=False)
        old_results.append({"scenario": sc, "intraday": intra_old, "review": review_old,
                            "intra_info": intra_info, "review_info": review_info})

    print(f"\n  {'标的':<12s} {'场景':<12s} {'盘中得分':>8s} {'回测得分':>8s} {'差值':>8s} {'盘中信号':>8s} {'回测信号':>8s}")
    print(f"  {'─' * 68}")
    for r in old_results:
        sc = r["scenario"]
        intra, review = r["intraday"], r["review"]
        diff = intra.total_score - review.total_score
        print(f"  {sc['symbol']:<12s} {sc['label']:<12s} {intra.total_score:>+8.1f} "
              f"{review.total_score:>+8.1f} {diff:>+8.1f} {intra.signal_count:>8d} {review.signal_count:>8d}")

    # 显示信号详情
    print(f"\n  信号明细:")
    for r in old_results:
        sc = r["scenario"]
        print(f"  {sc['symbol']} {sc['label']}:")
        print(f"    盘中: {fmt_signals(r['intraday'].signals)}")
        print(f"    回测: {fmt_signals(r['review'].signals)}")

    # ═══════════════════════════════════════════════════════
    # PART 2: 新版评分对比（级别系数 + 回测日+周 + 方向判断）
    # ═══════════════════════════════════════════════════════
    print(f"\n\n{'=' * 76}")
    print("  PART 2: 优化后（级别系数 + 回测日+周双级别 + 买卖方向判断）")
    print("=" * 76)

    new_results = []
    for sc in scenarios:
        sym = sc["symbol"]
        intra_new, intra_info = run_intraday(sym, sc["f15"], sc["f30"], use_old_scorer=False)
        review_new, review_info = run_review(sym, sc["daily"], use_old_scorer=False, use_weekly=True)
        new_results.append({"scenario": sc, "intraday": intra_new, "review": review_new,
                            "intra_info": intra_info, "review_info": review_info})

    print(f"\n  {'标的':<12s} {'场景':<12s} {'盘中得分':>10s} {'回测得分':>10s} {'差值':>8s} {'盘中信号':>8s} {'回测信号':>8s}")
    print(f"  {'─' * 72}")
    for r in new_results:
        sc = r["scenario"]
        intra, review = r["intraday"], r["review"]
        diff = intra.total_score - review.total_score
        i_dir = f" [{intra.direction}]" if intra.direction else ""
        r_dir = f" [{review.direction}]" if review.direction else ""
        print(f"  {sc['symbol']:<12s} {sc['label']:<12s} {fmt_score(intra):>10s} "
              f"{fmt_score(review):>10s} {diff:>+8.1f} {intra.signal_count:>8d} {review.signal_count:>8d}")

    # 显示信号详情
    print(f"\n  信号明细（含级别系数和方向判断）:")
    for r in new_results:
        sc = r["scenario"]
        intra, review = r["intraday"], r["review"]
        print(f"\n  {sc['symbol']} {sc['label']}:")
        print(f"    盘中: {fmt_signals(intra.signals)}")
        if intra.signal_count > 0:
            for s in intra.signals:
                mult = FREQ_MULTIPLIER.get(s.freq, 1.0)
                print(f"      [{s.freq}] {s.signal_type} conf={s.confidence:.2f} ×{mult} → 贡献={SIGNAL_WEIGHTS_OLD[s.signal_type]*s.confidence*mult:+.1f}")
            if intra.direction:
                print(f"      方向判断: {intra.direction}")

        print(f"    回测: {fmt_signals(review.signals)}")
        if review.signal_count > 0:
            for s in review.signals:
                mult = FREQ_MULTIPLIER.get(s.freq, 1.0)
                print(f"      [{s.freq}] {s.signal_type} conf={s.confidence:.2f} ×{mult} → 贡献={SIGNAL_WEIGHTS_OLD[s.signal_type]*s.confidence*mult:+.1f}")
            if review.direction:
                print(f"      方向判断: {review.direction}")

        # 周线数据信息
        if "weekly" in r["review_info"]:
            wi = r["review_info"]["weekly"]
            print(f"    周线: {wi['bars']}根 → {wi['bis']}笔  检出 {len(wi['signals'])} 信号")

    # ═══════════════════════════════════════════════════════
    # PART 3: 优化前后变化对比
    # ═══════════════════════════════════════════════════════
    print(f"\n\n{'=' * 76}")
    print("  PART 3: 优化前后变化对比")
    print("=" * 76)

    print(f"\n  {'标的':<12s} {'场景':<12s} │ {'盘中(旧)':>8s} {'盘中(新)':>10s} {'变化':>7s} │ {'回测(旧)':>8s} {'回测(新)':>10s} {'变化':>7s}")
    print(f"  {'─' * 85}")
    for old, new in zip(old_results, new_results):
        sc = old["scenario"]
        i_old = old["intraday"].total_score
        i_new = new["intraday"].total_score
        i_chg = i_new - i_old
        r_old = old["review"].total_score
        r_new = new["review"].total_score
        r_chg = r_new - r_old
        i_dir = f"[{new['intraday'].direction}]" if new['intraday'].direction else ""
        r_dir = f"[{new['review'].direction}]" if new['review'].direction else ""
        print(f"  {sc['symbol']:<12s} {sc['label']:<12s} │ {i_old:>+8.1f} {i_new:>+7.1f}{i_dir:<3s} {i_chg:>+7.1f} │ "
              f"{r_old:>+8.1f} {r_new:>+7.1f}{r_dir:<3s} {r_chg:>+7.1f}")

    # ═══════════════════════════════════════════════════════
    # PART 4: 优化效果分析
    # ═══════════════════════════════════════════════════════
    print(f"\n\n{'=' * 76}")
    print("  PART 4: 优化效果分析")
    print("=" * 76)
    print("""
  优化 1: 级别归一化权重
  ────────────────────────────────────────────────
  效果: 15min 信号得分 ×0.7 折扣，日线信号 ×1.5 加权，周线 ×1.8
  影响: 盘中模式小级别噪声信号被降权，回测模式大级别信号更突出
  举例: 15min一卖 旧=40×0.70=-28.0 → 新=40×0.70×0.7=-19.6（降低30%）
        日线三买 旧=50×0.85=+42.5 → 新=50×0.85×1.5=+63.8（提升50%）

  优化 2: 回测增加周线级别
  ────────────────────────────────────────────────
  效果: 日线 resample 生成周线，日+周双级别分析，可触发共振加分
  影响: 回测模式信号更丰富，与盘中模式的信息量差距缩小
  注意: 模拟数据日线仅~120根，周线约~17根，笔数可能不足

  优化 3: 买卖方向判断
  ────────────────────────────────────────────────
  效果: 标记每个标的的总体方向（偏多/偏空/分歧）
  影响: 当同一标的同时出现买卖信号时，"分歧"标记提醒用户谨慎
  举例: 买力=28.0 vs 卖力=26.3 → 差距<20 → 标记"分歧"

  待优化项（本次未实施）:
  ────────────────────────────────────────────────
  - 信号时间衰减: decay = 0.9^(hours_since / 24)
  - 盘中↔回测交叉验证: 统计历史准确率
  - 权重可配置化: SIGNAL_WEIGHTS → config.py
""")


if __name__ == "__main__":
    main()
