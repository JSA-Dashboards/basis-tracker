"""Hollander & Feuerhaken's daily bid PDF (hfgrain_scraper.py) -> Ingredion, South Bend Ethanol and Lakeside Grain bids, and Incobrasa's for the INCO series (inco_bids.fold_hf: tests/test_inco_hf_merge.py).

    python tests/test_hfgrain_scraper.py

Uses a saved copy of the real sheet (tests/fixtures/hfbids_2026-10-22.pdf: the 2026-10-22 sheet, REVISED, uploaded 15:39 CT) - no network. Reading the PDF needs pdfplumber
(requirements-dev.txt: on the droplet, not on Streamlit Cloud or every PC); without it those sections print SKIP and the pure ones still run. The last section files the rows into a
throwaway SQLite database and reads them back the way the app does.
"""
import logging
import os
import sys
import tempfile
import time as _time
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                                       # noqa: E402
import delivery_period as dp                                # noqa: E402
import hfgrain_scraper as hs                                # noqa: E402
from parsers.hfgrain_parser import parse_hfgrain_location   # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="hfgrain_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)                           # the guards log errors on purpose

try:
    import pdfplumber                                       # noqa: F401
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


PDF = open(os.path.join(HERE, "fixtures", "hfbids_2026-10-22.pdf"), "rb").read()
PAGE = ('<main><div class="sqs-html-content"><h1 style="text-align:center;white-space:pre-wrap;"><a href="/s/HFBids-20261022.pdf" target="_blank">'
        'Today’s Bid PDF</a></h1></div></main>')
CDN = "https://static1.squarespace.com/static/5acf7921a9e028b9d3e31630/t/6ada7492fc053d1287b81660/1792701586089/HFBids+20261022.pdf"
UPLOADED = datetime(2026, 10, 22, 20, 39, 46, 89000, tzinfo=timezone.utc)

print("hfgrain: finding the PDF on the Bids page")
check("the real markup: Today's Bid PDF -> the absolute /s/ address", hs.find_pdf_url(PAGE) == "https://www.hfgrain.com/s/HFBids-20261022.pdf", hs.find_pdf_url(PAGE))
check("another pdf link first does not win over the HFBids one", hs.find_pdf_url('<a href="/s/terms.pdf">Terms</a>' + PAGE) == "https://www.hfgrain.com/s/HFBids-20261022.pdf")
check("no HFBids in the address: a pdf link whose text says bid", hs.find_pdf_url('<a href="/files/sheet.pdf?x=1&amp;y=2">Today’s Bid PDF</a>') == "https://www.hfgrain.com/files/sheet.pdf?x=1&y=2")
check("no pdf link: None", hs.find_pdf_url('<a href="/about">About</a>') is None and hs.find_pdf_url("") is None and hs.find_pdf_url(None) is None)
check("the upload time is the millisecond stamp in the CDN address (Thu 2026-10-22 15:39 CT)", hs.upload_time(CDN) == UPLOADED, hs.upload_time(CDN))
check("an address without one: None", hs.upload_time("https://www.hfgrain.com/s/HFBids-20261022.pdf") is None and hs.upload_time(None) is None)

print("\nhfgrain: delivery labels (they must carry a month name)")
S = date(2026, 10, 22)
check("OCT -> 'Oct 2026'", hs.parse_period("OCT", S) == "Oct 2026")
check("a month before the sheet's month is next year's (JAN on an October sheet)", hs.parse_period("JAN", S) == "Jan 2027")
check("... and the sheet's own month and later stay this year (DEC on a Dec 30 sheet; JAN on it is next year)", hs.parse_period("DEC", date(2026, 12, 30)) == "Dec 2026" and hs.parse_period("JAN", date(2026, 12, 30)) == "Jan 2027")
check("LH OCT -> 'LH Oct 2026', F/H NOV -> 'FH Nov 2026'", hs.parse_period("LH OCT", S) == "LH Oct 2026" and hs.parse_period("F/H NOV", S) == "FH Nov 2026")
check("BY 10/25 (deliver by the 25th) -> 'By Oct 25', BY 11/10 -> 'By Nov 10'", hs.parse_period("BY 10/25", S) == "By Oct 25" and hs.parse_period("BY 11/10", S) == "By Nov 10")
check("stray spaces and case are tolerated", hs.parse_period("  by   10/25 ", S) == "By Oct 25" and hs.parse_period("lh  oct", S) == "LH Oct 2026")
check("what it does not know is None (WEEK 3, BY 13/40, an empty cell)", hs.parse_period("WEEK 3", S) is None and hs.parse_period("BY 13/40", S) is None and hs.parse_period("", S) is None)
check("delivery_period reads each label as the month it names", dp.canonical("By Oct 25", "ZCZ26") == (2026, 10) and dp.canonical("LH Oct 2026", "ZCZ26") == (2026, 10)
      and dp.canonical("Jan 2027", "ZCH27") == (2027, 1) and dp.canonical("By Nov 10", "ZCZ26") == (2026, 11))
check("'By ...' sorts as the earliest slot of its month (before the plain month)", dp.sub_rank("By Oct 25") == 0 and dp.sub_rank("Oct 2026") == 1 and dp.sub_rank("LH Oct 2026") == 2)

print("\nhfgrain: block titles")
check("'South Bend Ethanol - Corn' -> buyer and grain", hs._buyer("South Bend Ethanol - Corn") == ("south bend ethanol", "corn"))
check("a title with no grain ('CHS-Rochelle') is all buyer", hs._buyer("CHS-Rochelle") == ("chs-rochelle", None))
check("an en dash and stray spaces are tolerated", hs._buyer("Lakeside  Grain – Soybeans") == ("lakeside grain", "soybeans"))


def row(period, basis, contract, flat):
    return {"period": period, "basis": float(basis), "contract": contract, "flat": float(flat)}


SETTLE = {"CZ26": 460.5, "SX26": 1253.0, "SF27": 1264.75, "WZ26": 624.75}


def sheet(**kw):
    s = {"date": date(2026, 10, 22), "revised": True, "settle": dict(SETTLE), "blocks": [
        {"header": "South Bend Ethanol - Corn", "rows": [row("OCT", 10, "CZ26", 470.5), row("NOV", 0, "CZ26", 460.5), row("DEC", -15, "CZ26", 445.5)]},
        {"header": "Lakeside Grain - Soybeans", "rows": [row("OCT", -10, "SX26", 1243.0), row("DEC", -10, "SF27", 1254.75)]},
        {"header": "Lakeside Grain - Wheat", "rows": [row("OCT", -5, "WZ26", 619.75)]},
        {"header": "Cargill Hammond - Corn", "rows": [row("LH OCT", 0, "CZ26", 460.5)]},
        {"header": "Somebody New - Corn", "rows": [row("OCT", 0, "CZ26", 460.5)]},
    ]}
    s.update(kw)
    return s


print("\nhfgrain: from blocks to bids")
locs, rep = hs.build_locations(sheet())
check("two locations are filed: South Bend Ethanol and ONE Lakeside Grain (its soybean and wheat blocks together)",
      [(l["provider"], l["location"]) for l in locs] == [("South Bend Ethanol", "South Bend, IN"), ("Lakeside Grain", "Chicago, IL (Calumet River)")], [(l["provider"], l["location"]) for l in locs])
check("filed under the SHEET's date", all(l["timestamp"] == "2026-10-22T00:00:00Z" and l["sheet_date"] == "2026-10-22" for l in locs))
sb, lg = locs
check("South Bend: Corn, Oct/Nov/Dec 2026 vs ZCZ26, basis +10 / 0 / -15 (integer cents)", [(b["grain"], b["delivery"], b["cme_symbol"], b["basis_cents"]) for b in sb["bids"]]
      == [("Corn", "Oct 2026", "ZCZ26", 10), ("Corn", "Nov 2026", "ZCZ26", 0), ("Corn", "Dec 2026", "ZCZ26", -15)] and all(isinstance(b["basis_cents"], int) for b in sb["bids"]))
check("Lakeside: soybeans vs ZSX26 / ZSF27 and soft red winter wheat vs ZWZ26", [(b["grain"], b["delivery"], b["cme_symbol"], b["basis_cents"]) for b in lg["bids"]]
      == [("Soybeans", "Oct 2026", "ZSX26", -10), ("Soybeans", "Dec 2026", "ZSF27", -10), ("Wheat (Soft Red Winter)", "Oct 2026", "ZWZ26", -5)], lg["bids"])
check("row ids are unique and say where they came from (HF_...)", len({b["bid_id"] for b in sb["bids"] + lg["bids"]}) == 6 and all(b["bid_id"].startswith("HF_") for b in sb["bids"] + lg["bids"]), [b["bid_id"] for b in lg["bids"]])
check("a block we check but do not file is reported, not filed (Cargill Hammond: our own feed has it)", rep["checked_only"] == ["Cargill Hammond - Corn"] and "Hammond" not in str([l["location"] for l in locs]))
check("a block that is not in TARGETS is reported, not filed", rep["unmapped"] == ["Somebody New - Corn"])
check("every filed or checked row passed the settlement check (3 + 2 + 1 + 1)", rep["rows_ok"] == 7 and rep["bad_rows"] == [], rep)
check("the meta carries state, facility type and coordinates", sb["meta"] == {"state": "IN", "facility_type": "Corn Processing", "lat": 41.6764, "lon": -86.252}, sb["meta"])

bad = sheet()
bad["blocks"][0]["rows"][2] = row("DEC", -15, "CZ26", 445.0)                 # flat 445.00 against a settlement of 460.50 and a basis of -15: 0.50 out
locs2, rep2 = hs.build_locations(bad)
check("a row whose flat - basis is not the settlement is not filed; its neighbours are", [b["delivery"] for b in locs2[0]["bids"]] == ["Oct 2026", "Nov 2026"] and len(rep2["bad_rows"]) == 1, rep2["bad_rows"])
check("... and the ids stay a clean 1, 2 (they count the rows filed)", [b["bid_id"] for b in locs2[0]["bids"]] == ["HF_SB_C1", "HF_SB_C2"])
odd = sheet()
odd["blocks"][0]["rows"][0] = row("WEEK 3", 10, "CZ26", 470.5)
check("a period we do not know is a bad row, not a guess", len(hs.build_locations(odd)[1]["bad_rows"]) == 1 and len(hs.build_locations(odd)[0][0]["bids"]) == 2)
nosettle = sheet()
nosettle["blocks"][0]["rows"][0] = row("OCT", 10, "CH27", 484.25)
check("a contract missing from the settlement panel cannot be checked, so it is not filed", len(hs.build_locations(nosettle)[1]["bad_rows"]) == 1)
check("with no settlement panel at all, nothing is filed", hs.build_locations(sheet(settle={}))[0] == [])
on = sheet()
hs.TARGETS["cargill hammond"]["enabled"] = True
try:
    locs3, _ = hs.build_locations(on)
    check("flipping `enabled` files a block under its own provider/location (Hollander & Feuerhaken / Cargill Hammond (H&F sheet))", ("Hollander & Feuerhaken", "Cargill Hammond (H&F sheet)") in [(l["provider"], l["location"]) for l in locs3])
finally:
    hs.TARGETS["cargill hammond"]["enabled"] = False

s2 = sheet(blocks=[
    {"header": "Ingredion - Corn", "rows": [row("BY 10/25", 15, "CZ26", 475.5), row("OCT", 5, "CZ26", 465.5)]},
    {"header": "Incobrasa - Soybeans", "rows": [row("OCT", 20, "SX26", 1273.0), row("NOV", 25, "SX26", 1278.0), row("DEC", 15, "SF27", 1279.75)]},
    {"header": "Marquis Hennepin - Corn", "rows": [row("OCT", -12, "CZ26", 448.5)]},
])
l2, r2 = hs.build_locations(s2)
ing, inc = l2
check("Ingredion (the Argo plant) is filed in the existing Ingredion / Chicago, IL series: 'By Oct 25' +15 and Oct 2026 +5 vs ZCZ26, with Argo's coordinates",
      (ing["provider"], ing["location"], ing["merge"]) == ("Ingredion", "Chicago, IL", None) and [(b["delivery"], b["cme_symbol"], b["basis_cents"]) for b in ing["bids"]]
      == [("By Oct 25", "ZCZ26", 15), ("Oct 2026", "ZCZ26", 5)] and ing["meta"] == {"state": "IL", "facility_type": "Corn Processing", "lat": 41.77, "lon": -87.81}, ing)
check("Incobrasa is NOT a new series: it is INCO / Gilman, IL marked merge='inco', with no meta of its own (its location_meta is already there)", (inc["provider"], inc["location"], inc["merge"], inc["meta"])
      == ("INCO", "Gilman, IL", "inco", {}), inc)
check("... its rows: soybeans Oct 2026 / Nov 2026 vs ZSX26 +20 / +25 and Dec 2026 vs ZSF27 +15", [(b["grain"], b["delivery"], b["cme_symbol"], b["basis_cents"]) for b in inc["bids"]]
      == [("Soybeans", "Oct 2026", "ZSX26", 20), ("Soybeans", "Nov 2026", "ZSX26", 25), ("Soybeans", "Dec 2026", "ZSF27", 15)])
check("Marquis Hennepin is read and checked but not filed (only the four buyers Kolten named are taken)", r2["checked_only"] == ["Marquis Hennepin - Corn"] and len(l2) == 2, r2)
check("the other three not taken are off too: Cargill Hammond, ADM Mendota wheat, CHS-Rochelle", [k for k, v in hs.TARGETS.items() if not v["enabled"]] == ["marquis hennepin", "cargill hammond", "adm mendota", "chs-rochelle"]
      and [k for k, v in hs.TARGETS.items() if v["enabled"]] == ["ingredion", "south bend ethanol", "lakeside grain", "incobrasa"])

print("\nhfgrain: the parser")
snaps = [parse_hfgrain_location(dict(l, uploaded=UPLOADED)) for l in locs]
check("two snapshots, source 'hfgrain', the sheet's date as the timestamp", [(s.provider, s.location, s.source, s.timestamp) for s in snaps]
      == [("South Bend Ethanol", "South Bend, IN", "hfgrain", "2026-10-22T00:00:00Z"), ("Lakeside Grain", "Chicago, IL (Calumet River)", "hfgrain", "2026-10-22T00:00:00Z")])
check("the subject line says which sheet it was, that it was revised and when it was uploaded", all(s.emailSubject == "H&F bids PDF of 2026-10-22 (revised), uploaded 2026-10-22 20:39 UTC" and s.emailDate == "2026-10-22" for s in snaps), [s.emailSubject for s in snaps])
check("rows: grain, label, contract, basis, not spot", [(r.grain, r.deliveryMonth, r.futuresSymbol, r.basisCents, r.isSpot) for r in snaps[0].rows][0] == ("Corn", "Oct 2026", "ZCZ26", 10, False))
check("a location with no usable bids parses to None", parse_hfgrain_location({"provider": "X", "location": "Y", "timestamp": "2026-10-22T00:00:00Z", "bids": []}) is None)
dup = dict(locs[0], bids=[locs[0]["bids"][0], dict(locs[0]["bids"][0])])
check("a repeated row id is kept once", len(parse_hfgrain_location(dup).rows) == 1)
check("no upload time and not revised: a plain subject", parse_hfgrain_location(dict(locs[0], revised=False)).emailSubject == "H&F bids PDF of 2026-10-22")

print("\nhfgrain: the PDF itself (the saved sheet of 2026-10-22)")
if not HAVE_PDF:
    print("  SKIP  pdfplumber is not installed here - these checks run on the droplet and wherever requirements-dev.txt is installed")
else:
    sh = hs.extract_sheet(PDF)
    check("the sheet is dated 2026-10-22 and stamped REVISED", sh["date"] == date(2026, 10, 22) and sh["revised"] is True, (sh["date"], sh["revised"]))
    check("nine bid blocks, titled as printed", [b["header"] for b in sh["blocks"]] == [
        "Ingredion - Corn", "South Bend Ethanol - Corn", "Lakeside Grain - Soybeans", "Lakeside Grain - Wheat", "Cargill Hammond - Corn", "ADM Mendota - Wheat",
        "CHS-Rochelle", "Marquis Hennepin - Corn", "Incobrasa - Soybeans"], [b["header"] for b in sh["blocks"]])
    check("the hours / calendar tables are not mistaken for bid blocks (28 bid rows in all)", sum(len(b["rows"]) for b in sh["blocks"]) == 28)
    bb = {b["header"]: [(r["period"], r["basis"], r["contract"], r["flat"]) for r in b["rows"]] for b in sh["blocks"]}
    check("South Bend Ethanol: OCT +10, NOV 0, DEC -15 (CZ26; flat 470.50 / 460.50 / 445.50)", bb["South Bend Ethanol - Corn"] == [("OCT", 10.0, "CZ26", 470.5), ("NOV", 0.0, "CZ26", 460.5), ("DEC", -15.0, "CZ26", 445.5)], bb["South Bend Ethanol - Corn"])
    check("Marquis Hennepin: OCT -12, NOV +8, DEC +2", [(r[0], r[1]) for r in bb["Marquis Hennepin - Corn"]] == [("OCT", -12.0), ("NOV", 8.0), ("DEC", 2.0)])
    check("Lakeside wheat: WZ26 -5 / -5 / 0; soybeans SX26 -10, -10 and SF27 -10", [(r[1], r[2]) for r in bb["Lakeside Grain - Wheat"]] == [(-5.0, "WZ26"), (-5.0, "WZ26"), (0.0, "WZ26")]
          and [r[2] for r in bb["Lakeside Grain - Soybeans"]] == ["SX26", "SX26", "SF27"])
    check("a 'BY 10/25' row (Ingredion) and a 'LH OCT' row (Cargill Hammond) are read as printed", bb["Ingredion - Corn"][0] == ("BY 10/25", 15.0, "CZ26", 475.5) and bb["Cargill Hammond - Corn"][0][0] == "LH OCT")
    check("the settlement panel: 24 contracts, CZ26 460.50, SX26 1253.00, WZ26 624.75, CZ27 498.00", len(sh["settle"]) == 24 and sh["settle"]["CZ26"] == 460.5 and sh["settle"]["SX26"] == 1253.0
          and sh["settle"]["WZ26"] == 624.75 and sh["settle"]["CZ27"] == 498.0, sh["settle"])
    check("EVERY one of the 28 printed rows is flat = settlement + basis (the check the scraper relies on)", all(abs((r["flat"] - r["basis"]) - sh["settle"][r["contract"]]) < 0.011 for b in sh["blocks"] for r in b["rows"]))
    flocs, frep = hs.build_locations(sh)
    check("taken: Ingredion 4, South Bend Ethanol 3, Lakeside Grain 6 and Incobrasa 3 (for INCO); Marquis Hennepin, Cargill Hammond, ADM Mendota and CHS-Rochelle are checked only",
          [(l["provider"], len(l["bids"])) for l in flocs] == [("Ingredion", 4), ("South Bend Ethanol", 3), ("Lakeside Grain", 6), ("INCO", 3)]
          and [l["merge"] for l in flocs] == [None, None, None, "inco"] and frep["checked_only"] == ["Cargill Hammond - Corn", "ADM Mendota - Wheat", "CHS-Rochelle", "Marquis Hennepin - Corn"]
          and frep["unmapped"] == [] and frep["bad_rows"] == [] and frep["rows_ok"] == 28, (frep, [(l["provider"], len(l["bids"])) for l in flocs]))
    ib = [l for l in flocs if l["merge"] == "inco"][0]
    check("the sheet's Incobrasa rows are the ones hand-fed into INCO on 10-19 (Oct +20 ZSX26, Nov +25 ZSX26, Dec +15 ZSF27)", [(b["delivery"], b["cme_symbol"], b["basis_cents"]) for b in ib["bids"]]
          == [("Oct 2026", "ZSX26", 20), ("Nov 2026", "ZSX26", 25), ("Dec 2026", "ZSF27", 15)], ib["bids"])
    with mock.patch.object(hs, "extract_sheet", return_value=dict(sh, blocks=sh["blocks"] + [{"header": "Somebody New - Corn", "rows": [row("OCT", 0, "CZ26", 460.5)]}])):
        got = hs.fetch_hfgrain_bids(page_html=PAGE, pdf_bytes=PDF, today=date(2026, 10, 22))
    check("a new buyer on the sheet changes nothing that is filed", [(l["provider"], len(l["bids"])) for l in got] == [(l["provider"], len(l["bids"])) for l in flocs])

    print("\nhfgrain: fetch_hfgrain_bids (saved page + sheet)")
    got = hs.fetch_hfgrain_bids(page_html=PAGE, pdf_bytes=PDF, today=date(2026, 10, 22), uploaded=UPLOADED)
    check("four locations, the upload time carried through", len(got) == 4 and all(l["uploaded"] == UPLOADED for l in got))
    check("the next morning (the page still shows yesterday's sheet): still filed, under yesterday's date", len(hs.fetch_hfgrain_bids(page_html=PAGE, pdf_bytes=PDF, today=date(2026, 10, 23))) == 4
          and hs.fetch_hfgrain_bids(page_html=PAGE, pdf_bytes=PDF, today=date(2026, 10, 23))[0]["timestamp"] == "2026-10-22T00:00:00Z")
    check("a sheet 5 days old is still filed (a long weekend), 6 days old is not (the page stopped updating)", len(hs.fetch_hfgrain_bids(pdf_bytes=PDF, today=date(2026, 10, 27))) == 4 and hs.fetch_hfgrain_bids(pdf_bytes=PDF, today=date(2026, 10, 28)) == [])
    check("a sheet dated days in the future is not filed", hs.fetch_hfgrain_bids(pdf_bytes=PDF, today=date(2026, 10, 18)) == [])
    check("bytes that are not a PDF: nothing filed, no exception", hs.fetch_hfgrain_bids(pdf_bytes=b"<html>not a pdf</html>", today=date(2026, 10, 22)) == [])
    for what, patch in (("no date", dict(sh, date=None)), ("no bid blocks", dict(sh, blocks=[])), ("no settlement panel (nothing can be checked)", dict(sh, settle={}))):
        with mock.patch.object(hs, "extract_sheet", return_value=patch):
            check("a sheet with %s: nothing is filed" % what, hs.fetch_hfgrain_bids(pdf_bytes=PDF, today=date(2026, 10, 22)) == [])

    print("\nhfgrain: the whole fetch, with the network faked")

    class R:
        def __init__(self, text="", content=b"", url="", status=200):
            self.text, self.content, self.url, self.status_code = text, content, url, status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise hs.requests.HTTPError("HTTP %d" % self.status_code)

    calls = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        calls.append(url)
        if url.endswith("/bids"):
            return R(text=PAGE, url=url)
        return R(content=PDF, url=CDN)

    _real_get, _real_sleep = hs.requests.get, _time.sleep
    hs.requests.get, _time.sleep = fake_get, (lambda s: None)
    try:
        got = hs.fetch_hfgrain_bids(today=date(2026, 10, 22))
        check("page, then the PDF it links: four locations, upload time read from the CDN address", len(got) == 4 and calls == ["https://www.hfgrain.com/bids", "https://www.hfgrain.com/s/HFBids-20261022.pdf"]
              and got[0]["uploaded"] == UPLOADED, (calls, got[0]["uploaded"] if got else None))
        calls.clear()
        hs.requests.get = lambda url, **kw: (calls.append(url), R(status=500, url=url))[1]
        check("the page down three times: nothing filed, and no more than three tries", hs.fetch_hfgrain_bids(today=date(2026, 10, 22)) == [] and len(calls) == 3, calls)
        calls.clear()
        hs.requests.get = lambda url, **kw: (calls.append(url), R(text="<html>no link</html>", url=url))[1]
        check("a page with no PDF link: nothing filed, the PDF is never requested", hs.fetch_hfgrain_bids(today=date(2026, 10, 22)) == [] and len(calls) == 1, calls)
        calls.clear()
        hs.requests.get = lambda url, **kw: R(text=PAGE, url=url) if url.endswith("/bids") else R(content=b"<html>login</html>", url=url)
        check("a link that gives back an HTML page, not a PDF: nothing filed", hs.fetch_hfgrain_bids(today=date(2026, 10, 22)) == [])
    finally:
        hs.requests.get, _time.sleep = _real_get, _real_sleep

print("\nhfgrain: filed and read back through the database (throwaway SQLite)")
db.init_db()
fl = [dict(l, uploaded=UPLOADED) for l in locs]
snaps = [parse_hfgrain_location(l) for l in fl]
db.upsert_snapshots([s.model_dump() for s in snaps])
for l in fl:
    db.upsert_location_metas(l["provider"], [{"location": l["location"], **l["meta"]}])
sbd = db.get_snapshots("South Bend Ethanol", "South Bend, IN")
check("South Bend Ethanol reads back one snapshot (source 'hfgrain', the sheet's subject) with Oct / Nov / Dec 2026 at +10 / 0 / -15", len(sbd) == 1 and sbd[0].source == "hfgrain"
      and sbd[0].emailSubject == "H&F bids PDF of 2026-10-22 (revised), uploaded 2026-10-22 20:39 UTC"
      and [(r.grain, r.deliveryMonth, r.futuresSymbol, r.basisCents) for r in sbd[0].rows] == [("Corn", "Oct 2026", "ZCZ26", 10), ("Corn", "Nov 2026", "ZCZ26", 0), ("Corn", "Dec 2026", "ZCZ26", -15)],
      [(r.deliveryMonth, r.basisCents) for r in sbd[0].rows] if sbd else None)
lgd = db.get_snapshots("Lakeside Grain", "Chicago, IL (Calumet River)")
check("Lakeside Grain: ONE snapshot holding the soybean and the wheat rows", len(lgd) == 1 and sorted(r.grain for r in lgd[0].rows) == ["Soybeans", "Soybeans", "Wheat (Soft Red Winter)"], [(r.grain, r.basisCents) for r in lgd[0].rows] if lgd else None)
db.upsert_snapshots([s.model_dump() for s in snaps])
check("a second run of the same sheet adds nothing (same date, same row ids)", len(db.get_snapshots("South Bend Ethanol", "South Bend, IN")[0].rows) == 3 and len(db.get_snapshots("Lakeside Grain", "Chicago, IL (Calumet River)")[0].rows) == 3)
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT facility_type, state, lat, lon FROM location_meta WHERE provider = %s AND location = %s" % (db._ph(), db._ph()), ("South Bend Ethanol", "South Bend, IN"))
m = c.fetchone()
conn.close()
check("the location is on the map table with its type and coordinates", m is not None and m["facility_type"] == "Corn Processing" and m["state"] == "IN" and abs(m["lat"] - 41.6764) < 1e-6, dict(m) if m else None)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
