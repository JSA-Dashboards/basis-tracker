#!/usr/bin/env python3
"""Send a droplet cron-failure alert by Microsoft Graph. Standard library only.

No venv, no msal, no requests. The alerter must not share failure modes with
the jobs it watches -- if a job's virtualenv breaks, the alert about it still
has to go out. Everything here is stdlib and uses the system python3.

Credentials are READ, never stored here: GRAPH_* comes from an existing app
.env (default /opt/basis-tracker/.env) so there is exactly one copy of the
client secret on the box to rotate. See the memory note reference_graph_email --
the secret expires 2028-09-16.

Usage:
    notify.py --check                      # auth only, prove Mail.Send, send nothing
    notify.py --name JOB --rc N [...]      # send a failure alert
    ALERT_DRY_RUN=1 notify.py --name ...   # print the email instead of sending
"""
import argparse
import base64
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path

CONF_PATH = Path(os.environ.get("ALERT_CONF", "/opt/alerting/alert.conf"))
UNDELIVERED = Path("/opt/alerting/undelivered.log")
GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = 30

EXIT_MEANING = {
    124: "timed out (the wrapper's ALERT_TIMEOUT elapsed and the job was killed)",
    125: "the timeout command itself failed",
    126: "found but not executable",
    127: "command not found",
    137: "killed with SIGKILL (did not stop after SIGTERM)",
}


def parse_env(path: Path) -> dict:
    """Minimal KEY=VALUE reader. Tolerates quotes, 'export ', comments, CRLF."""
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def collect_secrets() -> list:
    """Every secret-ish value from every app .env on the box.

    Job logs get pasted into an email that lands in mailboxes and Exchange
    archives. A traceback can easily carry a DSN or a password, so the literal
    values are stripped before sending rather than trusting that they will not
    appear.
    """
    vals = []
    pat = re.compile(r"(PASSWORD|SECRET|TOKEN|_KEY|APIKEY|API_KEY)", re.I)
    for env in sorted(Path("/opt").glob("*/.env")):
        for k, v in parse_env(env).items():
            if pat.search(k) and len(v) >= 8:
                vals.append(v)
        url = parse_env(env)
        for k, v in url.items():
            m = re.search(r"://[^:/@\s]+:([^@/\s]+)@", v or "")
            if m and len(m.group(1)) >= 6:
                vals.append(m.group(1))
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str, secrets: list) -> str:
    for s in secrets:
        if s and s in text:
            text = text.replace(s, "[REDACTED]")
    # Catch-all for inline credentials in URLs we did not have a value for.
    return re.sub(r"(://[^:/@\s]+:)[^@/\s]+(@)", r"\1[REDACTED]\2", text)


def get_token(cfg: dict) -> str:
    tenant, client, secret = cfg["GRAPH_TENANT_ID"], cfg["GRAPH_CLIENT_ID"], cfg["GRAPH_CLIENT_SECRET"]
    body = urllib.parse.urlencode({
        "client_id": client,
        "client_secret": secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }).encode()
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Graph auth failed: HTTP {e.code} {e.read()[:300].decode('utf-8', 'replace')}")
    access = tok.get("access_token")
    if not access:
        raise RuntimeError(f"Graph auth returned no token: {tok.get('error_description') or tok}")
    return access


def token_roles(access: str) -> list:
    payload = access.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("roles", [])


def send(cfg: dict, subject: str, html: str) -> None:
    access = get_token(cfg)
    recips = [a.strip() for a in re.split(r"[,;]", cfg["ALERT_TO"]) if a.strip()]
    msg = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in recips],
        },
        "saveToSentItems": False,
    }
    url = f"{GRAPH}/users/{urllib.parse.quote(cfg['GRAPH_SENDER'])}/sendMail"
    req = urllib.request.Request(url, data=json.dumps(msg).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {access}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        if r.status not in (200, 202):
            raise RuntimeError(f"sendMail returned HTTP {r.status}")


def build(args, secrets) -> tuple:
    host = socket.gethostname()
    meaning = EXIT_MEANING.get(args.rc, "the job's own exit code")
    log_text = ""
    if args.log and Path(args.log).is_file():
        lines = Path(args.log).read_text(encoding="utf-8", errors="replace").splitlines()
        log_text = "\n".join(lines[-args.log_lines:])
    out_text = ""
    if args.stdout and Path(args.stdout).is_file():
        out_text = Path(args.stdout).read_text(encoding="utf-8", errors="replace").strip()

    subject = f"[{host}] FAILED: {args.name} (exit {args.rc})"
    rows = [
        ("Job", args.name), ("Host", host), ("Exit code", f"{args.rc} — {meaning}"),
        ("Started", args.start), ("Finished", args.end), ("Command", args.cmd),
        ("Log file", args.log or "none written — the job failed before it began logging"),
    ]
    html = ["<div style='font-family:system-ui,sans-serif;font-size:14px'>",
            f"<p><b>{escape(args.name)}</b> failed on <b>{escape(host)}</b>.</p>",
            "<table cellpadding='4' style='border-collapse:collapse;font-size:13px'>"]
    for k, v in rows:
        # The command is a crontab line today, but redact it too rather than
        # rely on that staying true.
        html.append(f"<tr><td style='color:#64748b'>{escape(k)}</td>"
                    f"<td><code>{escape(redact(str(v), secrets))}</code></td></tr>")
    html.append("</table>")
    for title, body in (("Last lines of the job log", log_text),
                        ("Output captured outside the job log", out_text)):
        if body.strip():
            html.append(f"<p style='color:#64748b;margin:14px 0 4px'>{title}</p>"
                        "<pre style='background:#f6f8fa;padding:10px;border-radius:6px;"
                        "font-size:12px;white-space:pre-wrap;overflow-x:auto'>"
                        f"{escape(redact(body, secrets))}</pre>")
    html.append("<p style='color:#94a3b8;font-size:12px'>Sent by /opt/alerting/cron-alert. "
                "Recipients are set in /opt/alerting/alert.conf.</p></div>")
    return subject, "".join(html)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="auth only; send nothing")
    p.add_argument("--name"); p.add_argument("--rc", type=int, default=1)
    p.add_argument("--start", default=""); p.add_argument("--end", default="")
    p.add_argument("--cmd", default=""); p.add_argument("--log", default="")
    p.add_argument("--stdout", default=""); p.add_argument("--log-lines", type=int, default=40)
    args = p.parse_args()

    cfg = parse_env(CONF_PATH)
    graph_env = Path(cfg.get("GRAPH_ENV_FILE", "/opt/basis-tracker/.env"))
    for k, v in parse_env(graph_env).items():
        cfg.setdefault(k, v)

    missing = [k for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
                           "GRAPH_SENDER", "ALERT_TO") if not cfg.get(k)]
    if missing:
        print(f"alerting misconfigured, missing: {', '.join(missing)} "
              f"(conf={CONF_PATH}, graph env={graph_env})", file=sys.stderr)
        return 2

    if args.check:
        roles = token_roles(get_token(cfg))
        ok = "Mail.Send" in roles
        print(f"token acquired; roles={roles}; Mail.Send={'yes' if ok else 'NO'}")
        print(f"sender={cfg['GRAPH_SENDER']} -> {cfg['ALERT_TO']}")
        return 0 if ok else 1

    if not args.name:
        print("--name is required", file=sys.stderr)
        return 2

    subject, html = build(args, collect_secrets())
    if os.environ.get("ALERT_DRY_RUN"):
        print(f"DRY RUN, nothing sent\nSubject: {subject}\n")
        flat = html.replace("</td>", " | ").replace("</tr>", "\n")
        flat = flat.replace("</p>", "\n").replace("</pre>", "\n")
        print(re.sub(r"<[^>]+>", "", flat))
        return 0
    try:
        send(cfg, subject, html)
        print(f"alert sent to {cfg['ALERT_TO']}")
        return 0
    except Exception as e:
        # Never let a mail failure hide the job failure: leave a local record.
        try:
            UNDELIVERED.parent.mkdir(parents=True, exist_ok=True)
            with UNDELIVERED.open("a", encoding="utf-8") as fh:
                fh.write(f"{args.start}\t{args.name}\trc={args.rc}\tsend failed: {e}\n")
        except Exception:
            pass
        print(f"ALERT SEND FAILED: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
