"""Ray-Carroll County Grain Growers (Carrollton MO) on the browser-free aghost scraper: ray-carroll.com/index.cfm?show=11&mid=26&theLocation=<id>&cmid=all&layout=19.

    python tests/test_raycarroll_dtn.py

Kolten 2026-10-27: "Can we check if we can scrap ray carroll". CLAUDE.md had it as 'Known-unbuilt: a 15-location co-op needing dropdown iteration per location x commodity'; the page turned out to
take the location in the URL (`theLocation`) and `cmid=all` for every commodity at once, server-rendered with the usual displayNumber() obfuscation, so it is eight GETs and one new parser
(`_parse_cashbid_grid`, which finds its columns by the header's NAMES). Uses the real markup and the page's own decoder of two locations (tests/fixtures/raycarroll_carrollton_2026-10-27.html and
raycarroll_stlouis_2026-10-27.html, trimmed to the decoder script and the commodity tables) - no network; the database is a throwaway SQLite.
"""
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                                            # noqa: E402
import dtn_http_scraper as dh                                    # noqa: E402
import vistacomm_scraper as vc                                   # noqa: E402
from bs4 import BeautifulSoup                                    # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="raycarroll_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


CARR = open(os.path.join(HERE, "fixtures", "raycarroll_carrollton_2026-10-27.html"), encoding="utf-8").read()
STL = open(os.path.join(HERE, "fixtures", "raycarroll_stlouis_2026-10-27.html"), encoding="utf-8").read()
RC = [s for s in dh.SITES if s["provider"] == "Ray-Carroll"]


class FakeDate(date):
    @classmethod
    def today(cls):
        return date(2026, 10, 27)          # '@S6X' is 2026 only while the year is 2026: pin the clock the symbol decoder reads


def parse(html, cfg=None):
    with mock.patch.object(vc, "date", FakeDate):
        return dh._parse_cashbid_grid(html, cfg or RC[0])


def rows(req):
    return [(r.grain, r.futuresSymbol, r.deliveryMonth, r.basisCents) for r in req.rows]


print("ray-carroll: the site rows")
ids = [int(s["url"].split("theLocation=")[1].split("&")[0]) for s in RC]
check("eight elevators, one page each, at the Location dropdown's own ids (Carrollton 1, Hardin 18, Norborne 7, Brunswick 20, Corder 10, Slater 12, Sumner 14, St. Louis 15)",
      sorted(ids) == [1, 7, 10, 12, 14, 15, 18, 20] and len(set(ids)) == 8, ids)
check("every URL is ray-carroll.com's cash-bid page for ALL commodities (cmid=all, layout 19)", all(s["url"].startswith("https://www.ray-carroll.com/index.cfm?show=11&mid=26&theLocation=") and s["url"].endswith("&cmid=all&layout=19") for s in RC))
check("the 'Off Farm' ids (2, 8, 11, 19, 21, 22) and Wakenda (9, no bids) are not read", not ({2, 8, 11, 19, 21, 22, 9} & set(ids)), ids)
check("all in Missouri; St. Louis is a River Terminal (2.49 million bushels on the Mississippi), the other seven Country Elevators",
      {s["state"] for s in RC} == {"MO"} and {s["location"]: s["facility_type"] for s in RC}["St. Louis, MO"] == "River Terminal" and sum(1 for s in RC if s["facility_type"] == "Country Elevator") == 7, [(s["location"], s["facility_type"]) for s in RC])
check("every site has a pin inside Missouri's bounding box and the eight are different", all(35.99 <= s["pin"][0] <= 40.62 and -95.78 <= s["pin"][1] <= -89.09 for s in RC) and len({s["pin"] for s in RC}) == 8, [s["pin"] for s in RC])
check("grid parsing, an identifying user agent and a pause are switched on for these and no other aghost site", all(s.get("grid") and s.get("ua") and s.get("delay", 0) >= 1 for s in RC)
      and [s["provider"] for s in dh.SITES if s.get("grid") or s.get("ua") or s.get("pin")] == ["Ray-Carroll"] * 8, [s["provider"] for s in dh.SITES if s.get("grid") or s.get("ua") or s.get("pin")])

print("\nray-carroll: Carrollton's page (the real markup, decoded with the page's own displayNumber)")
car = parse(CARR)
check("Carrollton: soybeans ZSX26 'Nov 2026' -45, corn ZCZ26 'Oct 2026' -15, wheat ZWZ26 'Oct 2026' -50 and ZWN27 'Jul 2027' -40",
      rows(car) == [("Soybeans", "ZSX26", "Nov 2026", -45), ("Corn", "ZCZ26", "Oct 2026", -15), ("Wheat", "ZWZ26", "Oct 2026", -50), ("Wheat", "ZWN27", "Jul 2027", -40)], rows(car))
check("filed under Ray-Carroll / 'Carrollton, MO', source 'web', none a spot row, row ids unique", (car.provider, car.location, car.source) == ("Ray-Carroll", "Carrollton, MO", "web") and not any(r.isSpot for r in car.rows) and len({r.id for r in car.rows}) == 4, [r.id for r in car.rows])
stl = parse(STL, next(s for s in RC if s["location"] == "St. Louis, MO"))
check("St. Louis: wheat only, ZWZ26 'Oct 2026' -15 and 'Nov 2026' -10 (one contract, two windows: two ids), ZWN27 'Jul 2027' -15",
      rows(stl) == [("Wheat", "ZWZ26", "Oct 2026", -15), ("Wheat", "ZWZ26", "Nov 2026", -10), ("Wheat", "ZWN27", "Jul 2027", -15)] and len({r.id for r in stl.rows}) == 3, rows(stl))


def swap_columns(html, a, b):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup.find_all("table", attrs={"name": "cashbids-data-table"}):
        for tr in t.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            ca, cb = cells[a], cells[b]
            ta, tb = ca.decode_contents(), cb.decode_contents()
            ca.clear()
            cb.clear()
            ca.append(BeautifulSoup(tb, "html.parser"))
            cb.append(BeautifulSoup(ta, "html.parser"))
    return str(soup)


print("\nray-carroll: columns are found by NAME, never by position or size")
swapped = swap_columns(CARR, 1, 3)
assert swapped != CARR
check("with the Cash Price and Basis columns swapped (header and cells) the same four bids come out", rows(parse(swapped)) == rows(car), rows(parse(swapped)))
moved = swap_columns(CARR, 2, 3)
check("with the Futures Month and Basis columns swapped too", rows(parse(moved)) == rows(car), rows(parse(moved)))
renamed = CARR.replace(">Basis<", ">Spread<", 1)
assert renamed != CARR
r2 = parse(renamed)
check("a table whose header no longer says Basis is skipped (not guessed from a number), the other two still file", rows(r2) == [("Corn", "ZCZ26", "Oct 2026", -15), ("Wheat", "ZWZ26", "Oct 2026", -50), ("Wheat", "ZWN27", "Jul 2027", -40)], rows(r2))
check("a page without the displayNumber decoder files nothing (None), it does not file raw numbers", parse(CARR.replace("function displayNumber", "function notTheDecoder")) is None)
check("a page with no commodity tables files nothing", parse("<html><body><p>Down for maintenance</p><script>function displayNumber(x, d) { x = x; }</script></body></html>") is None)
nodate = CARR.replace("Nov\xa030, 2026", "Harvest")        # the page writes its dates with a no-break space
assert nodate != CARR, "the fixture's soybean date is not where the test looks"
check("a row whose delivery is not a date is skipped, the rest are kept", len(parse(nodate).rows) == 3 and ("Soybeans", "ZSX26", "Nov 2026", -45) not in rows(parse(nodate)))
nosym = CARR.replace("@W7N", "TBD")
check("a row with no @-symbol is skipped (no contract to give the basis)", ("Wheat", "ZWN27", "Jul 2027", -40) not in rows(parse(nosym)) and len(parse(nosym).rows) == 3)

print("\nray-carroll: the whole fetch (the HTTP client faked)")


class FakeResp:
    def __init__(self, text):
        self.text = text


class FakeClient:
    instances = []

    def __init__(self, *a, **k):
        self.requests = []
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        self.requests.append((url, headers))
        return FakeResp(STL if "theLocation=15&" in url else CARR)


sleeps = []
with mock.patch.object(dh, "SITES", RC), mock.patch.object(dh, "API_SITES", []), mock.patch.object(dh.httpx, "Client", FakeClient), mock.patch.object(dh.time, "sleep", lambda s: sleeps.append(s)), mock.patch.object(vc, "date", FakeDate):
    reqs, metas = dh.fetch_dtn_http()
cl = FakeClient.instances[-1]
check("eight GETs, each with the identifying user agent (not the browser one the older aghost sites send)", len(cl.requests) == 8 and all(h == {"user-agent": dh._UA_ID} for u, h in cl.requests), cl.requests[:2])
check("a 1.2 s pause between the pages of the one host, none before the first (7)", sleeps == [1.2] * 7, sleeps)
check("eight snapshots filed under Ray-Carroll, St. Louis with its wheat", len(reqs) == 8 and {r.provider for r in reqs} == {"Ray-Carroll"} and [r for r in reqs if r.location == "St. Louis, MO"][0].rows[0].grain == "Wheat", [r.location for r in reqs])
check("the metas carry state, type and the pin (the address pin of Hardin, the Mississippi terminal at St. Louis)", {m["location"]: (m["state"], m["facility_type"], m["lat"], m["lon"]) for m in metas}["Hardin, MO"] == ("MO", "Country Elevator", 39.26958, -93.83216)
      and {m["location"]: (m["lat"], m["lon"]) for m in metas}["St. Louis, MO"] == (38.63431, -90.24738), metas[:2])
bare = {k: v for k, v in RC[0].items() if k != "pin"}
with mock.patch.object(dh, "SITES", [bare]), mock.patch.object(dh, "API_SITES", []), mock.patch.object(dh.httpx, "Client", FakeClient), mock.patch.object(dh.time, "sleep", lambda s: None), mock.patch.object(vc, "date", FakeDate):
    _, m_bare = dh.fetch_dtn_http()
check("a site added without a pin sends no lat / lon keys at all (a stored pin is never overwritten)", len(m_bare) == 1 and "lat" not in m_bare[0] and "lon" not in m_bare[0], m_bare)

print("\nray-carroll: bids and pins reach the database (throwaway SQLite)")
db.init_db()
db.upsert_snapshots([r.model_dump() for r in reqs])
db.upsert_location_metas("Ray-Carroll", [{"location": m["location"], "state": m["state"], "facility_type": m["facility_type"], "lat": m.get("lat"), "lon": m.get("lon")} for m in metas])
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT location, state, facility_type, lat, lon FROM location_meta WHERE provider = 'Ray-Carroll' ORDER BY location")
got = {r[0]: tuple(r[1:]) for r in c.fetchall()}
conn.close()
check("eight locations in location_meta with state, type and pin", len(got) == 8 and got["St. Louis, MO"] == ("MO", "River Terminal", 38.63431, -90.24738) and got["Carrollton, MO"] == ("MO", "Country Elevator", 39.35673, -93.4766), got.get("St. Louis, MO"))
rd = db.get_snapshots("Ray-Carroll", "Carrollton, MO")
check("Carrollton reads back as one snapshot with its 4 rows through the app's own loader", len(rd) == 1 and rd[0].source == "web" and len(rd[0].rows) == 4, [(s.source, len(s.rows)) for s in rd])
db.upsert_snapshots([r.model_dump() for r in reqs])
check("a second run the same day adds nothing (same row ids)", len(db.get_snapshots("Ray-Carroll", "Carrollton, MO")) == 1 and len(db.get_snapshots("Ray-Carroll", "Carrollton, MO")[0].rows) == 4)

print("\nray-carroll: auto_import passes the pins on")
src = open(os.path.join(os.path.dirname(HERE), "auto_import.py"), "rb").read().decode("utf-8")
i = src.index("def run_dtn_http")
j = src.index("def run_agricharts_md")
check("run_dtn_http puts each meta's lat / lon into the location meta it upserts (None keeps a stored pin)", '"lat": m.get("lat"), "lon": m.get("lon")' in src[i:j])

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
