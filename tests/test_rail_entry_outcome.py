"""Rail Entry (admin tab): what "Save all" did must stay on the page after its st.rerun() — above all a FAILED update email.

    python tests/test_rail_entry_outcome.py

Before 2026-10-06 the save handler drew "Saved ..." / "the email didn't send (...)" and then called st.rerun(), which wipes whatever was
drawn that run: a failed email looked exactly like a successful save. The outcome now goes through session_state and is drawn at the top
of the tab on the next run.

Drives the real ADMIN app (app.py) with streamlit.testing.v1.AppTest in total isolation: every Snowflake / Graph setting is blanked, the
database is a throwaway SQLite file, and the two things that matter are mocked — database.save_rail_fob (nothing is written) and
rail_report.send_rail_update_email (nothing is sent; it either succeeds or raises the error under test).
"""
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

try:
    from streamlit.testing.v1 import AppTest
except ImportError:                                           # an old Streamlit has no AppTest
    print("  [SKIP] streamlit.testing.v1 is not available")
    sys.exit(0)

# ISOLATION: pre-set (empty) every connection key so app.py's load_dotenv() cannot fill them from a .env file.
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_ROLE", "SNOWFLAKE_WAREHOUSE",
          "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY_PWD", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL", "RIVER_DATABASE_URL",
          "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_SENDER", "APP_PASSWORD", "VIEW_ONLY"):
    os.environ[k] = ""

import changes_report    # noqa: E402
import database          # noqa: E402
import rail_corridors    # noqa: E402
import rail_email_log    # noqa: E402
import rail_report       # noqa: E402
import river_fob_data    # noqa: E402

database.DB_PATH = Path(tempfile.mkdtemp()) / "throwaway.db"
assert not database._use_sf(), "this test must not touch Snowflake"

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""), flush=True)
    if not cond:
        FAILS.append(name)


ISO = rail_corridors.today_ct().isoformat()          # the tab's default Posting date (Central; the UTC date is already tomorrow after ~7 PM CT)
STAGED = [{"market": "CSX Columbus", "commodity": "Corn", "period": "Dec", "futures": "ZCH27", "bid": 10, "offer": 15, "bid_raw": None, "offer_raw": None},
          {"market": "CSX Freight", "commodity": "Freight", "period": "Return Trip", "futures": None, "bid": 3000, "offer": 3100, "bid_raw": None, "offer_raw": None}]


def run_case(label, send_side_effect, staged=STAGED, graph_ok=False, email_on=True, seed_posting=None):
    """Open the admin app with `staged` rundown rows, press Save all; returns (app, saves, sends). `graph_ok` = the Graph secrets are 'set'."""
    saves, sends = [], []

    def fake_save(date, source, rows):
        saves.append((date, source, [r["market"] for r in rows]))
        return len(rows)

    def fake_send(markets=None, to_addr=None):
        sends.append(markets)
        if isinstance(send_side_effect, Exception):
            raise send_side_effect
        return True

    patches = [mock.patch.object(database, "save_rail_fob", fake_save), mock.patch.object(rail_report, "send_rail_update_email", fake_send),
               mock.patch.object(river_fob_data, "list_dates", lambda: []), mock.patch.object(river_fob_data, "latest_date", lambda: None),
               mock.patch.object(river_fob_data, "load_archive", lambda: {}),
               mock.patch.object(changes_report, "_graph_configured", lambda: graph_ok)]
    for p in patches:
        p.start()
    try:
        at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=900)
        at.session_state["rail_entry_rows"] = staged
        at.session_state["rail_entry_warn"] = []
        at.session_state["rail_entry_meta"] = (ISO, "Corn")
        t0 = time.time()
        at.run()
        print("  %s: first run %.0fs; exceptions: %s" % (label, time.time() - t0, [str(e.value)[:160] for e in at.exception] or "none"), flush=True)
        btn = next((b for b in at.button if b.key == "rail_entry_save"), None)
        check("the Save all button is there (admin build, rows staged)", btn is not None)
        if seed_posting:                                      # the rundown the (mocked) save "wrote", so the ledger can see it
            c = database.get_conn()
            c.cursor().execute("INSERT INTO rail_fob (date, source, market, commodity, period, bid, captured_at) VALUES (?,?,?,?,?,?,?)", seed_posting)
            c.commit()
            c.close()
        if not email_on:
            at.checkbox(key="rail_entry_email").uncheck()
        btn.click()
        at.run()
    finally:
        for p in patches:
            p.stop()
    return at, saves, sends


print("the update email FAILS after Save all")
at, saves, sends = run_case("failing send", RuntimeError("Email send failed — Outlook: No module named 'win32com'; Graph: Graph auth failed: simulated; SMTP: SMTP not configured"))
manual = [s for s in saves if s[1] == "manual"]            # (the admin app may also archive Palmetto's scrape on load: not under test)
check("the rundown was saved and the update email was attempted for the BASIS corridor only (freight excluded)",
      sends == [["CSX Columbus"]] and {m for s in manual for m in s[2]} == {"CSX Columbus", "CSX Freight"}, (manual, sends))
errs = [e.value for e in at.error]
check("after the st.rerun() the error is STILL on the page, with the real reason and what to check",
      any("did NOT send" in e and "Graph auth failed: simulated" in e and "GRAPH_TENANT_ID" in e for e in errs), errs)
check("and so is the 'Saved 2 corridor(s)' confirmation", any("Saved 2 corridor(s): CSX Columbus, CSX Freight" in s.value for s in at.success), [s.value for s in at.success])
check("the staged preview is gone and the outcome is shown only once (it was popped)", "rail_entry_outcome" not in at.session_state and "rail_entry_rows" not in at.session_state)
warns = [w.value for w in at.warning]
check("with no Graph secrets the tab says so up front (before any save), naming the settings and where they go",
      any("can't send email" in w and "GRAPH_TENANT_ID" in w and "Settings" in w for w in warns), warns)
check("the Posting date defaults to today's CENTRAL date (after ~7 PM CT the UTC date is already tomorrow)",
      at.date_input(key="rail_entry_date").value == rail_corridors.today_ct(), (at.date_input(key="rail_entry_date").value, rail_corridors.today_ct()))

print("the update email SENDS")
at2, saves2, sends2 = run_case("working send", None, graph_ok=True)
succ2 = [e.value for e in at2.success]
check("the success lines survive the rerun too: saved + emailed", any("Saved 2 corridor(s)" in s for s in succ2) and any("Emailed update for: CSX Columbus" in s for s in succ2), succ2)
check("no error", not [e.value for e in at2.error], [e.value for e in at2.error])
check("and no 'can't send email' banner when the Graph secrets are there", not any("can't send email" in w.value for w in at2.warning), [w.value for w in at2.warning])

print("only freight saved")
at3, saves3, sends3 = run_case("freight only", None, staged=[STAGED[1]])
infos = [e.value for e in at3.info]
check("nothing was emailed and the page says only freight was saved", sends3 == [] and any("only freight corridors were saved" in i for i in infos), (sends3, infos))

print("the droplet catch-up emailer: what the app tells it")
hb = rail_email_log.read_ledger(kinds=("heartbeat",))
check("opening the Rail Entry tab writes a HEARTBEAT (proof for the droplet watcher that this app logs its sends)", len(hb) >= 1, hb)
_five_s_ago = rail_email_log.iso(rail_email_log.now_utc() - timedelta(seconds=5))
at5, saves5, sends5 = run_case("email switched off", None, graph_ok=True, email_on=False, seed_posting=(ISO, "manual", "CSX Columbus", "Corn", "Dec", 10, _five_s_ago))
skipped = rail_email_log.read_ledger(kinds=("skipped",))
check("saved with the email switch OFF: nothing is emailed and a 'skipped' ledger row says it was on purpose (the watcher leaves it alone)",
      sends5 == [] and [(r["market"], r["date"]) for r in skipped] == [("CSX Columbus", ISO)], (sends5, skipped))
check("and the page says so", any("without emailing" in i.value for i in at5.info), [i.value for i in at5.info])
check("a FAILED email leaves no 'update' row, so the watcher will catch it up", not rail_email_log.read_ledger(kinds=("update", "catchup")))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
