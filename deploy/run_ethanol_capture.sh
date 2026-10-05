#!/usr/bin/env bash
# run_ethanol_capture.sh — daily capture of the CME Chicago / NY Ethanol (Platts) futures,
# invoked by cron on the Droplet.
#
# Runs `ethanol_capture.py`: snapshots CU (Chicago) and AEZ (NY) from Massive into the
# ETHANOL_FUTURES table so the Nightly Recap can pre-fill "Chi Platts Eth". Massive keeps no
# history for these thinly traded contracts, so a missed day is gone for good — hence the
# failure alert (the crontab wraps this in /opt/alerting/cron-alert). On a weekend it exits 0
# having done nothing. flock prevents overlap; output is logged per-run and pruned at 30 days.
# Mirrors deploy/run_rail_recap.sh.
#
# Cron: 40 16 * * 1-5  (weekdays 4:40 PM Central — after the NYMEX energy settlement and
# the 4:30 settlement-archive job).
set -uo pipefail

APP_DIR="/opt/basis-tracker"                 # <-- clone path (edit me)
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/ethanol_capture_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard (its own lock).
exec 9>"$LOG_DIR/.ethanol_capture.lock"
if ! flock -n 9; then
    echo "$(date -Is) another ethanol capture is in progress — skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== ethanol capture start $(date -Is) ==="
    "$VENV/bin/python" ethanol_capture.py
    rc=$?    # capture immediately — must precede any other command (e.g. date)
    echo "=== ethanol capture finished $(date -Is) rc=$rc ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs.
find "$LOG_DIR" -name 'ethanol_capture_*.log' -mtime +30 -delete 2>/dev/null || true

exit "$rc"
