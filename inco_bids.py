#!/usr/bin/env python3
"""
inco_bids.py — Incobrasa (Gilman, IL) soybean-processor bids: carry-forward + posting.

Inco does NOT post daily. They send a sheet and it HOLDS until the next one (Kolten,
2026-09-24), so the bids archive needs a snapshot for every business day. Two commands:

  carry   Fill every missing business day (Mon-Fri) after the latest snapshot, up to
          today (Central), by repeating that sheet's bids. Idempotent — it never
          touches a day that already has a snapshot. Writes by default (it is the cron
          step in deploy/run_daily.sh); --dry-run only prints the plan.

  post    Record a NEW sheet for a date. That day is REPLACED outright (a carried
          snapshot may already be sitting there, and snapshot rows merge in place, so
          writing over it would leave stale rows), and the days after it are
          re-carried from the new sheet up to the next real sheet / today. Previews
          by default — nothing is written without --commit.

              printf 'FH Oct 2026, ZSX26, 20\\nLH Oct 2026, ZSX26, 0\\n' | \\
                  python inco_bids.py post --date 2026-10-02            # preview
              ... | python inco_bids.py post --date 2026-10-02 --commit  # write

          Sheet lines are  LABEL, FUTURES, BASIS  (basis in cents; "flat" = 0).

Rules this encodes (each one bit us once):
  * Labels MUST carry a month name. delivery_period.canonical() reads the first month
    name in a label and otherwise falls back to the futures contract's month, so a bare
    "9/21-9/26" on ZSX26 is read as NOVEMBER and lands on top of the real Nov row.
    Write "Sep 21-26", "FH Oct 2026", "Nov 2026".
  * An expired delivery window is dropped when carrying (a past month, or a date window
    like "Sep 21-27" once its last day has passed) — same convention as
    database._drop_stale_forward_rows / changes_report._curve_map; otherwise it becomes
    a phantom nearest month.
  * Carried snapshots are marked in snapshots.email_subject ("carried forward from
    YYYY-MM-DD sheet") so a real sheet can be told from a carried day.
  * Never carry a sheet older than --max-age days (default 21): a forgotten sheet should
    leave a visible gap, not weeks of identical bids.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Cron has no environment; load the repo's .env before database reads it (same as
# rail_report.py). No-op if the vars are already set.
load_dotenv(Path(__file__).with_name(".env"))

import database as db            # noqa: E402
import delivery_period as dp     # noqa: E402

PROV, LOC, SRC = "INCO", "Gilman, IL", "manual"
GRAIN = "Soybeans"
CARRY_PREFIX = "carried forward from "
MAX_AGE_DAYS = 21

_MON = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
_WIN = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2})-(\d{1,2})$")          # "Sep 21-27"
_HAS_MONTH = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
_FUT = re.compile(r"^ZS[FGHJKMNQUVXZ]\d{2}$")


# ── dates ─────────────────────────────────────────────────────────────────────

def today_ct() -> date:
    """Today's date in Central time (the droplet clock is UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=6)).date()


def business_days(after: date, through: date):
    """Mon-Fri dates in (after, through]."""
    d = after + timedelta(days=1)
    while d <= through:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


# ── database reads ────────────────────────────────────────────────────────────

def _q(sql: str, params=()) -> list[dict]:
    conn = db.get_conn()
    c = conn.cursor()
    try:
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def load_snaps() -> list[dict]:
    """Every INCO / Gilman snapshot (all sources), oldest first, with a parsed date."""
    ph = db._ph()
    rows = _q(f"SELECT id, timestamp, source, email_subject FROM snapshots "
              f"WHERE provider={ph} AND location={ph} ORDER BY timestamp", (PROV, LOC))
    for r in rows:
        r["d"] = date.fromisoformat(str(r["timestamp"])[:10])
    return rows


def load_rows(snapshot_id) -> list[dict]:
    ph = db._ph()
    rows = _q(f"SELECT grain, delivery_month, futures_symbol, basis_cents, is_spot, spot_grain "
              f"FROM snapshot_rows WHERE snapshot_id={ph} ORDER BY id", (int(snapshot_id),))
    return [{"grain": r["grain"], "deliveryMonth": r["delivery_month"],
             "futuresSymbol": r["futures_symbol"], "basisCents": r["basis_cents"],
             "isSpot": bool(r["is_spot"]), "spotGrain": r["spot_grain"]} for r in rows]


def is_carried(s: dict) -> bool:
    return str(s.get("email_subject") or "").startswith(CARRY_PREFIX)


# ── building snapshots ────────────────────────────────────────────────────────

def carried_rows(rows: list[dict], day: date) -> list[dict]:
    """The sheet's rows as they stand on `day`: expired delivery windows dropped."""
    out = []
    for r in rows:
        if not r.get("isSpot"):
            lbl, fut = str(r["deliveryMonth"]), r["futuresSymbol"]
            ym = dp.canonical(lbl, fut)
            if ym and ym < (day.year, day.month):
                continue                                  # a past delivery month
            m = _WIN.match(lbl.strip())
            if m and m.group(1) in _MON:                  # "Sep 21-27": last day passed?
                try:
                    if date(ym[0] if ym else day.year, _MON[m.group(1)], int(m.group(3))) < day:
                        continue
                except ValueError:
                    pass
        out.append(r)
    return out


def make_snap(day: date, rows: list[dict], carried_from: date | None = None) -> dict:
    s = {"timestamp": f"{day.isoformat()}T00:00:00Z", "provider": PROV, "location": LOC,
         "source": SRC,
         "rows": [{**r, "id": f"inco-{i}"} for i, r in enumerate(rows)]}
    if carried_from:
        s["emailSubject"] = f"{CARRY_PREFIX}{carried_from.isoformat()} sheet"
        s["emailDate"] = carried_from.isoformat()
    return s


# ── plans (read-only; apply_plan does the writing) ────────────────────────────

def plan_carry(snaps: list[dict], today: date, max_age: int = MAX_AGE_DAYS) -> dict:
    plan = {"delete": [], "write": [], "notes": []}
    past = [s for s in snaps if s["d"] <= today]
    if not past:
        plan["notes"].append("no INCO snapshots on or before %s — nothing to carry" % today)
        return plan
    base = past[-1]
    real = next((s for s in reversed(past) if not is_carried(s)), base)
    age = (today - real["d"]).days
    if age > max_age:
        plan["notes"].append(
            "STALE: the last real Inco sheet is %s (%d days old, limit %d) — NOT carrying "
            "forward. Post a new sheet, or pass --max-age." % (real["d"], age, max_age))
        return plan
    days = list(business_days(base["d"], today))
    if not days:
        plan["notes"].append("up to date — latest snapshot is %s (today %s)" % (base["d"], today))
        return plan
    rows = load_rows(base["id"])
    if not rows:
        plan["notes"].append("latest snapshot %s has no rows — nothing to carry" % base["d"])
        return plan
    for d in days:
        crow = carried_rows(rows, d)
        if not crow:
            plan["notes"].append("%s: every row has expired — skipped" % d)
            continue
        plan["write"].append(make_snap(d, crow, real["d"]))
    return plan


def plan_post(snaps: list[dict], D: date, sheet: list[dict], today: date,
              max_age: int = MAX_AGE_DAYS) -> dict:
    plan = {"delete": [], "write": [], "notes": []}
    if D > today:
        raise ValueError("--date %s is in the future (today is %s)" % (D, today))
    if D.weekday() >= 5:
        raise ValueError("--date %s is a weekend — date the sheet for the next business day" % D)

    # 1) fill any gap BEFORE D from the latest earlier snapshot (not if it is stale).
    before = [s for s in snaps if s["d"] < D]
    if before:
        base = before[-1]
        real_b = next((s for s in reversed(before) if not is_carried(s)), base)
        if (D - real_b["d"]).days > max_age:
            plan["notes"].append("gap before %s NOT filled: the previous real sheet is %s "
                                 "(%d days earlier, limit %d)" % (D, real_b["d"], (D - real_b["d"]).days, max_age))
        else:
            rows_b = load_rows(base["id"])
            have = {s["d"] for s in snaps}
            for d in business_days(base["d"], D - timedelta(days=1)):
                if d in have:
                    continue
                crow = carried_rows(rows_b, d)
                if crow:
                    plan["write"].append(make_snap(d, crow, real_b["d"]))

    # 2) replace D itself (a carried snapshot may already be there).
    plan["delete"] += [s for s in snaps if s["d"] == D]
    plan["write"].append(make_snap(D, sheet))

    # 3) re-carry the days after D, up to the next real sheet (or today).
    after = [s for s in snaps if s["d"] > D]
    nxt = next((s for s in after if not is_carried(s)), None)
    stop = (nxt["d"] - timedelta(days=1)) if nxt else today
    plan["delete"] += [s for s in after if is_carried(s) and (nxt is None or s["d"] < nxt["d"])]
    for d in business_days(D, stop):
        crow = carried_rows(sheet, d)
        if crow:
            plan["write"].append(make_snap(d, crow, D))
    return plan


def show_plan(plan: dict, verb: str) -> None:
    for n in plan["notes"]:
        print("inco_bids: " + n)
    for s in plan["delete"]:
        kind = "carried" if is_carried(s) else "real"
        print("inco_bids:   %s  delete existing snapshot #%s (%s)" % (s["d"], s["id"], kind))
    for s in plan["write"]:
        d = s["timestamp"][:10]
        how = s.get("emailSubject") or "REAL SHEET"
        print("inco_bids:   %s  %s %d row(s)  [%s]" % (d, verb, len(s["rows"]), how))


def apply_plan(plan: dict) -> bool:
    """Delete, write, then read every written day back. True only if all counts match."""
    expected = {s["timestamp"][:10]: len(s["rows"]) for s in plan["write"]}
    for s in plan["delete"]:
        db.delete_snapshot(int(s["id"]))
    if plan["write"]:
        db.upsert_snapshots(plan["write"])
    if not expected:
        return True
    ph = db._ph()
    got_rows = _q(f"SELECT s.timestamp AS ts, COUNT(r.id) AS n FROM snapshots s "
                  f"JOIN snapshot_rows r ON r.snapshot_id = s.id "
                  f"WHERE s.provider={ph} AND s.location={ph} AND s.timestamp >= {ph} "
                  f"GROUP BY s.timestamp", (PROV, LOC, min(expected) + "T00:00:00Z"))
    got = {str(r["ts"])[:10]: int(r["n"]) for r in got_rows}
    bad = {d: (got.get(d), n) for d, n in expected.items() if got.get(d) != n}
    if bad:
        print("inco_bids: MISMATCH on read-back (day: got, expected): %s" % bad)
        return False
    print("inco_bids: VERIFIED %d day(s) read back with matching row counts" % len(expected))
    return True


# ── sheet parsing ─────────────────────────────────────────────────────────────

def parse_sheet(text: str, D: date) -> tuple[list[dict], list[str]]:
    rows, errs, seen = [], [], set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            errs.append("line %d: expected 'LABEL, FUTURES, BASIS' — got %r" % (n, raw))
            continue
        lbl, fut, b = parts
        fut = fut.upper()
        if not _HAS_MONTH.search(lbl):
            errs.append("line %d: label %r has no month name — a bare date would be read as the "
                        "futures month (use e.g. 'Sep 21-26', 'FH Oct 2026')" % (n, lbl))
            continue
        if not _FUT.match(fut):
            errs.append("line %d: %r is not a soybean contract like ZSX26" % (n, fut))
            continue
        bl = b.lower().replace("+", "")
        try:
            basis = 0 if bl in ("flat", "fl") else int(bl)
        except ValueError:
            errs.append("line %d: basis %r is not a whole number of cents (or 'flat')" % (n, b))
            continue
        ym = dp.canonical(lbl, fut)
        if ym is None:
            errs.append("line %d: cannot tell which delivery month %r is" % (n, lbl))
            continue
        if ym < (D.year, D.month):
            errs.append("line %d: %r is already past on %s" % (n, lbl, D))
            continue
        if lbl.lower() in seen:
            errs.append("line %d: duplicate label %r" % (n, lbl))
            continue
        seen.add(lbl.lower())
        rows.append({"grain": GRAIN, "deliveryMonth": lbl, "futuresSymbol": fut,
                     "basisCents": basis, "isSpot": False, "spotGrain": None})
    if not rows and not errs:
        errs.append("the sheet is empty")
    return rows, errs


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_carry(args) -> int:
    today = date.fromisoformat(args.date) if args.date else today_ct()
    plan = plan_carry(load_snaps(), today, args.max_age)
    show_plan(plan, "carry" if args.dry_run else "write")
    if not plan["write"]:
        return 0
    if args.dry_run:
        print("inco_bids: DRY RUN — nothing written.")
        return 0
    return 0 if apply_plan(plan) else 1


def cmd_post(args) -> int:
    D = date.fromisoformat(args.date)
    today = date.fromisoformat(args.today) if args.today else today_ct()
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    sheet, errs = parse_sheet(text, D)
    if errs:
        for e in errs:
            print("inco_bids: ERROR " + e)
        return 2
    try:
        plan = plan_post(load_snaps(), D, sheet, today, args.max_age)
    except ValueError as e:
        print("inco_bids: ERROR %s" % e)
        return 2
    print("inco_bids: sheet for %s:" % D)
    for r in sheet:
        print("inco_bids:     %-14s %-7s %+d" % (r["deliveryMonth"], r["futuresSymbol"], r["basisCents"]))
    show_plan(plan, "write")
    if not args.commit:
        print("inco_bids: PREVIEW ONLY — nothing written. Re-run with --commit.")
        return 0
    return 0 if apply_plan(plan) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Incobrasa (Gilman, IL) bids: carry-forward + posting.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("carry", help="fill missing business days with the last sheet (cron)")
    pc.add_argument("--date", help="treat this YYYY-MM-DD as today (testing)")
    pc.add_argument("--max-age", type=int, default=MAX_AGE_DAYS)
    pc.add_argument("--dry-run", action="store_true")
    pc.set_defaults(fn=cmd_carry)

    pp = sub.add_parser("post", help="record a new sheet for a date (previews unless --commit)")
    pp.add_argument("--date", required=True, help="sheet date, YYYY-MM-DD (a business day)")
    pp.add_argument("--file", help="read the sheet from this file instead of stdin")
    pp.add_argument("--commit", action="store_true")
    pp.add_argument("--max-age", type=int, default=MAX_AGE_DAYS)
    pp.add_argument("--today", help=argparse.SUPPRESS)       # testing only
    pp.set_defaults(fn=cmd_post)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
