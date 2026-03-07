# -*- coding: utf-8 -*-
"""
历史数据仓库 — 透明缓存层，增量同步，快照秒级提取

仓库作为所有仿真快照的统一数据来源，避免重复下载。
用户无需直接操作仓库，run_sim() 自动管理。

存储位置：.data/sim/warehouse.db
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from czsc import RawBar, Freq

from .sim_builder import _CREATE_BARS, _CREATE_META, _bars_to_rows


_CREATE_SYNC_LOG = """
CREATE TABLE IF NOT EXISTS sync_log (
    symbol    TEXT NOT NULL,
    freq      TEXT NOT NULL,
    source    TEXT NOT NULL,
    last_dt   TEXT NOT NULL,
    bar_count INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, freq)
);
"""


class DataWarehouse:
    """
    历史数据仓库 — 持久化 SQLite，增量同步。

    数据存储在 .data/sim/warehouse.db，不同仿真快照共享底层数据。
    sync() 方法支持增量更新，仅下载新增数据。
    extract_session() 从仓库提取指定时间范围的数据创建快照。
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            try:
                from config import SIM_WAREHOUSE_DB
                db_path = SIM_WAREHOUSE_DB
            except (ImportError, AttributeError):
                db_path = ".data/sim/warehouse.db"

        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_BARS)
        self._conn.execute(_CREATE_SYNC_LOG)
        self._conn.commit()

    # ─────────────────────────────────────────────────────
    # 增量同步
    # ─────────────────────────────────────────────────────

    def sync(self, start_date: str, symbols: List[str] = None,
             include_us: bool = False):
        """
        增量同步历史数据到仓库。

        对已有数据的 (symbol, freq)：仅下载 last_dt 之后的新数据。
        对新 (symbol, freq)：从 start_date 开始全量下载。
        """
        import config

        end_date = datetime.now().strftime("%Y-%m-%d")

        # 合并标的池
        all_symbols = list(config.WHITELIST)
        if symbols:
            for s in symbols:
                if s not in all_symbols:
                    all_symbols.append(s)

        print(f"\n>>> 数据仓库同步: {start_date} → {end_date}")
        print(f"    标的: {len(all_symbols)} 只")
        print(f"    仓库: {self._path}")

        # 1. 指数日线（全量覆盖，数据小）
        self._sync_index_daily(config.INDEX_AK_CODES, start_date)

        # 2. 指数分钟线（全量覆盖，pytdx 返回固定窗口）
        self._sync_index_minute(config.INDEX_AK_CODES)

        # 3. 个股日线（增量）
        self._sync_stock_daily(all_symbols, start_date, end_date)

        # 4. 个股分钟线（增量）
        self._sync_stock_minute(all_symbols, start_date, end_date)

        # 5. 美股（可选）
        if include_us:
            self._sync_us_data(config.INDEX_US_CODES)

        self._print_summary()

    def _get_last_dt(self, symbol: str, freq: str) -> Optional[str]:
        """查询某 (symbol, freq) 的最新同步时间"""
        row = self._conn.execute(
            "SELECT last_dt FROM sync_log WHERE symbol=? AND freq=?",
            (symbol, freq),
        ).fetchone()
        return row[0] if row else None

    def _update_sync_log(self, symbol: str, freq: str,
                         source: str, last_dt: str, bar_count: int):
        """更新同步日志"""
        self._conn.execute(
            "INSERT OR REPLACE INTO sync_log "
            "(symbol, freq, source, last_dt, bar_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, freq, source, last_dt, bar_count,
             datetime.now().isoformat()),
        )
        self._conn.commit()

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

    # ─── 各数据源同步 ─────────────────────────────────────

    def _sync_index_daily(self, ak_codes: dict, start_date: str):
        """指数日线：AKShare，全量覆盖"""
        from .fetcher import AKShareSource
        src = AKShareSource()

        print(f"\n  [1/4] 指数日线（AKShare）...")
        for name, symbol in ak_codes.items():
            try:
                bars = src.get_index_daily(symbol, start_date=start_date)
                if bars:
                    self._insert_bars(bars, freq_override="日线")
                    last_dt = bars[-1].dt.strftime("%Y-%m-%d %H:%M:%S")
                    self._update_sync_log(symbol, "日线", "akshare",
                                          last_dt, len(bars))
                    print(f"    ✓ {name} ({symbol}): {len(bars)} 根")
                else:
                    print(f"    ✗ {name}: 无数据")
            except Exception as e:
                print(f"    ✗ {name}: {e}")

    def _sync_index_minute(self, ak_codes: dict):
        """指数分钟线：pytdx，全量覆盖"""
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
                        bars = tdx.get_index_minute_hist(symbol, freq,
                                                         count=2000)
                        if bars:
                            self._insert_bars(bars)
                            last_dt = bars[-1].dt.strftime(
                                "%Y-%m-%d %H:%M:%S")
                            self._update_sync_log(symbol, freq.value,
                                                  "pytdx", last_dt,
                                                  len(bars))
                            print(f"    ✓ {name} {freq.value}: "
                                  f"{len(bars)} 根")
                        else:
                            print(f"    ✗ {name} {freq.value}: 无数据")
                    except Exception as e:
                        print(f"    ✗ {name} {freq.value}: {e}")
        finally:
            tdx.disconnect()

    def _sync_stock_daily(self, symbols: List[str],
                          start_date: str, end_date: str):
        """个股日线：AKShare，增量追加"""
        from .fetcher import AKShareSource
        src = AKShareSource()

        print(f"\n  [3/4] 个股日线（AKShare, {len(symbols)} 只）...")
        for sym in symbols:
            last_dt = self._get_last_dt(sym, "日线")
            if last_dt:
                # 增量：从 last_dt 的下一天开始
                inc_start = (datetime.strptime(last_dt[:10], "%Y-%m-%d")
                             + timedelta(days=1)).strftime("%Y%m%d")
                if inc_start > end_date.replace("-", ""):
                    print(f"    ✓ {sym}: 已是最新")
                    continue
            else:
                inc_start = start_date.replace("-", "")

            try:
                bars = src.get_a_daily(sym, inc_start,
                                        end_date.replace("-", ""))
                if bars:
                    self._insert_bars(bars, freq_override="日线")
                    new_last = bars[-1].dt.strftime("%Y-%m-%d %H:%M:%S")
                    # 更新 bar_count 为仓库中总数
                    total = self._conn.execute(
                        "SELECT COUNT(*) FROM bars "
                        "WHERE symbol=? AND freq='日线'", (sym,)
                    ).fetchone()[0]
                    self._update_sync_log(sym, "日线", "akshare",
                                          new_last, total)
                    tag = "增量" if last_dt else "全量"
                    print(f"    ✓ {sym}: +{len(bars)} 根日线（{tag}）")
                else:
                    print(f"    ✗ {sym}: 无新数据")
            except Exception as e:
                print(f"    ✗ {sym}: {e}")

    def _sync_stock_minute(self, symbols: List[str],
                           start_date: str, end_date: str):
        """个股分钟线：BaoStock，增量追加"""
        from .baostock_source import BaoStockSource

        print(f"\n  [4/4] 个股分钟线（BaoStock, {len(symbols)} 只）...")
        bs_src = BaoStockSource()
        try:
            for sym in symbols:
                for freq in [Freq.F30, Freq.F15]:
                    last_dt = self._get_last_dt(sym, freq.value)
                    if last_dt:
                        inc_start = (
                            datetime.strptime(last_dt[:10], "%Y-%m-%d")
                            + timedelta(days=1)
                        ).strftime("%Y-%m-%d")
                        if inc_start > end_date:
                            print(f"    ✓ {sym} {freq.value}: 已是最新")
                            continue
                    else:
                        inc_start = start_date

                    try:
                        bars = bs_src.get_a_minute_hist(
                            sym, freq, inc_start, end_date)
                        if bars:
                            self._insert_bars(bars)
                            new_last = bars[-1].dt.strftime(
                                "%Y-%m-%d %H:%M:%S")
                            total = self._conn.execute(
                                "SELECT COUNT(*) FROM bars "
                                "WHERE symbol=? AND freq=?",
                                (sym, freq.value),
                            ).fetchone()[0]
                            self._update_sync_log(sym, freq.value,
                                                  "baostock", new_last,
                                                  total)
                            tag = "增量" if last_dt else "全量"
                            print(f"    ✓ {sym} {freq.value}: "
                                  f"+{len(bars)} 根（{tag}）")
                        else:
                            if not last_dt:
                                print(f"    ✗ {sym} {freq.value}: "
                                      f"无数据")
                            else:
                                print(f"    ✓ {sym} {freq.value}: "
                                      f"已是最新")
                    except Exception as e:
                        print(f"    ✗ {sym} {freq.value}: {e}")
        finally:
            bs_src.logout()

    def _sync_us_data(self, us_codes: dict):
        """美股数据：yfinance，全量覆盖"""
        from .fetcher import YFinanceSource

        print(f"\n  [可选] 美股指数（yfinance）...")
        yf = YFinanceSource()
        for name, symbol in us_codes.items():
            try:
                bars = yf.get_us_daily(symbol, period="1y")
                if bars:
                    self._insert_bars(bars, freq_override="日线")
                    last_dt = bars[-1].dt.strftime("%Y-%m-%d %H:%M:%S")
                    self._update_sync_log(symbol, "日线", "yfinance",
                                          last_dt, len(bars))
                    print(f"    ✓ {name}: {len(bars)} 根日线")
            except Exception as e:
                print(f"    ✗ {name}: {e}")

            for freq in [Freq.F30, Freq.F15]:
                try:
                    bars = yf.get_us_minute(symbol, freq)
                    if bars:
                        self._insert_bars(bars)
                        last_dt = bars[-1].dt.strftime(
                            "%Y-%m-%d %H:%M:%S")
                        self._update_sync_log(symbol, freq.value,
                                              "yfinance", last_dt,
                                              len(bars))
                        print(f"    ✓ {name} {freq.value}: "
                              f"{len(bars)} 根")
                except Exception as e:
                    print(f"    ✗ {name} {freq.value}: {e}")

    # ─────────────────────────────────────────────────────
    # 覆盖率检查
    # ─────────────────────────────────────────────────────

    def check_coverage(self, symbols: List[str],
                       start_date: str, end_date: str,
                       index_codes: dict = None) -> dict:
        """
        检查仓库数据覆盖情况。

        返回 {
            "covered": [(symbol, freq, bar_count), ...],
            "missing": [(symbol, freq, reason), ...],
            "total_bars": int,
        }
        """
        covered = []
        missing = []
        total = 0

        # 检查指数数据
        if index_codes:
            for name, symbol in index_codes.items():
                for freq_str in ["日线", "30分钟", "15分钟"]:
                    cnt = self._conn.execute(
                        "SELECT COUNT(*) FROM bars "
                        "WHERE symbol=? AND freq=?",
                        (symbol, freq_str),
                    ).fetchone()[0]
                    if cnt > 0:
                        covered.append((symbol, freq_str, cnt))
                        total += cnt
                    else:
                        missing.append((symbol, freq_str,
                                        f"指数 {name} 无数据"))

        # 检查个股数据
        for sym in symbols:
            for freq_str in ["日线", "30分钟", "15分钟"]:
                cnt = self._conn.execute(
                    "SELECT COUNT(*) FROM bars "
                    "WHERE symbol=? AND freq=?",
                    (sym, freq_str),
                ).fetchone()[0]
                if cnt > 0:
                    covered.append((sym, freq_str, cnt))
                    total += cnt
                else:
                    missing.append((sym, freq_str, "无数据"))

        return {
            "covered": covered,
            "missing": missing,
            "total_bars": total,
        }

    # ─────────────────────────────────────────────────────
    # 快照提取
    # ─────────────────────────────────────────────────────

    def extract_session(self, session_name: str,
                        start_date: str, end_date: str = None,
                        symbols: List[str] = None) -> str:
        """
        从仓库提取数据创建快照 SQLite。

        :return: session_path
        """
        import config

        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        all_symbols = list(config.WHITELIST)
        if symbols:
            for s in symbols:
                if s not in all_symbols:
                    all_symbols.append(s)

        session_dir = getattr(config, "SIM_SESSION_DIR",
                              ".data/sim/sessions")
        os.makedirs(session_dir, exist_ok=True)
        session_path = os.path.join(session_dir, f"{session_name}.db")

        # 创建 session SQLite
        sess_conn = sqlite3.connect(session_path)
        sess_conn.execute("PRAGMA journal_mode=WAL")
        sess_conn.execute(_CREATE_BARS)
        sess_conn.execute(_CREATE_META)
        sess_conn.commit()

        # 提取所有 bars（按 symbol+freq 批量复制）
        total_bars = 0
        rows = self._conn.execute(
            "SELECT symbol, freq, dt, open, high, low, close, vol, amount "
            "FROM bars ORDER BY symbol, freq, dt"
        ).fetchall()

        if rows:
            sess_conn.executemany(
                "INSERT OR REPLACE INTO bars "
                "(symbol, freq, dt, open, high, low, close, vol, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            total_bars = len(rows)

        # 元数据
        meta = {
            "start_date": start_date,
            "end_date": end_date,
            "created_at": datetime.now().isoformat(),
            "symbols": json.dumps(all_symbols),
            "type": "warehouse_extract",
        }
        for k, v in meta.items():
            sess_conn.execute(
                "INSERT OR REPLACE INTO session_meta (key, value) "
                "VALUES (?, ?)", (k, v),
            )
        sess_conn.commit()
        sess_conn.close()

        size_mb = os.path.getsize(session_path) / (1024 * 1024)
        print(f"\n  ── 快照提取完成 ──")
        print(f"  快照: {session_path} ({size_mb:.1f} MB)")
        print(f"  bar 数: {total_bars:,}")

        return session_path

    # ─────────────────────────────────────────────────────
    # 信息与管理
    # ─────────────────────────────────────────────────────

    def has_data(self) -> bool:
        """仓库是否有数据"""
        row = self._conn.execute("SELECT COUNT(*) FROM bars").fetchone()
        return row[0] > 0

    def summary(self) -> dict:
        """仓库概要"""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT symbol), COUNT(DISTINCT freq), "
            "COUNT(*) FROM bars"
        ).fetchone()
        sync_rows = self._conn.execute(
            "SELECT MAX(updated_at) FROM sync_log"
        ).fetchone()
        return {
            "path": self._path,
            "symbol_count": row[0],
            "freq_count": row[1],
            "bar_count": row[2],
            "last_sync": sync_rows[0] if sync_rows else None,
        }

    def _print_summary(self):
        """打印仓库概要"""
        s = self.summary()
        size_mb = os.path.getsize(self._path) / (1024 * 1024)
        print(f"\n  ── 数据仓库概要 ──")
        print(f"  标的: {s['symbol_count']}  级别: {s['freq_count']}  "
              f"总 bar: {s['bar_count']:,}")
        print(f"  文件: {self._path} ({size_mb:.1f} MB)")
        if s["last_sync"]:
            print(f"  最近同步: {s['last_sync'][:19]}")

    def print_info(self):
        """打印详细仓库信息"""
        self._print_summary()
        # 按 freq 统计
        rows = self._conn.execute(
            "SELECT freq, COUNT(DISTINCT symbol), COUNT(*) "
            "FROM bars GROUP BY freq ORDER BY freq"
        ).fetchall()
        if rows:
            print(f"\n  {'级别':<10} {'标的数':>6} {'bar数':>10}")
            for freq, sym_cnt, bar_cnt in rows:
                print(f"  {freq:<10} {sym_cnt:>6} {bar_cnt:>10,}")

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
