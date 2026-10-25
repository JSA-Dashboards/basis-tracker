"""Alcivia (WI) on the AgriCharts tenant scraper: alciviacoop.agricharts.com's cashbids-js.php feed (the one alcivia.com/wisconsin-grain-services/cash-bids-grain-wisconsin-grain/ embeds).

    python tests/test_alcivia_agricharts.py

Kolten 2026-10-25: "A couple scrapers to add ... Alcivia". One TENANTS row, plus two small options in agricharts_scraper: `titlecase` (the feed's names are ALL CAPS) and `pins` (the feed carries each
location's own latitude / longitude, which become the map pins; off for every other tenant, so their stored pins are untouched). Uses a trimmed copy of the real feed
(tests/fixtures/alcivia_feed_2026-10-25.json: three real locations + a hidden one + one with no bids) - no network; the database is a throwaway SQLite.
"""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import agricharts_scraper as ag                                  # noqa: E402
import database as db                                            # noqa: E402
from parsers.ksethanol_parser import parse_ksethanol_location    # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="alcivia_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


FEED = json.load(open(os.path.join(HERE, "fixtures", "alcivia_feed_2026-10-25.json"), encoding="utf-8"))
ALCIVIA = next((t for t in ag.TENANTS if t["provider"] == "Alcivia"), None)

print("alcivia: the tenant row and the two helpers")
check("Alcivia is a multi-location AgriCharts tenant on alciviacoop.agricharts.com, WI, with titlecase and pins on",
      ALCIVIA is not None and (ALCIVIA["host"], ALCIVIA["shape"], ALCIVIA["state"], ALCIVIA.get("titlecase"), ALCIVIA.get("pins")) == ("https://alciviacoop.agricharts.com", "multi", "WI", True, True), ALCIVIA)
check("no other tenant has pins or titlecase switched on (their stored pins and names are untouched)", [t["provider"] for t in ag.TENANTS if t.get("pins") or t.get("titlecase")] == ["Alcivia"],
      [t["provider"] for t in ag.TENANTS if t.get("pins") or t.get("titlecase")])
for raw, want in (("FALL CREEK", "Fall Creek"), ("BALDWIN", "Baldwin"), ("Four Corners (New Richmond)", "Four Corners (New Richmond)"), ("MENOMONIE FEED MILL", "Menomonie Feed Mill"), ("Kings Valley (Osseo)", "Kings Valley (Osseo)")):
    check("_title_caps(%r) -> %r" % (raw, want), ag._title_caps(raw) == want, ag._title_caps(raw))
for loc, want in (({"latitude": "44.968448000", "longitude": "-92.374671000"}, {"lat": 44.96845, "lon": -92.37467}), ({"latitude": None, "longitude": None}, {}), ({"latitude": "", "longitude": ""}, {}),
                  ({"latitude": "0", "longitude": "0"}, {}), ({"latitude": "95", "longitude": "-90"}, {}), ({}, {})):
    check("_feed_pin(%r) -> %r" % (loc, want), ag._feed_pin(loc) == want, ag._feed_pin(loc))


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return FakeResp("/* AgriCharts */\nvar bids = " + json.dumps(FEED) + ";\nvar x = 1;")


print("\nalcivia: the fetch (the real feed's shape)")
sess = FakeSession()
with mock.patch.object(ag.requests, "Session", lambda: sess), mock.patch.object(ag, "TENANTS", [ALCIVIA]), mock.patch.object(ag.time, "sleep", lambda s: None):
    locs = ag.fetch_agricharts_bids()
by = {l["location_name"]: l for l in locs}
check("it asks the tenant's own host for cashbids-js.php", len(sess.urls) == 1 and sess.urls[0].startswith("https://alciviacoop.agricharts.com/inc/cashbids/cashbids-js.php?"), sess.urls)
check("three locations: the hidden one and the one with no bids are not filed",
      sorted(by) == ["Baldwin, WI", "Four Corners (New Richmond), WI", "Menomonie Feed Mill, WI"], sorted(by))
check("every location is Alcivia / WI", {(l["provider"], l["state"]) for l in locs} == {("Alcivia", "WI")}, [(l["provider"], l["state"]) for l in locs])
check("facility types come from the feed (elevators, and the feed mill)", (by["Baldwin, WI"]["facility_type"], by["Four Corners (New Richmond), WI"]["facility_type"],
      by["Menomonie Feed Mill, WI"]["facility_type"]) == ("Country Elevator", "Country Elevator", "Feed Mill"), {k: v["facility_type"] for k, v in by.items()})
check("map pins are the feed's own coordinates (Baldwin WI 44.96845 / -92.37467)", (by["Baldwin, WI"]["lat"], by["Baldwin, WI"]["lon"]) == (44.96845, -92.37467), (by["Baldwin, WI"].get("lat"), by["Baldwin, WI"].get("lon")))
check("... and every filed location has one", all("lat" in l and "lon" in l for l in locs))
check("Baldwin carries its 14 bids (corn, soybeans, wheat)", len(by["Baldwin, WI"]["cashbids"]) == 14 and {b["grain"] for b in by["Baldwin, WI"]["cashbids"]} == {"CORN", "SOYBEANS", "WHEAT"})

print("\nalcivia: parsed through the shared AgriCharts parser")
for l in locs:
    l["timestamp"] = "2026-10-25T00:00:00Z"        # pin the scrape date: the parser drops bids whose month is already past
reqs = {l["location_name"]: parse_ksethanol_location(l) for l in locs}
check("each location parses to a snapshot filed under provider Alcivia", all(r is not None and r.provider == "Alcivia" for r in reqs.values()), reqs)
bal = reqs["Baldwin, WI"]
rows = {(r.grain, r.deliveryMonth): (r.futuresSymbol, r.basisCents) for r in bal.rows}
check("Baldwin corn Oct/Nov 26 -> 'Oct 2026' vs ZCZ26 at -75", rows.get(("Corn", "Oct 2026")) == ("ZCZ26", -75), rows)
check("Baldwin corn January 27 -> vs ZCH27 at -70; soybeans Oct/Nov 26 -> vs ZSX26 at -95", (rows.get(("Corn", "Jan 2027")), rows.get(("Soybeans", "Oct 2026"))) == (("ZCH27", -70), ("ZSX26", -95)),
      (rows.get(("Corn", "Jan 2027")), rows.get(("Soybeans", "Oct 2026"))))
check("Baldwin keeps its wheat, and its far Oct/Nov 27 corn bid", ("Wheat", "Jul 2027") in rows and rows.get(("Corn", "Oct 2027")) == ("ZCZ27", -65), rows)
check("row ids are unique within a snapshot", len({r.id for r in bal.rows}) == len(bal.rows))

print("\nalcivia: the pins reach location_meta, and a tenant without pins leaves a stored pin alone (throwaway SQLite)")
db.init_db()
db.upsert_snapshots([r.model_dump() for r in reqs.values()])
metas = [{"location": r.location, "state": by[r.location]["state"], "facility_type": by[r.location]["facility_type"], "lat": by[r.location].get("lat"), "lon": by[r.location].get("lon")} for r in reqs.values()]
db.upsert_location_metas("Alcivia", metas)       # the same dict shape run_agricharts_tenants builds
db.upsert_location_meta("Garden City Coop", "Garden City, KS", state="KS", facility_type="Country Elevator", lat=37.9717, lon=-100.8727)       # a pin set earlier
db.upsert_location_metas("Garden City Coop", [{"location": "Garden City, KS", "state": "KS", "facility_type": "Country Elevator", "lat": None, "lon": None}])   # a tenant with no pins
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT location, state, facility_type, lat, lon FROM location_meta WHERE provider = 'Alcivia' ORDER BY location")
got = [tuple(r) for r in c.fetchall()]
c.execute("SELECT state, lat, lon FROM location_meta WHERE provider = 'Garden City Coop' AND location = 'Garden City, KS'")
gc = tuple(c.fetchone())
conn.close()
check("Alcivia's three locations are in location_meta with state, type and the feed's pin", got == [("Baldwin, WI", "WI", "Country Elevator", 44.96845, -92.37467),
      ("Four Corners (New Richmond), WI", "WI", "Country Elevator", 45.12282, -92.37246), ("Menomonie Feed Mill, WI", "WI", "Feed Mill", 44.90801, -91.85325)], got)
check("Garden City Coop's stored pin survives an upsert whose lat / lon are None", gc == ("KS", 37.9717, -100.8727), gc)
snaps = db.get_snapshots("Alcivia", "Baldwin, WI")
check("Baldwin reads back one snapshot (source 'web') through the app's own loader", len(snaps) == 1 and snaps[0].source == "web" and len(snaps[0].rows) == len(bal.rows), [(s.source, len(s.rows)) for s in snaps])

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
