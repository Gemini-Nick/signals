#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIN_VERSION="3.11"

pick_python() {
  local candidates=()
  local candidate=""
  local resolved=""

  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("${PYTHON_BIN}")
  fi

  candidates+=(
    "${ROOT_DIR}/.venv/bin/python"
    python3.11
    python3
    python
  )

  for candidate in "${candidates[@]}"; do
    if [[ -x "${candidate}" ]]; then
      resolved="${candidate}"
    elif command -v "${candidate}" >/dev/null 2>&1; then
      resolved="$(command -v "${candidate}")"
    else
      continue
    fi

    if "${resolved}" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      printf '%s\n' "${resolved}"
      return 0
    fi
  done

  return 1
}

PYTHON_EXE="$(pick_python)" || {
  echo "Python ${MIN_VERSION}+ is required. Set PYTHON_BIN or bootstrap a local .venv first." >&2
  exit 1
}

exec "${PYTHON_EXE}" "$@"
