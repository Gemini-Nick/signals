# -*- coding: utf-8 -*-
"""
信号回测验证 API — 可视化信号回测（MACD + 缠论 + 可扩展）

输入股票代码 + 频率 + 信号组，返回 K线 + MACD + MA + 信号标记 + 前瞻评估。
"""
import logging
import traceback
from datetime import datetime, timedelta

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


def _fetch_kline(code: str, market: str, freq: str) -> pd.DataFrame:
    """通过 akshare 拉取 K 线，返回带 datetime index 的 DataFrame（含重试）"""
    import time
    import akshare as ak
    from signals.data.fetcher import _no_proxy

    days = 730 if freq == "daily" else 1460
    sdt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    edt = datetime.now().strftime("%Y%m%d")
    period = "daily" if freq == "daily" else "weekly"

    max_retries = 3
    for attempt in range(max_retries):
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
            break
        except (ConnectionError, Exception) as e:
            err_msg = str(e)
            if attempt < max_retries - 1 and (
                "RemoteDisconnected" in err_msg
                or "ConnectionReset" in err_msg
                or "Connection aborted" in err_msg
            ):
                wait = (attempt + 1) * 2
                logger.warning("回测K线: %s %s 第%d次重试 (等待%ds): %s",
                               code, freq, attempt + 1, wait, err_msg)
                time.sleep(wait)
                continue
            raise

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.rename(columns={
        "日期": "dt", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "vol",
    })
    df["dt"] = pd.to_datetime(df["dt"])
    return df.set_index("dt")


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

    result["mfe"] = round(mfe, 2)
    result["mae"] = round(mae, 2)

    # 方向判定（基于 T+10 或 T+5）
    ref = result["return_t10"] if result["return_t10"] is not None else result["return_t5"]
    if ref is not None:
        if ref > 2.0:
            result["direction_correct"] = 1
        elif ref < -2.0:
            result["direction_correct"] = 0

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
        # 找到对应的 df index
        try:
            sig_idx = df.index.get_loc(ev.dt, method="nearest")
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

@router.get("/run")
async def backtest_run(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
):
    """
    通用信号回测 — 输入代码+频率+信号组，返回 K线+信号+前瞻评估。

    支持 MACD 信号、缠论信号或全部信号。
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

        if signal_group in ("macd", "all"):
            macd_lookback = min(lookback, len(df) - 35)
            macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback)
            all_signals.extend(macd_sigs)

        if signal_group in ("czsc", "all"):
            czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label)
            all_signals.extend(czsc_sigs)

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

        return {
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

    except Exception as e:
        logger.exception("回测失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "detail": traceback.format_exc(),
        })


@router.get("/presets")
async def backtest_presets():
    """返回日期预设列表"""
    return _get_date_presets()
