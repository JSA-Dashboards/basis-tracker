"""backfill_futures_history.py — load the corn / soybean / wheat futures history into BASIS_TRACKER.FUTURES_PRICES.

Why: `futures_prices` (one row per date × contract) only started on 2026-06-22, when the daily capture began;
every earlier date had no stored curve, so the Net Carry tab fell back to TODAY'S curve for a past as-of date
(wrong) and the Return to Carry tracker had nothing to rebuild past crop years from. The Cost of Carry app already
archives the settlements in JSA.COST_OF_CARRY:

    FUTURES_HISTORY_ARCHIVE   product_code, month, year, date, price   2006-11-02 .. 2021-12-14   ZC, ZS only
    FUTURES_SETTLEMENTS       product_code, ticker, month, year, date, settle   2021-10-04 .. today  ZC ZS ZW KE ...

This copies them over, server-side (one INSERT ... SELECT per source), for dates BEFORE the first captured day only,
never touching an existing (date, symbol) row, so it is safe to re-run. Symbols are built the way the app spells
them: root + month letter + 2-digit year (ZCH27); price_cents is the archive's cents/bu.

    python backfill_futures_history.py            # dry run: counts only
    python backfill_futures_history.py --apply    # write

Needs a role that can read JSA.COST_OF_CARRY (the local ACCOUNTADMIN key can). Refuses to run unless the backend is
Snowflake (the .env fallback trap: a local SQLite would "succeed" against the wrong database).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(HERE / ".env")
import database as db  # noqa: E402

PRODUCTS = ("ZC", "ZS", "ZW", "KE")


def _select(table: str, price_col: str, lo: str | None, hi: str) -> str:
    """(d, s, px) rows of one archive table with date in [lo, hi), one row per (date, symbol)."""
    prods = ", ".join(f"'{p}'" for p in PRODUCTS)
    lo_sql = f"AND a.date >= '{lo}'" if lo else ""
    return f"""
        SELECT d, s, px FROM (
            SELECT TO_VARCHAR(a.date, 'YYYY-MM-DD') AS d,
                   a.product_code || a.month || RIGHT(TO_VARCHAR(a.year), 2) AS s,
                   a.{price_col} AS px,
                   ROW_NUMBER() OVER (PARTITION BY a.date, a.product_code, a.month, a.year ORDER BY a.{price_col}) AS rn
            FROM JSA.COST_OF_CARRY.{table} a
            WHERE a.product_code IN ({prods}) AND a.{price_col} IS NOT NULL
              AND a.date < '{hi}' {lo_sql}
        ) WHERE rn = 1"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill futures_prices from the Cost of Carry settlement archives.")
    ap.add_argument("--apply", action="store_true", help="write the rows (default: dry run)")
    args = ap.parse_args()

    if not db._use_sf():
        print("REFUSING: the database backend is not Snowflake (check USE_SNOWFLAKE / .env).", file=sys.stderr)
        return 2
    conn = db.get_conn()
    c = conn.cursor()
    try:
        # the boundary is the first day the DAILY CAPTURE wrote (not a backfilled one), so a re-run can still
        # fill gaps inside the backfilled range
        c.execute("SELECT MIN(date) AS d, COUNT(*) AS n FROM futures_prices "
                  "WHERE captured_at IS NULL OR captured_at NOT LIKE 'backfill:%'")
        r = c.fetchone()
        first, before = r["d"], r["n"]
        c.execute("SELECT COUNT(*) AS n FROM futures_prices")
        total = c.fetchone()["n"]
        print(f"futures_prices today: {total:,} rows ({before:,} captured daily, first captured day {first})")
        c.execute("SELECT TO_VARCHAR(MIN(date), 'YYYY-MM-DD') AS d FROM JSA.COST_OF_CARRY.FUTURES_SETTLEMENTS")
        settle_from = c.fetchone()["d"]
        # the two sources do not overlap here: settlements from their first day, the archive before it
        sources = [("FUTURES_SETTLEMENTS", "settle", settle_from, first),
                   ("FUTURES_HISTORY_ARCHIVE", "price", None, min(first, settle_from))]
        for table, col, lo, hi in sources:
            sel = _select(table, col, lo, hi)
            c.execute(f"SELECT COUNT(*) AS n FROM ({sel}) x WHERE NOT EXISTS "
                      f"(SELECT 1 FROM futures_prices f WHERE f.date = x.d AND f.symbol = x.s)")
            n = c.fetchone()["n"]
            print(f"  {table}: {n:,} (date, symbol) rows to add  [{lo or 'start'} .. {hi})")
            if args.apply and n:
                c.execute(f"""INSERT INTO futures_prices (date, symbol, price_cents, captured_at)
                              SELECT x.d, x.s, x.px, 'backfill:{table}' FROM ({sel}) x
                              WHERE NOT EXISTS (SELECT 1 FROM futures_prices f WHERE f.date = x.d AND f.symbol = x.s)""")
                print(f"    inserted {n:,}")
        if args.apply:
            conn.commit()
            c.execute("SELECT MIN(date) AS d, MAX(date) AS m, COUNT(*) AS n, COUNT(DISTINCT date) AS days FROM futures_prices")
            print("after:", dict(c.fetchone()))
        else:
            print("dry run — nothing written (use --apply)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
