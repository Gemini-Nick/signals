# -*- coding: utf-8 -*-
"""
信号回测验证引擎 —— 自我进化的核心

三层架构：
  SignalJournal    — 信号持久化（SQLite）
  ForwardEvaluator — 单信号前瞻评估（多窗口收益 + MFE/MAE）
  BacktestReport   — 双视角统计报告（群组级 + 交易级）

设计理念：
  信号发出 → 持久化 → N日后自动验证 → 用数据反哺权重
  这是将「拍脑袋的权重」变为「数据驱动的权重」的关键环节。
"""
import os
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

import config


# ─────────────────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────────────────

@dataclass
class SignalRecord:
    """一条信号的完整记录。"""
    symbol: str
    signal_date: str          # YYYY-MM-DD
    signal_type: str          # 一买/二买/三买/背驰买/趋势买/一卖/...
    freq: str                 # 15分钟/30分钟/日线
    confidence: float
    price: float
    total_score: float = 0.0
    market_direction: str = "分化"
    has_resonance: int = 0
    details: str = ""
    row_id: int = 0           # DB id, 0 = not persisted


@dataclass
class EvalResult:
    """单信号前瞻评估结果。"""
    return_t5: Optional[float] = None
    return_t10: Optional[float] = None
    return_t20: Optional[float] = None
    mfe: float = 0.0          # 最大有利偏移 %
    mae: float = 0.0          # 最大不利偏移 %（负数）
    mfe_day: int = 0
    mae_day: int = 0
    direction_correct: Optional[int] = None  # 1/0/None
    hit_target: int = 0
    days_to_target: Optional[int] = None


@dataclass
class TradePair:
    """一组买卖配对。"""
    symbol: str
    buy_record_id: int
    sell_record_id: int
    buy_date: str
    sell_date: str
    buy_price: float
    sell_price: float
    buy_signal_type: str
    sell_signal_type: str
    holding_days: int
    return_pct: float


@dataclass
class GroupStats:
    """一组信号的统计指标集。"""
    count: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    payoff_ratio: float = 0.0
    avg_mfe: float = 0.0
    avg_mae: float = 0.0
    mfe_mae_ratio: float = 0.0
    avg_return: Dict[int, float] = field(default_factory=dict)
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0


# ─────────────────────────────────────────────────────────
# SignalJournal — 信号持久化
# ─────────────────────────────────────────────────────────

class SignalJournal:
    """
    信号日志存储（SQLite）。

    每次 screener/review 运行时调用 log_batch() 把检出信号存入。
    INSERT OR IGNORE 防重复（同一标的+日期+类型+频率 唯一）。
    """

    def __init__(self, db_path: str = ""):
        self._db_path = db_path or config.BACKTEST_DB_PATH
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_tables()

    def _ensure_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS signal_records (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT NOT NULL,
                signal_date      TEXT NOT NULL,
                signal_type      TEXT NOT NULL,
                freq             TEXT NOT NULL,
                confidence       REAL NOT NULL,
                price            REAL NOT NULL,
                total_score      REAL DEFAULT 0,
                market_direction TEXT DEFAULT '分化',
                has_resonance    INTEGER DEFAULT 0,
                details          TEXT DEFAULT '',
                created_at       TEXT,

                evaluated        INTEGER DEFAULT 0,
                eval_date        TEXT,
                return_t5        REAL,
                return_t10       REAL,
                return_t20       REAL,
                mfe              REAL,
                mae              REAL,
                mfe_day          INTEGER,
                mae_day          INTEGER,
                direction_correct INTEGER,
                hit_target       INTEGER DEFAULT 0,
                days_to_target   INTEGER,

                UNIQUE(symbol, signal_date, signal_type, freq)
            );

            CREATE INDEX IF NOT EXISTS idx_pending
                ON signal_records(evaluated, signal_date);
            CREATE INDEX IF NOT EXISTS idx_type
                ON signal_records(signal_type, freq);

            CREATE TABLE IF NOT EXISTS trade_pairs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT NOT NULL,
                buy_record_id    INTEGER REFERENCES signal_records(id),
                sell_record_id   INTEGER REFERENCES signal_records(id),
                buy_date         TEXT,
                sell_date        TEXT,
                buy_price        REAL,
                sell_price       REAL,
                buy_signal_type  TEXT,
                sell_signal_type TEXT,
                holding_days     INTEGER,
                return_pct       REAL,
                UNIQUE(buy_record_id, sell_record_id)
            );
        """)

    def log_batch(self, records: List[SignalRecord]) -> int:
        """批量写入信号记录，返回新增条数。"""
        if not records:
            return 0
        inserted = 0
        with self._conn:
            for r in records:
                try:
                    self._conn.execute("""
                        INSERT OR IGNORE INTO signal_records
                        (symbol, signal_date, signal_type, freq, confidence,
                         price, total_score, market_direction, has_resonance,
                         details, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (r.symbol, r.signal_date, r.signal_type, r.freq,
                          r.confidence, r.price, r.total_score,
                          r.market_direction, r.has_resonance, r.details,
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    if self._conn.total_changes:
                        inserted += 1
                except sqlite3.IntegrityError:
                    pass
        return inserted

    def get_pending(self, min_age_days: int = 0) -> List[dict]:
        """获取待评估的信号记录（signal_date 距今 >= min_age_days）。"""
        min_age = min_age_days or config.BACKTEST_MIN_AGE_DAYS
        cur = self._conn.execute("""
            SELECT id, symbol, signal_date, signal_type, freq,
                   confidence, price, total_score, market_direction,
                   has_resonance, details
            FROM signal_records
            WHERE evaluated = 0
              AND julianday('now') - julianday(signal_date) >= ?
            ORDER BY signal_date
        """, (min_age,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_evaluated(self, signal_type: str = "", freq: str = "",
                      market_direction: str = "") -> List[dict]:
        """获取已评估记录，支持按类型/频率/大势筛选。"""
        sql = "SELECT * FROM signal_records WHERE evaluated = 1"
        params = []
        if signal_type:
            sql += " AND signal_type = ?"
            params.append(signal_type)
        if freq:
            sql += " AND freq = ?"
            params.append(freq)
        if market_direction:
            sql += " AND market_direction = ?"
            params.append(market_direction)
        sql += " ORDER BY signal_date"
        cur = self._conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_all_records(self, signal_type: str = "", freq: str = "") -> List[dict]:
        """获取所有记录（含未评估），用于买卖配对。"""
        sql = "SELECT * FROM signal_records WHERE 1=1"
        params = []
        if signal_type:
            sql += " AND signal_type = ?"
            params.append(signal_type)
        if freq:
            sql += " AND freq = ?"
            params.append(freq)
        sql += " ORDER BY signal_date"
        cur = self._conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def update_evaluation(self, row_id: int, result: EvalResult):
        """将评估结果写回数据库。"""
        with self._conn:
            self._conn.execute("""
                UPDATE signal_records SET
                    evaluated = 1,
                    eval_date = ?,
                    return_t5 = ?,
                    return_t10 = ?,
                    return_t20 = ?,
                    mfe = ?,
                    mae = ?,
                    mfe_day = ?,
                    mae_day = ?,
                    direction_correct = ?,
                    hit_target = ?,
                    days_to_target = ?
                WHERE id = ?
            """, (
                datetime.now().strftime("%Y-%m-%d"),
                result.return_t5, result.return_t10, result.return_t20,
                result.mfe, result.mae, result.mfe_day, result.mae_day,
                result.direction_correct, result.hit_target,
                result.days_to_target, row_id,
            ))

    def save_trade_pairs(self, pairs: List[TradePair]):
        """保存买卖配对。"""
        if not pairs:
            return
        with self._conn:
            for p in pairs:
                try:
                    self._conn.execute("""
                        INSERT OR IGNORE INTO trade_pairs
                        (symbol, buy_record_id, sell_record_id, buy_date, sell_date,
                         buy_price, sell_price, buy_signal_type, sell_signal_type,
                         holding_days, return_pct)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (p.symbol, p.buy_record_id, p.sell_record_id,
                          p.buy_date, p.sell_date, p.buy_price, p.sell_price,
                          p.buy_signal_type, p.sell_signal_type,
                          p.holding_days, p.return_pct))
                except sqlite3.IntegrityError:
                    pass

    def get_trade_pairs(self) -> List[dict]:
        """获取所有买卖配对。"""
        cur = self._conn.execute("SELECT * FROM trade_pairs ORDER BY buy_date")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def summary(self) -> dict:
        """数据库概要统计。"""
        cur = self._conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN evaluated = 1 THEN 1 ELSE 0 END) as evaluated,
                SUM(CASE WHEN evaluated = 0 THEN 1 ELSE 0 END) as pending
            FROM signal_records
        """)
        row = cur.fetchone()
        return {"total": row[0], "evaluated": row[1], "pending": row[2]}

    def close(self):
        self._conn.close()


# ─────────────────────────────────────────────────────────
# ForwardEvaluator — 单信号前瞻评估
# ─────────────────────────────────────────────────────────

class ForwardEvaluator:
    """
    对单条信号进行多窗口前瞻评估。

    核心概念：
    - 多窗口收益率（T+5/10/20）
    - MFE/MAE（最大有利/不利偏移）
    - 方向判定（带中性带）
    - 目标触及检测
    """
    WINDOWS = config.BACKTEST_EVAL_WINDOWS
    NEUTRAL_PCT = config.BACKTEST_NEUTRAL_PCT
    TARGET_PCT = config.BACKTEST_TARGET_PCT

    def evaluate(self, record: dict, forward_bars: list) -> Optional[EvalResult]:
        """
        评估一条信号的前瞻表现。

        :param record: signal_records 字典
        :param forward_bars: 信号日之后的日线 RawBar 列表
        :return: EvalResult 或 None（数据不足）
        """
        if not forward_bars:
            return None

        signal_price = record["price"]
        if signal_price <= 0:
            return None

        is_buy = "买" in record["signal_type"]
        result = EvalResult()
        mfe = 0.0
        mae = 0.0
        mfe_day = 0
        mae_day = 0

        # 遍历每根 K 线，计算收益和偏移
        for i, bar in enumerate(forward_bars):
            day_num = i + 1  # T+1, T+2, ...
            close_ret = (bar.close - signal_price) / signal_price * 100
            if not is_buy:
                close_ret = -close_ret  # 卖信号方向反转

            # 记录窗口收益率
            if day_num == 5:
                result.return_t5 = round(close_ret, 2)
            elif day_num == 10:
                result.return_t10 = round(close_ret, 2)
            elif day_num == 20:
                result.return_t20 = round(close_ret, 2)

            # MFE/MAE（用 high/low 而非 close）
            if is_buy:
                excursion_high = (bar.high - signal_price) / signal_price * 100
                excursion_low = (bar.low - signal_price) / signal_price * 100
            else:
                excursion_high = (signal_price - bar.low) / signal_price * 100
                excursion_low = (signal_price - bar.high) / signal_price * 100

            if excursion_high > mfe:
                mfe = excursion_high
                mfe_day = day_num
            if excursion_low < mae:
                mae = excursion_low
                mae_day = day_num

            # 目标触及（首次）
            if not result.hit_target and excursion_high >= self.TARGET_PCT:
                result.hit_target = 1
                result.days_to_target = day_num

            if day_num >= 20:
                break

        result.mfe = round(mfe, 2)
        result.mae = round(mae, 2)
        result.mfe_day = mfe_day
        result.mae_day = mae_day

        # 方向判定（基于 T+10 或最后可用窗口）
        ref_return = result.return_t10 or result.return_t5
        if ref_return is not None:
            if ref_return > self.NEUTRAL_PCT:
                result.direction_correct = 1
            elif ref_return < -self.NEUTRAL_PCT:
                result.direction_correct = 0
            # 否则 None（中性带内）

        return result


# ─────────────────────────────────────────────────────────
# TradePairMatcher — 买卖配对（FIFO）
# ─────────────────────────────────────────────────────────

class TradePairMatcher:
    """
    FIFO 买卖信号配对。

    同一标的的买信号 → 最近的卖信号，1:1 配对。
    借鉴 quantaxis QA_Performance.pnl_fifo 的 deque 算法。
    """

    def match(self, records: List[dict]) -> List[TradePair]:
        # 按标的分组
        by_symbol: Dict[str, List[dict]] = {}
        for r in records:
            by_symbol.setdefault(r["symbol"], []).append(r)

        pairs = []
        for symbol, recs in by_symbol.items():
            sorted_recs = sorted(recs, key=lambda r: r["signal_date"])
            buy_queue: deque = deque()

            for r in sorted_recs:
                if "买" in r["signal_type"]:
                    buy_queue.append(r)
                elif "卖" in r["signal_type"] and buy_queue:
                    buy_r = buy_queue.popleft()
                    try:
                        buy_dt = datetime.strptime(buy_r["signal_date"], "%Y-%m-%d")
                        sell_dt = datetime.strptime(r["signal_date"], "%Y-%m-%d")
                        holding = (sell_dt - buy_dt).days
                    except (ValueError, TypeError):
                        holding = 0

                    buy_price = buy_r["price"]
                    sell_price = r["price"]
                    ret = (sell_price - buy_price) / buy_price * 100 if buy_price > 0 else 0

                    pairs.append(TradePair(
                        symbol=symbol,
                        buy_record_id=buy_r["id"],
                        sell_record_id=r["id"],
                        buy_date=buy_r["signal_date"],
                        sell_date=r["signal_date"],
                        buy_price=buy_price,
                        sell_price=sell_price,
                        buy_signal_type=buy_r["signal_type"],
                        sell_signal_type=r["signal_type"],
                        holding_days=holding,
                        return_pct=round(ret, 2),
                    ))
        return pairs


# ─────────────────────────────────────────────────────────
# BacktestReport — 双视角统计报告
# ─────────────────────────────────────────────────────────

class BacktestReport:
    """
    双视角分析：
    - 视角 A（群组级）：按信号类型/频率/市场环境分组 → 胜率/PF/Expectancy
    - 视角 B（交易级）：买卖配对 → round-trip 收益/持仓天数/连续盈亏

    独创指标：
    - MFE/MAE ratio（vnpy/quantaxis 均未实现）
    - 信号衰减曲线
    - 置信度校准
    - 信号质量综合评分（SQS）
    - 权重修正建议
    """

    def __init__(self, eval_records: List[dict],
                 trade_pairs: Optional[List[dict]] = None):
        self._records = eval_records
        self._pairs = trade_pairs or []

    # ── 视角 A：群组统计 ──────────────────────────────────

    def _compute_group_stats(self, records: List[dict]) -> GroupStats:
        """计算一组记录的统计指标。"""
        if not records:
            return GroupStats()

        n = len(records)
        wins = [r for r in records if r.get("direction_correct") == 1]
        losses = [r for r in records if r.get("direction_correct") == 0]
        win_count = len(wins)
        loss_count = len(losses)

        wr = win_count / n if n > 0 else 0.0

        # 收益计算（用 return_t10 或 return_t5）
        returns = []
        for r in records:
            ret = r.get("return_t10") or r.get("return_t5") or 0.0
            returns.append(ret)

        pos_returns = [r for r in returns if r > 0]
        neg_returns = [r for r in returns if r < 0]
        total_pos = sum(pos_returns) if pos_returns else 0.0
        total_neg = abs(sum(neg_returns)) if neg_returns else 0.0

        pf = total_pos / total_neg if total_neg > 0 else (
            float("inf") if total_pos > 0 else 0.0)

        avg_win = sum(pos_returns) / len(pos_returns) if pos_returns else 0.0
        avg_loss = abs(sum(neg_returns) / len(neg_returns)) if neg_returns else 0.0
        payoff = avg_win / avg_loss if avg_loss > 0 else (
            float("inf") if avg_win > 0 else 0.0)

        lr = loss_count / n if n > 0 else 0.0
        expectancy = avg_win * wr - avg_loss * lr

        # MFE/MAE
        mfes = [r.get("mfe", 0) or 0 for r in records]
        maes = [r.get("mae", 0) or 0 for r in records]
        avg_mfe = sum(mfes) / n if n > 0 else 0.0
        avg_mae = sum(maes) / n if n > 0 else 0.0
        mfe_mae_ratio = avg_mfe / abs(avg_mae) if avg_mae != 0 else 0.0

        # 多窗口平均收益
        avg_ret = {}
        for w in [5, 10, 20]:
            key = f"return_t{w}"
            vals = [r.get(key) for r in records if r.get(key) is not None]
            avg_ret[w] = sum(vals) / len(vals) if vals else 0.0

        # 连续盈亏
        max_cw, max_cl = _max_consecutive(records)

        return GroupStats(
            count=n,
            win_rate=round(wr * 100, 1),
            profit_factor=round(pf, 2) if pf != float("inf") else 99.9,
            expectancy=round(expectancy, 2),
            payoff_ratio=round(payoff, 2) if payoff != float("inf") else 99.9,
            avg_mfe=round(avg_mfe, 2),
            avg_mae=round(avg_mae, 2),
            mfe_mae_ratio=round(mfe_mae_ratio, 2),
            avg_return={k: round(v, 2) for k, v in avg_ret.items()},
            max_consecutive_wins=max_cw,
            max_consecutive_losses=max_cl,
        )

    def by_signal_type(self) -> Dict[str, GroupStats]:
        groups: Dict[str, list] = {}
        for r in self._records:
            groups.setdefault(r["signal_type"], []).append(r)
        return {k: self._compute_group_stats(v) for k, v in groups.items()}

    def by_freq(self) -> Dict[str, GroupStats]:
        groups: Dict[str, list] = {}
        for r in self._records:
            groups.setdefault(r["freq"], []).append(r)
        return {k: self._compute_group_stats(v) for k, v in groups.items()}

    def by_market_direction(self) -> Dict[str, GroupStats]:
        groups: Dict[str, list] = {}
        for r in self._records:
            groups.setdefault(r.get("market_direction", "分化"), []).append(r)
        return {k: self._compute_group_stats(v) for k, v in groups.items()}

    def by_resonance(self) -> Dict[str, GroupStats]:
        groups: Dict[str, list] = {}
        for r in self._records:
            key = "共振" if r.get("has_resonance") else "单级别"
            groups.setdefault(key, []).append(r)
        return {k: self._compute_group_stats(v) for k, v in groups.items()}

    # ── 独创指标 ──────────────────────────────────────────

    def signal_decay_curve(self) -> Dict[str, Dict[int, float]]:
        """
        信号衰减曲线：买信号和卖信号分别的多窗口平均收益。
        {
          "买": {5: +1.2, 10: +2.1, 20: +2.8},
          "卖": {5: -0.8, 10: -1.5, 20: -2.1},
        }
        曲线拐点 = 信号效力衰减点 = 最佳持仓周期。
        """
        buy_records = [r for r in self._records if "买" in r.get("signal_type", "")]
        sell_records = [r for r in self._records if "卖" in r.get("signal_type", "")]

        result = {}
        for label, recs in [("买", buy_records), ("卖", sell_records)]:
            curve = {}
            for w in [5, 10, 20]:
                key = f"return_t{w}"
                vals = [r.get(key) for r in recs if r.get(key) is not None]
                curve[w] = round(sum(vals) / len(vals), 2) if vals else 0.0
            result[label] = curve
        return result

    def confidence_calibration(self) -> List[Tuple[str, float, float]]:
        """
        置信度校准：检验 detectors.py 的 confidence 设定是否合理。
        返回 [(区间标签, 预测置信度均值, 实际胜率)]
        """
        buckets = {"[0.50,0.65)": [], "[0.65,0.75)": [], "[0.75,0.85)": [],
                    "[0.85,1.00]": []}
        for r in self._records:
            conf = r.get("confidence", 0)
            dc = r.get("direction_correct")
            if dc is None:
                continue
            if conf < 0.65:
                buckets["[0.50,0.65)"].append((conf, dc))
            elif conf < 0.75:
                buckets["[0.65,0.75)"].append((conf, dc))
            elif conf < 0.85:
                buckets["[0.75,0.85)"].append((conf, dc))
            else:
                buckets["[0.85,1.00]"].append((conf, dc))

        result = []
        for label, items in buckets.items():
            if not items:
                continue
            avg_conf = sum(c for c, _ in items) / len(items)
            actual_wr = sum(dc for _, dc in items) / len(items) * 100
            result.append((label, round(avg_conf * 100, 1), round(actual_wr, 1)))
        return result

    def mfe_mae_analysis(self) -> Dict[str, dict]:
        """按信号类型的 MFE/MAE 分析。"""
        groups: Dict[str, list] = {}
        for r in self._records:
            groups.setdefault(r["signal_type"], []).append(r)

        result = {}
        for sig_type, recs in groups.items():
            mfes = [r.get("mfe", 0) or 0 for r in recs]
            maes = [r.get("mae", 0) or 0 for r in recs]
            n = len(recs)
            avg_mfe = sum(mfes) / n if n else 0
            avg_mae = sum(maes) / n if n else 0
            ratio = avg_mfe / abs(avg_mae) if avg_mae != 0 else 0
            result[sig_type] = {
                "count": n,
                "avg_mfe": round(avg_mfe, 2),
                "avg_mae": round(avg_mae, 2),
                "mfe_mae_ratio": round(ratio, 2),
            }
        return result

    def signal_quality_score(self) -> Dict[str, float]:
        """
        信号质量综合评分（SQS）。

        SQS = 30 * norm(win_rate)
            + 25 * norm(profit_factor)
            + 25 * norm(mfe_mae_ratio)
            + 20 * norm(expectancy)

        归一化到 0~100 分。
        """
        by_type = self.by_signal_type()
        if not by_type:
            return {}

        # 收集原始值
        raw = {}
        for sig_type, stats in by_type.items():
            raw[sig_type] = {
                "wr": stats.win_rate,
                "pf": min(stats.profit_factor, 10),  # 上限 cap
                "mfe_mae": min(stats.mfe_mae_ratio, 5),
                "exp": stats.expectancy,
            }

        # 各维度范围
        def _norm(val, lo, hi):
            if hi <= lo:
                return 0.5
            return max(0, min(1, (val - lo) / (hi - lo)))

        sqs = {}
        for sig_type, v in raw.items():
            score = (
                30 * _norm(v["wr"], 30, 80) +
                25 * _norm(v["pf"], 0.5, 4.0) +
                25 * _norm(v["mfe_mae"], 0.5, 3.0) +
                20 * _norm(v["exp"], -2, 4)
            )
            sqs[sig_type] = round(score, 0)
        return sqs

    def weight_recommendation(self) -> Dict[str, Tuple[int, int, str]]:
        """
        基于 SQS 排名，推荐 SIGNAL_WEIGHTS 调整。
        返回 {信号类型: (当前权重, 建议权重, 调整说明)}
        """
        from signals.core.scorer import SIGNAL_WEIGHTS

        sqs = self.signal_quality_score()
        if not sqs:
            return {}

        # SQS → 权重映射
        recommendations = {}
        for sig_type, score in sqs.items():
            current = abs(SIGNAL_WEIGHTS.get(sig_type, 0))
            if current == 0:
                continue

            # 权重调整逻辑
            if score >= 70:
                suggested = min(current + 5, 65)
                note = "SQS 优秀"
            elif score >= 50:
                suggested = current  # 保持
                note = "SQS 合格"
            elif score >= 30:
                suggested = max(current - 5, 15)
                note = "SQS 偏低"
            else:
                suggested = max(current - 10, 10)
                note = "SQS 差"

            # 保留原始正负号
            is_sell = SIGNAL_WEIGHTS.get(sig_type, 0) < 0
            diff = suggested - current
            if diff > 0:
                adj = f"+{diff}"
            elif diff < 0:
                adj = str(diff)
            else:
                adj = "不变"

            recommendations[sig_type] = (current, suggested, f"{note}, {adj}")

        return recommendations

    # ── 视角 B：买卖配对统计 ──────────────────────────────

    def trade_pair_summary(self) -> dict:
        """买卖配对统计。"""
        if not self._pairs:
            return {"pair_count": 0}

        returns = [p["return_pct"] for p in self._pairs]
        holdings = [p["holding_days"] for p in self._pairs]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r < 0]

        # 连续盈亏
        max_cw = max_cl = cur_cw = cur_cl = 0
        for r in returns:
            if r > 0:
                cur_cw += 1
                cur_cl = 0
            elif r < 0:
                cur_cl += 1
                cur_cw = 0
            else:
                cur_cw = cur_cl = 0
            max_cw = max(max_cw, cur_cw)
            max_cl = max(max_cl, cur_cl)

        return {
            "pair_count": len(self._pairs),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
            "avg_holding_days": round(sum(holdings) / len(holdings), 1),
            "win_count": len(wins),
            "loss_count": len(losses),
            "max_consecutive_wins": max_cw,
            "max_consecutive_losses": max_cl,
            "best_pair": max(self._pairs, key=lambda p: p["return_pct"]),
            "worst_pair": min(self._pairs, key=lambda p: p["return_pct"]),
        }

    # ── 输出 ──────────────────────────────────────────────

    def print_full_report(self):
        """输出完整的验证报告。"""
        n = len(self._records)
        if n == 0:
            print("\n  无已评估信号，请先运行 screener/review 积累信号后等待 20 天。")
            return

        print(f"\n{'═'*70}")
        print(f"  🐲 信号前瞻验证报告  |  样本: {n}  |  窗口: T+5/10/20")
        print(f"{'═'*70}")

        # ── 按信号类型 ────────────────────────────────────
        print(f"\n  ── 视角 A：按信号类型 {'─'*46}")
        sqs = self.signal_quality_score()
        by_type = self.by_signal_type()

        # 表头
        print(f"  {'信号':<8} {'样本':>4} {'胜率':>5} {'PF':>5} "
              f"{'期望值':>6} {'MFE均':>5} {'MAE均':>5} {'MFE/MAE':>7} {'SQS':>4}")
        print(f"  {'─'*62}")

        # 按 SQS 降序
        sorted_types = sorted(by_type.items(),
                              key=lambda x: sqs.get(x[0], 0), reverse=True)
        for sig_type, stats in sorted_types:
            score = sqs.get(sig_type, 0)
            print(f"  {sig_type:<8} {stats.count:>4} "
                  f"{stats.win_rate:>4.1f}% {stats.profit_factor:>5.2f} "
                  f"{stats.expectancy:>+5.2f}% "
                  f"{stats.avg_mfe:>+4.1f} {stats.avg_mae:>+4.1f} "
                  f"{stats.mfe_mae_ratio:>6.2f} {score:>4.0f}")

        # ── 信号衰减曲线 ─────────────────────────────────
        decay = self.signal_decay_curve()
        if decay:
            print(f"\n  ── 信号衰减曲线 {'─'*50}")
            print(f"  {'':8} {'T+5':>6} {'T+10':>6} {'T+20':>6}")
            for label, curve in decay.items():
                vals = "  ".join(f"{curve.get(w, 0):>+5.1f}%" for w in [5, 10, 20])
                peak_w = max(curve, key=lambda w: abs(curve[w])) if curve else 0
                print(f"  {label+'信号':<8} {vals}   ← T+{peak_w} 最优")

        # ── 按市场环境 ────────────────────────────────────
        by_dir = self.by_market_direction()
        if len(by_dir) > 1:
            print(f"\n  ── 按市场环境 {'─'*52}")
            for direction, stats in sorted(by_dir.items(),
                                           key=lambda x: -x[1].win_rate):
                print(f"  {direction}: {stats.count}条  "
                      f"胜率 {stats.win_rate:.1f}%  PF {stats.profit_factor:.2f}  "
                      f"期望值 {stats.expectancy:+.2f}%")

        # ── 置信度校准 ────────────────────────────────────
        cal = self.confidence_calibration()
        if cal:
            print(f"\n  ── 置信度校准 {'─'*52}")
            print(f"  {'预测区间':<14} {'实际胜率':>8} {'偏差':>8}")
            for label, avg_conf, actual_wr in cal:
                diff = actual_wr - avg_conf
                marker = "过度自信 ⚠️" if diff < -10 else (
                    "偏保守 ↑" if diff > 10 else "基本准确 ✓")
                print(f"  {label:<14} {actual_wr:>7.1f}% {diff:>+7.1f}%  {marker}")

        # ── 共振 vs 单级别 ────────────────────────────────
        by_res = self.by_resonance()
        if len(by_res) > 1:
            print(f"\n  ── 共振 vs 单级别 {'─'*48}")
            for label, stats in by_res.items():
                print(f"  {label}: {stats.count}条  "
                      f"胜率 {stats.win_rate:.1f}%  PF {stats.profit_factor:.2f}  "
                      f"MFE/MAE {stats.mfe_mae_ratio:.2f}")

        # ── 视角 B：买卖配对 ──────────────────────────────
        if self._pairs:
            ps = self.trade_pair_summary()
            print(f"\n  ── 视角 B：买卖配对 (FIFO) {'─'*40}")
            print(f"  完整配对: {ps['pair_count']} 组  |  "
                  f"平均持仓: {ps['avg_holding_days']:.0f} 天  |  "
                  f"平均收益: {ps['avg_return_pct']:+.1f}%")
            print(f"  最大连续盈: {ps['max_consecutive_wins']}  |  "
                  f"最大连续亏: {ps['max_consecutive_losses']}")
            best = ps.get("best_pair", {})
            if best:
                print(f"  最佳: {best.get('symbol','')} "
                      f"{best.get('buy_signal_type','')}→{best.get('sell_signal_type','')} "
                      f"{best.get('return_pct',0):+.1f}% ({best.get('holding_days',0)}日)")

        # ── 权重建议 ──────────────────────────────────────
        recs = self.weight_recommendation()
        if recs:
            print(f"\n  ── 权重建议（基于 SQS）{'─'*44}")
            print(f"  {'信号':<8} {'当前':>4} {'建议':>4} {'说明'}")
            print(f"  {'─'*50}")
            for sig_type, (current, suggested, note) in sorted(
                    recs.items(), key=lambda x: -sqs.get(x[0], 0)):
                print(f"  {sig_type:<8} {current:>4} {suggested:>4}  {note}")

        print(f"\n{'═'*70}")


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

def _max_consecutive(records: List[dict]) -> Tuple[int, int]:
    """计算最大连续盈/连续亏次数。"""
    max_cw = max_cl = cur_cw = cur_cl = 0
    for r in records:
        dc = r.get("direction_correct")
        if dc == 1:
            cur_cw += 1
            cur_cl = 0
        elif dc == 0:
            cur_cl += 1
            cur_cw = 0
        else:
            cur_cw = cur_cl = 0
        max_cw = max(max_cw, cur_cw)
        max_cl = max(max_cl, cur_cl)
    return max_cw, max_cl


# ─────────────────────────────────────────────────────────
# 信号存档钩子（供 screener / review_screener 调用）
# ─────────────────────────────────────────────────────────

def archive_signals(scored_symbols: list, market_direction: str = "分化"):
    """
    将 ScoredSymbol 列表中的信号存入 SQLite。

    在 screener.scan_once() 和 review_screener.review_stock_daily() 末尾调用。
    try/except 包裹，不影响主流程。
    """
    try:
        records = []
        for sc in scored_symbols:
            if not sc.signals:
                continue

            # 检测共振
            buy_freqs = {s.freq for s in sc.signals if "买" in s.signal_type}
            has_resonance = 1 if len(buy_freqs) > 1 else 0

            for sig in sc.signals:
                records.append(SignalRecord(
                    symbol=sc.symbol,
                    signal_date=sig.dt.strftime("%Y-%m-%d") if hasattr(sig.dt, "strftime")
                                else str(sig.dt)[:10],
                    signal_type=sig.signal_type,
                    freq=sig.freq,
                    confidence=sig.confidence,
                    price=sig.price,
                    total_score=sc.total_score,
                    market_direction=market_direction,
                    has_resonance=has_resonance,
                    details=sig.details,
                ))

        if records:
            journal = SignalJournal()
            n = journal.log_batch(records)
            journal.close()
            if n > 0:
                print(f"  [回测] 存档 {n} 条新信号（共 {len(records)} 条，去重后新增 {n}）")
    except Exception as e:
        print(f"  [回测] 信号存档异常（不影响主流程）: {e}")


# ─────────────────────────────────────────────────────────
# 回测主流程（供 run.py --mode backtest 调用）
# ─────────────────────────────────────────────────────────

def run_backtest(signal_type: str = "", freq_filter: str = ""):
    """
    回测验证主流程：
    1. 从 SQLite 取待评估信号
    2. 获取每条信号之后 20 天的日线数据
    3. 用 ForwardEvaluator 计算前瞻收益 + MFE/MAE
    4. 买卖配对
    5. 生成统计报告
    """
    journal = SignalJournal()
    summary = journal.summary()
    print(f"\n  信号数据库: 总计 {summary['total']} 条 | "
          f"已评估 {summary['evaluated']} | 待评估 {summary['pending']}")

    # Step 1: 评估待评估信号
    pending = journal.get_pending()
    if pending:
        print(f"\n  评估 {len(pending)} 条到期信号 ...")
        evaluator = ForwardEvaluator()
        _evaluate_pending(journal, evaluator, pending)

    # Step 2: 买卖配对
    all_records = journal.get_all_records()
    matcher = TradePairMatcher()
    pairs = matcher.match(all_records)
    if pairs:
        journal.save_trade_pairs(pairs)

    # Step 3: 生成报告
    filters = {}
    if signal_type:
        filters["signal_type"] = signal_type
    if freq_filter:
        filters["freq"] = freq_filter

    eval_records = journal.get_evaluated(**filters)
    trade_pairs = journal.get_trade_pairs()

    report = BacktestReport(eval_records, trade_pairs)
    report.print_full_report()

    journal.close()


def _evaluate_pending(journal: SignalJournal, evaluator: ForwardEvaluator,
                      pending: List[dict]):
    """评估待评估信号。"""
    from signals.data.fetcher import AKShareSource, USDataSource, detect_market

    ak_src = AKShareSource()
    us_src = None
    evaluated = 0

    for record in pending:
        symbol = record["symbol"]
        signal_date = record["signal_date"]

        try:
            market = detect_market(symbol)
            if market == "US":
                if us_src is None:
                    us_src = USDataSource()
                bars = us_src.get_us_daily(symbol)
            else:
                bars = ak_src.get_a_daily(symbol, sdt=signal_date)

            if not bars:
                continue

            # 找到信号日之后的 K 线
            forward_bars = []
            found_signal_date = False
            for bar in bars:
                bar_date = bar.dt.strftime("%Y-%m-%d") if hasattr(bar.dt, "strftime") \
                    else str(bar.dt)[:10]
                if bar_date == signal_date:
                    found_signal_date = True
                    continue
                if found_signal_date:
                    forward_bars.append(bar)
                    if len(forward_bars) >= 20:
                        break

            if len(forward_bars) < 5:
                continue

            result = evaluator.evaluate(record, forward_bars)
            if result:
                journal.update_evaluation(record["id"], result)
                evaluated += 1
                dc_str = {1: "✓", 0: "✗", None: "~"}.get(result.direction_correct, "~")
                print(f"    {symbol} {record['signal_type']}({record['freq']}) "
                      f"T+10={result.return_t10 or 0:+.1f}% "
                      f"MFE={result.mfe:+.1f}% MAE={result.mae:+.1f}% "
                      f"方向{dc_str}")

        except Exception as e:
            print(f"    {symbol} 评估失败: {e}")

    print(f"  完成评估 {evaluated}/{len(pending)} 条信号")
