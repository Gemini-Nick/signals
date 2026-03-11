# -*- coding: utf-8 -*-
"""
回测验证 API — 信号统计报告 + 买卖配对
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


def _open_journal():
    from signals.core.backtest import SignalJournal
    return SignalJournal()


def _serialize_group_stats(stats) -> dict:
    return {
        "count": stats.count,
        "win_rate": stats.win_rate,
        "profit_factor": min(stats.profit_factor, 99.9),
        "expectancy": stats.expectancy,
        "payoff_ratio": min(stats.payoff_ratio, 99.9),
        "avg_mfe": stats.avg_mfe,
        "avg_mae": stats.avg_mae,
        "mfe_mae_ratio": stats.mfe_mae_ratio,
        "avg_return": stats.avg_return,
        "max_consecutive_wins": stats.max_consecutive_wins,
        "max_consecutive_losses": stats.max_consecutive_losses,
    }


@router.get("/summary")
async def backtest_summary():
    """DB 概要统计"""
    try:
        journal = _open_journal()
        s = journal.summary()
        journal.close()
        return s
    except Exception as e:
        return {"total": 0, "evaluated": 0, "pending": 0, "error": str(e)}


@router.get("/report")
async def backtest_report(
    signal_type: str = Query("", description="筛选信号类型"),
    freq: str = Query("", description="筛选频率"),
):
    """完整回测报告"""
    try:
        from signals.core.backtest import SignalJournal, BacktestReport

        journal = SignalJournal()
        filters = {}
        if signal_type:
            filters["signal_type"] = signal_type
        if freq:
            filters["freq"] = freq

        eval_records = journal.get_evaluated(**filters)
        trade_pairs = journal.get_trade_pairs()
        journal.close()

        if not eval_records:
            return {"empty": True, "message": "暂无已评估信号"}

        report = BacktestReport(eval_records, trade_pairs)

        # 按信号类型
        by_type = {k: _serialize_group_stats(v)
                   for k, v in report.by_signal_type().items()}

        # 按频率
        by_freq = {k: _serialize_group_stats(v)
                   for k, v in report.by_freq().items()}

        # 按市场环境
        by_direction = {k: _serialize_group_stats(v)
                        for k, v in report.by_market_direction().items()}

        # 共振 vs 单级别
        by_resonance = {k: _serialize_group_stats(v)
                        for k, v in report.by_resonance().items()}

        # 衰减曲线
        decay = report.signal_decay_curve()

        # 置信度校准
        calibration = [
            {"bucket": label, "avg_confidence": avg_conf, "actual_win_rate": actual_wr}
            for label, avg_conf, actual_wr in report.confidence_calibration()
        ]

        # MFE/MAE 分析
        mfe_mae = report.mfe_mae_analysis()

        # SQS
        sqs = report.signal_quality_score()

        # 权重建议
        weight_rec = {}
        for sig_type, (current, suggested, note) in report.weight_recommendation().items():
            weight_rec[sig_type] = {
                "current": current, "suggested": suggested, "note": note,
            }

        # 总体 KPI（从全量 eval_records 算）
        total_count = len(eval_records)
        wins = sum(1 for r in eval_records if r.get("direction_correct") == 1)
        losses = sum(1 for r in eval_records if r.get("direction_correct") == 0)
        overall_wr = round(wins / total_count * 100, 1) if total_count else 0
        returns = [r.get("return_t10") or r.get("return_t5") or 0 for r in eval_records]
        pos_sum = sum(r for r in returns if r > 0)
        neg_sum = abs(sum(r for r in returns if r < 0))
        overall_pf = round(pos_sum / neg_sum, 2) if neg_sum > 0 else 0
        avg_win = sum(r for r in returns if r > 0) / max(wins, 1)
        avg_loss = abs(sum(r for r in returns if r < 0)) / max(losses, 1)
        overall_exp = round(avg_win * (wins / max(total_count, 1))
                            - avg_loss * (losses / max(total_count, 1)), 2)

        return {
            "empty": False,
            "kpi": {
                "total": total_count,
                "win_rate": overall_wr,
                "profit_factor": overall_pf,
                "expectancy": overall_exp,
            },
            "by_type": by_type,
            "by_freq": by_freq,
            "by_direction": by_direction,
            "by_resonance": by_resonance,
            "decay": decay,
            "calibration": calibration,
            "mfe_mae": mfe_mae,
            "sqs": sqs,
            "weight_rec": weight_rec,
        }

    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "error": str(e), "detail": traceback.format_exc()
        })


@router.get("/trade-pairs")
async def backtest_trade_pairs():
    """买卖配对列表 + 统计"""
    try:
        from signals.core.backtest import SignalJournal, BacktestReport, TradePairMatcher

        journal = SignalJournal()
        eval_records = journal.get_evaluated()
        all_records = journal.get_all_records()
        trade_pair_rows = journal.get_trade_pairs()
        journal.close()

        if not eval_records:
            return {"empty": True, "summary": {}, "pairs": []}

        report = BacktestReport(eval_records, trade_pair_rows)
        summary = report.trade_pair_summary()

        # 序列化 best/worst pair
        for key in ("best_pair", "worst_pair"):
            if key in summary and isinstance(summary[key], dict):
                pass  # already dict
            elif key in summary:
                summary[key] = {}

        return {
            "empty": False,
            "summary": summary,
            "pairs": trade_pair_rows,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/evaluate")
async def backtest_evaluate():
    """触发待评估信号评估"""
    try:
        from signals.core.backtest import (
            SignalJournal, ForwardEvaluator, TradePairMatcher,
            _evaluate_pending,
        )

        journal = SignalJournal()
        pending = journal.get_pending()
        if not pending:
            journal.close()
            return {"evaluated": 0, "message": "无待评估信号"}

        evaluator = ForwardEvaluator()
        _evaluate_pending(journal, evaluator, pending)

        # 重新配对
        all_records = journal.get_all_records()
        matcher = TradePairMatcher()
        pairs = matcher.match(all_records)
        if pairs:
            journal.save_trade_pairs(pairs)

        journal.close()
        return {"evaluated": len(pending), "message": f"已评估 {len(pending)} 条信号"}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
