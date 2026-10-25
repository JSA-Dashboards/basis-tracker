"""StoneX "Stonehedge" bids component (stonex_scraper.py) -> River Valley Cooperative's own elevators.

    python tests/test_stonex_scraper.py

Kolten 2026-10-25: "A couple scrapers to add. River Valley Coop https://www.rivervalleycoop.com/products-services/grain". That page embeds stonehedge.stonex.com/component/bids?key=..., a React client over one REST
call (GET api.stonehedge.stonex.com/settings/v2/delivery-periods, `Authorization: key <embed key>`). Uses a trimmed copy of the real response (tests/fixtures/stonex_rv_2026-10-25.json: all 37 delivery periods,
eight of the co-op's own locations - six with bids, Duncan and Eldridge with none - and three destination points) - no network; the database is a throwaway SQLite.
"""
import copy
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                                       # noqa: E402
import delivery_period as dp                                # noqa: E402
import stonex_scraper as sx                                 # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="stonex_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


PAYLOAD = json.load(open(os.path.join(HERE, "fixtures", "stonex_rv_2026-10-25.json"), encoding="utf-8"))
CFG = sx.SITES[0]
NOW = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
TS = "2026-10-25T00:00:00Z"

print("stonex: delivery-window labels")
for start, end, want in (("2026-10-01T00:00:00Z", "2026-10-31T00:00:00Z", "Oct 2026"), ("2026-11-01T00:00:00Z", "2026-11-15T00:00:00Z", "FH Nov 2026"), ("2026-11-16T00:00:00Z", "2026-11-30T00:00:00Z", "LH Nov 2026"),
                         ("2026-10-16T00:00:00Z", "2026-10-31T00:00:00Z", "LH Oct 2026"), ("2027-07-01T00:00:00Z", "2027-07-30T00:00:00Z", "Jul 2027"), ("2027-02-01T00:00:00Z", "2027-02-28T00:00:00Z", "Feb 2027"),
                         ("2026-10-19T00:00:00Z", "2026-10-24T00:00:00Z", "Oct 19-24 2026"), ("2026-10-28T00:00:00Z", "2026-11-03T00:00:00Z", "Oct 28-Nov 3 2026"), ("2026-12-16", "2026-12-31", "LH Dec 2026")):
    check("%s .. %s -> %r" % (start[:10], end[:10], want), sx._label(start, end) == want, sx._label(start, end))
check("an unreadable date gives no label", sx._label("garbage", "2026-10-31") is None and sx._label(None, None) is None)
check("the app reads the labels: 'FH Nov 2026' and 'LH Nov 2026' are Nov 2026 with the half-month ranks, 'Oct 19-24 2026' is Oct 2026",
      (dp.canonical("FH Nov 2026", "ZSX26"), dp.canonical("LH Nov 2026", "ZSX26"), dp.canonical("Oct 19-24 2026", "ZCZ26"), dp.sub_rank("FH Nov 2026"), dp.sub_rank("LH Nov 2026"), dp.sub_rank("Nov 2026"))
      == ((2026, 11), (2026, 11), (2026, 10), 0, 2, 1))

print("\nstonex: contract symbols")
for commodity, sym, want in (("Corn", "CZ26", "ZCZ26"), ("Soybeans", "SX26", "ZSX26"), ("Soybeans", "SF27", "ZSF27"), ("Corn", "CH27", "ZCH27"), ("Corn", "SZ26", None), ("Soybeans", "CX26", None),
                             ("Wheat", "WZ26", None), ("Corn", "", None), ("Corn", None, None), ("Corn", "CA26", None), ("Corn", "CZ2", None)):
    check("(%r, %r) -> %r" % (commodity, sym, want), sx._cme(commodity, sym) == want, sx._cme(commodity, sym))

print("\nstonex: River Valley's own elevators, from the real response")
reqs, metas, info = sx.build_site(CFG, PAYLOAD, TS, NOW)
by = {r.location: r for r in reqs}
check("six locations are filed (Duncan and Eldridge have no bids; the destination points are not River Valley's)", sorted(by) == ["Altona, IL", "DeWitt, IA", "Donahue, IA", "Sunbury, IA", "Valley, IL", "Wyoming, IL"], sorted(by))
check("every snapshot is River Valley Coop, source 'web', stamped with the scrape day", {(r.provider, r.source, r.timestamp) for r in reqs} == {("River Valley Coop", "web", TS)})
check("no ADM / CGB / Cargill location is filed under any name", not any(w in r.location for r in reqs for w in ("ADM", "CGB", "Cargill")))
check("the destination points and nothing else are the 'not the co-op's own' list", info["unmapped"] == ["ADM - Burlington", "CGB-Hennepin", "Cargill - Muscatine"], info["unmapped"])
check("row counts per location are the usable bids (Altona 18, DeWitt 20, Donahue 11, Sunbury 10, Valley 18, Wyoming 18)",
      {k: len(v.rows) for k, v in by.items()} == {"Altona, IL": 18, "DeWitt, IA": 20, "Donahue, IA": 11, "Sunbury, IA": 10, "Valley, IL": 18, "Wyoming, IL": 18}, {k: len(v.rows) for k, v in by.items()})
rows = {loc: {(r.grain, r.deliveryMonth): (r.futuresSymbol, r.basisCents) for r in req.rows} for loc, req in by.items()}
check("Altona corn Oct 2026 vs ZCZ26 at -17 (-0.17 $/bu); soybeans Oct 2026 vs ZSX26 at -45", (rows["Altona, IL"][("Corn", "Oct 2026")], rows["Altona, IL"][("Soybeans", "Oct 2026")]) == (("ZCZ26", -17), ("ZSX26", -45)),
      rows["Altona, IL"])
check("Altona soybeans Dec 2026 is vs the JANUARY contract (ZSF27, -29); corn Jan 2027 vs ZCH27 at -31", (rows["Altona, IL"][("Soybeans", "Dec 2026")], rows["Altona, IL"][("Corn", "Jan 2027")]) == (("ZSF27", -29), ("ZCH27", -31)))
check("DeWitt corn Nov 2026 -35; Wyoming corn Dec 2026 -23; Sunbury corn July (window ends the 30th) is 'Jul 2027' vs ZCN27 at -14",
      (rows["DeWitt, IA"][("Corn", "Nov 2026")][1], rows["Wyoming, IL"][("Corn", "Dec 2026")][1], rows["Sunbury, IA"][("Corn", "Jul 2027")]) == (-35, -23, ("ZCN27", -14)))
check("Donahue soybeans carry the first-half and last-half November bids: FH -44, LH -42, both vs ZSX26", (rows["Donahue, IA"][("Soybeans", "FH Nov 2026")], rows["Donahue, IA"][("Soybeans", "LH Nov 2026")]) == (("ZSX26", -44), ("ZSX26", -42)))
check("only corn and soybeans are filed", {g for r in rows.values() for g, _ in r} == {"Corn", "Soybeans"})
check("row ids are unique within each snapshot", all(len({x.id for x in r.rows}) == len(r.rows) for r in reqs))
check("nothing is flagged spot", not any(x.isSpot for r in reqs for x in r.rows))
mm = {m["location"]: m for m in metas}
check("each location's meta: state, Country Elevator, and the pin from River Valley's own map (Altona IL 41.11217 / -90.16264; DeWitt IA 41.81501 / -90.53763)",
      (mm["Altona, IL"]["state"], mm["Altona, IL"]["facility_type"], mm["Altona, IL"]["lat"], mm["Altona, IL"]["lon"], mm["DeWitt, IA"]["state"], mm["DeWitt, IA"]["lat"], mm["DeWitt, IA"]["lon"])
      == ("IL", "Country Elevator", 41.11217, -90.16264, "IA", 41.81501, -90.53763), mm["Altona, IL"])
check("... and every pin is inside its own state", all((m["state"] == "IL" and 36.9 <= m["lat"] <= 42.6 and -91.6 <= m["lon"] <= -87.0) or (m["state"] == "IA" and 40.3 <= m["lat"] <= 43.6 and -96.7 <= m["lon"] <= -90.1) for m in metas), metas)
check("all 22 of the co-op's StoneX names have a label, a state and an in-state pin in the SITES row", len(sx.RIVER_VALLEY_LOCATIONS) == 22 and all(
      (st == "IL" and 36.9 <= la <= 42.6 and -91.6 <= lo <= -87.0) or (st == "IA" and 40.3 <= la <= 43.6 and -96.7 <= lo <= -90.1) for _, st, la, lo in sx.RIVER_VALLEY_LOCATIONS.values()))
check("fetch_stonex(payloads=...) gives the same snapshots as build_site", [r.model_dump() for r in sx.fetch_stonex({"River Valley Coop": PAYLOAD}, NOW)[0]] == [r.model_dump() for r in reqs])

print("\nstonex: freshness, odd input")
check("a board whose newest own bid is 5+ days old files nothing (the newest is 2026-10-24 13:25 UTC)", sx.build_site(CFG, PAYLOAD, TS, datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc))[0] == [])
st = sx.build_site(CFG, PAYLOAD, TS, datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc))[2]
check("... and says it is stale", st["stale"] is True, st)
check("4 days 22 hours after the newest update is still fresh", len(sx.build_site(CFG, PAYLOAD, TS, datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))[0]) == 6)
check("a payload with no deliveryPeriods files nothing", sx.build_site(CFG, {}, TS, NOW)[0] == [] and sx.build_site(CFG, {"deliveryPeriods": []}, TS, NOW)[0] == [])
check("no payload (the request failed) files nothing", sx.fetch_stonex({"River Valley Coop": None}, NOW) == ([], []))
odd = copy.deepcopy(PAYLOAD)
odd["deliveryPeriods"].append({"name": "Dec 26", "start": "2026-12-01T00:00:00Z", "end": "2026-12-31T00:00:00Z", "isSpot": False, "lastUpdatedDateUtc": "2026-10-24T13:25:45Z", "commodity": {"id": 99, "name": "Wheat"},
                               "contract": {"symbol": "WZ26", "maturityMonth": 12, "maturityYear": 2026, "price": 6.0}, "bids": [{"location": {"id": "x", "name": "Altona"}, "basis": -0.2, "cash": 5.8}]})
odd["deliveryPeriods"].append({"name": "Mar 27", "start": "2027-03-01T00:00:00Z", "end": "2027-03-31T00:00:00Z", "isSpot": False, "lastUpdatedDateUtc": "2026-10-24T13:25:45Z", "commodity": {"id": 7, "name": "Corn"},
                               "contract": {"symbol": "", "maturityMonth": 3, "maturityYear": 2027, "price": 4.7}, "bids": [{"location": {"id": "x", "name": "Altona"}, "basis": -0.2, "cash": 4.5}]})
r2, _, i2 = sx.build_site(CFG, odd, TS, NOW)
check("an own bid on an unknown commodity (wheat) or with no contract symbol is skipped and counted, not filed or crashed on", i2["skipped"] == 2 and {len(x.rows) for x in r2 if x.location == "Altona, IL"} == {18}, i2)
sparse = copy.deepcopy(PAYLOAD)
for p in sparse["deliveryPeriods"]:
    for b in p["bids"]:
        if b["location"]["name"] == "Altona":
            b["basis"] = None
check("a location whose bids are all blank (basis null) is not filed", "Altona, IL" not in {r.location for r in sx.build_site(CFG, sparse, TS, NOW)[0]})

print("\nstonex: filed and read back through the database (throwaway SQLite)")
db.init_db()
db.upsert_snapshots([r.model_dump() for r in reqs])
db.upsert_location_metas("River Valley Coop", [{"location": m["location"], "state": m["state"], "facility_type": m["facility_type"], "lat": m["lat"], "lon": m["lon"]} for m in metas])
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT location, state, facility_type, lat, lon FROM location_meta WHERE provider = 'River Valley Coop' ORDER BY location")
got = [tuple(x) for x in c.fetchall()]
conn.close()
check("River Valley's six locations are in location_meta with state, type and pin", [g[:3] for g in got] == [("Altona, IL", "IL", "Country Elevator"), ("DeWitt, IA", "IA", "Country Elevator"),
      ("Donahue, IA", "IA", "Country Elevator"), ("Sunbury, IA", "IA", "Country Elevator"), ("Valley, IL", "IL", "Country Elevator"), ("Wyoming, IL", "IL", "Country Elevator")] and got[0][3:] == (41.11217, -90.16264), got)
snap = db.get_snapshots("River Valley Coop", "Altona, IL")
check("Altona reads back one snapshot (source 'web') with its 18 rows through the app's own loader", len(snap) == 1 and snap[0].source == "web" and len(snap[0].rows) == 18, [(s.source, len(s.rows)) for s in snap])
db.upsert_snapshots([r.model_dump() for r in reqs])
check("a second run the same day adds nothing (same row ids)", len(db.get_snapshots("River Valley Coop", "Altona, IL")[0].rows) == 18)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
