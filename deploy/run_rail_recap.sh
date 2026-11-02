#!/usr/bin/env bash
# run_rail_recap.sh — weekly JSA Rail Basis recap email, invoked by cron on the Droplet.
#
# Runs `rail_report.py --recap`: builds the full rail-corridor recap from Snowflake and
# emails it. On Linux there's no Outlook, so changes_report.send_email falls through to
# Microsoft Graph (same as the daily Changes email). flock prevents overlap; output is
# logged per-run and pruned at 30 days. Mirrors deploy/run_daily.sh.
#
# Cron: 0 11 * * 1  (Mon 11:00 AM Central — matches the retired desktop task).
set -uo pipefail

APP_DIR="/opt/basis-tracker"                 # <-- clone path (edit me)
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/rail_recap_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard (its own lock, separate from the daily run's).
exec 9>"$LOG_DIR/.rail_recap.lock"
if ! flock -n 9; then
    echo "$(date -Is) another rail recap run is in progress — skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== rail recap start $(date -Is) ==="
    "$VENV/bin/python" rail_report.py --recap
    rc=$?    # capture immediately — must precede any other command (e.g. date)
    echo "=== rail recap finished $(date -Is) rc=$rc ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs.
find "$LOG_DIR" -name 'rail_recap_*.log' -mtime +30 -delete 2>/dev/null || true

exit "$rc"
