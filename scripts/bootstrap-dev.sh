#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv}"
MIN_VERSION="3.11"

pick_python() {
  local candidates=()
  local candidate=""
  local resolved=""

  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("${PYTHON_BIN}")
  fi

  candidates+=(
    python3.11
    python3
    python
  )

  for candidate in "${candidates[@]}"; do
    if command -v "${candidate}" >/dev/null 2>&1; then
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
  echo "Python ${MIN_VERSION}+ is required to bootstrap Signals." >&2
  echo "Install Python 3.11, or set PYTHON_BIN to a 3.11 interpreter, then rerun this script." >&2
  exit 1
}

if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_EXE}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -e ".[dev]"

echo "Signals bootstrap complete."
echo "Use the repo interpreter via: bash scripts/python.sh ..."
