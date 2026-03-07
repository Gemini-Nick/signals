# -*- coding: utf-8 -*-
"""
仿真数据源 — 从 SQLite 快照读取数据，替代真实 API

实现与 AKShareSource / FutuSource / USDataSource 相同的方法签名，
使 IndexScreener 和 IntraDayScreener 可以无缝切换到仿真数据。

用法：
    sim = SimDataSource(".data/sim/sessions/2026-01-14.db")
    bars = sim.get_index_daily("sh000016")
    bars = sim.get_a_minute("SH.601958", Freq.F15)
    sim.close()
"""
import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

from czsc import RawBar, Freq


# Freq value → Freq 对象映射
_FREQ_MAP = {
    "日线": Freq.D, "周线": Freq.W,
    "1分钟": Freq.F1, "5分钟": Freq.F5,
    "15分钟": Freq.F15, "30分钟": Freq.F30,
    "60分钟": Freq.F60,
}


class SimDataSource:
    """
    从仿真快照 SQLite 读取数据。

    实现与真实数据源相同的接口签名，使 IndexScreener / IntraDayScreener
    可以通过 data_source 参数注入使用。
    """

    def __init__(self, session_path: str):
        if not os.path.exists(session_path):
            raise FileNotFoundError(f"仿真快照不存在: {session_path}")
        self._path = session_path
        self._conn = sqlite3.connect(session_path, check_same_thread=False)
        self._meta = self._load_meta()

    def _load_meta(self) -> dict:
        """加载会话元数据"""
        try:
            rows = self._conn.execute(
                "SELECT key, value FROM session_meta"
            ).fetchall()
            return {k: v for k, v in rows}
        except Exception:
            return {}

    @property
    def start_date(self) -> str:
        return self._meta.get("start_date", "")

    @property
    def end_date(self) -> str:
        return self._meta.get("end_date", "")

    @property
    def symbols(self) -> List[str]:
        raw = self._meta.get("symbols", "[]")
        return json.loads(raw)

    # ─────────────────────────────────────────────────────
    # 核心查询
    # ─────────────────────────────────────────────────────

    def _query(self, symbol: str, freq: str) -> List[RawBar]:
        """
        从 bars 表查询指定 symbol + freq 的全部 bar。

        :param symbol: 标的代码（与存储时一致）
        :param freq: 频率字符串，如 '日线'、'30分钟'、'15分钟'
        :return: List[RawBar]（按时间升序）
        """
        rows = self._conn.execute(
            "SELECT dt, open, high, low, close, vol, amount "
            "FROM bars WHERE symbol=? AND freq=? ORDER BY dt",
            (symbol, freq),
        ).fetchall()

        if not rows:
            return []

        freq_obj = _FREQ_MAP.get(freq, Freq.D)
        bars = []
        for i, (dt_str, o, h, l, c, v, a) in enumerate(rows):
            try:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                dt = datetime.strptime(dt_str, "%Y-%m-%d")
            bars.append(RawBar(
                symbol=symbol, dt=dt, id=i, freq=freq_obj,
                open=o, high=h, low=l, close=c,
                vol=int(v), amount=int(a),
            ))
        return bars

    def _available_symbols(self, freq: str = None) -> List[str]:
        """查询快照中有哪些 symbol"""
        if freq:
            rows = self._conn.execute(
                "SELECT DISTINCT symbol FROM bars WHERE freq=?", (freq,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT symbol FROM bars"
            ).fetchall()
        return [r[0] for r in rows]

    # ─────────────────────────────────────────────────────
    # AKShareSource 兼容接口
    # ─────────────────────────────────────────────────────

    def get_index_daily(self, symbol: str,
                        lookback_days: int = 180,
                        start_date: str = None) -> List[RawBar]:
        """A股指数日线"""
        return self._query(symbol, "日线")

    def get_index_minute(self, symbol: str, freq: Freq,
                         timeout: float = 30.0) -> List[RawBar]:
        """A股指数分钟线"""
        return self._query(symbol, freq.value)

    def get_a_minute(self, futu_code: str, freq: Freq,
                     timeout: float = 30.0) -> List[RawBar]:
        """A股个股分钟线"""
        return self._query(futu_code, freq.value)

    def get_a_minute_em(self, futu_code: str, freq: Freq,
                        max_retries: int = 3) -> List[RawBar]:
        """A股个股分钟线（东财兜底，仿真直接转发）"""
        return self.get_a_minute(futu_code, freq)

    def get_a_daily(self, futu_code: str, sdt: str = None, edt: str = None,
                    adj: str = "qfq", max_retries: int = 3) -> List[RawBar]:
        """A股个股日线"""
        return self._query(futu_code, "日线")

    # ─────────────────────────────────────────────────────
    # FutuSource 兼容接口
    # ─────────────────────────────────────────────────────

    def get_index_kline(self, futu_code: str, freq: Freq,
                        lookback_days: int = 180,
                        start: str = None) -> List[RawBar]:
        """HK/港股指数K线"""
        return self._query(futu_code, freq.value)

    def get_index_minute_hk(self, futu_code: str, freq: Freq,
                            num: int = 500) -> List[RawBar]:
        """HK指数分钟线"""
        return self._query(futu_code, freq.value)

    def get_a_minute_futu(self, futu_code: str, freq: Freq,
                          lookback_days: int = 10) -> List[RawBar]:
        """Futu A股分钟线"""
        return self.get_a_minute(futu_code, freq)

    def connect(self, timeout: float = 5.0):
        """No-op（仿真不需要连接）"""
        return self

    # ─────────────────────────────────────────────────────
    # USDataSource 兼容接口
    # ─────────────────────────────────────────────────────

    def get_us_index_kline(self, futu_code: str, freq: Freq,
                           lookback_days: int = 180,
                           start: str = None) -> List[RawBar]:
        """美股指数 ETF K线"""
        freq_str = freq.value if freq != Freq.D else "日线"
        return self._query(futu_code, freq_str)

    def get_us_minute(self, futu_code: str, freq: Freq,
                      **kwargs) -> List[RawBar]:
        """美股分钟线"""
        return self._query(futu_code, freq.value)

    def get_us_daily(self, futu_code: str, **kwargs) -> List[RawBar]:
        """美股日线"""
        return self._query(futu_code, "日线")

    # ─────────────────────────────────────────────────────
    # 信息与管理
    # ─────────────────────────────────────────────────────

    def summary(self) -> dict:
        """快照摘要"""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT symbol), COUNT(DISTINCT freq), COUNT(*) FROM bars"
        ).fetchone()
        return {
            "path": self._path,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "symbol_count": row[0],
            "freq_count": row[1],
            "bar_count": row[2],
            "meta": self._meta,
        }

    def print_info(self):
        """打印快照信息"""
        s = self.summary()
        print(f"\n  仿真快照: {s['path']}")
        print(f"  日期范围: {s['start_date']} → {s['end_date']}")
        print(f"  标的: {s['symbol_count']}  级别: {s['freq_count']}  "
              f"总 bar: {s['bar_count']:,}")

        # 按 freq 统计
        rows = self._conn.execute(
            "SELECT freq, COUNT(DISTINCT symbol), COUNT(*) "
            "FROM bars GROUP BY freq ORDER BY freq"
        ).fetchall()
        if rows:
            print(f"  {'级别':<10} {'标的数':>6} {'bar数':>8}")
            for freq, sym_cnt, bar_cnt in rows:
                print(f"  {freq:<10} {sym_cnt:>6} {bar_cnt:>8,}")

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─────────────────────────────────────────────────────────
# 工具：列出可用仿真会话
# ─────────────────────────────────────────────────────────

def list_sim_sessions(sim_dir: str = None) -> List[dict]:
    """列出所有可用的仿真快照"""
    if sim_dir is None:
        try:
            from config import SIM_SESSION_DIR
            sim_dir = SIM_SESSION_DIR
        except (ImportError, AttributeError):
            sim_dir = ".data/sim/sessions"

    if not os.path.exists(sim_dir):
        return []

    sessions = []
    for f in sorted(os.listdir(sim_dir)):
        if not f.endswith(".db"):
            continue
        path = os.path.join(sim_dir, f)
        name = f[:-3]  # 去掉 .db
        try:
            conn = sqlite3.connect(path)
            meta_rows = conn.execute(
                "SELECT key, value FROM session_meta"
            ).fetchall()
            meta = {k: v for k, v in meta_rows}
            row = conn.execute(
                "SELECT COUNT(DISTINCT symbol), COUNT(*) FROM bars"
            ).fetchone()
            conn.close()
            sessions.append({
                "name": name,
                "path": path,
                "start_date": meta.get("start_date", "?"),
                "end_date": meta.get("end_date", "?"),
                "symbol_count": row[0],
                "bar_count": row[1],
                "size_mb": os.path.getsize(path) / (1024 * 1024),
            })
        except Exception:
            sessions.append({
                "name": name, "path": path,
                "start_date": "?", "end_date": "?",
                "symbol_count": 0, "bar_count": 0,
                "size_mb": os.path.getsize(path) / (1024 * 1024),
            })

    return sessions
