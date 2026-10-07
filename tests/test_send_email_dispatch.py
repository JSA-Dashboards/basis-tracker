"""changes_report.send_email: Outlook -> Graph -> SMTP, and an error that says what was missing.

    python tests/test_send_email_dispatch.py

On 2026-10-06 the Cloud admin app had lost its GRAPH_* secrets. send_email skipped the Graph step silently, so the only error left was the
SMTP login of the long-dead interim relay ("535 5.7.3 Authentication unsuccessful") — and an evening went into chasing that. A missing Graph
configuration is now named in the error.

Nothing is sent: all three senders are replaced by recorders, so this is safe on a PC that has Outlook.
"""
import os
import sys
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import changes_report as cr      # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def run(outlook=None, graph_configured=False, graph=None, smtp=None):
    """Call send_email with the three senders replaced. Each of outlook / graph / smtp is None (works) or an Exception to raise.
    Returns (result, calls) where result is the path name or the RuntimeError message."""
    calls = []

    def mk(name, err):
        def f(*a, **k):
            calls.append(name)
            if err is not None:
                raise err
        return f

    fake_pywin = {"win32com": types.ModuleType("win32com"), "win32com.client": types.ModuleType("win32com.client")}   # so the Outlook branch is reachable anywhere
    with mock.patch.dict(sys.modules, fake_pywin), \
            mock.patch.object(cr, "send_via_outlook", mk("outlook", outlook)), \
            mock.patch.object(cr, "send_via_graph", mk("graph", graph)), \
            mock.patch.object(cr, "send_via_smtp", mk("smtp", smtp)), \
            mock.patch.object(cr, "_graph_configured", lambda: graph_configured):
        try:
            return cr.send_email("subject", "<p>x</p>", "to@example.com", bcc="group@example.com"), calls
        except RuntimeError as e:
            return str(e), calls


SMTP_DEAD = RuntimeError("(535, b'5.7.3 Authentication unsuccessful')")

print("send_email: the path that works wins")
res, calls = run(outlook=None)
check("a working Outlook sends and nothing else is tried", res == "outlook" and calls == ["outlook"], (res, calls))
res, calls = run(outlook=ImportError("No module named 'win32com'"), graph_configured=True, graph=None)
check("no Outlook (Cloud, droplet) + Graph configured -> Graph sends, SMTP is never tried", res == "graph" and calls == ["outlook", "graph"], (res, calls))
res, calls = run(outlook=ImportError("x"), graph_configured=False, graph=None, smtp=None)
check("no Outlook, Graph not configured, SMTP works -> SMTP", res == "smtp" and calls == ["outlook", "smtp"], (res, calls))

print("send_email: every failure is named (the 2026-10-06 case)")
res, calls = run(outlook=ImportError("No module named 'win32com'"), graph_configured=False, smtp=SMTP_DEAD)
check("Graph not configured + a dead SMTP relay: the error NAMES the missing Graph configuration, not just the SMTP login",
      "Graph: NOT CONFIGURED" in res and "GRAPH_TENANT_ID" in res and "Settings" in res and "535" in res and "No module named 'win32com'" in res and "graph" not in calls, res)
check("...and the Graph sender was never called", calls == ["outlook", "smtp"], calls)
res, calls = run(outlook=ImportError("x"), graph_configured=True, graph=RuntimeError("Graph auth failed: invalid_client"), smtp=SMTP_DEAD)
check("Graph configured but failing: its own error is reported (not 'not configured'), then SMTP is tried and reported too",
      "Graph: Graph auth failed: invalid_client" in res and "NOT CONFIGURED" not in res and "535" in res and calls == ["outlook", "graph", "smtp"], (res, calls))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
