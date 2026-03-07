# -*- coding: utf-8 -*-
"""
多数据源统一接口，统一输出 czsc.RawBar 列表

- TushareSource   : A股日线盘后分析
- AKShareSource   : A股/港股/美股 历史K线，候选池筛选
- FutuSource      : 盘中实时 K线订阅（A股/港股/美股）
- YFinanceSource  : 美股免费兜底（日线+分钟线）
- USDataSource    : 美股数据路由（Futu优先 → yfinance兜底）
"""

import os
import sys
import contextlib
import warnings
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable

warnings.filterwarnings("ignore")


@contextlib.contextmanager
def no_proxy():
    """临时清除代理环境变量（国内数据源不走代理）"""
    proxy_keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"]
    saved = {k: os.environ.pop(k) for k in proxy_keys if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)

# 兼容内部引用
_no_proxy = no_proxy

# 统一使用已安装的 czsc（0.10.11，Rust 加速版）
from czsc import RawBar, Freq


# ─────────────────────────────────────────────────────────
# 工具：DataFrame → RawBar 列表
# ─────────────────────────────────────────────────────────
def _to_raw_bars(df: pd.DataFrame, symbol: str, freq: Freq,
                 dt_col: str, o: str, h: str, l: str, c: str,
                 v: str, a: Optional[str] = None) -> List[RawBar]:
    """通用 DataFrame → RawBar 转换"""
    bars = []
    df = df.sort_values(dt_col).reset_index(drop=True)
    for i, row in df.iterrows():
        amount = int(float(row[a])) if a and a in row and pd.notna(row[a]) else 0
        vol = float(row[v]) if pd.notna(row[v]) else 0
        bars.append(RawBar(
            symbol=symbol,
            dt=pd.to_datetime(row[dt_col]),
            id=i,
            freq=freq,
            open=float(row[o]),
            high=float(row[h]),
            low=float(row[l]),
            close=float(row[c]),
            vol=int(vol),
            amount=amount,
        ))
    return bars


# ─────────────────────────────────────────────────────────
# 工具：市场检测
# ─────────────────────────────────────────────────────────
def detect_market(futu_code: str) -> str:
    """根据代码前缀判断市场：'A' / 'HK' / 'US' / 'UNKNOWN'"""
    prefix = futu_code.split(".")[0]
    if prefix in ("SH", "SZ", "BJ"):
        return "A"
    if prefix == "HK":
        return "HK"
    if prefix == "US":
        return "US"
    return "UNKNOWN"


# ─────────────────────────────────────────────────────────
# 1. Tushare — A股日线（盘后）
# ─────────────────────────────────────────────────────────
class TushareSource:
    """A股日线历史数据，适合盘后缠论分析"""

    def __init__(self, token: str):
        import tushare as ts
        ts.set_token(token)
        self._ts = ts

    @staticmethod
    def _futu_to_ts(futu_code: str) -> str:
        """SH.601958 → 601958.SH"""
        mkt, code = futu_code.split(".")
        return f"{code}.{mkt}"

    def get_minute(self, futu_code: str, freq: Freq,
                   lookback_days: int = 5) -> List[RawBar]:
        """
        A股分钟线（Tushare pro_bar）。
        限制：2次/分钟（需外部限速）；lookback_days 默认5天。
        """
        from datetime import datetime, timedelta
        ts_code = self._futu_to_ts(futu_code)
        freq_map = {"1分钟": "1min", "5分钟": "5min", "15分钟": "15min",
                    "30分钟": "30min", "60分钟": "60min"}
        ts_freq = freq_map.get(freq.value, "15min")
        edt = datetime.now().strftime("%Y%m%d")
        sdt = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        df = self._ts.pro_bar(ts_code=ts_code, freq=ts_freq,
                              start_date=sdt, end_date=edt)
        if df is None or df.empty:
            return []
        df = df.rename(columns={"trade_time": "dt"})
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_daily(self, symbol: str, sdt: str, edt: str,
                  adj: str = "qfq") -> List[RawBar]:
        """
        获取 A股日线 RawBar

        :param symbol: Tushare 格式代码，如 '601958.SH'
        :param sdt: 开始日期 'YYYYMMDD'
        :param edt: 结束日期 'YYYYMMDD'
        :param adj: 复权 qfq/hfq/None
        """
        df = self._ts.pro_bar(ts_code=symbol, adj=adj, freq="D",
                              start_date=sdt, end_date=edt)
        if df is None or df.empty:
            return []
        df = df.rename(columns={"trade_date": "dt"})
        return _to_raw_bars(df, symbol, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")


# ─────────────────────────────────────────────────────────
# 2. AKShare — A股/港股/美股 历史K线（免费，盘后）
# ─────────────────────────────────────────────────────────
class AKShareSource:
    """
    多市场历史K线，完全免费无限制
    支持：A股（日线+分钟线）/ 港股（日线）/ 美股（日线）
    """

    # AKShare A股 symbol 前缀映射
    _A_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

    def _futu_to_ak_a(self, futu_code: str) -> tuple:
        """SH.601958 → ('sh601958', '601958')"""
        mkt, code = futu_code.split(".")
        return self._A_PREFIX.get(mkt, "sh") + code, code

    def get_a_daily(self, futu_code: str, sdt: str, edt: str,
                    adj: str = "qfq", max_retries: int = 3) -> List[RawBar]:
        """A股日线（完整历史），内置重试 + 指数退避应对东财 SSL 间歇性故障。"""
        import akshare as ak
        import time as _time
        ak_sym, pure_code = self._futu_to_ak_a(futu_code)
        for attempt in range(1, max_retries + 1):
            try:
                with _no_proxy():
                    df = ak.stock_zh_a_hist(symbol=pure_code, period="daily",
                                             start_date=sdt.replace("-", ""),
                                             end_date=edt.replace("-", ""),
                                             adjust=adj)
                if df is None or df.empty:
                    return []
                df = df.rename(columns={"日期": "dt", "开盘": "open", "最高": "high",
                                         "最低": "low", "收盘": "close", "成交量": "vol",
                                         "成交额": "amount"})
                return _to_raw_bars(df, futu_code, Freq.D,
                                    "dt", "open", "high", "low", "close", "vol", "amount")
            except Exception as e:
                if attempt < max_retries:
                    _time.sleep(2 ** attempt)
                else:
                    raise

    def get_a_daily_sina(self, futu_code: str, sdt: str, edt: str,
                         adj: str = "qfq", timeout: float = 10.0) -> List[RawBar]:
        """A股日线（Sina源），东财 SSL 失败时的降级方案。带 timeout 防无限挂起。"""
        import akshare as ak
        ak_sym, _ = self._futu_to_ak_a(futu_code)
        with _no_proxy():
            df = self._call_with_timeout(
                lambda: ak.stock_zh_a_daily(symbol=ak_sym,
                                             start_date=sdt.replace("-", ""),
                                             end_date=edt.replace("-", ""),
                                             adjust=adj),
                timeout=timeout,
            )
        if df is None or df.empty:
            return []
        df = df.rename(columns={"date": "dt", "volume": "vol"})
        return _to_raw_bars(df, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    @staticmethod
    def _call_with_timeout(fn, timeout: float = 30.0):
        """在子线程中调用 fn，超时则抛 TimeoutError（防 Sina API 无限挂起）。"""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fn)
            try:
                return future.result(timeout=timeout)
            except FutureTimeout:
                raise TimeoutError(f"API 调用超时（>{timeout}s）")

    def get_a_minute(self, futu_code: str, freq: Freq,
                     timeout: float = 30.0) -> List[RawBar]:
        """
        A股分钟线（近 5 天，无复权）
        freq: Freq.F15 / Freq.F30
        带 timeout 保护，防止 Sina API 无限挂起。
        """
        import akshare as ak
        ak_sym, _ = self._futu_to_ak_a(futu_code)
        period_map = {"1分钟": "1", "5分钟": "5", "15分钟": "15",
                      "30分钟": "30", "60分钟": "60"}
        period = period_map.get(freq.value, "15")
        with _no_proxy():
            df = self._call_with_timeout(
                lambda: ak.stock_zh_a_minute(symbol=ak_sym, period=period, adjust=""),
                timeout=timeout,
            )
        if df is None or df.empty:
            return []
        df = df.rename(columns={"day": "dt", "volume": "vol"})
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_a_minute_em(self, futu_code: str, freq: Freq,
                        max_retries: int = 3) -> List[RawBar]:
        """
        A股分钟线 — 东财接口（push2his.eastmoney.com）。
        Sina 不可用时的备选源，支持更长历史但 SSL 间歇性超时。
        内置重试 + 指数退避。
        """
        import akshare as ak
        import time as _time
        _, pure_code = self._futu_to_ak_a(futu_code)
        period_map = {"1分钟": "1", "5分钟": "5", "15分钟": "15",
                      "30分钟": "30", "60分钟": "60"}
        period = period_map.get(freq.value, "15")
        for attempt in range(1, max_retries + 1):
            try:
                with _no_proxy():
                    df = ak.stock_zh_a_hist_min_em(
                        symbol=pure_code, period=period, adjust="")
                if df is None or df.empty:
                    return []
                df = df.rename(columns={
                    "时间": "dt", "开盘": "open", "最高": "high",
                    "最低": "low", "收盘": "close",
                    "成交量": "vol", "成交额": "amount",
                })
                return _to_raw_bars(df, futu_code, freq,
                                    "dt", "open", "high", "low",
                                    "close", "vol", "amount")
            except Exception as e:
                if attempt < max_retries:
                    wait = 2 ** attempt
                    _time.sleep(wait)
                else:
                    raise

    def get_hk_daily(self, futu_code: str, adj: str = "qfq") -> List[RawBar]:
        """港股日线（完整历史），futu_code: HK.00700"""
        import akshare as ak
        _, code = futu_code.split(".")
        df = ak.stock_hk_daily(symbol=code, adjust=adj)
        if df is None or df.empty:
            return []
        df = df.rename(columns={"date": "dt", "volume": "vol"})
        df["amount"] = 0
        return _to_raw_bars(df, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_us_daily(self, futu_code: str, adj: str = "qfq") -> List[RawBar]:
        """美股日线（完整历史），futu_code: US.AAPL"""
        import akshare as ak
        _, ticker = futu_code.split(".")
        df = ak.stock_us_daily(symbol=ticker, adjust=adj)
        if df is None or df.empty:
            return []
        df = df.rename(columns={"date": "dt", "volume": "vol"})
        df["amount"] = 0
        return _to_raw_bars(df, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    # 指数日线磁盘缓存目录
    _INDEX_CACHE_DIR = str(Path(__file__).resolve().parent.parent.parent / ".cache" / "index_daily")

    def get_index_daily(self, symbol: str,
                        lookback_days: int = 180,
                        start_date: str = None) -> List[RawBar]:
        """
        A股指数日线（带日级磁盘缓存）。
        symbol: AKShare格式，如 'sh000016'（上证50）
        盘中模式：传 lookback_days（滚动窗口，默认180自然日≈120交易日）
        盘后复盘：传 start_date（固定起点，如 '2024-09-24'）
        """
        import os, json

        # 尝试磁盘缓存（当日有效）
        today = datetime.now().strftime("%Y%m%d")
        cache_file = os.path.join(self._INDEX_CACHE_DIR, f"{symbol}_{today}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                df = pd.DataFrame(cached)
                df["dt"] = pd.to_datetime(df["dt"])
                if start_date:
                    cutoff = pd.to_datetime(start_date)
                else:
                    cutoff = pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
                df = df[df["dt"] >= cutoff]
                return _to_raw_bars(df, symbol, Freq.D,
                                    "dt", "open", "high", "low", "close", "vol", "amount")
            except Exception:
                pass  # 缓存损坏，走 API

        # API 拉取
        import akshare as ak
        with _no_proxy():
            df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or df.empty:
            return []
        df = df.rename(columns={"date": "dt", "volume": "vol"})
        df["amount"] = 0
        df["dt"] = pd.to_datetime(df["dt"])

        # 写入缓存（保存完整历史，裁剪在读取时做）
        try:
            os.makedirs(self._INDEX_CACHE_DIR, exist_ok=True)
            # 清理过期缓存（非今日的文件）
            for old in os.listdir(self._INDEX_CACHE_DIR):
                if old.endswith(".json") and today not in old:
                    os.remove(os.path.join(self._INDEX_CACHE_DIR, old))
            cache_df = df[["dt", "open", "high", "low", "close", "vol", "amount"]].copy()
            cache_df["dt"] = cache_df["dt"].astype(str)
            with open(cache_file, "w") as f:
                json.dump(cache_df.to_dict(orient="records"), f)
        except Exception:
            pass

        if start_date:
            cutoff = pd.to_datetime(start_date)
        else:
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
        df = df[df["dt"] >= cutoff]
        return _to_raw_bars(df, symbol, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_index_minute(self, symbol: str, freq: Freq,
                         timeout: float = 30.0) -> List[RawBar]:
        """
        A股指数分钟线（近5日）。
        symbol: AKShare格式，如 'sh000016'
        freq: Freq.F15 / Freq.F30
        实测：stock_zh_a_minute 对指数代码（sh前缀）完全支持，返回1970根。
        带 timeout 保护，防止 Sina API 无限挂起。
        """
        import akshare as ak
        period_map = {"1分钟": "1", "5分钟": "5", "15分钟": "15",
                      "30分钟": "30", "60分钟": "60"}
        period = period_map.get(freq.value, "15")
        with _no_proxy():
            df = self._call_with_timeout(
                lambda: ak.stock_zh_a_minute(symbol=symbol, period=period, adjust=""),
                timeout=timeout,
            )
        if df is None or df.empty:
            return []
        df = df.rename(columns={"day": "dt", "volume": "vol"})
        df["amount"] = df.get("amount", 0)
        return _to_raw_bars(df, symbol, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")


# ─────────────────────────────────────────────────────────
# 3. Futu — 盘中实时 K线订阅
# ─────────────────────────────────────────────────────────
class FutuSource:
    """
    盘中实时订阅，K线收盘时自动回调
    需要本地运行 FutuOpenD 网关（默认 127.0.0.1:11111）
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        self.host = host
        self.port = port
        self._ctx = None
        self._handler_cb: Optional[Callable] = None

    def connect(self, timeout: float = 5.0):
        """连接 FutuOpenD，先探测端口可达性，避免 SDK 内部无限重试。"""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((self.host, self.port))
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            raise ConnectionError(f"FutuOpenD 不可达 {self.host}:{self.port} ({e})")
        finally:
            sock.close()
        # 先 import futu 再抑制日志（SDK 在 import 时注册 logger）
        import logging
        from futu import OpenQuoteContext
        for name in ('futu', 'FTConsoleLog', 'FTFileLog'):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._ctx = OpenQuoteContext(host=self.host, port=self.port)
        return self

    def set_bar_callback(self, callback: Callable[[str, Freq, RawBar], None]):
        """
        设置 K 线回调函数
        callback(futu_code: str, freq: Freq, bar: RawBar)
        """
        from futu import CurKlineHandlerBase, RET_OK

        source = self

        class _Handler(CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != RET_OK or data is None or data.empty:
                    return ret, data
                for _, row in data.iterrows():
                    freq = source._kltype_to_freq(row.get("k_type", "K_15M"))
                    bar = RawBar(
                        symbol=row["code"],
                        dt=pd.to_datetime(row["time_key"]),
                        id=0,
                        freq=freq,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        vol=int(row["volume"]),
                        amount=int(float(row.get("turnover", 0))),
                    )
                    callback(row["code"], freq, bar)
                return ret, data

        self._ctx.set_handler(_Handler())

    def subscribe(self, codes: List[str], freqs: List[Freq]):
        """订阅实时 K 线"""
        from futu import SubType
        freq_map = {
            "1分钟": SubType.K_1M, "5分钟": SubType.K_5M,
            "15分钟": SubType.K_15M, "30分钟": SubType.K_30M,
            "60分钟": SubType.K_60M,
        }
        sub_types = [freq_map[f.value] for f in freqs if f.value in freq_map]
        ret, data = self._ctx.subscribe(codes, sub_types)
        return ret, data

    def get_index_kline(self, futu_code: str, freq: Freq,
                        lookback_days: int = 180,
                        start: str = None) -> List[RawBar]:
        """
        HK/港股指数历史K线（用于恒生科技 HK.800700）。
        盘中模式：lookback_days（默认180自然日）
        盘后复盘：传 start（如 '2024-09-24'）
        freq: Freq.D（日线）/ Freq.W（周线）
        注意：使用 request_history_kline，每次消耗1个历史K线额度。
        """
        from futu import KLType, AuType, RET_OK
        from datetime import datetime, timedelta
        if start is None:
            start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        ktype_map = {"日线": KLType.K_DAY, "周线": KLType.K_WEEK,
                     "60分钟": KLType.K_60M, "30分钟": KLType.K_30M,
                     "15分钟": KLType.K_15M}
        ktype = ktype_map.get(freq.value, KLType.K_DAY)
        ret, df, _ = self._ctx.request_history_kline(
            futu_code, start=start, ktype=ktype,
            autype=AuType.QFQ, max_count=2000
        )
        if ret != RET_OK or df is None or df.empty:
            return []
        df = df.rename(columns={"time_key": "dt", "volume": "vol",
                                 "turnover": "amount"})
        df["amount"] = df["amount"].fillna(0)
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_index_minute_hk(self, futu_code: str, freq: Freq,
                             num: int = 500) -> List[RawBar]:
        """
        HK指数分钟线（盘中，需先订阅）。
        使用 get_cur_kline（不消耗历史K线额度），需先 subscribe()。
        freq: Freq.F15 / Freq.F30
        """
        from futu import KLType, RET_OK
        ktype_map = {"15分钟": KLType.K_15M, "30分钟": KLType.K_30M,
                     "60分钟": KLType.K_60M}
        ktype = ktype_map.get(freq.value, KLType.K_15M)
        ret, df = self._ctx.get_cur_kline(futu_code, num=num, ktype=ktype)
        if ret != RET_OK or df is None or df.empty:
            return []
        df = df.rename(columns={"time_key": "dt", "volume": "vol",
                                  "turnover": "amount"})
        df["amount"] = df["amount"].fillna(0)
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    # ── A股分钟线（fallback 用）─────────────────────────────

    def get_a_minute(self, futu_code: str, freq: Freq,
                     lookback_days: int = 10) -> List[RawBar]:
        """
        A股分钟线历史（Futu request_history_kline）。
        futu_code: SH.601958 / SZ.000001
        freq: Freq.F15 / Freq.F30
        每次消耗1个历史K线额度（1000/天）。
        """
        from futu import KLType, AuType, RET_OK
        from datetime import datetime, timedelta
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        ktype_map = {
            "1分钟": KLType.K_1M, "5分钟": KLType.K_5M,
            "15分钟": KLType.K_15M, "30分钟": KLType.K_30M,
            "60分钟": KLType.K_60M,
        }
        ktype = ktype_map.get(freq.value, KLType.K_15M)
        ret, df, _ = self._ctx.request_history_kline(
            futu_code, start=start, ktype=ktype,
            autype=AuType.QFQ, max_count=2000
        )
        if ret != RET_OK or df is None or df.empty:
            return []
        df = df.rename(columns={"time_key": "dt", "volume": "vol",
                                 "turnover": "amount"})
        df["amount"] = df["amount"].fillna(0)
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    # ── 美股方法 ──────────────────────────────────────────

    def get_us_daily(self, futu_code: str,
                     lookback_days: int = 365) -> List[RawBar]:
        """
        美股日线历史（Futu request_history_kline）。
        需要已开通美股行情权限。
        """
        from futu import KLType, AuType, RET_OK
        from datetime import datetime, timedelta
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        ret, df, _ = self._ctx.request_history_kline(
            futu_code, start=start, ktype=KLType.K_DAY,
            autype=AuType.QFQ, max_count=2000
        )
        if ret != RET_OK or df is None or df.empty:
            return []
        df = df.rename(columns={"time_key": "dt", "volume": "vol",
                                 "turnover": "amount"})
        df["amount"] = df["amount"].fillna(0)
        return _to_raw_bars(df, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_us_minute(self, futu_code: str, freq: Freq,
                      lookback_days: int = 30) -> List[RawBar]:
        """
        美股分钟线历史（Futu request_history_kline）。
        freq: Freq.F5 / Freq.F15 / Freq.F30
        Futu 支持最多 8 年分钟线历史。
        """
        from futu import KLType, AuType, RET_OK
        from datetime import datetime, timedelta
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        ktype_map = {
            "1分钟": KLType.K_1M, "5分钟": KLType.K_5M,
            "15分钟": KLType.K_15M, "30分钟": KLType.K_30M,
            "60分钟": KLType.K_60M,
        }
        ktype = ktype_map.get(freq.value, KLType.K_15M)
        ret, df, _ = self._ctx.request_history_kline(
            futu_code, start=start, ktype=ktype,
            autype=AuType.QFQ, max_count=2000
        )
        if ret != RET_OK or df is None or df.empty:
            return []
        df = df.rename(columns={"time_key": "dt", "volume": "vol",
                                 "turnover": "amount"})
        df["amount"] = df["amount"].fillna(0)
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_us_index_kline(self, futu_code: str, freq: Freq,
                           lookback_days: int = 180,
                           start: str = None) -> List[RawBar]:
        """
        美股指数 ETF K线（SPY/QQQ/DIA）。
        复用 get_index_kline，接口完全一致。
        """
        return self.get_index_kline(futu_code, freq,
                                     lookback_days=lookback_days, start=start)

    def get_history_bars(self, futu_code: str, freq: Freq,
                         num: int = 500) -> List[RawBar]:
        """拉取历史K线用于初始化 CZSC 对象"""
        from futu import KLType, RET_OK
        kltype_map = {
            "15分钟": KLType.K_15M, "30分钟": KLType.K_30M,
            "60分钟": KLType.K_60M, "日线": KLType.K_DAY,
        }
        ktype = kltype_map.get(freq.value, KLType.K_15M)
        ret, df = self._ctx.get_cur_kline(futu_code, num=num, ktype=ktype)
        if ret != 0 or df is None or df.empty:
            return []
        df = df.rename(columns={"time_key": "dt", "volume": "vol",
                                  "turnover": "amount"})
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def close(self):
        if self._ctx:
            self._ctx.close()

    @staticmethod
    def _kltype_to_freq(k_type: str) -> Freq:
        mapping = {
            "K_1M": Freq.F1, "K_5M": Freq.F5,
            "K_15M": Freq.F15, "K_30M": Freq.F30,
            "K_60M": Freq.F60, "K_DAY": Freq.D,
        }
        return mapping.get(k_type, Freq.F15)


# ─────────────────────────────────────────────────────────
# 4. YFinance — 美股免费兜底
# ─────────────────────────────────────────────────────────
class YFinanceSource:
    """
    yfinance 免费数据源 — 无需连接、无需 API key
    适合 Futu 不可用时的美股数据兜底。
    限制：日线最多 1 年，分钟线 15min/30min 最多 60 天。
    """

    @staticmethod
    def _ticker(futu_code: str) -> str:
        """US.AAPL → AAPL"""
        return futu_code.split(".")[1]

    def get_us_daily(self, futu_code: str, period: str = "1y") -> List[RawBar]:
        """美股日线（最多 1 年历史）"""
        import yfinance as yf
        ticker = self._ticker(futu_code)
        tk = yf.Ticker(ticker)
        df = tk.history(period=period)
        if df is None or df.empty:
            return []
        df = df.reset_index()
        if df.columns is None or len(df.columns) == 0:
            return []
        if "Date" in df.columns:
            dt_col = "Date"
        elif "Datetime" in df.columns:
            dt_col = "Datetime"
        else:
            return []
        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(set(df.columns)):
            return []
        df = df.rename(columns={dt_col: "dt", "Open": "open", "High": "high",
                                 "Low": "low", "Close": "close", "Volume": "vol"})
        df["amount"] = 0
        # 去掉时区信息，确保 CZSC 兼容
        df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None)
        return _to_raw_bars(df, futu_code, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")

    def get_us_minute(self, futu_code: str, freq: Freq) -> List[RawBar]:
        """
        美股分钟线。
        yfinance 支持：1m(7天), 5m/15m/30m/60m(60天)
        """
        import yfinance as yf
        ticker = self._ticker(futu_code)
        # freq.value → yfinance interval
        interval_map = {
            "1分钟": "1m", "5分钟": "5m", "15分钟": "15m",
            "30分钟": "30m", "60分钟": "60m",
        }
        interval = interval_map.get(freq.value, "15m")
        period = "7d" if interval == "1m" else "60d"
        tk = yf.Ticker(ticker)
        df = tk.history(period=period, interval=interval)
        if df is None or df.empty:
            return []
        df = df.reset_index()
        if df.columns is None or len(df.columns) == 0:
            return []
        if "Datetime" in df.columns:
            dt_col = "Datetime"
        elif "Date" in df.columns:
            dt_col = "Date"
        else:
            return []
        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(set(df.columns)):
            return []
        df = df.rename(columns={dt_col: "dt", "Open": "open", "High": "high",
                                 "Low": "low", "Close": "close", "Volume": "vol"})
        df["amount"] = 0
        df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None)
        return _to_raw_bars(df, futu_code, freq,
                            "dt", "open", "high", "low", "close", "vol", "amount")


# ─────────────────────────────────────────────────────────
# 工具：异常分类（供 USDataSource 降级链使用）
# ─────────────────────────────────────────────────────────

# 不可恢复的异常类型，命中后立即放弃该数据源，不再重试
_FATAL_ERROR_TYPES = ("ImportError", "ModuleNotFoundError")


def _classify_error(e: Exception) -> str:
    """将异常分类为可读的失败原因，便于溯源"""
    err_type = type(e).__name__
    err_msg = str(e)
    # 网络类
    if any(k in err_type for k in ("Connection", "Timeout", "Socket", "Network")):
        return f"网络错误({err_type}): {err_msg}"
    if any(k in err_msg.lower() for k in ("refused", "timed out", "reset by peer")):
        return f"网络错误({err_type}): {err_msg}"
    # 认证/权限
    if any(k in err_msg.lower() for k in ("auth", "key", "permission",
                                           "forbidden", "401", "403")):
        return f"认证/权限错误({err_type}): {err_msg}"
    # 频率限制
    if any(k in err_msg.lower() for k in ("rate limit", "throttl", "pacing", "429")):
        return f"频率限制({err_type}): {err_msg}"
    # 数据不存在
    if any(k in err_msg.lower() for k in ("not found", "no data", "404",
                                           "invalid symbol")):
        return f"数据不存在({err_type}): {err_msg}"
    # 配置缺失
    if any(k in err_msg.lower() for k in ("未配置", "not configured", "empty")):
        return f"配置缺失({err_type}): {err_msg}"
    # 依赖缺失
    if err_type in _FATAL_ERROR_TYPES:
        return f"依赖未安装({err_type}): {err_msg}"
    # 其他
    return f"未知错误({err_type}): {err_msg}"


# ─────────────────────────────────────────────────────────
# 5. USDataSource — 美股数据路由（自动降级链）
# ─────────────────────────────────────────────────────────

class USDataSource:
    """
    美股统一数据入口 — 自动降级链。

    通过 providers 列表按优先级尝试数据源，每个源最多重试 3 次，
    全部失败后降级到下一个，yfinance 始终作为最终兜底。

    降级策略：
      盘中: [IBSource, FutuSource] → YFinanceSource
      盘后: [AlpacaSource]         → YFinanceSource

    向后兼容：USDataSource(futu_source=xxx) 等价于 providers=[xxx]
    """

    MAX_RETRIES = 3

    def __init__(self, futu_source: Optional[FutuSource] = None,
                 providers: Optional[list] = None):
        # 支持新 providers 列表，同时向后兼容旧 futu_source 参数
        if providers is not None:
            self._providers = providers
        elif futu_source is not None:
            self._providers = [futu_source]
        else:
            self._providers = []
        self._yf = YFinanceSource()

    def _try_source(self, src, method_name: str, label: str,
                    *args, **kwargs) -> Optional[List[RawBar]]:
        """
        对单个数据源尝试最多 MAX_RETRIES 次。
        dashboard 模式：仅 log 关键事件（成功/最终失败）。
        非 dashboard 模式：打印首次尝试 + 最终结果。
        遇到不可恢复错误（依赖缺失/认证错误）立即放弃。
        """
        from signals.dashboard import get_dashboard
        dash = get_dashboard()
        name = src.__class__.__name__
        fn = getattr(src, method_name)
        last_reason = ""

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                bars = fn(*args, **kwargs)
                if bars:
                    if dash:
                        dash.detail(f"  [{name}] {label} 成功 ({len(bars)}根)")
                    else:
                        print(f"  [{name}] {label} 成功 ({len(bars)}根)",
                              flush=True)
                    return bars
                last_reason = "返回空数据"
            except Exception as e:
                last_reason = _classify_error(e)
                # 依赖未安装 → 不可恢复，立即放弃
                if type(e).__name__ in _FATAL_ERROR_TYPES:
                    last_reason = "依赖未安装"
                    break
                # 认证/权限错误 → 不可恢复
                if "认证/权限" in last_reason or "配置缺失" in last_reason:
                    break

        # 最终失败：精简输出（截断长 URL）
        short_reason = last_reason[:80] + "..." if len(last_reason) > 80 else last_reason
        msg = f"  [{name}] {label} 失败({short_reason})，降级到下一数据源"
        if dash:
            dash.detail(msg)
        else:
            print(msg, flush=True)
        return None

    def _route(self, method_name: str, label: str,
               yf_method: str, yf_args: tuple, yf_kwargs: dict,
               *args, **kwargs) -> List[RawBar]:
        """统一路由逻辑：遍历 providers → yfinance 兜底。每个数据源最多重试 3 次。"""
        # 依次尝试 providers
        for src in self._providers:
            bars = self._try_source(src, method_name, label, *args, **kwargs)
            if bars:
                return bars

        # 最终兜底 yfinance
        bars = self._try_source(self._yf, yf_method,
                                f"{label}(兜底)", *yf_args, **yf_kwargs)
        return bars or []

    # ── 公开接口（签名不变）─────────────────────────────────

    def get_us_daily(self, futu_code: str, **kwargs) -> List[RawBar]:
        """美股日线，按降级链自动切换数据源"""
        return self._route(
            "get_us_daily", f"{futu_code} 日线",
            "get_us_daily", (futu_code,), {},
            futu_code, **kwargs,
        )

    def get_us_minute(self, futu_code: str, freq: Freq,
                      **kwargs) -> List[RawBar]:
        """美股分钟线，按降级链自动切换数据源"""
        return self._route(
            "get_us_minute", f"{futu_code} {freq.value}",
            "get_us_minute", (futu_code, freq), {},
            futu_code, freq, **kwargs,
        )

    def get_us_index_kline(self, futu_code: str, freq: Freq,
                           lookback_days: int = 180,
                           start: str = None) -> List[RawBar]:
        """美股指数 ETF K线，按降级链自动切换数据源"""
        # yfinance 兜底根据周期选择方法
        if freq == Freq.D:
            yf_method, yf_args = "get_us_daily", (futu_code,)
            yf_kwargs = {"period": "1y"}
        else:
            yf_method, yf_args = "get_us_minute", (futu_code, freq)
            yf_kwargs = {}

        return self._route(
            "get_us_index_kline",
            f"{futu_code} 指数{freq.value}",
            yf_method, yf_args, yf_kwargs,
            futu_code, freq,
            lookback_days=lookback_days, start=start,
        )

    def close(self):
        """清理 provider 连接"""
        for src in self._providers:
            if hasattr(src, "close"):
                try:
                    src.close()
                except Exception:
                    pass
