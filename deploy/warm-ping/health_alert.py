#!/usr/bin/env python3
"""E-mail an alert when a Streamlit Community Cloud app stops answering its health check, again as a reminder while it stays
down, and once more when it is back. Called by ping.sh (the keep-alive cron job). Standard library only.

It does not send mail itself: it reuses /opt/alerting/notify.py, the same Graph sender the cron-failure alerts use, so the
recipients (ALERT_TO in /opt/alerting/alert.conf), the Graph credentials (read from /opt/basis-tracker/.env) and the
secret-redaction are exactly the ones already proven on this box. Like notify.py it needs no virtualenv.

Usage (ping.sh passes all of these):
    health_alert.py --state down|still-down|recovered --app HOST [--since EPOCH] [--now EPOCH] [--fails N] [--detail TEXT]
    health_alert.py --state test                    # one real test mail: proves the path
    ALERT_DRY_RUN=1 health_alert.py ...             # print the mail instead of sending it
Environment: ALERT_DIR (where notify.py lives, default /opt/alerting), ALERT_CONF (alert.conf, read by notify.py).

Exit codes: 0 sent (or dry run), 2 alerting is misconfigured, 3 the send failed (recorded in notify's undelivered log).
"""
import argparse
import os
import re
import socket
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

sys.path.insert(0, os.environ.get("ALERT_DIR", "/opt/alerting"))
import notify  # noqa: E402  (the Graph sender behind the cron-failure alerts)

APPS = {   # host -> what it is and where it is rebooted
    "jsa-basis-tracker.streamlit.app": ("Basis Tracker (admin)",
        "share.streamlit.io, account koltenpostin93-blip: <b>basis-tracker &middot; master &middot; app.py</b>"),
    "river-fob.streamlit.app": ("River FOB Portal", "share.streamlit.io, the workspace that owns river-fob"),
}


def when(epoch, tz_name="America/Chicago") -> str:
    """An epoch as 'Mon 2026-11-09 07:21 CST' (US Central), UTC if the zone database is missing."""
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromtimestamp(int(epoch), ZoneInfo(tz_name))
    except Exception:
        d = datetime.fromtimestamp(int(epoch), timezone.utc)
    return d.strftime("%a %Y-%m-%d %H:%M %Z")


def span(seconds: int) -> str:
    m = max(int(seconds) // 60, 0)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def build(args, secrets) -> tuple:
    host = socket.gethostname()
    app = args.app or ""
    name, where = APPS.get(app, (app or "a Streamlit app", "share.streamlit.io, the workspace that owns this app"))
    now = int(args.now) if args.now else int(datetime.now(timezone.utc).timestamp())
    since = int(args.since) if args.since else now
    detail = notify.redact(args.detail or "", secrets)
    url = f"https://{app}/"
    health = f"https://{app}/~/+/_stcore/health"

    if args.state == "test":
        subject = f"[{host}] TEST: the Streamlit health alerts work"
        head = "This is a test of the Streamlit keep-alive alerts."
        lead = ("Nothing is wrong. The keep-alive job on this droplet now checks each app's real health endpoint every 10 minutes "
                "and sends mail like this when an app stops answering.")
        rows = [("Sent from", host), ("Sent at", when(now))]
        todo = ""
    elif args.state == "recovered":
        subject = f"[{host}] Streamlit app back up: {name}"
        head = f"{name} is answering again."
        lead = f"It failed its health check from {when(since)} and answers HTTP 200 \"ok\" again ({span(now - since)} later)."
        rows = [("App", url), ("Health check", health), ("Down since", when(since)), ("Back at", when(now))]
        todo = ""
    else:
        still = args.state == "still-down"
        subject = f"[{host}] Streamlit app {'STILL ' if still else ''}DOWN: {name}" + (f" ({span(now - since)})" if still else "")
        head = f"{name} is not answering." if not still else f"{name} is STILL not answering."
        lead = (f"Its health check has failed on {args.fails or '?'} runs in a row (the job runs every 10 minutes) since {when(since)}. "
                "The keep-alive page load still shows HTTP 200 for it, because that only fetches the wrapper page, so nothing else "
                "has flagged this.")
        rows = [("App", url), ("Health check", health), ("Last answer", detail or "no answer"),
                ("Down since", when(since)), ("Down for", span(now - since)), ("Failed runs", str(args.fails or "?"))]
        todo = ("<p style='margin:14px 0 4px'><b>What to do</b></p><ol style='margin:0;padding-left:20px'>"
                f"<li>Open {where}.</li>"
                "<li>Its &#8942; menu &rarr; <b>Reboot app</b> (a restart takes about a minute; there is no Manage app button on the "
                "error page of a dead app, so use this list).</li>"
                "<li>Afterwards <code>" + escape(health) + "</code> should say <code>ok</code>; the app's own <b>Manage app</b> "
                "log (once it is up) shows why it stopped.</li></ol>")

    out = ["<div style='font-family:system-ui,sans-serif;font-size:14px'>",
           f"<p style='font-size:16px'><b>{escape(head)}</b></p><p>{escape(lead)}</p>",
           "<table cellpadding='4' style='border-collapse:collapse;font-size:13px'>"]
    for k, v in rows:
        out.append(f"<tr><td style='color:#64748b'>{escape(k)}</td><td><code>{escape(str(v))}</code></td></tr>")
    out.append("</table>")
    out.append(todo)
    out.append("<p style='color:#94a3b8;font-size:12px;margin-top:14px'>Sent by /opt/warm-ping/ping.sh through /opt/alerting/notify.py. "
               "Recipients are set in /opt/alerting/alert.conf.</p></div>")
    return subject, "".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True, choices=["down", "still-down", "recovered", "test"])
    p.add_argument("--app", default="")
    p.add_argument("--since", default="")
    p.add_argument("--now", default="")
    p.add_argument("--fails", default="")
    p.add_argument("--detail", default="")
    args = p.parse_args()
    if args.state != "test" and not args.app:
        print("--app is required", file=sys.stderr)
        return 2

    cfg = notify.parse_env(notify.CONF_PATH)
    graph_env = Path(cfg.get("GRAPH_ENV_FILE", "/opt/basis-tracker/.env"))
    for k, v in notify.parse_env(graph_env).items():
        cfg.setdefault(k, v)
    missing = [k for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_SENDER", "ALERT_TO") if not cfg.get(k)]
    dry = bool(os.environ.get("ALERT_DRY_RUN"))
    if missing and not dry:
        print(f"alerting misconfigured, missing: {', '.join(missing)} (conf={notify.CONF_PATH}, graph env={graph_env})", file=sys.stderr)
        return 2

    subject, html = build(args, notify.collect_secrets())
    if dry:
        print(f"DRY RUN, nothing sent\nSubject: {subject}\n")
        flat = html.replace("</td>", " | ").replace("</tr>", "\n").replace("</p>", "\n").replace("</li>", "\n").replace("</ol>", "\n")
        print(re.sub(r"<[^>]+>", "", flat))
        return 0
    try:
        notify.send(cfg, subject, html)
        print(f"alert sent to {cfg['ALERT_TO']}")
        return 0
    except Exception as e:                                           # never lose the news: leave a local record
        try:
            notify.UNDELIVERED.parent.mkdir(parents=True, exist_ok=True)
            with notify.UNDELIVERED.open("a", encoding="utf-8") as fh:
                fh.write(f"{args.now}\thealth:{args.app}\tstate={args.state}\tsend failed: {e}\n")
        except Exception:
            pass
        print(f"ALERT SEND FAILED: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
