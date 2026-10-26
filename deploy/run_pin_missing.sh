#!/usr/bin/env bash
# run_pin_missing.sh - weekly: give a map pin to every location_meta row that has none (pin_missing.py), invoked by cron on the Droplet.
#
# A scraper creates a location_meta row with a state and a type but no coordinates; the map (get_map_data) needs a pin, so a new location showed in the Bids tab and not on the map until somebody ran
# geocode_locations.py by hand (the 2026-10-25 one-time pass pinned 586 of the 636 that were missing). This does the same job on a schedule: ADM's own addresses, the coordinates the AgriCharts feeds
# carry, then state-constrained OpenStreetMap lookups checked against the state's border. It mails the operator only when it placed something or something needs a look; a run with nothing to do is
# silent. Switch it off: touch /opt/basis-tracker/state/pin_missing.off. flock prevents overlap; output is logged per-run and pruned at 30 days. Mirrors deploy/run_hfgrain.sh.
#
# Cron: 45 17 * * 5  (Fridays 5:45 PM Central, after the 15:45 daily import and the 17:10 H&F catch-up, through /opt/alerting/cron-alert)
set -uo pipefail

APP_DIR="/opt/basis-tracker"                 # <-- clone path (edit me)
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/pin_missing_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard (its own lock).
exec 9>"$LOG_DIR/.pin_missing.lock"
if ! flock -n 9; then
    echo "$(date -Is) another pin_missing run is in progress - skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== pin_missing start $(date -Is) ==="
    "$VENV/bin/python" pin_missing.py "$@"
    rc=$?    # capture immediately - must precede any other command (e.g. date)
    echo "=== pin_missing finished $(date -Is) rc=$rc ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs.
find "$LOG_DIR" -name 'pin_missing_*.log' -mtime +30 -delete 2>/dev/null || true

exit "$rc"
