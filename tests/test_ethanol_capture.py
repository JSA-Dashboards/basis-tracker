"""The ethanol (Platts) futures capture: contract months, rows, storage, and the Nightly Recap proxy.

    python tests/test_ethanol_capture.py

Runs against a throwaway local SQLite file; Massive is faked, so nothing touches the network or Snowflake.
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                  # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="eth_test_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
import ethanol_capture as ec           # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def count(where="1=1"):
    conn = db.get_conn(); c = conn.cursor()
    try:
        c.execute("SELECT COUNT(*) AS n FROM ethanol_futures WHERE " + where)
        return dict(c.fetchone())["n"]
    except Exception:
        return None
    finally:
        conn.close()


print("contract_ym: the one-digit year comes from the settlement date")
check("CUV6 settles 2026-11-03 -> Oct 2026", ec.contract_ym("CUV6", "2026-11-03") == "2026-10")
check("CUZ6 settles 2027-01-05 -> Dec 2026 (year-end rollover)", ec.contract_ym("CUZ6", "2027-01-05") == "2026-12")
check("CUF7 settles 2027-02-02 -> Jan 2027", ec.contract_ym("CUF7", "2027-02-02") == "2027-01")
check("a 3-letter product works too: AEZX6 -> Nov 2026", ec.contract_ym("AEZX6", "2026-12-02") == "2026-11")
check("no date / short ticker / bad month letter -> None",
      ec.contract_ym("CUV6", None) is None and ec.contract_ym("CU", "2026-11-03") is None
      and ec.contract_ym("CUA6", "2026-11-03") is None)
check("a year digit that disagrees with the date is not guessed", ec.contract_ym("CUV9", "2026-11-03") is None)
check("iso_from_ns: Massive's nanosecond epochs", ec.iso_from_ns(1790792018885855057) == "2026-09-30T18:13:38Z"
      and ec.iso_from_ns(None) is None and ec.iso_from_ns("x") is None)

print("build_rows: a contract with nothing to say gets no row")
CONS = [{"ticker": "CUV6", "settlement_date": "2026-11-03"}, {"ticker": "CUX6", "settlement_date": "2026-12-02"},
        {"ticker": "CUZ6", "settlement_date": "2027-01-05"}, {"ticker": "CUF7", "settlement_date": "2027-02-02"}]
SNAPS = {
    "CUV6": {"session": {"settlement_price": 2.14, "previous_settlement": 2.14, "volume": 2},
             "last_trade": {"price": 2.14, "last_updated": 1790792018885855057}},
    "CUX6": {"session": {}, "last_trade": {"price": 2.10, "last_updated": 1790348670447376159}},   # traded, no settle
    "CUZ6": {"session": {"settlement_price": 1.93, "previous_settlement": 1.9325, "volume": 1}, "last_trade": {}},
    "CUF7": {"session": {}, "last_trade": {}},                                                      # nothing at all
}
rows = ec.build_rows("CU", CONS, SNAPS, date(2026, 10, 2), "2026-10-02T21:40:00Z")
by = {r["ticker"]: r for r in rows}
check("3 of 4 contracts have something; the empty one is skipped", sorted(by) == ["CUV6", "CUX6", "CUZ6"], sorted(by))
check("CUV6 row: settle, prev, volume, last trade, contract month, expiry",
      by["CUV6"]["settle"] == 2.14 and by["CUV6"]["prev_settle"] == 2.14 and by["CUV6"]["volume"] == 2
      and by["CUV6"]["contract_ym"] == "2026-10" and by["CUV6"]["expiry"] == "2026-11-03"
      and by["CUV6"]["last_trade_at"] == "2026-09-30T18:13:38Z", by["CUV6"])
check("a traded-but-unsettled contract keeps its last price, settle None", by["CUX6"]["settle"] is None and by["CUX6"]["last_price"] == 2.10)
check("a settled-but-untraded contract has no last-trade time", by["CUZ6"]["last_trade_at"] is None and by["CUZ6"]["settle"] == 1.93)
check("a listed contract Massive has no data for (AEZ today) yields no rows",
      ec.build_rows("AEZ", [{"ticker": "AEZV6", "settlement_date": "2026-11-03"}], {"AEZV6": {"session": {}, "last_trade": {}}},
                    date(2026, 10, 2), "x") == [] and ec.build_rows("AEZ", [{"ticker": "AEZV6", "settlement_date": "2026-11-03"}], {}, date(2026, 10, 2), "x") == [])

print("storage: a day is replaced wholesale, so re-runs never duplicate")
check("first save writes 3 rows", ec.save(rows, "CU") == 3 and count() == 3)
rows2 = [dict(r, settle=(2.20 if r["ticker"] == "CUV6" else r["settle"])) for r in rows]
ec.save(rows2, "CU")
check("saving the same day again replaces, not adds (still 3 rows)", count() == 3)
check("...and carries the corrected value", count("ticker='CUV6' AND settle=2.2") == 1)
check("a different capture date adds rows", ec.save([dict(r, asof_date="2026-10-05") for r in rows], "CU") == 3 and count() == 6)
check("an empty save writes nothing and does not fail", ec.save([], "CU") == 0)

print("proxy: the current-month contract of the latest capture on/before the as-of date")
conn = db.get_conn(); c = conn.cursor(); c.execute("DELETE FROM ethanol_futures"); conn.commit(); conn.close()


def seed(asof, vals):
    out = []
    for ticker, ym, settle in vals:
        out.append({"asof_date": asof, "product_code": "CU", "ticker": ticker, "contract_ym": ym, "expiry": None,
                    "settle": settle, "prev_settle": settle - 0.01, "volume": 2, "last_price": settle,
                    "last_trade_at": "2026-09-30T18:13:38Z", "captured_at": "x"})
    ec.save(out, "CU")


seed("2026-10-02", [("CUV6", "2026-10", 2.14), ("CUX6", "2026-11", 2.10), ("CUZ6", "2026-12", 1.93)])
seed("2026-11-02", [("CUV6", "2026-10", 2.20), ("CUX6", "2026-11", 2.25), ("CUZ6", "2026-12", 2.00)])
p = ec.proxy("CU", date(2026, 10, 4))
check("Sunday Oct 4 -> Friday's capture, October contract CUV6 at 2.14",
      p and p.ticker == "CUV6" and p.settle == 2.14 and p.captured_for == date(2026, 10, 2) and p.volume == 2, p)
check("the proxy says when the contract last traded", p and p.last_trade_at == "2026-09-30T18:13:38Z")
check("4 days after the capture is still fresh", ec.proxy("CU", date(2026, 10, 6)) is not None)
check("7 days after the capture is stale -> None (the box stays manual)", ec.proxy("CU", date(2026, 10, 9)) is None)
check("the custom age limit is honoured", ec.proxy("CU", date(2026, 10, 9), max_age_days=10) is not None)
p2 = ec.proxy("CU", date(2026, 11, 2))
check("Nov 2 rolls to the NOVEMBER contract CUX6 (2.25), not October's", p2 and p2.ticker == "CUX6" and p2.settle == 2.25, p2)
check("before the first capture -> None", ec.proxy("CU", date(2026, 9, 30)) is None)
check("a product with no rows (NY / AEZ) -> None", ec.proxy("AEZ", date(2026, 10, 4)) is None)
seed("2026-10-30", [("CUX6", "2026-11", 2.30), ("CUZ6", "2026-12", 2.05)])         # October contract no longer listed
p3 = ec.proxy("CU", date(2026, 10, 30))
check("no current-month contract on that day -> the nearest LATER one", p3 and p3.ticker == "CUX6", p3)

db.DB_PATH = Path(tempfile.mkdtemp(prefix="eth_empty_")) / "empty.db"
check("a database with no table yet -> None, no crash (the Nightly Recap works before the first capture)",
      ec.proxy("CU", date(2026, 10, 4)) is None)
db.DB_PATH = Path(tempfile.mkdtemp(prefix="eth_cli_")) / "cli.db"

print("main(): the safety rules, with Massive faked")
CALLS = []


def fake_paged(path, **params):
    CALLS.append(("paged", params.get("product_code")))
    return CONS if params.get("product_code") == "CU" else [{"ticker": "AEZV6", "settlement_date": "2026-11-03"}]


def fake_snaps(tickers, batch=20):
    CALLS.append(("snap", tuple(tickers)))
    return SNAPS if "CUV6" in tickers else {}


_p, _s = ec._paged, ec.fetch_snapshots
ec._paged, ec.fetch_snapshots = fake_paged, fake_snaps


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ec.main(argv)
    return rc, buf.getvalue()


try:
    CALLS.clear()
    rc, out = run(["--date", "2026-10-03"])                         # a Saturday
    check("a weekend: exits 0 without touching the network", rc == 0 and not CALLS and "weekend" in out, (rc, CALLS))
    rc, out = run(["--date", "2026-10-02"])
    check("writing without Snowflake is REFUSED (rc 2) — it would be lost in a local file", rc == 2 and count() in (None, 0), (rc, out[:80]))
    rc, out = run(["--date", "2026-10-02", "--dry-run"])
    check("--dry-run fetches and prints but writes nothing", rc == 0 and "DRY RUN" in out and count() in (None, 0), out[-120:])
    rc, out = run(["--date", "2026-10-02", "--allow-sqlite"])
    check("a real run saves the CU rows (3) and reports NY as empty without failing", rc == 0 and count("product_code='CU'") == 3
          and "AEZ" in out and "0 with data" in out, (rc, count(), out[-200:]))
    rc, out = run(["--date", "2026-10-02", "--allow-sqlite"])
    check("running it twice leaves the same 3 rows", rc == 0 and count("product_code='CU'") == 3)
    ec._paged = lambda path, **p: ([] if p.get("product_code") == "CU" else [])
    rc, out = run(["--date", "2026-10-02", "--dry-run"])
    check("CU returning nothing is an ERROR (rc 1) so cron-alert emails", rc == 1 and "ERROR" in out, (rc, out[-100:]))
finally:
    ec._paged, ec.fetch_snapshots = _p, _s

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
