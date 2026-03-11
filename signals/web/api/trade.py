# -*- coding: utf-8 -*-
"""
交易日志 API

端点:
- GET  /api/trade/list        — 交易列表
- GET  /api/trade/{id}        — 单条交易详情
- POST /api/trade/add         — 添加交易
- PUT  /api/trade/{id}        — 更新交易
- POST /api/trade/{id}/close  — 平仓
- POST /api/trade/{id}/score  — 评分
- DELETE /api/trade/{id}      — 删除
- GET  /api/trade/summary     — 统计
- GET  /api/trade/missed      — 遗漏信号
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from signals.core.trade_log import (
    TradeRecord, MissedSignal, get_trade_log,
)

router = APIRouter(prefix="/api/trade", tags=["trade"])


class TradeInput(BaseModel):
    symbol: str
    name: str = ""
    direction: str = "long"
    entry_date: str = ""
    entry_price: float = 0.0
    entry_reason: str = ""
    entry_signal: str = ""
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    position_pct: float = 0.0
    shares: int = 0
    notes: str = ""
    tags: str = ""


class CloseInput(BaseModel):
    exit_price: float
    exit_date: str = ""
    exit_reason: str = ""


class ScoreInput(BaseModel):
    timing: int = 0
    position: int = 0
    exit: int = 0
    error_type: str = ""


def _trade_to_dict(t: TradeRecord) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "name": t.name,
        "direction": t.direction,
        "entry_date": t.entry_date,
        "entry_price": t.entry_price,
        "entry_reason": t.entry_reason,
        "entry_signal": t.entry_signal,
        "exit_date": t.exit_date,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "position_pct": t.position_pct,
        "shares": t.shares,
        "timing_score": t.timing_score,
        "position_score": t.position_score,
        "exit_score": t.exit_score,
        "total_score": t.total_score,
        "error_type": t.error_type,
        "pnl_pct": t.pnl_pct,
        "pnl_amount": t.pnl_amount,
        "holding_days": t.holding_days,
        "is_open": t.is_open,
        "notes": t.notes,
        "tags": t.tags,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


@router.get("/list")
def list_trades(status: str = "all", limit: int = 50, offset: int = 0):
    log = get_trade_log()
    trades = log.list_trades(status=status, limit=limit, offset=offset)
    return {
        "trades": [_trade_to_dict(t) for t in trades],
        "total": len(trades),
    }


@router.get("/summary")
def get_summary(period: str = ""):
    log = get_trade_log()
    s = log.get_summary(period=period)
    return {
        "period": s.period,
        "total_trades": s.total_trades,
        "win_count": s.win_count,
        "loss_count": s.loss_count,
        "win_rate": s.win_rate,
        "avg_pnl_pct": s.avg_pnl_pct,
        "max_win_pct": s.max_win_pct,
        "max_loss_pct": s.max_loss_pct,
        "total_pnl": s.total_pnl,
        "avg_holding_days": s.avg_holding_days,
        "avg_score": s.avg_score,
        "error_counts": s.error_counts,
    }


@router.get("/missed")
def list_missed():
    log = get_trade_log()
    signals = log.list_missed_signals(limit=30)
    return {
        "missed": [
            {
                "symbol": s.symbol,
                "name": s.name,
                "signal_type": s.signal_type,
                "signal_date": s.signal_date,
                "signal_price": s.signal_price,
                "current_price": s.current_price,
                "max_price_after": s.max_price_after,
                "potential_pnl_pct": s.potential_pnl_pct,
            }
            for s in signals
        ],
    }


@router.get("/{trade_id}")
def get_trade(trade_id: int):
    log = get_trade_log()
    trade = log.get_trade(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return _trade_to_dict(trade)


@router.post("/add")
def add_trade(data: TradeInput):
    log = get_trade_log()
    record = TradeRecord(
        symbol=data.symbol,
        name=data.name,
        direction=data.direction,
        entry_date=data.entry_date,
        entry_price=data.entry_price,
        entry_reason=data.entry_reason,
        entry_signal=data.entry_signal,
        exit_date=data.exit_date,
        exit_price=data.exit_price,
        exit_reason=data.exit_reason,
        position_pct=data.position_pct,
        shares=data.shares,
        notes=data.notes,
        tags=data.tags,
    )
    trade_id = log.add_trade(record)
    return {"id": trade_id, "message": "Trade added"}


@router.put("/{trade_id}")
def update_trade(trade_id: int, data: TradeInput):
    log = get_trade_log()
    trade = log.get_trade(trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    trade.symbol = data.symbol
    trade.name = data.name
    trade.direction = data.direction
    trade.entry_date = data.entry_date
    trade.entry_price = data.entry_price
    trade.entry_reason = data.entry_reason
    trade.entry_signal = data.entry_signal
    trade.exit_date = data.exit_date
    trade.exit_price = data.exit_price
    trade.exit_reason = data.exit_reason
    trade.position_pct = data.position_pct
    trade.shares = data.shares
    trade.notes = data.notes
    trade.tags = data.tags

    log.update_trade(trade)
    return {"message": "Trade updated"}


@router.post("/{trade_id}/close")
def close_trade(trade_id: int, data: CloseInput):
    log = get_trade_log()
    ok = log.close_trade(trade_id, data.exit_price, data.exit_date, data.exit_reason)
    if not ok:
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"message": "Trade closed"}


@router.post("/{trade_id}/score")
def score_trade(trade_id: int, data: ScoreInput):
    log = get_trade_log()
    ok = log.score_trade(
        trade_id,
        timing=data.timing,
        position=data.position,
        exit=data.exit,
        error_type=data.error_type,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"message": "Trade scored"}


@router.delete("/{trade_id}")
def delete_trade(trade_id: int):
    log = get_trade_log()
    log.delete_trade(trade_id)
    return {"message": "Trade deleted"}
