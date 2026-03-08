#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立测试服务器 — 用 mock 数据验证 Web UI 前端效果。
不依赖 signals/ 包的任何模块（czsc/akshare 等），可在任何环境运行。

用法: python test_web.py [--port 8000]
"""
import sys
import json
import time
import random
import math
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

STATIC_DIR = Path(__file__).parent / "signals" / "web" / "static"

app = FastAPI(title="隆小侠 LONG CLAW — Mock Server")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── Mock 数据 ─────────────────────────────────────────

def _mock_index_reports():
    """生成 11 只指数的 mock IndexReport 数据"""
    indices = [
        ("上证50",   "sh000016", 2850.32, "上涨趋势", "二买", "无", "三买"),
        ("沪深300",  "sh000300", 3425.67, "上涨趋势", "无", "趋势买", "二买"),
        ("创业板指", "sz399006", 2180.45, "中枢震荡", "无", "无", "三买"),
        ("科创50",   "sh000688", 1465.89, "上涨趋势", "二买", "二买", "二买"),
        ("超大盘",   "sh000043", 4520.11, "上涨趋势", "无", "无", "无"),
        ("中证500",  "sh000905", 5890.34, "中枢震荡", "无", "三买", "无"),
        ("中证1000", "sh000852", 6234.56, "下跌趋势", "无", "无", "背驰卖"),
        ("恒生科技", "HK.800700", 4850.78, "上涨趋势", "三买", "无", "趋势买"),
        ("标普500",  "US.SPY", 5890.12, "上涨趋势", "无", "二买", "无"),
        ("纳斯达克", "US.QQQ", 498.34, "上涨趋势", "趋势买", "无", "三买"),
        ("道琼斯",   "US.DIA", 42350.67, "中枢震荡", "无", "无", "无"),
    ]
    reports = []
    for name, symbol, price, trend, d_sig, f30_sig, f15_sig in indices:
        has_buy = any("买" in s for s in [d_sig, f30_sig, f15_sig])
        has_sell = any("卖" in s for s in [d_sig, f30_sig, f15_sig])
        is_bullish = trend == "上涨趋势"
        three_aligned = (trend == "上涨趋势"
                         and f30_sig != "无" and "买" in f30_sig
                         and f15_sig != "无" and "买" in f15_sig)
        reports.append({
            "name": name, "symbol": symbol, "data_available": True,
            "latest_price": price,
            "daily_last_dt": datetime.now().strftime("%Y-%m-%d"),
            "summary": f"{name} | {trend} | {'有信号' if has_buy else '无信号'}",
            "daily_trend": trend,
            "daily_last_direction": "向上" if is_bullish else "向下",
            "daily_latest_signal": d_sig,
            "daily_bi_count": random.randint(8, 25),
            "daily_zs": {"zd": price * 0.97, "zg": price * 1.01, "bi_count": 5} if random.random() > 0.3 else None,
            "f30_trend": random.choice(["上涨趋势", "中枢震荡"]),
            "f30_last_direction": "向上",
            "f30_latest_signal": f30_sig,
            "f30_bi_count": random.randint(5, 20),
            "f30_zs": None,
            "f15_trend": random.choice(["上涨趋势", "中枢震荡", "下跌趋势"]),
            "f15_last_direction": "向上" if random.random() > 0.4 else "向下",
            "f15_latest_signal": f15_sig,
            "f15_bi_count": random.randint(5, 15),
            "f15_zs": None,
            "has_buy_signal": has_buy,
            "has_sell_signal": has_sell,
            "is_bullish": is_bullish,
            "three_level_aligned": three_aligned,
        })
    return reports


def _mock_market_context():
    """生成 mock MarketContext"""
    return {
        "overall_direction": "偏多",
        "direction_strength": 0.65,
        "structural_divergence": "",
        "growth_vs_value": "成长",
        "recommended_style": "成长",
        "gate_industry_scan": True,
        "sentiment_phase": "修复",
        "divergence_score": -1.5,
        "position_suggestion": "维持进攻为主 + 关注超跌反弹方向",
        "rotation_stage": "科技领涨",
        "rotation_detail": "科技45% > 消费25% > 顺周期20%",
        "allocation_suggestion": "科技40% | 消费30% | 顺周期20% | 现金10%",
        "buy_indices": ["上证50", "沪深300", "科创50", "恒生科技", "标普500", "纳斯达克"],
        "sell_indices": ["中证1000"],
        "bullish_indices": ["上证50", "沪深300", "科创50", "恒生科技", "标普500", "纳斯达克"],
        "bearish_indices": ["中证1000"],
        "shield_sectors": ["银行", "电力"],
        "sword_sectors": ["半导体", "消费电子", "软件开发"],
        "summary": "大市偏多（强度 +0.65），成长风格占优。上涨趋势：上证50、沪深300等6只。情绪: 修复。",
    }


def _generate_ohlcv(base_price=3400, bars=200, freq="daily"):
    """生成模拟 K 线数据"""
    data = []
    price = base_price
    now = datetime.now()
    if freq == "daily":
        delta = timedelta(days=1)
    elif freq == "30min":
        delta = timedelta(minutes=30)
    else:
        delta = timedelta(minutes=15)

    start = now - delta * bars
    for i in range(bars):
        dt = start + delta * i
        # 跳过周末 (daily)
        if freq == "daily" and dt.weekday() >= 5:
            continue
        change = random.gauss(0, price * 0.012)
        o = price
        c = price + change
        h = max(o, c) + abs(random.gauss(0, price * 0.005))
        l = min(o, c) - abs(random.gauss(0, price * 0.005))
        vol = random.uniform(5e8, 2e9)
        data.append({
            "time": int(dt.timestamp()),
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": round(vol, 0),
        })
        price = c
    return data


def _generate_bi_list(ohlcv):
    """从 OHLCV 数据生成模拟笔线（每 8-15 根K线一笔）"""
    bis = []
    if len(ohlcv) < 20:
        return bis

    i = 0
    direction = "up"
    while i < len(ohlcv) - 10:
        length = random.randint(8, 15)
        end_i = min(i + length, len(ohlcv) - 1)
        segment = ohlcv[i:end_i + 1]

        if direction == "up":
            low_bar = min(segment, key=lambda x: x["low"])
            high_bar = max(segment, key=lambda x: x["high"])
            bis.append({
                "sdt": segment[0]["time"],
                "edt": segment[-1]["time"],
                "high": high_bar["high"],
                "low": low_bar["low"],
                "direction": "up",
                "power": round(high_bar["high"] - low_bar["low"], 2),
            })
        else:
            high_bar = max(segment, key=lambda x: x["high"])
            low_bar = min(segment, key=lambda x: x["low"])
            bis.append({
                "sdt": segment[0]["time"],
                "edt": segment[-1]["time"],
                "high": high_bar["high"],
                "low": low_bar["low"],
                "direction": "down",
                "power": round(high_bar["high"] - low_bar["low"], 2),
            })

        direction = "down" if direction == "up" else "up"
        i = end_i
    return bis


def _generate_signals(ohlcv, bi_list):
    """生成模拟买卖点信号"""
    signals = []
    signal_types = ["二买", "三买", "趋势买", "背驰买", "二卖", "三卖"]
    for bi in bi_list[2:]:  # 跳过前两笔
        if random.random() > 0.7:
            sig_type = random.choice(signal_types)
            signals.append({
                "dt": bi["edt"],
                "type": sig_type,
                "freq": "日线",
                "price": round((bi["high"] + bi["low"]) / 2, 2),
                "confidence": round(random.uniform(0.6, 0.95), 2),
                "details": f"模拟信号 — {sig_type}确认",
            })
    return signals


# ── API 路由 ──────────────────────────────────────────

@app.get("/api/index/context")
def api_index_context():
    return _mock_market_context()

@app.get("/api/index/reports")
def api_index_reports():
    return _mock_index_reports()

@app.get("/api/index/status")
def api_index_status():
    return {"ready": True, "running": False, "last_update": time.time(),
            "error": "", "index_count": 11, "signal_count": 0}

@app.get("/api/chart/{symbol}")
def api_chart(symbol: str, freq: str = Query("daily")):
    # 根据指数名称确定 base price
    price_map = {
        "上证50": 2850, "沪深300": 3425, "创业板指": 2180, "科创50": 1465,
        "超大盘": 4520, "中证500": 5890, "中证1000": 6234,
        "恒生科技": 4850, "标普500": 5890, "纳斯达克": 498, "道琼斯": 42350,
    }
    base = price_map.get(symbol, 3000)
    ohlcv = _generate_ohlcv(base_price=base, bars=200, freq=freq)
    bi_list = _generate_bi_list(ohlcv)
    signals = _generate_signals(ohlcv, bi_list)
    return {
        "ohlcv": ohlcv,
        "bi_list": bi_list,
        "fx_list": [],
        "zhongshu": [],
        "signals": signals,
        "meta": {"name": symbol, "symbol": symbol, "freq": freq},
    }

@app.get("/api/screener/results")
def api_screener():
    return []


# ── 静态文件 ──────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── 启动 ──────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn
    print(f"\n🐲 隆小侠 Web UI (Mock Server)")
    print(f"   🌐 http://localhost:{args.port}\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
