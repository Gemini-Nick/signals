#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only observer for the local Signals launchd runtime and data lanes."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_LABEL = "com.zhangqilong.ai.signals.runtime"
DEFAULT_LOG_DIR = Path("/tmp/longclaw-guardian")
DEFAULT_PID_DIR = Path.home() / ".longclaw/runtime-v2/pids"
DEFAULT_PORT = "8011"
DEFAULT_SYMBOLS = ["688802", "300575", "sh000001", "sh000300", "sz399006"]
LANES = ["quote_lane", "signal_lane", "workbench_lane", "board_lane"]
LANE_SHORT = {
    "quote_lane": "quote",
    "signal_lane": "signal",
    "workbench_lane": "workbench",
    "board_lane": "board",
}
MODULES = [
    "quote_snapshots",
    "index_minute",
    "stock_minute",
    "minute_readiness_probe",
    "market_pools",
    "strategy_snapshot",
    "board_heat_minute",
    "concept_heat_minute",
    "chain_heat_snapshots",
    "technical_signal_scan",
    "knowledge_market_views",
    "board_ranking",
    "stock_daily",
    "index_daily",
    "weekly_rollup",
    "terminal_realtime_pool",
    "cache_preheat",
    "signal_pool",
    "board_cons",
]
FREQS = ["5分钟", "15分钟", "30分钟", "日线", "周线"]
TZ_BEIJING = ZoneInfo("Asia/Shanghai")


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
    except Exception:
        return ""


def _read_pid(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _pid_status(name: str, pid_file: Path) -> dict[str, Any]:
    pid = _read_pid(pid_file)
    status = {
        "name": name,
        "pid_file": str(pid_file),
        "pid": pid,
        "state": "missing",
        "command": "",
        "rss_kb": "",
        "cpu_pct": "",
    }
    if not pid:
        return status
    command = _run(["ps", "-p", pid, "-o", "command="])
    if not command:
        status["state"] = "stale"
        return status
    metrics = _run(["ps", "-p", pid, "-o", "rss=,%cpu="]).split()
    status.update({
        "state": "running",
        "command": command,
        "rss_kb": metrics[0] if metrics else "",
        "cpu_pct": metrics[1] if len(metrics) > 1 else "",
    })
    return status


def _launchd_status(label: str) -> dict[str, Any]:
    raw = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    status: dict[str, Any] = {"label": label, "raw": raw}
    for line in raw.splitlines():
        item = line.strip()
        for key in ("state", "pid", "runs", "last exit code", "program", "path"):
            prefix = f"{key} = "
            if item.startswith(prefix):
                status[key.replace(" ", "_")] = item[len(prefix):]
    return status


def _port_listener(port: str) -> str:
    return _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])


def _tail(path: Path, lines: int = 20) -> list[str]:
    if not path.exists():
        return []
    output = _run(["tail", "-n", str(lines), str(path)])
    return output.splitlines()[-lines:] if output else []


def _db():
    from signals.sync.db import get_db

    return get_db()


def _runtime_mode() -> dict[str, Any]:
    now = datetime.now(TZ_BEIJING)
    weekday = now.weekday() < 5
    current = now.time()
    intraday = weekday and time(9, 15) <= current <= time(15, 5)
    postmarket = weekday and time(15, 5) < current <= time(23, 30)
    mode = "intraday_low_latency" if intraday else "postmarket_backfill" if postmarket else "off_hours"
    return {
        "mode": mode,
        "beijing_time": now.isoformat(timespec="seconds"),
        "intraday": intraday,
        "postmarket": postmarket,
    }


def _module_meta(db) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for module in MODULES:
        docs = list(db["sync_log"].find(
            {"$or": [{"_id": f"{module}:_meta"}, {"_id": {"$regex": f"^{module}:.*:_meta$"}}]},
            {"_id": 1, "module": 1, "market": 1, "lane": 1, "status": 1, "last_run": 1, "next_due_at": 1,
             "elapsed_seconds": 1, "max_runtime_seconds": 1, "error_msg": 1, "degraded_reason": 1, "result": 1,
             "remaining": 1, "next_cursor": 1, "skipped_count": 1, "selected_symbols": 1, "priority_symbols": 1},
        ).sort("last_run", -1).limit(5))
        for doc in docs:
            doc.pop("_id", None)
            rows.append(doc)
    return rows


def _freshness(db) -> list[dict[str, Any]]:
    return list(db["data_freshness"].find(
        {},
        {"_id": 0, "domain": 1, "market": 1, "mode": 1, "lane": 1, "collection": 1, "freshness": 1,
         "latest_dt": 1, "updated_at": 1, "stale_reason": 1, "count": 1},
    ).sort("updated_at", -1).limit(80))


def _provider_health(db) -> list[dict[str, Any]]:
    if "provider_health" not in db.list_collection_names():
        return []
    return list(db["provider_health"].find(
        {},
        {"_id": 0, "provider": 1, "endpoint": 1, "domain": 1, "status": 1, "ok": 1, "updated_at": 1, "error": 1, "error_msg": 1},
    ).sort("updated_at", -1).limit(40))


def _readiness(db) -> dict[str, Any]:
    rows = list(db["minute_readiness"].find(
        {},
        {"_id": 0, "domain": 1, "symbol": 1, "freq": 1, "status": 1, "latest_dt": 1, "root_cause_class": 1},
    ).sort("checked_at", -1).limit(120))
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        domain = row.get("domain") or "unknown"
        item = summary.setdefault(domain, {"ready": 0, "not_ready": 0})
        if row.get("status") == "ready":
            item["ready"] += 1
        else:
            item["not_ready"] += 1
    return {"summary": summary, "rows": rows}


def _board_heat_probe(db) -> dict[str, Any]:
    rows = []
    for kind in ("industry", "concept"):
        latest = db["board_heat_ticks"].find_one(
            {"kind": kind},
            {"trade_minute": 1, "source": 1},
            sort=[("trade_minute", -1)],
        ) or {}
        rows.append({
            "kind": kind,
            "count": db["board_heat_ticks"].count_documents({"kind": kind}),
            "latest_dt": latest.get("trade_minute"),
            "source": latest.get("source", ""),
        })
    latest = db["chain_heat_snapshots"].find_one(
        {"market": "A"},
        {"trade_minute": 1, "source": 1},
        sort=[("trade_minute", -1)],
    ) or {}
    rows.append({
        "kind": "chain_heat",
        "count": db["chain_heat_snapshots"].count_documents({"market": "A"}),
        "latest_dt": latest.get("trade_minute"),
        "source": latest.get("source", ""),
    })
    return {"rows": rows}


def _postmarket_state(db) -> dict[str, Any]:
    run = db["sync_runs"].find_one(
        {"run_id": {"$regex": "^postmarket:"}},
        {"_id": 0},
        sort=[("updated_at", -1), ("started_at", -1)],
    ) or {}
    if not run:
        return {"run": None, "tasks": [], "summary": {}}
    run_id = run.get("run_id")
    tasks = list(db["sync_tasks"].find(
        {"run_id": run_id},
        {"_id": 0, "module": 1, "phase": 1, "shard_key": 1, "status": 1, "attempts": 1,
         "heartbeat_at": 1, "updated_at": 1, "finished_at": 1, "error_msg": 1, "cursor": 1},
    ).sort([("phase", 1), ("order", 1)]))
    summary: dict[str, int] = {}
    for task in tasks:
        status = task.get("status") or "unknown"
        summary[status] = summary.get(status, 0) + 1
    return {"run": run, "tasks": tasks, "summary": summary}


def _normalize_stock_symbol(symbol: str) -> list[str]:
    raw = str(symbol or "").strip()
    if not raw:
        return []
    candidates = [raw]
    lower = raw.lower()
    upper = raw.upper()
    for value in (lower, upper):
        if value not in candidates:
            candidates.append(value)
    if raw.isdigit() and len(raw) == 6:
        market = "SH" if raw.startswith(("5", "6", "9")) else "SZ" if raw.startswith(("0", "3")) else "BJ"
        for value in (f"{market}.{raw}", raw):
            if value not in candidates:
                candidates.append(value)
    if lower.startswith(("sh", "sz")) and len(lower) == 8:
        code = lower[2:]
        market = lower[:2].upper()
        for value in (lower, f"{market}.{code}", f"{code}.{market}"):
            if value not in candidates:
                candidates.append(value)
    return candidates


def _symbol_probe(db, symbols: list[str]) -> list[dict[str, Any]]:
    active = db["market_pools"].find_one({"pool": "active"}, {"symbols": 1}, sort=[("dt", -1), ("updated_at", -1)]) or {}
    active_symbols = set(active.get("symbols") or [])
    terminal_pool = db["terminal_stock_pool"].find_one(
        {"pool": "terminal_stock_pool", "market": "A"},
        {"stocks.symbol": 1, "stocks.raw_code": 1},
        sort=[("updated_at", -1)],
    ) or {}
    terminal_symbols = set()
    for item in terminal_pool.get("stocks") or []:
        if isinstance(item, dict):
            for value in (item.get("symbol"), item.get("raw_code")):
                if value:
                    terminal_symbols.add(str(value))
    snapshot = db["strategy_snapshots"].find_one(
        {"snapshot": {"$exists": True}},
        {"snapshot.candidates.symbol": 1, "snapshot.decision_queue.symbol": 1},
        sort=[("updated_at", -1), ("as_of", -1)],
    ) or {}
    strategy_symbols = set()
    for key in ("candidates", "decision_queue"):
        for item in ((snapshot.get("snapshot") or {}).get(key) or []):
            if isinstance(item, dict) and item.get("symbol"):
                strategy_symbols.add(str(item["symbol"]))
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        candidates = _normalize_stock_symbol(symbol)
        freq_rows = []
        for collection in ("index_bars", "bars"):
            for candidate in candidates:
                for freq in FREQS:
                    query = {"meta.symbol": candidate, "meta.freq": freq}
                    count = db[collection].count_documents(query)
                    if not count:
                        continue
                    latest = db[collection].find_one(query, {"dt": 1, "meta.source": 1, "close": 1}, sort=[("dt", -1)]) or {}
                    freq_rows.append({
                        "collection": collection,
                        "symbol": candidate,
                        "freq": freq,
                        "count": count,
                        "latest_dt": latest.get("dt"),
                        "source": (latest.get("meta") or {}).get("source", ""),
                        "close": latest.get("close"),
                    })
        rows.append({
            "input": symbol,
            "normalized_candidates": candidates,
            "active_pool_member": bool(set(candidates) & active_symbols),
            "terminal_stock_pool_member": bool(set(candidates) & terminal_symbols),
            "strategy_candidate": bool(set(candidates) & strategy_symbols),
            "freqs": freq_rows,
        })
    return rows


def build_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    log_dir = Path(args.log_dir)
    pid_dir = Path(args.pid_dir)
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runtime_mode": _runtime_mode(),
        "launchd": _launchd_status(args.label),
        "processes": {
            "web": _pid_status("web", pid_dir / "signals-web.pid"),
            "postmarket": _pid_status("postmarket", pid_dir / "signals-postmarket.pid"),
            "lanes": {
                lane: _pid_status(lane, pid_dir / f"signals-lane-{lane}.pid")
                for lane in LANES
            },
        },
        "port_listener": _port_listener(args.port),
        "logs": {
            "runtime": _tail(log_dir / "signals.runtime.log", args.tail_lines),
            "web": _tail(log_dir / "signals.web.launchd.log", args.tail_lines),
            "postmarket": _tail(log_dir / "signals.postmarket.launchd.log", args.tail_lines),
            **{
                lane: _tail(log_dir / f"signals.{LANE_SHORT[lane]}.launchd.log", args.tail_lines)
                for lane in LANES
            },
        },
        "mongo": {"available": False},
    }
    try:
        db = _db()
        snapshot["mongo"] = {
            "available": True,
            "module_meta": _module_meta(db),
            "freshness": _freshness(db),
            "provider_health": _provider_health(db),
            "symbol_probe": _symbol_probe(db, args.symbols),
            "readiness": _readiness(db),
            "board_heat": _board_heat_probe(db),
            "postmarket": _postmarket_state(db),
        }
    except Exception as exc:
        snapshot["mongo"] = {"available": False, "error": f"{exc.__class__.__name__}: {exc}"}
    return snapshot


def print_human(snapshot: dict[str, Any]) -> None:
    print(f"Signals observer @ {snapshot['generated_at']}")
    runtime_mode = snapshot.get("runtime_mode") or {}
    print(f"mode: {runtime_mode.get('mode')} BJ={runtime_mode.get('beijing_time')}")
    launchd = snapshot.get("launchd") or {}
    print(f"launchd {launchd.get('label')}: state={launchd.get('state', '')} pid={launchd.get('pid', '')} runs={launchd.get('runs', '')} last_exit={launchd.get('last_exit_code', '')}")
    web = (snapshot.get("processes") or {}).get("web") or {}
    print(f"web: {web.get('state')} pid={web.get('pid')} rss={web.get('rss_kb')}KB cpu={web.get('cpu_pct')}")
    postmarket = (snapshot.get("processes") or {}).get("postmarket") or {}
    print(f"postmarket: {postmarket.get('state')} pid={postmarket.get('pid')} rss={postmarket.get('rss_kb')}KB cpu={postmarket.get('cpu_pct')}")
    for lane, item in ((snapshot.get("processes") or {}).get("lanes") or {}).items():
        print(f"{lane}: {item.get('state')} pid={item.get('pid')} rss={item.get('rss_kb')}KB cpu={item.get('cpu_pct')}")
    mongo = snapshot.get("mongo") or {}
    if not mongo.get("available"):
        print(f"mongo: unavailable {mongo.get('error', '')}")
        return
    print("\nmodule meta:")
    for item in mongo.get("module_meta", [])[:20]:
        print(f"- {item.get('module')} lane={item.get('lane', '')} market={item.get('market', '')} status={item.get('status')} last={_json_default(item.get('last_run'))} next={_json_default(item.get('next_due_at'))} reason={item.get('degraded_reason') or item.get('error_msg') or ''}")
    post_state = mongo.get("postmarket") or {}
    run = post_state.get("run") or {}
    if run:
        print("\npostmarket:")
        print(f"- run={run.get('run_id')} trade_date={run.get('trade_date')} status={run.get('status')} phase={run.get('phase')} heartbeat={_json_default(run.get('heartbeat_at'))} tasks={post_state.get('summary')}")
        for task in (post_state.get("tasks") or [])[:16]:
            print(f"  - {task.get('phase')}/{task.get('module')} status={task.get('status')} attempts={task.get('attempts')} error={task.get('error_msg') or ''}")
    print("\nsymbol probe:")
    for item in mongo.get("symbol_probe", []):
        freqs = ", ".join(f"{row['collection']}:{row['symbol']}:{row['freq']}@{_json_default(row.get('latest_dt'))}" for row in item.get("freqs", []))
        print(f"- {item['input']} terminal_pool={item.get('terminal_stock_pool_member')} active={item['active_pool_member']} strategy={item['strategy_candidate']} {freqs or 'MISS'}")
    print("\nreadiness:")
    for domain, item in (mongo.get("readiness") or {}).get("summary", {}).items():
        print(f"- {domain}: ready={item.get('ready', 0)} not_ready={item.get('not_ready', 0)}")
    print("\nboard heat:")
    for item in (mongo.get("board_heat") or {}).get("rows", []):
        print(f"- {item.get('kind')}: count={item.get('count')} latest={_json_default(item.get('latest_dt'))} source={item.get('source')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe Signals launchd/lane/data health")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--label", default=os.getenv("SIGNALS_RUNTIME_LABEL", DEFAULT_LABEL))
    parser.add_argument("--log-dir", default=os.getenv("LONGCLAW_LOG_DIR", str(DEFAULT_LOG_DIR)))
    parser.add_argument("--pid-dir", default=os.getenv("LONGCLAW_PID_DIR", str(DEFAULT_PID_DIR)))
    parser.add_argument("--port", default=os.getenv("LONGCLAW_SIGNALS_WEB_PORT", DEFAULT_PORT))
    parser.add_argument("--tail-lines", type=int, default=20)
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    args = parser.parse_args()

    snapshot = build_snapshot(args)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, default=_json_default, indent=2))
    else:
        print_human(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
