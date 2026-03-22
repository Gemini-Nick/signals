# -*- coding: utf-8 -*-
"""
方向扫描脚本 — 观点驱动的信号扫描

用法:
    python scripts/scan_direction.py --direction 上证50
    python scripts/scan_direction.py --direction 半导体
    python scripts/scan_direction.py --direction 上证50 --mode panic
    python scripts/scan_direction.py --codes SH.601318,SZ.000001

输出: 成分股的 MA位置 + 缠论信号 + 量能状态，按确认维度数量排序
"""
import sys
import os
import argparse
from datetime import datetime, timedelta

# 项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import warnings
warnings.filterwarnings("ignore")


def get_index_constituents(index_name: str) -> list:
    """获取指数成分股列表（Futu格式）"""
    import akshare as ak
    from config import INDEX_AK_CODES

    # 先检查是不是指数名
    ak_code = INDEX_AK_CODES.get(index_name)
    if ak_code:
        # 去掉 sh/sz 前缀，只取数字
        code = ak_code.replace("sh", "").replace("sz", "")
        try:
            df = ak.index_stock_cons(symbol=code)
            codes = df["品种代码"].tolist()
            symbols = []
            for c in codes:
                if c.startswith("6"):
                    symbols.append(f"SH.{c}")
                else:
                    symbols.append(f"SZ.{c}")
            return symbols
        except Exception as e:
            print(f"[WARN] 获取{index_name}成分股失败: {e}")
            return []

    # 不是指数，尝试作为行业名
    try:
        from signals.layers.industry import get_industry_stocks
        stocks = get_industry_stocks(index_name)
        if stocks:
            return stocks
    except Exception as e:
        print(f"[WARN] 获取{index_name}成分股失败: {e}")

    return []


def analyze_stock(symbol: str, bars, mode: str = "belief") -> dict:
    """分析单只股票: MA位置 + 缠论信号 + 量能状态"""
    from czsc import Freq
    from signals.core.analyzer import SymbolAnalyzer
    from signals.core.detectors import detect_all_signals
    from signals.core.ma_levels import compute_ma_levels
    from signals.core.anomaly import compute_anomaly_profile

    result = {
        "symbol": symbol,
        "name": "",
        "price": 0.0,
        "change_pct": 0.0,
        "ma_status": "",
        "ma_detail": "",
        "czsc_signals": [],
        "volume_status": "",
        "cap_score": 0,
        "grade": "C",
        "action": "等待",
        "confirmations": 0,
    }

    if not bars or len(bars) < 60:
        result["action"] = "数据不足"
        return result

    latest = bars[-1]
    prev = bars[-2] if len(bars) > 1 else latest
    result["price"] = latest.close
    result["change_pct"] = round((latest.close - prev.close) / prev.close * 100, 2)

    # 1. MA 位置
    ma_ctx = compute_ma_levels(bars, symbol)
    if ma_ctx:
        result["ma_detail"] = ma_ctx.trend_summary
        # 找最近的支撑/阻力
        near_support = None
        for lvl in ma_ctx.support_levels:
            if abs(lvl.distance_pct) <= 5.0:
                near_support = lvl
                break
        if near_support:
            result["ma_status"] = f"{near_support.name}支撑({near_support.distance_pct:+.1f}%)"
            result["confirmations"] += 1
        elif ma_ctx.key_levels:
            kl = ma_ctx.key_levels[0]
            result["ma_status"] = f"{kl.name}({kl.distance_pct:+.1f}%)"
        else:
            result["ma_status"] = ma_ctx.trend_summary

    # 2. 缠论信号
    try:
        analyzer = SymbolAnalyzer(symbol, Freq.D, bars)
        signals = detect_all_signals(analyzer.czsc, symbol)
        buy_signals = [s for s in signals if "买" in s.signal_type]
        sell_signals = [s for s in signals if "卖" in s.signal_type]

        if buy_signals:
            # 只取最新的买点
            latest_buy = buy_signals[-1]
            result["czsc_signals"].append(
                f"{latest_buy.signal_type}(conf={latest_buy.confidence:.0%})"
            )
            result["confirmations"] += 1
        if sell_signals:
            latest_sell = sell_signals[-1]
            result["czsc_signals"].append(
                f"{latest_sell.signal_type}(conf={latest_sell.confidence:.0%})"
            )
    except Exception as e:
        result["czsc_signals"] = [f"分析异常: {e}"]

    # 3. 量能状态
    anomaly = compute_anomaly_profile(symbol, bars)
    if anomaly:
        result["cap_score"] = int(anomaly.capitulation_score)
        vol_item = anomaly.items.get("volume")
        if vol_item and vol_item.z_score is not None:
            if vol_item.z_score >= 2.0:
                result["volume_status"] = f"放量({vol_item.z_score:.1f}σ)"
                result["confirmations"] += 1
            elif vol_item.z_score <= -1.5:
                result["volume_status"] = f"缩量({vol_item.z_score:.1f}σ)"
            else:
                result["volume_status"] = "正常"
        else:
            result["volume_status"] = "正常"

        # panic模式: 割肉指标高 = 确认
        if mode == "panic" and anomaly.capitulation_score >= 60:
            result["confirmations"] += 1
    else:
        result["volume_status"] = "无数据"

    # 4. 分级
    if result["confirmations"] >= 2:
        result["grade"] = "A"
        result["action"] = "推荐关注"
    elif result["confirmations"] >= 1:
        result["grade"] = "B"
        result["action"] = "可观察"
    else:
        result["grade"] = "C"
        result["action"] = "等待"

    return result


def scan_direction(direction: str, mode: str = "belief",
                   codes: list = None, top_n: int = 50) -> list:
    """
    扫描一个方向的成分股信号。

    :param direction: 方向名（如"上证50"、"半导体"）
    :param mode: "belief"(信念模式) / "panic"(恐慌抄底)
    :param codes: 直接指定股票代码列表（优先于direction）
    :param top_n: 最多扫描前N只
    :return: 分析结果列表，按确认维度排序
    """
    from signals.data.fetcher import AKShareSource

    # 获取股票列表
    if codes:
        symbols = codes
        print(f"[SCAN] 扫描自定义列表: {len(symbols)} 只")
    else:
        symbols = get_index_constituents(direction)
        if not symbols:
            print(f"[ERROR] 无法获取 {direction} 的成分股")
            return []
        print(f"[SCAN] {direction}: {len(symbols)} 只成分股")

    symbols = symbols[:top_n]

    # 获取日K数据
    ak_source = AKShareSource()
    edt = datetime.now().strftime("%Y%m%d")
    sdt = (datetime.now() - timedelta(days=300)).strftime("%Y%m%d")

    results = []
    for i, sym in enumerate(symbols):
        try:
            bars = ak_source.get_a_daily(sym, sdt, edt)
            if not bars:
                continue
            r = analyze_stock(sym, bars, mode)
            # 尝试获取股票名称
            try:
                import akshare as ak
                code = sym.replace("SH.", "").replace("SZ.", "")
                r["name"] = code  # 用代码作为默认名
            except Exception:
                pass
            results.append(r)
            if (i + 1) % 10 == 0:
                print(f"  已扫描 {i+1}/{len(symbols)}...")
        except Exception as e:
            print(f"  [SKIP] {sym}: {e}")
            continue

    # 排序: 确认维度多的排前面，同级别按涨跌幅排
    results.sort(key=lambda x: (-x["confirmations"], -x["change_pct"]))
    return results


def print_results(results: list, mode: str = "belief"):
    """格式化输出结果"""
    if not results:
        print("\n无结果")
        return

    grade_a = [r for r in results if r["grade"] == "A"]
    grade_b = [r for r in results if r["grade"] == "B"]
    grade_c = [r for r in results if r["grade"] == "C"]

    mode_label = "恐慌抄底" if mode == "panic" else "信念方向"
    print(f"\n{'='*60}")
    print(f"  扫描模式: {mode_label} | 共 {len(results)} 只")
    print(f"{'='*60}")

    if grade_a:
        print(f"\n★ 多维确认 ({len(grade_a)} 只) — 推荐关注")
        print("-" * 58)
        for r in grade_a:
            signals_str = " + ".join(r["czsc_signals"]) if r["czsc_signals"] else "无"
            print(f"  {r['symbol']:12s} {r['change_pct']:+6.2f}%  "
                  f"MA: {r['ma_status']:16s}  "
                  f"信号: {signals_str:20s}  "
                  f"量: {r['volume_status']}")
            if mode == "panic" and r["cap_score"] >= 60:
                print(f"  {'':12s}  割肉指标: {r['cap_score']}分")

    if grade_b:
        print(f"\n◆ 单维确认 ({len(grade_b)} 只) — 可观察")
        print("-" * 58)
        for r in grade_b:
            signals_str = " + ".join(r["czsc_signals"]) if r["czsc_signals"] else "无"
            print(f"  {r['symbol']:12s} {r['change_pct']:+6.2f}%  "
                  f"MA: {r['ma_status']:16s}  "
                  f"信号: {signals_str:20s}  "
                  f"量: {r['volume_status']}")

    if grade_c:
        print(f"\n○ 暂无信号 ({len(grade_c)} 只)")
        # 只打印前 10 只
        for r in grade_c[:10]:
            print(f"  {r['symbol']:12s} {r['change_pct']:+6.2f}%  "
                  f"MA: {r['ma_status']}")
        if len(grade_c) > 10:
            print(f"  ... 还有 {len(grade_c) - 10} 只")

    print(f"\n{'='*60}")
    print(f"  A级(多维确认): {len(grade_a)} | "
          f"B级(单维确认): {len(grade_b)} | "
          f"C级(等待): {len(grade_c)}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="方向扫描 — 观点驱动的信号扫描")
    parser.add_argument("--direction", "-d", type=str, default="",
                        help="方向名称，如'上证50'、'半导体'、'银行'")
    parser.add_argument("--mode", "-m", type=str, default="belief",
                        choices=["belief", "panic"],
                        help="模式: belief(信念方向) / panic(恐慌抄底)")
    parser.add_argument("--codes", "-c", type=str, default="",
                        help="直接指定股票代码，逗号分隔，如 SH.601318,SZ.000001")
    parser.add_argument("--top", "-t", type=int, default=50,
                        help="最多扫描前N只（默认50）")
    args = parser.parse_args()

    if not args.direction and not args.codes:
        parser.print_help()
        sys.exit(1)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None

    print(f"\n[START] 方向扫描 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    results = scan_direction(
        direction=args.direction,
        mode=args.mode,
        codes=codes,
        top_n=args.top,
    )
    print_results(results, args.mode)


if __name__ == "__main__":
    main()
