"""Northern Canola Growers Association cash table -> Bunge Altona and Cargill West Fargo canola bids (and Cargill's flat-price canola no longer stored as zeros).

    python tests/test_ncga_scraper.py

Uses a saved copy of the page's price block (tests/fixtures/ncga_home_2026-10-13.html, the real markup) — no network. The last section files the rows into a
throwaway SQLite database and reads them back through database.get_snapshots, the way the app does.
"""
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                                       # noqa: E402
import ncga_scraper as ns                                   # noqa: E402
from parsers.cargill_parser import parse_cargill_location   # noqa: E402
from parsers.ncga_parser import parse_ncga_location         # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="ncga_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)                           # the guards log errors on purpose

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


HTML = open(os.path.join(HERE, "fixtures", "ncga_home_2026-10-13.html"), encoding="utf-8").read()
TODAY = date(2026, 10, 14)                  # the fetch date: a day after the table is stamped (NCGA updates during the business day)

print("ncga: the table (saved copy of the page on 2026-10-13)")
t = ns.parse_table(HTML)
check("the page's own update date and units sentence are read", t["updated"] == date(2026, 10, 13) and t["units_ok"] is True, (t["updated"], t["units_ok"]))
check("five buyers in the page's order, five arrival months", list(t["buyers"]) == ["ADM Velva", "Bunge Altona", "ADM Enderlin", "Cargill West Fargo", "CHS Hallock"]
      and t["months"] == ["Oct", "Nov", "Dec", "Jan", "Feb"], (list(t["buyers"]), t["months"]))
bg = {r["label"]: (r["price"], r["basis"]) for r in t["buyers"]["Bunge Altona"]}
check("Bunge Altona: no bid in October ('--' is None, not 0), then 25.18 / -25, 25.75 / -20, 25.59 / -25, 25.88 / -25",
      bg == {"Oct": (None, None), "Nov": (25.18, -25), "Dec": (25.75, -20), "Jan": (25.59, -25), "Feb": (25.88, -25)}, bg)
cg = {r["label"]: (r["price"], r["basis"]) for r in t["buyers"]["Cargill West Fargo"]}
check("Cargill West Fargo: the 'Change' cell's '75' / '45' does not leak into the basis; February's basis is a real 0",
      cg == {"Oct": (26.10, -25), "Nov": (26.30, -10), "Dec": (26.45, -10), "Jan": (26.45, -5), "Feb": (26.75, 0)}, cg)
check("the other three read as the page shows them (ADM Velva Oct -45, ADM Enderlin Nov -8, CHS Hallock Oct -60)",
      t["buyers"]["ADM Velva"][0]["basis"] == -45 and t["buyers"]["ADM Enderlin"][1]["basis"] == -8 and t["buyers"]["CHS Hallock"][0]["basis"] == -60)

print("ncga: the ICE canola contract a month is quoted against")
check("Oct and Nov 2026 -> RSX26, Dec 2026 and Jan 2027 -> RSF27, Feb and Mar 2027 -> RSH27",
      [ns.ice_canola_symbol(2026, 10), ns.ice_canola_symbol(2026, 11), ns.ice_canola_symbol(2026, 12), ns.ice_canola_symbol(2027, 1), ns.ice_canola_symbol(2027, 2), ns.ice_canola_symbol(2027, 3)]
      == ["RSX26", "RSX26", "RSF27", "RSF27", "RSH27", "RSH27"])
check("Apr-May -> K, Jun-Jul -> N, Aug-Sep -> the new-crop Nov of that year (CHS posts Sep 2027 off RSX27)",
      [ns.ice_canola_symbol(2027, m) for m in (4, 5, 6, 7, 8, 9)] == ["RSK27", "RSK27", "RSN27", "RSN27", "RSX27", "RSX27"])

print("ncga: what is filed")
locs = ns.fetch_ncga_bids(html=HTML, today=TODAY)
check("only the two buyers we have no feed for (Bunge Altona, Cargill West Fargo), each under its real provider",
      [(l["provider"], l["location"]) for l in locs] == [("Bunge", "Altona, MB"), ("Cargill", "West Fargo")], [(l["provider"], l["location"]) for l in locs])
check("filed under the day we looked (Oct 14), like every other scraper — with NCGA's own date (Oct 13) kept beside it",
      all(l["timestamp"] == "2026-10-14T00:00:00Z" and l["table_date"] == "2026-10-13" for l in locs), [(l["timestamp"], l["table_date"]) for l in locs])
alt = locs[0]["bids"]
check("Altona: October has no bid so four rows, Nov 2026 .. Feb 2027, basis x100 (-25 C$/t is stored -2500), each with its contract",
      [(b["delivery"], b["cme_symbol"], b["basis_cents"]) for b in alt] == [("Nov 2026", "RSX26", -2500), ("Dec 2026", "RSF27", -2000), ("Jan 2027", "RSF27", -2500), ("Feb 2027", "RSH27", -2500)],
      [(b["delivery"], b["cme_symbol"], b["basis_cents"]) for b in alt])
car = locs[1]["bids"]
check("Cargill West Fargo: five rows, Oct 2026 .. Feb 2027, and the zero basis of February is kept (0, not missing)",
      [(b["delivery"], b["basis_cents"]) for b in car] == [("Oct 2026", -2500), ("Nov 2026", -1000), ("Dec 2026", -1000), ("Jan 2027", -500), ("Feb 2027", 0)], [(b["delivery"], b["basis_cents"]) for b in car])
check("row ids carry the NCGA_ prefix and the buyer, month and year", [b["bid_id"] for b in alt] == ["NCGA_BG_202611", "NCGA_BG_202612", "NCGA_BG_202701", "NCGA_BG_202702"]
      and car[0]["bid_id"] == "NCGA_CG_202610")
check("Altona is registered as an oilseed crusher in Manitoba with its coordinates; West Fargo only needs its state", locs[0]["meta"] == {"state": "MB", "facility_type": "Oilseed Processing", "lat": 49.1, "lon": -97.567}
      and locs[1]["meta"] == {"state": "ND"})
check("the flat price NCGA shows rides along for reference (not stored)", alt[0]["price"] == 25.18 and car[0]["price"] == 26.10)

print("ncga: the year of an arrival month, across New Year")
dec = HTML.replace("Last Updated: Tue, Oct 13 2026", "Last Updated: Wed, Dec 16 2026")
dec = dec.replace("\n                        Oct\n", "\n                        Dec\n", 1)        # the first row now reads Dec (the page lists the current month on)
ld = ns.fetch_ncga_bids(html=dec, today=date(2026, 12, 16))
check("after a Dec 16 update a 'Dec' row is 2026 and 'Jan' / 'Feb' are 2027", [b["delivery"] for b in ld[1]["bids"]][:1] == ["Dec 2026"] and "Jan 2027" in [b["delivery"] for b in ld[1]["bids"]]
      and "Feb 2027" in [b["delivery"] for b in ld[1]["bids"]], [b["delivery"] for b in ld[1]["bids"]])

print("ncga: nothing is filed when the page cannot be trusted")
check("the units sentence changed -> []", ns.fetch_ncga_bids(html=HTML.replace("Canadian dollars per metric ton", "US dollars per hundredweight"), today=TODAY) == [])
check("no table -> []", ns.fetch_ncga_bids(html="<html><body><p>Last Updated: Tue, Oct 13 2026</p></body></html>", today=TODAY) == [])
check("no 'Last Updated' date -> []", ns.fetch_ncga_bids(html=HTML.replace("Last Updated: Tue, Oct 13 2026", "Updated sometime"), today=TODAY) == [])
check("empty page -> []", ns.fetch_ncga_bids(html="", today=TODAY) == [])
check("a table 5 days old (Fri table, Wed run after a holiday) is still filed; 6 days old is not (the page stopped updating)",
      len(ns.fetch_ncga_bids(html=HTML, today=date(2026, 10, 18))) == 2 and ns.fetch_ncga_bids(html=HTML, today=date(2026, 10, 19)) == [])
check("a target buyer missing from the table is skipped, the other still files",
      [l["location"] for l in ns.fetch_ncga_bids(html=HTML.replace("Bunge Altona", "Bunge Somewhere"), today=TODAY)] == ["West Fargo"])

print("ncga: the fetch tries again when the connection is flaky")
import time as _time                                         # noqa: E402


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


_calls = {"n": 0, "fail_first": 2}


def _flaky_get(url, headers=None, timeout=None):
    _calls["n"] += 1
    if _calls["n"] <= _calls["fail_first"]:
        raise ns.requests.exceptions.ConnectTimeout("simulated connect timeout")
    return _Resp(HTML)


_real_get, _real_sleep = ns.requests.get, _time.sleep
ns.requests.get, _time.sleep = _flaky_get, (lambda s: None)
try:
    got = ns.fetch_ncga_bids(today=TODAY)
    check("two timeouts then a good page: filed on the third try", len(got) == 2 and _calls["n"] == 3, (len(got), _calls["n"]))
    _calls.update(n=0, fail_first=99)
    check("three timeouts in a row: nothing filed, and no more than three tries", ns.fetch_ncga_bids(today=TODAY) == [] and _calls["n"] == 3, _calls["n"])
finally:
    ns.requests.get, _time.sleep = _real_get, _real_sleep

print("ncga: the parser")
snaps = [parse_ncga_location(l) for l in locs]
check("two snapshots, source 'ncga', the right provider / location / fetch date", [(s.provider, s.location, s.source, s.timestamp) for s in snaps]
      == [("Bunge", "Altona, MB", "ncga", "2026-10-14T00:00:00Z"), ("Cargill", "West Fargo", "ncga", "2026-10-14T00:00:00Z")])
check("the subject line says which of NCGA's tables it was", all(s.emailSubject == "NCGA cash table of 2026-10-13" and s.emailDate == "2026-10-13" for s in snaps), [(s.emailSubject, s.emailDate) for s in snaps])
check("rows: grain Canola, the inferred contract, basis x100, not spot", [(r.grain, r.deliveryMonth, r.futuresSymbol, r.basisCents, r.isSpot) for r in snaps[0].rows][:2]
      == [("Canola", "Nov 2026", "RSX26", -2500, False), ("Canola", "Dec 2026", "RSF27", -2000, False)])
check("a location with no usable bids parses to None", parse_ncga_location({"provider": "Bunge", "location": "X", "timestamp": "2026-10-13T00:00:00Z", "bids": []}) is None)

print("cargill: a flat-price-only canola bid is no longer stored as a zero basis")
flat = {"grain": "Canola", "barchart_symbol": "n.a.", "basis_str": "0.0000", "delivery_start": "10/01/2026", "delivery_end": "10/31/2026"}
corn = {"grain": "Yellow Corn", "barchart_symbol": "CZ6", "basis_str": "-0.2500", "delivery_start": "10/01/2026", "delivery_end": "10/31/2026"}
flax = {"grain": "Flaxseed", "barchart_symbol": "n.a.", "basis_str": "0.0000", "delivery_start": "10/01/2026", "delivery_end": "10/31/2026"}
cs = parse_cargill_location({"location_name": "West Fargo", "timestamp": "2026-10-13T00:00:00Z", "cashbids": [flat, dict(flat, delivery_start="11/01/2026"), flax]})
check("West Fargo's canola rows are skipped; its flaxseed (also flat-priced) is left as it was", cs is not None and [r.grain for r in cs.rows] == ["Flaxseed"], [r.grain for r in (cs.rows if cs else [])])
check("a location with nothing but flat-price canola has nothing to store", parse_cargill_location({"location_name": "West Fargo", "timestamp": "2026-10-13T00:00:00Z", "cashbids": [flat]}) is None)
cc = parse_cargill_location({"location_name": "Somewhere", "timestamp": "2026-10-13T00:00:00Z", "cashbids": [corn, flat]})
check("a corn bid with a real futures month is untouched (-0.25 -> -25, ZCZ26)", cc is not None and [(r.grain, r.futuresSymbol, r.basisCents) for r in cc.rows] == [("Corn", "ZCZ26", -25)])
cr = parse_cargill_location({"location_name": "Somewhere", "timestamp": "2026-10-13T00:00:00Z", "cashbids": [dict(flat, barchart_symbol="RSX6", basis_str="-0.2500")]})
check("a canola bid that DOES carry a futures month is kept (RSX6 -> ZRX26)", cr is not None and [(r.grain, r.futuresSymbol) for r in cr.rows] == [("Canola", "ZRX26")])

print("bunge: flat-priced winter canola (no futures month) is no longer stored as a zero basis")
from parsers.bunge_parser import parse_bunge_location       # noqa: E402

cairo = {"location_name": "Cairo, IL", "timestamp": "2026-10-14T00:00:00Z", "cashbids": [
    {"commodity": "soybeans", "delivery": "Oct '26", "symbol": "@SX26", "basis": "0.1200"},
    {"commodity": "winter-canola", "delivery": "Lower Miss", "symbol": "@RS", "basis": "0.00"},
    {"commodity": "winter-canola", "delivery": "Mid Miss", "symbol": "@RS", "basis": "0.00"},
    {"commodity": "winter-canola", "delivery": "Ohio River", "symbol": "@RS", "basis": "0.00"}]}
bc = parse_bunge_location(cairo)
check("Cairo keeps its soybean bid and drops the three barge-point canola placeholders ('@RS', basis 0.00)",
      bc is not None and [(r.grain, r.futuresSymbol, r.basisCents) for r in bc.rows] == [("Soybeans", "ZSX26", 12)], [(r.grain, r.futuresSymbol) for r in (bc.rows if bc else [])])
check("a location with nothing but those placeholders has nothing to store", parse_bunge_location(dict(cairo, cashbids=cairo["cashbids"][1:])) is None)
bk = parse_bunge_location(dict(cairo, cashbids=[{"commodity": "winter-canola", "delivery": "Jan '27", "symbol": "@RSF27", "basis": "-0.25"}]))
check("a canola bid that does name a contract (@RSF27) is kept", bk is not None and [r.grain for r in bk.rows] == ["Canola"])
check("soybean meal and corn symbols are unaffected by the contract-month rule", parse_bunge_location({"location_name": "X", "timestamp": "2026-10-14T00:00:00Z", "cashbids": [
    {"commodity": "corn", "delivery": "Oct", "symbol": "@CZ26", "basis": "-0.20"}, {"commodity": "soybean-meal", "delivery": "Oct", "symbol": "@SMZ26", "basis": "3.00"}]}).rows[1].futuresSymbol == "ZMZ26")

print("ncga: filed and read back through the database (throwaway SQLite)")
db.init_db()
cg_scrape = parse_cargill_location({"location_name": "West Fargo", "timestamp": "2026-10-14T00:00:00Z", "cashbids": [flat, flax]})
db.upsert_snapshots([cg_scrape.model_dump()])                                  # the Cargill run, first: only the flaxseed row survives
db.upsert_snapshots([s.model_dump() for s in snaps])                           # then NCGA
db.upsert_location_metas("Bunge", [locs[0]["meta"] | {"location": locs[0]["location"]}])
db.upsert_location_metas("Cargill", [locs[1]["meta"] | {"location": locs[1]["location"]}])
alt_db = db.get_snapshots("Bunge", "Altona, MB")
check("Bunge / Altona, MB reads back four canola rows, basis as quoted (-25, -20, -25, -25), and says which NCGA table they came from", len(alt_db) == 1
      and [r.basisCents for r in alt_db[0].rows] == [-25, -20, -25, -25] and alt_db[0].source == "ncga" and alt_db[0].emailSubject == "NCGA cash table of 2026-10-13", [(r.deliveryMonth, r.basisCents) for r in alt_db[0].rows] if alt_db else None)
wf = db.get_snapshots("Cargill", "West Fargo")
check("Cargill / West Fargo: ONE snapshot (still source 'web': the Cargill run made it first) holding the flaxseed row and the five NCGA canola rows, -25 .. 0",
      len(wf) == 1 and wf[0].source == "web" and sorted(r.grain for r in wf[0].rows) == ["Canola"] * 5 + ["Flaxseed"]
      and [r.basisCents for r in wf[0].rows if r.grain == "Canola"] == [-25, -10, -10, -5, 0], [(r.grain, r.basisCents) for r in wf[0].rows] if wf else None)
db.upsert_snapshots([s.model_dump() for s in snaps])
check("a second run of the same page adds nothing (same date, same row ids)", len(db.get_snapshots("Bunge", "Altona, MB")[0].rows) == 4 and len(db.get_snapshots("Cargill", "West Fargo")[0].rows) == 6)
mp = {(m["provider"], m["location"]): m for m in db.get_map_data()}
check("Altona is on the map with its coordinates and its canola bids", ("Bunge", "Altona, MB") in mp and mp[("Bunge", "Altona, MB")]["lat"] == 49.1
      and sorted(b["basis"] for b in mp[("Bunge", "Altona, MB")]["bids"]) == [-25, -25, -25, -20], mp.get(("Bunge", "Altona, MB")))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
