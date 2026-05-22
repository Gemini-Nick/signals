from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import config
from signals.core.market_time import market_timezone, naive_market_now


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
        include_ai_factor_factory: bool = False,
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
            "ai_factor_factory": self._ai_factor_factory() if include_ai_factor_factory else self._ai_factor_factory_stub(strategy_snapshot),
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
            "recovery_state": "unavailable",
            "critical_blocker": {},
            "daily_coverage_date": "",
            "terminal_ready_date": "",
            "live_low_latency": {"modules": [], "summary": {}},
            "postmarket_backfill": {"run": None, "tasks": [], "summary": {}},
            "mongo_stock_cache": {"freqs": [], "summary": {}},
            "terminal_outputs": [],
            "provider_health": [],
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
        provider_health = self._cache_provider_health(db)
        blockers = self._cache_blockers(live, postmarket, provider_health)
        daily_coverage_date = str((mongo_cache.get("summary") or {}).get("daily_coverage_date") or "")
        terminal_ready_date = self._cache_terminal_ready_date(trade_date, postmarket, terminal_outputs)
        critical_blocker = self._cache_critical_blocker(postmarket, provider_health)
        recovery_state = self._cache_recovery_state(
            trade_date=trade_date,
            daily_coverage_date=daily_coverage_date,
            terminal_ready_date=terminal_ready_date,
            postmarket=postmarket,
            critical_blocker=critical_blocker,
        )
        return {
            "available": True,
            "mode": "mongo",
            "updated_at": utc_now().isoformat(),
            "trade_date": trade_date,
            "recovery_state": recovery_state,
            "critical_blocker": critical_blocker,
            "daily_coverage_date": daily_coverage_date,
            "terminal_ready_date": terminal_ready_date,
            "live_low_latency": live,
            "postmarket_backfill": postmarket,
            "mongo_stock_cache": mongo_cache,
            "terminal_outputs": terminal_outputs,
            "provider_health": provider_health,
            "blockers": blockers,
        }

    def _cache_trade_date(self, db) -> str:
        try:
            from signals.data.mongo_fallback import get_last_trading_day

            return get_last_trading_day("A")
        except Exception:
            pass
        run = self._find_one(db, "sync_runs", {}, {"trade_date": 1, "started_at": 1}, sort=[("started_at", -1)])
        if run and run.get("trade_date"):
            return str(run.get("trade_date"))
        return naive_market_now("A").date().isoformat()

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
                doc = self._merge_stock_minute_selection(doc, selection)
            modules.append(self._cache_sync_doc_summary(module, label, lane, doc))

        ok_count = sum(1 for item in modules if item.get("status") == "ok")
        freshness = [item.get("freshness_seconds") for item in modules if item.get("freshness_seconds") is not None]
        minute = next((item for item in modules if item.get("module") == "minute_readiness_probe"), {})
        stock_minute = next((item for item in modules if item.get("module") == "stock_minute"), {})
        minute_not_ready = self._int_from_result(minute, "not_ready")
        problem_modules = [
            item.get("module")
            for item in modules
            if item.get("status") != "ok"
        ]
        return {
            "modules": modules,
            "summary": {
                "ok_modules": ok_count,
                "total_modules": len(modules),
                "strict_status": "ok" if ok_count == len(modules) and minute_not_ready == 0 else "degraded",
                "problem_modules": problem_modules,
                "freshness_seconds_max": max(freshness) if freshness else None,
                "minute_checked": self._int_from_result(minute, "checked"),
                "minute_not_ready": minute_not_ready,
                "minute_not_ready_samples": self._cache_minute_not_ready_samples(db),
                "selected_symbols": len(stock_minute.get("selected_symbols") or []),
                "skipped_symbols": int(stock_minute.get("skipped_count") or self._int_from_result(stock_minute, "skipped") or 0),
            },
        }

    def _merge_stock_minute_selection(self, doc: Mapping[str, Any], selection: Mapping[str, Any]) -> Mapping[str, Any]:
        if not doc:
            return selection
        merged = dict(doc)
        for key in (
            "selected_symbols",
            "priority_symbols",
            "pinned_symbols",
            "skipped_symbols",
            "skipped_count",
            "candidate_count",
            "source_counts",
            "max_symbols",
            "rotation_enabled",
            "rotation_policy",
            "minute_scope",
            "tier_counts",
            "universe_total",
            "universe_cached",
            "universe_pending",
            "universe_error",
        ):
            if key in selection:
                merged[key] = selection.get(key)
        if "tail_counts" not in merged and "tail_counts" in selection:
            merged["tail_counts"] = selection.get("tail_counts")
        return merged

    def _cache_postmarket_backfill(self, db) -> Dict[str, Any]:
        run = self._find_one(db, "sync_runs", {}, sort=[("started_at", -1), ("updated_at", -1)])
        if not run:
            return {"run": None, "tasks": [], "summary": {"status": "not_started", "progress_pct": 0}}

        run_id = str(run.get("run_id") or run.get("_id") or "")
        tasks = self._find_many(db, "sync_tasks", {"run_id": run_id}, sort=[("order", 1)])
        tasks = self._current_postmarket_tasks(tasks)
        stock_daily_progress = self._find_one(db, "sync_log", {"_id": "stock_daily:progress:_meta"}) or {}
        board_cons_progress = self._find_one(db, "sync_log", {"_id": "board_cons:_meta"}) or {}

        rows: List[Dict[str, Any]] = []
        status_counts: Dict[str, int] = {}
        completed = 0
        critical_task_count = 0
        critical_completed = 0
        optional_task_count = 0
        optional_completed = 0
        progress_values: List[float] = []
        critical_progress_values: List[float] = []
        optional_progress_values: List[float] = []
        critical_status_counts: Dict[str, int] = {}
        optional_status_counts: Dict[str, int] = {}
        sample_errors: List[Any] = []
        for task in tasks:
            module = str(task.get("module") or "")
            status = str(task.get("status") or "pending")
            blocks_run = bool(task.get("blocks_run", True))
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "ok":
                completed += 1
            summary = dict(task.get("result_summary") or {})
            cursor = dict(task.get("cursor") or {})
            shard_key = str(task.get("shard_key") or "all")
            if module == "stock_daily":
                shard_progress = self._find_one(db, "sync_log", {"_id": f"stock_daily:progress:{shard_key}"}) if shard_key not in {"all", "aggregate"} else stock_daily_progress
                if shard_progress:
                    summary.update(self._project_fields(shard_progress, [
                        "scope", "shard_key", "shard_index", "shard_count", "global_total",
                        "total", "processed", "remaining", "inserted", "skipped",
                        "errors", "deferred", "latest_symbol", "latest_status", "progress_pct",
                        "inserted_per_min", "processed_per_min", "landing_rate",
                        "missing_symbols", "deferred_symbols", "elapsed_seconds",
                    ]))
            if module == "stock_daily" and stock_daily_progress:
                cursor.update(self._project_fields(stock_daily_progress, [
                    "shard_count", "global_total",
                ]))
            if module == "board_cons":
                shard_progress = self._find_one(db, "sync_log", {"_id": f"board_cons:{shard_key}:_meta"}) if shard_key not in {"all", "aggregate"} else board_cons_progress
                if shard_progress:
                    summary.update(self._project_fields(shard_progress, [
                        "shard_key", "processed", "processed_groups", "remaining", "next_cursor",
                        "total_groups", "sample_errors", "unmapped", "source_counts",
                    ]))
                    cursor.update(self._project_fields(shard_progress, ["next_cursor", "remaining", "total_groups"]))
            progress_pct = self._task_progress_pct(status, summary, cursor)
            eta_seconds = self._task_eta_seconds(task, progress_pct)
            if progress_pct is not None:
                progress_values.append(progress_pct)
                if blocks_run:
                    critical_progress_values.append(progress_pct)
                else:
                    optional_progress_values.append(progress_pct)
            task_done = status == "ok" or (
                status in {"partial", "degraded"} and progress_pct is not None and progress_pct >= 99.9
            )
            if blocks_run:
                critical_task_count += 1
                critical_status_counts[status] = critical_status_counts.get(status, 0) + 1
                if task_done:
                    critical_completed += 1
            else:
                optional_task_count += 1
                optional_status_counts[status] = optional_status_counts.get(status, 0) + 1
                if task_done:
                    optional_completed += 1
            errors = summary.get("sample_errors")
            if isinstance(errors, list):
                sample_errors.extend(errors[:3])
            row = {
                "task_id": str(task.get("_id") or ""),
                "module": module,
                "phase": task.get("phase") or "",
                "shard_key": task.get("shard_key") or "all",
                "status": status,
                "blocks_run": blocks_run,
                "attempts": int(task.get("attempts") or 0),
                "depends_on": task.get("depends_on") or [],
                "cursor": _json_safe(cursor),
                "result_summary": _json_safe(summary),
                "progress_pct": progress_pct,
                "eta_seconds": eta_seconds,
                "error_msg": str(task.get("error_msg") or "")[:500],
                "started_at": self._iso(task.get("started_at")),
                "updated_at": self._iso(task.get("updated_at")),
                "heartbeat_at": self._iso(task.get("heartbeat_at")),
                "finished_at": self._iso(task.get("finished_at")),
            }
            rows.append(row)

        task_count = len(rows)
        run_status = str(run.get("status") or "")
        all_tasks_ok = bool(task_count and completed == task_count and set(status_counts) <= {"ok"})
        if run_status == "ok" or all_tasks_ok:
            progress_pct = 100.0
            eta_seconds = 0
        else:
            progress_pct = round(sum(progress_values) / task_count, 2) if task_count and progress_values else (
                round(completed / task_count * 100, 2) if task_count else 0
            )
            eta_seconds = self._postmarket_eta_seconds(rows, progress_pct, run.get("started_at"))
        if critical_task_count and critical_completed == critical_task_count:
            critical_progress_pct = 100.0
            critical_status = "ok"
        else:
            critical_progress_pct = (
                round(sum(critical_progress_values) / critical_task_count, 2)
                if critical_task_count and critical_progress_values
                else (round(critical_completed / critical_task_count * 100, 2) if critical_task_count else 100.0)
            )
            critical_status = run_status or "pending"
        optional_progress_pct = (
            round(sum(optional_progress_values) / optional_task_count, 2)
            if optional_task_count and optional_progress_values
            else (round(optional_completed / optional_task_count * 100, 2) if optional_task_count else 100.0)
        )
        return {
            "run": {
                "run_id": run_id,
                "trade_date": str(run.get("trade_date") or ""),
                "status": run_status,
                "recovery_state": str(run.get("recovery_state") or ""),
                "critical_blocker": _json_safe(run.get("critical_blocker") or {}),
                "blocked_tasks": _json_safe(run.get("blocked_tasks") or []),
                "optional_blocked_tasks": _json_safe(run.get("optional_blocked_tasks") or []),
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
                "critical_task_count": critical_task_count,
                "critical_completed": critical_completed,
                "critical_status": critical_status,
                "critical_progress_pct": critical_progress_pct,
                "critical_status_counts": critical_status_counts,
                "optional_task_count": optional_task_count,
                "optional_completed": optional_completed,
                "optional_progress_pct": optional_progress_pct,
                "optional_status_counts": optional_status_counts,
                "eta_seconds": eta_seconds,
                "sample_errors": _json_safe(sample_errors[:10]),
                "recovery_state": str(run.get("recovery_state") or ""),
                "critical_blocker": _json_safe(run.get("critical_blocker") or {}),
                "stock_daily_landing_rate": stock_daily_progress.get("landing_rate", 0),
                "stock_daily_inserted_per_min": stock_daily_progress.get("inserted_per_min", 0),
                "stock_daily_missing_symbols": stock_daily_progress.get("missing_symbols", 0),
                "stock_daily_deferred_symbols": stock_daily_progress.get("deferred_symbols", stock_daily_progress.get("deferred", 0)),
            },
        }

    def _current_postmarket_tasks(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = [task for task in tasks if str(task.get("status") or "") != "obsolete"]
        try:
            from signals.sync.postmarket import POSTMARKET_TASKS

            current = {(task.module, task.shard_key) for task in POSTMARKET_TASKS}
        except Exception:
            return rows
        if not current:
            return rows
        filtered = [
            task for task in rows
            if (str(task.get("module") or ""), str(task.get("shard_key") or "all")) in current
        ]
        return filtered or rows

    def _cache_daily_coverage_date(self, db, trade_date: str) -> str:
        fallback = str(trade_date or "")[:10]
        latest = self._find_one(db, "bars", {"meta.freq": "日线"}, {"dt": 1}, sort=[("dt", -1)]) or {}
        latest_dt = self._coerce_datetime(latest.get("dt"))
        if latest_dt:
            return latest_dt.date().isoformat()
        run = self._find_one(
            db,
            "sync_runs",
            {"run_id": {"$regex": "^postmarket:"}},
            {"trade_date": 1, "started_at": 1, "updated_at": 1},
            sort=[("started_at", -1), ("updated_at", -1)],
        ) or {}
        return str(run.get("trade_date") or fallback)[:10]

    def _cache_mongo_stock_cache(self, db, trade_date: str) -> Dict[str, Any]:
        freqs = ["日线", "周线", "5分钟", "15分钟", "30分钟", "60分钟"]
        rows: List[Dict[str, Any]] = []
        stock_daily_progress = self._find_one(db, "sync_log", {"_id": "stock_daily:progress:_meta"}) or {}
        daily_coverage_date = self._cache_daily_coverage_date(db, trade_date)
        daily_snapshot_coverage = self._cache_daily_snapshot_coverage(db, daily_coverage_date)
        stock_minute_doc = (
            self._find_one(db, "sync_log", {"_id": "stock_minute:selection:_meta"})
            or self._find_one(db, "sync_log", {"_id": "stock_minute:_meta"})
            or {}
        )
        minute_result = dict(stock_minute_doc.get("result") or {})
        minute_symbols = len(stock_minute_doc.get("selected_symbols") or []) or int(minute_result.get("selected") or 0)
        tail_counts = dict(stock_minute_doc.get("tail_counts") or minute_result.get("tail_counts") or {})
        minute_universe = self._cache_minute_universe(db, trade_date)
        daily_latest = None
        for freq in freqs:
            query = {"meta.freq": freq}
            latest = self._find_one(db, "bars", query, {"dt": 1, "meta.symbol": 1}, sort=[("dt", -1)])
            if freq == "日线":
                daily_latest = latest
                progress_total = int(stock_daily_progress.get("total") or stock_daily_progress.get("expected_codes") or 0)
                processed = int(stock_daily_progress.get("processed") or 0)
                valid_universe = int(daily_snapshot_coverage.get("valid_universe") or 0)
                cached_today = int(daily_snapshot_coverage.get("cached_today") or 0)
                symbols = progress_total
                today_symbols = processed
                total_bars = int(stock_daily_progress.get("inserted") or 0)
                latest_symbol = str(stock_daily_progress.get("latest_symbol") or (latest or {}).get("meta", {}).get("symbol", ""))
                source = "sync_log:stock_daily:progress"
                if valid_universe > 0:
                    symbols = valid_universe
                    today_symbols = cached_today
                    source = daily_snapshot_coverage.get("source") or "fullmarket_spot_snapshots+bars"
            elif freq in {"5分钟", "15分钟", "30分钟"}:
                symbols = minute_symbols
                today_symbols = minute_symbols
                total_bars = int(minute_symbols * int(tail_counts.get(freq) or 0))
                latest_symbol = str((latest or {}).get("meta", {}).get("symbol", ""))
                source = "sync_log:stock_minute:selection"
            else:
                symbols = 1 if latest else 0
                today_symbols = 0
                total_bars = 0
                latest_symbol = str((latest or {}).get("meta", {}).get("symbol", ""))
                source = "latest_probe"
            if freq == "日线":
                symbols = max(symbols, today_symbols)
            rows.append({
                "freq": freq,
                "symbols": symbols,
                "today_symbols": today_symbols,
                "total_bars": total_bars,
                "latest_dt": self._iso(latest.get("dt") if latest else None),
                "latest_symbol": latest_symbol,
                "source": source,
                "coverage_date": daily_coverage_date if freq == "日线" else trade_date,
            })

        daily = next((item for item in rows if item.get("freq") == "日线"), {})
        return {
            "freqs": rows,
            "summary": {
                "daily_symbols": daily.get("symbols", 0),
                "daily_today_symbols": daily.get("today_symbols", 0),
                "daily_coverage_date": daily_coverage_date,
                "latest_daily_dt": daily.get("latest_dt") or self._iso((daily_latest or {}).get("dt") if daily_latest else None),
                "minute_universe_total": minute_universe.get("total", 0),
                "minute_universe_cached": minute_universe.get("cached", 0),
                "minute_universe_pending": minute_universe.get("pending", 0),
                "minute_universe_running": minute_universe.get("running", 0),
                "minute_universe_error": minute_universe.get("error", 0),
                "minute_universe_dropped": minute_universe.get("dropped", 0),
                "daily_landing_rate": stock_daily_progress.get("landing_rate", 0),
                "daily_inserted_per_min": stock_daily_progress.get("inserted_per_min", 0),
                "daily_missing_symbols": (
                    daily.get("symbols", 0) - daily.get("today_symbols", 0)
                    if daily_snapshot_coverage.get("valid_universe")
                    else stock_daily_progress.get("missing_symbols", 0)
                ),
                "daily_deferred_symbols": stock_daily_progress.get("deferred_symbols", stock_daily_progress.get("deferred", 0)),
                "daily_coverage_source": daily_snapshot_coverage.get("source") or "sync_log:stock_daily:progress",
                "daily_invalid_snapshot_rows": daily_snapshot_coverage.get("invalid_rows", 0),
            },
        }

    def _cache_daily_snapshot_coverage(self, db, trade_date: str) -> Dict[str, Any]:
        date_key = str(trade_date or "").replace("-", "")[:8]
        if not date_key:
            return {}
        try:
            trade_dt = datetime.strptime(date_key, "%Y%m%d")
        except ValueError:
            return {}
        valid_query = {
            "date_key": date_key,
            "code": {"$regex": r"^\d{6}$"},
            "price": {"$gt": 0},
            "open": {"$gt": 0},
            "high": {"$gt": 0},
            "low": {"$gt": 0},
        }
        try:
            valid_codes = set(db["fullmarket_spot_snapshots"].distinct("code", valid_query))
            if not valid_codes:
                return {}
            cached_codes = set(db["bars"].distinct("meta.symbol", {"meta.freq": "日线", "dt": trade_dt}))
            invalid_rows = self._count(db, "fullmarket_spot_snapshots", {"date_key": date_key}) - len(valid_codes)
            return {
                "valid_universe": len(valid_codes),
                "cached_today": len(valid_codes.intersection(cached_codes)),
                "invalid_rows": max(0, invalid_rows),
                "source": "fullmarket_spot_snapshots.valid_universe + bars.daily",
            }
        except Exception:
            return {}

    def _cache_minute_universe(self, db, trade_date: str) -> Dict[str, int]:
        try:
            rows = list(db["minute_preheat_universe"].find({"trade_date": trade_date}, {"status": 1}))
        except Exception:
            return {}
        counts: Dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "pending")
            counts[status] = counts.get(status, 0) + 1
        active_statuses = ("cached", "pending", "running", "error", "stale")
        counts["total"] = sum(counts.get(status, 0) for status in active_statuses)
        return counts

    def _cache_minute_not_ready_samples(self, db, limit: int = 8) -> List[Dict[str, Any]]:
        try:
            latest = db["minute_readiness"].find_one({}, {"trade_date": 1}, sort=[("checked_at", -1)])
            trade_date = latest.get("trade_date") if latest else None
            query = {"status": {"$ne": "ready"}}
            if trade_date:
                query["trade_date"] = trade_date
            rows = list(
                db["minute_readiness"].find(
                    query,
                    {"_id": 0, "domain": 1, "symbol": 1, "freq": 1, "root_cause_class": 1, "latest_dt": 1, "source": 1},
                ).sort([("domain", 1), ("symbol", 1), ("freq", 1)]).limit(limit)
            )
        except Exception:
            return []
        return [_json_safe(row) for row in rows]

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
                "ready_date": self._terminal_doc_date(latest or {}),
                "status": (latest or {}).get("status") or ("ok" if latest else "missing"),
            })
        return rows

    def _terminal_doc_date(self, doc: Mapping[str, Any]) -> str:
        for key in ("trade_date", "as_of", "date", "dt"):
            value = doc.get(key)
            if value:
                text = self._iso(value)[:10]
                if text:
                    return text
        return ""

    def _cache_terminal_ready_date(
        self,
        trade_date: str,
        postmarket: Mapping[str, Any],
        terminal_outputs: List[Dict[str, Any]],
    ) -> str:
        tasks = [
            task
            for task in postmarket.get("tasks", [])
            if str(task.get("module") or "") in {"terminal_realtime_pool", "strategy_snapshot", "cache_preheat"}
        ]
        if (
            str((postmarket.get("run") or {}).get("trade_date") or "")[:10] == str(trade_date or "")[:10]
            and tasks
            and all(str(task.get("status") or "") == "ok" for task in tasks)
        ):
            return str(trade_date or "")[:10]
        dates = [
            str(item.get("ready_date") or "")[:10]
            for item in terminal_outputs
            if str(item.get("collection") or "") in {"terminal_stock_pool", "strategy_snapshots"}
            and str(item.get("ready_date") or "")[:10]
        ]
        return min(dates) if dates else ""

    def _cache_provider_health(self, db) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            docs = db["provider_health"].find({}, {"_id": 0}).sort("updated_at", -1).limit(12)
        except Exception:
            return rows
        for doc in docs:
            rows.append({
                "provider": str(doc.get("provider") or ""),
                "endpoint": str(doc.get("endpoint") or ""),
                "domain": str(doc.get("domain") or ""),
                "status": self._provider_health_status(doc),
                "avg_latency_ms": doc.get("avg_latency_ms"),
                "last_error_type": str(doc.get("last_error_type") or "")[:240],
                "cooldown_hit_type": str(doc.get("cooldown_hit_type") or "")[:240],
                "attempt_count": int(doc.get("attempt_count") or 0),
                "success_count": int(doc.get("success_count") or 0),
                "risk_error_count": int(doc.get("risk_error_count") or 0),
                "cooldown_hit_count": int(doc.get("cooldown_hit_count") or 0),
                "degraded_count": int(doc.get("degraded_count") or 0),
                "last_success_at": self._iso(doc.get("last_success_at")),
                "last_error_at": self._iso(doc.get("last_error_at")),
                "cooldown_until": self._iso(doc.get("cooldown_until")),
                "updated_at": self._iso(doc.get("updated_at")),
            })
        return rows

    def _provider_blocker_row(self, item: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "scope": "provider_health",
            "module": item.get("provider"),
            "provider": item.get("provider"),
            "endpoint": item.get("endpoint"),
            "domain": item.get("domain") or "",
            "status": item.get("status"),
            "error_msg": item.get("last_error_type") or item.get("cooldown_hit_type") or "",
            "last_error_at": item.get("last_error_at") or "",
            "last_success_at": item.get("last_success_at") or "",
            "cooldown_until": item.get("cooldown_until") or "",
        }

    @staticmethod
    def _is_fullmarket_provider(item: Mapping[str, Any]) -> bool:
        return (
            str(item.get("provider") or "") == "eastmoney"
            and str(item.get("endpoint") or "") == "fullmarket_spot_snapshot"
        )

    def _provider_problem_blockers(self, provider_health: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in provider_health or []:
            status = str(item.get("status") or "")
            if status in {"degraded", "error", "stale"} and not item.get("updated_at"):
                continue
            if status in {"degraded", "cooldown", "error", "stale"} and self._provider_has_healthy_peer(item, provider_health or []):
                continue
            if status in {"degraded", "cooldown", "error", "stale"}:
                rows.append(self._provider_blocker_row(item))
        rows.sort(key=lambda item: 0 if self._is_fullmarket_provider(item) else 1)
        return rows

    def _cache_critical_blocker(
        self,
        postmarket: Mapping[str, Any],
        provider_health: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        provider_blocker = next(
            (item for item in self._provider_problem_blockers(provider_health) if self._is_fullmarket_provider(item)),
            None,
        )
        if provider_blocker:
            return provider_blocker
        provider_recovered = any(
            self._is_fullmarket_provider(item)
            and str(item.get("status") or "") in {"ok", "running"}
            and bool(item.get("last_success_at"))
            for item in provider_health or []
        )
        run = postmarket.get("run") if isinstance(postmarket.get("run"), Mapping) else {}
        run_blocker = run.get("critical_blocker") if isinstance(run.get("critical_blocker"), Mapping) else {}
        if run_blocker and self._is_fullmarket_provider(run_blocker) and not provider_recovered:
            return _json_safe(dict(run_blocker))
        if not provider_recovered:
            for task in postmarket.get("tasks", []):
                if str(task.get("module") or "") != "fullmarket_spot_snapshot":
                    continue
                status = str(task.get("status") or "")
                if status in {"degraded", "error", "stale"}:
                    return {
                        "scope": "postmarket_backfill",
                        "module": "fullmarket_spot_snapshot",
                        "provider": "eastmoney",
                        "endpoint": "fullmarket_spot_snapshot",
                        "status": status,
                        "error_msg": task.get("error_msg") or "",
                    }
        return {}

    def _cache_recovery_state(
        self,
        *,
        trade_date: str,
        daily_coverage_date: str,
        terminal_ready_date: str,
        postmarket: Mapping[str, Any],
        critical_blocker: Mapping[str, Any],
    ) -> str:
        if critical_blocker:
            run_state = str(((postmarket.get("run") or {}).get("recovery_state") if isinstance(postmarket.get("run"), Mapping) else "") or "")
            return run_state if run_state in {"waiting_for_source", "partial/source_blocked"} else "source_blocked"
        run = postmarket.get("run") if isinstance(postmarket.get("run"), Mapping) else {}
        run_status = str(run.get("status") or "")
        if (
            str(trade_date or "")[:10]
            and str(daily_coverage_date or "")[:10]
            and str(daily_coverage_date or "")[:10] != str(trade_date or "")[:10]
        ):
            return "old_cache_readable"
        if str(trade_date or "")[:10] and terminal_ready_date and terminal_ready_date == str(trade_date or "")[:10]:
            return "terminal_ready"
        if run_status in {"running", "partial"}:
            return "postmarket_running"
        if run_status == "ok":
            return "ok"
        return run_status or "unknown"

    def _cache_blockers(self, live: Dict[str, Any], postmarket: Dict[str, Any], provider_health: List[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
        blockers: List[Dict[str, Any]] = []
        provider_blockers = self._provider_problem_blockers(provider_health or [])
        for item in live.get("modules", []):
            status = str(item.get("status") or "")
            failed_calls = self._int_from_result(item, "failed_calls") or self._int_from_result(item, "errors")
            minute_not_ready = self._int_from_result(item, "not_ready")
            error_msg = str(item.get("error_msg") or item.get("degraded_reason") or "")
            if (
                status in {"error", "degraded", "missing", "partial", "running", "stale"}
                or failed_calls > 0
                or minute_not_ready > 0
                or "orphaned_running_module" in error_msg
            ):
                blockers.append({
                    "scope": "live_low_latency",
                    "module": item.get("module"),
                    "status": status,
                    "failed_calls": failed_calls,
                    "not_ready": minute_not_ready,
                    "error_msg": error_msg,
                })
        postmarket_by_module: Dict[str, Dict[str, Any]] = {}
        for task in postmarket.get("tasks", []):
            status = str(task.get("status") or "")
            if status in {"error", "degraded", "stale"}:
                module = str(task.get("module") or "postmarket")
                summary = task.get("result_summary", {}) if isinstance(task.get("result_summary"), dict) else {}
                result = summary.get("result") if isinstance(summary.get("result"), dict) else {}
                row = postmarket_by_module.setdefault(module, {
                    "scope": "postmarket_backfill",
                    "module": module,
                    "status": status,
                    "task_count": 0,
                    "error_count": 0,
                    "deferred_count": 0,
                    "error_msg": "",
                    "sample_errors": [],
                })
                row["task_count"] = int(row.get("task_count") or 0) + 1
                row["error_count"] = int(row.get("error_count") or 0) + int(summary.get("errors") or result.get("errors") or 0)
                row["deferred_count"] = int(row.get("deferred_count") or 0) + int(summary.get("deferred") or result.get("deferred") or 0)
                if status == "error":
                    row["status"] = "error"
                elif row.get("status") != "error" and status == "degraded":
                    row["status"] = "degraded"
                if not row.get("error_msg"):
                    row["error_msg"] = task.get("error_msg") or ""
                errors = summary.get("sample_errors") or result.get("sample_errors") or []
                if isinstance(errors, list):
                    row["sample_errors"] = [*row.get("sample_errors", []), *errors[:3]][:6]
        blockers.extend(postmarket_by_module.values())
        return [*provider_blockers, *blockers][:12]

    @staticmethod
    def _provider_has_healthy_peer(item: Mapping[str, Any], provider_health: List[Dict[str, Any]]) -> bool:
        domain = str(item.get("domain") or "")
        endpoint = str(item.get("endpoint") or "")
        provider = str(item.get("provider") or "")
        if not domain or not endpoint:
            return False
        for peer in provider_health:
            if str(peer.get("provider") or "") == provider:
                continue
            if str(peer.get("domain") or "") != domain or str(peer.get("endpoint") or "") != endpoint:
                continue
            if str(peer.get("status") or "") in {"ok", "running"} and peer.get("last_success_at"):
                return True
        return False

    def _provider_health_status(self, doc: Mapping[str, Any]) -> str:
        status = str(doc.get("status") or "")
        if status != "cooldown":
            return status
        cooldown_until = doc.get("cooldown_until")
        updated_at = doc.get("updated_at")
        if (
            isinstance(cooldown_until, datetime)
            and (
                cooldown_until <= naive_market_now("A")
                or (isinstance(updated_at, datetime) and cooldown_until <= updated_at)
            )
        ):
            return "cooldown_expired"
        return status

    def _latest_sync_doc(self, db, module: str) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        for doc_id in (f"{module}:A:_meta", f"{module}:_meta"):
            doc = self._find_one(db, "sync_log", {"_id": doc_id})
            if doc:
                candidates.append(doc)
        doc = self._find_one(db, "sync_log", {"module": module}, sort=[("last_run", -1), ("updated_at", -1)])
        if doc:
            candidates.append(doc)
        if not candidates:
            return {}

        def sort_key(item: Mapping[str, Any]) -> datetime:
            return self._coerce_datetime(item.get("last_run") or item.get("updated_at")) or datetime.min

        return max(candidates, key=sort_key)

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
            "raw_status": str(doc.get("status") or ""),
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
        row["status"] = self._cache_effective_module_status(row)
        if row["status"] != "ok" and row["status"] != row.get("raw_status") and not row.get("degraded_reason"):
            row["degraded_reason"] = "strict_low_latency_status"
        return row

    def _cache_effective_module_status(self, row: Mapping[str, Any]) -> str:
        status = str(row.get("raw_status") or row.get("status") or "missing").lower()
        if not status:
            return "missing"
        pause_ok = self._cache_a_share_low_latency_pause_ok(row)
        if pause_ok and self._a_share_low_latency_non_trading_day():
            if status in {"ok", "partial", "degraded", "stale", "warm", "fresh"}:
                return "ok"
        if pause_ok and status in {"stale", "warm", "fresh"}:
            return "ok"
        if pause_ok and status == "ok" and row.get("freshness") == "stale":
            return "ok"
        if status != "ok":
            return status
        error_msg = str(row.get("error_msg") or "").lower()
        if "orphaned_running_module" in error_msg:
            return "degraded"
        if self._int_from_result(row, "failed_calls") > 0 or self._int_from_result(row, "errors") > 0:
            return "partial"
        if self._int_from_result(row, "not_ready") > 0:
            return "partial"
        planned = self._int_from_result(row, "planned_calls")
        empty = self._int_from_result(row, "empty_calls") or self._int_from_result(row, "empty")
        if planned > 0 and empty >= planned:
            return "partial"
        if row.get("freshness") == "stale":
            if pause_ok:
                return "ok"
            return "stale"
        return "ok"

    def _cache_a_share_low_latency_pause_ok(self, row: Mapping[str, Any]) -> bool:
        module = str(row.get("module") or "")
        if module not in {
            "quote_snapshots",
            "stock_minute",
            "index_minute",
            "minute_readiness_probe",
            "market_pools",
            "board_heat_minute",
            "concept_heat_minute",
            "chain_heat_snapshots",
        }:
            return False
        if not self._a_share_low_latency_paused():
            return False
        return self._cache_row_touched_current_market_day(row)

    def _a_share_low_latency_paused(self) -> bool:
        now = naive_market_now("A")
        if self._a_share_low_latency_non_trading_day(now):
            return True
        if now.weekday() >= 5:
            return True
        current = now.time()
        return not (
            datetime_time(9, 30) <= current < datetime_time(11, 30)
            or datetime_time(13, 0) <= current < datetime_time(15, 0)
        )

    def _a_share_low_latency_non_trading_day(self, now: datetime | None = None) -> bool:
        now = now or naive_market_now("A")
        try:
            from signals.core.trading_dates import is_trading_day

            return not is_trading_day("A", now.date())
        except Exception:
            return now.weekday() >= 5

    def _cache_row_touched_current_market_day(self, row: Mapping[str, Any]) -> bool:
        today = naive_market_now("A").date()
        accepted_dates = {today.isoformat()}
        earliest_date = today
        try:
            from signals.data.mongo_fallback import get_last_trading_day

            last_trade_day = str(get_last_trading_day("A") or "")[:10]
            if last_trade_day:
                accepted_dates.add(last_trade_day)
                earliest_date = datetime.strptime(last_trade_day, "%Y-%m-%d").date()
        except Exception:
            pass

        def is_today(value: Any) -> bool:
            dt = self._coerce_datetime(value)
            if dt:
                day = dt.date()
                return day.isoformat() in accepted_dates or earliest_date <= day <= today
            text = str(value or "")
            if not text:
                return False
            if text[:10] in accepted_dates:
                return True
            try:
                day = datetime.strptime(text[:10], "%Y-%m-%d").date()
                return earliest_date <= day <= today
            except ValueError:
                return False

        if is_today(row.get("latest_dt")) or is_today(row.get("last_run")):
            return True
        result = row.get("result")
        if not isinstance(result, Mapping):
            return False
        return any(
            is_today(result.get(key))
            for key in (
                "latest_dt",
                "last_dt",
                "latest_minute",
                "trade_date",
                "as_of",
                "updated_at",
                "last_run",
            )
        )

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

    def _task_eta_seconds(self, task: Mapping[str, Any], progress_pct: Optional[float]) -> Optional[int]:
        if progress_pct is None or progress_pct <= 0 or progress_pct >= 100:
            return None
        started_at = self._coerce_datetime(task.get("started_at"))
        if not started_at:
            return None
        elapsed = max(0, (naive_market_now("A") - started_at).total_seconds())
        total_estimate = elapsed / (progress_pct / 100)
        remaining = int(max(0, total_estimate - elapsed))
        return remaining

    def _postmarket_eta_seconds(
        self,
        rows: List[Mapping[str, Any]],
        progress_pct: float,
        started_at_raw: Any,
    ) -> Optional[int]:
        running_etas = [
            int(row["eta_seconds"])
            for row in rows
            if isinstance(row.get("eta_seconds"), int)
        ]
        if running_etas:
            return max(running_etas)
        if progress_pct <= 0 or progress_pct >= 100:
            return None
        started_at = self._coerce_datetime(started_at_raw)
        if not started_at:
            return None
        elapsed = max(0, (naive_market_now("A") - started_at).total_seconds())
        return int(max(0, elapsed / (progress_pct / 100) - elapsed))

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
        return max(0, int((naive_market_now("A") - dt).total_seconds()))

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
            return value.astimezone(market_timezone("A")).replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.astimezone(market_timezone("A")).replace(tzinfo=None) if parsed.tzinfo else parsed
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
            if isinstance(value, str) and value.strip().isdigit():
                return int(value)
        value = row.get(key)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return 0

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
            from signals.data.mongo_fallback import get_db

            db = get_db()
            if db is not None:
                doc = db["strategy_snapshots"].find_one(
                    {"snapshot": {"$exists": True}},
                    {"_id": 0, "snapshot": 1},
                    sort=[("updated_at", -1), ("as_of", -1)],
                )
                if doc and isinstance(doc.get("snapshot"), Mapping):
                    snapshot = dict(doc["snapshot"])
                    snapshot.setdefault("read_model_source", "mongodb.strategy_snapshots")
                    return snapshot
        except Exception:
            pass
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

    def _ai_factor_factory(self) -> Dict[str, Any]:
        try:
            from signals.strategy.ai_factor_factory import build_ai_factor_factory

            factory = build_ai_factor_factory()
            return dict(factory) if isinstance(factory, Mapping) else {}
        except Exception as exc:
            return {
                "title": "AI因子工厂",
                "summary": {"total": 0, "verified": 0, "live_enabled": 0, "draft": 0},
                "factors": [],
                "active_factor_id": "",
                "error": f"ai_factor_factory_error:{exc.__class__.__name__}",
            }

    def _ai_factor_factory_stub(self, strategy_snapshot: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "factory_id": "ai_factor_factory",
            "as_of": str(strategy_snapshot.get("as_of") or ""),
            "generated_at": str(strategy_snapshot.get("generated_at") or ""),
            "title": "AI因子工厂",
            "summary": {"load_mode": "lazy"},
            "active_factor_id": "",
            "phases": [],
            "research_modes": {},
            "factor_registry": {},
            "candidate_factor_ideas": [],
            "factor_idea_queue": [],
            "ideas": [],
            "factors": [],
            "data_lineage": {"source": "/api/strategy/ai-factor-factory"},
            "error": "",
        }

    def _snapshot_source_freshness(self, snapshot: Mapping[str, Any], source_name: str) -> str:
        confidence = snapshot.get("source_confidence") or {}
        for item in confidence.get("sources", []) if isinstance(confidence, Mapping) else []:
            if item.get("name") == source_name:
                return str(item.get("freshness") or "")
        return ""

    def _record_strategy_snapshot_run(self, snapshot: Mapping[str, Any]) -> None:
        as_of = str(snapshot.get("as_of") or naive_market_now("A").date().isoformat())[:10]
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
