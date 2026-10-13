"""futures_capture.py — the daily futures capture into futures_prices: every CBOT session's settlement filed under
that session's OWN date.

Why (found 2026-10-12): the old capture stored "the latest Massive flat file" under the run's date. Those files
land around 9 PM Central, so at the 3:45 PM run the latest one was YESTERDAY's session, and every daily row from
2026-06-22 to 2026-10-09 held the settlement of the trading day before its date (the run log even said so:
"Massive futures curve: 58 contracts (session 2026-10-08)" then "stored 58 contracts for 2026-10-09").
audit_futures_dating.py measures it; repair_futures_dating.py is the one-off correction.

Now each run reads Massive's 1session bars (`massive_futures.fetch_session_prices`), and every bar names its
session, so a price can only be filed under the day it belongs to:

- A session is taken once it has closed (1:20 PM Central). Its price is the exchange settlement when the bar
  carries one (`captured_at` 'massive:settle@<UTC time>'), else the session's close ('massive:close@...') —
  the settlement can post after the 3:45 PM run; the close is within about a cent of it.
- Every run re-reads the last CATCHUP_DAYS of sessions, so the next run replaces a close with the settlement
  and fills a day that was missed (a failed run, a skipped day). A better source replaces a worse one
  (settle > close > adm), never the reverse.
- It only writes dates it owns: a date already holding a row written another way (the old capture, the
  backfill, a hand repair) is left alone, and it never writes before FIRST_DAY — earlier dates are
  repair_futures_dating.py's.
- Symbols Massive doesn't carry (Minneapolis wheat) come from ADM's live feed, for today only and only between
  the close and the 7 PM reopen, when that feed shows the day's close ('adm:last@...').

Best-effort: auto_import calls `run()` and logs a failure; the scrape never depends on it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
CLOSE = time(13, 20)          # CBOT grains' day session closes 1:20 PM Central
REOPEN = time(19, 0)          # the overnight session (the NEXT day's) opens 7 PM Central
CATCHUP_DAYS = 14             # calendar days of sessions each run re-reads
FIRST_DAY = date(2026, 10, 13)  # the first day this capture writes; earlier dates belong to the repair

RANK = {"adm:last": 0, "massive:close": 1, "massive:settle": 2}
OWN_PREFIXES = ("massive:", "adm:")


def tag(source: str, stamp: str) -> str:
    """captured_at for a row: '<source>@<UTC time>', e.g. 'massive:settle@2026-10-13T20:55:01Z'."""
    return f"{source}@{stamp}"


def source_of(captured_at) -> str | None:
    """The source part of a tag ('massive:settle'), or None for a row this capture did not write."""
    s = str(captured_at or "")
    if not s.startswith(OWN_PREFIXES):
        return None
    return s.split("@", 1)[0]


def session_closed(d: date, now_ct: datetime) -> bool:
    """True once the session that ends on `d` has closed."""
    return d < now_ct.date() or (d == now_ct.date() and now_ct.time() >= CLOSE)


def plan(sessions: dict, existing: dict, now_ct: datetime, adm: dict | None = None, stamp: str = "",
         first_day: date = FIRST_DAY, catchup_days: int = CATCHUP_DAYS) -> tuple[list, dict]:
    """Which rows to write. Pure.

    sessions: {date: {symbol: (cents, official)}} — massive_futures.fetch_session_prices
    existing: {'YYYY-MM-DD': {symbol: (cents, captured_at)}} — database.get_futures_rows over the window
    adm:      {symbol: cents} — ADM's live curve, or None
    Returns (rows [(date_iso, symbol, cents, captured_at)], notes {date_iso: text}) — notes say what was done
    or why a date was left alone.
    """
    today = now_ct.date()
    lo = max(first_day, today - timedelta(days=catchup_days))
    rows, notes = [], {}

    def want(d_iso, sym, cents, source):
        old = existing.get(d_iso, {}).get(sym)
        if old is None:
            return True
        old_src = source_of(old[1])
        if old_src is None:
            return False
        r_new, r_old = RANK[source], RANK.get(old_src, -1)
        return r_new > r_old or (r_new == r_old and abs(float(old[0]) - cents) > 1e-6)

    by_date: dict[date, dict[str, tuple[float, str]]] = {}
    for d, prices in sessions.items():
        if lo <= d <= today and session_closed(d, now_ct):
            by_date[d] = {s: (c, "massive:settle" if official else "massive:close") for s, (c, official) in prices.items()}
    if (adm and today >= lo and CLOSE <= now_ct.time() < REOPEN
            and (today in by_date or _is_trading_day(today))):
        day = by_date.setdefault(today, {})
        for s, c in adm.items():
            day.setdefault(s, (float(c), "adm:last"))

    for d in sorted(by_date):
        d_iso = d.isoformat()
        foreign = [s for s, (_, cap) in existing.get(d_iso, {}).items() if source_of(cap) is None]
        if foreign:
            notes[d_iso] = f"left alone: {len(foreign)} rows were written another way (old capture / backfill)"
            continue
        n_before = len(rows)
        for s, (c, src) in sorted(by_date[d].items()):
            if want(d_iso, s, c, src):
                rows.append((d_iso, s, c, tag(src, stamp)))
        mix: dict[str, int] = {}
        for _, src in by_date[d].values():
            kind = src.split(":")[1]
            mix[kind] = mix.get(kind, 0) + 1
        kinds = ", ".join("%s %d" % kv for kv in sorted(mix.items()))
        notes[d_iso] = "%d written of %d (%s)" % (len(rows) - n_before, len(by_date[d]), kinds)
    return rows, notes


def _is_trading_day(d: date) -> bool:
    import grain_calendar
    return grain_calendar.is_trading_day(d)


def run(now: datetime | None = None, dry_run: bool = False) -> int:
    """Capture what is due (see the module doc). Returns rows written (or that would be, with dry_run)."""
    import database
    import massive_futures

    now_ct = (now or datetime.now(timezone.utc)).astimezone(CT)
    today = now_ct.date()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sessions = massive_futures.fetch_session_prices(today, sessions=10)
    adm = None
    if CLOSE <= now_ct.time() < REOPEN:
        try:
            import adm_futures
            adm = adm_futures.fetch_futures_curve()
        except Exception as exc:
            log.warning("Futures capture: ADM curve failed (%s); Massive only", exc)
    lo = max(FIRST_DAY, today - timedelta(days=CATCHUP_DAYS))
    existing = database.get_futures_rows(lo.isoformat(), today.isoformat())
    rows, notes = plan(sessions, existing, now_ct, adm, stamp)
    for d_iso, text in sorted(notes.items()):
        log.info("Futures capture %s: %s", d_iso, text)
    if not notes:
        log.info("Futures capture: no closed session on or after %s yet", lo)
    if dry_run:
        log.info("Futures capture: DRY RUN, %d rows not written", len(rows))
        return len(rows)
    n = database.save_futures_rows(rows)
    log.info("Futures capture: stored %d rows", n)
    return n


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser(description="Store each CBOT session's settlement under its own date.")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written; write nothing")
    args = ap.parse_args()
    import database as _db
    if not args.dry_run and not _db._use_sf():
        print("futures_capture: ERROR the backend is not Snowflake (check USE_SNOWFLAKE / .env)")
        raise SystemExit(2)
    run(dry_run=args.dry_run)
