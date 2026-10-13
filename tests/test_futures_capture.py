"""The daily futures capture (futures_capture.py): each session's settlement under its OWN date.

    python tests/test_futures_capture.py

The regression this guards (2026-10-12): the old capture stored the latest Massive flat file under the run's
date, and at 3:45 PM that file was yesterday's session. Here Massive is faked with bars like the real
2026-10-08 .. 10-12 ones, and the rows go to a throwaway local SQLite file — nothing touches the network
or Snowflake.
"""
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                  # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="fut_test_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
db.init_db()
import futures_capture as fc           # noqa: E402
import massive_futures as mf           # noqa: E402

FAILS = []
CT = ZoneInfo("America/Chicago")
D = date.fromisoformat


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=CT)


# Real ZCZ6 1session bars (newest first), as /aggs returned them on the night of 2026-10-12.
BARS_ZCZ6 = [
    {"session_end_date": "2026-10-13", "open": 483, "close": 479},                           # still trading
    {"session_end_date": "2026-10-12", "close": 483.5, "settlement_price": 483},
    {"session_end_date": "2026-10-09", "close": 480.5, "settlement_price": 479.75},
    {"session_end_date": "2026-10-08", "close": 499.75, "settlement_price": 500.25},
]

print("massive_futures: tickers, symbols and bar prices")
check("ZCZ26 -> ZCZ6, KEH27 -> KEH7; MW and spreads -> None",
      mf.massive_ticker("ZCZ26") == "ZCZ6" and mf.massive_ticker("KEH27") == "KEH7"
      and mf.massive_ticker("MWZ26") is None and mf.massive_ticker("ZCZ6") is None)
check("a /contracts record gets its year from its last trading day",
      mf.symbol_from_contract({"ticker": "ZCZ6", "last_trade_date": "2026-12-14"}) == "ZCZ26"
      and mf.symbol_from_contract({"ticker": "ZSF7", "last_trade_date": "2027-01-14"}) == "ZSF27"
      and mf.symbol_from_contract({"ticker": "ZCZ0", "last_trade_date": "2030-12-13"}) == "ZCZ30")
check("spreads, butterflies and other roots are not outrights",
      mf.symbol_from_contract({"ticker": "ZCZ6-ZCH7", "last_trade_date": "2026-12-14"}) is None
      and mf.symbol_from_contract({"ticker": "ZC:BF H7-K7-N7", "last_trade_date": "2027-03-12"}) is None
      and mf.symbol_from_contract({"ticker": "ZNZ6", "last_trade_date": "2026-12-14"}) is None)
check("a year digit that disagrees with the dates is refused",
      mf.symbol_from_contract({"ticker": "ZCZ7", "last_trade_date": "2026-12-14"}) is None)
sp = mf.session_prices(BARS_ZCZ6)
check("each bar is filed under its own session_end_date",
      sp[D("2026-10-09")] == (479.75, True) and sp[D("2026-10-08")] == (500.25, True), sp)
check("a bar without a settlement gives its close, marked not official", sp[D("2026-10-13")] == (479.0, False))

SESSIONS = {}
for d, v in sp.items():
    SESSIONS.setdefault(d, {})["ZCZ26"] = v
SESSIONS[D("2026-10-12")]["ZSX26"] = (1292.0, True)

print("plan: what a run writes")
NO_FLOOR = D("2026-01-01")
rows, notes = fc.plan(SESSIONS, {}, at("2026-10-12T15:55:00"), stamp="T", first_day=NO_FLOOR)
got = {(d, s): (c, cap) for d, s, c, cap in rows}
check("THE BUG: 10-12's row holds 10-12's settlement (483), not 10-09's (479.75)",
      got.get(("2026-10-12", "ZCZ26")) == (483, "massive:settle@T"), got.get(("2026-10-12", "ZCZ26")))
check("10-09's row holds 10-09's settlement (479.75), 10-08's holds 500.25",
      got.get(("2026-10-09", "ZCZ26"))[0] == 479.75 and got.get(("2026-10-08", "ZCZ26"))[0] == 500.25)
check("the session still trading (ends 10-13) is not written", not any(d == "2026-10-13" for d, *_ in rows))

rows, _ = fc.plan(SESSIONS, {}, at("2026-10-12T11:00:00"), first_day=NO_FLOOR)
check("before the 1:20 PM close, today's session is not taken (yesterday's still is)",
      not any(d == "2026-10-12" for d, *_ in rows) and any(d == "2026-10-09" for d, *_ in rows))

rows, _ = fc.plan(SESSIONS, {}, at("2026-10-12T21:30:00"), first_day=NO_FLOOR)
check("after the 7 PM reopen, today's session is still filed under today, and tomorrow's is not",
      ("2026-10-12", "ZCZ26", 483, "massive:settle@") in [(d, s, c, cap) for d, s, c, cap in rows]
      and not any(d == "2026-10-13" for d, *_ in rows))

PROV = {D("2026-10-12"): {"ZCZ26": (483.5, False)}}
rows, _ = fc.plan(PROV, {}, at("2026-10-12T15:50:00"), stamp="A", first_day=NO_FLOOR)
check("settlement not out yet at 3:50 PM: the close is stored, tagged massive:close",
      rows == [("2026-10-12", "ZCZ26", 483.5, "massive:close@A")], rows)
existing = {"2026-10-12": {"ZCZ26": (483.5, "massive:close@A")}}
rows, _ = fc.plan(SESSIONS, existing, at("2026-10-13T15:50:00"), stamp="B", first_day=NO_FLOOR)
check("the next run replaces that close with the settlement",
      ("2026-10-12", "ZCZ26", 483, "massive:settle@B") in rows, rows)
existing = {"2026-10-12": {"ZCZ26": (483.0, "massive:settle@B"), "ZSX26": (1292.0, "massive:settle@B")}}
rows, _ = fc.plan(SESSIONS, existing, at("2026-10-13T15:50:00"), stamp="C", first_day=NO_FLOOR)
check("an unchanged settlement is not rewritten", not any(d == "2026-10-12" for d, *_ in rows), rows)
rows, _ = fc.plan(PROV, existing, at("2026-10-13T15:50:00"), first_day=NO_FLOOR)
check("a close never replaces a stored settlement", rows == [], rows)

old = {"2026-10-09": {"ZCZ26": (500.25, "2026-10-09T20:56:15.123+00:00")}}
rows, notes = fc.plan(SESSIONS, old, at("2026-10-12T15:55:00"), first_day=NO_FLOOR)
check("a date holding rows from the old capture is left alone (the repair's job)",
      not any(d == "2026-10-09" for d, *_ in rows) and notes["2026-10-09"].startswith("left alone"), notes)
bf = {"2026-10-08": {"ZSX26": (1300.0, "backfill:FUTURES_SETTLEMENTS")}}
rows, _ = fc.plan(SESSIONS, bf, at("2026-10-12T15:55:00"), first_day=NO_FLOOR)
check("so is a date holding backfilled rows", not any(d == "2026-10-08" for d, *_ in rows))

rows, _ = fc.plan(SESSIONS, {}, at("2026-10-12T15:55:00"))
check("nothing before FIRST_DAY (%s) is ever written" % fc.FIRST_DAY,
      all(D(d) >= fc.FIRST_DAY for d, *_ in rows) and rows == [], rows)
rows, _ = fc.plan(SESSIONS, {}, at("2026-10-28T15:55:00"), first_day=NO_FLOOR)
check("a session older than CATCHUP_DAYS (14) is not re-read: on 10-28 nothing up to 10-13", rows == [], rows)
rows, _ = fc.plan(SESSIONS, {}, at("2026-10-27T15:55:00"), first_day=NO_FLOOR)
check("on 10-27 the window still reaches 10-13 (its close; that fixture bar never settled)",
      [r[:3] for r in rows] == [("2026-10-13", "ZCZ26", 479.0)], rows)

print("plan: ADM fills only what Massive lacks, today, between the close and the reopen")
ADM = {"MWZ26": 612.25, "ZCZ26": 483.25}
rows, _ = fc.plan(SESSIONS, {}, at("2026-10-12T15:55:00"), adm=ADM, stamp="T", first_day=NO_FLOOR)
got = {(d, s): (c, cap) for d, s, c, cap in rows}
check("MW comes from ADM, tagged adm:last", got.get(("2026-10-12", "MWZ26")) == (612.25, "adm:last@T"))
check("Massive's ZCZ26 settlement wins over ADM's quote", got.get(("2026-10-12", "ZCZ26")) == (483, "massive:settle@T"))
check("ADM is never filed under an earlier date", not any(s == "MWZ26" and d != "2026-10-12" for d, s, *_ in rows))
rows, _ = fc.plan(SESSIONS, {}, at("2026-10-12T19:30:00"), adm=ADM, first_day=NO_FLOOR)
check("after 7 PM ADM shows the next session: not used", not any(s == "MWZ26" for _, s, *_ in rows))
rows, _ = fc.plan({}, {}, at("2026-10-10T15:55:00"), adm=ADM, first_day=NO_FLOOR)
check("not on a Saturday", rows == [])
rows, _ = fc.plan({}, {}, at("2026-11-26T15:55:00"), adm=ADM, first_day=NO_FLOOR)
check("not on Thanksgiving (grain holiday)", rows == [])
rows, _ = fc.plan({}, {}, at("2026-11-11T15:55:00"), adm=ADM, first_day=NO_FLOOR)
check("but yes on Veterans Day (grains trade)", ("2026-11-11", "MWZ26", 612.25) in [r[:3] for r in rows])

print("run(): fake Massive + ADM, real (SQLite) storage")
fake_sessions = {D("2026-10-13"): {"ZCZ26": (481.0, False), "ZSX26": (1280.0, True)},
                 D("2026-10-14"): {"ZCZ26": (478.0, False)}}           # in progress at 15:55 on 10-13
mf.fetch_session_prices = lambda on, sessions=10: fake_sessions
import adm_futures                     # noqa: E402
adm_futures.fetch_futures_curve = lambda: {"MWZ26": 610.0}
n = fc.run(now=at("2026-10-13T15:55:00"))
conn = db.get_conn(); c = conn.cursor()
c.execute("SELECT date, symbol, price_cents, captured_at FROM futures_prices ORDER BY date, symbol")
stored = [tuple(r) for r in c.fetchall()]; conn.close()
check("three rows for 10-13 (close, settle, ADM), none for the unfinished 10-14", n == 3 and
      [(d, s, p) for d, s, p, _ in stored] == [("2026-10-13", "MWZ26", 610.0), ("2026-10-13", "ZCZ26", 481.0),
                                               ("2026-10-13", "ZSX26", 1280.0)], stored)
check("each tagged with its source", [cap.split("@")[0] for *_, cap in stored] ==
      ["adm:last", "massive:close", "massive:settle"], stored)
fake_sessions[D("2026-10-13")]["ZCZ26"] = (481.25, True)
n = fc.run(now=at("2026-10-14T15:55:00"))
conn = db.get_conn(); c = conn.cursor()
c.execute("SELECT price_cents, captured_at FROM futures_prices WHERE date='2026-10-13' AND symbol='ZCZ26'")
r = c.fetchone(); conn.close()
check("the next day's run upgrades 10-13's close to its settlement", r[0] == 481.25 and r[1].startswith("massive:settle@"), tuple(r))

print()
print("FAILED: %s" % FAILS if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
