# -*- coding: utf-8 -*-
"""
推送分析结果到 Upstash Redis，供 Vercel 前端读取。

用法：
  python run.py --mode index --push             # 分析完自动推送
  python -m signals.deploy.push_to_kv           # 独立运行（先跑分析再推送）

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


def push_from_screener(screener=None):
    """
    从 IndexScreener 推送结果到 Upstash Redis。

    :param screener: IndexScreener 实例。为 None 则通过 WebEngine 获取。
    """
    redis = _get_redis()
    pushed_keys = []

    # 如果没有传入 screener，尝试从 WebEngine 获取
    if screener is None:
        from signals.web.services.engine import get_engine
        engine = get_engine()
        if not engine.is_ready():
            print("  WebEngine 尚未初始化，运行 L1 分析...")
            engine.run_l1()
        # 从 engine 提取等效数据
        ctx = engine.get_market_context()
        reports = engine.get_index_reports()
        analyzers = engine.state.analyzers
        scored = engine.get_scored_symbols()
    else:
        # 从 IndexScreener 直接获取
        analyzers = screener.analyzers
        reports = [az.report() for az in analyzers.values()]
        # 重建 MarketContext
        from signals.layers.market_context import build_market_context
        ctx = build_market_context(reports)
        scored = []

    # ── 1. MarketContext ──
    if ctx:
        data = serialize_market_context(ctx)
        redis.set("signals:context", json.dumps(data, ensure_ascii=False))
        pushed_keys.append("signals:context")

    # ── 2. IndexReports ──
    if reports:
        data = [serialize_index_report(r) for r in reports]
        redis.set("signals:reports", json.dumps(data, ensure_ascii=False))
        pushed_keys.append("signals:reports")

    # ── 3. Status ──
    status = {
        "ready": bool(ctx),
        "running": False,
        "last_update": time.time(),
        "error": "",
        "index_count": len(analyzers),
        "signal_count": 0,
    }
    redis.set("signals:status", json.dumps(status, ensure_ascii=False))
    pushed_keys.append("signals:status")

    # ── 4. Chart Data ──
    chart_count = 0
    for name, idx_az in analyzers.items():
        chart_count += _push_chart_data(redis, name, idx_az)
    pushed_keys.append(f"signals:chart:*  ({chart_count} charts)")

    # ── 5. Screener Results ──
    data = [serialize_scored_symbol(s) for s in scored]
    redis.set("signals:screener", json.dumps(data, ensure_ascii=False))
    pushed_keys.append("signals:screener")

    # ── 6. Meta ──
    meta = {
        "last_push_at": time.time(),
        "index_count": len(analyzers),
        "pushed_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    redis.set("signals:meta", json.dumps(meta, ensure_ascii=False))
    pushed_keys.append("signals:meta")

    print(f"\n  Upstash 推送完成:")
    for k in pushed_keys:
        print(f"    {k}")
    print(f"  共 {chart_count} 张图表数据\n")

    return pushed_keys


# 向后兼容
push = push_from_screener


if __name__ == "__main__":
    push_from_screener()
