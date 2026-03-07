# -*- coding: utf-8 -*-
"""
BaoStock 数据源 — A股个股历史分钟线

完全免费，无限额，无需注册。
支持 5min/15min/30min/60min，数据追溯至 1999 年。
限制：不支持指数分钟线（指数用 pytdx）。

安装：pip install baostock
"""
import pandas as pd
from datetime import datetime
from typing import List, Optional

from czsc import RawBar, Freq
from .fetcher import _to_raw_bars


# BaoStock freq → frequency 参数映射
_FREQ_MAP = {
    "5分钟": "5", "15分钟": "15", "30分钟": "30", "60分钟": "60",
}


def _futu_to_bs(futu_code: str) -> str:
    """SH.600519 → sh.600519"""
    mkt, code = futu_code.split(".")
    return f"{mkt.lower()}.{code}"


class BaoStockSource:
    """
    A股个股历史分钟线（BaoStock）。

    用法：
        src = BaoStockSource()
        bars = src.get_a_minute_hist("SH.600519", Freq.F15,
                                      "2025-01-01", "2025-03-06")
    """

    def __init__(self):
        self._logged_in = False

    def _login(self):
        if self._logged_in:
            return
        import baostock as bs
        self._bs = bs
        result = bs.login()
        if result.error_code != '0':
            raise ConnectionError(f"BaoStock login 失败: {result.error_msg}")
        self._logged_in = True

    def logout(self):
        if self._logged_in:
            self._bs.logout()
            self._logged_in = False

    def get_a_minute_hist(self, futu_code: str, freq: Freq,
                          start_date: str, end_date: str = None
                          ) -> List[RawBar]:
        """
        获取 A 股个股历史分钟线。

        :param futu_code: Futu 格式代码，如 'SH.600519'
        :param freq: Freq.F15 / Freq.F30 等
        :param start_date: 开始日期 'YYYY-MM-DD'
        :param end_date: 结束日期 'YYYY-MM-DD'，默认今天
        :return: List[RawBar]
        """
        self._login()
        bs_code = _futu_to_bs(futu_code)
        frequency = _FREQ_MAP.get(freq.value)
        if frequency is None:
            raise ValueError(f"BaoStock 不支持的频率: {freq.value}（仅支持 5/15/30/60 分钟）")

        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        rs = self._bs.query_history_k_data_plus(
            bs_code,
            "date,time,code,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag="3",  # 不复权
        )

        if rs.error_code != '0':
            print(f"  [BaoStock] {futu_code} {freq.value} 查询失败: {rs.error_msg}")
            return []

        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return []

        df = pd.DataFrame(rows, columns=rs.fields)

        # BaoStock time 格式: "20250306150000000"（毫秒级）→ 截取前14位
        df["dt"] = pd.to_datetime(df["time"].str[:14], format="%Y%m%d%H%M%S")
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["vol"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)

        # 过滤无效行
        df = df.dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            return []

        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_a_daily(self, futu_code: str,
                    start_date: str, end_date: str = None) -> List[RawBar]:
        """
        A股日线（BaoStock 兜底）。

        :param futu_code: Futu 格式代码
        :param start_date: 开始日期 'YYYY-MM-DD'
        :param end_date: 结束日期，默认今天
        """
        self._login()
        bs_code = _futu_to_bs(futu_code)
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        rs = self._bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",  # 前复权
        )

        if rs.error_code != '0':
            return []

        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return []

        df = pd.DataFrame(rows, columns=rs.fields)
        df["dt"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["vol"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
        df = df.dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            return []

        return _to_raw_bars(df, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.logout()
