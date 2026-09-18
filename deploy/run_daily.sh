#!/usr/bin/env bash
# run_daily.sh — daily basis-tracker scrape, invoked by cron on the Droplet.
#
# Runs the full auto_import (all scrapers → Snowflake) and sends the daily Changes
# email via Graph. auto_import calls load_dotenv(), so we cd into APP_DIR first and
# it reads .env from there. flock prevents overlapping runs (auto_import also has
# its own internal lock as a backstop). Output is logged per-run and pruned at 30d.
#
# Cron installs this; adjust APP_DIR to wherever the repo is cloned.
set -uo pipefail

APP_DIR="/opt/basis-tracker"                 # <-- clone path (edit me)
VENV="$APP_DIR/.venv"                         # virtualenv created during setup
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/auto_import_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard: skip silently if a run is already going.
exec 9>"$LOG_DIR/.auto_import.lock"
if ! flock -n 9; then
    echo "$(date -Is) another auto_import run is in progress — skipping" >>"$LOG"
    exit 0
fi

{
    echo "=== auto_import start $(date -Is) ==="
    "$VENV/bin/python" auto_import.py
    echo "=== auto_import finished $(date -Is) rc=$? ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs.
find "$LOG_DIR" -name 'auto_import_*.log' -mtime +30 -delete 2>/dev/null || true
