"""DTN content-services scraper: the state of a location is its `states` override, then the ', ST' in its name, then the site's default (dtn_content_scraper.SITES).

    python tests/test_dtn_content_states.py

2026-10-26 (Kolten: "Both" - fix the Premier and Keystone labels): Premier Cooperative's site default was 'WI' but ten of its sites are in Iowa, and Keystone Cooperative's was 'IN' but College Corner,
Eldorado and Verona are Ohio and Fremont MI, Hamilton, Holland and White Cloud are West Michigan. fetch_dtn_content writes `state` into location_meta every day (the upsert keeps a stored value only
for None), so a label corrected in the database alone would be put back by the next run. No network: requests.get is canned; the real SITES rows are used.
"""
import logging
import os
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing anything that imports database

import dtn_content_scraper as dc      # noqa: E402

logging.disable(logging.CRITICAL)
FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:600]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def site(provider):
    return next(s for s in dc.SITES if s["provider"] == provider)


def bids(names):
    return [{"location": {"id": i, "name": n}, "commodityDisplayName": "Corn", "basisPrice": -0.25, "contractDeliveryLabel": "Dec 2026", "symbol": "@C6Z"} for i, n in enumerate(names, 1)]


PAYLOADS = {
    site("Premier Cooperative")["site_id"]: bids(["Big River Dyersville", "Bunge PDC", "Fairbanks", "Ft Atkinson/Waucoma", "Homeland (Lawler)", "Manchester", "Mazomanie", "Mineral Point", "New Albin",
                                                  "Ossian", "Postville", "Richland Center", "Valero CC", "Viserion Mcgregor", "Westby"]),
    site("Keystone Cooperative")["site_id"]: bids(["College Corner", "Columbia City", "Eldorado", "Fairground Elevator", "Fremont MI", "Hagerstown", "Hamilton", "Holland", "Legacy Feed", "Malden",
                                                   "Pershing", "Reynolds", "Scircleville Grain", "Union Mills", "Verona", "White Cloud"]),
    site("Centerra Co-op")["site_id"]: bids(["Mansfield", "Volant", "Coshocton"]),
    site("NFP")["site_id"]: bids(["Estherville, IA", "Round Lake, MN", "Switz City, IN"]),
    site("Central United Co-op")["site_id"]: bids(["Bird Island", "New Ulm", "Hector"]),
}


class FakeResp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def fake_get(url, headers=None, timeout=None):
    for sid, data in PAYLOADS.items():
        if "/sites/%s/" % sid in url:
            return FakeResp(data)
    raise AssertionError("unexpected url " + url)


print("dtn_content: the real SITES rows")
prem, key = site("Premier Cooperative"), site("Keystone Cooperative")
check("Premier keeps 'WI' as its default and overrides ten Iowa sites", prem["state"] == "WI" and len(prem["states"]) == 10 and set(prem["states"].values()) == {"IA"}, prem.get("states"))
check("Keystone keeps 'IN' as its default and overrides seven sites: three Ohio, four Michigan",
      key["state"] == "IN" and key["states"] == {"College Corner": "OH", "Eldorado": "OH", "Verona": "OH", "Fremont MI": "MI", "Hamilton": "MI", "Holland": "MI", "White Cloud": "MI"}, key.get("states"))

print("\ndtn_content: the state each location is filed with (canned responses, real config)")
keep = [site(p) for p in ("Premier Cooperative", "Keystone Cooperative", "Centerra Co-op", "NFP", "Central United Co-op")]
with mock.patch.object(dc, "SITES", keep), mock.patch.object(dc.requests, "get", fake_get):
    reqs, metas = dc.fetch_dtn_content()
st = {(m["provider"], m["location"]): m["state"] for m in metas}
for prov, loc, want in (("Premier Cooperative", "Manchester", "IA"), ("Premier Cooperative", "Postville", "IA"), ("Premier Cooperative", "Valero CC", "IA"), ("Premier Cooperative", "Fairbanks", "IA"),
                        ("Premier Cooperative", "Ft Atkinson/Waucoma", "IA"), ("Premier Cooperative", "Mazomanie", "WI"), ("Premier Cooperative", "Westby", "WI"), ("Premier Cooperative", "Richland Center", "WI"),
                        ("Premier Cooperative", "Bunge PDC", "WI"), ("Keystone Cooperative", "College Corner", "OH"), ("Keystone Cooperative", "Eldorado", "OH"), ("Keystone Cooperative", "Verona", "OH"),
                        ("Keystone Cooperative", "Fremont MI", "MI"), ("Keystone Cooperative", "Hamilton", "MI"), ("Keystone Cooperative", "Holland", "MI"), ("Keystone Cooperative", "White Cloud", "MI"),
                        ("Keystone Cooperative", "Union Mills", "IN"), ("Keystone Cooperative", "Malden", "IN"), ("Keystone Cooperative", "Pershing", "IN"), ("Centerra Co-op", "Volant", "PA"),
                        ("Centerra Co-op", "Mansfield", "OH"), ("NFP", "Estherville, IA", "IA"), ("NFP", "Round Lake, MN", "MN"), ("NFP", "Switz City, IN", "IN"), ("Central United Co-op", "New Ulm", "MN")):
    check("%s / %s -> %s" % (prov, loc, want), st.get((prov, loc)) == want, st.get((prov, loc)))
check("every location of the five sites is filed (15 + 16 + 3 + 3 + 3)", len(metas) == 40 and len(reqs) == 40, (len(metas), len(reqs)))

print("\ndtn_content: without the overrides the old labels come back (the reason the config must change, not only the database)")
old = [dict(s, states={}) if s["provider"] in ("Premier Cooperative", "Keystone Cooperative") else s for s in keep]
with mock.patch.object(dc, "SITES", old), mock.patch.object(dc.requests, "get", fake_get):
    _, metas0 = dc.fetch_dtn_content()
st0 = {(m["provider"], m["location"]): m["state"] for m in metas0}
check("with no `states`, Premier's Manchester is WI and Keystone's Verona is IN again", (st0[("Premier Cooperative", "Manchester")], st0[("Keystone Cooperative", "Verona")]) == ("WI", "IN"))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
