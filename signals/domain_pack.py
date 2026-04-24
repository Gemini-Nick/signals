from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import config


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
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
        runs = await self.list_runs()
        recent_runs = runs[:recent_limit]
        review_runs = [run for run in recent_runs if run.get("capability") == "review"][:10]
        return {
            "pack_id": "signals",
            "title": "Signals",
            "recent_runs": recent_runs,
            "review_runs": review_runs,
            "backtest_summary": self._backtest_summary(),
            "pending_backlog_preview": self._pending_backlog_preview(backlog_limit),
            "connector_health": self._connector_health(),
            "operator_actions": [
                {
                    "action_id": "pack:signals:run:review",
                    "run_id": "signals:dashboard",
                    "kind": "run_pack",
                    "label": "Run Review",
                    "payload": {"pack_id": "signals", "capability": "review", "input": {"mode": "review"}},
                },
                {
                    "action_id": "pack:signals:run:backtest",
                    "run_id": "signals:dashboard",
                    "kind": "run_pack",
                    "label": "Run Backtest",
                    "payload": {"pack_id": "signals", "capability": "backtest", "input": {"mode": "backtest"}},
                },
            ],
        }

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

        journal = SignalJournal()
        try:
            summary = journal.summary()
            return {
                "total": int(summary.get("total") or 0),
                "evaluated": int(summary.get("evaluated") or 0),
                "pending": int(summary.get("pending") or 0),
            }
        finally:
            journal.close()

    def _pending_backlog_preview(self, limit: int) -> List[Dict[str, Any]]:
        from signals.core.backtest import SignalJournal

        journal = SignalJournal()
        try:
            return [dict(item) for item in journal.get_pending()[:limit]]
        finally:
            journal.close()

    def _connector_health(self) -> List[Dict[str, Any]]:
        return [
            {
                "connector_id": "tushare",
                "status": "ok" if config.TUSHARE_TOKEN else "critical",
                "summary": "Tushare market data token",
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
