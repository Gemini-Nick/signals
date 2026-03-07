# -*- coding: utf-8 -*-
"""
仿真快照构建器 — 从历史 API 下载完整数据，打包为 SQLite 快照

数据源分工：
  - 指数日线：AKShare stock_zh_index_daily
  - 指数 30M/15M：pytdx get_index_bars
  - 个股日线：AKShare stock_zh_a_hist
  - 个股 30M/15M：BaoStock query_history_k_data_plus
  - 美股：yfinance（可选）

用法：
    builder = SimSessionBuilder("2026-01-14")
    builder.build(start_date="2026-01-14", symbols=["SH.601958"])
"""
import os
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from czsc import RawBar, Freq


# ─────────────────────────────────────────────────────────
# SQLite 表结构
# ─────────────────────────────────────────────────────────

_CREATE_META = """
CREATE TABLE IF NOT EXISTS session_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CREATE_BARS = """
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT    NOT NULL,
    freq    TEXT    NOT NULL,
    dt      TEXT    NOT NULL,
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    vol     REAL    DEFAULT 0,
    amount  REAL    DEFAULT 0,
    PRIMARY KEY (symbol, freq, dt)
);
"""


def _bars_to_rows(bars: List[RawBar], freq_override: str = None) -> list:
    """RawBar 列表 → SQLite 可插入的 tuple 列表"""
    rows = []
    for b in bars:
        rows.append((
            b.symbol,
            freq_override or b.freq.value,
            b.dt.strftime("%Y-%m-%d %H:%M:%S"),
            b.open, b.high, b.low, b.close,
            b.vol, b.amount,
        ))
    return rows


class SimSessionBuilder:
    """
    构建完整的仿真快照 SQLite。

    快照存储在 .data/sim/sessions/{name}.db
    """

    def __init__(self, session_name: str, sim_dir: str = None):
        if sim_dir is None:
            try:
                from config import SIM_SESSION_DIR
                sim_dir = SIM_SESSION_DIR
            except (ImportError, AttributeError):
                sim_dir = ".data/sim/sessions"

        os.makedirs(sim_dir, exist_ok=True)
        self._path = os.path.join(sim_dir, f"{session_name}.db")
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_META)
        self._conn.execute(_CREATE_BARS)
        self._conn.commit()
        self._stats: Dict[str, int] = {}

    @property
    def path(self) -> str:
        return self._path

    def build(self, start_date: str, end_date: str = None,
              symbols: Optional[List[str]] = None,
              include_us: bool = False):
        """
        一键构建完整仿真快照。

        :param start_date: 起始日期 'YYYY-MM-DD'
        :param end_date: 结束日期，默认今天
        :param symbols: 额外标的列表（Futu 格式），与 WHITELIST 合并
        :param include_us: 是否包含美股指数
        """
        import config

        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        # 合并标的池
        all_symbols = list(config.WHITELIST)
        if symbols:
            for s in symbols:
                if s not in all_symbols:
                    all_symbols.append(s)

        print(f"\n>>> 构建仿真快照: {start_date} → {end_date}")
        print(f"    标的: {len(all_symbols)} 只")
        print(f"    存储: {self._path}")

        # 1. 指数日线（AKShare）
        self._build_index_daily(config.INDEX_AK_CODES, start_date, end_date)

        # 2. 指数分钟线（pytdx）
        self._build_index_minute(config.INDEX_AK_CODES)

        # 3. 个股日线（AKShare）
        self._build_stock_daily(all_symbols, start_date, end_date)

        # 4. 个股分钟线（BaoStock）
        self._build_stock_minute(all_symbols, start_date, end_date)

        # 5. 美股（可选）
        if include_us:
            self._build_us_data(config.INDEX_US_CODES, start_date, end_date)

        # 6. 元数据
        self._save_meta(start_date, end_date, all_symbols)

        # 7. 报告
        self._print_summary()

    # ─────────────────────────────────────────────────────
    # 私有：各层数据构建
    # ─────────────────────────────────────────────────────

    def _build_index_daily(self, ak_codes: dict, start_date: str, end_date: str):
        """指数日线：AKShare"""
        from .fetcher import AKShareSource, no_proxy
        src = AKShareSource()

        print(f"\n  [1/4] 指数日线（AKShare）...")
        for name, symbol in ak_codes.items():
            try:
                bars = src.get_index_daily(symbol, start_date=start_date)
                if bars:
                    self._insert_bars(bars, freq_override="日线")
                    print(f"    ✓ {name} ({symbol}): {len(bars)} 根日线")
                    self._stats[f"idx_daily_{name}"] = len(bars)
                else:
                    print(f"    ✗ {name} ({symbol}): 无数据")
            except Exception as e:
                print(f"    ✗ {name} ({symbol}): {e}")

    def _build_index_minute(self, ak_codes: dict):
        """指数分钟线：pytdx"""
        from .pytdx_source import PytdxSource

        print(f"\n  [2/4] 指数分钟线（pytdx）...")
        try:
            tdx = PytdxSource()
        except Exception as e:
            print(f"    ✗ pytdx 初始化失败: {e}")
            return

        try:
            for name, symbol in ak_codes.items():
                for freq in [Freq.F30, Freq.F15]:
                    try:
                        bars = tdx.get_index_minute_hist(symbol, freq, count=2000)
                        if bars:
                            self._insert_bars(bars)
                            label = f"{name} {freq.value}"
                            print(f"    ✓ {label}: {len(bars)} 根")
                            self._stats[f"idx_{freq.value}_{name}"] = len(bars)
                        else:
                            print(f"    ✗ {name} {freq.value}: 无数据")
                    except Exception as e:
                        print(f"    ✗ {name} {freq.value}: {e}")
        finally:
            tdx.disconnect()

    def _build_stock_daily(self, symbols: List[str], start_date: str, end_date: str):
        """个股日线：AKShare"""
        from .fetcher import AKShareSource

        print(f"\n  [3/4] 个股日线（AKShare, {len(symbols)} 只）...")
        src = AKShareSource()
        for sym in symbols:
            try:
                bars = src.get_a_daily(sym, start_date.replace("-", ""),
                                        end_date.replace("-", ""))
                if bars:
                    self._insert_bars(bars, freq_override="日线")
                    print(f"    ✓ {sym}: {len(bars)} 根日线")
                    self._stats[f"stk_daily_{sym}"] = len(bars)
                else:
                    print(f"    ✗ {sym}: 无数据")
            except Exception as e:
                print(f"    ✗ {sym}: {e}")

    def _build_stock_minute(self, symbols: List[str], start_date: str, end_date: str):
        """个股分钟线：BaoStock"""
        from .baostock_source import BaoStockSource

        print(f"\n  [4/4] 个股分钟线（BaoStock, {len(symbols)} 只）...")
        bs_src = BaoStockSource()
        try:
            for sym in symbols:
                for freq in [Freq.F30, Freq.F15]:
                    try:
                        bars = bs_src.get_a_minute_hist(sym, freq,
                                                         start_date, end_date)
                        if bars:
                            self._insert_bars(bars)
                            label = f"{sym} {freq.value}"
                            print(f"    ✓ {label}: {len(bars)} 根")
                            self._stats[f"stk_{freq.value}_{sym}"] = len(bars)
                        else:
                            print(f"    ✗ {sym} {freq.value}: 无数据")
                    except Exception as e:
                        print(f"    ✗ {sym} {freq.value}: {e}")
        finally:
            bs_src.logout()

    def _build_us_data(self, us_codes: dict, start_date: str, end_date: str):
        """美股数据：yfinance"""
        from .fetcher import YFinanceSource

        print(f"\n  [可选] 美股指数（yfinance）...")
        yf = YFinanceSource()
        for name, symbol in us_codes.items():
            try:
                bars = yf.get_us_daily(symbol, period="1y")
                if bars:
                    self._insert_bars(bars, freq_override="日线")
                    print(f"    ✓ {name} ({symbol}): {len(bars)} 根日线")
                    self._stats[f"us_daily_{name}"] = len(bars)
            except Exception as e:
                print(f"    ✗ {name}: {e}")

            # 分钟线
            for freq in [Freq.F30, Freq.F15]:
                try:
                    bars = yf.get_us_minute(symbol, freq)
                    if bars:
                        self._insert_bars(bars)
                        print(f"    ✓ {name} {freq.value}: {len(bars)} 根")
                        self._stats[f"us_{freq.value}_{name}"] = len(bars)
                except Exception as e:
                    print(f"    ✗ {name} {freq.value}: {e}")

    # ─────────────────────────────────────────────────────
    # 工具
    # ─────────────────────────────────────────────────────

    def _insert_bars(self, bars: List[RawBar], freq_override: str = None):
        """批量插入 bar 数据"""
        rows = _bars_to_rows(bars, freq_override)
        self._conn.executemany(
            "INSERT OR REPLACE INTO bars "
            "(symbol, freq, dt, open, high, low, close, vol, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def _save_meta(self, start_date: str, end_date: str,
                   symbols: List[str]):
        """保存会话元数据"""
        meta = {
            "start_date": start_date,
            "end_date": end_date,
            "created_at": datetime.now().isoformat(),
            "symbols": json.dumps(symbols),
            "stats": json.dumps(self._stats),
            "type": "historical",
        }
        for k, v in meta.items():
            self._conn.execute(
                "INSERT OR REPLACE INTO session_meta (key, value) VALUES (?, ?)",
                (k, v),
            )
        self._conn.commit()

    def _print_summary(self):
        """打印构建摘要"""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT symbol), COUNT(DISTINCT freq), COUNT(*) FROM bars"
        ).fetchone()
        sym_cnt, freq_cnt, bar_cnt = row

        # 文件大小
        size_mb = os.path.getsize(self._path) / (1024 * 1024)

        print(f"\n  ── 仿真快照构建完成 ──")
        print(f"  标的数: {sym_cnt}")
        print(f"  级别数: {freq_cnt}")
        print(f"  总 bar: {bar_cnt:,}")
        print(f"  文件:   {self._path} ({size_mb:.1f} MB)")

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
