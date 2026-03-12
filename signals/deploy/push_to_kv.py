# -*- coding: utf-8 -*-
"""
推送分析结果到 Upstash Redis，供 Vercel 前端读取。

用法：
  python run.py --mode index --push             # 分析完自动推送
  python run.py --mode index --push --dry-run   # dry-run（mock Redis，不连网）
  python -m signals.deploy.push_to_kv           # 独立运行（先跑分析再推送）
  python -m signals.deploy.push_to_kv --dry-run # 独立 dry-run（mock 数据）

环境变量：
  UPSTASH_REDIS_REST_URL    Upstash REST endpoint
  UPSTASH_REDIS_REST_TOKEN  Upstash REST token
"""
import os
import json
import time

from signals.web.services.serializers import (
    serialize_market_context,
    serialize_index_report,
    serialize_bars,
    serialize_bi_list,
    serialize_fx_list,
    serialize_zhongshu,
    serialize_signals,
    serialize_scored_symbol,
)


class _MockRedis:
    """内存 mock，用于 dry-run 验证序列化 + 推送逻辑（不连网）"""

    def __init__(self):
        self._store = {}

    def set(self, key, value):
        self._store[key] = value
        size = len(value) if isinstance(value, str) else len(json.dumps(value))
        print(f"    [mock] SET {key}  ({size:,} bytes)")

    def get(self, key):
        return self._store.get(key)

    def summary(self):
        total = sum(len(v) if isinstance(v, str) else 0 for v in self._store.values())
        print(f"\n  [mock] 共 {len(self._store)} 个 key, 总计 {total:,} bytes")


def _get_redis():
    """创建 Upstash Redis 客户端"""
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        raise EnvironmentError(
            "请设置 UPSTASH_REDIS_REST_URL 和 UPSTASH_REDIS_REST_TOKEN 环境变量。\n"
            "可在 .env 文件中配置，或通过 Upstash Console 获取。"
        )
    from upstash_redis import Redis
    return Redis(url=url, token=token)


def _push_chart_data(redis, name, idx_az):
    """推送单个指数的三个周期图表数据，返回推送数量"""
    count = 0
    freq_map = {
        "daily": "_daily",
        "30min": "_f30",
        "15min": "_f15",
    }
    for freq, attr in freq_map.items():
        sa = getattr(idx_az, attr, None)
        if sa is None:
            continue

        chart_data = {
            "ohlcv": serialize_bars(sa),
            "bi_list": serialize_bi_list(sa),
            "fx_list": serialize_fx_list(sa),
            "zhongshu": serialize_zhongshu(sa),
            "signals": [],
            "meta": {
                "name": name,
                "symbol": idx_az.symbol,
                "freq": freq,
            },
        }

        # 信号检测
        try:
            from signals.core.detectors import detect_all_signals
            detected = detect_all_signals(sa.czsc, idx_az.symbol)
            chart_data["signals"] = serialize_signals(detected)
        except Exception:
            pass

        key = f"signals:chart:{name}:{freq}"
        redis.set(key, json.dumps(chart_data, ensure_ascii=False))
        count += 1
    return count


def _build_mock_data():
    """生成 mock 分析数据，用于 dry-run 验证推送逻辑"""
    from datetime import datetime

    mock_ctx = {
        "overall_sentiment": "震荡偏多",
        "phase": "上涨",
        "strong_sectors": ["半导体", "有色金属"],
        "weak_sectors": [],
        "summary": "[dry-run] 模拟市场环境数据",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    mock_reports = [
        {
            "name": "沪深300",
            "symbol": "000300.SH",
            "direction": "上涨",
            "score": 75,
            "signals": ["日线一买"],
            "zs_level": "日线",
            "summary": "[dry-run] 模拟指数报告",
        },
        {
            "name": "创业板指",
            "symbol": "399006.SZ",
            "direction": "盘整",
            "score": 50,
            "signals": [],
            "zs_level": "30分钟",
            "summary": "[dry-run] 模拟指数报告",
        },
    ]

    return mock_ctx, mock_reports


def push_from_screener(screener=None, dry_run=False):
    """
    从 IndexScreener 推送结果到 Upstash Redis。

    :param screener: IndexScreener 实例。为 None 则通过 WebEngine 获取。
    :param dry_run: True 则使用 MockRedis + mock 数据，不连网。
    """
    if dry_run:
        redis = _MockRedis()
        print("\n  [dry-run] 使用 mock 数据验证推送逻辑...\n")
    else:
        redis = _get_redis()

    pushed_keys = []

    # ── 获取数据 ──
    if dry_run and screener is None:
        # dry-run 模式：用 mock 数据，跳过分析引擎
        ctx_data, reports_data = _build_mock_data()
        analyzers = {}
        scored = []
    elif screener is None:
        from signals.web.services.engine import get_engine
        engine = get_engine()
        if not engine.is_ready():
            print("  WebEngine 尚未初始化，运行 L1 分析...")
            engine.run_l1()
        ctx = engine.get_market_context()
        reports = engine.get_index_reports()
        analyzers = engine.state.analyzers
        scored = engine.get_scored_symbols()
        ctx_data = serialize_market_context(ctx) if ctx else None
        reports_data = [serialize_index_report(r) for r in reports] if reports else None
    else:
        analyzers = screener.analyzers
        reports = [az.report() for az in analyzers.values()]
        from signals.layers.market_context import build_market_context
        ctx = build_market_context(reports)
        scored = []
        ctx_data = serialize_market_context(ctx) if ctx else None
        reports_data = [serialize_index_report(r) for r in reports] if reports else None

    # ── 1. MarketContext ──
    if ctx_data:
        redis.set("signals:context", json.dumps(ctx_data, ensure_ascii=False))
        pushed_keys.append("signals:context")

    # ── 2. IndexReports ──
    if reports_data:
        redis.set("signals:reports", json.dumps(reports_data, ensure_ascii=False))
        pushed_keys.append("signals:reports")

    # ── 3. Status ──
    status = {
        "ready": bool(ctx_data),
        "running": False,
        "last_update": time.time(),
        "error": "",
        "index_count": len(analyzers) or len(reports_data or []),
        "signal_count": 0,
    }
    redis.set("signals:status", json.dumps(status, ensure_ascii=False))
    pushed_keys.append("signals:status")

    # ── 4. Chart Data ──
    chart_count = 0
    for name, idx_az in analyzers.items():
        chart_count += _push_chart_data(redis, name, idx_az)
    if chart_count:
        pushed_keys.append(f"signals:chart:*  ({chart_count} charts)")

    # ── 5. Screener Results ──
    if dry_run:
        data = scored  # already plain dicts or empty
    else:
        data = [serialize_scored_symbol(s) for s in scored]
    redis.set("signals:screener", json.dumps(data, ensure_ascii=False))
    pushed_keys.append("signals:screener")

    # ── 6. Meta ──
    meta = {
        "last_push_at": time.time(),
        "index_count": len(analyzers) or len(reports_data or []),
        "pushed_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": dry_run,
    }
    redis.set("signals:meta", json.dumps(meta, ensure_ascii=False))
    pushed_keys.append("signals:meta")

    print(f"\n  {'[dry-run] ' if dry_run else ''}Upstash 推送完成:")
    for k in pushed_keys:
        print(f"    {k}")
    if chart_count:
        print(f"  共 {chart_count} 张图表数据")

    if dry_run and isinstance(redis, _MockRedis):
        redis.summary()

    print()
    return pushed_keys


# 向后兼容
push = push_from_screener


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="推送分析结果到 Upstash Redis")
    parser.add_argument("--dry-run", action="store_true",
                        help="使用 mock 数据验证推送逻辑（不连网）")
    cli_args = parser.parse_args()
    push_from_screener(dry_run=cli_args.dry_run)
