# -*- coding: utf-8 -*-
"""
pytdx 数据源 — A股指数历史分钟线 + 个股兜底 + 行业板块日线/成分股

通达信协议，免费无注册，连接公共行情服务器。
支持 1min/5min/15min/30min/60min + 日线。
指数分钟线约 500+ 交易日历史，个股类似。
行业板块 880xxx 日线 K 线 + block.dat 成分股。

安装：pip install pytdx
"""
import pandas as pd
from datetime import datetime
from typing import List, Optional, Tuple

from czsc import RawBar, Freq
from .fetcher import _to_raw_bars


# 通达信公共行情服务器列表（多备选，自动切换）
_TDX_SERVERS = [
    ('119.147.212.81', 7709),
    ('114.80.63.12', 7709),
    ('218.75.126.9', 7709),
    ('221.194.181.176', 7709),
    ('124.74.236.94', 7721),
]

# freq → pytdx category 映射
_FREQ_CATEGORY = {
    "1分钟": 8, "5分钟": 0, "15分钟": 1,
    "30分钟": 2, "60分钟": 3,
}

# AKShare 指数符号 → pytdx (market, code) 映射
# sh 开头 → market=1（上海）
# sz 开头 → market=0（深圳）
def _ak_to_tdx(ak_symbol: str) -> Tuple[int, str]:
    """'sh000016' → (1, '000016')"""
    prefix = ak_symbol[:2].lower()
    code = ak_symbol[2:]
    market = 1 if prefix == "sh" else 0
    return market, code


def _futu_to_tdx(futu_code: str) -> Tuple[int, str]:
    """'SH.600519' → (1, '600519')"""
    mkt, code = futu_code.split(".")
    market = 1 if mkt.upper() == "SH" else 0
    return market, code


class PytdxSource:
    """
    A股指数/个股历史分钟线（通达信协议）。

    用法：
        src = PytdxSource()
        bars = src.get_index_minute_hist("sh000016", Freq.F15, count=2000)
        bars = src.get_stock_minute_hist("SH.600519", Freq.F30, count=1500)
    """

    _MAX_PER_REQUEST = 800  # pytdx 单次最多 800 根
    _MAX_PAGES = 26         # 最多 26 页 = 20800 根

    def __init__(self):
        self._api = None
        self._connected = False

    def _connect(self):
        """连接通达信服务器（自动尝试多个备选）"""
        if self._connected:
            return
        from pytdx.hq import TdxHq_API

        self._api = TdxHq_API()
        for host, port in _TDX_SERVERS:
            try:
                self._api.connect(host, port, time_out=10)
                self._connected = True
                return
            except Exception:
                continue
        raise ConnectionError("所有通达信服务器均不可达")

    def disconnect(self):
        if self._connected and self._api:
            try:
                self._api.disconnect()
            except Exception:
                pass
            self._connected = False

    def get_index_minute_hist(self, ak_symbol: str, freq: Freq,
                              count: int = 2000) -> List[RawBar]:
        """
        获取 A 股指数历史分钟线。

        :param ak_symbol: AKShare 格式，如 'sh000016'（上证50）
        :param freq: Freq.F15 / Freq.F30 等
        :param count: 需要的 bar 数量（自动分页）
        :return: List[RawBar]
        """
        self._connect()
        category = _FREQ_CATEGORY.get(freq.value)
        if category is None:
            raise ValueError(f"pytdx 不支持的频率: {freq.value}")

        market, code = _ak_to_tdx(ak_symbol)
        return self._fetch_bars(
            lambda offset, size: self._api.get_index_bars(
                category, market, code, offset, size
            ),
            ak_symbol, freq, count,
        )

    def get_stock_minute_hist(self, futu_code: str, freq: Freq,
                              count: int = 2000) -> List[RawBar]:
        """
        获取 A 股个股历史分钟线（BaoStock 不可用时的兜底）。

        :param futu_code: Futu 格式代码，如 'SH.600519'
        :param freq: Freq.F15 / Freq.F30 等
        :param count: 需要的 bar 数量
        :return: List[RawBar]
        """
        self._connect()
        category = _FREQ_CATEGORY.get(freq.value)
        if category is None:
            raise ValueError(f"pytdx 不支持的频率: {freq.value}")

        market, code = _futu_to_tdx(futu_code)
        return self._fetch_bars(
            lambda offset, size: self._api.get_security_bars(
                category, market, code, offset, size
            ),
            futu_code, freq, count,
        )

    def _fetch_bars(self, fetch_fn, symbol: str, freq: Freq,
                    count: int) -> List[RawBar]:
        """
        分页拉取并拼接 bar 数据。
        pytdx 每次最多返回 800 根，需用 offset 分页。
        """
        frames = []
        remaining = count
        page = 0

        while remaining > 0 and page < self._MAX_PAGES:
            size = min(remaining, self._MAX_PER_REQUEST)
            offset = page * self._MAX_PER_REQUEST
            try:
                data = fetch_fn(offset, size)
            except Exception as e:
                if page == 0:
                    raise
                break  # 非首页失败，返回已获取的数据

            if data is None or len(data) == 0:
                break

            df = self._api.to_df(data)
            if df.empty:
                break

            frames.append(df)
            remaining -= len(df)
            page += 1

            # 如果返回的数据少于请求数，说明已到头
            if len(df) < size:
                break

        if not frames:
            return []

        df = pd.concat(frames, ignore_index=True)

        # pytdx 返回的字段：datetime, open, high, low, close, vol, amount
        df = df.rename(columns={"datetime": "dt"})
        df["dt"] = pd.to_datetime(df["dt"])
        df["amount"] = df.get("amount", 0)

        # 按时间升序排列（pytdx 返回倒序）
        df = df.sort_values("dt").reset_index(drop=True)

        return _to_raw_bars(df, symbol, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.disconnect()
