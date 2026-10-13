"""repair_futures_dating.py — one-off correction of the old daily futures capture (2026-06-22 .. 2026-10-12).

What audit_futures_dating.py found (2026-10-12):
- From 2026-08-31 (Massive's flat file became the primary source) to 2026-10-09, every daily row held the
  settlement of the trading day BEFORE its date (29 dates, all of them).
- Before that (ADM's live quote, 2026-06-22 .. 08-27) the rows are their own date's settlement, except
  06-22 (a 9:51 PM run: overnight trade), 07-17 and 08-17 (morning runs: pre-open quotes) and two runs dated
  a Saturday (07-11, 08-29) that hold Friday's settlement.
- Trading days with no row at all: missed or failed runs, and Columbus Day 10-12 (the import skipped it).

The fix (futures_capture.py) only writes from 2026-10-13. This rebuilds the bad dates from Massive's 1session
bars — each date gets exactly its own session's settlement for every tracked contract that traded — deletes the
Saturday rows, and fills the missing trading days. The ADM-era dates the audit found right are left alone, and
so are the backfilled rows (before 2026-06-22), which the audit found right.

    python repair_futures_dating.py                       # DRY RUN: print the plan, write nothing
    python repair_futures_dating.py --apply --backup <f>  # write it (after a backup CSV of every row it touches)
    python repair_futures_dating.py --restore <f>         # undo: put the backed-up rows back exactly

Rows it writes carry captured_at 'repair:massive-settle@<UTC time>'. Run it outside the 3:45 PM import.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

import database as db                  # noqa: E402
import grain_calendar                  # noqa: E402
import massive_futures as mf           # noqa: E402

FIRST = date(2026, 6, 22)              # the first day of the old daily capture
LAST = date(2026, 10, 12)              # the day before futures_capture.FIRST_DAY
REBUILD_ALWAYS = {"2026-06-22", "2026-07-17", "2026-08-17"}   # ADM-era rows that are not the settlement
LATE_FROM = date(2026, 8, 31)          # from here every old row is a day late
TOL = 0.005


def is_old_capture(cap) -> bool:
    s = str(cap or "")
    return not s.startswith(("backfill:", "massive:", "adm:", "repair:"))


def load_bars(symbols: set[str]) -> dict[str, dict[str, float]]:
    """{symbol: {session date iso: settlement}} for every contract that traded in the window."""
    key = mf._api_key()
    from concurrent.futures import ThreadPoolExecutor
    def one(sym):
        out = {}
        for b in mf.fetch_bars(mf.massive_ticker(sym), key, limit=200):
            d = str(b.get("session_end_date"))[:10]
            if FIRST.isoformat() <= d <= LAST.isoformat() and b.get("settlement_price"):
                out[d] = float(b["settlement_price"])
        return sym, out
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, sorted(s for s in symbols if mf.massive_ticker(s))))


def plan(existing: dict, bars: dict) -> tuple[list, list, dict]:
    """(deletes [(date, symbol)], inserts [(date, symbol, cents)], notes {date: text}). Pure."""
    sessions = sorted({d for b in bars.values() for d in b})
    deletes, inserts, notes = [], [], {}
    all_dates = sorted(set(existing) | set(sessions))
    for d in all_dates:
        if not (FIRST.isoformat() <= d <= LAST.isoformat()):
            continue
        rows = existing.get(d, {})
        old = {s: v for s, v in rows.items() if is_old_capture(v[1])}
        if rows and len(old) != len(rows):
            notes[d] = "skipped: holds rows not written by the old capture"
            continue
        dd = date.fromisoformat(d)
        own = {s: b[d] for s, b in bars.items() if d in b}
        if not grain_calendar.is_trading_day(dd) and not own:
            if old:
                deletes += [(d, s) for s in old]
                notes[d] = f"not a trading day: delete {len(old)} rows (they hold {grain_calendar.prior_trading_day(dd)}'s settlement)"
            continue
        if not own:
            notes[d] = "no Massive settlements for this date: left as is"
            continue
        if old and dd < LATE_FROM and d not in REBUILD_ALWAYS:
            continue                    # ADM era, audited right
        same = sum(1 for s, v in old.items() if s in own and abs(float(v[0]) - own[s]) <= TOL)
        deletes += [(d, s) for s in old]
        inserts += [(d, s, own[s]) for s in sorted(own)]
        gone = sorted(set(old) - set(own))
        notes[d] = (f"rebuild: {len(old)} old rows ({same} already equal) -> {len(own)} settlements"
                    + (f"; {len(gone)} rows with no settlement that day dropped ({', '.join(gone[:6])}{'...' if len(gone) > 6 else ''})" if gone else "")
                    if old else f"fill: no rows -> {len(own)} settlements")
    return deletes, inserts, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild the old daily futures capture from Massive's session bars.")
    ap.add_argument("--apply", action="store_true", help="write the plan (default: dry run)")
    ap.add_argument("--backup", help="CSV to save every row the plan deletes or overwrites (required with --apply)")
    ap.add_argument("--restore", help="put the rows of a backup CSV back exactly, and remove the repair's rows on those dates")
    ap.add_argument("--plan-csv", help="write the planned inserts/deletes to this CSV")
    args = ap.parse_args(argv)
    if not db._use_sf():
        print("ERROR: the backend is not Snowflake (check USE_SNOWFLAKE / .env)")
        return 2

    if args.restore:
        return restore(Path(args.restore))

    existing = db.get_futures_rows(FIRST.isoformat(), LAST.isoformat())
    symbols = {s for rows in existing.values() for s in rows}
    # the contracts of the tracked roots listed during the window (an empty date has no rows to name them)
    for on in (FIRST, date(2026, 8, 31), LAST):
        symbols |= set(mf.list_outrights(on).values())
    bars = load_bars(symbols)
    deletes, inserts, notes = plan(existing, bars)

    kinds = Counter(t.split(":")[0] for t in notes.values())
    print(f"window {FIRST} .. {LAST}: {len(existing)} dates hold rows; {len(notes)} dates in the plan ({dict(kinds)})")
    for d in sorted(notes):
        print(f"  {d} {date.fromisoformat(d):%a}  {notes[d]}")
    print(f"total: delete {len(deletes)} rows, insert {len(inserts)} rows")
    if args.plan_csv:
        with open(args.plan_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["action", "date", "symbol", "old_cents", "new_cents"])
            new = {(d, s): c for d, s, c in inserts}
            for d, s in deletes:
                w.writerow(["replace" if (d, s) in new else "delete", d, s, existing[d][s][0], new.get((d, s), "")])
            for (d, s), c in sorted(new.items()):
                if s not in existing.get(d, {}):
                    w.writerow(["insert", d, s, "", c])
        print("plan written to", args.plan_csv)
    if not args.apply:
        print("DRY RUN: nothing written.")
        return 0
    if not args.backup:
        print("ERROR: --apply needs --backup <csv>")
        return 2

    touched = sorted({d for d, _ in deletes} | {d for d, _, _ in inserts})
    with open(args.backup, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "price_cents", "captured_at"])
        for d in touched:
            for s, (c, cap) in sorted(existing.get(d, {}).items()):
                w.writerow([d, s, c, cap])
    print(f"backup of {sum(len(existing.get(d, {})) for d in touched)} rows on {len(touched)} dates -> {args.backup}")

    stamp = "repair:massive-settle@" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.get_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN")
        for i in range(0, len(deletes), 200):
            part = deletes[i:i + 200]
            c.execute("DELETE FROM futures_prices WHERE " +
                      " OR ".join(["(date = %s AND symbol = %s)"] * len(part)),
                      tuple(x for ds in part for x in ds))
        db._merge_bulk(c, "futures_prices",
                       [("date", "string"), ("symbol", "string"), ("price_cents", "float"), ("captured_at", "string")],
                       [(d, s, float(p), stamp) for d, s, p in inserts], ["date", "symbol"],
                       update_cols=["price_cents", "captured_at"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(f"applied: deleted {len(deletes)}, wrote {len(inserts)} rows tagged {stamp}")
    return 0


def restore(path: Path) -> int:
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    dates = sorted({r["date"] for r in rows})
    conn = db.get_conn()
    c = conn.cursor()
    try:
        c.execute("BEGIN")
        for d in dates:
            c.execute("DELETE FROM futures_prices WHERE date = %s AND captured_at LIKE 'repair:%%'", (d,))
        db._merge_bulk(c, "futures_prices",
                       [("date", "string"), ("symbol", "string"), ("price_cents", "float"), ("captured_at", "string")],
                       [(r["date"], r["symbol"], float(r["price_cents"]), r["captured_at"]) for r in rows],
                       ["date", "symbol"], update_cols=["price_cents", "captured_at"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(f"restored {len(rows)} rows on {len(dates)} dates from {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
