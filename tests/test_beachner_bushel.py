"""Beachner Grain (Parsons KS; 24 elevators in SE Kansas and NE Oklahoma) on the Bushel-sites scraper: beachnergrain.com/cashbidssingle-<id>.

    python tests/test_beachner_bushel.py

Kolten 2026-10-27: "Lets also add in this bid scrapper https://www.beachnergrain.com/cash-bids". One SITES row ("pages" style, one GET page per elevator) plus four small options in
bushelsites_scraper: `grains` (Beachner's boards are named by commodity CLASS: 'Hard Wheat' is Kansas City KE, 'Soft Wheat' Chicago ZW, 'Milo' is priced off the corn board), a row id that carries the
grain (Corn and Milo are both ZC), a pin per page, and retries (a connection reset on 2026-10-27 would have cost the whole site's 24 pages). Uses the real cash-bid markup of two locations
(tests/fixtures/beachner_afton_2026-10-27.html, beachner_welch_2026-10-27.html) - no network; the database is a throwaway SQLite.
"""
import collections
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import bushelsites_scraper as bs                                 # noqa: E402
import database as db                                            # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="beachner_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


AFTON = open(os.path.join(HERE, "fixtures", "beachner_afton_2026-10-27.html"), encoding="utf-8").read()
WELCH = open(os.path.join(HERE, "fixtures", "beachner_welch_2026-10-27.html"), encoding="utf-8").read()
CFG = bs.SITES.get("Beachner Grain")
EXPECTED = {1962: "Afton", 2037: "Altamont", 2038: "Bronson", 2039: "Chanute", 2040: "Erie", 2041: "Fredonia", 2042: "Garnett", 2043: "Greeley", 2045: "Harris", 2046: "Humboldt", 2047: "Independence",
            2048: "Kincaid", 2049: "LaCygne", 2050: "Miami", 2051: "Mound City", 2052: "Neodesha", 2083: "Neosho Valley", 2053: "Paola", 2054: "Parsons", 2055: "Richmond", 2063: "St. Paul",
            2057: "Thayer", 2058: "Walnut", 2059: "Welch"}        # the dropdown's option values on beachnergrain.com/cash-bids, 2026-10-27
OKLAHOMA = {"Afton", "Miami", "Welch"}
BOX = {"KS": (36.99, 40.01, -102.06, -94.58), "OK": (33.6, 37.01, -103.01, -94.42)}

print("beachner: the site row")
check("a Bushel 'pages' site, country elevators, one second between pages", CFG is not None and CFG["style"] == "pages" and CFG["facility"] == "Country Elevator" and CFG.get("delay", 0) >= 1.0, CFG and {k: CFG[k] for k in ("style", "facility", "delay")})
pages = CFG["pages"] if CFG else []
ids = [int(p["url"].rsplit("-", 1)[1]) for p in pages]
check("24 pages, one per option of the site's own location dropdown, each at /cashbidssingle-<id> on beachnergrain.com",
      sorted(ids) == sorted(EXPECTED) and len(set(ids)) == 24 and all(p["url"] == f"https://www.beachnergrain.com/cashbidssingle-{i}" for p, i in zip(pages, ids)), sorted(set(EXPECTED) ^ set(ids)))
names = {i: p["loc"][0] for p, i in zip(pages, ids)}
check("every location is named 'Town, ST' after the site's own town name and the names are unique", all(names[i] == f"{EXPECTED[i]}, {p['loc'][1]}" for p, i in zip(pages, ids)) and len(set(names.values())) == 24, names)
check("Afton, Miami and Welch are Oklahoma, the other 21 Kansas (the street addresses on each /cbloc page)", all((p["loc"][1] == "OK") == (EXPECTED[i] in OKLAHOMA) and p["loc"][1] in ("KS", "OK") for p, i in zip(pages, ids)),
      {names[i]: p["loc"][1] for p, i in zip(pages, ids)})
check("every location is a Country Elevator", all(p["loc"][2] == "Country Elevator" for p in pages))
inbox = [p for p in pages if p.get("pin") and BOX[p["loc"][1]][0] <= p["pin"][0] <= BOX[p["loc"][1]][1] and BOX[p["loc"][1]][2] <= p["pin"][1] <= BOX[p["loc"][1]][3]]
check("every page carries a pin inside its own state's bounding box, and the 24 pins are all different", len(inbox) == 24 and len({p["pin"] for p in pages}) == 24, [names[i] for p, i in zip(pages, ids) if p not in inbox])
check("the class-named boards map to the right grain and contract root: Hard Wheat -> Wheat HRW / KE, Soft Wheat -> Wheat SRW / ZW, Milo -> Milo / ZC (priced off corn)",
      CFG["grains"] == {"corn": ("Corn", "ZC"), "soybeans": ("Soybeans", "ZS"), "milo": ("Milo", "ZC"), "hard wheat": ("Wheat HRW", "KE"), "soft wheat": ("Wheat SRW", "ZW")}, CFG["grains"])
check("no other Bushel site has a grains map (their boards, ids and grains are as they were)", [k for k, v in bs.SITES.items() if v.get("grains")] == ["Beachner Grain"], [k for k, v in bs.SITES.items() if v.get("grains")])


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise bs.requests.HTTPError("HTTP %d" % self.status)


class FakeSession:
    """GET /cashbidssingle-<id>: `fail` = {id: number of leading failures, or -1 for always} (a connection reset), `status` = {id: http status}."""

    def __init__(self, pages=None, fail=None, status=None):
        self.headers = {}
        self.pages = pages or {}
        self.fail = fail or {}
        self.status = status or {}
        self.calls = collections.Counter()

    def get(self, url, timeout=None):
        pid = int(url.rsplit("-", 1)[1])
        self.calls[pid] += 1
        nf = self.fail.get(pid, 0)
        if nf == -1 or self.calls[pid] <= nf:
            raise bs.requests.ConnectionError("Connection aborted: reset by peer")
        return FakeResp(self.pages.get(pid, AFTON), self.status.get(pid, 200))


def scrape(sess, cfg=CFG):
    sleeps = []
    with mock.patch.object(bs.requests, "Session", lambda: sess), mock.patch.object(bs.time, "sleep", lambda s: sleeps.append(s)):
        return bs.scrape_site(cfg), sleeps


print("\nbeachner: scraping the 24 pages (the real markup of Afton, and Welch's for Welch)")
sess = FakeSession(pages={2059: WELCH})
boards, sleeps = scrape(sess)
per = collections.defaultdict(list)
for b in boards:
    per[b["location"]].append(b)
check("one GET per page, 120 boards: five at each of the 24 locations", sum(sess.calls.values()) == 24 and len(boards) == 120 and len(per) == 24 and all(len(v) == 5 for v in per.values()), (sum(sess.calls.values()), len(boards)))
check("the pause between pages is the configured second (23 of them, none before the first page)", sleeps == [1.0] * 23, sleeps)
check("each board gets the grain and contract root of its heading", {(b["grain"], b["root"]) for b in boards} == {("Corn", "ZC"), ("Soybeans", "ZS"), ("Milo", "ZC"), ("Wheat HRW", "KE"), ("Wheat SRW", "ZW")}, {(b["grain"], b["root"]) for b in boards})
check("each board carries its page's pin and state", all((b["lat"], b["lon"]) == p["pin"] and b["state"] == p["loc"][1] for p in pages for b in per[p["loc"][0]]))

print("\nbeachner: parsed into snapshot rows")
reqs = {loc: [bs.parse_board(b, "Beachner Grain") for b in bl] for loc, bl in per.items()}
afton = {r.grain: r for q in reqs["Afton, OK"] for r in q.rows}
check("Afton: Corn vs ZCZ26 at -35, Hard Wheat vs KEZ26 (Kansas City) at -106, Milo vs ZCZ26 at -110, Soft Wheat vs ZWZ26 (Chicago) at -116, Soybeans vs ZSX26 at -6",
      {g: (r.futuresSymbol, r.basisCents) for g, r in afton.items()} == {"Corn": ("ZCZ26", -35), "Wheat HRW": ("KEZ26", -106), "Milo": ("ZCZ26", -110), "Wheat SRW": ("ZWZ26", -116), "Soybeans": ("ZSX26", -6)},
      {g: (r.futuresSymbol, r.basisCents) for g, r in afton.items()})
check("every bid is the 'Cash' bid, not a spot-file row", {(r.deliveryMonth, r.isSpot) for q in reqs["Afton, OK"] for r in q.rows} == {("Cash", False)})
check("Corn and Milo share a contract (ZCZ26) but not a row id: all 5 ids at a location are different", len({r.id for q in reqs["Afton, OK"] for r in q.rows}) == 5, [r.id for q in reqs["Afton, OK"] for r in q.rows])
welch = {r.grain: r.basisCents for q in reqs["Welch, OK"] for r in q.rows}
check("Welch (its own page): corn -37, hard wheat -110, milo -110, soft wheat -120, soybeans -10", welch == {"Corn": -37, "Wheat HRW": -110, "Milo": -110, "Wheat SRW": -120, "Soybeans": -10}, welch)
check("120 rows over the 24 locations", sum(len(q.rows) for qs in reqs.values() for q in qs) == 120)
oats = AFTON.replace('<h3 class="fcControls">Milo</h3>', '<h3 class="fcControls">Oats</h3>')
assert oats != AFTON, "the fixture's Milo heading is not where the test looks"
check("a board whose heading is not in the map is skipped, not guessed ('Oats'): 4 boards at that page", sum(1 for b in scrape(FakeSession(pages={1962: oats}))[0] if b["location"] == "Afton, OK") == 4)
_soup = bs.BeautifulSoup(AFTON, "html.parser")
_soup.find("div", class_="cbCommodity").find("ul", class_="fcControls1").decompose()          # the Corn board keeps its header row but loses its bid row
noliner = str(_soup)
assert noliner != AFTON, "the fixture's bid row is not where the test looks"
check("a board with a header but no bid row is skipped (4 boards at that page)", sum(1 for b in scrape(FakeSession(pages={1962: noliner}))[0] if b["location"] == "Afton, OK") == 4)
old = bs.parse_board({"location": "X, WI", "state": "WI", "facility_type": "Corn Processing", "grain": "Corn", "rows": [{"c1": "Oct 2026", "c3": "-0.2500", "c6": "Dec 26 Corn"}]}, "Some Plant")
check("a site WITHOUT a grains map keeps its old row id ('C_0_Oct2026') and its grain-based root", old.rows[0].id == "C_0_Oct2026" and old.rows[0].futuresSymbol == "ZCZ26" and old.rows[0].basisCents == -25, old.rows[0])
check("_fut_symbol: the grain's root unless one is given", bs._fut_symbol("Dec 26 Corn", "Corn") == "ZCZ26" and bs._fut_symbol("Dec 26 KCBT Red Wheat", "Wheat", "KE") == "KEZ26" and bs._fut_symbol("Dec 26 Wheat", "Wheat") == "ZWZ26")

print("\nbeachner: one bad page must not cost the other 23")
s2 = FakeSession(fail={2039: 2})
b2, sl2 = scrape(s2)
check("two connection resets in a row on one page are retried and the page is read on the third try (waits of 2 s then 4 s before it)", s2.calls[2039] == 3 and len(b2) == 120 and sl2.count(2) == 1 and sl2.count(4) == 1, (s2.calls[2039], len(b2), sl2))
s3 = FakeSession(fail={2039: -1})
b3, _ = scrape(s3)
check("a page that keeps failing is tried three times and skipped: 23 locations, Chanute absent, the rest intact", s3.calls[2039] == 3 and len(b3) == 115 and "Chanute, KS" not in {b["location"] for b in b3}, (s3.calls[2039], len(b3)))
s4 = FakeSession(status={2040: 500})
b4, _ = scrape(s4)
check("an HTTP error is handled the same way (Erie skipped after three tries)", s4.calls[2040] == 3 and len(b4) == 115 and "Erie, KS" not in {b["location"] for b in b4}, (s4.calls[2040], len(b4)))
try:
    scrape(FakeSession(fail={i: -1 for i in EXPECTED}))
    raised = False
except bs.requests.RequestException:
    raised = True
check("when EVERY page fails the error is raised, so run_bushelsites logs the whole site as failed, as it always has", raised)

print("\nbeachner: pins and bids reach the database (throwaway SQLite)")
db.init_db()
snaps = [q for qs in reqs.values() for q in qs]
db.upsert_snapshots([q.model_dump() for q in snaps])
metas = {}
for b in boards:
    metas[b["location"]] = {"location": b["location"], "state": b["state"], "facility_type": b["facility_type"], "lat": b.get("lat"), "lon": b.get("lon")}     # the dict run_bushelsites builds
db.upsert_location_metas("Beachner Grain", list(metas.values()))
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT location, state, facility_type, lat, lon FROM location_meta WHERE provider = 'Beachner Grain' ORDER BY location")
got = {r[0]: tuple(r[1:]) for r in c.fetchall()}
conn.close()
check("24 locations in location_meta, Afton OK with its address pin and Chanute KS with its", len(got) == 24 and got["Afton, OK"] == ("OK", "Country Elevator", 36.69627, -94.96474) and got["Chanute, KS"] == ("KS", "Country Elevator", 37.66011, -95.46195), got.get("Afton, OK"))
rd = db.get_snapshots("Beachner Grain", "Afton, OK")
check("Afton reads back as ONE snapshot (the five boards merged) with its 5 rows, through the app's own loader", len(rd) == 1 and rd[0].source == "web" and len(rd[0].rows) == 5, [(s.source, len(s.rows)) for s in rd])
db.upsert_snapshots([q.model_dump() for q in snaps])
check("a second run the same day adds nothing (same row ids)", len(db.get_snapshots("Beachner Grain", "Afton, OK")) == 1 and len(db.get_snapshots("Beachner Grain", "Afton, OK")[0].rows) == 5)

print("\nbeachner: auto_import passes the pins on")
src = open(os.path.join(os.path.dirname(HERE), "auto_import.py"), "rb").read().decode("utf-8")
i = src.index("def run_bushelsites")
j = src.index("def run_agricharts_tenants")
check("run_bushelsites puts each board's lat / lon into the location meta it upserts (None keeps a stored pin)", '"lat": b.get("lat"), "lon": b.get("lon")' in src[i:j])

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
