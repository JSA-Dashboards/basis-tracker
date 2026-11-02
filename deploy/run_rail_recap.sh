#!/usr/bin/env bash
# run_rail_recap.sh — monthly JSA Rail Basis recap email, invoked by cron on the Droplet.
#
# Runs `rail_report.py --recap`: builds the full rail-corridor recap from Snowflake and
# emails it. On Linux there's no Outlook, so changes_report.send_email falls through to
# Microsoft Graph (same as the daily Changes email). flock prevents overlap; output is
# logged per-run and pruned at 30 days. Mirrors deploy/run_daily.sh.
#
# Cron: 0 11 1-3 * 1-5  (Mon–Fri on days 1–3 of each month, 11:00 AM Central)
# The first-business-day guard below exits early on day 2 or 3 unless the 1st was a weekend.
set -uo pipefail

# ── First-business-day-of-month guard ────────────────────────────────────────
# Cron fires Mon–Fri on days 1–3. We only want to send on the FIRST business day.
#   day 1 Mon–Fri  → send (it IS day 1)
#   day 2 Monday   → send (day 1 was Sunday)
#   day 3 Monday   → send (day 1 was Saturday)
#   everything else → skip
_dom=$(date +%-d)   # day-of-month, no leading zero
_dow=$(date +%u)    # 1=Mon … 7=Sun
if   [ "$_dom" -eq 1 ]; then
    : # always send on the 1st (it's a weekday per cron schedule)
elif [ "$_dom" -eq 2 ] && [ "$_dow" -eq 1 ]; then
    : # 2nd + Monday → 1st was Sunday
elif [ "$_dom" -eq 3 ] && [ "$_dow" -eq 1 ]; then
    : # 3rd + Monday → 1st was Saturday
else
    echo "$(date -Is) not first business day of month (dom=$_dom dow=$_dow) — skipping" \
        >> "$(dirname "$0")/../logs/rail_recap_skip.log" 2>/dev/null || true
    exit 0
fi
# ─────────────────────────────────────────────────────────────────────────────

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
