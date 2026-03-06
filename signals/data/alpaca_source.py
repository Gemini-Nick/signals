# -*- coding: utf-8 -*-
"""
AlpacaSource — Alpaca 美股数据源（盘后优先）

通过 Alpaca Market Data API (REST) 获取美股历史K线。
免费方案：无需入金，6-7年分钟线历史，15分钟延迟。
适合盘后复盘 / 回测场景。

依赖：pip install alpaca-py
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import List

from czsc import RawBar, Freq

from .fetcher import _to_raw_bars


class AlpacaSource:
    """
    Alpaca 美股数据源 — 盘后优先。
    无状态 REST API，线程安全，无需 connect/close。
    """

    def __init__(self, api_key: str, secret_key: str):
        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY / ALPACA_SECRET_KEY 未配置")
        self.api_key = api_key
        self.secret_key = secret_key
        self._client = None

    def _get_client(self):
        """懒加载 Alpaca 客户端"""
        if self._client is None:
            from alpaca.data import StockHistoricalDataClient
            self._client = StockHistoricalDataClient(
                self.api_key, self.secret_key
            )
        return self._client

    @staticmethod
    def _ticker(futu_code: str) -> str:
        """US.SPY → SPY"""
        return futu_code.split(".")[1]

    @staticmethod
    def _freq_to_timeframe(freq: Freq):
        """Freq → Alpaca TimeFrame"""
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        mapping = {
            "日线": TimeFrame.Day,
            "周线": TimeFrame.Week,
            "60分钟": TimeFrame(1, TimeFrameUnit.Hour),
            "30分钟": TimeFrame(30, TimeFrameUnit.Minute),
            "15分钟": TimeFrame(15, TimeFrameUnit.Minute),
            "5分钟": TimeFrame(5, TimeFrameUnit.Minute),
            "1分钟": TimeFrame(1, TimeFrameUnit.Minute),
        }
        return mapping.get(freq.value, TimeFrame(15, TimeFrameUnit.Minute))

    def _fetch_bars(self, futu_code: str, freq: Freq,
                    lookback_days: int = 180,
                    start: str = None) -> List[RawBar]:
        """核心方法：从 Alpaca 拉取历史K线并转为 RawBar。"""
        from alpaca.data.requests import StockBarsRequest

        client = self._get_client()
        ticker = self._ticker(futu_code)
        timeframe = self._freq_to_timeframe(freq)

        # 计算起止时间
        if start:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
        else:
            start_dt = datetime.now() - timedelta(days=lookback_days)
        end_dt = datetime.now()

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=timeframe,
            start=start_dt,
            end=end_dt,
        )

        bars_set = client.get_stock_bars(request)
        if not bars_set or not bars_set.data:
            return []

        bar_list = bars_set.data.get(ticker, [])
        if not bar_list:
            return []

        records = []
        for b in bar_list:
            records.append({
                "dt": b.timestamp,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "vol": int(b.volume),
                "amount": 0,
            })

        df = pd.DataFrame(records)
        df["dt"] = pd.to_datetime(df["dt"])
        # 去掉时区信息，确保 CZSC 兼容
        if df["dt"].dt.tz is not None:
            df["dt"] = df["dt"].dt.tz_localize(None)

        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    # ── 公开接口（与 FutuSource / IBSource 美股方法签名一致）──

    def get_us_daily(self, futu_code: str,
                     lookback_days: int = 365, **kwargs) -> List[RawBar]:
        """美股日线历史"""
        return self._fetch_bars(futu_code, Freq.D, lookback_days=lookback_days)

    def get_us_minute(self, futu_code: str, freq: Freq,
                      lookback_days: int = 60, **kwargs) -> List[RawBar]:
        """美股分钟线历史（15min/30min/60min）"""
        return self._fetch_bars(futu_code, freq, lookback_days=lookback_days)

    def get_us_index_kline(self, futu_code: str, freq: Freq,
                           lookback_days: int = 180,
                           start: str = None) -> List[RawBar]:
        """美股指数 ETF K线（SPY/QQQ/DIA），复用 _fetch_bars"""
        return self._fetch_bars(futu_code, freq,
                                lookback_days=lookback_days, start=start)
