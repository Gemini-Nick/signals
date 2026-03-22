# -*- coding: utf-8 -*-
"""
信号回测验证 API — 可视化信号回测（MACD + 缠论 + 可扩展）

输入股票代码 + 频率 + 信号组，返回 K线 + MACD + MA + 信号标记 + 前瞻评估。
"""
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


# ─────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────

def _detect_market(code: str) -> str:
    """根据代码长度/格式判断市场: 'A' or 'HK'"""
    code = code.strip()
    if len(code) == 5:
        return "HK"
    return "A"


def _build_symbol(code: str, market: str) -> str:
    """构造 Futu 格式代码"""
    if market == "HK":
        return f"HK.{code}"
    if code.startswith(("6", "5")):
        return f"SH.{code}"
    return f"SZ.{code}"


def _kline_cache_path(code: str, freq: str) -> Path:
    """K线缓存文件路径"""
    cache_dir = Path(__file__).resolve().parent.parent.parent.parent / ".data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 统一 freq 命名: D/daily → daily, W/weekly → weekly
    freq_norm = "daily" if freq in ("D", "daily") else "weekly"
    return cache_dir / f"kline_{code}_{freq_norm}.json"


def _save_kline_cache(df: pd.DataFrame, code: str, freq: str):
    """保存K线到磁盘缓存"""
    import json
    try:
        path = _kline_cache_path(code, freq)
        records = []
        for dt_idx, row in df.iterrows():
            records.append({
                "dt": dt_idx.strftime("%Y-%m-%d"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "vol": int(row.get("vol", 0)),
            })
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        logger.info("K线缓存已保存: %s (%d 根)", path.name, len(records))
    except Exception as e:
        logger.warning("K线缓存保存失败: %s", e)


def _load_kline_cache(code: str, freq: str) -> pd.DataFrame:
    """从磁盘缓存加载K线"""
    import json
    path = _kline_cache_path(code, freq)
    logger.info("K线缓存路径: %s (exists=%s)", path, path.exists())
    if not path.exists():
        return pd.DataFrame()
    try:
        # 24h 过期
        import os
        age = datetime.now().timestamp() - os.path.getmtime(path)
        if age > 86400:
            logger.info("K线缓存已过期(%dh): %s", int(age / 3600), path.name)
            return pd.DataFrame()
        records = json.loads(path.read_text(encoding="utf-8"))
        df = pd.DataFrame(records)
        df["dt"] = pd.to_datetime(df["dt"])
        df = df.set_index("dt")
        logger.info("K线从磁盘缓存加载: %s (%d 根)", path.name, len(df))
        return df
    except Exception as e:
        logger.warning("K线缓存加载失败: %s", e)
        return pd.DataFrame()


def _fetch_kline(code: str, market: str, freq: str) -> pd.DataFrame:
    """
    拉取 K 线数据，降级链：
      东财 ak.stock_zh_a_hist → 新浪 ak.stock_zh_a_daily → MongoDB 历史数据
    返回带 datetime index 的 DataFrame。
    """
    import akshare as ak
    from signals.data.fetcher import _no_proxy
    from signals.data.mongo_fallback import get_kline_docs, save_kline

    days = 730 if freq == "daily" else 1460
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    edt = datetime.now().strftime("%Y%m%d")
    period = "daily" if freq == "daily" else "weekly"

    _NETWORK_ERRORS = (
        "RemoteDisconnected", "ConnectionReset", "Connection aborted",
        "ConnectionError", "timeout", "Max retries exceeded",
        "SSLError", "SSL",
    )

    df = None

    # ── 源 1: 东财 ──────────────────────────────────
    try:
        with _no_proxy():
            if market == "A":
                df = ak.stock_zh_a_hist(
                    symbol=code, period=period,
                    start_date=sdt, end_date=edt, adjust="qfq")
            else:
                df = ak.stock_hk_hist(
                    symbol=code, period=period,
                    start_date=sdt, end_date=edt, adjust="qfq")
        if df is not None and not df.empty:
            df = df.rename(columns={
                "日期": "dt", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "vol",
            })
            df["dt"] = pd.to_datetime(df["dt"])
            df = df.set_index("dt")
            _save_kline_cache(df, code, freq)
            save_kline("kline_cache", code, freq, _df_to_records(df))
            return df
    except Exception as e:
        err_msg = str(e)
        is_network = any(k in err_msg for k in _NETWORK_ERRORS)
        if is_network:
            logger.warning("东财K线失败: %s %s — %s", code, freq, err_msg[:80])
        else:
            logger.warning("东财K线异常: %s %s — %s", code, freq, err_msg[:80])

    # ── 源 2: 新浪（仅 A 股日线）────────────────────
    if market == "A" and freq == "daily":
        try:
            sina_code = f"sz{code}" if code.startswith(("0", "3")) else f"sh{code}"
            with _no_proxy():
                df2 = ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq")
            if df2 is not None and not df2.empty:
                cutoff = datetime.now() - timedelta(days=days)
                df2["date"] = pd.to_datetime(df2["date"])
                df2 = df2[df2["date"] >= cutoff]
                df2 = df2.rename(columns={
                    "date": "dt", "volume": "vol",
                })
                df2 = df2[["dt", "open", "high", "low", "close", "vol"]]
                df2 = df2.set_index("dt")
                logger.info("新浪K线成功: %s %s (%d 根)", code, freq, len(df2))
                _save_kline_cache(df2, code, freq)
                save_kline("kline_cache", code, freq, _df_to_records(df2))
                return df2
        except Exception as e:
            logger.warning("新浪K线失败: %s — %s", code, str(e)[:80])

    # ── 源 3: MongoDB 历史数据 ──────────────────────
    mongo_docs = get_kline_docs("kline_cache", code, freq)
    if mongo_docs:
        df3 = pd.DataFrame(mongo_docs)
        df3["dt"] = pd.to_datetime(df3["dt"])
        df3 = df3.set_index("dt")
        for col in ["open", "high", "low", "close", "vol"]:
            if col in df3.columns:
                df3[col] = pd.to_numeric(df3[col], errors="coerce")
        return df3

    raise ConnectionError(
        f"所有数据源均失败（东财/新浪/MongoDB），{code} 无可用数据。"
    )


def _df_to_records(df: pd.DataFrame) -> list:
    """DataFrame → K 线记录列表（用于存储）"""
    records = []
    for dt_idx, row in df.iterrows():
        records.append({
            "dt": dt_idx.strftime("%Y-%m-%d"),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": int(row.get("vol", 0)),
        })
    return records


def _dt_to_unix(dt) -> int:
    """datetime / Timestamp → unix seconds"""
    if hasattr(dt, "timestamp"):
        return int(dt.timestamp())
    return int(pd.Timestamp(dt).timestamp())


def _serialize_ohlcv(df: pd.DataFrame) -> list:
    """DataFrame → [{time, open, high, low, close, volume}]"""
    result = []
    for dt_idx, row in df.iterrows():
        result.append({
            "time": _dt_to_unix(dt_idx),
            "open": round(float(row["open"]), 4),
            "high": round(float(row["high"]), 4),
            "low": round(float(row["low"]), 4),
            "close": round(float(row["close"]), 4),
            "volume": int(row.get("vol", 0)),
        })
    return result


def _compute_macd_data(df: pd.DataFrame) -> list:
    """计算 MACD 指标，返回图表格式 [{time, dif, dea, bar}]"""
    closes = df["close"]
    if len(closes) < 26:
        return []
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    result = []
    for dt_idx in df.index:
        if pd.isna(dea[dt_idx]):
            continue
        result.append({
            "time": _dt_to_unix(dt_idx),
            "dif": round(float(dif[dt_idx]), 4),
            "dea": round(float(dea[dt_idx]), 4),
            "bar": round(float(hist[dt_idx]), 4),
        })
    return result


def _compute_ma_lines(df: pd.DataFrame) -> list:
    """计算 MA 均线，返回 [{label, color, data: [{time, value}]}]"""
    closes = df["close"]
    ma_lines = []
    for period, label, color in [
        (20, "MA20", "#e040fb"),
        (60, "MA60", "#26a69a"),
        (120, "MA120", "#ff6d00"),
        (250, "MA250", "#78909c"),
    ]:
        if len(closes) < period:
            continue
        ma_vals = closes.rolling(period).mean()
        line_data = []
        for dt_idx, val in ma_vals.dropna().items():
            line_data.append({
                "time": _dt_to_unix(dt_idx),
                "value": round(float(val), 4),
            })
        ma_lines.append({"label": label, "color": color, "data": line_data})
    return ma_lines


def _compute_forward_eval(df: pd.DataFrame, sig_idx: int) -> dict:
    """
    计算信号触发后的前瞻评估。

    所有信号都视为买入方向（MACD Pattern A/B 均为看多信号，
    缠论买信号看多、卖信号在此标注 direction 反转）。

    返回: {return_t5, return_t10, return_t20, mfe, mae, mfe_day, mae_day,
           direction_correct, data_sufficient}
    """
    signal_price = df.iloc[sig_idx]["close"]
    if signal_price <= 0:
        return {}

    remaining = len(df) - sig_idx - 1
    result = {
        "return_t5": None, "return_t10": None, "return_t20": None,
        "mfe": 0.0, "mae": 0.0, "mfe_day": 0, "mae_day": 0,
        "direction_correct": None, "data_sufficient": remaining >= 20,
    }

    mfe, mae = 0.0, 0.0
    for i in range(1, min(remaining + 1, 21)):
        bar = df.iloc[sig_idx + i]
        close_ret = (bar["close"] - signal_price) / signal_price * 100
        high_ret = (bar["high"] - signal_price) / signal_price * 100
        low_ret = (bar["low"] - signal_price) / signal_price * 100

        if i == 5:
            result["return_t5"] = round(close_ret, 2)
        elif i == 10:
            result["return_t10"] = round(close_ret, 2)
        elif i == 20:
            result["return_t20"] = round(close_ret, 2)

        if high_ret > mfe:
            mfe = high_ret
            result["mfe_day"] = i
        if low_ret < mae:
            mae = low_ret
            result["mae_day"] = i

    result["mfe"] = round(float(mfe), 2)
    result["mae"] = round(float(mae), 2)

    # 方向判定（基于 T+10 或 T+5）
    ref = result["return_t10"] if result["return_t10"] is not None else result["return_t5"]
    if ref is not None:
        if ref > 2.0:
            result["direction_correct"] = 1
        elif ref < -2.0:
            result["direction_correct"] = 0

    # 确保所有值是 Python 原生类型（避免 numpy.bool_ / numpy.float64 序列化失败）
    result["data_sufficient"] = bool(result["data_sufficient"])
    return result


def _compute_kpi(signal_evals: list) -> dict:
    """从信号+评估列表计算 KPI"""
    total = len(signal_evals)
    if total == 0:
        return {"total": 0}

    valid = [s for s in signal_evals if s.get("eval", {}).get("return_t10") is not None]
    wins = sum(1 for s in valid if (s.get("eval", {}).get("direction_correct") == 1))
    losses = sum(1 for s in valid if (s.get("eval", {}).get("direction_correct") == 0))
    valid_count = len(valid)

    returns_t10 = [s["eval"]["return_t10"] for s in valid if s["eval"]["return_t10"] is not None]
    mfes = [s["eval"]["mfe"] for s in valid if s["eval"].get("mfe") is not None]
    maes = [s["eval"]["mae"] for s in valid if s["eval"].get("mae") is not None]

    win_rate = round(wins / valid_count * 100, 1) if valid_count else 0
    avg_return = round(sum(returns_t10) / len(returns_t10), 2) if returns_t10 else 0
    avg_mfe = round(sum(mfes) / len(mfes), 2) if mfes else 0
    avg_mae = round(sum(maes) / len(maes), 2) if maes else 0

    # 期望 = avg_win * WR - avg_loss * LR
    pos = [r for r in returns_t10 if r > 0]
    neg = [r for r in returns_t10 if r < 0]
    avg_win = sum(pos) / len(pos) if pos else 0
    avg_loss = abs(sum(neg) / len(neg)) if neg else 0
    wr = wins / valid_count if valid_count else 0
    lr = losses / valid_count if valid_count else 0
    expectancy = round(avg_win * wr - avg_loss * lr, 2)

    # 按信号类型分组
    by_type = {}
    for s in valid:
        sig_type = s.get("type", "")
        if sig_type not in by_type:
            by_type[sig_type] = {"count": 0, "wins": 0, "returns": []}
        by_type[sig_type]["count"] += 1
        if s["eval"].get("direction_correct") == 1:
            by_type[sig_type]["wins"] += 1
        by_type[sig_type]["returns"].append(s["eval"]["return_t10"])

    by_type_kpi = {}
    for sig_type, info in by_type.items():
        cnt = info["count"]
        wr_type = round(info["wins"] / cnt * 100, 1) if cnt else 0
        avg_r = round(sum(info["returns"]) / cnt, 2) if cnt else 0
        by_type_kpi[sig_type] = {"count": cnt, "win_rate": wr_type, "avg_return_t10": avg_r}

    return {
        "total": total,
        "evaluated": valid_count,
        "win_rate": win_rate,
        "avg_return_t10": avg_return,
        "expectancy": expectancy,
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "by_type": by_type_kpi,
    }


def _get_date_presets() -> list:
    """将 config.DATE_PRESETS 转为前端格式"""
    presets = []
    for key, info in config.DATE_PRESETS.items():
        if "date" not in info:
            continue
        date_str = info["date"]
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            presets.append({
                "key": key,
                "date": date_str,
                "time": int(dt.timestamp()),
                "label": info["label"],
            })
        except ValueError:
            pass
    return sorted(presets, key=lambda x: x["time"])


# ─────────────────────────────────────────────────────
# MACD 信号检测
# ─────────────────────────────────────────────────────

def _detect_macd(df: pd.DataFrame, symbol: str, freq_label: str, lookback: int) -> list:
    """运行 MACD 信号检测，返回信号列表"""
    from signals.core.macd_detector import detect_macd_signals

    signals = detect_macd_signals(df, symbol, freq_label, lookback=lookback)
    result = []
    for sig in signals:
        sig_idx = df.index.get_loc(sig.dt)
        eval_data = _compute_forward_eval(df, sig_idx)

        result.append({
            "dt": _dt_to_unix(sig.dt),
            "date_str": sig.dt.strftime("%Y-%m-%d"),
            "type": sig.pattern,
            "group": "macd",
            "price": round(sig.price, 4),
            "confidence": sig.confidence,
            "details": sig.details,
            "eval": eval_data,
        })
    return result


# ─────────────────────────────────────────────────────
# 缠论信号检测
# ─────────────────────────────────────────────────────

def _detect_czsc(df: pd.DataFrame, symbol: str, freq_label: str) -> tuple:
    """
    运行缠论信号检测，返回 (signals_list, bi_list, zhongshu_list)。
    从 DataFrame 构建 CZSC 对象，然后调用 detect_all_signals。
    """
    from czsc import CZSC, RawBar, Freq
    from signals.core.detectors import detect_all_signals

    freq_map = {"日线": Freq.D, "周线": Freq.W}
    freq_enum = freq_map.get(freq_label, Freq.D)

    # DataFrame → RawBar list
    bars = []
    for i, (dt_idx, row) in enumerate(df.iterrows()):
        bars.append(RawBar(
            symbol=symbol, dt=dt_idx, id=i, freq=freq_enum,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            vol=int(row.get("vol", 0)), amount=0,
        ))

    if len(bars) < 35:
        return [], [], []

    czsc_obj = CZSC(bars, max_bi_num=200)
    events = detect_all_signals(czsc_obj, symbol)

    # 过滤掉 MACD 类信号（避免与 macd group 重复）
    events = [e for e in events if "MACD" not in e.signal_type]

    # 转换信号
    signals = []
    for ev in events:
        # 找到对应的 df index (pandas 3.0+ 不支持 method="nearest")
        try:
            sig_idx = df.index.get_indexer([ev.dt], method="nearest")[0]
            if sig_idx < 0:
                continue
        except Exception:
            continue

        eval_data = _compute_forward_eval(df, sig_idx)

        signals.append({
            "dt": _dt_to_unix(ev.dt),
            "date_str": ev.dt.strftime("%Y-%m-%d") if hasattr(ev.dt, "strftime") else str(ev.dt)[:10],
            "type": ev.signal_type,
            "group": "czsc",
            "price": round(ev.price, 4),
            "confidence": ev.confidence,
            "details": ev.details,
            "eval": eval_data,
        })

    # 序列化笔线
    bi_list = []
    for bi in czsc_obj.bi_list:
        bi_list.append({
            "sdt": _dt_to_unix(bi.fx_a.dt),
            "edt": _dt_to_unix(bi.fx_b.dt),
            "high": round(bi.high, 4),
            "low": round(bi.low, 4),
            "direction": "up" if bi.direction.value == "向上" else "down",
            "power": round(bi.power_price, 4) if hasattr(bi, "power_price") else 0,
        })

    # 序列化中枢（从笔中提取）
    zhongshu = _extract_zhongshu(czsc_obj)

    return signals, bi_list, zhongshu


def _extract_zhongshu(czsc_obj) -> list:
    """从 CZSC 对象提取中枢"""
    bis = czsc_obj.bi_list
    if len(bis) < 3:
        return []

    result = []
    i = 0
    while i < len(bis) - 2:
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]
        zg = min(b1.high, b3.high)
        zd = max(b1.low, b3.low)
        if zg > zd:
            # 找中枢的延伸
            end_idx = i + 2
            for j in range(i + 3, len(bis)):
                if bis[j].high >= zd and bis[j].low <= zg:
                    end_idx = j
                else:
                    break
            result.append({
                "zd": round(zd, 4),
                "zg": round(zg, 4),
                "start_dt": _dt_to_unix(bis[i].fx_a.dt),
                "end_dt": _dt_to_unix(bis[end_idx].fx_b.dt),
                "bi_count": end_idx - i + 1,
            })
            i = end_idx + 1
        else:
            i += 1
    return result


# ─────────────────────────────────────────────────────
# API 端点
# ─────────────────────────────────────────────────────

def _detect_entry_factors(
    df: pd.DataFrame,
    factor: str,
    lookback: int = 999,
    **kwargs,
) -> list:
    """运行入场因子检测"""
    from signals.core.entry_factors import (
        detect_gap_entries,
        detect_trend_breakout_entries,
        detect_volatility_contraction_entries,
    )

    signals = []

    if factor in ("gap", "all_factors"):
        gap_sigs = detect_gap_entries(
            df,
            gap_pct_min=float(kwargs.get("gap_pct_min", 2.0)),
            volume_ratio_min=float(kwargs.get("volume_ratio_min", 1.5)),
            lookback=lookback,
        )
        # 添加前瞻评估
        for sig in gap_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(gap_sigs)

    if factor in ("trend_breakout", "all_factors"):
        trend_sigs = detect_trend_breakout_entries(
            df,
            lookback_days=int(kwargs.get("trend_lookback", 20)),
            volume_ratio_min=float(kwargs.get("volume_ratio_min", 1.3)),
            lookback=lookback,
        )
        for sig in trend_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(trend_sigs)

    if factor in ("vol_contraction", "all_factors"):
        vol_sigs = detect_volatility_contraction_entries(
            df,
            bb_period=int(kwargs.get("bb_period", 20)),
            squeeze_threshold=float(kwargs.get("squeeze_threshold", 0.05)),
            lookback=lookback,
        )
        for sig in vol_sigs:
            try:
                sig_idx = df.index.get_indexer([pd.Timestamp(sig["date_str"])], method="nearest")[0]
                sig["eval"] = _compute_forward_eval(df, sig_idx)
            except Exception:
                sig["eval"] = {}
        signals.extend(vol_sigs)

    return signals


def _detect_all_signals(df, symbol, freq_label, signal_group, lookback, factor,
                        gap_pct_min, volume_ratio_min, trend_lookback, bb_period, squeeze_threshold):
    """统一信号检测 — 返回 (signals, bi_list, zhongshu, warnings)"""
    all_signals = []
    bi_list = []
    zhongshu = []
    warnings = []

    if signal_group in ("macd", "all"):
        macd_lookback = min(lookback, len(df) - 35)
        macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback)
        all_signals.extend(macd_sigs)

    if signal_group in ("czsc", "all"):
        try:
            czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label)
            all_signals.extend(czsc_sigs)
        except Exception as czsc_err:
            logger.warning("缠论信号检测失败: %s", czsc_err)
            warnings.append(f"缠论信号检测失败: {str(czsc_err)[:80]}")

    effective_factor = factor if factor else ("all_factors" if signal_group == "all" else "")
    if effective_factor:
        try:
            factor_sigs = _detect_entry_factors(
                df, effective_factor, lookback,
                gap_pct_min=gap_pct_min, volume_ratio_min=volume_ratio_min,
                trend_lookback=trend_lookback, bb_period=bb_period,
                squeeze_threshold=squeeze_threshold,
            )
            all_signals.extend(factor_sigs)
        except Exception as factor_err:
            logger.warning("入场因子检测失败: %s", factor_err)
            warnings.append(f"入场因子检测失败: {str(factor_err)[:80]}")

    all_signals.sort(key=lambda s: s["dt"])
    return all_signals, bi_list, zhongshu, warnings


@router.get("/analyze")
async def backtest_analyze(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    # 入场因子
    factor: str = Query("", description="入场因子: gap / trend_breakout / vol_contraction"),
    gap_pct_min: float = Query(2.0), volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20), bb_period: int = Query(20), squeeze_threshold: float = Query(0.05),
    # 基础风控
    stop_loss: float = Query(5.0), trail_stop: float = Query(50.0),
    max_hold: int = Query(20), slippage: float = Query(0.1),
    # 高级出场
    take_profit: float = Query(0), ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    batch_exit: str = Query("0"), batch1_ratio: float = Query(50),
    batch1_target: float = Query(5), batch2_target: float = Query(10),
):
    """
    一体化分析 — 信号检测 + 交易模拟，一次请求返回全部数据。
    """
    from signals.core.trade_simulator import SimConfig, simulate_trades
    import dataclasses

    try:
        code = code.strip()
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = "日线" if freq == "daily" else "周线"

        # 1. 拉取K线
        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={
                "error": f"无法获取 {code} 的{freq_label}数据"
            })

        # 2. 信号检测
        all_signals, bi_list, zhongshu, warnings = _detect_all_signals(
            df, symbol, freq_label, signal_group, lookback, factor,
            gap_pct_min, volume_ratio_min, trend_lookback, bb_period, squeeze_threshold,
        )

        # 3. 序列化图表数据
        ohlcv = _serialize_ohlcv(df)
        macd_data = _compute_macd_data(df)
        ma_lines = _compute_ma_lines(df)
        kpi = _compute_kpi(all_signals)

        # 4. 交易模拟
        sim_kwargs = dict(
            stop_loss_pct=stop_loss, trail_stop_pct=trail_stop,
            max_hold_days=max_hold, slippage=slippage / 100.0,
        )
        if take_profit > 0: sim_kwargs["take_profit_pct"] = take_profit
        if ma_exit_period > 0: sim_kwargs["ma_exit_period"] = ma_exit_period
        if profit_drawdown > 0: sim_kwargs["profit_drawdown_pct"] = profit_drawdown
        if batch_exit == "1":
            sim_kwargs["batch_exit_enabled"] = True
            sim_kwargs["batch_exit_ratios"] = [batch1_ratio / 100.0, (100 - batch1_ratio) / 100.0]
            sim_kwargs["batch_exit_targets"] = [batch1_target, batch2_target]

        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim = simulate_trades(df, all_signals, SimConfig(**sim_kwargs))

        result = {
            "symbol": symbol, "code": code, "freq": freq_label,
            "ohlcv": ohlcv, "macd": macd_data, "ma_lines": ma_lines,
            "signals": all_signals, "kpi": kpi,
            "date_presets": _get_date_presets(),
            "bi_list": bi_list, "zhongshu": zhongshu,
            "sim_trades": sim.trades, "sim_equity": sim.equity_curve,
            "sim_kpi": sim.kpi, "sim_config": sim.config,
            "sim_skip_reasons": sim.skip_reasons,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    except Exception as e:
        logger.exception("分析失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={"error": str(e), "detail": traceback.format_exc()})


@router.get("/scan")
async def backtest_scan(
    code: str = Query(...), freq: str = Query("daily"),
    signal_group: str = Query("all"), lookback: int = Query(999),
    factor: str = Query(""), gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5), trend_lookback: int = Query(20),
    bb_period: int = Query(20), squeeze_threshold: float = Query(0.05),
    stop_loss: float = Query(5.0), trail_stop: float = Query(50.0),
    max_hold: int = Query(20), slippage: float = Query(0.1),
    take_profit: float = Query(0), ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    scan_param: str = Query(""), scan_values: str = Query(""),
    scan_param2: str = Query(""), scan_values2: str = Query(""),
    scan_metric: str = Query("sharpe"),
):
    """独立参数扫描端点 — 避免阻塞主分析请求。"""
    from signals.core.trade_simulator import SimConfig, run_parameter_scan
    import dataclasses

    try:
        code = code.strip()
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = "日线" if freq == "daily" else "周线"

        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={"error": f"无法获取 {code} 的{freq_label}数据"})

        all_signals, _, _, _ = _detect_all_signals(
            df, symbol, freq_label, signal_group, lookback, factor,
            gap_pct_min, volume_ratio_min, trend_lookback, bb_period, squeeze_threshold,
        )

        sim_kwargs = dict(
            stop_loss_pct=stop_loss, trail_stop_pct=trail_stop,
            max_hold_days=max_hold, slippage=slippage / 100.0,
        )
        if take_profit > 0: sim_kwargs["take_profit_pct"] = take_profit
        if ma_exit_period > 0: sim_kwargs["ma_exit_period"] = ma_exit_period
        if profit_drawdown > 0: sim_kwargs["profit_drawdown_pct"] = profit_drawdown

        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim_config = SimConfig(**sim_kwargs)

        if not scan_param or not scan_values:
            return {"error": "请指定扫描参数和取值"}

        values1 = [float(v.strip()) for v in scan_values.split(",") if v.strip()]
        values2 = None
        p2_name = None
        if scan_param2 and scan_values2:
            values2 = [float(v.strip()) for v in scan_values2.split(",") if v.strip()]
            p2_name = scan_param2

        scan_result = run_parameter_scan(
            df, all_signals, sim_config,
            param1_name=scan_param, param1_values=values1,
            param2_name=p2_name, param2_values=values2,
            metric=scan_metric,
        )
        return scan_result

    except Exception as e:
        logger.exception("参数扫描失败: code=%s", code)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/run")
async def backtest_run(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    # Phase 2: 入场因子
    factor: str = Query("", description="入场因子: gap / trend_breakout / vol_contraction"),
    gap_pct_min: float = Query(2.0, description="跳空幅度阈值%"),
    volume_ratio_min: float = Query(1.5, description="量比阈值"),
    trend_lookback: int = Query(20, description="趋势突破回看天数"),
    bb_period: int = Query(20, description="布林带周期"),
    squeeze_threshold: float = Query(0.05, description="挤压阈值"),
):
    """
    通用信号回测 — 支持 MACD / 缠论 / 入场因子。
    """
    try:
        code = code.strip()
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = "日线" if freq == "daily" else "周线"

        # 1. 拉取K线
        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={
                "error": f"无法获取 {code} 的{freq_label}数据"
            })

        # 2. 信号检测
        all_signals = []
        bi_list = []
        zhongshu = []
        warnings = []

        if signal_group in ("macd", "all"):
            macd_lookback = min(lookback, len(df) - 35)
            macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback)
            all_signals.extend(macd_sigs)

        if signal_group in ("czsc", "all"):
            try:
                czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label)
                all_signals.extend(czsc_sigs)
            except Exception as czsc_err:
                logger.warning("缠论信号检测失败: %s", czsc_err)
                warnings.append(f"缠论信号检测失败: {str(czsc_err)[:80]}")

        # Phase 2: 入场因子（选了具体因子则只跑该因子，"全部信号"时跑所有因子）
        effective_factor = factor if factor else ("all_factors" if signal_group == "all" else "")
        if effective_factor:
            try:
                factor_sigs = _detect_entry_factors(
                    df, effective_factor, lookback,
                    gap_pct_min=gap_pct_min,
                    volume_ratio_min=volume_ratio_min,
                    trend_lookback=trend_lookback,
                    bb_period=bb_period,
                    squeeze_threshold=squeeze_threshold,
                )
                all_signals.extend(factor_sigs)
            except Exception as factor_err:
                logger.warning("入场因子检测失败: %s", factor_err)
                warnings.append(f"入场因子检测失败: {str(factor_err)[:80]}")

        # 3. 按时间排序
        all_signals.sort(key=lambda s: s["dt"])

        # 4. 序列化图表数据
        ohlcv = _serialize_ohlcv(df)
        macd_data = _compute_macd_data(df)
        ma_lines = _compute_ma_lines(df)

        # 5. KPI
        kpi = _compute_kpi(all_signals)

        # 6. 日期预设
        date_presets = _get_date_presets()

        result = {
            "symbol": symbol,
            "code": code,
            "freq": freq_label,
            "ohlcv": ohlcv,
            "macd": macd_data,
            "ma_lines": ma_lines,
            "signals": all_signals,
            "kpi": kpi,
            "date_presets": date_presets,
            "bi_list": bi_list,
            "zhongshu": zhongshu,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    except Exception as e:
        logger.exception("回测失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "detail": traceback.format_exc(),
        })


@router.get("/simulate")
async def backtest_simulate(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    # 基础风控
    stop_loss: float = Query(5.0, description="止损百分比"),
    trail_stop: float = Query(50.0, description="移动止盈回撤百分比"),
    max_hold: int = Query(20, description="最大持仓天数"),
    slippage: float = Query(0.1, description="滑点百分比"),
    # Phase 2: 入场因子
    factor: str = Query("", description="入场因子"),
    gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20),
    bb_period: int = Query(20),
    squeeze_threshold: float = Query(0.05),
    # Phase 3: 高级出场
    take_profit: float = Query(0, description="固定止盈%"),
    ma_exit_period: int = Query(0, description="均线离场周期"),
    profit_drawdown: float = Query(0, description="利润回撤%"),
    batch_exit: str = Query("0", description="分批出场 0/1"),
    batch1_ratio: float = Query(50, description="第1批仓位%"),
    batch1_target: float = Query(5, description="第1批目标%"),
    batch2_target: float = Query(10, description="第2批目标%"),
    # Phase 4: 参数扫描 (1D/2D)
    scan_param: str = Query("", description="扫描维度1"),
    scan_values: str = Query("", description="维度1取值"),
    scan_param2: str = Query("", description="扫描维度2"),
    scan_values2: str = Query("", description="维度2取值"),
    scan_metric: str = Query("sharpe", description="优化目标"),
):
    """
    交易模拟回测 — 支持高级出场 + 分批出场 + 2D参数扫描。
    """
    from signals.core.trade_simulator import SimConfig, simulate_trades, run_parameter_scan

    try:
        code = code.strip()
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = "日线" if freq == "daily" else "周线"

        # 1. 拉取K线
        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={
                "error": f"无法获取 {code} 的{freq_label}数据"
            })

        # 2. 信号检测
        all_signals = []
        bi_list = []
        zhongshu = []
        warnings = []

        if signal_group in ("macd", "all"):
            macd_lookback = min(lookback, len(df) - 35)
            macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback)
            all_signals.extend(macd_sigs)

        if signal_group in ("czsc", "all"):
            try:
                czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label)
                all_signals.extend(czsc_sigs)
            except Exception as czsc_err:
                logger.warning("缠论信号检测失败: %s", czsc_err)
                warnings.append(f"缠论信号检测失败: {str(czsc_err)[:80]}")

        # Phase 2: 入场因子（选了具体因子则只跑该因子，"全部信号"时跑所有因子）
        effective_factor = factor if factor else ("all_factors" if signal_group == "all" else "")
        if effective_factor:
            try:
                factor_sigs = _detect_entry_factors(
                    df, effective_factor, lookback,
                    gap_pct_min=gap_pct_min,
                    volume_ratio_min=volume_ratio_min,
                    trend_lookback=trend_lookback,
                    bb_period=bb_period,
                    squeeze_threshold=squeeze_threshold,
                )
                all_signals.extend(factor_sigs)
            except Exception as factor_err:
                logger.warning("入场因子检测失败: %s", factor_err)
                warnings.append(f"入场因子检测失败: {str(factor_err)[:80]}")

        all_signals.sort(key=lambda s: s["dt"])

        # 3. 构建模拟配置
        sim_kwargs = dict(
            stop_loss_pct=stop_loss,
            trail_stop_pct=trail_stop,
            max_hold_days=max_hold,
            slippage=slippage / 100.0,
        )
        # Phase 3: 高级出场字段 (仅当 SimConfig 支持时)
        if take_profit > 0:
            sim_kwargs["take_profit_pct"] = take_profit
        if ma_exit_period > 0:
            sim_kwargs["ma_exit_period"] = ma_exit_period
        if profit_drawdown > 0:
            sim_kwargs["profit_drawdown_pct"] = profit_drawdown
        if batch_exit == "1":
            sim_kwargs["batch_exit_enabled"] = True
            sim_kwargs["batch_exit_ratios"] = [batch1_ratio / 100.0, (100 - batch1_ratio) / 100.0]
            sim_kwargs["batch_exit_targets"] = [batch1_target, batch2_target]

        # 过滤掉 SimConfig 不支持的字段
        import dataclasses
        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim_config = SimConfig(**sim_kwargs)

        # 4. 参数扫描
        scan_result = None
        if scan_param and scan_values:
            try:
                values1 = [float(v.strip()) for v in scan_values.split(",") if v.strip()]
                values2 = None
                p2_name = None
                if scan_param2 and scan_values2:
                    values2 = [float(v.strip()) for v in scan_values2.split(",") if v.strip()]
                    p2_name = scan_param2

                if values1:
                    scan_result = run_parameter_scan(
                        df, all_signals, sim_config,
                        param1_name=scan_param,
                        param1_values=values1,
                        param2_name=p2_name,
                        param2_values=values2,
                        metric=scan_metric,
                    )
            except Exception as e:
                logger.warning("参数扫描失败: %s", e)
                scan_result = {"error": str(e)}

        # 5. 单次模拟
        sim = simulate_trades(df, all_signals, sim_config)

        # 6. 序列化图表数据
        ohlcv = _serialize_ohlcv(df)
        macd_data = _compute_macd_data(df)
        ma_lines = _compute_ma_lines(df)

        # 7. 原始 KPI
        forward_kpi = _compute_kpi(all_signals)

        result = {
            "symbol": symbol,
            "code": code,
            "freq": freq_label,
            "ohlcv": ohlcv,
            "macd": macd_data,
            "ma_lines": ma_lines,
            "signals": all_signals,
            "bi_list": bi_list,
            "zhongshu": zhongshu,
            "forward_kpi": forward_kpi,
            "sim_trades": sim.trades,
            "sim_equity": sim.equity_curve,
            "sim_kpi": sim.kpi,
            "sim_config": sim.config,
            "sim_skip_reasons": sim.skip_reasons,
            "date_presets": _get_date_presets(),
        }

        if scan_result:
            result["scan"] = scan_result
        if warnings:
            result["warnings"] = warnings

        return result

    except Exception as e:
        logger.exception("模拟回测失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "detail": traceback.format_exc(),
        })


@router.get("/export")
async def backtest_export(
    code: str = Query(...),
    freq: str = Query("daily"),
    signal_group: str = Query("all"),
    lookback: int = Query(999),
    stop_loss: float = Query(5.0),
    trail_stop: float = Query(50.0),
    max_hold: int = Query(20),
    slippage: float = Query(0.1),
    factor: str = Query(""),
    gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20),
    bb_period: int = Query(20),
    squeeze_threshold: float = Query(0.05),
    take_profit: float = Query(0),
    ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    batch_exit: str = Query("0"),
    batch1_ratio: float = Query(50),
    batch1_target: float = Query(5),
    batch2_target: float = Query(10),
):
    """Phase 5: CSV 导出"""
    from signals.core.trade_simulator import SimConfig, simulate_trades
    from fastapi.responses import StreamingResponse
    import io
    import csv

    try:
        code = code.strip()
        market = _detect_market(code)
        symbol = _build_symbol(code, market)
        freq_label = "日线" if freq == "daily" else "周线"

        df = _fetch_kline(code, market, freq)
        if df.empty:
            return JSONResponse(status_code=404, content={"error": f"无数据: {code}"})

        # 信号检测 (同 /simulate)
        all_signals = []
        if signal_group in ("macd", "all"):
            all_signals.extend(_detect_macd(df, symbol, freq_label, min(lookback, len(df) - 35)))
        if signal_group in ("czsc", "all"):
            try:
                czsc_sigs, _, _ = _detect_czsc(df, symbol, freq_label)
                all_signals.extend(czsc_sigs)
            except Exception as czsc_err:
                logger.warning("导出: 缠论信号检测失败: %s", czsc_err)
        if factor:
            all_signals.extend(_detect_entry_factors(df, factor, lookback,
                gap_pct_min=gap_pct_min, volume_ratio_min=volume_ratio_min,
                trend_lookback=trend_lookback, bb_period=bb_period,
                squeeze_threshold=squeeze_threshold))
        all_signals.sort(key=lambda s: s["dt"])

        # 模拟
        import dataclasses
        sim_kwargs = dict(
            stop_loss_pct=stop_loss, trail_stop_pct=trail_stop,
            max_hold_days=max_hold, slippage=slippage / 100.0,
        )
        if take_profit > 0:
            sim_kwargs["take_profit_pct"] = take_profit
        valid_fields = {f.name for f in dataclasses.fields(SimConfig)}
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in valid_fields}
        sim = simulate_trades(df, all_signals, SimConfig(**sim_kwargs))

        # 构建 CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "信号日", "类型", "组", "入场日", "入场价", "成交方式",
            "出场日", "出场价", "出场原因", "持仓天",
            "毛利%", "净利%", "成本%", "MFE%", "MAE%", "跳过原因",
        ])
        for t in sim.trades:
            writer.writerow([
                t.get("signal_date", ""), t.get("signal_type", ""), t.get("signal_group", ""),
                t.get("entry_date", ""), t.get("entry_price", ""), t.get("fill_type", ""),
                t.get("exit_date", ""), t.get("exit_price", ""), t.get("exit_reason", ""),
                t.get("holding_days", ""),
                t.get("return_pct", ""), t.get("net_return_pct", ""), t.get("cost_pct", ""),
                t.get("mfe_pct", ""), t.get("mae_pct", ""), t.get("skip_reason", ""),
            ])

        output.seek(0)
        filename = f"backtest_{code}_{datetime.now().strftime('%Y%m%d')}.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        logger.exception("导出失败: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/presets")
async def backtest_presets():
    """返回日期预设列表"""
    return _get_date_presets()
