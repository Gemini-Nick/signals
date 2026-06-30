#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.zhangqilong.ai.signals.hot-rank-clues"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/longclaw"
LOG_FILE="${LOG_DIR}/signals.hot-rank-clues.launchd.log"
ERR_FILE="${LOG_DIR}/signals.hot-rank-clues.launchd.err.log"
UID_VALUE="$(id -u)"

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}" "${ROOT_DIR}/data/imports/wind_hot_rank"

cat > "${PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${ROOT_DIR}/scripts/run_hot_rank_clues_once.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>SIGNALS_SYNC_TIMEZONE</key>
    <string>Asia/Shanghai</string>
    <key>HOT_RANK_WIND_EXPORT_DIR</key>
    <string>${ROOT_DIR}/data/imports/wind_hot_rank</string>
  </dict>
  <key>StartInterval</key>
  <integer>900</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>
  <key>StandardErrorPath</key>
  <string>${ERR_FILE}</string>
</dict>
</plist>
PLIST

plutil -lint "${PLIST}"
launchctl bootout "gui/${UID_VALUE}" "${PLIST}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${UID_VALUE}" "${PLIST}"
launchctl kickstart -k "gui/${UID_VALUE}/${LABEL}" || true

echo "installed ${LABEL}"
echo "plist=${PLIST}"
echo "stdout=${LOG_FILE}"
echo "stderr=${ERR_FILE}"
launchctl print "gui/${UID_VALUE}/${LABEL}" | sed -n '1,80p'
