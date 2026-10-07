#!/usr/bin/env bash
# run_rail_email_watch.sh — catch-up for rail update emails, invoked by cron on the Droplet.
#
# Runs `rail_email_watch.py`: emails any rundown that was SAVED but never EMAILED (the Cloud app's send failed, a post_rail.py
# send failed ...) — see the docstring there for the safety rules (baseline, grace period, unarmed until the Cloud app has
# logged, state/rail_email_watch.off kill switch). Prints one line per run, appended to ONE rolling log (trimmed at 1 MB):
# a per-run log file would be 100+ files a day.
#
# Cron: */10 6-22 * * *   (every 10 minutes, 06:00-22:50 Central). Wrapped in /opt/alerting/cron-alert, which emails on a
# non-zero exit — the watcher only exits non-zero after the same problem repeats, so an outage does not alert every 10 minutes.
set -uo pipefail

APP_DIR="/opt/basis-tracker"
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"
LOG="$LOG_DIR/rail_email_watch.log"

mkdir -p "$LOG_DIR" "$APP_DIR/state"
cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard (its own lock).
exec 9>"$LOG_DIR/.rail_email_watch.lock"
if ! flock -n 9; then
    echo "$(date -Is) another watcher run is in progress — skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    printf '%s ' "$(date -Is)"
    "$VENV/bin/python" rail_email_watch.py "$@"
    rc=$?    # capture immediately — must precede any other command
} >>"$LOG" 2>&1

# One rolling log, kept small.
if [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -n 3000 "$LOG" >"$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" || true
fi

exit "$rc"
