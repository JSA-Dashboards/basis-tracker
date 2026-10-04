#!/usr/bin/env python3
"""
ethanol_capture.py — daily capture of the CME Chicago / NY Ethanol (Platts) futures from Massive.

Why: the Nightly Recap's "Chi Platts Eth" / "NY Platts Eth" ($/gal) are typed by hand. Massive
carries the CME contracts that settle against those Platts prices — CU (Chicago) and AEZ (NY) — but
only as FUTURES, thinly traded, and with no history: /aggs has a bar only on days a contract traded,
while /snapshot carries settlement_price + previous_settlement. A daily series therefore has to be
snapshotted forward every weekday, which is what this does:

    python ethanol_capture.py              # capture today's snapshot (cron: deploy/run_ethanol_capture.sh)
    python ethanol_capture.py --dry-run    # fetch and print, write nothing
    python ethanol_capture.py --show       # print the proxy the Nightly Recap would pre-fill

What the number is, and is not: the CURRENT-MONTH contract's settle (CUV6 in October) is the market's
mark for that month's AVERAGE Platts Chicago price — a fair proxy for the level, NOT the day's Platts
assessment. The Nightly Recap labels it as the futures settle and the box stays editable. NY (AEZ, and
NIE) is LISTED on Massive but has no settlements or trades at all (checked 2026-10-04), so it captures
nothing until that changes; the same code picks it up the day it does.

Rows are keyed (asof_date, product, ticker) and a day is replaced wholesale, so re-runs are safe. The
snapshot is always the CURRENT state: `asof_date` is the capture date, and `last_trade_at` records when
each contract last actually traded so a stale mark is visible (the front month can go days without a
print). There is no backfill.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Cron has no environment; load the repo's .env by absolute path before database reads it.
load_dotenv(Path(__file__).with_name(".env"))

import database as db            # noqa: E402

BASE = "https://api.massive.com/futures/v1"
PRODUCTS = {"CU": "Chicago Ethanol (Platts)", "AEZ": "NY Ethanol (Platts)"}
TABLE = "ethanol_futures"
MONTH_CODES = "FGHJKMNQUVXZ"         # Jan .. Dec
STALE_DAYS = 5                       # don't pre-fill from a capture older than this

DDL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    asof_date     TEXT NOT NULL,
    product_code  TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    contract_ym   TEXT,
    expiry        TEXT,
    settle        REAL,
    prev_settle   REAL,
    volume        INTEGER,
    last_price    REAL,
    last_trade_at TEXT,
    captured_at   TEXT,
    PRIMARY KEY (asof_date, product_code, ticker)
)"""
COLS = ["asof_date", "product_code", "ticker", "contract_ym", "expiry", "settle", "prev_settle",
        "volume", "last_price", "last_trade_at", "captured_at"]


# ── helpers ───────────────────────────────────────────────────────────────────

def today_ct() -> date:
    """Today's date in Central time (the droplet clock is local, Cloud's is UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=6)).date()


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def iso_from_ns(ns) -> str | None:
    """Massive timestamps are nanosecond epochs."""
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def contract_ym(ticker: str, settlement_date: str | None) -> str | None:
    """'YYYY-MM' of the contract month: CUV6 + settles 2026-11-03 -> 2026-10. The month letter is
    second-to-last and the year is one digit (decade-ambiguous on its own), so the year comes from
    the settlement date, which falls in the month AFTER the contract month (Dec -> next January)."""
    if not ticker or len(ticker) < 3 or not settlement_date:
        return None
    cm = MONTH_CODES.find(ticker[-2]) + 1
    if cm == 0 or not ticker[-1].isdigit():
        return None
    try:
        ex = date.fromisoformat(str(settlement_date)[:10])
    except ValueError:
        return None
    year = ex.year if ex.month > cm else ex.year - 1
    if year % 10 != int(ticker[-1]):          # inconsistent -> don't guess
        return None
    return f"{year:04d}-{cm:02d}"


# ── Massive ───────────────────────────────────────────────────────────────────

def _get(path: str | None = None, url: str | None = None, **params) -> dict:
    key = os.getenv("MASSIVE_API_KEY")
    if not key:
        raise RuntimeError("MASSIVE_API_KEY is not set (looked in the environment and the repo .env)")
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url or (BASE + path), headers={"Authorization": "Bearer " + key},
                             params=None if url else params, timeout=40)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError("Massive %s -> HTTP %s: %s" % (path or url, r.status_code, r.text[:200]))
            if r.status_code < 500 and r.status_code != 429:
                break                           # a 4xx is not going to heal by retrying
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise last


def _paged(path: str, **params) -> list[dict]:
    js = _get(path, **params)
    out = list(js.get("results", []))
    pages = 1
    while js.get("next_url") and pages < 10:
        js = _get(url=js["next_url"])
        out += js.get("results", [])
        pages += 1
    return out


def list_outrights(product: str, today: date) -> list[dict]:
    """The product's single-month outright contracts today, nearest expiry first. (The endpoint also
    lists calendar spreads and strips, and repeats a contract's record per date.)"""
    rx = re.compile(re.escape(product) + "[" + MONTH_CODES + r"]\d")
    seen = {}
    for c in _paged("/contracts", product_code=product, date=today.isoformat(), limit=1000):
        if rx.fullmatch(str(c.get("ticker", ""))):
            seen[c["ticker"]] = c
    return sorted(seen.values(), key=lambda c: str(c.get("settlement_date") or ""))


def fetch_snapshots(tickers: list[str], batch: int = 20) -> dict[str, dict]:
    out = {}
    for i in range(0, len(tickers), batch):
        js = _get("/snapshot", **{"ticker.any_of": ",".join(tickers[i:i + batch])})
        for s in js.get("results", []):
            t = (s.get("details") or {}).get("ticker")
            if t:
                out[t] = s
    return out


def build_rows(product: str, contracts: list[dict], snaps: dict[str, dict], asof: date,
               captured_at: str) -> list[dict]:
    """One row per contract that has ANYTHING to say (a settlement or a trade). A listed contract
    with no data at all — AEZ / NIE today — yields no row rather than a row of nulls."""
    rows = []
    for c in contracts:
        t = c["ticker"]
        s = snaps.get(t)
        if not s:
            continue
        se, lt = s.get("session") or {}, s.get("last_trade") or {}
        settle, last = _f(se.get("settlement_price")), _f(lt.get("price"))
        if settle is None and last is None:
            continue
        rows.append({
            "asof_date": asof.isoformat(), "product_code": product, "ticker": t,
            "contract_ym": contract_ym(t, c.get("settlement_date")), "expiry": c.get("settlement_date"),
            "settle": settle, "prev_settle": _f(se.get("previous_settlement")),
            "volume": _i(se.get("volume")), "last_price": last,
            "last_trade_at": iso_from_ns(lt.get("last_updated")), "captured_at": captured_at,
        })
    return rows


# ── storage ───────────────────────────────────────────────────────────────────

def save(rows: list[dict], product: str) -> int:
    """Replace this product's rows for the capture date. Returns rows written."""
    if not rows:
        return 0
    conn = db.get_conn()
    c = conn.cursor()
    ph = db._ph()
    try:
        c.execute(DDL)
        c.execute(f"DELETE FROM {TABLE} WHERE asof_date={ph} AND product_code={ph}",
                  (rows[0]["asof_date"], product))
        sql = f"INSERT INTO {TABLE} ({','.join(COLS)}) VALUES ({','.join([ph] * len(COLS))})"
        c.executemany(sql, [tuple(r[k] for k in COLS) for r in rows])
        conn.commit()
        return len(rows)
    finally:
        conn.close()


@dataclass
class Proxy:
    product: str
    ticker: str            # e.g. CUV6
    contract_ym: str       # '2026-10'
    settle: float          # $/gal
    prev_settle: float | None
    volume: int | None     # lots in the contract's last active session
    last_trade_at: str | None
    captured_for: date     # the capture date this came from


def _pick(rows: list[dict], cap: date) -> dict | None:
    """The current-month contract of the capture date, else the nearest later one."""
    cur = cap.strftime("%Y-%m")
    rows = sorted((r for r in rows if r.get("contract_ym")), key=lambda r: r["contract_ym"])
    for r in rows:
        if r["contract_ym"] >= cur:
            return r
    return None


def proxy(product: str, as_of: date, max_age_days: int = STALE_DAYS) -> Proxy | None:
    """The settle to pre-fill for `as_of`: the latest capture on/before it (not older than
    `max_age_days`), taken from that day's current-month contract. None when there is nothing
    usable — including when the table does not exist yet — so the caller's box just stays manual."""
    ph = db._ph()
    try:
        conn = db.get_conn()
        c = conn.cursor()
        try:
            c.execute(f"SELECT MAX(asof_date) AS d FROM {TABLE} "
                      f"WHERE product_code={ph} AND asof_date<={ph} AND settle IS NOT NULL",
                      (product, as_of.isoformat()))
            r = c.fetchone()
            d = dict(r)["d"] if r else None
            if not d:
                return None
            cap = date.fromisoformat(str(d)[:10])
            if (as_of - cap).days > max_age_days:
                return None
            c.execute(f"SELECT ticker, contract_ym, settle, prev_settle, volume, last_trade_at FROM {TABLE} "
                      f"WHERE product_code={ph} AND asof_date={ph} AND settle IS NOT NULL ORDER BY contract_ym",
                      (product, cap.isoformat()))
            rows = [dict(x) for x in c.fetchall()]
        finally:
            conn.close()
    except Exception:
        return None
    p = _pick(rows, cap)
    if not p:
        return None
    return Proxy(product, p["ticker"], p["contract_ym"], float(p["settle"]), _f(p.get("prev_settle")),
                 _i(p.get("volume")), p.get("last_trade_at"), cap)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture the CME ethanol (Platts) futures from Massive.")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print; write nothing")
    ap.add_argument("--show", action="store_true", help="print the Nightly Recap proxy and exit")
    ap.add_argument("--date", help="label the capture YYYY-MM-DD. The snapshot is always the CURRENT "
                                   "state — use this only to file the last session over a weekend")
    ap.add_argument("--allow-sqlite", action="store_true",
                    help="permit writing to the local SQLite fallback (testing only)")
    args = ap.parse_args(argv)

    asof = date.fromisoformat(args.date) if args.date else today_ct()
    if args.show:
        for p in PRODUCTS:
            px = proxy(p, asof)
            print("%-4s %s" % (p, ("%s %s settle %.4f (prev %s, %s lots, last traded %s, captured %s)" % (
                px.ticker, px.contract_ym, px.settle, px.prev_settle, px.volume, px.last_trade_at,
                px.captured_for)) if px else "nothing usable on/before %s" % asof))
        return 0

    if asof.weekday() >= 5:
        print("ethanol_capture: %s is a weekend — the market is closed, nothing new to capture "
              "(use --date to file Friday's session)" % asof)
        return 0
    if not args.dry_run and not db._use_sf() and not args.allow_sqlite:
        print("ethanol_capture: ERROR the database backend is not Snowflake, so this would write to a "
              "local SQLite file and be lost — check USE_SNOWFLAKE / the .env (or pass --allow-sqlite)")
        return 2

    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = 0
    for product, label in PRODUCTS.items():
        contracts = list_outrights(product, asof)
        snaps = fetch_snapshots([c["ticker"] for c in contracts])
        rows = build_rows(product, contracts, snaps, asof, captured_at)
        print("ethanol_capture: %s %-26s %d contracts listed, %d with data" % (product, label, len(contracts), len(rows)))
        for r in rows[:4]:
            print("    %-7s %s settle %-8s prev %-8s vol %-4s last trade %s" % (
                r["ticker"], r["contract_ym"], r["settle"], r["prev_settle"], r["volume"], r["last_trade_at"]))
        if not rows:
            # Chicago must have data (it does every day); NY has none today, so empty is not an error.
            if product == "CU":
                print("ethanol_capture: ERROR no CU contract returned a settlement or trade")
                status = 1
            continue
        if args.dry_run:
            continue
        print("ethanol_capture: %s saved %d rows for %s" % (product, save(rows, product), asof))
    if args.dry_run:
        print("ethanol_capture: DRY RUN — nothing written.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
