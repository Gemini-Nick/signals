# -*- coding: utf-8 -*-
"""
信号回测验证 API — 可视化信号回测（MACD + 缠论 + 可扩展）

输入股票代码 + 频率 + 信号组，返回 K线 + MACD + MA + 信号标记 + 前瞻评估。
"""
import logging
import traceback
from datetime import datetime, timedelta
from io import BytesIO

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

import config
from signals.core.backtest_history import BacktestHistoryStore, SCHEMA_VERSION
from signals.core.market_time import market_now, to_unix_seconds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


def _history_store() -> BacktestHistoryStore:
    return BacktestHistoryStore()


@router.get("/history")
async def backtest_history(limit: int = Query(50, ge=1, le=200)):
    return {
        "schema_version": SCHEMA_VERSION,
        "items": _history_store().list(limit=limit),
        "limit": limit,
    }


@router.get("/history/{history_id}")
async def backtest_history_item(history_id: str):
    try:
        item = _history_store().get(history_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="backtest history entry not found")
    return item


@router.post("/history")
async def save_backtest_history(request: Request):
    try:
        payload = await request.json()
        return _history_store().save(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/history/{history_id}")
async def delete_backtest_history(history_id: str):
    try:
        item = _history_store().delete(history_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "id": item.get("id"), "deletedAt": item.get("deletedAt")}


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


def _fetch_kline_legacy(code: str, market: str, freq: str) -> pd.DataFrame:
    """通过 akshare 拉取 K 线，返回带 datetime index 的 DataFrame"""
    import akshare as ak
    from signals.data.fetcher import _no_proxy

    days = 365 * 5
    now = market_now(market, symbol=_build_symbol(code, market))
    sdt = (now - timedelta(days=days)).strftime("%Y%m%d")
    edt = now.strftime("%Y%m%d")
    period = "daily" if freq == "daily" else "weekly"

    with _no_proxy():
        if market == "A":
            df = ak.stock_zh_a_hist(
                symbol=code, period=period,
                start_date=sdt, end_date=edt, adjust="qfq")
        else:
            df = ak.stock_hk_hist(
                symbol=code, period=period,
                start_date=sdt, end_date=edt, adjust="qfq")

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.rename(columns={
        "日期": "dt", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "vol",
    })
    df["dt"] = pd.to_datetime(df["dt"])
    return df.set_index("dt")


def _fetch_kline(code: str, market: str, freq: str) -> pd.DataFrame:
    """Fetch web backtest K-lines through the gateway as historical data."""
    from signals.data.gateway import get_kline
    from signals.data.models import DataRequest

    response = get_kline(
        DataRequest(
            domain="kline",
            mode="historical",
            market=market,
            freq=freq,
            symbol=code,
            purpose="backtest",
        ),
        legacy_fetcher=lambda: _fetch_kline_legacy(code, market, freq),
    )
    return response.data if response.data is not None else pd.DataFrame()


def _dt_to_unix(dt, *, market: str = "", symbol: str = "") -> int:
    """datetime / Timestamp → unix seconds"""
    return to_unix_seconds(dt, market=market, symbol=symbol)


def _serialize_ohlcv(df: pd.DataFrame, *, market: str = "", symbol: str = "") -> list:
    """DataFrame → [{time, open, high, low, close, volume}]"""
    result = []
    for dt_idx, row in df.iterrows():
        result.append({
            "time": _dt_to_unix(dt_idx, market=market, symbol=symbol),
            "open": round(float(row["open"]), 4),
            "high": round(float(row["high"]), 4),
            "low": round(float(row["low"]), 4),
            "close": round(float(row["close"]), 4),
            "volume": int(row.get("vol", 0)),
        })
    return result


def _compute_macd_data(df: pd.DataFrame, *, market: str = "", symbol: str = "") -> list:
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
            "time": _dt_to_unix(dt_idx, market=market, symbol=symbol),
            "dif": round(float(dif[dt_idx]), 4),
            "dea": round(float(dea[dt_idx]), 4),
            "bar": round(float(hist[dt_idx]), 4),
        })
    return result


def _compute_ma_lines(df: pd.DataFrame, *, market: str = "", symbol: str = "") -> list:
    """计算 MA 均线，返回 [{label, color, data: [{time, value}]}]"""
    from signals.core.ma_levels import KEY_MA_COLORS, KEY_MA_PERIODS

    closes = df["close"]
    ma_lines = []
    for period in KEY_MA_PERIODS:
        if len(closes) < period:
            continue
        ma_vals = closes.rolling(period).mean()
        line_data = []
        for dt_idx, val in ma_vals.dropna().items():
            line_data.append({
                "time": _dt_to_unix(dt_idx, market=market, symbol=symbol),
                "value": round(float(val), 4),
            })
        ma_lines.append({"label": f"MA{period}", "color": KEY_MA_COLORS.get(period, "#2962ff"), "data": line_data})
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
                "time": to_unix_seconds(dt, market="A"),
                "label": info["label"],
            })
        except ValueError:
            pass
    return sorted(presets, key=lambda x: x["time"])


# ─────────────────────────────────────────────────────
# MACD 信号检测
# ─────────────────────────────────────────────────────

def _detect_macd(df: pd.DataFrame, symbol: str, freq_label: str, lookback: int, *, market: str = "") -> list:
    """运行 MACD 信号检测，返回信号列表"""
    from signals.core.macd_detector import detect_macd_signals

    signals = detect_macd_signals(df, symbol, freq_label, lookback=lookback)
    result = []
    for sig in signals:
        sig_idx = df.index.get_loc(sig.dt)
        eval_data = _compute_forward_eval(df, sig_idx)

        result.append({
            "dt": _dt_to_unix(sig.dt, market=market, symbol=symbol),
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

def _detect_czsc(df: pd.DataFrame, symbol: str, freq_label: str, *, market: str = "") -> tuple:
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
            "dt": _dt_to_unix(ev.dt, market=market, symbol=symbol),
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
            "sdt": _dt_to_unix(bi.fx_a.dt, market=market, symbol=symbol),
            "edt": _dt_to_unix(bi.fx_b.dt, market=market, symbol=symbol),
            "high": round(bi.high, 4),
            "low": round(bi.low, 4),
            "direction": "up" if bi.direction.value == "向上" else "down",
            "power": round(bi.power_price, 4) if hasattr(bi, "power_price") else 0,
        })

    # 序列化中枢（从笔中提取）
    zhongshu = _extract_zhongshu(czsc_obj, market=market, symbol=symbol)

    return signals, bi_list, zhongshu


def _extract_zhongshu(czsc_obj, *, market: str = "", symbol: str = "") -> list:
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
                "start_dt": _dt_to_unix(bis[i].fx_a.dt, market=market, symbol=symbol),
                "end_dt": _dt_to_unix(bis[end_idx].fx_b.dt, market=market, symbol=symbol),
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
            macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback, market=market)
            all_signals.extend(macd_sigs)

        if signal_group in ("czsc", "all"):
            czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label, market=market)
            all_signals.extend(czsc_sigs)

        # 3. 按时间排序
        all_signals.sort(key=lambda s: s["dt"])

        # 4. 序列化图表数据
        ohlcv = _serialize_ohlcv(df, market=market, symbol=symbol)
        macd_data = _compute_macd_data(df, market=market, symbol=symbol)
        ma_lines = _compute_ma_lines(df, market=market, symbol=symbol)

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


@router.get("/simulate")
async def backtest_simulate(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    stop_loss: float = Query(5.0, description="止损百分比"),
    trail_stop: float = Query(50.0, description="移动止盈回撤百分比"),
    max_hold: int = Query(20, description="最大持仓天数"),
    slippage: float = Query(0.1, description="滑点百分比"),
    scan_param: str = Query("", description="扫描参数名 (stop_loss_pct / trail_stop_pct / max_hold_days)"),
    scan_values: str = Query("", description="扫描值列表 (逗号分隔, 如 3,5,7,10)"),
    scan_metric: str = Query("sharpe", description="优化目标 (sharpe / win_rate / expectancy)"),
):
    """
    交易模拟回测 — 基于 trade_simulator 的 Stop-Entry 成交模型。
    支持可选的参数扫描优化。
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

        # 2. 信号检测 (复用现有逻辑)
        all_signals = []
        bi_list = []
        zhongshu = []

        if signal_group in ("macd", "all"):
            macd_lookback = min(lookback, len(df) - 35)
            macd_sigs = _detect_macd(df, symbol, freq_label, macd_lookback, market=market)
            all_signals.extend(macd_sigs)

        if signal_group in ("czsc", "all"):
            czsc_sigs, bi_list, zhongshu = _detect_czsc(df, symbol, freq_label, market=market)
            all_signals.extend(czsc_sigs)

        all_signals.sort(key=lambda s: s["dt"])

        # 3. 构建模拟配置
        sim_config = SimConfig(
            stop_loss_pct=stop_loss,
            trail_stop_pct=trail_stop,
            max_hold_days=max_hold,
            slippage=slippage / 100.0,
        )

        # 4. 参数扫描 or 单次模拟
        scan_result = None
        if scan_param and scan_values:
            try:
                values = [float(v.strip()) for v in scan_values.split(",") if v.strip()]
                if values:
                    scan_result = run_parameter_scan(
                        df, all_signals, sim_config,
                        param1_name=scan_param,
                        param1_values=values,
                        metric=scan_metric,
                    )
            except Exception as e:
                logger.warning("参数扫描失败: %s", e)

        # 5. 单次模拟
        sim = simulate_trades(df, all_signals, sim_config)

        # 6. 序列化图表数据
        ohlcv = _serialize_ohlcv(df, market=market, symbol=symbol)
        macd_data = _compute_macd_data(df, market=market, symbol=symbol)
        ma_lines = _compute_ma_lines(df, market=market, symbol=symbol)

        # 7. 原始 KPI (前瞻评估)
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
            # 模拟结果
            "sim_trades": sim.trades,
            "sim_equity": sim.equity_curve,
            "sim_kpi": sim.kpi,
            "sim_config": sim.config,
            "sim_skip_reasons": sim.skip_reasons,
            "date_presets": _get_date_presets(),
        }

        if scan_result:
            result["scan"] = scan_result

        return result

    except Exception as e:
        logger.exception("模拟回测失败: code=%s freq=%s", code, freq)
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "detail": traceback.format_exc(),
        })


@router.get("/analyze")
async def backtest_analyze(
    code: str = Query(..., description="股票代码 (如 002759, 09988)"),
    freq: str = Query("daily", description="daily / weekly / monthly"),
    signal_group: str = Query("all", description="macd / czsc / all"),
    lookback: int = Query(999, description="信号回看窗口"),
    factor: str = Query("", description="入场因子: gap / trend_breakout / 200d_new_high_breakout / vol_contraction / candle_run / candle_accel"),
    gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20),
    bb_period: int = Query(20),
    squeeze_threshold: float = Query(0.05),
    run_count: int = Query(3),
    body_ratio: float = Query(0.5),
    accel_count: int = Query(3),
    stop_loss: float = Query(5.0),
    trail_stop: float = Query(50.0),
    max_hold: int = Query(20),
    slippage: float = Query(0.1),
    take_profit: float = Query(0),
    ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    batch_exit: str = Query("0"),
    batch1_ratio: float = Query(50),
    batch1_target: float = Query(5),
    batch2_target: float = Query(10),
    atr_exit_period: int = Query(0),
    atr_exit_mult: float = Query(2.0),
):
    """Canonical Signals 回测分析入口。"""
    from signals.services.backtest import backtest_analyze as _backtest_analyze

    return await _backtest_analyze(
        code=code,
        freq=freq,
        signal_group=signal_group,
        lookback=lookback,
        factor=factor,
        gap_pct_min=gap_pct_min,
        volume_ratio_min=volume_ratio_min,
        trend_lookback=trend_lookback,
        bb_period=bb_period,
        squeeze_threshold=squeeze_threshold,
        run_count=run_count,
        body_ratio=body_ratio,
        accel_count=accel_count,
        stop_loss=stop_loss,
        trail_stop=trail_stop,
        max_hold=max_hold,
        slippage=slippage,
        take_profit=take_profit,
        ma_exit_period=ma_exit_period,
        profit_drawdown=profit_drawdown,
        batch_exit=batch_exit,
        batch1_ratio=batch1_ratio,
        batch1_target=batch1_target,
        batch2_target=batch2_target,
        atr_exit_period=atr_exit_period,
        atr_exit_mult=atr_exit_mult,
    )


@router.post("/report")
async def backtest_report(request: Request, format: str = Query("html", description="html / pdf")):
    """将当前回测结果生成 HTML/PDF 报告附件。"""
    try:
        data = await request.json()
        fmt = format.lower().lstrip(".")
        from signals.core.backtest_report import render_backtest_report, report_filename

        content = render_backtest_report(data, fmt)
        media_type = "application/pdf" if fmt == "pdf" else "text/html; charset=utf-8"
        filename = report_filename(data, fmt)
        return StreamingResponse(
            BytesIO(content),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.exception("回测报告生成失败")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/scan")
async def backtest_scan(
    code: str = Query(...),
    freq: str = Query("daily"),
    signal_group: str = Query("all"),
    lookback: int = Query(999),
    factor: str = Query(""),
    gap_pct_min: float = Query(2.0),
    volume_ratio_min: float = Query(1.5),
    trend_lookback: int = Query(20),
    bb_period: int = Query(20),
    squeeze_threshold: float = Query(0.05),
    run_count: int = Query(3),
    body_ratio: float = Query(0.5),
    accel_count: int = Query(3),
    stop_loss: float = Query(5.0),
    trail_stop: float = Query(50.0),
    max_hold: int = Query(20),
    slippage: float = Query(0.1),
    take_profit: float = Query(0),
    ma_exit_period: int = Query(0),
    profit_drawdown: float = Query(0),
    atr_exit_period: int = Query(0),
    atr_exit_mult: float = Query(2.0),
    scan_param: str = Query(""),
    scan_values: str = Query(""),
    scan_param2: str = Query(""),
    scan_values2: str = Query(""),
    scan_metric: str = Query("sharpe"),
):
    """Canonical Signals 参数扫描入口。"""
    from signals.services.backtest import backtest_scan as _backtest_scan

    return await _backtest_scan(
        code=code,
        freq=freq,
        signal_group=signal_group,
        lookback=lookback,
        factor=factor,
        gap_pct_min=gap_pct_min,
        volume_ratio_min=volume_ratio_min,
        trend_lookback=trend_lookback,
        bb_period=bb_period,
        squeeze_threshold=squeeze_threshold,
        run_count=run_count,
        body_ratio=body_ratio,
        accel_count=accel_count,
        stop_loss=stop_loss,
        trail_stop=trail_stop,
        max_hold=max_hold,
        slippage=slippage,
        take_profit=take_profit,
        ma_exit_period=ma_exit_period,
        profit_drawdown=profit_drawdown,
        atr_exit_period=atr_exit_period,
        atr_exit_mult=atr_exit_mult,
        scan_param=scan_param,
        scan_values=scan_values,
        scan_param2=scan_param2,
        scan_values2=scan_values2,
        scan_metric=scan_metric,
    )


@router.get("/presets")
async def backtest_presets():
    """返回日期预设列表"""
    return _get_date_presets()


@router.get("/summary")
async def backtest_summary():
    """轻量回测状态摘要，避免 Dashboard 加载时触发外部行情请求。"""
    return {
        "total": 0,
        "status": "ready",
        "mode": "historical",
        "data_source": "gateway",
        "date_presets": len(_get_date_presets()),
    }


@router.get("/health/push2his")
async def push2his_health(
    code: str = Query("002759"),
    timeout: int = Query(8, ge=1, le=30),
    live: bool = Query(False, description="显式 live=true 时才直连东财做人工诊断"),
):
    from signals.web2.api.backtest import push2his_health as _impl

    return await _impl(code=code, timeout=timeout, live=live)


@router.post("/push")
async def backtest_push(request: Request):
    from signals.web2.api.backtest import backtest_push as _impl

    return await _impl(request)


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
    from signals.web2.api.backtest import backtest_export as _impl

    return await _impl(
        code=code,
        freq=freq,
        signal_group=signal_group,
        lookback=lookback,
        stop_loss=stop_loss,
        trail_stop=trail_stop,
        max_hold=max_hold,
        slippage=slippage,
        factor=factor,
        gap_pct_min=gap_pct_min,
        volume_ratio_min=volume_ratio_min,
        trend_lookback=trend_lookback,
        bb_period=bb_period,
        squeeze_threshold=squeeze_threshold,
        take_profit=take_profit,
        ma_exit_period=ma_exit_period,
        profit_drawdown=profit_drawdown,
        batch_exit=batch_exit,
        batch1_ratio=batch1_ratio,
        batch1_target=batch1_target,
        batch2_target=batch2_target,
    )


@router.post("/batch")
async def backtest_batch(request: Request):
    from signals.web2.api.backtest import backtest_batch as _impl

    return await _impl(request)
