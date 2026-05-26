#!/usr/bin/env bash
# Snapshot the warehouse, retain the 14 newest archives, delete the
# rest. POSIX wrapper for cron / systemd / launchd.
#
# Required env vars (set by your scheduler unit):
#   FLOWMETRICS_HOME   install root (holds contracts/ and data/)
#   FLOWMETRICS_VENV   venv to run flow from

set -euo pipefail

: "${FLOWMETRICS_HOME:?set FLOWMETRICS_HOME}"
: "${FLOWMETRICS_VENV:?set FLOWMETRICS_VENV}"

cd "$FLOWMETRICS_HOME"

"$FLOWMETRICS_VENV/bin/flow" backup --data-dir data

# Keep the 14 most recent; nothing older.
ls -1t data/_backups/flowmetrics-*.tar.gz 2>/dev/null \
  | tail -n +15 \
  | xargs -r rm --
