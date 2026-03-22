# -*- coding: utf-8 -*-
"""
交易日志 — MongoDB 优先 + SQLite 降级 + 操作评分 + 遗漏分析

功能:
1. 交易记录增删查改 (TradeRecord)
2. 操作评分: 入场时机 / 仓位 / 出场时机
3. 错误分类: A-type(系统方差) / B-type(执行偏差) / C-type(情绪交易)
4. 遗漏分析: 信号出现但未操作的标的
5. 月度/季度统计

数据存储:
  MongoDB (跨设备): Mac/AutoDL/手机 共享交易记录
  SQLite  (降级):   无 MongoDB 时自动切换本地存储
"""
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_log = logging.getLogger("signals.trade_log")

# 数据库路径（SQLite 降级用）
_DB_DIR = Path(__file__).resolve().parent.parent.parent / ".data"
_DB_PATH = _DB_DIR / "trade_log.db"


@dataclass
class TradeRecord:
    """交易记录"""
    id: int = 0
    symbol: str = ""                # "SZ.002261"
    name: str = ""                  # "拓维信息"
    direction: str = "long"         # "long" / "short"
    # 入场
    entry_date: str = ""            # "2026-03-05"
    entry_price: float = 0.0
    entry_reason: str = ""          # "一买信号 + 板块强势"
    entry_signal: str = ""          # "一买" / "二买" / 手动
    # 出场
    exit_date: str = ""             # "2026-03-10" (空=持仓中)
    exit_price: float = 0.0
    exit_reason: str = ""           # "止盈" / "止损" / "信号消失"
    # 仓位
    position_pct: float = 0.0       # 仓位比例 0-100
    shares: int = 0                 # 股数
    # 评分
    timing_score: int = 0           # 入场时机评分 1-5
    position_score: int = 0         # 仓位管理评分 1-5
    exit_score: int = 0             # 出场时机评分 1-5
    error_type: str = ""            # "A" / "B" / "C" / ""
    # 结果
    pnl_pct: float = 0.0            # 盈亏百分比
    pnl_amount: float = 0.0         # 盈亏金额
    holding_days: int = 0           # 持仓天数
    # 元数据
    notes: str = ""                 # 备注
    tags: str = ""                  # 逗号分隔标签 "追涨,情绪交易"
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_open(self) -> bool:
        return not self.exit_date

    @property
    def total_score(self) -> float:
        scores = [s for s in [self.timing_score, self.position_score, self.exit_score] if s > 0]
        return round(sum(scores) / len(scores), 1) if scores else 0.0


@dataclass
class MissedSignal:
    """遗漏的信号（系统发出但未操作）"""
    symbol: str = ""
    name: str = ""
    signal_type: str = ""           # "一买" / "二买"
    signal_date: str = ""
    signal_price: float = 0.0
    # 事后追踪
    current_price: float = 0.0
    max_price_after: float = 0.0    # 信号后最高价
    potential_pnl_pct: float = 0.0  # 如果买了能赚多少


@dataclass
class TradeSummary:
    """交易统计"""
    period: str = ""                # "2026-03" / "2026-Q1"
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    max_win_pct: float = 0.0
    max_loss_pct: float = 0.0
    total_pnl: float = 0.0
    avg_holding_days: float = 0.0
    avg_score: float = 0.0
    error_counts: Dict[str, int] = field(default_factory=dict)  # {"A": 3, "B": 1, "C": 2}


class TradeLog:
    """交易日志管理器（MongoDB 优先，SQLite 降级）"""

    def __init__(self, db_path: Optional[Path] = None):
        # 尝试 MongoDB
        self._mongo_trades = None
        self._mongo_missed = None
        self._use_mongo = False
        try:
            import config
            if getattr(config, "DB_ENABLED", False):
                from signals.sync.db import get_db
                db = get_db()
                db.command("ping")
                self._mongo_trades = db["trades"]
                self._mongo_missed = db["missed_signals"]
                self._use_mongo = True
                _log.info("TradeLog 后端: MongoDB")
        except Exception as e:
            _log.debug(f"TradeLog MongoDB 不可用，降级 SQLite: {e}")

        # SQLite 降级（总是初始化）
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        if not self._use_mongo:
            _log.info("TradeLog 后端: SQLite")

    def _init_db(self):
        """初始化数据库表"""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    direction TEXT DEFAULT 'long',
                    entry_date TEXT NOT NULL,
                    entry_price REAL DEFAULT 0,
                    entry_reason TEXT DEFAULT '',
                    entry_signal TEXT DEFAULT '',
                    exit_date TEXT DEFAULT '',
                    exit_price REAL DEFAULT 0,
                    exit_reason TEXT DEFAULT '',
                    position_pct REAL DEFAULT 0,
                    shares INTEGER DEFAULT 0,
                    timing_score INTEGER DEFAULT 0,
                    position_score INTEGER DEFAULT 0,
                    exit_score INTEGER DEFAULT 0,
                    error_type TEXT DEFAULT '',
                    pnl_pct REAL DEFAULT 0,
                    pnl_amount REAL DEFAULT 0,
                    holding_days INTEGER DEFAULT 0,
                    notes TEXT DEFAULT '',
                    tags TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS missed_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    signal_type TEXT DEFAULT '',
                    signal_date TEXT NOT NULL,
                    signal_price REAL DEFAULT 0,
                    current_price REAL DEFAULT 0,
                    max_price_after REAL DEFAULT 0,
                    potential_pnl_pct REAL DEFAULT 0,
                    created_at TEXT DEFAULT ''
                )
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    # ── CRUD ──────────────────────────────────────────────

    def add_trade(self, trade: TradeRecord) -> int:
        """添加交易记录，返回 ID"""
        now = datetime.now().isoformat()
        trade.created_at = now
        trade.updated_at = now

        # 计算盈亏
        if trade.exit_price > 0 and trade.entry_price > 0:
            trade.pnl_pct = round((trade.exit_price - trade.entry_price) / trade.entry_price * 100, 2)
            if trade.direction == "short":
                trade.pnl_pct = -trade.pnl_pct

        if self._use_mongo:
            doc = {k: v for k, v in asdict(trade).items() if k != "id"}
            result = self._mongo_trades.insert_one(doc)
            _log.info(f"添加交易 [MongoDB]: {trade.symbol} {trade.entry_date}")
            return 0  # MongoDB 不返回 int ID

        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO trades (symbol, name, direction, entry_date, entry_price,
                    entry_reason, entry_signal, exit_date, exit_price, exit_reason,
                    position_pct, shares, timing_score, position_score, exit_score,
                    error_type, pnl_pct, pnl_amount, holding_days, notes, tags,
                    created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.symbol, trade.name, trade.direction,
                trade.entry_date, trade.entry_price,
                trade.entry_reason, trade.entry_signal,
                trade.exit_date, trade.exit_price, trade.exit_reason,
                trade.position_pct, trade.shares,
                trade.timing_score, trade.position_score, trade.exit_score,
                trade.error_type, trade.pnl_pct, trade.pnl_amount,
                trade.holding_days, trade.notes, trade.tags,
                trade.created_at, trade.updated_at,
            ))
            trade.id = cur.lastrowid
            _log.info(f"添加交易 [SQLite]: {trade.symbol} {trade.entry_date} → ID={trade.id}")
            return trade.id

    def update_trade(self, trade: TradeRecord) -> bool:
        """更新交易记录"""
        if not trade.id:
            return False

        trade.updated_at = datetime.now().isoformat()

        # 重算盈亏
        if trade.exit_price > 0 and trade.entry_price > 0:
            trade.pnl_pct = round((trade.exit_price - trade.entry_price) / trade.entry_price * 100, 2)
            if trade.direction == "short":
                trade.pnl_pct = -trade.pnl_pct

        with self._conn() as conn:
            conn.execute("""
                UPDATE trades SET
                    symbol=?, name=?, direction=?, entry_date=?, entry_price=?,
                    entry_reason=?, entry_signal=?, exit_date=?, exit_price=?,
                    exit_reason=?, position_pct=?, shares=?,
                    timing_score=?, position_score=?, exit_score=?,
                    error_type=?, pnl_pct=?, pnl_amount=?, holding_days=?,
                    notes=?, tags=?, updated_at=?
                WHERE id=?
            """, (
                trade.symbol, trade.name, trade.direction,
                trade.entry_date, trade.entry_price,
                trade.entry_reason, trade.entry_signal,
                trade.exit_date, trade.exit_price, trade.exit_reason,
                trade.position_pct, trade.shares,
                trade.timing_score, trade.position_score, trade.exit_score,
                trade.error_type, trade.pnl_pct, trade.pnl_amount,
                trade.holding_days, trade.notes, trade.tags,
                trade.updated_at, trade.id,
            ))
        return True

    def close_trade(self, trade_id: int, exit_price: float,
                    exit_date: str = "", exit_reason: str = "") -> bool:
        """平仓"""
        trade = self.get_trade(trade_id)
        if not trade:
            return False
        trade.exit_price = exit_price
        trade.exit_date = exit_date or datetime.now().strftime("%Y-%m-%d")
        trade.exit_reason = exit_reason

        # 计算持仓天数
        try:
            d1 = datetime.strptime(trade.entry_date, "%Y-%m-%d")
            d2 = datetime.strptime(trade.exit_date, "%Y-%m-%d")
            trade.holding_days = (d2 - d1).days
        except Exception:
            pass

        return self.update_trade(trade)

    def delete_trade(self, trade_id: int) -> bool:
        """删除交易记录"""
        with self._conn() as conn:
            conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        return True

    def get_trade(self, trade_id: int) -> Optional[TradeRecord]:
        """获取单条交易"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return None
        return self._row_to_trade(row)

    def list_trades(self, status: str = "all", limit: int = 50,
                    offset: int = 0) -> List[TradeRecord]:
        """
        列出交易记录。
        status: "all" / "open" / "closed"
        """
        if self._use_mongo:
            return self._mongo_list_trades(status, limit, offset)

        query = "SELECT * FROM trades"
        params = []

        if status == "open":
            query += " WHERE exit_date = '' OR exit_date IS NULL"
        elif status == "closed":
            query += " WHERE exit_date != '' AND exit_date IS NOT NULL"

        query += " ORDER BY entry_date DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def _mongo_list_trades(self, status, limit, offset) -> List[TradeRecord]:
        mongo_query = {}
        if status == "open":
            mongo_query["$or"] = [
                {"exit_date": ""},
                {"exit_date": {"$exists": False}},
                {"exit_date": None},
            ]
        elif status == "closed":
            mongo_query["exit_date"] = {"$nin": ["", None]}
        cursor = self._mongo_trades.find(mongo_query).sort(
            [("entry_date", -1)]
        ).skip(offset).limit(limit)
        return [self._doc_to_trade(doc) for doc in cursor]

    @staticmethod
    def _doc_to_trade(doc: dict) -> "TradeRecord":
        """MongoDB document → TradeRecord"""
        return TradeRecord(
            id=0,
            symbol=doc.get("symbol", ""),
            name=doc.get("name", ""),
            direction=doc.get("direction", "long"),
            entry_date=doc.get("entry_date", ""),
            entry_price=doc.get("entry_price", 0.0),
            entry_reason=doc.get("entry_reason", ""),
            entry_signal=doc.get("entry_signal", ""),
            exit_date=doc.get("exit_date", "") or "",
            exit_price=doc.get("exit_price", 0.0),
            exit_reason=doc.get("exit_reason", ""),
            position_pct=doc.get("position_pct", 0.0),
            shares=doc.get("shares", 0),
            timing_score=doc.get("timing_score", 0),
            position_score=doc.get("position_score", 0),
            exit_score=doc.get("exit_score", 0),
            error_type=doc.get("error_type", ""),
            pnl_pct=doc.get("pnl_pct", 0.0),
            pnl_amount=doc.get("pnl_amount", 0.0),
            holding_days=doc.get("holding_days", 0),
            notes=doc.get("notes", ""),
            tags=doc.get("tags", ""),
            created_at=doc.get("created_at", ""),
            updated_at=doc.get("updated_at", ""),
        )

    def score_trade(self, trade_id: int, timing: int = 0,
                    position: int = 0, exit: int = 0,
                    error_type: str = "") -> bool:
        """给交易评分"""
        trade = self.get_trade(trade_id)
        if not trade:
            return False
        if timing:
            trade.timing_score = max(1, min(5, timing))
        if position:
            trade.position_score = max(1, min(5, position))
        if exit:
            trade.exit_score = max(1, min(5, exit))
        if error_type in ("A", "B", "C", ""):
            trade.error_type = error_type
        return self.update_trade(trade)

    # ── 遗漏信号 ──────────────────────────────────────────

    def add_missed_signal(self, signal: MissedSignal) -> int:
        """记录遗漏的信号"""
        now = datetime.now().isoformat()

        if self._use_mongo:
            doc = {
                "symbol": signal.symbol, "name": signal.name,
                "signal_type": signal.signal_type, "signal_date": signal.signal_date,
                "signal_price": signal.signal_price, "current_price": signal.current_price,
                "max_price_after": signal.max_price_after,
                "potential_pnl_pct": signal.potential_pnl_pct,
                "created_at": now,
            }
            self._mongo_missed.insert_one(doc)
            return 0

        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO missed_signals
                    (symbol, name, signal_type, signal_date, signal_price,
                     current_price, max_price_after, potential_pnl_pct, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                signal.symbol, signal.name, signal.signal_type,
                signal.signal_date, signal.signal_price,
                signal.current_price, signal.max_price_after,
                signal.potential_pnl_pct, now,
            ))
            return cur.lastrowid

    def list_missed_signals(self, limit: int = 30) -> List[MissedSignal]:
        """列出遗漏信号"""
        if self._use_mongo:
            cursor = self._mongo_missed.find({}).sort(
                "signal_date", -1
            ).limit(limit)
            return [MissedSignal(
                symbol=r.get("symbol", ""), name=r.get("name", ""),
                signal_type=r.get("signal_type", ""),
                signal_date=r.get("signal_date", ""),
                signal_price=r.get("signal_price", 0.0),
                current_price=r.get("current_price", 0.0),
                max_price_after=r.get("max_price_after", 0.0),
                potential_pnl_pct=r.get("potential_pnl_pct", 0.0),
            ) for r in cursor]

        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM missed_signals ORDER BY signal_date DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [MissedSignal(
            symbol=r["symbol"], name=r["name"],
            signal_type=r["signal_type"], signal_date=r["signal_date"],
            signal_price=r["signal_price"], current_price=r["current_price"],
            max_price_after=r["max_price_after"],
            potential_pnl_pct=r["potential_pnl_pct"],
        ) for r in rows]

    # ── 统计 ──────────────────────────────────────────────

    def get_summary(self, period: str = "") -> TradeSummary:
        """
        获取交易统计。
        period: "" (全部) / "2026-03" (月) / "2026-Q1" (季度)
        """
        trades = self._query_by_period(period)
        closed = [t for t in trades if not t.is_open]

        summary = TradeSummary(period=period or "all")
        summary.total_trades = len(closed)

        if not closed:
            return summary

        wins = [t for t in closed if t.pnl_pct > 0]
        losses = [t for t in closed if t.pnl_pct <= 0]

        summary.win_count = len(wins)
        summary.loss_count = len(losses)
        summary.win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0

        pnls = [t.pnl_pct for t in closed]
        summary.avg_pnl_pct = round(sum(pnls) / len(pnls), 2)
        summary.max_win_pct = round(max(pnls), 2) if pnls else 0.0
        summary.max_loss_pct = round(min(pnls), 2) if pnls else 0.0
        summary.total_pnl = round(sum(t.pnl_amount for t in closed), 2)

        days = [t.holding_days for t in closed if t.holding_days > 0]
        summary.avg_holding_days = round(sum(days) / len(days), 1) if days else 0.0

        scores = [t.total_score for t in closed if t.total_score > 0]
        summary.avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

        # 错误分类统计
        for t in closed:
            if t.error_type:
                summary.error_counts[t.error_type] = summary.error_counts.get(t.error_type, 0) + 1

        return summary

    def _query_by_period(self, period: str) -> List[TradeRecord]:
        """按时间段查询交易"""
        if not period:
            return self.list_trades(status="all", limit=9999)

        # 月度: "2026-03"
        if len(period) == 7 and "-" in period:
            start = f"{period}-01"
            # 简单计算下个月
            y, m = int(period[:4]), int(period[5:7])
            if m == 12:
                end = f"{y+1}-01-01"
            else:
                end = f"{y}-{m+1:02d}-01"
        # 季度: "2026-Q1"
        elif "Q" in period:
            y = int(period[:4])
            q = int(period[-1])
            start_month = (q - 1) * 3 + 1
            end_month = start_month + 3
            start = f"{y}-{start_month:02d}-01"
            if end_month > 12:
                end = f"{y+1}-01-01"
            else:
                end = f"{y}-{end_month:02d}-01"
        else:
            return self.list_trades(status="all", limit=9999)

        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades WHERE entry_date >= ? AND entry_date < ? ORDER BY entry_date",
                (start, end)
            ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    # ── 辅助 ──────────────────────────────────────────────

    @staticmethod
    def _row_to_trade(row) -> TradeRecord:
        return TradeRecord(
            id=row["id"],
            symbol=row["symbol"],
            name=row["name"],
            direction=row["direction"],
            entry_date=row["entry_date"],
            entry_price=row["entry_price"],
            entry_reason=row["entry_reason"],
            entry_signal=row["entry_signal"],
            exit_date=row["exit_date"] or "",
            exit_price=row["exit_price"],
            exit_reason=row["exit_reason"],
            position_pct=row["position_pct"],
            shares=row["shares"],
            timing_score=row["timing_score"],
            position_score=row["position_score"],
            exit_score=row["exit_score"],
            error_type=row["error_type"],
            pnl_pct=row["pnl_pct"],
            pnl_amount=row["pnl_amount"],
            holding_days=row["holding_days"],
            notes=row["notes"],
            tags=row["tags"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ── 便捷函数 ──────────────────────────────────────────

_log_instance: Optional[TradeLog] = None


def get_trade_log() -> TradeLog:
    """获取全局 TradeLog 实例"""
    global _log_instance
    if _log_instance is None:
        _log_instance = TradeLog()
    return _log_instance
