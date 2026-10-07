#!/usr/bin/env python3
"""
rail_email_watch.py — the droplet's catch-up for rail update emails.

A rundown is saved by the Cloud admin app (or post_rail.py) and that same code sends the update email. When the send fails — as it
did on 2026-10-06, when the Cloud app's Secrets had lost the Graph keys — nothing else noticed. This job (cron, every 10 minutes
06:00-22:50 CT, deploy/run_rail_email_watch.sh) looks for postings that were saved but never emailed (rail_email_log has no row
covering them) and sends the same update email the app would have: To kpostin@, BCC the JSA group.

Safety rules — it is a client broadcast, so it errs towards staying silent:
  * The first run only records a BASELINE: nothing saved before it is ever emailed by this job.
  * A posting must be older than --grace-min (default 15) so the app's own send has had time to finish.
  * It is UNARMED (it only logs what it would send) until the Cloud app has written the ledger after the baseline — its Rail Entry
    tab writes a heartbeat when opened, and every send it makes is logged. Until then the app may still run code that sends without
    logging, and a catch-up would be a duplicate. --force-armed skips that check.
  * A save made with the Rail Entry email switch off (or post_rail.py --no-email) writes a 'skipped' row: left alone.
  * Everything it sends is also remembered in state/rail_email_watch.json, so a failed ledger write can never make it send twice.
  * state/rail_email_watch.off (or --dry-run) stops the sending. It exits non-zero (an alert through cron-alert) only after the same
    problem repeats, so an outage does not mail an alert every 10 minutes.

    python rail_email_watch.py --dry-run                 # say what it would do; sends nothing, writes nothing
    python rail_email_watch.py --force-armed --to kpostin@jpsi.com --no-bcc --grace-min 0     # a live test that emails only you
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dotenv import load_dotenv                                       # noqa: E402

load_dotenv(HERE / ".env")

import rail_email_log as rel                                         # noqa: E402
from rail_corridors import today_ct                                  # noqa: E402

STATE_DIR = HERE / "state"
FAIL_ALERT_AT = (2, 12, 72, 288)        # consecutive failing runs (20 min, 2 h, 12 h, 2 days at a 10-minute cadence) that exit non-zero
UNARMED_ALERT_AT = (12, 72)             # consecutive runs with postings waiting while still unarmed (2 h, 12 h)
SENT_KEEP = 300


# ── state ─────────────────────────────────────────────────────────────────────
def load_state(state_dir: Path) -> dict:
    try:
        st = json.loads((state_dir / "rail_email_watch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("fail_streak", 0)
    st.setdefault("unarmed_runs", 0)
    st.setdefault("sent", [])
    return st


def save_state(state_dir: Path, st: dict) -> None:
    st["sent"] = st.get("sent", [])[-SENT_KEEP:]
    st["last_run"] = rel.iso(rel.now_utc())
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "rail_email_watch.json.tmp"
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, state_dir / "rail_email_watch.json")


def _ct(dt: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%a %H:%M CT")
    except Exception:                                                # no tz database: CST
        return (dt - timedelta(hours=6)).strftime("%a %H:%M CST")


# ── one run ───────────────────────────────────────────────────────────────────
def run(args, *, send=None, now=None, state_dir: Path | None = None) -> int:
    """One pass. Returns the process exit code (0 = fine / nothing to alert, 1 = alert through cron-alert)."""
    state_dir = Path(state_dir or args.state_dir or STATE_DIR)
    if (state_dir / "rail_email_watch.off").exists():
        print("disabled: %s exists" % (state_dir / "rail_email_watch.off"))
        return 0
    now = now or rel.now_utc()
    st = load_state(state_dir)

    def alert_streak(key: str, at: tuple) -> int:
        st[key] += 1
        return 1 if st[key] in at else 0

    try:
        import database as db
        if not db._use_sf() and not args.allow_sqlite:
            raise RuntimeError("the database backend is not Snowflake (USE_SNOWFLAKE missing from .env?): refusing to watch an empty local SQLite")
        base = rel.baseline() if not args.dry_run else _peek_baseline()
        if args.dry_run and args.baseline_iso:                       # a look at real data as if the watcher had started then (never writes)
            base = rel.parse_ts(args.baseline_iso)
        if base is None:
            if args.dry_run:
                print("dry-run: no baseline yet; a real run would start watching from now (nothing saved before that is ever emailed)")
                return 0
            base = rel.set_baseline()
            print("baseline set at %s: watching from now; nothing saved before it will be emailed" % _ct(base))
            st["fail_streak"] = 0
            save_state(state_dir, st)
            return 0

        today = today_ct()
        min_date = (today - timedelta(days=args.horizon_days)).isoformat()
        max_date = (today + timedelta(days=1)).isoformat()
        postings = rel.latest_postings(None, min_date=min_date)
        ledger = rel.read_ledger(base)
        pending = rel.uncovered(postings, ledger, now=now, grace=timedelta(minutes=args.grace_min), since=base,
                                min_date=min_date, max_date=max_date, sent=frozenset(st["sent"]))
        proof = rel.cloud_proof(ledger, base)
        armed = bool(args.force_armed or proof)
        status = ("armed (Cloud app checked in %s)" % _ct(proof)) if proof else ("armed (--force-armed)" if args.force_armed else "UNARMED (the Cloud app has not checked in since the baseline)")

        if not pending:
            print("ok · %s · nothing waiting" % status)
            st["fail_streak"] = 0
            st["unarmed_runs"] = 0
            if not args.dry_run:
                save_state(state_dir, st)
            return 0

        names = sorted({p["market"] for p in pending})
        what = "; ".join("%s %s (saved %s)" % (p["market"], p["date"], _ct(p["last_capture"])) for p in pending)
        if args.dry_run or not armed:
            print("%s · WOULD email %d corridor(s) but %s: %s" % (status, len(names), "this is a dry run" if args.dry_run else "staying silent", what))
            if args.dry_run:
                return 0
            st["fail_streak"] = 0
            code = alert_streak("unarmed_runs", UNARMED_ALERT_AT)
            save_state(state_dir, st)
            if code:
                print("ALERT: postings have been waiting %d runs and the Cloud app still has not written the ledger "
                      "(reboot the admin app and open its Rail Entry tab, or pass --force-armed)" % st["unarmed_runs"])
            return code

        if send is None:
            from rail_report import send_rail_update_email as send
        kw = {"to_addr": args.to, "ledger_kind": "catchup"}
        if args.no_bcc:
            kw["bcc"] = None
        send(markets=names, **kw)
        st["sent"] += [rel.signature(p["market"], p["date"], p["last_capture"]) for p in pending]
        st["fail_streak"] = 0
        st["unarmed_runs"] = 0
        st["last_ok"] = rel.iso(now)
        save_state(state_dir, st)
        print("%s · SENT catch-up update for %d corridor(s): %s" % (status, len(names), what))
        return 0
    except Exception as exc:                                         # noqa: BLE001 — one bad run must not stop the next one
        traceback.print_exc()
        code = alert_streak("fail_streak", FAIL_ALERT_AT)
        try:
            save_state(state_dir, st)
        except OSError:
            pass
        print("ERROR (%d in a row): %s: %s%s" % (st["fail_streak"], type(exc).__name__, exc, " — ALERT" if code else ""))
        return code


def _peek_baseline():
    """--dry-run must not create anything: read the baseline only if the ledger table already exists."""
    try:
        return rel.baseline()
    except Exception:                                                # noqa: BLE001 — no table yet
        return None


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Catch-up emailer for saved-but-never-emailed rail rundowns.")
    ap.add_argument("--dry-run", action="store_true", help="say what it would do; send nothing, write nothing")
    ap.add_argument("--grace-min", type=float, default=15.0, help="leave postings younger than this alone (default 15)")
    ap.add_argument("--horizon-days", type=int, default=2, help="only postings dated within this many days of today (default 2)")
    ap.add_argument("--force-armed", action="store_true", help="send even if the Cloud app has not written the ledger yet")
    ap.add_argument("--to", default=None, help="override the To: address (default kpostin@jpsi.com)")
    ap.add_argument("--no-bcc", action="store_true", help="do not BCC the JSA group (a test that emails only --to)")
    ap.add_argument("--allow-sqlite", action="store_true", help="tests only: accept a local SQLite database")
    ap.add_argument("--baseline-iso", default=None, help="with --dry-run only: pretend the baseline was this UTC timestamp (to preview real postings)")
    ap.add_argument("--state-dir", default=None, help="where the state file / kill switch live (default ./state)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args()))
