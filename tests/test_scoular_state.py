"""Scoular scraper: a location's state is the one its PAGE names ('Ada, KS'), not the one the id table says.

    python tests/test_scoular_state.py

2026-10-23 (Kolten: "This should be the state of KS, not IL"): scoular_scraper reads the location NAME from the page but the STATE from SCOULAR_LOCATIONS, which is keyed by Bushel
id. Bushel answers nine retired ids with one sibling's page, 'Ada, KS', and the first of them in the table (1889, 'Andres, IL') won the name de-dup, so Ada, KS was filed in
location_meta as IL (Kansas coordinates, Illinois state) and every daily run wrote IL back. No network: the pages are canned and the database is a throwaway SQLite.
"""
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                                       # noqa: E402
import scoular_scraper as sc                                # noqa: E402
from parsers.scoular_parser import parse_scoular_location   # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="scoular_state_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


print("scoular: the state a page names for itself")
for name, want in (("Ada, KS", "KS"), ("Minneapolis, KS", "KS"), ("Goodland Crush, KS", "KS"), ("Pisgah, IL - Elevator", "IL"), ("Waverly, IL - Shuttle", "IL"), ("Madrid, NE Ethanol", "NE"),
                   ("Scoular Paxton, NE Bids", "NE"), ("Weyers Cave, VA - Transloader", "VA"), ("Scoular - Butte, MT", "MT")):
    check("%r -> %s" % (name, want), sc._page_state(name) == want, sc._page_state(name))
for name in ("Scoular Idalia", "Tribune - Main / NW / Horace", "Scoular - Walnut Grove Farms / Schochoh", "AGP - Manning", "Scoular Julesburg Cash Bids", "Smith, XX", "Acme, Inc", "", None):
    check("%r names no state -> ''" % (name,), sc._page_state(name) == "", sc._page_state(name))


def page(name, grain, basis, futures):
    li = lambda cls, text: '<li class="%s">%s</li>' % (cls, text)
    row = ('<ul class="fcControls1"><li class="c1"><span class="cashBidsColumnOne">Cash</span></li>' + li("c2", "9.50") + li("c3", basis) + li("c4", "5.00") + li("c5", "0.01")
           + li("c6", futures) + li("c7", "10:00") + "</ul>")
    return ('<html><body><div class="cashBidLocation"><h2>%s</h2><div class="cbCommodity"><h3>%s</h3><ul class="fcControlsSubHdr"><li>Delivery</li></ul>%s</div></div></body></html>'
            % (name, grain, row))


ADA = page("Ada, KS", "HRWW", "-0.82", "Dec 26 KCBT Red Wheat")
PAGES = {1889: ADA, 1894: ADA,                                                 # retired ids: Bushel answers them with the Ada page
         1945: page("Minneapolis, KS", "YC", "-0.55", "Dec 26 Corn"),
         1916: page("Scoular Idalia", "YC", "-0.40", "Dec 26 Corn"),           # a page with no state in its name
         1942: page("Pisgah, IL - Elevator", "YSB", "-0.30", "Nov 26 Soybeans")}


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self):
        self.headers = {}

    def get(self, url, timeout=None):
        return FakeResp(PAGES[int(url.rsplit("-", 1)[1])])


print("\nscoular: the fetch, with 1889 ('Andres, IL') and 1894 ('Owensboro, KY') both answering with the Ada page")
with mock.patch.object(sc.requests, "Session", FakeSession), mock.patch.object(sc, "REQUEST_DELAY", 0):
    locs = sc.fetch_scoular_bids([1889, 1894, 1945, 1916, 1942])
by_name = {l["location_name"]: l for l in locs}
check("five ids, four locations: the Ada page counts once", sorted(by_name) == ["Ada, KS", "Minneapolis, KS", "Pisgah, IL - Elevator", "Scoular Idalia"], sorted(by_name))
check("the table still says id 1889 is Andres, IL (that is how it was found)", sc.SCOULAR_LOCATIONS[1889]["state"] == "IL" and sc.SCOULAR_LOCATIONS[1889]["name"] == "Andres, IL")
check("Ada, KS is filed under KS - the state its page names, not the IL of the first id that returned it", by_name["Ada, KS"]["state"] == "KS", by_name["Ada, KS"])
check("a page that names no state keeps the table's (Scoular Idalia -> CO)", by_name["Scoular Idalia"]["state"] == "CO", by_name["Scoular Idalia"])
check("a page that agrees with the table is unchanged (Minneapolis -> KS, Pisgah -> IL)", (by_name["Minneapolis, KS"]["state"], by_name["Pisgah, IL - Elevator"]["state"]) == ("KS", "IL"))

print("\nscoular: the next scrape repairs a row already stored with the wrong state (run_scoular's own meta line, throwaway SQLite)")
db.init_db()
db.upsert_location_meta("Scoular", "Ada, KS", state="IL", facility_type="Country Elevator", lat=39.1514, lon=-97.8892)      # as found on 2026-10-23
snaps = [parse_scoular_location(l) for l in locs]
check("every canned page parses to a snapshot", all(s is not None for s in snaps), [l["location_name"] for l, s in zip(locs, snaps) if s is None])
db.upsert_location_metas("Scoular", [{"location": s.location, "state": l.get("state") or None} for l, s in zip(locs, snaps) if s])
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT state, facility_type, lat, lon FROM location_meta WHERE provider = %s AND location = %s" % (db._ph(), db._ph()), ("Scoular", "Ada, KS"))
row = c.fetchone()
conn.close()
check("Ada, KS reads back KS, its type and coordinates untouched", tuple(row) == ("KS", "Country Elevator", 39.1514, -97.8892), tuple(row))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
