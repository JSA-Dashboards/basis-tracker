"""The daily Changes email waits out a mail service that is down or busy, and a run whose email did not go out says so to cron.

    python tests/test_email_retry.py

Kolten 2026-11-12: "Outlook is down and could cause an issue." That afternoon Microsoft Graph answered 502 / 503 for the tracker's mailbox. The 15:45 run sent the Daily Basis Changes email
ONCE and logged a warning on failure - and every alert about it (cron-alert, the scraper alert, pull_health) goes through the same Graph path, so nobody would have been told.
Now: changes_report.send_with_retry / transient_send_error / send_daily_changes_email(retry_waits=...) wait out 429 / 5xx / timeouts / dropped connections (DAILY_SEND_RETRY_WAITS,
about 35 minutes) and send the same message again; anything else fails at once; auto_import._send_daily_changes_email never raises and the run exits 3 when the email did not go, so
cron-alert and healthchecks (an HTTP ping, not mail) see a failed run. No network, no database, and sleeping is a recorded no-op.
"""
import ast
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import changes_report as cr            # noqa: E402
import database as db                  # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="mailretry_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


SLEEPS = []
mock.patch.object(cr.time, "sleep", lambda s: SLEEPS.append(s)).start()       # never really wait

G503 = "Email send failed — Outlook: No module named 'win32com'; Graph: Graph sendMail failed [503]: {\"error\":{\"code\":\"UnknownError\",\"message\":\"\"}}; SMTP: SMTP not configured"
G403 = "Email send failed — Outlook: No module named 'win32com'; Graph: Graph sendMail failed [403]: {\"error\":{\"code\":\"ErrorAccessDenied\",\"message\":\"Access is denied.\"}}; SMTP: SMTP not configured"

print("which failures are worth waiting out")
T = cr.transient_send_error
check("Graph's own 503 inside the dispatcher's combined message", T(RuntimeError(G503)))
check("502, 504, 500 and 429 (throttled) too", all(T(RuntimeError(G503.replace("[503]", "[%d]" % c))) for c in (500, 502, 504, 429)))
check("a timeout and a dropped connection (what `requests` raises while Exchange is down)",
      T(RuntimeError("HTTPSConnectionPool(host='graph.microsoft.com', port=443): Read timed out. (read timeout=30)"))
      and T(RuntimeError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"))
      and T(RuntimeError("HTTPSConnectionPool(host='graph.microsoft.com', port=443): Max retries exceeded with url: /v1.0/users/x/sendMail")))
check("access denied (403), bad request (400), unauthorised (401) are not: waiting does not fix them",
      not any(T(RuntimeError(G503.replace("[503]", "[%d]" % c))) for c in (400, 401, 403, 404)) and not T(RuntimeError(G403)))
check("a bad client secret or missing settings are not",
      not T(RuntimeError("Email send failed — Outlook: x; Graph: Graph auth failed: AADSTS7000215: Invalid client secret provided; SMTP: y"))
      and not T(RuntimeError("Email send failed — Outlook: x; Graph: NOT CONFIGURED — GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET are missing; SMTP: y")))
check("a three-digit number that is not a bracketed status is not one", not T(RuntimeError("could not send 503 rows to [Daily Bids]")))

print("\nsend_with_retry")


def flaky(fails, exc=None):
    calls = []

    def f():
        calls.append(1)
        if len(calls) <= fails:
            raise (exc or RuntimeError(G503))
        return "graph"
    return f, calls


del SLEEPS[:]
f, calls = flaky(0)
check("a send that works is sent once, no waiting", cr.send_with_retry(f, (10, 20)) == "graph" and len(calls) == 1 and SLEEPS == [], (calls, SLEEPS))
f, calls = flaky(2)
r = cr.send_with_retry(f, (10, 20, 30))
check("503 twice, then it works: three tries, waiting 10 then 20 seconds, and the third one's result comes back", r == "graph" and len(calls) == 3 and SLEEPS == [10, 20], (r, len(calls), SLEEPS))
del SLEEPS[:]
f, calls = flaky(99)
try:
    cr.send_with_retry(f, (10, 20))
    err = None
except RuntimeError as e:
    err = str(e)
check("a service that stays down: every wait is used (3 tries, 10 + 20 s) and the last error is raised", err == G503 and len(calls) == 3 and SLEEPS == [10, 20], (err, len(calls), SLEEPS))
del SLEEPS[:]
f, calls = flaky(99, RuntimeError(G403))
try:
    cr.send_with_retry(f, (10, 20))
    err = None
except RuntimeError as e:
    err = str(e)
check("a 403 is raised at once: one try, no waiting", err == G403 and len(calls) == 1 and SLEEPS == [], (len(calls), SLEEPS))
del SLEEPS[:]
f, calls = flaky(99)
try:
    cr.send_with_retry(f)
    err = None
except RuntimeError as e:
    err = str(e)
check("no waits (the default, what a person at a button gets) is exactly one try", err == G503 and len(calls) == 1 and SLEEPS == [], (len(calls), SLEEPS))
del SLEEPS[:]
seq = [RuntimeError(G503), RuntimeError(G403)]
n = []


def switches():
    n.append(1)
    raise seq[len(n) - 1]


try:
    cr.send_with_retry(switches, (10, 20, 30))
    err = None
except RuntimeError as e:
    err = str(e)
check("the service answers again but now refuses (503 then 403): it stops there, not after all the waits", err == G403 and len(n) == 2 and SLEEPS == [10], (err, len(n), SLEEPS))
del SLEEPS[:]
import requests                         # noqa: E402

f, calls = flaky(1, requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)"))
check("the exception type `requests` raises is retried too", cr.send_with_retry(f, (7,)) == "graph" and len(calls) == 2 and SLEEPS == [7], (len(calls), SLEEPS))

print("\nthe daily email")
W = cr.DAILY_SEND_RETRY_WAITS
check("the waits are whole seconds that add up to 20 - 60 minutes: long enough to ride out a blip, inside cron-alert's 90-minute limit with the 15-minute run",
      all(isinstance(w, int) and w > 0 for w in W) and 20 * 60 <= sum(W) <= 60 * 60, (W, sum(W)))


class Sender:
    def __init__(self, fails, msg=G503):
        self.fails, self.msg, self.calls = fails, msg, []

    def __call__(self, subject, html, to_addr, cc=None, inline_images=None, bcc=None):
        self.calls.append((subject, html, to_addr, cc, bool(inline_images), bcc))
        if len(self.calls) <= self.fails:
            raise RuntimeError(self.msg)
        return "graph"


BUILDS = []


def run_daily(sender, **kw):
    del BUILDS[:]
    del SLEEPS[:]
    with mock.patch.object(cr, "build_changes_email_html", lambda mode="spot": BUILDS.append(mode) or "<p>report</p>"), mock.patch.object(cr, "signature_html", lambda: "<p>sig</p>"), \
            mock.patch.object(cr, "_table_watermark", lambda: None), mock.patch.object(cr, "send_email", sender), mock.patch.dict(os.environ, {"CHANGES_EMAIL_CC": "", "DAILY_BIDS_BCC": ""}):
        return cr.send_daily_changes_email("kolten@example.com", **kw)


s = Sender(2)
ok = run_daily(s, retry_waits=(5, 7))
check("503 twice then it goes: True, three sends of the SAME message (subject, body, recipients), the report built once, waits 5 and 7",
      ok is True and len(s.calls) == 3 and len(set(s.calls)) == 1 and len(BUILDS) == 1 and SLEEPS == [5, 7], (ok, len(s.calls), len(set(s.calls)), BUILDS, SLEEPS))
check("the message is the usual one: 'JSA Daily Basis Changes - ...' to the recipient, the report plus the signature, the group on BCC",
      s.calls[0][0].startswith("JSA Daily Basis Changes - ") and s.calls[0][1] == "<p>report</p><p>sig</p>" and s.calls[0][2] == "kolten@example.com" and bool(s.calls[0][5]), s.calls[0])
s = Sender(99)
try:
    run_daily(s, retry_waits=(5, 7))
    err = None
except RuntimeError as e:
    err = str(e)
check("still down after every wait: three tries, then the error is raised (the run logs it and exits 3)", err == G503 and len(s.calls) == 3 and SLEEPS == [5, 7], (err, len(s.calls), SLEEPS))
s = Sender(99, G403)
try:
    run_daily(s, retry_waits=(5, 7))
    err = None
except RuntimeError as e:
    err = str(e)
check("a refusal (403) is not waited out: one try", err == G403 and len(s.calls) == 1 and SLEEPS == [], (len(s.calls), SLEEPS))
s = Sender(1)
try:
    run_daily(s)
    err = None
except RuntimeError as e:
    err = str(e)
check("called as before (no retry_waits: the CLI, a person): one try and the error", err == G503 and len(s.calls) == 1 and SLEEPS == [], (len(s.calls), SLEEPS))

print("\nauto_import: the run's side of it")
src = open(os.path.join(ROOT, "auto_import.py"), "rb").read().decode("utf-8").replace("\r\n", "\n")
tree = ast.parse(src)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_send_daily_changes_email")
ns = {"log": logging.getLogger("auto_import_test")}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "auto_import.py", "exec"), ns)      # the real function, without importing the whole scraper stack
send_daily = ns["_send_daily_changes_email"]
got = {}


def fake_ok(*a, **kw):
    got["kw"] = kw
    return True


with mock.patch.object(cr, "send_daily_changes_email", fake_ok):
    check("it sends with the retry waits and says True", send_daily() is True and got["kw"] == {"retry_waits": cr.DAILY_SEND_RETRY_WAITS}, got)


def fake_down(*a, **kw):
    raise RuntimeError(G503)


with mock.patch.object(cr, "send_daily_changes_email", fake_down):
    r = send_daily()
check("when the email cannot be sent it says False and never raises (the rest of the run, the client reports, still goes on)", r is False, r)
check("the run: only an emailing run (not --no-email) sends, and it keeps the answer",
      re.search(r"_email_failed = False\n\s+if not args\.no_email:\n\s+_email_failed = not _send_daily_changes_email\(\)", src) is not None)
check("the run ends with exit 3 when it did not go, after the pull-health exit",
      re.search(r"sys\.exit\(_health_rc\)\n\s+if _email_failed:\n(?:.*\n)*?\s+sys\.exit\(3\)", src) is not None)

if FAILS:
    print("\n%d FAILED:" % len(FAILS))
    for f_ in FAILS:
        print("  -", f_)
    sys.exit(1)
print("\nall email-retry checks passed")
