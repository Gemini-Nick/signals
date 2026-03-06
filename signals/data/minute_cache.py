# -*- coding: utf-8 -*-
"""
分钟线本地缓存 —— 解决 AKShare 5 天窗口限制

每次运行把 API 返回的分钟线追加到 SQLite，随时间推移窗口自动扩展到 20-60 天。
CZSC 需要 15+ 笔才能形成可靠中枢，5 天 (~40 根 15M) 远远不够。
"""
import os
import sqlite3
from datetime import datetime, timedelta
from typing import List

from czsc import RawBar, Freq


# ─────────────────────────────────────────────────────────
# 默认配置（可被 config.py 覆盖）
# ─────────────────────────────────────────────────────────
_DEFAULT_DB_PATH = ".data/minute_cache.db"
_DEFAULT_MAX_DAYS = 60


def _get_config():
    """从 config.py 读取缓存配置，读不到则用默认值。"""
    try:
        from config import MINUTE_CACHE_DB_PATH, MINUTE_CACHE_MAX_DAYS
        return MINUTE_CACHE_DB_PATH, MINUTE_CACHE_MAX_DAYS
    except ImportError:
        return _DEFAULT_DB_PATH, _DEFAULT_MAX_DAYS


class MinuteCache:
    """
    分钟线 SQLite 缓存层。

    用法：
        cache = MinuteCache()
        cached = cache.get("SH.600519", "15分钟")
        cache.merge("SH.600519", "15分钟", fresh_bars)
        cache.close()
    """

    _CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS minute_bars (
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

    def __init__(self, db_path: str = ""):
        if not db_path:
            db_path, _ = _get_config()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(self._CREATE_SQL)
        self._conn.commit()

    # ── 读取 ──────────────────────────────────────────────

    def get(self, symbol: str, freq: str) -> List[RawBar]:
        """读取缓存中指定标的/频率的全部分钟线（按时间升序）。"""
        rows = self._conn.execute(
            "SELECT dt, open, high, low, close, vol, amount "
            "FROM minute_bars WHERE symbol=? AND freq=? ORDER BY dt",
            (symbol, freq),
        ).fetchall()
        if not rows:
            return []

        # freq 字符串 → Freq 对象
        freq_obj = _str_to_freq(freq)
        bars = []
        for i, (dt_str, o, h, l, c, v, a) in enumerate(rows):
            bars.append(RawBar(
                symbol=symbol,
                dt=datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S"),
                id=i,
                freq=freq_obj,
                open=o, high=h, low=l, close=c,
                vol=int(v), amount=int(a),
            ))
        return bars

    def count(self, symbol: str, freq: str) -> int:
        """缓存条数。"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE symbol=? AND freq=?",
            (symbol, freq),
        ).fetchone()
        return row[0] if row else 0

    # ── 写入 ──────────────────────────────────────────────

    def merge(self, symbol: str, freq: str, bars: List[RawBar]):
        """
        合并新数据到缓存（INSERT OR REPLACE 去重）。
        每根 bar 以 (symbol, freq, dt) 为主键去重。
        """
        if not bars:
            return
        data = []
        for bar in bars:
            dt_str = bar.dt.strftime("%Y-%m-%d %H:%M:%S")
            data.append((
                symbol, freq, dt_str,
                bar.open, bar.high, bar.low, bar.close,
                bar.vol, bar.amount,
            ))
        self._conn.executemany(
            "INSERT OR REPLACE INTO minute_bars "
            "(symbol, freq, dt, open, high, low, close, vol, amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            data,
        )
        self._conn.commit()

    # ── 清理 ──────────────────────────────────────────────

    def cleanup(self, max_age_days: int = 0):
        """清理超过 max_age_days 天的老数据。"""
        if max_age_days <= 0:
            _, max_age_days = _get_config()
        cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._conn.execute("DELETE FROM minute_bars WHERE dt < ?", (cutoff,))
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────

_FREQ_MAP = {
    "1分钟": Freq.F1, "5分钟": Freq.F5,
    "15分钟": Freq.F15, "30分钟": Freq.F30,
    "60分钟": Freq.F60,
}


def _str_to_freq(s: str) -> Freq:
    return _FREQ_MAP.get(s, Freq.F15)
