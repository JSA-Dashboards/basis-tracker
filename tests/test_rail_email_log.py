"""The rail email ledger (rail_email_log) and the droplet catch-up emailer (rail_email_watch).

    python tests/test_rail_email_log.py

A rundown that was saved but never emailed is emailed by the watcher — and ONLY then: this is a client broadcast, so every way it
could send twice or send what was left off on purpose is checked here. Nothing touches Snowflake or Graph: the database is a throwaway
SQLite file and every send is a recorder.
"""
import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY",
          "DATABASE_URL", "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_SENDER", "RAIL_EMAIL_VIA"):
    os.environ[k] = ""

import database                                   # noqa: E402
import rail_corridors                            # noqa: E402
import rail_email_log as rel                     # noqa: E402
import rail_email_watch as watch                 # noqa: E402
import rail_report                               # noqa: E402

database.DB_PATH = Path(tempfile.mkdtemp()) / "throwaway.db"
assert not database._use_sf(), "this test must not touch Snowflake"

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


UTC = timezone.utc
T = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
M = timedelta(minutes=1)
GRACE = 15 * M
SINCE = T
MIN_D, MAX_D = "2026-10-04", "2026-10-07"


def post(market, date, minutes_after_T):
    return {"market": market, "date": date, "last_capture": T + minutes_after_T * M}


def led(kind, market=None, date=None, snap_min=None, via="cloud", sent_min=0):
    return {"kind": kind, "market": market, "date": date, "via": via,
            "snapshot_at": None if snap_min is None else T + snap_min * M, "sent_at": T + sent_min * M}


def unc(postings, ledger, now_min, sent=frozenset(), since=SINCE):
    return rel.uncovered(postings, ledger, now=T + now_min * M, grace=GRACE, since=since, min_date=MIN_D, max_date=MAX_D, sent=sent)


print("uncovered(): which postings were saved but never emailed")
P = [post("CSX Columbus", "2026-10-06", 2), post("NS Ft Wayne", "2026-10-06", 2)]
check("saved 20 min ago, no ledger row -> both are waiting", [p["market"] for p in unc(P, [], 22)] == ["CSX Columbus", "NS Ft Wayne"])
check("saved 10 min ago -> left alone: the app's own send may still be running (grace 15 min)", unc(P, [], 12) == [])
check("saved BEFORE the baseline -> history, never emailed by the watcher", unc(P, [], 60, since=T + 30 * M) == [])
check("an 'update' row written after the save, showing that date -> covered",
      unc(P, [led("update", "CSX Columbus", "2026-10-06", snap_min=5), led("update", "NS Ft Wayne", "2026-10-06", snap_min=5)], 60) == [])
check("only the corridors it names are covered (NS is still waiting)", [p["market"] for p in unc(P, [led("update", "CSX Columbus", "2026-10-06", snap_min=5)], 60)] == ["NS Ft Wayne"])
check("a RE-SAVE after the email (captured later than the row's snapshot) is waiting again",
      [p["market"] for p in unc([post("CSX Columbus", "2026-10-06", 40)], [led("update", "CSX Columbus", "2026-10-06", snap_min=5)], 70)] == ["CSX Columbus"])
check("an email built before the save does not cover it (snapshot 1 min < capture 2 min)", len(unc(P[:1], [led("update", "CSX Columbus", "2026-10-06", snap_min=1)], 60)) == 1)
check("an email that showed a LATER date covers the older posting (superseded)",
      unc([post("CSX Columbus", "2026-10-06", 2)], [led("update", "CSX Columbus", "2026-10-07", snap_min=30)], 60) == [])
check("...but an email that only showed an EARLIER date does not cover a newer posting",
      len(unc([post("CSX Columbus", "2026-10-07", 2)], [led("update", "CSX Columbus", "2026-10-06", snap_min=30)], 60)) == 1)
check("a 'recap', a 'catchup' and a 'skipped' (saved with the email off) row all count as handled",
      all(unc(P[:1], [led(k, "CSX Columbus", "2026-10-06", snap_min=5)], 60) == [] for k in ("recap", "catchup", "skipped")))
check("a 'heartbeat' / 'baseline' row covers nothing", len(unc(P[:1], [led("heartbeat", snap_min=5), led("baseline", snap_min=5)], 60)) == 1)
check("freight / shuttle corridors are never emailed", unc([post("CSX Freight", "2026-10-06", 2), post("BN Shuttle", "2026-10-06", 2)], [], 60) == [])
check("a corridor the email drops (UP Illinois (Dom)) is ignored", unc([post("UP Illinois (Dom)", "2026-10-06", 2)], [], 60) == [])
check("a posting dated outside the horizon (a bulk rebuild of old history) is ignored",
      unc([post("CSX Columbus", "2026-08-12", 2), post("CSX Columbus", "2026-10-09", 2)], [], 60) == [])
sig = rel.signature("CSX Columbus", "2026-10-06", T + 2 * M)
check("a posting the watcher already sent itself (state file) is not sent again, even if the ledger write failed",
      [p["market"] for p in unc(P, [], 60, sent=frozenset([sig]))] == ["NS Ft Wayne"])
check("a posting with no capture time (an old row) is ignored", unc([{"market": "CSX Columbus", "date": "2026-10-06", "last_capture": None}], [], 60) == [])

print("cloud_proof(): is the Cloud app writing the ledger yet?")
check("no cloud row -> not proven (the watcher stays silent)", rel.cloud_proof([led("update", via="droplet", sent_min=5), led("baseline", via="droplet")], SINCE) is None)
check("a Cloud heartbeat after the baseline proves it", rel.cloud_proof([led("heartbeat", via="cloud", sent_min=5)], SINCE) == T + 5 * M)
check("a Cloud send (update / skipped) proves it too", rel.cloud_proof([led("skipped", via="cloud", sent_min=7)], SINCE) == T + 7 * M)
check("a Cloud row from BEFORE the baseline does not", rel.cloud_proof([led("heartbeat", via="cloud", sent_min=-5)], SINCE) is None)

print("helpers")
check("is_emailable", rel.is_emailable("CSX Columbus") and not rel.is_emailable("BN Freight") and not rel.is_emailable("UP Shuttle") and not rel.is_emailable("UP Illinois (Dom)") and not rel.is_emailable(""))
check("parse_ts reads the stored forms (offset, Z, naive = UTC, blank)", rel.parse_ts("2026-10-06T16:00:51.872245+00:00") == datetime(2026, 10, 6, 16, 0, 51, 872245, tzinfo=UTC)
      and rel.parse_ts("2026-10-06T16:00:51Z") == datetime(2026, 10, 6, 16, 0, 51, tzinfo=UTC) and rel.parse_ts("2026-10-06T16:00:51") == datetime(2026, 10, 6, 16, 0, 51, tzinfo=UTC)
      and rel.parse_ts("") is None and rel.parse_ts(None) is None and rel.parse_ts("garbage") is None)
check("where_am_i: local here, 'cloud' under /mount/src or RAIL_EMAIL_VIA", rel.where_am_i() in ("local", "droplet", "cloud"))

# ── the database round trip, on a throwaway SQLite ────────────────────────────
print("the ledger on a database")
conn = database.get_conn()
conn.cursor().execute("""CREATE TABLE rail_fob (date TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual', market TEXT NOT NULL, rail TEXT, commodity TEXT,
                         period TEXT NOT NULL, period_order INTEGER, futures TEXT, bid INTEGER, offer INTEGER, bid_raw TEXT, offer_raw TEXT, captured_at TEXT)""")
conn.commit()
conn.close()
TODAY = rail_corridors.today_ct().isoformat()


def put(market, date, captured, source="manual"):
    c = database.get_conn()
    c.cursor().execute("INSERT INTO rail_fob (date, source, market, commodity, period, bid, captured_at) VALUES (?,?,?,?,?,?,?)",
                       (date, source, market, "Corn", "Dec", 10, rel.iso(captured)))
    c.commit()
    c.close()


T0 = rel.now_utc()
put("CSX Columbus", TODAY, T0 - 5 * M)                       # an old posting (before the watcher starts)
snap = rel.snapshot(["CSX Columbus", "NS Ft Wayne"])
check("snapshot: the latest posting of each emailable corridor asked for (NS has none yet)", [(p["market"], p["date"]) for p in snap["postings"]] == [("CSX Columbus", TODAY)])
put("CSX Freight", TODAY, T0 - 5 * M)
put("UP Illinois (Dom)", TODAY, T0 - 5 * M)
put("NS Ft Wayne", TODAY, T0 - 5 * M, source="palmetto")     # not a manual rundown
check("snapshot(None) = every emailable manual corridor: no freight, no dropped corridor, no Palmetto",
      [p["market"] for p in rel.snapshot(None)["postings"]] == ["CSX Columbus"])
check("record(): one row per corridor, with the snapshot time and the capture it covers",
      rel.record(snap, "update", "JSA Rail Update") == 1 and [(r["kind"], r["market"], r["date"]) for r in rel.read_ledger()] == [("update", "CSX Columbus", TODAY)])
check("mark_handled(): a 'skipped' row for an intentional no-email save", rel.mark_handled(["CSX Columbus"], "skipped") == 1 and [r["kind"] for r in rel.read_ledger(kinds=("skipped",))] == ["skipped"])
rel.heartbeat()
check("heartbeat(): a row with no corridor", [(r["market"], r["via"]) for r in rel.read_ledger(kinds=("heartbeat",))] == [(None, rel.where_am_i())])
check("no baseline until the watcher sets one", rel.baseline() is None)

# ── the watcher, end to end (simulated clock; sends are recorders) ───────────
print("rail_email_watch: one posting's life")
db_path_ledger_reset = database.get_conn()
db_path_ledger_reset.cursor().execute("DELETE FROM rail_email_log")
db_path_ledger_reset.commit()
db_path_ledger_reset.close()
state = Path(tempfile.mkdtemp())
sent_calls = []


def args(**kw):
    ns = argparse.Namespace(dry_run=False, grace_min=15.0, horizon_days=2, force_armed=False, to=None, no_bcc=False, allow_sqlite=True, state_dir=None, baseline_iso=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def fake_send_factory(now_sim, fail=False, log_ledger=True):
    def fake_send(markets=None, to_addr=None, ledger_kind="update", bcc="UNSET"):
        sent_calls.append({"markets": list(markets), "to": to_addr, "kind": ledger_kind, "bcc": bcc})
        if fail:
            raise RuntimeError("Graph auth failed: simulated")
        if log_ledger:                                       # what rail_report._ledger_record does, on the simulated clock
            sn = rel.snapshot(markets)
            rel.record({"at": now_sim, "postings": sn["postings"]}, ledger_kind, "x")
        return True
    return fake_send


import contextlib
import io


def run_quiet(now_sim, send=None, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = watch.run(args(**kw), send=send or fake_send_factory(now_sim), now=now_sim, state_dir=state)
    return rc, buf.getvalue()


rc, out = run_quiet(T0, dry_run=True)
check("--dry-run before any baseline creates nothing", rc == 0 and "no baseline yet" in out and rel.baseline() is None, out)
rc, out = run_quiet(T0)
check("first run only sets the BASELINE (nothing earlier is ever emailed)", rc == 0 and "baseline set" in out and rel.baseline() is not None and sent_calls == [], out)
BASE = rel.baseline()
put("CSX Columbus", TODAY, BASE + 2 * M)
put("NS Ft Wayne", TODAY, BASE + 2 * M)
put("CSX Freight", TODAY, BASE + 2 * M)
put("UP Illinois (Dom)", TODAY, BASE + 2 * M)
rc, out = run_quiet(BASE + 10 * M)
check("8 minutes after the save: inside the grace period -> nothing waiting", rc == 0 and "nothing waiting" in out and sent_calls == [], out)
rc, out = run_quiet(BASE + 20 * M)
check("18 minutes after, but the Cloud app has not written the ledger -> UNARMED: says what it WOULD send and sends nothing",
      rc == 0 and "UNARMED" in out and "WOULD email 2 corridor(s)" in out and "CSX Columbus" in out and "NS Ft Wayne" in out and "Freight" not in out and sent_calls == [], out)
_sf = state / "rail_email_watch.json"
state_before = _sf.read_text() if _sf.exists() else None
rc, out = run_quiet(BASE + 20 * M, dry_run=True)
check("--dry-run says the same and writes nothing (not even the state file)", rc == 0 and "dry run" in out and sent_calls == [] and (_sf.read_text() if _sf.exists() else None) == state_before, out)
rel._insert([{"sent_at": rel.iso(BASE + 3 * M), "via": "cloud", "host": "cloud-pod", "kind": "heartbeat"}])      # the Cloud app opens its Rail Entry tab
rc, out = run_quiet(BASE + 21 * M, dry_run=True)
check("dry-run once armed: still sends nothing", rc == 0 and "armed (Cloud app checked in" in out and "dry run" in out and sent_calls == [], out)
rc, out = run_quiet(BASE + 21 * M)
check("armed: ONE catch-up email for the two basis corridors (freight and the dropped corridor left out), logged as 'catchup'",
      rc == 0 and "SENT catch-up update for 2 corridor(s)" in out and len(sent_calls) == 1 and sent_calls[0]["markets"] == ["CSX Columbus", "NS Ft Wayne"]
      and sent_calls[0]["kind"] == "catchup" and sent_calls[0]["bcc"] == "UNSET", (out, sent_calls))
check("the send is logged in the ledger (so the next run sees it covered)", {(r["kind"], r["market"]) for r in rel.read_ledger(kinds=("catchup",))} == {("catchup", "CSX Columbus"), ("catchup", "NS Ft Wayne")})
rc, out = run_quiet(BASE + 31 * M)
check("ten minutes later: nothing waiting, nothing sent again", rc == 0 and "nothing waiting" in out and len(sent_calls) == 1, out)

print("rail_email_watch: re-saves, intentional skips, failures, duplicates")
put("CSX Columbus", TODAY, BASE + 40 * M)                        # a correction re-saved later
rc, out = run_quiet(BASE + 50 * M)
check("a re-save 10 minutes ago is inside the grace period", "nothing waiting" in out and len(sent_calls) == 1, out)
rc, out = run_quiet(BASE + 60 * M, send=fake_send_factory(BASE + 60 * M, fail=True))
check("the send FAILS: no ledger row, exit 0 on the first failure (no alert yet), the posting is still waiting", rc == 0 and "ERROR (1 in a row)" in out and len(sent_calls) == 2, out)
rc, out = run_quiet(BASE + 70 * M, send=fake_send_factory(BASE + 70 * M, fail=True))
check("the second failure in a row exits 1 -> cron-alert emails the failure (once, not every 10 minutes)", rc == 1 and "ERROR (2 in a row)" in out and "ALERT" in out, out)
rc, out = run_quiet(BASE + 80 * M, send=fake_send_factory(BASE + 80 * M, fail=True))
check("a third failure stays quiet (alerts come at 2, 12, 72 ... failures)", rc == 0 and "ERROR (3 in a row)" in out, out)
rc, out = run_quiet(BASE + 90 * M)
check("the next run retries and succeeds: the re-saved corridor goes out, the streak resets",
      rc == 0 and "SENT catch-up update for 1 corridor(s)" in out and sent_calls[-1]["markets"] == ["CSX Columbus"] and watch.load_state(state)["fail_streak"] == 0, out)

put("NS Ft Wayne", TODAY, BASE + 100 * M)
rel.record({"at": BASE + 101 * M, "postings": rel.snapshot(["NS Ft Wayne"])["postings"]}, "skipped")      # saved with the email switch OFF (a moment after the save)
n_before = len(sent_calls)
rc, out = run_quiet(BASE + 130 * M)
check("a save with the email off ('skipped') is left alone", rc == 0 and "nothing waiting" in out and len(sent_calls) == n_before, out)

put("CSX Columbus", TODAY, BASE + 140 * M)
rc, out = run_quiet(BASE + 160 * M, send=fake_send_factory(BASE + 160 * M, log_ledger=False))
check("the email goes out but the LEDGER WRITE fails (here: not attempted)", rc == 0 and "SENT catch-up" in out and len(sent_calls) == n_before + 1, out)
rc, out = run_quiet(BASE + 170 * M)
check("...it is NOT sent a second time: the state file remembers it (no 10-minute duplicate loop)", rc == 0 and "nothing waiting" in out and len(sent_calls) == n_before + 1, out)

print("rail_email_watch: switches")
(state / "rail_email_watch.off").write_text("off")
put("CSX Columbus", TODAY, BASE + 180 * M)
rc, out = run_quiet(BASE + 220 * M)
check("the kill switch file stops it before it touches anything", rc == 0 and "disabled" in out and len(sent_calls) == n_before + 1, out)
(state / "rail_email_watch.off").unlink()
rc, out = run_quiet(BASE + 220 * M, force_armed=True, to="kpostin@jpsi.com", no_bcc=True)
check("--force-armed --to ... --no-bcc: a live test that emails only the address given, still logged as a catch-up",
      rc == 0 and sent_calls[-1]["to"] == "kpostin@jpsi.com" and sent_calls[-1]["bcc"] is None and sent_calls[-1]["kind"] == "catchup", (out, sent_calls[-1]))
rc, out = run_quiet(BASE + 230 * M, allow_sqlite=False)
check("on a non-Snowflake backend it refuses (an empty local SQLite would silently watch nothing)", rc == 0 and "not Snowflake" in out and "ERROR" in out, out)

print("rail_report: the send functions write the ledger")
sent_mail = []


def fake_send_email(subject, html, to_addr, cc=None, inline_images=None, bcc=None):
    sent_mail.append({"subject": subject, "to": to_addr, "bcc": bcc})
    return "fake"


rc = database.get_conn()
rc.cursor().execute("DELETE FROM rail_email_log")
rc.commit()
rc.close()
with mock.patch.object(rail_report, "send_email", fake_send_email), mock.patch.object(rail_report, "build_rail_html", lambda **k: ("<p>x</p>", {})):
    ok = rail_report.send_rail_update_email(markets=["CSX Columbus"])
    check("send_rail_update_email: sent To kpostin, BCC the JSA group, and logged one 'update' row for the corridor",
          ok and sent_mail[-1]["bcc"] == "jsagroup@jpsi.com" and [(r["kind"], r["market"], r["via"]) for r in rel.read_ledger(kinds=("update",))] == [("update", "CSX Columbus", rel.where_am_i())], sent_mail)
    rail_report.send_rail_update_email(markets=["CSX Columbus"], to_addr="me@example.com", bcc=None, ledger_kind="catchup")
    check("bcc=None sends to the one address only; ledger_kind='catchup' is what gets logged", sent_mail[-1]["bcc"] is None and sent_mail[-1]["to"] == "me@example.com"
          and len(rel.read_ledger(kinds=("catchup",))) == 1)
    rail_report.send_rail_recap_email()
    check("send_rail_recap_email logs a 'recap' row per emailable corridor (no freight, no dropped corridor)",
          sorted((r["kind"], r["market"]) for r in rel.read_ledger(kinds=("recap",))) == [("recap", "CSX Columbus"), ("recap", "NS Ft Wayne")])
    with mock.patch.object(rel, "record", side_effect=RuntimeError("table locked")), mock.patch.object(rel, "snapshot", side_effect=RuntimeError("db down")):
        check("a ledger failure never fails the send", rail_report.send_rail_update_email(markets=["CSX Columbus"]) is True and len(sent_mail) == 4)
    check("no corridors -> no email, as before", rail_report.send_rail_update_email(markets=[]) is False)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
