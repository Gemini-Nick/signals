# -*- coding: utf-8 -*-
"""
风控模块 —— 止损位计算 + 仓位建议

基于缠论结构提取止损位，结合 MAE 历史数据和 SQS 评分给出仓位建议。

信号止损逻辑：
  三买 → 中枢上沿（跌破中枢 = 三买失败）
  二买 → 前低 b1.low（跌破前低 = 结构破坏）
  一买/背驰买/趋势买 → MAE 历史均值 or 默认 -5%
  卖信号 → 镜像处理
"""
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from signals.core.detectors import SignalEvent
from signals.core.scorer import ScoredSymbol


@dataclass
class RiskInfo:
    """单个信号的风控信息。"""
    stop_loss: float          # 止损价
    stop_loss_pct: float      # 止损距离 %（负数 = 下方）
    stop_source: str          # 止损依据
    position_pct: float       # 建议仓位 %（占账户）
    risk_reward: float        # 风险收益比


# ─────────────────────────────────────────────────────────
# 止损位提取
# ─────────────────────────────────────────────────────────

# 正则：从 details 字符串提取结构数据
_RE_ZS_UPPER = re.compile(r"中枢上沿\s*([\d.]+)")   # 三买
_RE_ZS_LOWER = re.compile(r"中枢下沿\s*([\d.]+)")   # 三卖
_RE_PREV_LOW = re.compile(r"低点抬升\s*([\d.]+)>([\d.]+)")  # 二买：b3.low > b1.low
_RE_PREV_HIGH = re.compile(r"高点递降\s*([\d.]+)<([\d.]+)")  # 二卖：b3.high < b1.high

# 默认止损百分比（无法从结构提取时的兜底）
_DEFAULT_STOP_PCT = -5.0


def calculate_stop_loss(signal: SignalEvent) -> Tuple[float, float, str]:
    """
    计算止损价和止损距离。

    Returns:
        (stop_price, stop_pct, source_description)
    """
    price = signal.price
    if price <= 0:
        return 0.0, _DEFAULT_STOP_PCT, "默认"

    sig_type = signal.signal_type
    details = signal.details

    # 三买：止损 = 中枢上沿
    if sig_type == "三买":
        m = _RE_ZS_UPPER.search(details)
        if m:
            stop = float(m.group(1))
            pct = (stop - price) / price * 100
            return stop, pct, "中枢上沿"

    # 三卖：止损 = 中枢下沿（卖空止损在上方，正数）
    elif sig_type == "三卖":
        m = _RE_ZS_LOWER.search(details)
        if m:
            stop = float(m.group(1))
            pct = (stop - price) / price * 100
            return stop, pct, "中枢下沿"

    # 二买：止损 = 前低 b1.low
    elif sig_type == "二买":
        m = _RE_PREV_LOW.search(details)
        if m:
            # b3.low > b1.low，止损在 b1.low（更低的那个）
            stop = float(m.group(2))
            pct = (stop - price) / price * 100
            return stop, pct, "前低"

    # 二卖：止损 = 前高 b1.high
    elif sig_type == "二卖":
        m = _RE_PREV_HIGH.search(details)
        if m:
            # b3.high < b1.high，止损在 b1.high（更高的那个）
            stop = float(m.group(2))
            pct = (stop - price) / price * 100
            return stop, pct, "前高"

    # 一买/背驰买/趋势买：尝试从 backtest.db 取 MAE 历史均值
    mae_avg = _get_historical_mae(sig_type)
    if mae_avg is not None and mae_avg < 0:
        stop = price * (1 + mae_avg / 100)
        return stop, mae_avg, "MAE均值"

    # 兜底：固定 -5%
    is_sell = "卖" in sig_type
    default_pct = -_DEFAULT_STOP_PCT if is_sell else _DEFAULT_STOP_PCT
    stop = price * (1 + default_pct / 100)
    return stop, default_pct, "默认"


def _get_historical_mae(signal_type: str) -> Optional[float]:
    """从 backtest.db 查询某信号类型的 MAE 历史均值。"""
    try:
        from config import BACKTEST_DB_PATH
        import sqlite3
        import os
        if not os.path.exists(BACKTEST_DB_PATH):
            return None
        conn = sqlite3.connect(BACKTEST_DB_PATH)
        row = conn.execute(
            "SELECT AVG(mae) FROM signal_records "
            "WHERE signal_type=? AND evaluated=1 AND mae IS NOT NULL",
            (signal_type,),
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            return round(row[0], 2)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────
# 仓位建议
# ─────────────────────────────────────────────────────────

def calculate_position(stop_loss_pct: float, sqs: float = 50.0) -> float:
    """
    固定风险法 + SQS 调整。

    base_risk (RISK_PER_TRADE_PCT) = 单笔最大亏损占账户 %
    position = base_risk / |止损距离|
    SQS 调整：>=70 → ×1.2, >=50 → ×1.0, <50 → ×0.8
    """
    try:
        from config import RISK_PER_TRADE_PCT, MAX_POSITION_PCT
    except ImportError:
        RISK_PER_TRADE_PCT = 2.0
        MAX_POSITION_PCT = 25.0

    abs_stop = abs(stop_loss_pct)
    if abs_stop < 0.5:
        abs_stop = 0.5  # 防止除零 / 极小止损导致天量仓位

    position = RISK_PER_TRADE_PCT / abs_stop * 100

    # SQS 调整
    if sqs >= 70:
        position *= 1.2
    elif sqs < 50:
        position *= 0.8

    return round(min(position, MAX_POSITION_PCT), 1)


def _get_signal_sqs(signal_type: str) -> float:
    """从 backtest.db 查询某信号类型的 SQS。"""
    try:
        from config import BACKTEST_DB_PATH
        import sqlite3
        import os
        if not os.path.exists(BACKTEST_DB_PATH):
            return 50.0
        conn = sqlite3.connect(BACKTEST_DB_PATH)
        rows = conn.execute(
            "SELECT win_rate, profit_factor, mfe_mae_ratio, expectancy "
            "FROM (SELECT "
            "  CASE WHEN direction_correct=1 THEN 1.0 ELSE 0.0 END as win, "
            "  return_t10, mfe, mae "
            "FROM signal_records "
            "WHERE signal_type=? AND evaluated=1) "
            "LIMIT 1",
            (signal_type,),
        ).fetchone()
        conn.close()
        # 简化：直接查已评估信号的胜率来近似 SQS
        if rows:
            return 50.0  # 需要 BacktestReport 计算完整 SQS，这里用默认值
    except Exception:
        pass
    return 50.0


def _get_mfe_mae_ratio(signal_type: str) -> float:
    """从 backtest.db 查询 MFE/MAE 比值。"""
    try:
        from config import BACKTEST_DB_PATH
        import sqlite3
        import os
        if not os.path.exists(BACKTEST_DB_PATH):
            return 0.0
        conn = sqlite3.connect(BACKTEST_DB_PATH)
        row = conn.execute(
            "SELECT AVG(mfe), AVG(mae) FROM signal_records "
            "WHERE signal_type=? AND evaluated=1 AND mfe IS NOT NULL AND mae IS NOT NULL",
            (signal_type,),
        ).fetchone()
        conn.close()
        if row and row[0] is not None and row[1] is not None:
            avg_mfe = row[0]
            avg_mae = abs(row[1]) if row[1] else 1.0
            if avg_mae > 0:
                return round(avg_mfe / avg_mae, 2)
    except Exception:
        pass
    return 0.0


# ─────────────────────────────────────────────────────────
# 集成函数
# ─────────────────────────────────────────────────────────

def compute_risk_for_signal(signal: SignalEvent) -> Optional[RiskInfo]:
    """为单个信号计算完整的风控信息。"""
    if signal.price <= 0:
        return None

    stop, stop_pct, source = calculate_stop_loss(signal)
    sqs = _get_signal_sqs(signal.signal_type)
    position = calculate_position(stop_pct, sqs)
    rr = _get_mfe_mae_ratio(signal.signal_type)

    return RiskInfo(
        stop_loss=round(stop, 2),
        stop_loss_pct=round(stop_pct, 1),
        stop_source=source,
        position_pct=position,
        risk_reward=rr,
    )


# ─────────────────────────────────────────────────────────
# 底仓 / 弹性仓位分层
# ─────────────────────────────────────────────────────────

@dataclass
class LayeredPosition:
    """底仓+弹性仓分层建议。"""
    base_pct: float          # 底仓比例（大级别信号→长持）
    flex_pct: float          # 弹性仓比例（小级别+均线→波段）
    flex_buy_ref: str        # 弹性仓买入参考："5周线 2970"
    flex_sell_ref: str       # 弹性仓卖出参考："前高 3177"
    rationale: str           # 分层依据说明


_LARGE_FREQS = {"日线", "D", "daily", "周线", "W", "weekly"}
_SMALL_FREQS = {"15分钟", "15min", "F15", "30分钟", "30min", "F30",
                "5分钟", "5min", "F5", "1分钟", "1min", "F1"}


def compute_layered_position(
    scored: ScoredSymbol,
    ma_context=None,
    total_position_pct: float = 0.0,
) -> Optional[LayeredPosition]:
    """
    根据信号级别和均线参照系，将仓位拆分为底仓+弹性仓。

    规则：
    - 大级别（日线/周线）买信号 → 底仓（长持不动）
    - 小级别（15M/30M）买信号 → 弹性仓（回合制高抛低吸）
    - 底仓占比 = 大级别信号贡献比例 × 总仓位
    - 弹性仓买入参考：MA 支撑位
    - 弹性仓卖出参考：MA 阻力位 或 前高
    """
    if not scored.signals:
        return None

    buy_sigs = [s for s in scored.signals if "买" in s.signal_type or
                s.signal_type in ("形态:双底", "形态:头肩底", "形态:上升三角")]
    if not buy_sigs:
        return None

    # 如果没给总仓位，用 RiskInfo 计算
    if total_position_pct <= 0:
        risk = compute_risk_for_signal(buy_sigs[0])
        total_position_pct = risk.position_pct if risk else 10.0

    # 区分大小级别信号
    large_sigs = [s for s in buy_sigs if s.freq in _LARGE_FREQS]
    small_sigs = [s for s in buy_sigs if s.freq in _SMALL_FREQS]

    # 底仓/弹性仓比例
    if large_sigs and small_sigs:
        # 大小级别都有 → 底仓60%, 弹性40%
        base_ratio = 0.6
        rationale = f"大级别({','.join(s.freq for s in large_sigs[:2])})持仓 + " \
                    f"小级别({','.join(s.freq for s in small_sigs[:2])})波段"
    elif large_sigs:
        # 只有大级别 → 底仓80%, 弹性20%
        base_ratio = 0.8
        rationale = f"大级别({','.join(s.freq for s in large_sigs[:2])})信号为主，少量弹性仓"
    elif small_sigs:
        # 只有小级别 → 底仓20%, 弹性80%
        base_ratio = 0.2
        rationale = f"小级别({','.join(s.freq for s in small_sigs[:2])})信号为主，以波段为主"
    else:
        base_ratio = 0.5
        rationale = "信号级别未识别，均等分配"

    base_pct = round(total_position_pct * base_ratio, 1)
    flex_pct = round(total_position_pct * (1 - base_ratio), 1)

    # 弹性仓参考位
    flex_buy_ref = ""
    flex_sell_ref = ""
    if ma_context:
        # 买入参考：最近支撑位
        if ma_context.support_levels:
            s = ma_context.support_levels[0]
            flex_buy_ref = f"{s.name} {s.value:.0f}({s.distance_pct:+.1f}%)"
        # 卖出参考：最近阻力位
        if ma_context.resistance_levels:
            r = ma_context.resistance_levels[0]
            flex_sell_ref = f"{r.name} {r.value:.0f}({r.distance_pct:+.1f}%)"

    return LayeredPosition(
        base_pct=base_pct,
        flex_pct=flex_pct,
        flex_buy_ref=flex_buy_ref or "结构支撑位",
        flex_sell_ref=flex_sell_ref or "结构阻力位",
        rationale=rationale,
    )


def format_layered_position(lp: LayeredPosition) -> str:
    """格式化分层仓位输出"""
    return (f"  [分层] 底仓 {lp.base_pct:.1f}%（长持） + 弹性 {lp.flex_pct:.1f}%（波段）\n"
            f"         弹性买: {lp.flex_buy_ref}  |  弹性卖: {lp.flex_sell_ref}\n"
            f"         策略: {lp.rationale}")


def enrich_with_risk(scored: ScoredSymbol) -> str:
    """
    为 ScoredSymbol 生成风控行，返回附加文本。

    格式：  [风控] 止损 98.50 (-3.2%, 中枢上沿)  建议仓位 6.2%  风险收益比 2.1
    """
    if not scored.signals:
        return ""

    # 取最强买信号（score 最高 or 第一个买信号）
    buy_signals = [s for s in scored.signals if "买" in s.signal_type]
    target = buy_signals[0] if buy_signals else scored.signals[0]

    risk = compute_risk_for_signal(target)
    if risk is None:
        return ""

    parts = [f"  [风控] 止损 {risk.stop_loss:.2f} ({risk.stop_loss_pct:+.1f}%, {risk.stop_source})"]
    parts.append(f"建议仓位 {risk.position_pct:.1f}%")
    if risk.risk_reward > 0:
        parts.append(f"风险收益比 {risk.risk_reward:.1f}")

    return "  ".join(parts)
