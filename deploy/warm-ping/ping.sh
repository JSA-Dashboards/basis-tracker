#!/usr/bin/env bash
# Streamlit Community Cloud keep-alive AND health check. Runs from cron every 10 minutes:
#     */10 * * * * /opt/warm-ping/ping.sh        (a plain crontab line, NOT cron-alert: see below)
# Source of truth is deploy/warm-ping/ in the basis-tracker repo; the box runs the copy in /opt/warm-ping/.
#
# 1) WARM: a cookie-jar curl of each app's public ?view=1 URL follows the 303 through to a real 200
#    (a plain GET of the base URL only gets the 303), so Community Cloud does not put the app to sleep.
#    The result is only the WRAPPER page: it answers 200 even while the app behind it is dead.
#    (2026-11-09: the admin app was down for hours while this log said 200 every 10 minutes.)
# 2) HEALTH: GET https://<app>/~/+/_stcore/health must answer 200 with the body "ok". A dead app answers
#    503 "application is down" (or 502). A check that fails is repeated twice more, WARM_SLEEP seconds
#    apart, so a restart blip is not counted. A run that still fails counts as one failure; after
#    WARM_FAILS_TO_ALERT consecutive failing runs (default 2, i.e. 10-20 minutes) health_alert.py e-mails
#    kpostin@jpsi.com once, then reminds every 6 hours, and says when the app is back.
#    Nothing is alerted when the droplet itself has no network (the control request below fails).
#
# Plain crontab line BY DESIGN, not wrapped in /opt/alerting/cron-alert: a curl blip must not page. The
# script exits 0 whatever happens; its only outputs are the log lines and the alert e-mails.
#
# Settings come from the environment so the tests can drive it; the defaults are the production ones.
#   WARM_URLS   space-separated URLs to keep warm      HEALTH_HOSTS  space-separated app hosts to check
#   WARM_LOG    log file (trimmed to the last 2000 lines)       WARM_STATE  per-app state directory
#   WARM_CURL   curl binary      WARM_ALERT  the alert command   WARM_SLEEP  seconds between health retries
#   WARM_NOW    epoch seconds (tests fake the clock)      WARM_CONTROL  a URL that must answer if the network is up
#   WARM_FAILS_TO_ALERT  consecutive failing runs before the first alert      WARM_REMIND_SECS  seconds between reminders
set -u

LOG=${WARM_LOG:-/opt/warm-ping/ping.log}
STATE=${WARM_STATE:-/opt/warm-ping/state}
CURL=${WARM_CURL:-curl}
ALERT=${WARM_ALERT:-/opt/warm-ping/health_alert.py}
SLEEP=${WARM_SLEEP:-40}
NOW=${WARM_NOW:-$(date +%s)}
CONTROL=${WARM_CONTROL:-https://graph.microsoft.com/}
FAILS_TO_ALERT=${WARM_FAILS_TO_ALERT:-2}
REMIND_SECS=${WARM_REMIND_SECS:-21600}
# jsa-us-rail-dashboard has answered 404 (the app no longer exists) for days: it is warmed as before but never health-checked.
URLS=${WARM_URLS:-"https://jsa-basis-tracker.streamlit.app/?view=1 https://river-fob.streamlit.app/?view=1 https://jsa-us-rail-dashboard.streamlit.app/?view=1"}
HOSTS=${HEALTH_HOSTS:-"jsa-basis-tracker.streamlit.app river-fob.streamlit.app"}

mkdir -p "$STATE" 2>/dev/null
if command -v flock >/dev/null 2>&1; then          # a run that overruns 10 minutes must not overlap the next one
  exec 9>"$STATE/.lock"
  flock -n 9 || exit 0
fi

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# -- 1) warm -----------------------------------------------------------------------------------------------
JAR=$(mktemp)
for u in $URLS; do
  res=$("$CURL" -s -L -c "$JAR" -b "$JAR" -o /dev/null -w "%{http_code} %{time_total}s" \
        -A "JSA-warm-ping" --max-time 45 "$u" 2>/dev/null || echo "ERR")
  log "$res $u"
done
rm -f "$JAR"

# -- 2) health ---------------------------------------------------------------------------------------------
health() {                                           # prints "ok" or "FAIL <http code> <body>"
  local out code body
  out=$("$CURL" -s -m 20 -A "JSA-warm-ping" -w $'\n%{http_code}' "https://$1/~/+/_stcore/health" 2>/dev/null) || out=$'\n000'
  code=${out##*$'\n'}
  body=${out%$'\n'*}
  body=$(printf '%s' "$body" | head -c 80 | tr -d '\r\n')
  if [ "$code" = "200" ] && [ "$body" = "ok" ]; then echo ok; else echo "FAIL $code ${body:-<empty>}"; fi
}

check_app() {                                        # three tries, SLEEP seconds apart
  local r i
  for i in 1 2 3; do
    r=$(health "$1")
    if [ "$r" = ok ]; then echo ok; return; fi
    [ "$i" -lt 3 ] && sleep "$SLEEP"
  done
  echo "$r"
}

net=$("$CURL" -s -o /dev/null -w '%{http_code}' --max-time 15 "$CONTROL" 2>/dev/null || echo 000)
if [ "$net" = "000" ]; then
  log "HEALTH skipped: no network (control $CONTROL did not answer)"
else
  for host in $HOSTS; do
    f="$STATE/${host//./_}"
    res=$(check_app "$host")
    if [ "$res" = ok ]; then
      log "HEALTH ok https://$host"
      if [ -f "$f.alerted" ]; then                   # an alert went out: say it is back
        since=$(cat "$f.since" 2>/dev/null || echo "$NOW")
        "$ALERT" --state recovered --app "$host" --since "$since" --now "$NOW" --detail "back to HTTP 200 ok" >/dev/null 2>&1 \
          || log "HEALTH alert (recovered) for $host could not be sent"
      fi
      rm -f "$f.fails" "$f.since" "$f.alerted" "$f.detail"
      continue
    fi
    fails=$(( $(cat "$f.fails" 2>/dev/null || echo 0) + 1 ))
    [ -f "$f.since" ] || echo "$NOW" > "$f.since"
    echo "$fails" > "$f.fails"
    echo "$res" > "$f.detail"
    log "HEALTH FAIL($fails) https://$host $res"
    since=$(cat "$f.since")
    if [ ! -f "$f.alerted" ]; then
      if [ "$fails" -ge "$FAILS_TO_ALERT" ]; then
        if "$ALERT" --state down --app "$host" --since "$since" --now "$NOW" --fails "$fails" --detail "$res" >/dev/null 2>&1; then
          echo "$NOW" > "$f.alerted"
        else
          log "HEALTH alert (down) for $host could not be sent: it will be retried on the next run"
        fi
      fi
    elif [ $(( NOW - $(cat "$f.alerted") )) -ge "$REMIND_SECS" ]; then
      if "$ALERT" --state still-down --app "$host" --since "$since" --now "$NOW" --fails "$fails" --detail "$res" >/dev/null 2>&1; then
        echo "$NOW" > "$f.alerted"
      else
        log "HEALTH alert (still down) for $host could not be sent: it will be retried on the next run"
      fi
    fi
  done
fi

tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" || true
exit 0
