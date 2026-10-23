#!/usr/bin/env bash
# run_hfgrain.sh — evening catch-up for Hollander & Feuerhaken's daily bid PDF (hfgrain.com/bids),
# invoked by cron on the Droplet.
#
# The 15:45 daily run (auto_import.py) already tries the sheet, but H&F uploads it around 15:40 CT
# (15:39 on 2026-10-22) and a day it comes later is missed by that run. The site keeps ONLY today's
# PDF (every older /s/HFBids-YYYYMMDD.pdf is a 404), so a sheet that is not captured the day it is
# up can be replaced before we ever see it, and there is no backfill. This runs just
# `auto_import.py --hfgrain-only`: the sheet is filed under its own date, so one the 15:45 run has
# already filed adds nothing. On a weekend or a grain-market holiday auto_import's trading-day guard
# exits 0 having done nothing. flock prevents overlap; output is logged per-run and pruned at 30 days.
# Mirrors deploy/run_ethanol_capture.sh.
#
# Cron: 10 17 * * 1-5  (weekdays 5:10 PM Central, through /opt/alerting/cron-alert)
set -uo pipefail

APP_DIR="/opt/basis-tracker"                 # <-- clone path (edit me)
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/hfgrain_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard (its own lock).
exec 9>"$LOG_DIR/.hfgrain.lock"
if ! flock -n 9; then
    echo "$(date -Is) another H&F capture is in progress — skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== hfgrain start $(date -Is) ==="
    "$VENV/bin/python" auto_import.py --hfgrain-only
    rc=$?    # capture immediately — must precede any other command (e.g. date)
    echo "=== hfgrain finished $(date -Is) rc=$rc ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs.
find "$LOG_DIR" -name 'hfgrain_*.log' -mtime +30 -delete 2>/dev/null || true

exit "$rc"
