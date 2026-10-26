"""pin_missing.py - gives a map pin to every location_meta row that has none (weekly droplet job; Kolten 2026-10-26: "Both" - fix the labels and build the weekly job).

Why a row has no pin: pins were assigned once in June by a manual name-only Nominatim script (geocode_locations.py), frozen into coords_seed.json and loaded into Snowflake at migration; nothing re-runs it,
init_db returns before seeding on Snowflake, and a scraper creates a location_meta row with a state and a type but no coordinates. get_map_data needs a pin, so those rows showed in the Bids tab and not on
the map. The 2026-10-25 one-time pass pinned the 586 that could be placed; this is the same method on a schedule, for whatever a scraper adds next. In order of trust:

  tier 0  ADM's own Google-geocoded addresses (the bootstrap call adm_scraper already makes), matched by display name;
  tier 1  the coordinates every AgriCharts cashbids-js.php feed carries per location (agricharts_scraper.TENANTS + agricharts_md_scraper.SITES hosts), matched by name inside the provider's own host;
  tier 2  Nominatim, structured (city + state, never a bare name - bare-name lookups are what put Mendota in California), accepted only when the result is a settlement whose name is the city AND sits
          inside the state's bounding box; when the state has several such towns the one nearest the provider's other pins wins (the town's other spellings are asked too: OSM writes LeRoy as 'Le Roy');
  ALIAS   hand-made readings for names that are not a town ('Meredith Rd Elburn', 'Homeland (Lawler)').

Every pin is checked against its state's border and against the provider's other locations (more than 120 miles from them: held back, not written). What it cannot place is listed and left alone - never
guessed - and retried after 30 days. It writes lat / lon only (and the state, for a row that had none, when an ALIAS names it). The operator (CHANGES_EMAIL_TO) is mailed only when something changed or
something needs a look; a run with nothing to do is silent.

Switches: state/pin_missing.off (touch it) stops the job; --dry-run writes and mails nothing; --no-email; --retry-all re-tries what it gave up on. Undo a run: set lat = lon = NULL on the rows it mailed.
Add a hand-made reading: ALIAS below. A pin held back as 'far' that is right: ACCEPT_FAR. One that is wrong: HOLD.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dotenv import load_dotenv                                       # noqa: E402

load_dotenv(HERE / ".env")

import requests                                                      # noqa: E402

STATE_DIR = HERE / "state"
FAR_MILES = 120          # an OSM pin this far from the provider's other pins in the same state is held back
RETRY_DAYS = 30          # a row that could not be placed is tried again after this long
MAX_LOOKUPS = 400        # new Nominatim requests in one run
UA = "jsa-basis-tracker-pin-job/1.0 (+https://github.com/JSA-Dashboards/basis-tracker)"

# (lat_min, lat_max, lon_min, lon_max): outer extents of each state / province; a pin must fall inside its state's box (+0.1 degree)
BOX = {
    "AL": (30.14, 35.01, -88.47, -84.89), "AZ": (31.33, 37.00, -114.82, -109.04), "AR": (33.00, 36.50, -94.62, -89.64), "CA": (32.53, 42.01, -124.41, -114.13),
    "CO": (36.99, 41.00, -109.06, -102.04), "CT": (40.95, 42.05, -73.73, -71.79), "DE": (38.45, 39.84, -75.79, -75.05), "FL": (24.40, 31.00, -87.63, -79.97),
    "GA": (30.36, 35.00, -85.61, -80.84), "ID": (41.99, 49.00, -117.24, -111.04), "IL": (36.97, 42.51, -91.51, -87.02), "IN": (37.77, 41.76, -88.10, -84.78),
    "IA": (40.38, 43.50, -96.64, -90.14), "KS": (36.99, 40.00, -102.05, -94.59), "KY": (36.50, 39.15, -89.57, -81.96), "LA": (28.93, 33.02, -94.04, -88.82),
    "ME": (43.06, 47.46, -71.08, -66.95), "MD": (37.91, 39.72, -79.49, -75.05), "MA": (41.24, 42.89, -73.51, -69.93), "MI": (41.70, 48.31, -90.42, -82.12),
    "MN": (43.50, 49.38, -97.24, -89.49), "MS": (30.17, 35.00, -91.66, -88.10), "MO": (35.99, 40.61, -95.77, -89.10), "MT": (44.36, 49.00, -116.05, -104.04),
    "NE": (40.00, 43.00, -104.05, -95.31), "NV": (35.00, 42.00, -120.01, -114.04), "NH": (42.70, 45.31, -72.56, -70.70), "NJ": (38.93, 41.36, -75.56, -73.89),
    "NM": (31.33, 37.00, -109.05, -103.00), "NY": (40.50, 45.02, -79.76, -71.86), "NC": (33.84, 36.59, -84.32, -75.46), "ND": (45.93, 49.00, -104.05, -96.55),
    "OH": (38.40, 41.98, -84.82, -80.52), "OK": (33.62, 37.00, -103.00, -94.43), "OR": (41.99, 46.29, -124.57, -116.46), "PA": (39.72, 42.52, -80.52, -74.69),
    "RI": (41.15, 42.02, -71.91, -71.12), "SC": (32.03, 35.22, -83.35, -78.54), "SD": (42.48, 45.95, -104.06, -96.44), "TN": (34.98, 36.68, -90.31, -81.65),
    "TX": (25.84, 36.50, -106.65, -93.51), "UT": (37.00, 42.00, -114.05, -109.04), "VT": (42.73, 45.02, -73.44, -71.46), "VA": (36.54, 39.47, -83.68, -75.24),
    "WA": (45.54, 49.00, -124.85, -116.92), "WV": (37.20, 40.64, -82.64, -77.72), "WI": (42.49, 47.31, -92.89, -86.25), "WY": (40.99, 45.01, -111.06, -104.05),
    "ON": (41.67, 56.86, -95.16, -74.32), "MB": (48.99, 60.00, -102.05, -88.94), "SK": (48.99, 60.00, -110.00, -101.36), "AB": (48.99, 60.00, -120.00, -110.00),
    "QC": (44.99, 62.60, -79.76, -57.10), "BC": (48.30, 60.00, -139.06, -114.03),
}
NAMES = {"AL": "Alabama", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "ID": "Idaho",
         "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
         "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
         "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
         "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
         "ON": "Ontario", "MB": "Manitoba", "SK": "Saskatchewan", "AB": "Alberta", "QC": "Quebec", "BC": "British Columbia"}
CANADA = {"ON", "MB", "SK", "AB", "QC", "BC"}
NATIONAL = ("ADM", "CHS", "Cargill", "Bunge", "CGB")        # providers whose locations are spread across the country: the 'far from the others' rule does not apply


def inside(lat, lon, st, tol=0.10):
    b = BOX.get(st)
    return bool(b) and b[0] - tol <= lat <= b[1] + tol and b[2] - tol <= lon <= b[3] + tol


def hav(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(x))


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def norm(s):
    s = re.sub(r"\(.*?\)", " ", s or "")
    s = re.sub(r",\s*[A-Z]{2}\b.*$", "", s)
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9]", "", s.lower())


# --------------------------------------------------------------------------------------------------- hand-made readings
# (provider, location) -> ("place", city, state-or-None) to look that town up (None = the state on file), or ("copy", provider, location, state) to reuse an existing pin.
ALIAS = {
    ("CHS", "Meredith Rd Elburn"): ("place", "Elburn", "IL"),
    ("CHS", "Nestle Davenport"): ("place", "Davenport", "IA"),
    ("CHS", "Worthing Ag Grain"): ("place", "Worthing", "SD"),
    ("CHS", "Delivered Tharaldson"): ("place", "Casselton", "ND"),
    ("CHS", "Hallock Drayton"): ("place", "Hallock", "MN"),
    ("Premier Cooperative", "Big River Dyersville"): ("place", "Dyersville", "IA"),
    ("Premier Cooperative", "Homeland (Lawler)"): ("place", "Lawler", "IA"),
    ("Premier Cooperative", "Valero CC"): ("place", "Charles City", "IA"),
    ("Premier Cooperative", "Viserion Mcgregor"): ("place", "McGregor", "IA"),
    ("Premier Cooperative", "Ft Atkinson/Waucoma"): ("place", "Fort Atkinson", "IA"),
    ("Premier Cooperative", "Fairbanks"): ("place", "Fairbank", "IA"),          # the POET plant at Fairbank, IA
    ("Bunge", "Fairmont City"): ("place", "Fairmont City", "IL"),
    ("See-Mor", "Bunge Warren, IL"): ("place", "Warren", "IL"),
    ("Cargill", "Solae - Gibson City"): ("place", "Gibson City", "IL"),
    ("Cargill", "Fort Dodge"): ("place", "Fort Dodge", "IA"),
    ("Mendota", "Mendota BN Rail"): ("copy", "ADM", "Mendota, IL (Northern Crossing Rail)", "IL"),
    ("Mendota", "Clinton Proc"): ("copy", "ADM", "Clinton, IA (Corn Processing)", "IA"),
    ("Bartlett", "KC - Fairfax Terminal"): ("place", "Kansas City", None),
    ("Bartlett", "KC - KCT Terminal"): ("place", "Kansas City", None),
    ("Bartlett", "KC RR Terminal"): ("place", "Kansas City", None),
    ("Star of West", "SOTW Charlotte Group"): ("place", "Charlotte", "MI"),
    ("Star of West", "Frankenmuth Area"): ("place", "Frankenmuth", "MI"),
    ("Star of West", "Rapson/Cass City"): ("place", "Cass City", "MI"),
    ("Agtegra", "AGP Aberdeen"): ("place", "Aberdeen", "SD"),
    ("Agtegra", "Redfield Energy"): ("place", "Redfield", "SD"),
    ("Agtegra", "Redfield Elevator"): ("place", "Redfield", "SD"),
    ("Dawson Grain", "AGP Dawson, MN"): ("place", "Dawson", "MN"),
    ("Scoular", "Tribune - Main / NW / Horace"): ("place", "Tribune", "KS"),
    ("Scoular", "Scoular-Downs"): ("place", "Downs", "KS"),
    ("Scoular", "Port of Richmond, VA"): ("place", "Richmond", "VA"),
    ("Harvestone", "Blue Flint"): ("place", "Underwood", "ND"),
    ("Harvestone", "Dakota Spirit"): ("place", "Spiritwood", "ND"),
    ("Big River", "Aledo/Edgington, IL"): ("place", "Aledo", "IL"),
    ("Big River", "W. Burlington, IA"): ("place", "West Burlington", "IA"),
    ("Cooperative Producers", "Grand Island, NE"): ("place", "Grand Island", "NE"),
    ("Alto", "Alto Pekin, IL"): ("place", "Pekin", "IL"),
    ("One Earth", "One Earth Energy, IL"): ("place", "Gibson City", "IL"),
    ("CGB", "NORTH DAKOTA SOYBEAN PROCESSORS LLC"): ("place", "Casselton", "ND"),
}
# far from the provider's other pins and checked by hand as right (the provider really has a site there)
ACCEPT_FAR = {("Agtegra", "AGP Dawson"), ("Big River", "Boyceville, WI"), ("Jennie-O", "Barron, WI"), ("Jennie-O", "Dawson, MN"), ("Jennie-O", "Perham, MN"), ("Landus", "Mcleansboro, IL"),
              ("Landus", "Rushville, IN"), ("Prairie Grain Partners", "Boyd, MN"), ("Prairie Grain Partners", "Clarkfield, MN"), ("Primient", "Loudon"), ("Star of West", "Churchville"),
              ("Star of West", "Willard"), ("Scoular", "Goodland Crush, KS"), ("Scoular", "Tribune - Main / NW / Horace"), ("Scoular", "Scoular-Downs"), ("Bartlett", "Cherryvale"),
              ("Bartlett", "Great Bend"), ("Bartlett", "Healy"), ("Bartlett", "Scott City"), ("Bartlett", "Wichita")}
# looked at by hand and NOT trusted, although within FAR_MILES
HOLD = {("CPI", "Lewis, NE"): "the only 'Lewis' OSM knows in Nebraska is near Lincoln, 95 mi from CPI's other elevators around Hastings"}
SKIP_RE = re.compile(r"futures[- ]?only|direct\s*haul|cross\s*country|nil\s+direct|to buy for|west fob|\bfob\b|bocker ruff|bacres|green bison|ibec|sun terminal|west terminal|agp west|"
                     r"semo milling|north dakota soybean processors|gene cooper|legacy feed|farm elevator|fairground|bunge pdc|absolute energy|dump fee|wheeler elevator|"
                     r"hills terminal|see-mor|badger state|ace ethanol|scircleville|\bgsi-(brickyard|echo valley|hillcrest|rodene)", re.I)
QUIET = {"not a place name"}          # reasons nobody can act on (SKIP_RE is hand-made): remembered so they are not tried again, never mailed
# name words that mean 'a facility', stripped from the end of a name before it is looked up as a town
SUFFIX = re.compile(r"\s+(?:Elevator|Terminal|Grain|Energy|Ethanol|Milling|Mill|Coop|Co-op|Feed|Area|Transloader|Shuttle|Cash Bids?|Group|Proc|Crush|Plant|Processing|Rail|Downtown|"
                    r"North|South|East|West|Central|Elev)\s*$", re.I)
PREFIX = re.compile(r"^(?:AGP|CGB|CHS|CFE|GSI|SOTW|ZFS|Scoular|Mennel Milling|Mennel|Big River)\s*[-–]?\s+", re.I)
STOPCITY = {"", "west", "east", "north", "south", "sun", "route 30", "main", "farm", "legacy", "fairground"}


def city_variants(location):
    """The town names to try for a location name, in order; with the state the name itself carries (', IA' or a trailing 'MI')."""
    s = (location or "").strip()
    st = None
    m = re.match(r"^(.+?),\s*([A-Z]{2})\b", s)
    if m:
        s, st = m.group(1).strip(), m.group(2)
    else:
        m = re.match(r"^(.+?)\s+([A-Z]{2})$", s)
        if m and m.group(2) in BOX:
            s, st = m.group(1).strip(), m.group(2)
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"^.*\bat\s+", "", s)                 # 'Mill 240 at Tarkio'
    s = PREFIX.sub("", s).strip()
    s = re.sub(r"\s+-\s+.*$", "", s)                 # 'Tribune - Main / NW'
    parts = [p.strip() for p in re.split(r"/|\s&\s", s) if p.strip()] or [s]
    out = []
    for p in parts:
        for q in re.split(r"(?<=\w)-(?=\w)", p) if "-" in p and not p.lower().startswith(("st-", "mt-")) else [p]:
            q = q.strip()
            for _ in range(3):
                q2 = SUFFIX.sub("", q).strip()
                if q2 == q:
                    break
                q = q2
            for v in (q, re.sub(r"^Ft\.?\s", "Fort ", q), re.sub(r"^Mt\.?\s", "Mount ", q), re.sub(r"^St\.?\s", "Saint ", q), re.sub(r"^St\.?\s", "St. ", q),
                      re.sub(r"^(Le|La|De)([A-Z][a-z])", r"\1 \2", q)):          # OSM writes LeRoy as 'Le Roy'
                v = v.strip()
                if v and v not in out and v.lower() not in STOPCITY:
                    out.append(v)
    return out, st


# --------------------------------------------------------------------------------------------------- Nominatim (structured, cached)
class Osm:
    """Structured Nominatim lookups, cached in a JSON file, one request per 1.25 s, a polite User-Agent. lookup() -> list of results, or None when the request failed (not cached: tried again next run)."""

    def __init__(self, cache_path: Path, session=None, sleep=time.sleep, clock=time.monotonic):
        self.path = Path(cache_path)
        try:
            self.cache = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self.session, self.sleep, self.clock = session or requests, sleep, clock
        self.requests = 0
        self._last = None

    @staticmethod
    def key(city, st):
        return "%s|%s" % (city.lower(), st)

    def cached(self, city, st):
        return self.key(city, st) in self.cache

    def lookup(self, city, st):
        k = self.key(city, st)
        if k in self.cache:
            return self.cache[k]
        if self._last is not None:
            wait = 1.25 - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        params = {"city": city, "state": NAMES[st], "countrycodes": "ca" if st in CANADA else "us", "format": "jsonv2", "limit": 6, "addressdetails": 1}
        res = None
        for attempt in range(3):
            try:
                self._last = self.clock()
                self.requests += 1
                r = self.session.get("https://nominatim.openstreetmap.org/search", params=params, headers={"User-Agent": UA}, timeout=30)
                if r.status_code == 429:
                    self.sleep(20)
                    continue
                r.raise_for_status()
                res = r.json()
                break
            except Exception:
                self.sleep(5 * (attempt + 1))
        if res is None:
            return None
        self.cache[k] = res
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.cache), encoding="utf-8")
        except OSError:
            pass
        return res


def candidates(results, city, st):
    """Every settlement result whose name is the city and whose point is inside the state, in Nominatim's own (importance) order, the same town returned twice (a node and a boundary) once."""
    want = norm(city)
    out = []
    for x in results or []:
        if x.get("category") not in ("place", "boundary"):
            continue
        addr = x.get("address") or {}
        names = {norm(x.get("name") or "")} | {norm(addr.get(k) or "") for k in ("city", "town", "village", "hamlet", "municipality", "suburb", "locality")}
        names = {re.sub(r"^(villageof|cityof|townof)", "", n) for n in names if n} | names          # OSM names some as 'Village of Medina'
        names = {re.sub(r"(township|city|village|town|county)$", "", n) for n in names if n} | names
        if want not in names and re.sub(r"(township|city|village|town)$", "", want) not in names:
            continue
        la, lo = float(x["lat"]), float(x["lon"])
        if inside(la, lo, st) and all(hav(la, lo, c[0], c[1]) > 3 for c in out):
            out.append((la, lo, x))
    return out


def spelling(v):
    """A town name with the spellings OSM and the bid sheets disagree on folded together: 'LeRoy' = 'Le Roy', 'Ft X' = 'Fort X', 'St. X' = 'Saint X', 'Mt X' = 'Mount X'."""
    s = (v or "").strip().lower()
    s = re.sub(r"^ft\.?\s", "fort ", s)
    s = re.sub(r"^mt\.?\s", "mount ", s)
    s = re.sub(r"^st\.?\s", "saint ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def county_of(x):
    d = x.get("display_name") or ""
    return d.split(",")[1].strip() if d.count(",") >= 2 else ""


# --------------------------------------------------------------------------------------------------- the other sources
def feed_host_map(providers=None):
    """{host: set(providers)} for every AgriCharts host the scrapers read; only those of `providers` when given."""
    from urllib.parse import urlparse
    import agricharts_md_scraper as md
    import agricharts_scraper as ag
    hosts = defaultdict(set)
    for s in md.SITES:
        u = urlparse(s["url"])
        hosts["%s://%s" % (u.scheme, u.netloc)].add(s["provider"])
    for t in ag.TENANTS:
        hosts[t["host"]].add(t["provider"])
    return {h: p for h, p in hosts.items() if providers is None or p & set(providers)}


def fetch_feeds(providers, session=None, sleep=time.sleep):
    """{provider: [(host, location dict, lat, lon)]} from each needed host's cashbids-js.php (every location carries latitude / longitude)."""
    import agricharts_scraper as ag
    session = session or requests
    out = defaultdict(list)
    for host, provs in sorted(feed_host_map(providers).items()):
        try:
            r = session.get(host + "/inc/cashbids/cashbids-js.php" + ag._QS, headers=ag._HEADERS, timeout=30)
            m = ag._BIDS_RE.search(r.text)
            feed = json.loads(m.group(1)) if m else []
        except Exception:
            continue
        for l in feed:
            try:
                la, lo = float(l.get("latitude")), float(l.get("longitude"))
            except (TypeError, ValueError):
                continue
            if (la, lo) == (0.0, 0.0) or str(l.get("hide_on_sites_and_apis") or "") in ("1", "true"):
                continue
            keep = {k: l.get(k) for k in ("name", "display_name", "city", "state")}
            for p in provs:
                out[p].append((host, keep, la, lo))
        sleep(0.5)
    return dict(out)


def fetch_adm():
    """{display name lower: (lat, lon, state)} from ADM's Gradable bootstrap (each market carries a Google-geocoded address)."""
    import httpx
    import adm_scraper as am
    with httpx.Client(headers=am._HEADERS, timeout=30, follow_redirects=True) as client:
        r = client.get(am.BOOTSTRAP_URL)
        r.raise_for_status()
        boot = am._parse_json(r)
    out = {}
    for m in boot.get("markets", []):
        g = ((m.get("address") or {}).get("geocode") or {})
        if g.get("lat") is not None:
            out[str(m.get("display_name", "")).strip().lower()] = (g["lat"], g["lng"], ((m.get("address") or {}).get("fbn_normalized") or {}).get("country_subdivision"))
    return out


# --------------------------------------------------------------------------------------------------- the plan (pure: every source is an argument)
class Plan:
    def __init__(self):
        self.proposals = {}          # (provider, location) -> {lat, lon, source, state, ...}
        self.unplaced = {}           # key -> reason
        self.transient = set()       # keys that failed for a reason that may pass (a failed request): not remembered
        self.held = []               # (key, proposal, miles)
        self.label_issues = []       # (provider, location, on file, really)
        self.ambiguous = []          # picked the first of several same-name towns with nothing to compare


def plan(rows, existing, *, adm=None, feeds=None, osm=None, max_lookups=MAX_LOOKUPS) -> Plan:
    """rows: [{provider, location, state, facility_type}] without a pin; existing: {(provider, location): (lat, lon)} of every pinned row."""
    P = Plan()
    prop, fail = P.proposals, P.unplaced
    rowmap = {(r["provider"], r["location"]): r for r in rows}
    no_tier2 = set()

    # tier 0: ADM's own addresses
    for row in rows:
        if row["provider"] == "ADM" and adm and row["location"].strip().lower() in adm:
            la, lo, st = adm[row["location"].strip().lower()]
            st = row["state"] or st
            if st in BOX and inside(la, lo, st):
                prop[(row["provider"], row["location"])] = {"lat": round(la, 5), "lon": round(lo, 5), "source": "adm-bootstrap", "state": row["state"]}

    # tier 1: AgriCharts feeds
    by_provider = defaultdict(list)
    for prov, items in (feeds or {}).items():
        by_provider[prov].extend(items)
    dup_hosts = set()          # a feed whose locations share one pin (a head-office address on all) is not trusted
    per_host = defaultdict(Counter)
    for items in by_provider.values():
        for host, l, la, lo in items:
            per_host[host][(round(la, 4), round(lo, 4))] += 1
    for host, c in per_host.items():
        if c and c.most_common(1)[0][1] >= 4:
            dup_hosts.add(host)
    for row in rows:
        k = (row["provider"], row["location"])
        if k in prop or row["provider"] not in by_provider:
            continue
        want = norm(row["location"])
        levels = defaultdict(list)
        for host, l, la, lo in by_provider[row["provider"]]:
            if host in dup_hosts or not want:
                continue
            nm = norm(l.get("name") or "")
            vs, _ = city_variants(l.get("name") or "")
            core = norm(vs[0]) if vs else ""
            if want == nm:
                levels[1].append((host, l, la, lo))
            elif want == core:
                levels[2].append((host, l, la, lo))
            elif want == norm(l.get("display_name") or ""):
                levels[3].append((host, l, la, lo))
            elif want == norm(l.get("city") or ""):
                levels[4].append((host, l, la, lo))
        if not levels:
            continue
        hits = levels[min(levels)]
        if len({(round(h[2], 3), round(h[3], 3)) for h in hits}) > 1:
            fail[k] = "feed: %d different candidates" % len({(round(h[2], 3), round(h[3], 3)) for h in hits})
            continue
        host, l, la, lo = hits[0]
        st = row["state"] or (l.get("state") or "").strip().upper() or None
        if not st or st not in BOX:
            fail[k] = "feed match but no state to check it against"
            continue
        if not inside(la, lo, st):
            fail[k] = "the feed puts it at %.3f,%.3f (%s, %s) which is outside %s, the state on file" % (la, lo, l.get("city"), l.get("state"), st)
            no_tier2.add(k)
            continue
        prop[k] = {"lat": round(la, 5), "lon": round(lo, 5), "source": "feed:" + host.split("//")[1], "state": row["state"]}

    # aliases + tier 2
    pending = {}
    for row in rows:
        k = (row["provider"], row["location"])
        if k in prop or k in no_tier2:
            continue
        al = ALIAS.get(k)
        if al and al[0] == "copy":
            src = existing.get((al[1], al[2]))
            if src:
                prop[k] = {"lat": round(src[0], 5), "lon": round(src[1], 5), "source": "copy:%s/%s" % (al[1], al[2]), "state": al[3] if row["state"] is None else row["state"]}
                fail.pop(k, None)
            else:
                fail[k] = "copy source %s / %s has no pin" % (al[1], al[2])
            continue
        if not al and SKIP_RE.search(row["location"]):
            fail[k] = "not a place name"
            continue
        if al and al[0] == "place":
            variants, st = [al[1]], al[2] or row["state"]
            set_state = row["state"] is None
            if row["state"] and al[2] and row["state"] != al[2]:
                P.label_issues.append((row["provider"], row["location"], row["state"], al[2]))
        else:
            variants, name_st = city_variants(row["location"])
            if name_st and row["state"] and name_st != row["state"]:
                fail[k] = "the name says %s but the state on file is %s" % (name_st, row["state"])
                continue
            st = row["state"] or name_st
            set_state = False
        if not st or st not in BOX:
            fail[k] = "no state to constrain the lookup"
            continue
        if not variants:
            fail[k] = "no town name left after cleaning"
            continue
        if osm is None:
            fail[k] = "no geocoder available"
            P.transient.add(k)
            continue
        cands, used, budget_out, failed_req = [], None, False, False
        for v in variants:
            if cands and spelling(v) != spelling(used):          # another SPELLING of the town already found is worth asking (LeRoy / Le Roy); another town named in the location is not
                continue
            if not osm.cached(v, st) and osm.requests >= max_lookups:
                budget_out = True
                break
            res = osm.lookup(v, st)
            if res is None:
                failed_req = True
                break
            got = candidates(res, v, st)
            if got and not cands:
                used = v
            cands += [g for g in got if all(hav(g[0], g[1], c[0], c[1]) > 3 for c in cands)]
        if budget_out or failed_req:                              # never place on half an answer: the spelling not yet asked might hold the nearer town
            fail[k] = "lookup budget used up" if budget_out else "Nominatim request failed"
            P.transient.add(k)
        elif cands:
            fail.pop(k, None)
            if len(cands) == 1:
                la, lo, x = cands[0]
                prop[k] = {"lat": round(la, 5), "lon": round(lo, 5), "source": "osm:%s,%s" % (used, st), "state": st if set_state else row["state"], "osm": x.get("display_name", "")[:80], "n_cand": 1}
            else:
                pending[k] = (used, st, cands, set_state)
        else:
            fail[k] = "no settlement named %s inside %s" % ("/".join(variants[:3]), st)

    def anchor(provider, st, exclude=None):
        pts = [v for (p, l), v in existing.items() if p == provider and inside(v[0], v[1], st)]
        pts += [(v["lat"], v["lon"]) for (p, l), v in prop.items() if p == provider and (p, l) != exclude and inside(v["lat"], v["lon"], st)]
        return (median([p[0] for p in pts]), median([p[1] for p in pts])) if len(pts) >= 3 else None

    for k, (used, st, cands, set_state) in pending.items():
        a = anchor(k[0], st)
        if a:
            la, lo, x = min(cands, key=lambda c: hav(a[0], a[1], c[0], c[1]))
            how = "nearest of %d to the provider's other pins" % len(cands)
        else:
            la, lo, x = cands[0]
            how = "first of %d (no other pins to compare)" % len(cands)
            P.ambiguous.append((k, used, st, [county_of(c[2]) for c in cands[:4]], county_of(x)))
        prop[k] = {"lat": round(la, 5), "lon": round(lo, 5), "source": "osm:%s,%s" % (used, st), "state": st if set_state else rowmap[k]["state"], "osm": x.get("display_name", "")[:80],
                   "n_cand": len(cands), "how": how}

    # held back: looked at by hand, or an OSM pin far from the same provider's other pins in that state
    for k, why in HOLD.items():
        if k in prop:
            v = prop.pop(k)
            fail[k] = "held back by hand: %s (%s)" % (why, v.get("osm", "")[:50])
    for k, v in list(prop.items()):
        if not v["source"].startswith("osm:") or k in ACCEPT_FAR or k[0] in NATIONAL:
            continue
        st = v.get("state") or rowmap[k]["state"] or v["source"].split(",")[-1]
        a = anchor(k[0], st, exclude=k)
        if a and hav(a[0], a[1], v["lat"], v["lon"]) > FAR_MILES:
            d = hav(a[0], a[1], v["lat"], v["lon"])
            P.held.append((k, v, d))
            del prop[k]
            fail[k] = "held back: %s is %.0f mi from %s's other %s locations (%s) - a same-named town elsewhere, or the state on file is wrong" % (v["source"].split(":")[1], d, k[0], st,
                                                                                                                                                 v.get("osm", "")[:50])
    return P


# --------------------------------------------------------------------------------------------------- the job
def load_state(state_dir: Path) -> dict:
    try:
        st = json.loads((state_dir / "pin_missing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("unplaced", {})
    st.setdefault("runs", 0)
    return st


def save_state(state_dir: Path, st: dict, now: datetime) -> None:
    st["last_run"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "pin_missing.json.tmp"
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, state_dir / "pin_missing.json")


def load_rows(db):
    """(unpinned rows, {(provider, location): (lat, lon)} of the pinned ones) from location_meta. Goes through get_all_location_meta(): a dict per row on every backend (Snowflake's cursor hands back dicts,
    so unpacking a row as a tuple reads the column NAMES - the dry run against the live database found that; SQLite's rows unpack as values and hid it)."""
    rows, existing = [], {}
    for m in db.get_all_location_meta():
        if m["lat"] is None or m["lon"] is None:
            rows.append({"provider": m["provider"], "location": m["location"], "state": (m["state"] or "").strip().upper() or None, "facility_type": m["facility_type"] or None})
        else:
            existing[(m["provider"], m["location"])] = (float(m["lat"]), float(m["lon"]))
    rows.sort(key=lambda r: (r["provider"], r["location"]))
    return rows, existing


def build_email(placed, new_unplaced, held, label_issues, still_open, today, first_run):
    def table(head, rows):
        th = "".join("<th style='text-align:left;padding:3px 10px;border-bottom:1px solid #cbd5e1'>%s</th>" % h for h in head)
        tr = "".join("<tr>" + "".join("<td style='padding:3px 10px;border-bottom:1px solid #eef2f6'>%s</td>" % c for c in r) + "</tr>" for r in rows)
        return "<table style='border-collapse:collapse;font-size:12px'><tr>%s</tr>%s</table>" % (th, tr)
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = []
    if placed:
        body.append("<p><b>%d location%s got a map pin</b></p>" % (len(placed), "" if len(placed) == 1 else "s"))
        body.append(table(["Provider", "Location", "State", "Pin", "From"], [(esc(p), esc(l), esc(v.get("state") or ""), "%.4f, %.4f" % (v["lat"], v["lon"]), esc(v["source"])) for (p, l), v in placed]))
    if held:
        body.append("<p><b>%d held back for a look</b> (the only same-name town the geocoder has is far from that provider's other locations, or the state on file may be wrong)</p>" % len(held))
        body.append(table(["Provider", "Location", "Would be", "Miles from the rest"], [(esc(k[0]), esc(k[1]), esc(v.get("osm", "")[:60]), "%.0f" % d) for k, v, d in held]))
    if label_issues:
        body.append("<p><b>The state on file looks wrong for %d</b> (pinned where they really are)</p>" % len(label_issues))
        body.append(table(["Provider", "Location", "On file", "Really"], [(esc(a), esc(b), esc(c), esc(d)) for a, b, c, d in label_issues]))
    if new_unplaced:
        body.append("<p><b>%d could not be placed</b>%s</p>" % (len(new_unplaced), " (everything still open from before, once)" if first_run else ""))
        body.append(table(["Provider", "Location", "Why"], [(esc(k.split("|", 1)[0]), esc(k.split("|", 1)[1]), esc(w)) for k, w in new_unplaced]))
    body.append("<p style='color:#64748b;font-size:12px;margin-top:14px'>%d location%s still have no pin in all. A pin comes from ADM's own addresses, an AgriCharts feed, or a state-constrained "
                "OpenStreetMap lookup checked against the state's border; what could not be placed is left alone and tried again after %d days. To place one by hand, add it to ALIAS in "
                "pin_missing.py; to switch the job off, touch state/pin_missing.off. Auto-generated by pin_missing.py.</p>" % (still_open, "" if still_open == 1 else "s", RETRY_DAYS))
    n = len(placed)
    subject = "Basis Tracker - map pins: %d placed%s%s" % (n, ", %d held back" % len(held) if held else "", ", %d to place by hand" % len(new_unplaced) if new_unplaced else "")
    html = ("<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1e293b'><p style='font-size:15px;font-weight:700'>Basis Tracker - map pins for new locations, %s</p>%s</div>"
            % (today, "".join(body)))
    return subject, html


def send_default(subject: str, html: str) -> None:
    from changes_report import send_email
    send_email(subject, html, os.getenv("CHANGES_EMAIL_TO", "kpostin@jpsi.com"), cc="")


def run(args, *, send=None, now=None, state_dir=None, osm=None, adm=None, feeds=None, db=None) -> int:
    """One pass. Returns the exit code (0 = fine / nothing to do). Every source is injectable, so the tests need no network."""
    state_dir = Path(state_dir or args.state_dir or STATE_DIR)
    if (state_dir / "pin_missing.off").exists():
        print("disabled: %s exists" % (state_dir / "pin_missing.off"))
        return 0
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if db is None:
        import database as db
    if not db._use_sf() and not args.allow_sqlite:
        raise RuntimeError("the database backend is not Snowflake (USE_SNOWFLAKE missing from .env?): refusing to pin rows in an empty local SQLite")
    st = load_state(state_dir)
    first_run = st["runs"] == 0
    rows, existing = load_rows(db)
    print("pin_missing: %d location(s) without a pin, %d pinned" % (len(rows), len(existing)))
    live = {"%s|%s" % (r["provider"], r["location"]) for r in rows}
    for key in [k for k in st["unplaced"] if k not in live]:                          # pinned by hand since (or gone): no longer remembered
        del st["unplaced"][key]

    # what is worth trying: not a row given up on within the last RETRY_DAYS (unless the state on file changed)
    todo, remembered = [], {}
    for r in rows:
        key = "%s|%s" % (r["provider"], r["location"])
        rec = st["unplaced"].get(key)
        if rec and not args.retry_all and rec.get("state") == r["state"] and rec.get("last_try", "") >= (now - timedelta(days=RETRY_DAYS)).strftime("%Y-%m-%d"):
            remembered[key] = rec
        else:
            todo.append(r)
    unmailed = [(k, rec["why"]) for k, rec in remembered.items() if not rec.get("emailed")]       # given up on earlier, and the mail about it did not go out
    if not todo and not unmailed:
        print("pin_missing: nothing new to try (%d remembered as unplaceable)" % len(remembered))
        st["runs"] += 1
        if not args.dry_run:
            save_state(state_dir, st, now)
        return 0

    if todo:
        if adm is None and any(r["provider"] == "ADM" for r in todo):
            try:
                adm = fetch_adm()
            except Exception as exc:
                print("pin_missing: ADM bootstrap unavailable (%s)" % exc)
                adm = None
        if feeds is None:
            feeds = fetch_feeds({r["provider"] for r in todo}) if any(p & {r["provider"] for r in todo} for p in feed_host_map().values()) else {}
        if osm is None:
            osm = Osm(state_dir / "nominatim_cache.json")
        P = plan(todo, existing, adm=adm, feeds=feeds, osm=osm, max_lookups=args.max_lookups)
    else:
        P = Plan()
    print("pin_missing: %d placed, %d not placed (%d held back), %d new geocoder request(s)" % (len(P.proposals), len(P.unplaced), len(P.held), getattr(osm, "requests", 0)))
    for (p, l), v in sorted(P.proposals.items()):
        print("   + %-26s %-30s %9.4f %10.4f  %s" % (p[:26], l[:30], v["lat"], v["lon"], v["source"]))
    for k, w in sorted(P.unplaced.items()):
        print("   - %-26s %-30s %s" % (k[0][:26], k[1][:30], w))

    if args.dry_run:
        return 0
    by_prov = defaultdict(list)
    rowmap = {(r["provider"], r["location"]): r for r in todo}
    for (p, l), v in P.proposals.items():
        item = {"location": l, "lat": v["lat"], "lon": v["lon"]}
        if rowmap[(p, l)]["state"] is None and v.get("state"):
            item["state"] = v["state"]
        by_prov[p].append(item)
    for p, items in sorted(by_prov.items()):
        db.upsert_location_metas(p, items)

    # the books
    for (p, l) in P.proposals:
        st["unplaced"].pop("%s|%s" % (p, l), None)
    new_unplaced = []
    for (p, l), why in P.unplaced.items():
        key = "%s|%s" % (p, l)
        if (p, l) in P.transient:
            continue
        old = st["unplaced"].get(key)
        rec = {"why": why, "state": rowmap[(p, l)]["state"], "first": (old or {}).get("first", today), "last_try": today,
               "emailed": why in QUIET or bool(old and old.get("emailed") and old.get("why") == why)}
        st["unplaced"][key] = rec
        if not rec["emailed"]:
            new_unplaced.append((key, why))
    new_unplaced += unmailed
    still_open = len(rows) - len(P.proposals)
    placed = sorted(P.proposals.items())
    # only what is new is worth a mail: a held-back row is tried again every RETRY_DAYS and held again for the same reason, and a wrong label on a row that is still unpinned shows up again too
    fresh = {k for k, _ in new_unplaced}
    held_keys = {"%s|%s" % h[0] for h in P.held}
    held = [h for h in P.held if "%s|%s" % h[0] in fresh]
    listed = [(k, w) for k, w in new_unplaced if k not in held_keys]                 # a held row is in the held table, not listed twice
    labels = [x for x in P.label_issues if (x[0], x[1]) in P.proposals or "%s|%s" % (x[0], x[1]) in fresh]
    news = placed or listed or held or labels
    seen = True
    if news and not args.no_email:
        subject, html = build_email(placed, listed, held, labels, still_open, today, first_run)
        try:
            (send or send_default)(subject, html)
        except Exception as exc:                       # the pins are written; the mail is retried next run (the rows are not marked emailed)
            print("pin_missing: could not send the mail: %s" % exc)
            seen = False
    if seen:                                           # --no-email counts as seen: the list was on the screen
        for key in fresh:
            st["unplaced"][key]["emailed"] = True
    st["runs"] += 1
    save_state(state_dir, st, now)
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Give a map pin to every location_meta row that has none.")
    ap.add_argument("--dry-run", action="store_true", help="plan and print; write and mail nothing")
    ap.add_argument("--no-email", action="store_true", help="mail nothing; what it would have mailed counts as seen (the next run does not mail it)")
    ap.add_argument("--retry-all", action="store_true", help="try again what was given up on within the last %d days" % RETRY_DAYS)
    ap.add_argument("--state-dir", default=None, help="where state/kill switch/geocoder cache live (default ./state)")
    ap.add_argument("--allow-sqlite", action="store_true", help="work on a local SQLite (tests)")
    ap.add_argument("--max-lookups", type=int, default=MAX_LOOKUPS, help="new geocoder requests in one run")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args()))
