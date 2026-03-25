#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🐲 隆小侠 性能剖析 — Layer 1 + Layer 2

用法：
  python profile_l1l2.py                  # 全市场
  python profile_l1l2.py --skip-futu      # 跳过 Futu/US（省额度）
  python profile_l1l2.py --market a       # 仅 A 股
  python profile_l1l2.py --top-n 5        # L2 取前 5 行业
"""
import sys
import time
import resource
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ─────────────────────────────────────────────────────────
# 计时基础设施
# ─────────────────────────────────────────────────────────

@dataclass
class TimingRecord:
    name: str
    wall_s: float = 0.0
    cpu_s: float = 0.0
    children: List["TimingRecord"] = field(default_factory=list)


@contextmanager
def timed(name: str, parent: Optional[TimingRecord] = None):
    rec = TimingRecord(name=name)
    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    try:
        yield rec
    finally:
        rec.wall_s = time.perf_counter() - wall0
        rec.cpu_s = time.process_time() - cpu0
        if parent is not None:
            parent.children.append(rec)


# ─────────────────────────────────────────────────────────
# API 调用追踪
# ─────────────────────────────────────────────────────────

_api_log: List[dict] = []


def _make_wrapper(orig_fn, source: str, fn_name: str):
    """为单个函数创建追踪 wrapper。"""
    def wrapper(*args, **kwargs):
        hint = _extract_hint(fn_name, args, kwargs)
        t0 = time.perf_counter()
        error = None
        rows = 0
        try:
            result = orig_fn(*args, **kwargs)
            if hasattr(result, '__len__'):
                rows = len(result)
            return result
        except Exception as e:
            error = f"{e.__class__.__name__}: {str(e)[:80]}"
            raise
        finally:
            _api_log.append({
                "source": source,
                "function": fn_name,
                "hint": hint,
                "elapsed_s": time.perf_counter() - t0,
                "rows": rows,
                "error": error,
            })
    return wrapper


def _extract_hint(fn_name: str, args, kwargs) -> str:
    """从调用参数中提取可读的上下文信息。"""
    if fn_name in ("stock_zh_index_daily", "stock_zh_a_minute"):
        sym = args[0] if args else kwargs.get("symbol", "")
        if fn_name == "stock_zh_a_minute":
            period = args[1] if len(args) > 1 else kwargs.get("period", "")
            return f"{sym}, {period}"
        return str(sym)
    if fn_name == "stock_board_industry_hist_em":
        return str(args[0] if args else kwargs.get("symbol", ""))
    if fn_name in ("stock_margin_detail_sse",):
        return str(kwargs.get("date", args[0] if args else ""))
    if fn_name in ("stock_zt_pool_em", "stock_zt_pool_strong_em",
                    "stock_zt_pool_dtgc_em", "stock_zt_pool_zbgc_em"):
        return str(kwargs.get("date", args[0] if args else ""))
    return ""


def patch_akshare():
    """Monkey-patch akshare 函数。"""
    import akshare as ak
    targets = [
        "stock_zh_index_daily",
        "stock_zh_a_minute",
        "stock_board_change_em",
        "stock_board_industry_name_em",
        "stock_board_industry_cons_em",
        "stock_board_industry_hist_em",
        "stock_zt_pool_em",
        "stock_zt_pool_strong_em",
        "stock_margin_detail_sse",
        "stock_zt_pool_dtgc_em",
        "stock_zt_pool_zbgc_em",
        "stock_info_a_code_name",
    ]
    for name in targets:
        orig = getattr(ak, name, None)
        if orig and not getattr(orig, "_patched", False):
            w = _make_wrapper(orig, "AKShare", name)
            w._patched = True
            setattr(ak, name, w)


def patch_futu():
    """Monkey-patch FutuSource 方法。"""
    try:
        from signals.data.fetcher import FutuSource
        for method_name in ("get_index_kline", "get_a_minute",
                            "get_us_daily", "get_us_minute",
                            "get_us_index_kline"):
            orig = getattr(FutuSource, method_name, None)
            if orig and not getattr(orig, "_patched", False):
                w = _make_wrapper(orig, "Futu", method_name)
                w._patched = True
                setattr(FutuSource, method_name, w)
    except ImportError:
        pass


def patch_yfinance():
    """Monkey-patch YFinanceSource 方法。"""
    try:
        from signals.data.fetcher import YFinanceSource
        for method_name in ("get_us_daily", "get_us_minute"):
            orig = getattr(YFinanceSource, method_name, None)
            if orig and not getattr(orig, "_patched", False):
                w = _make_wrapper(orig, "yfinance", method_name)
                w._patched = True
                setattr(YFinanceSource, method_name, w)
    except ImportError:
        pass


# ─────────────────────────────────────────────────────────
# 报告输出
# ─────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 12) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _build_source_stats() -> dict:
    """从 _api_log 聚合数据源统计，供 CLI 和飞书卡片共用。"""
    sources = {}
    for e in _api_log:
        s = e["source"]
        sources.setdefault(s, {"total": 0, "ok": 0, "fail": 0, "time": 0.0})
        sources[s]["total"] += 1
        sources[s]["time"] += e["elapsed_s"]
        if e["error"]:
            sources[s]["fail"] += 1
        else:
            sources[s]["ok"] += 1
    return sources


def build_perf_summary(root: TimingRecord) -> dict:
    """组装性能摘要 dict，供飞书卡片和 CLI 共用。"""
    total_wall = root.wall_s or 0.001
    total_api = len(_api_log)
    ok_count = sum(1 for e in _api_log if not e["error"])
    fail_count = total_api - ok_count
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)

    l1_s = sum(c.wall_s for c in root.children if c.name.startswith("L1"))
    l2_s = sum(c.wall_s for c in root.children if c.name.startswith("L2"))

    sources = _build_source_stats()
    failures = []
    for e in _api_log:
        if e["error"]:
            hint = f"({e['hint']})" if e["hint"] else ""
            failures.append(f"{e['source']}.{e['function']}{hint} → {e['error']}")

    return {
        "total_s": round(total_wall, 1),
        "api_count": total_api,
        "ok_count": ok_count,
        "fail_count": fail_count,
        "mem_mb": round(rss_mb),
        "l1_s": round(l1_s, 1),
        "l2_s": round(l2_s, 1),
        "sources": sources,
        "failures": failures,
    }


def print_report(root: TimingRecord, detail: bool = False):
    total_wall = root.wall_s or 0.001
    total_cpu = sum(c.cpu_s for c in root.children)
    total_api = len(_api_log)
    total_rows = sum(e["rows"] for e in _api_log)
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    ok_count = sum(1 for e in _api_log if not e["error"])
    fail_count = total_api - ok_count

    # ══ 始终输出：一行精简摘要 ══
    l1_s = sum(c.wall_s for c in root.children if c.name.startswith("L1"))
    l2_s = sum(c.wall_s for c in root.children if c.name.startswith("L2"))
    l1_pct = l1_s / total_wall * 100 if total_wall else 0
    l2_pct = l2_s / total_wall * 100 if total_wall else 0

    print(f"\n🐲 隆小侠 L1+L2  "
          f"总计 {total_wall:.1f}s | API {total_api}次 | "
          f"✅{ok_count} ❌{fail_count}")
    print(f"  L1 指数研判 {l1_s:.1f}s ({l1_pct:.0f}%)  |  "
          f"L2 行业筛选 {l2_s:.1f}s ({l2_pct:.0f}%)")

    if not detail:
        # 有失败时额外提示一行
        if fail_count > 0:
            failures = [e for e in _api_log if e["error"]]
            for e in failures[:2]:
                hint = f"({e['hint']})" if e["hint"] else ""
                print(f"  ⚠ {e['source']}.{e['function']}{hint} "
                      f"→ {e['error'][:60]}")
            if len(failures) > 2:
                print(f"  ... 共 {fail_count} 个失败，用 --detail 查看全部")
        print(f"  (用 --detail 查看完整性能分析)")
        return

    # ══ --detail 模式：完整输出 ══
    print(f"\n{'═' * 60}")

    # ── 耗时瀑布 ──
    print("─" * 60)
    for child in root.children:
        pct = child.wall_s / total_wall * 100
        print(f"  {child.name}  {child.wall_s:.1f}s  "
              f"{_bar(pct)}  {pct:.0f}%")
        for sub in child.children:
            sub_pct = sub.wall_s / total_wall * 100
            print(f"    {sub.name}  {sub.wall_s:.1f}s  "
                  f"{_bar(sub_pct)}  {sub_pct:.0f}%")
    print("─" * 60)
    print(f"  总计 {total_wall:.1f}s  |  API {total_api}次  "
          f"|  内存 {rss_mb:.0f}MB  |  CPU {total_cpu:.1f}s  "
          f"|  数据 {total_rows:,}行")

    # ── 数据源健康度 ──
    sources = _build_source_stats()
    print(f"\n数据源健康度")
    print("─" * 60)
    print(f"  {'源':<10} {'调用':>4} {'成功':>4} {'失败':>4} "
          f"{'平均延迟':>8}  状态")
    print("  " + "─" * 56)

    all_sources = ["AKShare", "Futu", "yfinance", "Tushare"]
    for s in all_sources:
        if s in sources:
            d = sources[s]
            avg = d["time"] / d["total"] if d["total"] else 0
            fail_rate = d["fail"] / d["total"] if d["total"] else 0
            if fail_rate > 0.3:
                status = "❌ 不稳定"
            elif fail_rate > 0.1:
                status = "⚠️  间歇失败"
            else:
                status = "✅ 正常"
            if s == "Futu":
                status += f" (额度 {d['total']}/1000)"
            print(f"  {s:<12} {d['total']:>4} {d['ok']:>4} {d['fail']:>4} "
                  f"{avg:>7.2f}s  {status}")
        else:
            print(f"  {s:<12}    -    -    -        -  ⏭  未触发")

    # 失败明细
    failures = [e for e in _api_log if e["error"]]
    if failures:
        print(f"\n  失败明细:")
        for e in failures:
            hint = f"({e['hint']})" if e["hint"] else ""
            print(f"    ✗ {e['source']}.{e['function']}{hint} "
                  f"→ {e['error']}")

    # ── 最慢 API Top 5 ──
    sorted_apis = sorted(_api_log, key=lambda x: -x["elapsed_s"])
    print(f"\n  最慢 API Top 5:")
    for i, e in enumerate(sorted_apis[:5], 1):
        hint = f"({e['hint']})" if e["hint"] else ""
        name = f"{e['function']}{hint}"
        err = " ✗" if e["error"] else ""
        print(f"    {i}. {name:<42} {e['elapsed_s']:>6.2f}s  "
              f"{e['rows']:>6,}行{err}")

    # ── 定时轮询评估 ──
    print(f"\n{'─' * 60}")
    print(f"定时轮询评估 (低频单机场景)")
    print("─" * 60)

    interval = max(60, int(total_wall * 3))
    print(f"  当前: 单次 ~{total_wall:.0f}s → 轮询间隔建议 ≥{interval}s")
    print(f"  硬件: 单机足够 (CPU {total_cpu:.1f}s, "
          f"内存 {rss_mb:.0f}MB, CZSC Rust加速)")

    # ── 优化建议 ──
    print(f"\n  近期可做 (不改架构):")

    l1_children = [c for c in root.children if c.name.startswith("L1")]
    if l1_children:
        l1_rec = l1_children[0]
        fetch_subs = [s for s in l1_rec.children if "CZSC" not in s.name]
        if len(fetch_subs) >= 2:
            seq_total = sum(s.wall_s for s in fetch_subs)
            par_est = max(s.wall_s for s in fetch_subs)
            saving = seq_total - par_est
            if saving > 1:
                new_total = total_wall - saving
                print(f"    ▸ L1 三源并行 (ThreadPool) → "
                      f"省 ~{saving:.0f}s，总耗时降至 ~{new_total:.0f}s")

    l2_pool_apis = [e for e in _api_log if e["function"] in (
        "stock_board_change_em", "stock_board_industry_name_em",
        "stock_zt_pool_em", "stock_zt_pool_strong_em",
        "stock_margin_detail_sse", "stock_zt_pool_dtgc_em",
        "stock_zt_pool_zbgc_em", "stock_info_a_code_name")]
    if len(l2_pool_apis) >= 3:
        l2_total = sum(e["elapsed_s"] for e in l2_pool_apis)
        l2_max = max(e["elapsed_s"] for e in l2_pool_apis)
        l2_saving = l2_total - l2_max
        if l2_saving > 1:
            print(f"    ▸ L2 {len(l2_pool_apis)}个 pool 并行加载 → "
                  f"省 ~{l2_saving:.0f}s")

    daily_apis = [e for e in _api_log
                  if e["function"] == "stock_zh_index_daily" and not e["error"]]
    if daily_apis:
        daily_total = sum(e["elapsed_s"] for e in daily_apis)
        print(f"    ▸ 指数日线日级磁盘缓存 → "
              f"省 {len(daily_apis)}次API / ~{daily_total:.0f}s")

    name_apis = [e for e in _api_log
                 if e["function"] == "stock_info_a_code_name" and not e["error"]]
    if name_apis:
        name_total = sum(e["elapsed_s"] for e in name_apis)
        print(f"    ▸ 股票名称映射本地持久化 → "
              f"省 {len(name_apis)}次API / ~{name_total:.1f}s")

    total_saving = 0
    if l1_children:
        fetch_subs = [s for s in l1_children[0].children if "CZSC" not in s.name]
        if len(fetch_subs) >= 2:
            total_saving += sum(s.wall_s for s in fetch_subs) - max(s.wall_s for s in fetch_subs)
    if len(l2_pool_apis) >= 3:
        total_saving += sum(e["elapsed_s"] for e in l2_pool_apis) - max(e["elapsed_s"] for e in l2_pool_apis)
    if daily_apis:
        total_saving += sum(e["elapsed_s"] for e in daily_apis)
    if total_saving > 2:
        optimized = total_wall - total_saving
        print(f"    → 合计: ~{total_wall:.0f}s → ~{max(optimized, 5):.0f}s")

    print(f"\n  中期优化 (可选):")
    futu_fails = sources.get("Futu", {}).get("fail", 0)
    futu_total = sources.get("Futu", {}).get("total", 0)
    if futu_total > 0 and futu_fails / futu_total > 0.1:
        print(f"    ▸ Futu 失败率 {futu_fails}/{futu_total}，"
              f"考虑升级 VIP 或切换 IB Gateway")
    elif futu_total == 0:
        print(f"    ▸ Futu 未使用 → 如需港/美股实时，可开通 Futu VIP")
    else:
        print(f"    ▸ 数据源升级: Futu VIP (更大额度) 或 IB Gateway (美股)")

    ak_fails = sources.get("AKShare", {}).get("fail", 0)
    ak_total = sources.get("AKShare", {}).get("total", 0)
    if ak_total > 0 and ak_fails / ak_total > 0.2:
        print(f"    ▸ AKShare 失败率 {ak_fails}/{ak_total} 偏高，"
              f"建议 Tushare Pro 作为备用 (积分200+免费)")

    print(f"    ▸ 增量更新: 分钟线只拉最新 bar，不每次全量")
    print(f"    ▸ AKShare 限频时自动降速 (exponential backoff)")

    print(f"\n  暂不需要:")
    print(f"    ✗ 多机/分布式 — 低频策略单机绑绑有余")
    print(f"    ✗ Redis 缓存 — 简单磁盘文件缓存足够")
    print(f"    ✗ 异步IO — ThreadPool 已够 (网络IO为主)")
    print("═" * 60)


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def run_profile(args):
    # 必须先 patch 再 import 使用方
    patch_akshare()
    patch_futu()
    patch_yfinance()

    import config
    from signals.layers.index_screener import IndexScreener

    # 市场过滤
    ak_codes = config.INDEX_AK_CODES
    futu_codes = config.INDEX_FUTU_CODES
    us_codes = config.INDEX_US_CODES

    if args.skip_futu:
        futu_codes = {}
        us_codes = {}
    elif args.market:
        markets = {m.strip().lower() for m in args.market.split(",")}
        if "a" not in markets:
            ak_codes = {}
        if "hk" not in markets:
            futu_codes = {}
        if "us" not in markets:
            us_codes = {}

    lb = config.INDEX_LOOKBACK_DAYS
    top_n = args.top_n or config.RANK_TOP_N

    root = TimingRecord(name="total")

    # ── Layer 1 ──────────────────────────────────────
    with timed("L1 指数研判", root) as l1:
        screener = IndexScreener(
            ak_codes=ak_codes, futu_codes=futu_codes, us_codes=us_codes)

        with timed("L1.1 A股指数 (AKShare)", l1):
            screener._load_ak_indices(lookback_days=lb)

        with timed("L1.2 港股指数 (Futu)", l1):
            screener._load_futu_indices(lookback_days=lb)

        with timed("L1.3 美股ETF (Futu/yf)", l1):
            screener._load_us_indices(lookback_days=lb)

        with timed("L1.4 CZSC分析", l1):
            ctx = screener.analyze()

    # 打印 Layer 1 报告（保留原有输出）
    ctx.print_report()

    # ── Layer 2 ──────────────────────────────────────
    with timed("L2 行业筛选", root) as l2:
        from signals.layers.industry import get_industry_representatives
        try:
            gain_list, composite_list, merged_list, *_ = \
                get_industry_representatives(top_n)
        except Exception as e:
            print(f"  [!] Layer 2 异常: {e}")
            gain_list, composite_list, merged_list = [], [], []

    # Layer 2 简要输出
    if gain_list:
        print(f"\n  涨幅榜 Top 3: ", end="")
        print(", ".join(f"{r.name}({r.gain_pct:+.1f}%)" for r in gain_list[:3]))
    if composite_list:
        print(f"  综合榜 Top 3: ", end="")
        print(", ".join(
            f"{r.name}({r.composite_score:.0f}分)" for r in composite_list[:3]))
    if merged_list:
        total_stocks = sum(len(r.pool_codes) for r in merged_list)
        print(f"  合计: {len(merged_list)} 行业, {total_stocks} 只代表股")

    # ── 汇总 ──
    root.wall_s = sum(c.wall_s for c in root.children)
    root.cpu_s = sum(c.cpu_s for c in root.children)

    # CLI 报告
    print_report(root, detail=args.detail)

    # 组装 perf_summary
    perf = build_perf_summary(root)

    # 飞书卡片推送
    if not args.no_push:
        try:
            card = ctx.to_feishu_card(
                perf_summary=perf,
                l2_gain=gain_list[:3] if gain_list else [],
                l2_composite=composite_list[:3] if composite_list else [],
            )
            from signals.notify import send_card
            send_card(card)
        except Exception as e:
            print(f"  [!] 飞书卡片推送异常: {e}")

    return ctx, perf


def main():
    parser = argparse.ArgumentParser(
        description="🐲 隆小侠 性能剖析 — Layer 1 + Layer 2")
    parser.add_argument("--skip-futu", action="store_true",
                        help="跳过 Futu/US，省额度")
    parser.add_argument("--market", default=None,
                        help="市场过滤: a / hk / us / a,hk / all")
    parser.add_argument("--top-n", type=int, default=None,
                        help="L2 取前 N 行业 (默认 config.RANK_TOP_N)")
    parser.add_argument("--detail", action="store_true",
                        help="显示完整性能分析（瀑布图+健康度+优化建议）")
    parser.add_argument("--no-push", action="store_true",
                        help="跳过飞书推送（调试用）")
    args = parser.parse_args()
    run_profile(args)


if __name__ == "__main__":
    main()
