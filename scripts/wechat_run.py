#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Tool Runner — CC 决定调哪个工具，这里只负责执行

不做意图理解，不做关键词匹配。CC 理解用户意图后按工具名调用。

用法:
    python scripts/wechat_run.py industry_ranking
    python scripts/wechat_run.py industry_ranking --concepts
    python scripts/wechat_run.py review
    python scripts/wechat_run.py review --date 2024-09-24
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description="WeChat tool runner")
    sub = parser.add_subparsers(dest="tool")

    # industry_ranking
    p_ind = sub.add_parser("industry_ranking", help="全市场行业排行")
    p_ind.add_argument("--concepts", action="store_true", help="包含概念板块排行")

    # review
    p_rev = sub.add_parser("review", help="盘后复盘")
    p_rev.add_argument("--date", default="yesterday", help="复盘日期")

    args = parser.parse_args()

    if not args.tool:
        parser.print_help()
        sys.exit(1)

    from signals.wechat.skills import industry_ranking, review

    if args.tool == "industry_ranking":
        print(industry_ranking(include_concepts=args.concepts))
    elif args.tool == "review":
        print(review(date=args.date))


if __name__ == "__main__":
    main()
