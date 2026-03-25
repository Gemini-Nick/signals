# -*- coding: utf-8 -*-
"""
方向扫描脚本 — 观点驱动的信号扫描（CLI 入口）

用法:
    python scripts/scan_direction.py --direction 上证50
    python scripts/scan_direction.py --direction 半导体 --mode panic
    python scripts/scan_direction.py --codes SH.601318,SZ.000001
"""
import sys
import os
import argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import warnings
warnings.filterwarnings("ignore")

from signals.core.signal_filter import scan_direction, ScanResult
from typing import List


def print_results(results: List[ScanResult], mode: str = "belief"):
    """格式化输出扫描结果"""
    if not results:
        print("\n无结果")
        return

    grade_a = [r for r in results if r.grade == "A"]
    grade_b = [r for r in results if r.grade == "B"]
    grade_c = [r for r in results if r.grade == "C"]

    mode_label = "恐慌抄底" if mode == "panic" else "信念方向"
    print(f"\n{'='*60}")
    print(f"  扫描模式: {mode_label} | 共 {len(results)} 只")
    print(f"{'='*60}")

    if grade_a:
        print(f"\n★ 多维确认 ({len(grade_a)} 只) — 推荐关注")
        print("-" * 58)
        for r in grade_a:
            sigs = " + ".join(r.czsc_signals) if r.czsc_signals else "无"
            print(f"  {r.symbol:12s} {r.change_pct:+6.2f}%  "
                  f"MA: {r.ma_status:16s}  "
                  f"信号: {sigs:20s}  "
                  f"量: {r.volume_status}")
            if mode == "panic" and r.cap_score >= 60:
                print(f"  {'':12s}  割肉指标: {r.cap_score}分  "
                      f"兑现目标: {r.target_price}")

    if grade_b:
        print(f"\n◆ 单维确认 ({len(grade_b)} 只) — 可观察")
        print("-" * 58)
        for r in grade_b:
            sigs = " + ".join(r.czsc_signals) if r.czsc_signals else "无"
            print(f"  {r.symbol:12s} {r.change_pct:+6.2f}%  "
                  f"MA: {r.ma_status:16s}  "
                  f"信号: {sigs:20s}  "
                  f"量: {r.volume_status}")

    if grade_c:
        print(f"\n○ 暂无信号 ({len(grade_c)} 只)")
        for r in grade_c[:10]:
            print(f"  {r.symbol:12s} {r.change_pct:+6.2f}%  MA: {r.ma_status}")
        if len(grade_c) > 10:
            print(f"  ... 还有 {len(grade_c) - 10} 只")

    print(f"\n{'='*60}")
    print(f"  A级: {len(grade_a)} | B级: {len(grade_b)} | C级: {len(grade_c)}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="方向扫描 — 观点驱动的信号扫描")
    parser.add_argument("--direction", "-d", type=str, default="")
    parser.add_argument("--mode", "-m", type=str, default="belief",
                        choices=["belief", "panic"])
    parser.add_argument("--codes", "-c", type=str, default="")
    parser.add_argument("--top", "-t", type=int, default=50)
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
