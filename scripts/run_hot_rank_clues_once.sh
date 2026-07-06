#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

export SIGNALS_SYNC_TIMEZONE="${SIGNALS_SYNC_TIMEZONE:-Asia/Shanghai}"
export HOT_RANK_WIND_EXPORT_DIR="${HOT_RANK_WIND_EXPORT_DIR:-${ROOT_DIR}/data/imports/wind_hot_rank}"
mkdir -p "${HOT_RANK_WIND_EXPORT_DIR}"

should_run="$(bash "${ROOT_DIR}/scripts/python.sh" - <<'PY'
from datetime import datetime, time
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("Asia/Shanghai"))
windows = (
    (time(9, 15), time(15, 35)),
    (time(20, 30), time(22, 30)),
)
in_window = any(start <= now.time() <= end for start, end in windows)
print("run" if in_window else f"skip:{now.isoformat(timespec='seconds')}")
PY
)"

if [[ "${HOT_RANK_FORCE:-0}" != "1" && "${should_run}" != "run" ]]; then
  echo "hot_rank_clues skipped (${should_run}); set HOT_RANK_FORCE=1 to override"
  exit 0
fi

echo "hot_rank_clues start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
bash "${ROOT_DIR}/scripts/python.sh" -m signals.sync.engine --once --module hot_rank_clues
bash "${ROOT_DIR}/scripts/python.sh" -m signals.sync.engine --once --module terminal_realtime_pool
bash "${ROOT_DIR}/scripts/python.sh" -m signals.sync.engine --once --module quote_snapshots
bash "${ROOT_DIR}/scripts/python.sh" -m signals.sync.engine --once --module strategy_snapshot
echo "hot_rank_clues done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
