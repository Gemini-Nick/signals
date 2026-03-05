# -*- coding: utf-8 -*-
"""
IBSource — Interactive Brokers 美股数据源（盘中优先）

通过 IB Gateway / TWS 获取美股历史K线，输出 czsc.RawBar 列表。
需要本地运行 IB Gateway（默认 127.0.0.1:4001）。
月费 $1.50-10（非专业用户美股行情订阅）。

依赖：pip install ib_async
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import List, Optional

from czsc import RawBar, Freq

from .fetcher import _to_raw_bars


class IBSource:
    """
    IB Gateway 美股数据源 — 盘中优先。
    接口与 FutuSource 美股方法一致，可直接作为 USDataSource 的 provider。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 4001,
                 client_id: int = 1):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._ib = None

    def connect(self) -> "IBSource":
        """同步连接 IB Gateway/TWS"""
        from ib_async import IB
        self._ib = IB()
        self._ib.connect(self.host, self.port, clientId=self.client_id)
        return self

    def close(self):
        """断开连接"""
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            self._ib = None

    @staticmethod
    def _ticker(futu_code: str) -> str:
        """US.SPY → SPY"""
        return futu_code.split(".")[1]

    def _make_contract(self, ticker: str):
        """创建 IB Stock 合约（美股 ETF / 个股）"""
        from ib_async import Stock
        return Stock(ticker, "SMART", "USD")

    @staticmethod
    def _freq_to_bar_size(freq: Freq) -> str:
        """Freq → IB barSizeSetting"""
        mapping = {
            "日线": "1 day",
            "周线": "1 week",
            "60分钟": "1 hour",
            "30分钟": "30 mins",
            "15分钟": "15 mins",
            "5分钟": "5 mins",
            "1分钟": "1 min",
        }
        return mapping.get(freq.value, "15 mins")

    @staticmethod
    def _make_duration(lookback_days: int, freq: Freq) -> str:
        """根据回溯天数和周期生成 IB durationStr"""
        if freq == Freq.D or freq == Freq.W:
            return f"{lookback_days} D"
        # 分钟线：IB 最大支持 "365 D"
        return f"{min(lookback_days, 365)} D"

    def _fetch_bars(self, futu_code: str, freq: Freq,
                    lookback_days: int = 180,
                    start: str = None) -> List[RawBar]:
        """核心方法：从 IB 拉取历史K线并转为 RawBar。"""
        if not self._ib or not self._ib.isConnected():
            raise ConnectionError("IB Gateway 未连接")

        ticker = self._ticker(futu_code)
        contract = self._make_contract(ticker)
        bar_size = self._freq_to_bar_size(freq)

        if start:
            delta = (datetime.now() - datetime.strptime(start, "%Y-%m-%d")).days
            lookback_days = max(delta + 5, lookback_days)
        duration = self._make_duration(lookback_days, freq)

        bars_data = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )

        if not bars_data:
            return []

        records = []
        for b in bars_data:
            records.append({
                "dt": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "vol": b.volume,
                "amount": 0,
            })

        df = pd.DataFrame(records)
        df["dt"] = pd.to_datetime(df["dt"])
        if df["dt"].dt.tz is not None:
            df["dt"] = df["dt"].dt.tz_localize(None)

        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    # ── 公开接口（与 FutuSource 美股方法签名一致）──────────

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
