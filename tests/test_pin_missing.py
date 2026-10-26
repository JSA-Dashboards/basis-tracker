"""pin_missing.py - the weekly job that gives a map pin to every location_meta row that has none.

    python tests/test_pin_missing.py

Kolten 2026-10-26 ("Both"): after the 2026-10-25 one-time pass (586 pins) nothing kept new locations pinned. This job is the same method on a schedule. No network: the geocoder is a dict, the feeds and ADM's
addresses are passed in, and the database is a throwaway SQLite. The scenarios are the real cases the pass met: Primient's LeRoy (two LeRoys in Illinois), Hopkinsville's Skyline (the only Skyline OSM
has is in eastern Kentucky), 'Village of Medina', Keystone's 'Eldorado Hills' (a neighbourhood, not the town), Total Grain's Hillsboro (a feed whose own state field is its head office's), CFE's Germantown
(a feed that contradicts our state).
"""
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                 # noqa: E402
import pin_missing as pm              # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="pin_missing_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)
FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:600]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def res(name, lat, lon, county="Some County", state="Iowa", cat="place", typ="town"):
    return {"category": cat, "type": typ, "name": name, "lat": str(lat), "lon": str(lon), "display_name": "%s, %s, %s, United States" % (name, county, state), "address": {}}


class FakeOsm:
    """A geocoder that is a dict: (city lower, ST) -> results (None = the request failed). Every lookup counts as a request."""

    def __init__(self, table):
        self.table, self.requests, self.calls = table, 0, []

    def cached(self, city, st):
        return False

    def lookup(self, city, st):
        self.requests += 1
        self.calls.append((city, st))
        return self.table.get((city.lower(), st), [])


def R(provider, location, state=None):
    return {"provider": provider, "location": location, "state": state, "facility_type": "Country Elevator"}


print("pin_missing: reading a location name")
for loc, want in (("Troy", (["Troy"], None)), ("Fremont MI", (["Fremont"], "MI")), ("AGP Aberdeen", (["Aberdeen"], None)), ("Redfield Energy", (["Redfield"], None)),
                  ("Weyers Cave, VA - Transloader", (["Weyers Cave"], "VA")), ("Mill 240 at Tarkio, MO", (["Tarkio"], "MO")), ("Sheldon North, IA", (["Sheldon"], "IA")),
                  ("Tribune - Main / NW / Horace", (["Tribune"], None))):
    check("%r -> %r" % (loc, want), pm.city_variants(loc) == want, pm.city_variants(loc))
v, _ = pm.city_variants("Ft Atkinson/Waucoma")
check("'Ft Atkinson/Waucoma' tries Fort Atkinson, then Waucoma", v[:2] == ["Ft Atkinson", "Fort Atkinson"] and "Waucoma" in v, v)
check("'Mt. Olive' also tries Mount Olive; 'St. Joseph' also Saint Joseph; 'LeRoy' also 'Le Roy'", "Mount Olive" in pm.city_variants("Mt. Olive")[0] and "Saint Joseph" in pm.city_variants("St. Joseph")[0]
      and "Le Roy" in pm.city_variants("LeRoy")[0])
check("'Hitchcock-Tulare, SD' tries both towns", pm.city_variants("Hitchcock-Tulare, SD")[0][:2] == ["Hitchcock", "Tulare"], pm.city_variants("Hitchcock-Tulare, SD"))

print("\npin_missing: which geocoder results count as the town")
rs = [res("Medina", 43.2203, -78.3866, "Orleans County", "New York", "boundary", "administrative"), res("Medina", 43.2200, -78.3870, "Orleans County", "New York", "place", "village"),
      res("Eldorado Hills", 41.07, -85.2, "Allen County", "Indiana", "place", "neighbourhood"), res("Medina Road", 43.0, -78.0, "X", "New York", "highway", "residential"),
      res("Medina", 41.14, -81.86, "Medina County", "Ohio", "boundary", "administrative")]
c = pm.candidates(rs, "Medina", "NY")
check("the same town returned as a boundary and a node counts once; a road and an Ohio Medina do not count", len(c) == 1 and abs(c[0][0] - 43.2203) < 1e-4, [(x[0], x[1]) for x in c])
check("'Village of Medina' is the town Medina", len(pm.candidates([dict(res("Village of Medina", 43.22, -78.39, "Orleans County", "New York", "boundary", "administrative"))], "Medina", "NY")) == 1)
check("a neighbourhood called 'Eldorado Hills' is not the town Eldorado", pm.candidates([rs[2]], "Eldorado", "IN") == [])
two = [res("LeRoy", 42.455, -88.763, "Boone County", "Illinois", "boundary", "administrative"), res("LeRoy", 40.348, -88.762, "McLean County", "Illinois", "place", "village")]
check("two LeRoys in Illinois come back in Nominatim's own order", [round(x[0], 1) for x in pm.candidates(two, "LeRoy", "IL")] == [42.5, 40.3])

print("\npin_missing: the plan - ADM's addresses, the feeds, the geocoder")
existing = {("ADM", "Mendota, IL (Northern Crossing Rail)"): (41.5473, -89.1176),
            ("Primient", "Decatur"): (39.85, -88.95), ("Primient", "Mattoon"): (39.48, -88.38), ("Primient", "Heyworth"): (40.31, -88.97),
            ("Hopkinsville Elevator", "Casky, KY"): (36.82, -87.43), ("Hopkinsville Elevator", "Hopkinsville, KY"): (36.87, -87.49), ("Hopkinsville Elevator", "Russellville, KY"): (36.85, -86.89),
            ("Landus", "A, IL"): (41.6, -88.2), ("Landus", "B, IL"): (41.7, -88.4), ("Landus", "C, IL"): (41.8, -88.1),
            ("CHS", "Alpha"): (45.0, -97.0), ("CHS", "Beta"): (45.1, -97.1), ("CHS", "Gamma"): (45.2, -97.2)}
FEEDS = {
    "FS Grain": [("https://www.fsgrain.com", {"name": "BUDA", "city": "Buda", "state": "IL"}, 41.3, -89.7), ("https://www.fsgrain.com", {"name": "Kentland, IN", "city": "Newark", "state": "IL"}, 41.545, -88.434)],
    "Garden City Coop": [("https://gccoop.agricharts.com", {"name": "Milepost Elevator", "city": "Ulysses", "state": "KS"}, 37.48277, -101.61699)],
    "Total Grain Marketing": [("https://totalgrain.agricharts.com", {"name": "Hillsboro, IN", "city": "Hillsboro", "state": "MO"}, 40.037, -87.152)],          # the feed's state field is its head office's
    "Dup Co": [("https://dup.example", {"name": n, "city": n, "state": "IA"}, 42.0, -93.0) for n in ("ALPHA", "BETA", "GAMMA", "DELTA")],
    "Two Co": [("https://two.example", {"name": "Twin", "city": "Twin", "state": "IA"}, 42.1, -93.1), ("https://two.example", {"name": "Twin", "city": "Twin", "state": "IA"}, 41.5, -92.0)],
}
ADM = {"blenheim, on": (42.335, -82.006, "ON"), "nowhere, ks": (45.0, -70.0, "ME")}
TABLE = {
    ("twin", "IA"): [res("Twin", 42.2, -93.2)],
    ("solo", "IA"): [res("Solo", 42.0, -93.0)],
    ("leroy", "IL"): two,
    ("twinsville", "IA"): [res("Twinsville", 41.0, -92.0, "Wapello County"), res("Twinsville", 43.0, -95.0, "Clay County")],
    ("skyline", "KY"): [res("Skyline", 37.0759, -82.9815, "Letcher County", "Kentucky")],
    ("mcleansboro", "IL"): [res("Mcleansboro", 38.0934, -88.5356, "Hamilton County", "Illinois")],
    ("farflung", "SD"): [res("Farflung", 43.0, -103.0, "Custer County", "South Dakota")],
    ("lewis", "NE"): [res("Lewis", 40.8158, -96.8376, "Lancaster County", "Nebraska")],
    ("eldorado", "IN"): [rs[2]],
    ("springfield", "MO"): [res("Springfield", 39.9242, -83.8088, "Clark County", "Ohio")],          # Nominatim answered with an Ohio Springfield: outside Missouri's border
    ("medina", "NY"): [res("Village of Medina", 43.2203, -78.3866, "Orleans County", "New York", "boundary", "administrative")],
    ("failtown", "IA"): None,
    ("manchester", "IA"): [res("Manchester", 42.4841, -91.4555, "Delaware County")],
    ("dyersville", "IA"): [res("Dyersville", 42.4845, -91.1226, "Dubuque County")],
    ("fremont", "MI"): [res("Fremont", 43.4678, -85.9411, "Newaygo County", "Michigan")],
    ("fairmont city", "IL"): [res("Fairmont City", 38.6498, -90.0932, "Saint Clair County", "Illinois")],
    ("fairbank", "IA"): [res("Fairbank", 42.6428, -92.0460, "Buchanan County")],
}
rows = [R("ADM", "Blenheim, ON", "ON"), R("ADM", "Nowhere, KS", "KS"), R("FS Grain", "Buda, IL", "IL"), R("Garden City Coop", "Milepost, KS", "KS"), R("Total Grain Marketing", "Hillsboro, IN", "IN"),
        R("FS Grain", "Kentland, IN", "IN"), R("Dup Co", "Alpha, IA", "IA"), R("Two Co", "Twin, IA", "IA"), R("Solo Co", "Solo, IA", "IA"), R("Primient", "LeRoy", "IL"),
        R("Nobody Co", "Twinsville, IA", "IA"), R("Hopkinsville Elevator", "Skyline, KY", "KY"), R("Landus", "Mcleansboro, IL", "IL"), R("CHS", "Farflung", "SD"), R("CPI", "Lewis, NE", "NE"),
        R("Keystone Cooperative", "Eldorado", "IN"), R("Wrongstate Co", "Springfield, MO", "MO"), R("Keystone Cooperative", "Fremont MI", "IN"), R("Western New York Energy", "Medina, NY", "NY"),
        R("Fail Co", "Failtown, IA", "IA"), R("Premier Cooperative", "Big River Dyersville", "WI"), R("Premier Cooperative", "Fairbanks", "IA"), R("Bunge", "Fairmont City", None),
        R("Mendota", "Mendota BN Rail", None), R("Mendota", "Clinton Proc", None), R("CGB", "CGB - Cross Country", None)]
osm = FakeOsm(TABLE)
P = pm.plan(rows, existing, adm=ADM, feeds=FEEDS, osm=osm)
pr, un = P.proposals, P.unplaced
g = lambda p, l: pr.get((p, l))
check("tier 0: ADM's own address places 'Blenheim, ON'", g("ADM", "Blenheim, ON") and g("ADM", "Blenheim, ON")["source"] == "adm-bootstrap" and g("ADM", "Blenheim, ON")["lat"] == 42.335)
check("... but an ADM address outside the state on file is not used ('Nowhere, KS' is not placed)", ("ADM", "Nowhere, KS") not in pr, g("ADM", "Nowhere, KS"))
check("tier 1: a feed location named BUDA places 'Buda, IL'", g("FS Grain", "Buda, IL") and g("FS Grain", "Buda, IL")["source"].startswith("feed:") and g("FS Grain", "Buda, IL")["lat"] == 41.3)
check("tier 1: the feed's 'Milepost Elevator' places 'Milepost, KS' (facility word stripped)", g("Garden City Coop", "Milepost, KS") and g("Garden City Coop", "Milepost, KS")["lon"] == -101.61699)
check("tier 1: Total Grain's 'Hillsboro, IN' is placed at the feed's pin although the feed's own state field says MO (the pin is checked against the state on file, IN)", g("Total Grain Marketing", "Hillsboro, IN")
      and g("Total Grain Marketing", "Hillsboro, IN")["lat"] == 40.037)
check("tier 1: a feed that puts 'Kentland, IN' in Illinois is not believed - and the geocoder is NOT asked for it", ("FS Grain", "Kentland, IN") in un and "outside IN" in un[("FS Grain", "Kentland, IN")]
      and ("Kentland", "IN") not in osm.calls, (un.get(("FS Grain", "Kentland, IN")), osm.calls))
check("tier 1: a feed with four locations on one pin (a head-office address) is not trusted", ("Dup Co", "Alpha, IA") not in pr and ("Dup Co", "Alpha, IA") in un)
check("tier 1: two different feed candidates fall through to the geocoder, and its answer replaces the earlier reason", g("Two Co", "Twin, IA") and g("Two Co", "Twin, IA")["source"] == "osm:Twin,IA"
      and ("Two Co", "Twin, IA") not in un)
check("tier 2: one same-name town in the state is placed", g("Solo Co", "Solo, IA") and g("Solo Co", "Solo, IA")["n_cand"] == 1)
check("tier 2: two LeRoys - the one nearest Primient's other Illinois pins (McLean County) wins over Nominatim's first (Boone County)", g("Primient", "LeRoy") and abs(g("Primient", "LeRoy")["lat"] - 40.348) < 1e-3
      and "nearest" in g("Primient", "LeRoy")["how"], g("Primient", "LeRoy"))
check("tier 2: two towns and nothing to compare - the first is taken and the row is listed as ambiguous", g("Nobody Co", "Twinsville, IA") and abs(g("Nobody Co", "Twinsville, IA")["lat"] - 41.0) < 1e-3
      and [a[0] for a in P.ambiguous] == [("Nobody Co", "Twinsville, IA")], P.ambiguous)
check("held: Hopkinsville's 'Skyline' (the only one OSM has is in Letcher County, 240 mi from the others) is held back, not written", ("Hopkinsville Elevator", "Skyline, KY") not in pr
      and [h[0] for h in P.held] == [("Hopkinsville Elevator", "Skyline, KY")] and "held back" in un[("Hopkinsville Elevator", "Skyline, KY")])
check("held: ACCEPT_FAR lets Landus's Mcleansboro (far, checked by hand) through", g("Landus", "Mcleansboro, IL") and g("Landus", "Mcleansboro, IL")["source"].startswith("osm:"))
check("held: a national provider's far-flung site is not subject to the rule", g("CHS", "Farflung") is not None)
check("held: HOLD keeps CPI's 'Lewis, NE' out although it is within 120 mi", ("CPI", "Lewis, NE") not in pr and "by hand" in un[("CPI", "Lewis, NE")], un.get(("CPI", "Lewis, NE")))
check("a neighbourhood ('Eldorado Hills') is not Keystone's Eldorado: not placed", ("Keystone Cooperative", "Eldorado") not in pr and "no settlement named Eldorado" in un[("Keystone Cooperative", "Eldorado")])
check("a Springfield found only in Illinois is not Missouri's: not placed", ("Wrongstate Co", "Springfield, MO") not in pr)
check("'Fremont MI' on a row labeled IN is flagged, not looked up", "the name says MI" in un[("Keystone Cooperative", "Fremont MI")] and ("Fremont", "IN") not in osm.calls and ("Fremont", "MI") not in osm.calls)
check("'Village of Medina' places Medina, NY", g("Western New York Energy", "Medina, NY") and abs(g("Western New York Energy", "Medina, NY")["lat"] - 43.2203) < 1e-4)
check("a failed geocoder request is transient: the row is not placed and is not remembered", ("Fail Co", "Failtown, IA") in un and ("Fail Co", "Failtown, IA") in P.transient)
check("ALIAS: Premier 'Big River Dyersville' on file as WI (the old provider-wide label) is looked up as Dyersville, IA, pinned there, reported as a wrong label, and its state left as it is",
      g("Premier Cooperative", "Big River Dyersville") and abs(g("Premier Cooperative", "Big River Dyersville")["lat"] - 42.4845) < 1e-4 and g("Premier Cooperative", "Big River Dyersville")["state"] == "WI"
      and P.label_issues == [("Premier Cooperative", "Big River Dyersville", "WI", "IA")], P.label_issues)
check("ALIAS: Premier 'Fairbanks' is the POET plant at Fairbank, IA", g("Premier Cooperative", "Fairbanks") and g("Premier Cooperative", "Fairbanks")["source"] == "osm:Fairbank,IA")
check("ALIAS: Bunge 'Fairmont City' had no state: it gets IL with its pin", g("Bunge", "Fairmont City") and g("Bunge", "Fairmont City")["state"] == "IL")
check("ALIAS copy: 'Mendota BN Rail' takes ADM's Mendota pin and IL; 'Clinton Proc' (source has no pin) is not placed", g("Mendota", "Mendota BN Rail") and g("Mendota", "Mendota BN Rail")["lat"] == 41.5473
      and g("Mendota", "Mendota BN Rail")["state"] == "IL" and ("Mendota", "Clinton Proc") in un)
check("a name that is not a place is listed as such, not looked up", un[("CGB", "CGB - Cross Country")] == "not a place name")

print("\npin_missing: reading location_meta (rows are dicts on Snowflake; the SQLite rows of the tests unpack as tuples, which once hid a bug)")


class DictDb:
    """A database whose rows are dicts only - what Snowflake's cursor gives back through database.get_all_location_meta()."""

    def get_all_location_meta(self):
        return [{"provider": "P", "location": "B pinned", "state": "ia", "facility_type": "", "region": "", "lat": 42.5, "lon": -93.5},
                {"provider": "P", "location": "A none", "state": "", "facility_type": "Country Elevator", "region": "", "lat": None, "lon": None},
                {"provider": "O", "location": "half", "state": "IL ", "facility_type": "", "region": "", "lat": 40.0, "lon": None}]


rws, ex = pm.load_rows(DictDb())
check("unpinned = either coordinate missing, state upper-cased / blank -> None, sorted by provider then location",
      [(r["provider"], r["location"], r["state"], r["facility_type"]) for r in rws] == [("O", "half", "IL", None), ("P", "A none", None, "Country Elevator")], rws)
check("pinned rows come back as {(provider, location): (lat, lon)}", ex == {("P", "B pinned"): (42.5, -93.5)}, ex)

print("\npin_missing: other spellings of the same town (OSM writes LeRoy as 'Le Roy') are asked too; other towns a name lists are not")
check("spelling(): LeRoy = Le Roy, Ft. X = Fort X, St X = Saint X, Mt. X = Mount X, but Hitchcock != Tulare",
      pm.spelling("LeRoy") == pm.spelling("Le Roy") and pm.spelling("Ft. Dodge") == pm.spelling("Fort Dodge") and pm.spelling("St Joseph") == pm.spelling("Saint Joseph")
      and pm.spelling("Mt. Olive") == pm.spelling("Mount Olive") and pm.spelling("Hitchcock") != pm.spelling("Tulare"))
boone = res("Leroy Township", 42.4551, -88.7634, "Boone County", "Illinois", "boundary", "administrative")
mclean = res("Le Roy", 40.3478, -88.7615, "McLean County", "Illinois", "place", "city")
LR = {("leroy", "IL"): [boone], ("le roy", "IL"): [mclean], ("hitchcock", "SD"): [res("Hitchcock", 44.63, -98.40, "Beadle County", "South Dakota")],
      ("tulare", "SD"): [res("Tulare", 44.74, -98.51, "Spink County", "South Dakota")]}
ex_lr = {("Primient", "Decatur"): (39.85, -88.95), ("Primient", "Mattoon"): (39.48, -88.38), ("Primient", "Heyworth"): (40.31, -88.97)}
osm_lr = FakeOsm(LR)
P3 = pm.plan([R("Primient", "LeRoy", "IL"), R("Dakota Co", "Hitchcock-Tulare, SD", "SD")], ex_lr, osm=osm_lr)
lr = P3.proposals.get(("Primient", "LeRoy"))
check("Primient 'LeRoy': the first spelling finds only Leroy Township in Boone County (155 mi off); 'Le Roy' is asked too and McLean County, beside Heyworth, wins", lr and abs(lr["lat"] - 40.3478) < 1e-3
      and lr["n_cand"] == 2 and ("Le Roy", "IL") in osm_lr.calls and not P3.held, (lr, osm_lr.calls))
check("'Hitchcock-Tulare, SD' is placed at Hitchcock, and Tulare (another town) is not asked", P3.proposals.get(("Dakota Co", "Hitchcock-Tulare, SD")) and ("Tulare", "SD") not in osm_lr.calls, osm_lr.calls)


class HalfOsm(FakeOsm):
    def lookup(self, city, st):
        r = super().lookup(city, st)
        return None if city == "Le Roy" else r


P4 = pm.plan([R("Primient", "LeRoy", "IL")], ex_lr, osm=HalfOsm(LR))
check("the first spelling answered but the second request failed: not placed on half an answer (the spelling not yet heard might hold the nearer town), asked again next run",
      ("Primient", "LeRoy") not in P4.proposals and ("Primient", "LeRoy") in P4.transient and ("Primient", "LeRoy") not in P4.held)

print("\npin_missing: the budget")
o2 = FakeOsm({("aa", "IA"): [res("Aa", 42, -93)], ("bb", "IA"): [res("Bb", 42.1, -93.1)]})
P2 = pm.plan([R("X", "Aa, IA", "IA"), R("X", "Bb, IA", "IA")], {}, osm=o2, max_lookups=1)
check("with a budget of one request the second row is left for next week, not given up on", ("X", "Aa, IA") in P2.proposals and ("X", "Bb, IA") in P2.transient and o2.requests == 1)

print("\npin_missing: the job, on a throwaway SQLite")
db.init_db()
conn = db.get_conn()
c = conn.cursor()
c.execute("DELETE FROM location_meta")
conn.commit()
conn.close()
db.upsert_location_meta("Premier Cooperative", "Manchester", state="IA", facility_type="Country Elevator")
db.upsert_location_meta("Bunge", "Fairmont City", facility_type="River Terminal")
db.upsert_location_meta("Primient", "LeRoy", state="IL", facility_type="Corn Processing")
for loc, la, lo in (("Decatur", 39.85, -88.95), ("Mattoon", 39.48, -88.38), ("Heyworth", 40.31, -88.97)):
    db.upsert_location_meta("Primient", loc, state="IL", facility_type="Corn Processing", lat=la, lon=lo)
db.upsert_location_meta("Keystone Cooperative", "Fremont MI", state="IN", facility_type="Country Elevator")
db.upsert_location_meta("Futures Co", "Futures-Only", facility_type="Country Elevator")
db.upsert_location_meta("Pinned Co", "Already, IA", state="IA", facility_type="Country Elevator", lat=42.0, lon=-93.0)
state_dir = Path(tempfile.mkdtemp(prefix="pin_state_"))
NOW = datetime(2026, 10, 30, 22, 45, tzinfo=timezone.utc)
mails = []
osm3 = FakeOsm(TABLE)


def go(extra=(), when=NOW, send=None, osm_=None):
    args = pm.parse_args(["--allow-sqlite", "--state-dir", str(state_dir)] + list(extra))
    return pm.run(args, send=send or (lambda s, h: mails.append((s, h))), now=when, osm=osm_ or osm3, adm={}, feeds={}, db=db)


def meta(p, l):
    cc = db.get_conn()
    cu = cc.cursor()
    cu.execute("SELECT state, lat, lon FROM location_meta WHERE provider = %s AND location = %s" % (db._ph(), db._ph()), (p, l))
    r = cu.fetchone()
    cc.close()
    return tuple(r) if r else None


(state_dir / "pin_missing.off").write_text("x")
check("the kill switch stops the job: nothing written", go() == 0 and meta("Premier Cooperative", "Manchester")[1] is None and not mails)
(state_dir / "pin_missing.off").unlink()
check("--dry-run writes nothing, mails nothing, and leaves no state file", go(["--dry-run"]) == 0 and meta("Premier Cooperative", "Manchester")[1] is None and not mails and not (state_dir / "pin_missing.json").exists())
try:
    pm.run(pm.parse_args(["--state-dir", str(state_dir)]), now=NOW, osm=osm3, adm={}, feeds={}, db=db)
    refused = False
except RuntimeError:
    refused = True
check("it refuses to run against a local SQLite unless told to", refused)

check("run 1 exits 0", go() == 0)
m1 = meta("Premier Cooperative", "Manchester")
check("run 1: Premier 'Manchester' is pinned in Iowa; Bunge 'Fairmont City' is pinned and now has state IL; Primient 'LeRoy' got the McLean County one",
      m1[1] is not None and abs(m1[1] - 42.4841) < 1e-4 and meta("Bunge", "Fairmont City")[0] == "IL" and abs(meta("Primient", "LeRoy")[1] - 40.348) < 1e-3, (m1, meta("Bunge", "Fairmont City"), meta("Primient", "LeRoy")))
check("run 1: rows it could not place stay unpinned ('Fremont MI' on file as IN, 'Futures-Only'); a row that already had a pin is untouched",
      meta("Keystone Cooperative", "Fremont MI")[1] is None and meta("Futures Co", "Futures-Only")[1] is None and meta("Pinned Co", "Already, IA") == ("IA", 42.0, -93.0))
check("run 1: one mail to the operator: 3 placed, 1 to place by hand (Fremont MI); the name that is not a place ('Futures-Only') is not mailed, but still counts as open", len(mails) == 1
      and "3 placed" in mails[0][0] and "1 to place by hand" in mails[0][0] and "Futures-Only" not in mails[0][1] and "Fremont MI" in mails[0][1] and "Manchester" in mails[0][1]
      and "2 locations still have no pin" in mails[0][1], mails[:1])
st1 = json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))
check("run 1: the two it gave up on are remembered, and marked mailed", sorted(st1["unplaced"]) == ["Futures Co|Futures-Only", "Keystone Cooperative|Fremont MI"] and all(v["emailed"] for v in st1["unplaced"].values()), st1)

calls_before = osm3.requests
check("run 2 (a week later): nothing to try, nothing mailed, no geocoder request", go(when=NOW + timedelta(days=7)) == 0 and len(mails) == 1 and osm3.requests == calls_before)
check("run 3 (31 days later): the two are tried again; the answer is the same, so nothing is mailed again; 'first' is kept", go(when=NOW + timedelta(days=31)) == 0 and len(mails) == 1
      and json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"]["Futures Co|Futures-Only"]["first"] == "2026-10-30")

db.upsert_location_meta("Fairbank Co", "Solo, IA", state="IA", facility_type="Country Elevator")
check("run 4: a new location is pinned and mailed on its own ('1 placed')", go(when=NOW + timedelta(days=38)) == 0 and meta("Fairbank Co", "Solo, IA")[1] is not None and len(mails) == 2 and "1 placed" in mails[1][0], mails[1:])

db.upsert_location_meta("Springfield Co", "Springfield, MO", state="MO", facility_type="Country Elevator")


def boom(subject, html):
    raise RuntimeError("smtp down")


go(when=NOW + timedelta(days=45), send=boom)
check("a failed mail does not undo the work: the row it could not place is remembered as NOT mailed", json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"]
      ["Springfield Co|Springfield, MO"]["emailed"] is False)
n_before = len(mails)
go(when=NOW + timedelta(days=52))
check("the next run mails it (even though the row is inside its 30 days) and then marks it mailed", len(mails) == n_before + 1 and "Springfield, MO" in mails[-1][1]
      and json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"]["Springfield Co|Springfield, MO"]["emailed"] is True)

for loc, la, lo in (("Casky, KY", 36.82, -87.43), ("Hopkinsville, KY", 36.87, -87.49), ("Russellville, KY", 36.85, -86.89)):
    db.upsert_location_meta("Hopkinsville Elevator", loc, state="KY", facility_type="Country Elevator", lat=la, lon=lo)
db.upsert_location_meta("Hopkinsville Elevator", "Skyline, KY", state="KY", facility_type="Country Elevator")
n_before = len(mails)
go(when=NOW + timedelta(days=60))
check("a held-back row (Skyline: the only one OSM has is 240 mi from the others) is not pinned and is mailed once - in the held table, not also under 'could not be placed'",
      meta("Hopkinsville Elevator", "Skyline, KY")[1] is None and len(mails) == n_before + 1 and "held back for a look" in mails[-1][1] and "could not be placed</b>" not in mails[-1][1]
      and "1 held back" in mails[-1][0], mails[-1:])
n_before = len(mails)
go(when=NOW + timedelta(days=91))
check("31 days later it is tried again, held again for the same reason, and NOT mailed again (nothing else is new)", len(mails) == n_before and meta("Hopkinsville Elevator", "Skyline, KY")[1] is None, mails[-1:])

db.upsert_location_meta("Keystone Cooperative", "Fremont MI", state="MI", facility_type="Country Elevator", lat=43.47, lon=-85.95)
go(when=NOW + timedelta(days=92))
check("a remembered row that somebody pinned by hand is dropped from the books",
      "Keystone Cooperative|Fremont MI" not in json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"] and len(mails) == n_before)

db.upsert_location_meta("Quiet Co", "Nowhereville, IA", state="IA", facility_type="Country Elevator")
go(["--no-email"], when=NOW + timedelta(days=93))
check("--no-email mails nothing, and what it found counts as seen", len(mails) == n_before and json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"]
      ["Quiet Co|Nowhereville, IA"]["emailed"] is True)
go(when=NOW + timedelta(days=94))
check("... so the next run does not mail it either", len(mails) == n_before)

db.upsert_location_meta("Fail Co", "Failtown, IA", state="IA", facility_type="Country Elevator")
before = osm3.requests
go(when=NOW + timedelta(days=95))
check("a failed geocoder request is not written off: no mail, not in the books", len(mails) == n_before and osm3.requests == before + 1
      and "Fail Co|Failtown, IA" not in json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"])
go(when=NOW + timedelta(days=96))
check("... and it is asked again on the very next run", osm3.requests == before + 2 and len(mails) == n_before)

db.upsert_location_meta("Label Co", "Fremont MI", state="IN", facility_type="Country Elevator")
go(when=NOW + timedelta(days=97))
rec = json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"].get("Label Co|Fremont MI")
check("Keystone's case: 'Fremont MI' filed under IN is given up on (the name says MI) and remembered with the state it was filed under", rec and rec["state"] == "IN" and rec["emailed"] is True
      and meta("Label Co", "Fremont MI")[1] is None, rec)
db.upsert_location_meta("Label Co", "Fremont MI", state="MI")
n_before = len(mails)
go(when=NOW + timedelta(days=98))
check("the label is corrected to MI: it is tried again the next day, not 30 days on, and pinned at Fremont, Michigan", meta("Label Co", "Fremont MI")[1] is not None
      and abs(meta("Label Co", "Fremont MI")[1] - 43.4678) < 1e-4 and len(mails) == n_before + 1 and "1 placed" in mails[-1][0], (meta("Label Co", "Fremont MI"), mails[-1:]))
check("... and leaves the books at once", "Label Co|Fremont MI" not in json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))["unplaced"])

print("\npin_missing: the mail")
subj, html = pm.build_email([(("A&B <Co>", "Town, IA"), {"lat": 42.0, "lon": -93.0, "source": "osm:Town,IA", "state": "IA"})], [("P|L", "no settlement named X inside IA")], [], [], 12, "2026-10-30", False)
check("the mail escapes what it prints and says how many are still open", "A&amp;B &lt;Co&gt;" in html and "12 locations still have no pin" in html and "1 placed" in subj)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
