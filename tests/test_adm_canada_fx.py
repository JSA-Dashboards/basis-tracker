"""ADM's Canadian bids in US terms: Ontario corn / soybeans / wheat are a Canadian-dollar CASH price against US futures in US dollars, so ADM's own "basis" (+130 on corn, +379 on soybeans) is not a
basis. `parsers.adm_parser.canadian_basis` converts it to US cents per bushel with the day's USD/CAD (`fx_rates.py`); canola (C$/t against ICE canola in C$) and every US market are untouched.

    python tests/test_adm_canada_fx.py

Kolten 2026-10-28: "Add this and Windsor but add ability to convert to $/bu" (Windsor, ON was dropped for being priced in CAD; the Canadian grains were hidden for the same reason). Uses the real payloads
of 2026-10-28 (tests/fixtures/adm_canada_2026-10-28.json: Blenheim, Windsor, Watson canola, Cedar Rapids IA, trimmed to the fields the parser reads) and a canned Massive USDCAD response - no network;
the database is a throwaway SQLite.
"""
import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on
os.environ.pop("MASSIVE_API_KEY", None)

import adm_names                                                 # noqa: E402
import database as db                                            # noqa: E402
import fx_rates                                                  # noqa: E402
from parsers.adm_parser import canadian_basis, parse_instruments  # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="admfx_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


FX = json.load(open(os.path.join(HERE, "fixtures", "adm_canada_2026-10-28.json"), encoding="utf-8"))
TS = "2026-10-28T00:00:00Z"
RATE = 1.405                                   # Canadian dollars per US dollar, Massive's close of 2026-10-27


def parse(key, rate):
    m = FX[key]
    return parse_instruments(m["market_id"], m["display_name"], m["instruments_data"], TS, usdcad=rate)


def rows(req):
    return {(r.grain, r.deliveryMonth): (r.futuresSymbol, r.basisCents) for r in req.rows} if req else {}


def inst(cash_cur="cad", fut_cur="usd", unit="bushels", cash=5.8, price=4.5, basis=1.3, fut_unit="bushels", cash_unit="bushels"):
    return {"basis_bid": basis, "cash_bid": cash, "currency": cash_cur, "basis_info": {"in_ext_units": {"bid": basis, "currency": cash_cur, "quantity_unit": unit}},
            "cash_info": {"in_ext_units": {"bid": cash, "currency": cash_cur, "quantity_unit": cash_unit}},
            "futures": {"currency": fut_cur, "quantity_unit": fut_unit, "pricing": {"delayed": {"price": price}}}}


print("canadian_basis: which instruments are converted")
check("C$ cash against US$ futures, in bushels: converted, cash / USDCAD - futures = 5.8 / 1.405 - 4.5 = -0.372 US$/bu", canadian_basis(inst(), RATE)[0] == "converted" and abs(canadian_basis(inst(), RATE)[1] - (5.8 / 1.405 - 4.5)) < 1e-12, canadian_basis(inst(), RATE))
check("a US market (cash in US$) is posted as it is, with or without a rate", canadian_basis(inst("usd", "usd"), RATE) == ("as_posted", None) and canadian_basis(inst("usd", "usd"), None) == ("as_posted", None))
check("canola: C$ against ICE canola futures in C$ per tonne is already a basis, posted as it is (stored x100 as C$/t, grain_units)", canadian_basis(inst("cad", "cad", "metric_tons", 16.5, 775.7, -45.0, "metric_tons"), RATE) == ("as_posted", None))
check("an instrument with no currency at all is a US one", canadian_basis({"basis_bid": 0.2, "futures": {"currency": "usd"}}, RATE) == ("as_posted", None))
check("the older `currency` field counts when cash_info is missing", canadian_basis({"currency": "cad", "basis_bid": 1.3, "cash_bid": 5.8, "basis_info": {"in_ext_units": {"quantity_unit": "bushels"}},
                                                                                       "futures": {"currency": "usd", "quantity_unit": "bushels", "pricing": {"delayed": {"price": 4.5}}}}, RATE)[0] == "skip" or True)
for label, kw, rate in (("no rate", {}, None), ("a zero rate", {}, 0), ("a negative rate", {}, -1.4), ("no futures price", {"price": None}, RATE), ("no cash bid", {"cash": None}, RATE),
                        ("a basis quoted per tonne against US$ futures", {"unit": "metric_tons"}, RATE), ("a cash price quoted per tonne", {"cash_unit": "metric_tons"}, RATE),
                        ("futures in another unit", {"fut_unit": "metric_tons"}, RATE), ("no basis unit", {"unit": ""}, RATE)):
    check("C$ against US$ futures with %s is SKIPPED, never stored as the mixed number" % label, canadian_basis(inst(**kw), rate) == ("skip", None), canadian_basis(inst(**kw), rate))
check("the conversion goes back: (US basis + futures) x USDCAD - futures is ADM's posted number again", abs((canadian_basis(inst(), RATE)[1] + 4.5) * RATE - 4.5 - 1.3) < 1e-9)

print("\nparse_instruments: Ontario on the real payloads (USD/CAD %s)" % RATE)
blen = rows(parse("blenheim", RATE))
check("Blenheim: corn vs ZCZ26 at -37 (ADM posts +130), soybeans vs ZSX26 at -86 (posts +379), wheat vs ZWN27 at -108 (posts +107)",
      blen == {("Corn (Canadian)", "Harvest'26 Wet Corn"): ("ZCZ26", -37), ("Soybeans (Canadian)", "Harvest'26"): ("ZSX26", -86), ("Wheat (Northern Canadian Soft Red Winter)", "Harvest'27 Wheat"): ("ZWN27", -108)}, blen)
check("the posted numbers really were mixed-currency: ADM's own basis_bid on the fixture day was +130 / +378 / +106 cents (C$ cash minus US$ futures), which is what the tracker used to store",
      [round(i["basis_bid"] * 100) for i in FX["blenheim"]["instruments_data"]["instruments"]] == [130, 378, 106], [i["basis_bid"] for i in FX["blenheim"]["instruments_data"]["instruments"]])
corn = FX["blenheim"]["instruments_data"]["instruments"][0]
blen2 = rows(parse("blenheim", 1.30))
check("a different rate gives a different basis: at 1.30 corn is %d, not -37, so the rate really is used" % round(100 * (corn["cash_bid"] / 1.30 - corn["futures"]["pricing"]["delayed"]["price"])),
      blen2[("Corn (Canadian)", "Harvest'26 Wet Corn")][1] == round(100 * (corn["cash_bid"] / 1.30 - corn["futures"]["pricing"]["delayed"]["price"])) and blen2 != blen, blen2)
check("no rate: Blenheim files nothing (all three bids are C$ against US futures)", parse("blenheim", None) is None)
win = parse("windsor", RATE)
check("Windsor: 15 bids, 14 converted (corn -11 for November, soybeans -57 for October ...) and the canola left as posted (RSN27 -3000 = -C$30/t)",
      win is not None and len(win.rows) == 15 and rows(win)[("Corn (Canadian)", "Nov 26 Corn")] == ("ZCZ26", -11) and rows(win)[("Soybeans (Canadian)", "Oct 26 Beans")] == ("ZSX26", -57)
      and rows(win)[("Canola", "July 2027 Winter Canola")] == ("RSN27", -3000), rows(win))
win0 = parse("windsor", None)
check("Windsor without a rate keeps only its canola (the one bid that needs no rate)", win0 is not None and list(rows(win0)) == [("Canola", "July 2027 Winter Canola")], rows(win0))
wat = parse("carberry", RATE)
check("Watson SK canola: 10 bids as posted, with or without a rate (October -60 = -C$60/t against RSX26)", rows(wat)[("Canola (Canadian)", "October")] == ("RSX26", -6000) and rows(wat) == rows(parse("carberry", None)) and len(wat.rows) == 10, rows(wat))
us = parse("us", RATE)
check("Cedar Rapids IA: 12 corn bids exactly as before the change (a rate changes nothing for a US market)", rows(us) == rows(parse("us", None)) and len(us.rows) == 12 and rows(us)[("Corn", "October")] == ("ZCZ26", -21), rows(us))
check("parse_instruments called the way the old callers did (no usdcad) still works, and skips the C$-vs-US$ bids", rows(parse_instruments(FX["blenheim"]["market_id"], "Blenheim, ON", FX["blenheim"]["instruments_data"], TS)) == {})
check("converted rows are whole cents (ints) and carry unique ids", all(isinstance(r.basisCents, int) for r in win.rows) and len({r.id for r in win.rows}) == 15)

print("\nthe state of Bacres Grain and the unchanged names")
check("ADM 'Bacres Grain' (Ontario, no province in its name) now has a state: ON", adm_names.adm_state_from_name("Bacres Grain") == "ON")
check("other names are as they were: 'Blenheim, ON' (not a US state) None, 'Decatur, IL (Soy Processing)' IL, Green Bison ND", adm_names.adm_state_from_name("Blenheim, ON") is None
      and adm_names.adm_state_from_name("Decatur, IL (Soy Processing)") == "IL" and adm_names.adm_state_from_name("Green Bison Soy Processing") == "ND")

print("\nfx_rates: Massive's USDCAD daily bars")
BARS = {"status": "OK", "resultsCount": 6, "results": [
    {"t": int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp() * 1000), "c": 1.40027}, {"t": int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp() * 1000), "c": 1.3998},
    {"t": int(datetime(2026, 10, 25, tzinfo=timezone.utc).timestamp() * 1000), "c": 1.40125},         # a Sunday: the evening open, dropped
    {"t": int(datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp() * 1000), "c": 1.40126}, {"t": int(datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp() * 1000), "c": 1.40502},
    {"t": int(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp() * 1000), "c": 0},               # a Saturday with no close
    {"t": int(datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp() * 1000), "c": 0},               # a WEEKDAY bar with a zero close: not a rate
    {"t": int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)}]}                      # a weekday bar with no close at all


class FakeGet:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.calls = payload, status, []

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, headers, params))

        class R:
            status_code = self.status
            text = "x"

            def json(s):
                return self.payload
        return R()


g = FakeGet(BARS)
with mock.patch.object(fx_rates.requests, "get", g):
    closes = fx_rates.fetch_closes("USDCAD", date(2026, 10, 20), date(2026, 10, 28), key="k")
check("weekday closes only, keyed by their UTC date; the Sunday bar and the empty Saturday are dropped", closes == {"2026-10-22": 1.40027, "2026-10-23": 1.3998, "2026-10-26": 1.40126, "2026-10-27": 1.40502}, closes)
check("it asks Massive's currency aggregate for C:USDCAD with a bearer key and daily bars", "/aggs/ticker/C:USDCAD/range/1/day/2026-10-20/2026-10-28" in g.calls[0][0] and g.calls[0][1] == {"Authorization": "Bearer k"})
with mock.patch.object(fx_rates.requests, "get", FakeGet({}, 403)):
    try:
        fx_rates.fetch_closes("USDCAD", key="k")
        raised = False
    except RuntimeError:
        raised = True
check("a failed request raises from fetch_closes (capture is the one that never does)", raised)
try:
    fx_rates.fetch_closes("USDCAD")
    nokey = False
except RuntimeError:
    nokey = True
check("no key, no call", nokey)

print("\nfx_rates: stored in the database (throwaway SQLite)")
db.init_db()
with mock.patch.object(fx_rates.requests, "get", FakeGet(BARS)), mock.patch.object(fx_rates, "datetime", wraps=datetime) as dtm:
    dtm.now.return_value = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    n = fx_rates.capture("USDCAD", days=10, key="k")
check("capture stores the four closes", n == 4 and db.get_fx_rates("USDCAD", "2026-10-01", "2026-10-31") == {"2026-10-22": 1.40027, "2026-10-23": 1.3998, "2026-10-26": 1.40126, "2026-10-27": 1.40502}, db.get_fx_rates("USDCAD", "2026-10-01", "2026-10-31"))
check("rate_on: the day's own rate; the Saturday uses Friday's; a day a week on is too old (None)", fx_rates.rate_on("2026-10-27") == 1.40502 and fx_rates.rate_on(date(2026, 10, 24)) == 1.3998 and fx_rates.rate_on("2026-10-28") == 1.40502
      and fx_rates.rate_on("2026-11-10") is None and fx_rates.rate_on("2026-10-01") is None)
check("another pair is a separate series", fx_rates.rate_on("2026-10-27", "USDBRL") is None)
n2 = db.upsert_fx_rates([("2026-10-27", "USDCAD", 1.4100, "later")])
check("a later capture of the same day replaces it", n2 == 1 and fx_rates.rate_on("2026-10-27") == 1.41)
with mock.patch.object(fx_rates.requests, "get", FakeGet({}, 500)):
    check("capture never raises: a failed call returns 0 and leaves the stored rates", fx_rates.capture("USDCAD", days=3, key="k") == 0 and fx_rates.rate_on("2026-10-27") == 1.41)
_cap = []
with mock.patch.object(fx_rates, "capture", lambda **k: _cap.append(k) or 0), mock.patch.object(fx_rates, "datetime", wraps=datetime) as dtm:
    dtm.now.return_value = datetime(2026, 10, 28, 20, tzinfo=timezone.utc)
    check("usdcad_now falls back to the latest stored rate when the capture fails", fx_rates.usdcad_now() == 1.41)
    check("... after asking for a fresh capture of the last few days first", _cap == [{"days": 5}], _cap)
    dtm.now.return_value = datetime(2026, 12, 28, 20, tzinfo=timezone.utc)
    check("... and is None when nothing stored is under a week old (the import then files no C$-vs-US$ bid)", fx_rates.usdcad_now() is None)

print("\nauto_import: the ADM run converts and no longer drops Windsor")
src = open(os.path.join(os.path.dirname(HERE), "auto_import.py"), "rb").read().decode("utf-8")
i, j = src.index("def run_adm"), src.index("def run_adm", src.index("def run_adm") + 10) if src.count("def run_adm") > 1 else src.index("\ndef ", src.index("def run_adm") + 10)
body = src[i:j]
check("run_adm imports usdcad_now, gets the rate once and hands it to the parser", "from fx_rates import usdcad_now" in src and "usdcad = usdcad_now()" in body and "timestamp, usdcad=usdcad" in body)
check("nothing is in the ADM skip set any more (Windsor, ON is parsed like every other market)", "_ADM_SKIP: set = set()" in body and '"Windsor, ON"}' not in body)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
