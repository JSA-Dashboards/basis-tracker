"""The Equity (Illinois co-op) on the AgriCharts tenant scraper: theequity.agricharts.com's cashbids-js.php feed (the one theequity.com/grain/cash-bids embeds).

    python tests/test_theequity_agricharts.py

Kolten 2026-11-10: "Add this to basis scrapper and note Effingham and Horace are two feed mills." One TENANTS row plus one small option in agricharts_scraper, `facility_by_name`: the feed labels ALL six
sites 'Country Elevator' (the multi shape trusts the feed's own facility_type), but Effingham and Horace are feed mills, so those two are filed as 'Feed Mill'. Its monthly windows begin on the 1st (no
open_windows), every site carries its own latitude / longitude (pins). Uses a trimmed copy of the real feed of 2026-11-10 (tests/fixtures/theequity_feed_2026-11-10.json, original values) - no network;
the database is a throwaway SQLite.
"""
import copy
import json
import logging
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import agricharts_scraper as ag                                  # noqa: E402
import database as db                                            # noqa: E402
from parsers.ksethanol_parser import parse_ksethanol_location    # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="theequity_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


FEED = json.load(open(os.path.join(HERE, "fixtures", "theequity_feed_2026-11-10.json"), encoding="utf-8"))
EQ = next((t for t in ag.TENANTS if t["provider"] == "The Equity"), None)
SITES = ["Effingham, IL", "Horace, IL", "Martinsville, IL", "Snyder, IL", "Strasburg, IL", "Westfield, IL"]
MILLS = ["Effingham, IL", "Horace, IL"]

print("the equity: the tenant row")
check("a multi-location AgriCharts tenant on theequity.agricharts.com, IL, with the feed's pins", EQ is not None and (EQ["host"], EQ["shape"], EQ["state"], EQ.get("pins")) == ("https://theequity.agricharts.com", "multi", "IL", True), EQ)
check("Effingham and Horace are the two feed mills, keyed on the feed's site name upper-cased", EQ is not None and EQ.get("facility_by_name") == {"EFFINGHAM": "Feed Mill", "HORACE": "Feed Mill"}, EQ)
check("it needs no skip, grain_alias, open_windows or titlecase (its windows begin on the 1st, its names are mixed case, plain 'Wheat' is how ZW bids are filed elsewhere)",
      EQ is not None and not any(EQ.get(k) for k in ("skip", "grain_alias", "open_windows", "titlecase", "want", "skip_notes", "skip_zero_basis")), EQ)
check("only The Equity has facility_by_name; the provider name is unique", [t["provider"] for t in ag.TENANTS if t.get("facility_by_name")] == ["The Equity"] and [t["provider"] for t in ag.TENANTS].count("The Equity") == 1,
      [t["provider"] for t in ag.TENANTS])


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, feed):
        self.headers = {}
        self.urls = []
        self.feed = feed

    def get(self, url, timeout=None):
        self.urls.append(url)
        return FakeResp("/* AgriCharts */\nvar bids = " + json.dumps(self.feed) + ";\nvar x = 1;")


def fetch(tenant, feed=FEED):
    sess = FakeSession(feed)
    with mock.patch.object(ag.requests, "Session", lambda: sess), mock.patch.object(ag, "TENANTS", [tenant]), mock.patch.object(ag.time, "sleep", lambda s: None):
        return ag.fetch_agricharts_bids(), sess


print("\nthe equity: the fetch (the real feed's shape)")
locs, sess = fetch(EQ)
by = {l["location_name"]: l for l in locs}
check("it asks the tenant's own host for cashbids-js.php", len(sess.urls) == 1 and sess.urls[0].startswith("https://theequity.agricharts.com/inc/cashbids/cashbids-js.php?"), sess.urls)
check("six Illinois sites, each as 'Town, IL' (Horace keeps the feed's name, not its city Chrisman)", sorted(by) == sorted(SITES), sorted(by))
check("every location is filed under the provider 'The Equity' with state IL", {(l["provider"], l["state"]) for l in locs} == {("The Equity", "IL")}, [(l["provider"], l["state"]) for l in locs])
check("Effingham and Horace are 'Feed Mill'; the other four stay what the feed says, 'Country Elevator'",
      {n: l["facility_type"] for n, l in by.items()} == {"Effingham, IL": "Feed Mill", "Horace, IL": "Feed Mill", "Martinsville, IL": "Country Elevator", "Snyder, IL": "Country Elevator", "Strasburg, IL": "Country Elevator", "Westfield, IL": "Country Elevator"},
      {n: l["facility_type"] for n, l in by.items()})
check("the feed itself calls all six 'Country Elevator' (so the override is what makes the two mills)", {l["facility_type"] for l in FEED} == {"Country Elevator"}, {l["facility_type"] for l in FEED})
locs_plain, _ = fetch({k: v for k, v in EQ.items() if k != "facility_by_name"})
check("without facility_by_name all six would be filed as Country Elevator", {l["facility_type"] for l in locs_plain} == {"Country Elevator"}, {l["facility_type"] for l in locs_plain})
odd = copy.deepcopy(FEED)
for l in odd:
    if l["name"] == "Horace":
        l["name"] = " horace "
locs_odd, _ = fetch(EQ, odd)
check("the key matches the feed's name whatever its case or padding", [l["facility_type"] for l in locs_odd if "horace" in l["location_name"].lower()] == ["Feed Mill"], [(l["location_name"], l["facility_type"]) for l in locs_odd])
check("map pins are the feed's own coordinates (Effingham 39.12848 / -88.54392, Horace 39.72163 / -87.70729)",
      (by["Effingham, IL"]["lat"], by["Effingham, IL"]["lon"], by["Horace, IL"]["lat"], by["Horace, IL"]["lon"]) == (39.12848, -88.54392, 39.72163, -87.70729), (by["Effingham, IL"].get("lat"), by["Horace, IL"].get("lat")))
check("every site has a pin", all(l.get("lat") and l.get("lon") for l in locs))
check("no open_windows on any location (its windows are calendar months)", all("open_windows" not in l for l in locs))
check("42 bids: 7 per site (corn x3, soybeans x3, wheat x1)", sum(len(l["cashbids"]) for l in locs) == 42 and all(Counter(b["grain"] for b in l["cashbids"]) == Counter({"Corn": 3, "Soybeans": 3, "Wheat": 1}) for l in locs),
      [Counter(b["grain"] for b in l["cashbids"]) for l in locs][:2])


def parse(loc_list, when):
    out = {}
    for l in loc_list:
        l = dict(l, timestamp=when)
        out[l["location_name"]] = parse_ksethanol_location(l)
    return out


def rowmap(req):
    return {(r.grain, r.futuresSymbol, r.deliveryMonth): r.basisCents for r in req.rows}


print("\nthe equity: parsed through the shared AgriCharts parser, on the day it was added (2026-11-10)")
reqs = parse(locs, "2026-11-10T00:00:00Z")
eff = rowmap(reqs["Effingham, IL"])
check("Effingham: corn Nov -23 / Dec -17 (ZCZ26) and Jan -25 (ZCH27), soybeans -30 / -25 / -20 (ZSF27), wheat -60 (ZWZ26)",
      eff == {("Corn", "ZCZ26", "Nov 2026"): -23, ("Corn", "ZCZ26", "Dec 2026"): -17, ("Corn", "ZCH27", "Jan 2027"): -25, ("Soybeans", "ZSF27", "Nov 2026"): -30, ("Soybeans", "ZSF27", "Dec 2026"): -25,
              ("Soybeans", "ZSF27", "Jan 2027"): -20, ("Wheat", "ZWZ26", "Nov 2026"): -60}, eff)
hor = rowmap(reqs["Horace, IL"])
check("Horace: corn -18 / -5 / -15, the same soybeans, wheat -80", hor == {("Corn", "ZCZ26", "Nov 2026"): -18, ("Corn", "ZCZ26", "Dec 2026"): -5, ("Corn", "ZCH27", "Jan 2027"): -15, ("Soybeans", "ZSF27", "Nov 2026"): -30,
      ("Soybeans", "ZSF27", "Dec 2026"): -25, ("Soybeans", "ZSF27", "Jan 2027"): -20, ("Wheat", "ZWZ26", "Nov 2026"): -80}, hor)
check("every site files all 7 bids with unique row ids, and every basis is the feed's own", all(
      len(r.rows) == 7 and len({x.id for x in r.rows}) == 7 and sorted(rowmap(r).values()) == sorted(b["basis"] for l in FEED if l["name"] + ", IL" == n for b in l["cashbids"]) for n, r in reqs.items()),
      {n: len(r.rows) for n, r in reqs.items()})
check("42 rows in all, and the provider is 'The Equity' on every request", sum(len(r.rows) for r in reqs.values()) == 42 and {r.provider for r in reqs.values()} == {"The Equity"})

print("\nthe equity: the window rule on other days")
dec = parse(locs, "2026-12-05T00:00:00Z")
check("on 2026-12-05 the November windows (ended 11/30) and the November wheat are gone; December and January are left (4 bids per site)",
      all(sorted((g, m) for g, _, m in rowmap(r)) == [("Corn", "Dec 2026"), ("Corn", "Jan 2027"), ("Soybeans", "Dec 2026"), ("Soybeans", "Jan 2027")] for r in dec.values()),
      sorted(rowmap(dec["Effingham, IL"])))
nov = parse(locs, "2026-11-20T00:00:00Z")
check("on 2026-11-20 the November windows (begun 11/01, open until 11/30) are still this month's bids: still 7 per site", all(len(r.rows) == 7 for r in nov.values()), {n: len(r.rows) for n, r in nov.items()})

print("\nthe equity: types and pins reach the database (throwaway SQLite)")
db.init_db()
db.upsert_snapshots([r.model_dump() for r in reqs.values()])
metas = [{"location": r.location, "state": by[r.location]["state"], "facility_type": by[r.location]["facility_type"], "lat": by[r.location].get("lat"), "lon": by[r.location].get("lon")} for r in reqs.values()]
db.upsert_location_metas("The Equity", metas)       # the same dict shape run_agricharts_tenants builds
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT location, state, facility_type, lat, lon FROM location_meta WHERE provider = 'The Equity' ORDER BY location")
got = [tuple(r) for r in c.fetchall()]
conn.close()
check("six locations in location_meta, state IL, the two mills typed 'Feed Mill', each with the feed's pin", got == [
      ("Effingham, IL", "IL", "Feed Mill", 39.12848, -88.54392), ("Horace, IL", "IL", "Feed Mill", 39.72163, -87.70729), ("Martinsville, IL", "IL", "Country Elevator", 39.33511, -87.88111),
      ("Snyder, IL", "IL", "Country Elevator", 39.28855, -87.66824), ("Strasburg, IL", "IL", "Country Elevator", 39.3496, -88.61695), ("Westfield, IL", "IL", "Country Elevator", 39.45587, -87.98846)], got)
snaps = db.get_snapshots("The Equity", "Horace, IL")
check("Horace reads back one snapshot (source 'web') with its 7 rows through the app's own loader", len(snaps) == 1 and snaps[0].source == "web" and len(snaps[0].rows) == 7, [(s.source, len(s.rows)) for s in snaps])
db.upsert_snapshots([r.model_dump() for r in reqs.values()])
check("a second run the same day adds nothing (same row ids)", len(db.get_snapshots("The Equity", "Horace, IL")) == 1 and len(db.get_snapshots("The Equity", "Horace, IL")[0].rows) == 7)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
