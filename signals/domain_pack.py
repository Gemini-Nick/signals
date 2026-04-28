from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import config


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


class SignalsPack:
    """
    Domain-pack wrapper for the existing Signals CLI/runtime.

    The pack does not reimplement analysis logic. It turns the current `run.py`
    entrypoint into a reusable pack interface and writes a lightweight local run
    ledger that the desktop control plane can consume.
    """

    def __init__(
        self,
        *,
        repo_root: Optional[Path] = None,
        state_root: Optional[Path] = None,
        python_executable: Optional[str] = None,
    ) -> None:
        self.repo_root = repo_root or Path(__file__).resolve().parents[1]
        self.state_root = state_root or self.repo_root / ".longclaw" / "domain-pack"
        self.python_executable = python_executable or sys.executable
        self.runner_script = self.repo_root / "run.py"

    def describe(self) -> Dict[str, Any]:
        return {
            "pack_id": "signals",
            "domain": "financial_analysis",
            "version": "0.1.0",
            "owner_repo": "Signals",
            "runtime": "cloud",
            "description": "Financial analysis domain pack backed by the existing LONG CLAW Signals engine.",
            "capabilities": self.capabilities(),
            "metadata": {
                "phase": "phase1",
                "entrypoint": str(self.runner_script),
                "state_root": str(self.state_root),
            },
        }

    def health(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "checks": {
                "runner_exists": self.runner_script.exists(),
                "tushare_token_configured": bool(config.TUSHARE_TOKEN),
                "futu_configured": bool(config.FUTU_HOST and config.FUTU_PORT),
                "weclaw_enabled": config.WECLAW_ENABLED,
                "notes_dir": config.NOTES_DIR,
            },
        }

    def capabilities(self) -> List[Dict[str, Any]]:
        return [
            self._capability("intraday", "Intraday Monitor", "实时盘中监测与多层联动分析"),
            self._capability("review", "Review", "盘后复盘与阶段性对比"),
            self._capability("index", "Index Report", "快速指数报告"),
            self._capability("backtest", "Backtest", "历史信号验证与回测"),
            self._capability("weekly", "Weekly", "周度总结与观察"),
            self._capability("rss", "RSS", "市场资讯订阅与推送"),
        ]

    async def list_runs(self) -> List[Dict[str, Any]]:
        runs_dir = self.state_root / "runs"
        if not runs_dir.exists():
            return []
        runs: List[Dict[str, Any]] = []
        for metadata_path in sorted(runs_dir.glob("*/run.json")):
            metadata = self._read_run_metadata(metadata_path.parent.name)
            if metadata:
                runs.append(self._canonical_run(metadata))
        runs.sort(key=lambda run: run["created_at"], reverse=True)
        return runs

    async def run(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        request_payload = dict(request)
        input_payload = dict(request_payload.get("input", request_payload))
        mode = input_payload.get("mode") or request_payload.get("capability", "intraday")
        run_id = f"signals-{uuid.uuid4().hex[:10]}"
        run_dir = self.state_root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "stdout.log"
        metadata_path = run_dir / "run.json"

        command = [self.python_executable, str(self.runner_script), "--mode", str(mode)]
        command.extend(self._build_cli_args(input_payload))

        metadata: Dict[str, Any] = {
            "run_id": run_id,
            "domain": "financial_analysis",
            "capability": str(mode),
            "status": "queued",
            "requested_by": request_payload.get("requested_by") or input_payload.get("requested_by"),
            "summary": f"Signals {mode}",
            "created_at": utc_now().isoformat(),
            "started_at": None,
            "finished_at": None,
            "metadata": {
                "mode": mode,
                "command": command,
                "cwd": str(self.repo_root),
                "stdout_path": str(stdout_path),
                "state_root": str(self.state_root),
                "input": _json_safe(input_payload),
            },
        }

        wait = bool(request_payload.get("wait", False))
        if wait:
            metadata["status"] = "running"
            metadata["started_at"] = utc_now().isoformat()
            self._write_run_metadata(metadata_path, metadata)
            completed = subprocess.run(
                command,
                cwd=self.repo_root,
                text=True,
                capture_output=True,
            )
            stdout_path.write_text(
                (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else ""),
                encoding="utf-8",
            )
            metadata["status"] = "succeeded" if completed.returncode == 0 else "failed"
            metadata["finished_at"] = utc_now().isoformat()
            metadata["metadata"]["returncode"] = completed.returncode
        else:
            with stdout_path.open("w", encoding="utf-8") as log_handle:
                process = subprocess.Popen(
                    command,
                    cwd=self.repo_root,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            metadata["status"] = "running"
            metadata["started_at"] = utc_now().isoformat()
            metadata["metadata"]["pid"] = process.pid

        self._write_run_metadata(metadata_path, metadata)
        return {
            "run": self._canonical_run(metadata),
            "artifacts": await self.list_artifacts(run_id),
            "review_actions": await self.review_actions(run_id),
        }

    async def list_artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        metadata = self._read_run_metadata(run_id)
        if not metadata:
            return []
        artifacts: List[Dict[str, Any]] = []
        stdout_path = metadata["metadata"].get("stdout_path")
        if stdout_path:
            artifacts.append(
                {
                    "artifact_id": f"{run_id}:stdout",
                    "run_id": run_id,
                    "kind": "stdout_log",
                    "uri": stdout_path,
                    "title": "signals stdout",
                }
            )
        output_dir = metadata["metadata"]["input"].get("output_dir")
        if output_dir:
            artifacts.append(
                {
                    "artifact_id": f"{run_id}:output",
                    "run_id": run_id,
                    "kind": "output_dir",
                    "uri": str(output_dir),
                    "title": "signals output",
                }
            )
        return artifacts

    async def review_actions(self, run_id: str) -> List[Dict[str, Any]]:
        metadata = self._read_run_metadata(run_id)
        if not metadata:
            return []
        report_path = metadata["metadata"].get("backtest_report_path")
        if not report_path:
            return []
        return [
            {
                "action_id": f"pack:signals:report:push:{run_id}",
                "run_id": run_id,
                "kind": "push_report",
                "label": "Push Report",
                "payload": {
                    "run_id": run_id,
                    "report_path": str(report_path),
                },
            }
        ]

    async def dashboard(
        self,
        *,
        recent_limit: int = 20,
        backlog_limit: int = 10,
    ) -> Dict[str, Any]:
        strategy_snapshot = self._strategy_snapshot()
        self._record_strategy_snapshot_run(strategy_snapshot)
        runs = await self.list_runs()
        recent_runs = runs[:recent_limit]
        review_runs = [run for run in recent_runs if run.get("capability") == "review"][:10]
        connector_health = self._connector_health()
        diagnostics = self._diagnostics(connector_health)
        backtest_summary = self._backtest_summary()
        pending_backlog = self._pending_backlog_preview(backlog_limit)
        backtest_jobs = self._backtest_jobs(recent_runs)
        status = self._dashboard_status(connector_health)
        overview = self._overview(backtest_summary, strategy_snapshot)
        cache_status = self._cache_status()
        return {
            "pack_id": "signals",
            "title": "Signals",
            "status": status,
            "notice": self._dashboard_notice(status, connector_health),
            "diagnostics": diagnostics,
            "overview": overview,
            "recent_runs": recent_runs,
            "review_runs": review_runs,
            "buy_candidates": strategy_snapshot.get("candidates", []),
            "sell_warnings": strategy_snapshot.get("warnings", []),
            "chart_context": strategy_snapshot.get("chart_context"),
            "backtest_summary": backtest_summary,
            "backtest_jobs": backtest_jobs,
            "pending_backlog_preview": pending_backlog,
            "connector_health": connector_health,
            "deep_links": self._deep_links(),
            "operator_actions": self._operator_actions(),
            "daily_brief": strategy_snapshot.get("daily_brief", {}),
            "decision_queue": strategy_snapshot.get("decision_queue", []),
            "strategy_kpis": strategy_snapshot.get("strategy_kpis", {}),
            "source_confidence": strategy_snapshot.get("source_confidence", {}),
            "cache_status": cache_status,
        }

    def _operator_actions(self) -> List[Dict[str, Any]]:
        actions = [
            {
                "action_id": "pack:signals:run:review",
                "run_id": "signals:dashboard",
                "kind": "run_pack",
                "label": "Run Review",
                "payload": {"pack_id": "signals", "capability": "review", "input": {"mode": "review"}},
                "metadata": {},
            },
            {
                "action_id": "pack:signals:run:backtest",
                "run_id": "signals:dashboard",
                "kind": "run_pack",
                "label": "Run Backtest",
                "payload": {"pack_id": "signals", "capability": "backtest", "input": {"mode": "backtest"}},
                "metadata": {},
            },
        ]
        web_url = os.environ.get("LONGCLAW_SIGNALS_WEB_BASE_URL", "").rstrip("/")
        if web_url:
            actions.append({
                "action_id": "pack:signals:web:url",
                "run_id": "signals:dashboard",
                "kind": "open_url",
                "label": "Open Signals Terminal",
                "payload": {"url": web_url},
                "metadata": {},
            })
        return actions

    def _deep_links(self) -> List[Dict[str, Any]]:
        web_url = os.environ.get("LONGCLAW_SIGNALS_WEB_BASE_URL", "").rstrip("/")
        links = []
        if web_url:
            links.extend([
                {
                    "link_id": "signals-terminal",
                    "label": "Signals Terminal",
                    "url": web_url,
                    "kind": "web",
                },
                {
                    "link_id": "signals-legacy",
                    "label": "Signals Legacy",
                    "url": f"{web_url}/legacy",
                    "kind": "web",
                },
            ])
        return links

    def _diagnostics(self, connector_health: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "diagnostic_id": f"signals-{item['connector_id']}",
                "status": item.get("status", "unknown"),
                "label": item.get("connector_id", ""),
                "detail": item.get("summary", ""),
                "metadata": item.get("details", {}),
            }
            for item in connector_health
        ]

    def _dashboard_status(self, connector_health: List[Dict[str, Any]]) -> str:
        statuses = {item.get("status") for item in connector_health}
        if "critical" in statuses:
            return "degraded"
        if "warning" in statuses:
            return "degraded"
        return "healthy"

    def _dashboard_notice(self, status: str, connector_health: List[Dict[str, Any]]) -> str:
        if status == "healthy":
            return ""
        bad = [
            item.get("connector_id", "")
            for item in connector_health
            if item.get("status") in {"critical", "warning"}
        ]
        return "Degraded connectors: " + ", ".join([item for item in bad if item])

    def _cache_status_empty(self, mode: str, error: str = "") -> Dict[str, Any]:
        return {
            "available": False,
            "mode": mode,
            "updated_at": utc_now().isoformat(),
            "error": error,
            "live_low_latency": {"modules": [], "summary": {}},
            "postmarket_backfill": {"run": None, "tasks": [], "summary": {}},
            "mongo_stock_cache": {"freqs": [], "summary": {}},
            "terminal_outputs": [],
            "blockers": [],
        }

    def _cache_status(self) -> Dict[str, Any]:
        if not config.MONGO_URL:
            return self._cache_status_empty("mongo_not_configured")
        try:
            from signals.data.mongo_fallback import get_db

            db = get_db()
            if db is None:
                return self._cache_status_empty("mongo_unavailable")
            db.command("ping")
        except Exception as exc:
            return self._cache_status_empty("mongo_error", f"{exc.__class__.__name__}: {exc}")

        trade_date = self._cache_trade_date(db)
        live = self._cache_live_low_latency(db)
        postmarket = self._cache_postmarket_backfill(db)
        mongo_cache = self._cache_mongo_stock_cache(db, trade_date)
        terminal_outputs = self._cache_terminal_outputs(db)
        blockers = self._cache_blockers(live, postmarket)
        return {
            "available": True,
            "mode": "mongo",
            "updated_at": utc_now().isoformat(),
            "trade_date": trade_date,
            "live_low_latency": live,
            "postmarket_backfill": postmarket,
            "mongo_stock_cache": mongo_cache,
            "terminal_outputs": terminal_outputs,
            "blockers": blockers,
        }

    def _cache_trade_date(self, db) -> str:
        run = self._find_one(db, "sync_runs", {}, {"trade_date": 1, "started_at": 1}, sort=[("started_at", -1)])
        if run and run.get("trade_date"):
            return str(run.get("trade_date"))
        return datetime.now().date().isoformat()

    def _cache_live_low_latency(self, db) -> Dict[str, Any]:
        specs = [
            ("quote_snapshots", "quote_lane", "quotes"),
            ("stock_minute", "signal_lane", "stock minute"),
            ("index_minute", "signal_lane", "index minute"),
            ("minute_readiness_probe", "signal_lane", "minute readiness"),
            ("market_pools", "workbench_lane", "market pools"),
            ("board_heat_minute", "board_lane", "board heat"),
            ("concept_heat_minute", "board_lane", "concept heat"),
            ("chain_heat_snapshots", "board_lane", "chain heat"),
        ]
        modules: List[Dict[str, Any]] = []
        for module, lane, label in specs:
            doc = self._latest_sync_doc(db, module)
            if module == "stock_minute":
                selection = self._find_one(db, "sync_log", {"_id": "stock_minute:selection:_meta"}) or {}
                doc = {**doc, **{key: value for key, value in selection.items() if key not in {"_id", "module"}}} if doc else selection
            modules.append(self._cache_sync_doc_summary(module, label, lane, doc))

        ok_count = sum(1 for item in modules if item.get("status") in {"ok", "partial", "running"})
        freshness = [item.get("freshness_seconds") for item in modules if item.get("freshness_seconds") is not None]
        minute = next((item for item in modules if item.get("module") == "minute_readiness_probe"), {})
        stock_minute = next((item for item in modules if item.get("module") == "stock_minute"), {})
        return {
            "modules": modules,
            "summary": {
                "ok_modules": ok_count,
                "total_modules": len(modules),
                "freshness_seconds_max": max(freshness) if freshness else None,
                "minute_checked": self._int_from_result(minute, "checked"),
                "minute_not_ready": self._int_from_result(minute, "not_ready"),
                "selected_symbols": len(stock_minute.get("selected_symbols") or []),
                "skipped_symbols": int(stock_minute.get("skipped_count") or self._int_from_result(stock_minute, "skipped") or 0),
            },
        }

    def _cache_postmarket_backfill(self, db) -> Dict[str, Any]:
        run = self._find_one(db, "sync_runs", {}, sort=[("started_at", -1), ("updated_at", -1)])
        if not run:
            return {"run": None, "tasks": [], "summary": {"status": "not_started", "progress_pct": 0}}

        run_id = str(run.get("run_id") or run.get("_id") or "")
        tasks = self._find_many(db, "sync_tasks", {"run_id": run_id}, sort=[("order", 1)])
        stock_daily_progress = self._find_one(db, "sync_log", {"_id": "stock_daily:progress:_meta"}) or {}
        board_cons_progress = self._find_one(db, "sync_log", {"_id": "board_cons:_meta"}) or {}

        rows: List[Dict[str, Any]] = []
        status_counts: Dict[str, int] = {}
        completed = 0
        progress_values: List[float] = []
        sample_errors: List[Any] = []
        for task in tasks:
            module = str(task.get("module") or "")
            status = str(task.get("status") or "pending")
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "ok":
                completed += 1
            summary = dict(task.get("result_summary") or {})
            cursor = dict(task.get("cursor") or {})
            if module == "stock_daily" and stock_daily_progress:
                summary.update(self._project_fields(stock_daily_progress, [
                    "scope", "total", "processed", "remaining", "inserted", "skipped",
                    "errors", "latest_symbol", "latest_status", "progress_pct",
                ]))
            if module == "board_cons" and board_cons_progress:
                summary.update(self._project_fields(board_cons_progress, [
                    "processed", "processed_groups", "remaining", "next_cursor",
                    "total_groups", "sample_errors", "unmapped", "source_counts",
                ]))
                cursor.update(self._project_fields(board_cons_progress, ["next_cursor", "remaining", "total_groups"]))
            progress_pct = self._task_progress_pct(status, summary, cursor)
            if progress_pct is not None:
                progress_values.append(progress_pct)
            errors = summary.get("sample_errors")
            if isinstance(errors, list):
                sample_errors.extend(errors[:3])
            row = {
                "task_id": str(task.get("_id") or ""),
                "module": module,
                "phase": task.get("phase") or "",
                "shard_key": task.get("shard_key") or "all",
                "status": status,
                "attempts": int(task.get("attempts") or 0),
                "depends_on": task.get("depends_on") or [],
                "cursor": _json_safe(cursor),
                "result_summary": _json_safe(summary),
                "progress_pct": progress_pct,
                "error_msg": str(task.get("error_msg") or "")[:500],
                "started_at": self._iso(task.get("started_at")),
                "updated_at": self._iso(task.get("updated_at")),
                "heartbeat_at": self._iso(task.get("heartbeat_at")),
                "finished_at": self._iso(task.get("finished_at")),
            }
            rows.append(row)

        task_count = len(rows)
        progress_pct = round(sum(progress_values) / task_count, 2) if task_count and progress_values else (
            round(completed / task_count * 100, 2) if task_count else 0
        )
        return {
            "run": {
                "run_id": run_id,
                "trade_date": str(run.get("trade_date") or ""),
                "status": str(run.get("status") or ""),
                "phase": str(run.get("phase") or ""),
                "owner_pid": run.get("owner_pid") or "",
                "started_at": self._iso(run.get("started_at")),
                "updated_at": self._iso(run.get("updated_at")),
                "heartbeat_at": self._iso(run.get("heartbeat_at")),
                "finished_at": self._iso(run.get("finished_at")),
            },
            "tasks": rows,
            "summary": {
                "task_count": task_count,
                "completed": completed,
                "status_counts": status_counts,
                "progress_pct": progress_pct,
                "sample_errors": _json_safe(sample_errors[:10]),
            },
        }

    def _cache_mongo_stock_cache(self, db, trade_date: str) -> Dict[str, Any]:
        freqs = ["日线", "周线", "5分钟", "15分钟", "30分钟", "60分钟"]
        rows: List[Dict[str, Any]] = []
        trade_start, trade_end = self._trade_date_bounds(trade_date)
        stock_daily_progress = self._find_one(db, "sync_log", {"_id": "stock_daily:progress:_meta"}) or {}
        for freq in freqs:
            query = {"meta.freq": freq}
            latest = self._find_one(db, "bars", query, {"dt": 1, "meta.symbol": 1}, sort=[("dt", -1)])
            today_query = dict(query)
            if trade_start and trade_end:
                today_query["dt"] = {"$gte": trade_start, "$lt": trade_end}
            today_symbols = self._distinct_count(db, "bars", "meta.symbol", today_query) if trade_start else 0
            symbols = self._distinct_count(db, "bars", "meta.symbol", query)
            if freq == "日线":
                progress_total = int(stock_daily_progress.get("total") or stock_daily_progress.get("expected_codes") or 0)
                symbols = max(symbols, progress_total, today_symbols)
            elif symbols == 0:
                symbols = today_symbols
            rows.append({
                "freq": freq,
                "symbols": symbols,
                "today_symbols": today_symbols,
                "total_bars": self._count(db, "bars", query),
                "latest_dt": self._iso(latest.get("dt") if latest else None),
                "latest_symbol": (latest or {}).get("meta", {}).get("symbol", ""),
            })

        daily = next((item for item in rows if item.get("freq") == "日线"), {})
        return {
            "freqs": rows,
            "summary": {
                "daily_symbols": daily.get("symbols", 0),
                "daily_today_symbols": daily.get("today_symbols", 0),
                "latest_daily_dt": daily.get("latest_dt", ""),
            },
        }

    def _cache_terminal_outputs(self, db) -> List[Dict[str, Any]]:
        outputs = [
            ("terminal_technical_signals", "generated_at"),
            ("knowledge_market_views", "generated_at"),
            ("terminal_stock_pool", "generated_at"),
            ("strategy_snapshots", "generated_at"),
            ("chain_heat_snapshots", "updated_at"),
        ]
        rows: List[Dict[str, Any]] = []
        for collection, sort_field in outputs:
            latest = self._find_one(db, collection, {}, sort=[(sort_field, -1), ("updated_at", -1)])
            rows.append({
                "collection": collection,
                "generated": bool(latest),
                "count": self._count(db, collection, {}),
                "latest_at": self._iso((latest or {}).get(sort_field) or (latest or {}).get("updated_at")),
                "status": (latest or {}).get("status") or ("ok" if latest else "missing"),
            })
        return rows

    def _cache_blockers(self, live: Dict[str, Any], postmarket: Dict[str, Any]) -> List[Dict[str, Any]]:
        blockers: List[Dict[str, Any]] = []
        for item in live.get("modules", []):
            status = str(item.get("status") or "")
            if status in {"error", "degraded", "missing"}:
                blockers.append({
                    "scope": "live_low_latency",
                    "module": item.get("module"),
                    "status": status,
                    "error_msg": item.get("error_msg") or item.get("degraded_reason") or "",
                })
        for task in postmarket.get("tasks", []):
            status = str(task.get("status") or "")
            if status in {"error", "degraded", "stale"}:
                blockers.append({
                    "scope": "postmarket_backfill",
                    "module": task.get("module"),
                    "status": status,
                    "error_msg": task.get("error_msg") or "",
                    "sample_errors": task.get("result_summary", {}).get("sample_errors") or [],
                })
        return blockers[:12]

    def _latest_sync_doc(self, db, module: str) -> Dict[str, Any]:
        doc = self._find_one(db, "sync_log", {"module": module}, sort=[("last_run", -1), ("updated_at", -1)])
        if doc:
            return doc
        return self._find_one(db, "sync_log", {"_id": f"{module}:_meta"}) or {}

    def _cache_sync_doc_summary(self, module: str, label: str, lane: str, doc: Mapping[str, Any]) -> Dict[str, Any]:
        if not doc:
            return {"module": module, "label": label, "lane": lane, "status": "missing", "freshness": "missing"}
        last_dt = doc.get("last_dt") or doc.get("latest_dt")
        last_run = doc.get("last_run") or doc.get("updated_at")
        freshness_seconds = self._freshness_seconds(last_run)
        result = dict(doc.get("result") or {})
        row = {
            "module": module,
            "label": label,
            "lane": doc.get("lane") or lane,
            "status": str(doc.get("status") or ""),
            "freshness": self._freshness_label(freshness_seconds),
            "freshness_seconds": freshness_seconds,
            "latest_dt": self._iso(last_dt),
            "last_run": self._iso(last_run),
            "next_due_at": self._iso(doc.get("next_due_at")),
            "elapsed_seconds": doc.get("elapsed_seconds") or doc.get("runtime_seconds"),
            "error_msg": str(doc.get("error_msg") or doc.get("error") or "")[:500],
            "degraded_reason": str(doc.get("degraded_reason") or "")[:500],
            "result": _json_safe(result),
        }
        for key in (
            "selected_symbols", "priority_symbols", "pinned_symbols", "skipped_symbols",
            "skipped_count", "candidate_count", "source_counts", "planned_calls",
            "empty_calls", "failed_calls", "written", "skipped_existing",
        ):
            if key in doc:
                row[key] = _json_safe(doc.get(key))
        return row

    def _task_progress_pct(self, status: str, summary: Mapping[str, Any], cursor: Mapping[str, Any]) -> Optional[float]:
        for source in (summary, cursor):
            value = source.get("progress_pct")
            if isinstance(value, (int, float)):
                return round(float(value), 2)
            next_cursor = source.get("next_cursor")
            total_groups = source.get("total_groups")
            if isinstance(next_cursor, (int, float)) and isinstance(total_groups, (int, float)) and total_groups:
                return round(float(next_cursor) / float(total_groups) * 100, 2)
            processed = source.get("processed")
            total = source.get("total") or source.get("expected_codes") or source.get("total_groups")
            if isinstance(processed, (int, float)) and isinstance(total, (int, float)) and total:
                return round(float(processed) / float(total) * 100, 2)
        if status == "ok":
            return 100.0
        return None

    def _trade_date_bounds(self, trade_date: str) -> tuple[Optional[datetime], Optional[datetime]]:
        try:
            start = datetime.fromisoformat(str(trade_date)[:10])
        except Exception:
            return None, None
        return start, start + timedelta(days=1)

    def _freshness_seconds(self, value: Any) -> Optional[int]:
        dt = self._coerce_datetime(value)
        if not dt:
            return None
        return max(0, int((datetime.now() - dt).total_seconds()))

    def _freshness_label(self, seconds: Optional[int]) -> str:
        if seconds is None:
            return "missing"
        if seconds <= 5 * 60:
            return "fresh"
        if seconds <= 60 * 60:
            return "warm"
        return "stale"

    def _coerce_datetime(self, value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        return None

    def _iso(self, value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if value is None:
            return ""
        return str(value)

    def _find_one(self, db, collection: str, query: Mapping[str, Any], projection: Optional[Mapping[str, int]] = None, sort=None):
        try:
            return db[collection].find_one(dict(query), projection, sort=sort)
        except Exception:
            return None

    def _find_many(self, db, collection: str, query: Mapping[str, Any], projection: Optional[Mapping[str, int]] = None, sort=None) -> List[Dict[str, Any]]:
        try:
            cursor = db[collection].find(dict(query), projection)
            if sort:
                cursor = cursor.sort(sort)
            return list(cursor)
        except Exception:
            return []

    def _count(self, db, collection: str, query: Mapping[str, Any]) -> int:
        try:
            return int(db[collection].count_documents(dict(query), maxTimeMS=1500))
        except TypeError:
            try:
                return int(db[collection].count_documents(dict(query)))
            except Exception:
                return 0
        except Exception:
            return 0

    def _distinct_count(self, db, collection: str, field: str, query: Mapping[str, Any]) -> int:
        try:
            return len(db[collection].distinct(field, dict(query), maxTimeMS=1500))
        except TypeError:
            try:
                return len(db[collection].distinct(field, dict(query)))
            except Exception:
                return 0
        except Exception:
            return 0

    def _project_fields(self, doc: Mapping[str, Any], keys: List[str]) -> Dict[str, Any]:
        return {key: _json_safe(doc.get(key)) for key in keys if key in doc}

    def _int_from_result(self, row: Mapping[str, Any], key: str) -> int:
        result = row.get("result")
        if isinstance(result, Mapping):
            value = result.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        value = row.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    def _overview(
        self,
        backtest_summary: Dict[str, int],
        strategy_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        strategy_snapshot = dict(strategy_snapshot or {})
        themes = [
            dict(item)
            for item in strategy_snapshot.get("themes", [])
            if isinstance(item, Mapping)
        ]
        board_themes = [item for item in themes if item.get("domain") == "board"][:5]
        concept_themes = [item for item in themes if item.get("domain") == "concept"][:5]

        if board_themes or concept_themes:
            cluster_summary = {
                "industry_top": [
                    {
                        "label": item.get("name", ""),
                        "change_pct": item.get("change_pct", item.get("strength", 0)),
                        "leader": item.get("leader", ""),
                        "phase": item.get("phase", ""),
                        "confidence": item.get("confidence"),
                    }
                    for item in board_themes
                ],
                "concept_top": [
                    {
                        "label": item.get("name", ""),
                        "change_pct": item.get("change_pct", item.get("strength", 0)),
                        "leader": item.get("leader", ""),
                        "phase": item.get("phase", ""),
                        "confidence": item.get("confidence"),
                    }
                    for item in concept_themes
                ],
                "sources": {"board": "strategy_snapshot", "concept": "strategy_snapshot"},
                "freshness": {
                    "board": self._snapshot_source_freshness(strategy_snapshot, "board"),
                    "concept": self._snapshot_source_freshness(strategy_snapshot, "concept"),
                },
            }
            data_warnings = []
        else:
            board_resp = self._rank_snapshot("board")
            concept_resp = self._rank_snapshot("concept")
            cluster_summary = {
                "industry_top": board_resp.get("items", []),
                "concept_top": concept_resp.get("items", []),
                "sources": {
                    "board": board_resp.get("source", ""),
                    "concept": concept_resp.get("source", ""),
                },
                "freshness": {
                    "board": board_resp.get("freshness", ""),
                    "concept": concept_resp.get("freshness", ""),
                },
            }
            data_warnings = [
                warning
                for warning in [
                    board_resp.get("warning", ""),
                    concept_resp.get("warning", ""),
                ]
                if warning
            ]
        strategy_kpis = dict(strategy_snapshot.get("strategy_kpis") or {})
        return {
            "market_regime": strategy_snapshot.get("market_regime", {}),
            "cluster_summary": cluster_summary,
            "review_summary": {
                "backtest_total": backtest_summary.get("total", 0),
                "backtest_pending": backtest_summary.get("pending", 0),
                "signals_total": strategy_kpis.get("signals_total", 0),
                "signals_pending": strategy_kpis.get("signals_pending", 0),
            },
            "data_warning": " ".join(data_warnings),
        }

    def _strategy_snapshot(self) -> Dict[str, Any]:
        try:
            from signals.strategy.snapshot import get_strategy_snapshot

            snapshot = get_strategy_snapshot()
            return dict(snapshot) if isinstance(snapshot, Mapping) else {}
        except Exception as exc:
            return {
                "daily_brief": {
                    "title": "Signals 策略简报暂不可用",
                    "summary": f"strategy_snapshot_error:{exc.__class__.__name__}",
                    "changed_since_last": {},
                },
                "candidates": [],
                "warnings": [],
                "decision_queue": [],
                "strategy_kpis": {},
                "source_confidence": {"overall": 0, "sources": []},
            }

    def _snapshot_source_freshness(self, snapshot: Mapping[str, Any], source_name: str) -> str:
        confidence = snapshot.get("source_confidence") or {}
        for item in confidence.get("sources", []) if isinstance(confidence, Mapping) else []:
            if item.get("name") == source_name:
                return str(item.get("freshness") or "")
        return ""

    def _record_strategy_snapshot_run(self, snapshot: Mapping[str, Any]) -> None:
        as_of = str(snapshot.get("as_of") or datetime.now().date().isoformat())[:10]
        run_id = f"strategy-snapshot-{as_of}"
        path = self.state_root / "runs" / run_id / "run.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            brief = dict(snapshot.get("daily_brief") or {})
            generated_at = str(snapshot.get("generated_at") or utc_now().isoformat())
            metadata = {
                "run_id": run_id,
                "domain": "financial_analysis",
                "capability": "strategy_snapshot",
                "status": "completed",
                "requested_by": "signals.dashboard",
                "summary": brief.get("summary", "Signals strategy snapshot"),
                "created_at": generated_at,
                "started_at": generated_at,
                "finished_at": generated_at,
                "metadata": {
                    "as_of": as_of,
                    "candidate_count": len(snapshot.get("candidates") or []),
                    "warning_count": len(snapshot.get("warnings") or []),
                    "theme_count": len(snapshot.get("themes") or []),
                    "source_confidence": snapshot.get("source_confidence", {}),
                },
            }
            self._write_run_metadata(path, metadata)
        except Exception:
            return

    def _rank_snapshot(self, domain: str) -> Dict[str, Any]:
        try:
            from signals.data.gateway import get_board_rank, get_concept_rank
            from signals.data.models import DataRequest

            fn = get_board_rank if domain == "board" else get_concept_rank
            resp = fn(DataRequest(
                domain=domain,
                mode="realtime",
                market="A",
                purpose="cluster",
                allow_stale=True,
            ))
            df = resp.data
            items: List[Dict[str, Any]] = []
            if df is not None and not getattr(df, "empty", True):
                sort_col = "change_pct" if "change_pct" in df.columns else None
                if sort_col:
                    df = df.sort_values(sort_col, ascending=False)
                for _, row in df.head(5).iterrows():
                    items.append({
                        "label": str(row.get("board_name") or row.get("name") or ""),
                        "change_pct": float(row.get("change_pct", 0) or 0),
                        "leader": str(row.get("leader_name", "") or ""),
                    })
            return {
                "items": items,
                "source": resp.source,
                "freshness": resp.freshness,
                "warning": "" if items else f"{domain}_snapshot_empty",
            }
        except Exception as exc:
            return {
                "items": [],
                "source": "gateway",
                "freshness": "empty",
                "warning": f"{domain}_snapshot_error:{exc.__class__.__name__}",
            }

    def _backtest_jobs(self, recent_runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        jobs = []
        for run in recent_runs:
            if run.get("capability") != "backtest":
                continue
            metadata = dict(run.get("metadata") or {})
            jobs.append({
                "job_id": run.get("run_id", ""),
                "status": run.get("status", "idle"),
                "symbol": str(metadata.get("symbol") or ""),
                "freq": str(metadata.get("freq") or ""),
                "summary": run.get("summary", ""),
                "updated_at": run.get("finished_at") or run.get("created_at"),
                "source": "state_root",
                "metadata": metadata,
            })
        return jobs[:10]

    async def push_report(
        self,
        *,
        run_id: Optional[str] = None,
        report_path: Optional[str] = None,
        report_data: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(report_data or {})
        if not payload:
            resolved_path = report_path
            if not resolved_path and run_id:
                metadata = self._read_run_metadata(run_id)
                if metadata:
                    resolved_path = metadata["metadata"].get("backtest_report_path")
            if not resolved_path:
                raise RuntimeError("Signals push_report requires report_path or report_data")
            payload = json.loads(Path(resolved_path).read_text(encoding="utf-8"))

        from signals.notify.backtest_notify import push_backtest_report

        ok = push_backtest_report(payload)
        return {"ok": ok}

    def eval_suite(self) -> List[Dict[str, Any]]:
        return [
            {
                "suite_id": "signals_pack_compile",
                "name": "Signals Pack Compile",
                "description": "Basic syntax verification for the Signals pack wrapper.",
                "command": "python -m py_compile run.py signals/domain_pack.py",
            }
        ]

    def _capability(self, capability_id: str, name: str, description: str) -> Dict[str, Any]:
        return {
            "capability_id": capability_id,
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "default": capability_id},
                    "start": {"type": "string"},
                    "industries": {"type": "string"},
                    "market": {"type": "string"},
                    "notes": {"type": "string"},
                    "push": {"type": "boolean"},
                },
            },
        }

    def _build_cli_args(self, input_payload: Mapping[str, Any]) -> List[str]:
        args: List[str] = []
        boolean_flags = {
            "push",
            "dry_run",
            "list_dates",
            "create",
            "list_sessions",
            "sync",
        }
        passthrough_keys = [
            "start",
            "industries",
            "notes",
            "file",
            "source",
            "author",
            "market",
            "signal_type",
            "freq_filter",
            "session",
            "end",
            "symbols",
            "port",
            "themes",
            "symbol",
        ]
        for key in passthrough_keys:
            value = input_payload.get(key)
            if value is None or value == "":
                continue
            args.extend([f"--{key.replace('_', '-')}", str(value)])
        for key in boolean_flags:
            if input_payload.get(key):
                args.append(f"--{key.replace('_', '-')}")
        return args

    def _canonical_run(self, metadata: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "run_id": metadata["run_id"],
            "domain": metadata["domain"],
            "capability": metadata["capability"],
            "status": metadata["status"],
            "session_id": None,
            "task_id": None,
            "requested_by": metadata.get("requested_by"),
            "summary": metadata.get("summary", ""),
            "created_at": metadata["created_at"],
            "started_at": metadata.get("started_at"),
            "finished_at": metadata.get("finished_at"),
            "metadata": metadata.get("metadata", {}),
        }

    def _read_run_metadata(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = self.state_root / "runs" / run_id / "run.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_run_metadata(self, path: Path, metadata: Mapping[str, Any]) -> None:
        path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _backtest_summary(self) -> Dict[str, int]:
        from signals.core.backtest import SignalJournal

        journal = None
        try:
            journal = SignalJournal()
            summary = journal.summary()
            return {
                "total": int(summary.get("total") or 0),
                "evaluated": int(summary.get("evaluated") or 0),
                "pending": int(summary.get("pending") or 0),
            }
        except Exception:
            return {"total": 0, "evaluated": 0, "pending": 0}
        finally:
            if journal is not None:
                journal.close()

    def _pending_backlog_preview(self, limit: int) -> List[Dict[str, Any]]:
        from signals.core.backtest import SignalJournal

        journal = None
        try:
            journal = SignalJournal()
            return [dict(item) for item in journal.get_pending()[:limit]]
        except Exception:
            return []
        finally:
            if journal is not None:
                journal.close()

    def _connector_health(self) -> List[Dict[str, Any]]:
        mongo_status = "not_configured"
        mongo_details: Dict[str, Any] = {"configured": bool(config.MONGO_URL)}
        if config.MONGO_URL:
            try:
                from signals.data.mongo_fallback import get_db

                db = get_db()
                if db is not None:
                    db.command("ping")
                    mongo_status = "ok"
                    mongo_details["database"] = db.name
                else:
                    mongo_status = "warning"
            except Exception as exc:
                mongo_status = "warning"
                mongo_details["error"] = f"{exc.__class__.__name__}: {exc}"

        return [
            {
                "connector_id": "mongodb",
                "status": mongo_status,
                "summary": "MongoDB cache/read model",
                "details": mongo_details,
            },
            {
                "connector_id": "tushare",
                "status": "ok" if config.TUSHARE_TOKEN else "info",
                "summary": "Tushare market data token (optional when MongoDB cache is healthy)",
                "details": {"configured": bool(config.TUSHARE_TOKEN)},
            },
            {
                "connector_id": "futu",
                "status": "ok" if config.FUTU_HOST and config.FUTU_PORT else "warning",
                "summary": "Futu OpenD market data bridge",
                "details": {"host": config.FUTU_HOST, "port": config.FUTU_PORT},
            },
            {
                "connector_id": "weclaw_notify",
                "status": "ok" if config.WECLAW_ENABLED else "info",
                "summary": "WeClaw push notification bridge",
                "details": {"enabled": config.WECLAW_ENABLED},
            },
            {
                "connector_id": "runner",
                "status": "ok" if self.runner_script.exists() else "critical",
                "summary": "Signals CLI runtime",
                "details": {"runner_exists": self.runner_script.exists()},
            },
        ]
