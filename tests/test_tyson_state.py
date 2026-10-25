"""Tyson LGS feed: a State of 'Unknown' is no state (tyson_scraper._clean_state), so the daily meta upsert cannot overwrite a state a person set.

    python tests/test_tyson_state.py

2026-10-25 (map-pin audit): the feed gives Camilla (GA) and Humboldt (TN) a State of 'Unknown' while their pins, from the same feed, are right. run_tyson upserts state every day and the upsert
keeps the stored value only for None, so a hand-set GA would be written back to 'Unknown' on the next run. No network: urlopen is canned and the database is a throwaway SQLite.
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

import database as db                                       # noqa: E402
import tyson_scraper as ts                                  # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="tyson_state_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


print("tyson: _clean_state")
for raw, want in (("Unknown", None), ("UNKNOWN", None), ("  unknown ", None), ("", None), ("   ", None), (None, None), (5, None), ("GA", "GA"), (" TN ", "TN"), ("Georgia", "Georgia")):
    check("%r -> %r" % (raw, want), ts._clean_state(raw) == want, ts._clean_state(raw))

FEED = {"locations": [
    {"Location": {"Name": "Camilla", "Type": "Elevator", "City": "Camilla", "State": "Unknown", "Latitude": 31.23129, "Longitude": -84.210464}, "BidRows": []},
    {"Location": {"Name": "Dyer", "Type": "Elevator", "City": "Dyer", "State": "TN", "Latitude": 36.064268, "Longitude": -88.992122}, "BidRows": []},
]}


class FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(FEED).encode()


print("\ntyson: the fetch, with the feed saying 'Unknown' for Camilla")
with mock.patch.object(ts.urllib.request, "urlopen", lambda req, timeout=None: FakeResp()):
    locs = ts.fetch_tyson_bids()
by = {l["location_name"]: l for l in locs}
check("Camilla comes back with no state (None), pin kept", by["Camilla"]["state"] is None and (by["Camilla"]["lat"], by["Camilla"]["lon"]) == (31.23129, -84.210464), by["Camilla"])
check("Dyer keeps its TN", by["Dyer"]["state"] == "TN", by["Dyer"])

print("\ntyson: a state set by hand survives the daily meta upsert (run_tyson's own call, throwaway SQLite)")
db.init_db()
db.upsert_location_meta("Tyson", "Camilla", state="GA", facility_type="Country Elevator", lat=31.23129, lon=-84.210464)      # set by hand 2026-10-25
for loc in locs:
    db.upsert_location_meta("Tyson", loc["location_name"], state=loc.get("state") or None, facility_type=None, lat=loc["lat"], lon=loc["lon"])
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT location, state, facility_type FROM location_meta WHERE provider = 'Tyson' AND location IN ('Camilla', 'Dyer') ORDER BY location")
rows = [tuple(r) for r in c.fetchall()]       # init_db() seeds the whole Tyson roster, so only these two rows are looked at
conn.close()
check("Camilla is still GA after the run's upsert (and still a Country Elevator); Dyer is TN", [r[:2] for r in rows] == [("Camilla", "GA"), ("Dyer", "TN")] and rows[0][2] == "Country Elevator", rows)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
