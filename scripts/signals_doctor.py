#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _http_json(url: str, timeout: int = 10) -> tuple[bool, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
        return True, {"url": url, "status_code": response.status, "payload": payload}
    except urllib.error.HTTPError as exc:
        return False, {"url": url, "status_code": exc.code, "error": str(exc)}
    except Exception as exc:
        return False, {"url": url, "error": f"{exc.__class__.__name__}: {exc}"}


def _launchd(label: str) -> dict[str, Any]:
    try:
        uid = subprocess.check_output(["id", "-u"], text=True).strip()
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        return {
            "label": label,
            "ok": result.returncode == 0,
            "summary": "\n".join(
                line.strip()
                for line in result.stdout.splitlines()
                if "state =" in line or "pid =" in line or "runs =" in line
            ),
        }
    except Exception as exc:
        return {"label": label, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def _launchd_absent(label: str) -> dict[str, Any]:
    detail = _launchd(label)
    return {
        "label": label,
        "ok": not detail.get("ok", False),
        "loaded": bool(detail.get("ok", False)),
        "summary": detail.get("summary", ""),
    }


def _mongo_check() -> dict[str, Any]:
    try:
        from signals.data.mongo_fallback import get_db

        db = get_db()
        if db is None:
            return {"ok": False, "status": "disabled"}
        db.command("ping")
        return {
            "ok": True,
            "database": db.name,
            "counts": {
                name: db[name].estimated_document_count()
                for name in ["bars", "quote_snapshots", "market_pools", "signals"]
            },
        }
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def _compact_endpoint(name: str, detail: dict[str, Any]) -> dict[str, Any]:
    if "payload" not in detail:
        return detail

    payload = detail.get("payload") or {}
    compact = {k: v for k, v in detail.items() if k != "payload"}
    if name == "pack_dashboard":
        overview = payload.get("overview") or {}
        coverage = overview.get("market_regime") or {}
        compact["payload"] = {
            "status": payload.get("status"),
            "diagnostics": payload.get("diagnostics", []),
            "market_regime": {
                "label": coverage.get("label"),
                "primary_theme": coverage.get("primary_theme"),
                "active_pool_count": coverage.get("active_pool_count"),
                "signal_count": coverage.get("signal_count"),
                "candidate_count": coverage.get("candidate_count"),
                "warning_count": coverage.get("warning_count"),
            },
        }
    elif name == "cache_health":
        compact["payload"] = {
            "status": payload.get("status"),
            "coverage": payload.get("coverage", {}),
        }
    elif name == "cluster_latest":
        compact["payload"] = {
            "status": payload.get("status"),
            "cluster_count": len(payload.get("clusters", []) or []),
            "market_status": payload.get("market_status", {}),
        }
    elif name == "backtest_health":
        compact["payload"] = {
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "live_check": payload.get("live_check"),
            "checks": payload.get("checks", []),
        }
    else:
        compact["payload"] = payload
    return compact


def main() -> int:
    _load_env_file(Path.home() / ".longclaw" / "runtime-v2" / "stack.env")
    _load_env_file(REPO_ROOT / ".env")

    base_url = os.environ.get("LONGCLAW_SIGNALS_WEB_BASE_URL", "http://127.0.0.1:8011").rstrip("/")
    verbose = "--verbose" in sys.argv[1:]
    checks: dict[str, Any] = {
        "mongo": _mongo_check(),
        "launchd_runtime": _launchd("com.zhangqilong.ai.signals.runtime"),
        "retired_launchd_signals_web_absent": _launchd_absent("com.zhangqilong.ai.signals.web"),
        "retired_launchd_signals_sync_absent": _launchd_absent("com.zhangqilong.ai.signals.sync"),
        "retired_launchd_signals_web2_absent": _launchd_absent("com.zhangqilong.ai.signals.web2"),
        "retired_launchd_signals_quote_absent": _launchd_absent("com.zhangqilong.ai.signals.quote"),
        "retired_launchd_signals_signal_absent": _launchd_absent("com.zhangqilong.ai.signals.signal"),
        "retired_launchd_signals_workbench_absent": _launchd_absent("com.zhangqilong.ai.signals.workbench"),
        "retired_launchd_signals_board_absent": _launchd_absent("com.zhangqilong.ai.signals.board"),
    }

    endpoints = {
        "pack_dashboard": f"{base_url}/api/pack/dashboard",
        "cache_health": f"{base_url}/api/health/cache",
        "cluster_latest": f"{base_url}/api/cluster/latest?top=1",
        "backtest_health": f"{base_url}/api/backtest/health/push2his",
    }
    for name, url in endpoints.items():
        ok, detail = _http_json(url)
        detail = {"ok": ok, **detail}
        checks[name] = detail if verbose else _compact_endpoint(name, detail)

    coverage = checks.get("cache_health", {}).get("payload", {}).get("coverage", {})
    dashboard = checks.get("pack_dashboard", {}).get("payload", {})
    critical_ok = all(
        checks[name].get("ok")
        for name in [
            "mongo",
            "launchd_runtime",
            "retired_launchd_signals_web_absent",
            "retired_launchd_signals_sync_absent",
            "retired_launchd_signals_web2_absent",
            "retired_launchd_signals_quote_absent",
            "retired_launchd_signals_signal_absent",
            "retired_launchd_signals_workbench_absent",
            "retired_launchd_signals_board_absent",
            "pack_dashboard",
            "cache_health",
            "cluster_latest",
            "backtest_health",
        ]
    )
    critical_ok = critical_ok and dashboard.get("status") == "healthy"

    summary = {
        "ok": bool(critical_ok),
        "web_base_url": base_url,
        "dashboard_status": dashboard.get("status", ""),
        "active_pool_count": coverage.get("count", 0),
        "bars_coverage_pct": coverage.get("bars_coverage_pct", 0),
        "quote_coverage_pct": coverage.get("quote_coverage_pct", 0),
        "checks": checks,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if critical_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
