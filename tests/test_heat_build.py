"""heat_build.py - the offline builder of the Map tab's production heat data: QuickStats rows -> one number per county, the fill of the counties NASS withheld, the two files, the lattice.

    python tests/test_heat_build.py

Everything runs on canned rows and a toy county map: no NASS call, no database. The fill is a copy of rma-production-map's heat_map.fill_withheld, so its rules are tested here too.
"""
import io
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import heat_build as hb  # noqa: E402
import heat_layer as hl  # noqa: E402

try:
    import shapely  # noqa: F401
    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False

FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + str(detail)[:500]) if (not ok and str(detail)) else ""))
    if not ok:
        FAILS.append(name)


def row(st, county, value, ansi=None, practice="ALL PRODUCTION PRACTICES", cls="ALL CLASSES", fips_st="17"):
    return {"state_alpha": st, "state_fips_code": fips_st, "county_ansi": ansi or county[:3].upper().replace(" ", "0"), "county_name": county, "Value": value, "prodn_practice_desc": practice, "class_desc": cls}


def cty(rows):
    return pd.DataFrame(rows, columns=["State", "fips", "Value"])


print("one number per county")
f = hb.county_frame("Corn", [row("IL", "ADAMS", "1,000", "001"), row("IL", "ADAMS", "400", "001", "IRRIGATED"), row("IL", "ADAMS", "700", "001", "NON-IRRIGATED")])
check("the all-practices row is the county's number, not the sum or the largest", f["Value"].tolist() == [1000.0], f)
f = hb.county_frame("Corn", [row("IL", "ADAMS", "400", "001", "IRRIGATED"), row("IL", "ADAMS", "700", "001", "NON-IRRIGATED")])
check("no all-practices row: the largest row (the NASS tab's rule)", f["Value"].tolist() == [700.0], f)
f = hb.county_frame("Corn", [row("IL", "ADAMS", "1,234,567", "001")])
check("thousands separators parse", f["Value"].tolist() == [1234567.0])
f = hb.county_frame("Corn", [row("IL", "ADAMS", "(D)", "001"), row("IL", "PIKE", "5", "149")])
check("a withheld (D) county stays in the frame as 0 (the fill treats a zero as withheld)", dict(zip(f["fips"], f["Value"])) == {"17001": 0.0, "17149": 5.0}, f)
f = hb.county_frame("Corn", [row("IL", "ADAMS", "9", "001"), row("IL", "A REAL NAME", "50", "999"), row("IL", "ANOTHER NAME", "50", "998"), row("IL", "THIRD NAME", "9", "000")])
check("county codes 000, 998 and 999 are not counties, whatever the name", f["fips"].tolist() == ["17001"], f)
f = hb.county_frame("Corn", [row("IL", "ADAMS", "9", "001"), row("IL", "OTHER (COMBINED) COUNTIES", "50", "123"), row("IL", "Other Counties", "5", "124")])
check("a county named 'other ...' is a catch-all (any case), whatever its code", f["fips"].tolist() == ["17001"], f)
f = hb.county_frame("Corn", [row("IL", "ADAMS", "9", "001"), row("US", "NATION", "9", "002", fips_st="99"), row("PR", "SAN JUAN", "9", "003", fips_st="72")])
check("rows outside the 50 states (US, PR) are not counties", f["fips"].tolist() == ["17001"], f)
f = hb.county_frame("Corn", [row("AL", "BALDWIN", "5", "3", fips_st="1")])
check("FIPS codes are zero-padded: state 1 + county 3 = 01003", f["fips"].tolist() == ["01003"], f)
f = hb.county_frame("Corn", [{"state_alpha": "IL", "state_fips_code": "17", "county_ansi": "001", "county_name": "ADAMS", "Value": "8"}])
check("rows without class / practice columns still read", f["Value"].tolist() == [8.0], f)
check("no rows: an empty frame with the columns", hb.county_frame("Corn", []).empty and list(hb.county_frame("Corn", []).columns) == ["State", "fips", "Value"])
check("only catch-all rows: an empty frame", hb.county_frame("Corn", [row("IL", "OTHER COUNTIES", "5", "998")]).empty)
wheat = [row("KS", "SALINE", "100", "169", cls="WINTER", fips_st="20"), row("KS", "SALINE", "50", "169", cls="SPRING, (EXCL DURUM)", fips_st="20"), row("KS", "SALINE", "25", "169", cls="SPRING, DURUM", fips_st="20")]
check("wheat, published by class, is the sum of its classes", hb.county_frame("Wheat", wheat)["Value"].tolist() == [175.0])
check("... unless the county has an all-classes row, which stands alone (nothing counted twice)", hb.county_frame("Wheat", wheat + [row("KS", "SALINE", "160", "169", cls="ALL CLASSES", fips_st="20")])["Value"].tolist() == [160.0])
two = hb.county_frame("Wheat", wheat + [row("KS", "SALINE", "30", "169", "IRRIGATED", cls="WINTER", fips_st="20")])
check("a class's all-practices row beats its irrigated row before the classes are summed", two["Value"].tolist() == [175.0], two)

print("state totals")
st = hb.state_totals([row("IL", "", "2,343,300,000", "000"), row("IL", "", "999", "000", "IRRIGATED"), row("IA", "", "2,761,500,000", "000"), row("US", "", "9,999,999,999", "000"), row("NE", "", "(D)", "000")])
check("the all-practices row, the 50 states only, withheld left out", st == {"IL": 2343300000.0, "IA": 2761500000.0}, st)
check("the largest of several all-practices rows", hb.state_totals([row("IL", "", "5", "000"), row("IL", "", "9", "000")]) == {"IL": 9.0})
check("no rows: nothing", hb.state_totals([]) == {})

print("the QuickStats call")


class Resp:
    def __init__(self, payload):
        self.b = json.dumps(payload).encode()

    def read(self, *a):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


calls = []


def fake_open(url, timeout=0):
    calls.append(url)
    return Resp({"data": [{"state_alpha": "IL"}]})


with mock.patch.object(hb.urllib.request, "urlopen", fake_open), mock.patch.object(hb.time, "sleep", lambda s: None):
    cache = Path(tempfile.mkdtemp())
    p = {"commodity_desc": "CORN", "year": "2025"}
    r1 = hb.fetch(p, "SECRETKEY", cache)
    r2 = hb.fetch(p, "SECRETKEY", cache)
    check("rows come back", r1 == [{"state_alpha": "IL"}])
    check("the answer is cached on disk: the second ask never reaches NASS", len(calls) == 1 and r2 == r1, calls)
    check("the key goes in the URL but not in the cache's name", "key=SECRETKEY" in calls[0] and not any("SECRETKEY" in f.name for f in cache.glob("*")) and "SECRETKEY" not in "".join(f.read_text() for f in cache.glob("*")))
    hb.fetch({"commodity_desc": "SOYBEANS", "year": "2025"}, "K", cache)
    check("another query is another file", len(list(cache.glob("*.json"))) == 2)
    check("with no cache folder every ask goes out", (hb.fetch(p, "K", None), hb.fetch(p, "K", None)) and len(calls) == 4)

    def err400(url, timeout=0):
        raise hb.urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b""))
    with mock.patch.object(hb.urllib.request, "urlopen", err400):
        check("NASS's 400 means no such rows: an empty list, cached as such", hb.fetch({"a": "1"}, "K", cache) == [] and (cache / (hb.hashlib.sha1(json.dumps({"a": "1"}, sort_keys=True).encode()).hexdigest()[:16] + ".json")).exists())
    n = {"c": 0}

    def flaky(url, timeout=0):
        n["c"] += 1
        if n["c"] < 3:
            raise hb.urllib.error.URLError("timed out")
        return Resp({"data": [{"x": 1}]})
    with mock.patch.object(hb.urllib.request, "urlopen", flaky):
        check("a throttled / failed call is retried and then succeeds", hb.fetch({"b": "1"}, "K", None) == [{"x": 1}] and n["c"] == 3, n)

    def always500(url, timeout=0):
        raise hb.urllib.error.HTTPError(url, 500, "boom", {}, io.BytesIO(b""))
    with mock.patch.object(hb.urllib.request, "urlopen", always500):
        try:
            hb.fetch({"c": "1"}, "K", None)
            raised = False
        except hb.urllib.error.HTTPError:
            raised = True
        check("a call that keeps failing raises (a half-built year is never written)", raised)

print("the key")
with mock.patch.object(hb, "HERE", Path(tempfile.mkdtemp())) as _:
    env_dir = hb.HERE
    (env_dir / ".env").write_text("OTHER=1\nNASS_API_KEY='fromfile'\n", encoding="utf-8")
    with mock.patch.dict(os.environ, {"NASS_API_KEY": "fromenv"}):
        check("the argument beats the environment, which beats .env", hb._nass_key("fromarg") == "fromarg" and hb._nass_key() == "fromenv")
    with mock.patch.dict(os.environ, {}, clear=True):
        check(".env is the last resort", hb._nass_key() == "fromfile")
    (env_dir / ".env").unlink()
    with mock.patch.dict(os.environ, {}, clear=True):
        try:
            hb._nass_key()
            exited = False
        except SystemExit:
            exited = True
        check("no key anywhere: it stops and says so", exited)

print("the fill (gap_states / fill_withheld)")
check("gap_states lists only the states that fall short by more than half a percent",
      hb.gap_states(cty([("IL", "17001", 600.0), ("IA", "19001", 995.0), ("IN", "18001", 1000.0)]), {"IL": 1000.0, "IA": 1000.0, "IN": 1000.0, "NE": 500.0}) == {"IL": 400.0, "NE": 500.0})
check("... ignoring missing, zero and overshooting totals", hb.gap_states(cty([("IL", "17001", 1010.0)]), {"IL": 1000.0, "IA": 0, "IN": None, "KS": -5.0}) == {})
g = hb.gap_states(cty([("IL", "17001", 994.9)]), {"IL": 1000.0})
check("the threshold is half a percent: short by 5.1 of 1000 (0.51%) is a gap, by 4.9 (0.49%) is not, by 2% certainly is",
      list(g) == ["IL"] and math.isclose(g["IL"], 5.1) and hb.gap_states(cty([("IL", "17001", 995.1)]), {"IL": 1000.0}) == {} and hb.gap_states(cty([("IL", "17001", 980.0)]), {"IL": 1000.0}) == {"IL": 20.0}, g)
check("a state 2% short is filled, one 0.4% short is left alone", len(hb.fill_withheld(cty([("IL", "17001", 980.0)]), {"IL": 1000.0}, [(cty([("IL", "17003", 100.0)]), {"IL": 1000.0})])[0]) == 2)
cur = cty([("IL", "17001", 300.0), ("IL", "17003", 200.0), ("IL", "17005", 100.0)])
past = [(cty([("IL", "17007", 200.0)]), {"IL": 1000.0}), (cty([("IL", "17009", 50.0), ("IL", "17007", 999.0)]), {"IL": 500.0})]
rma = cty([("IL", "17001", 10.0), ("IL", "17011", 5.0), ("IL", "17007", 3.0), ("IL", "17009", 2.0)])
df, info = hb.fill_withheld(cur, {"IL": 1000.0}, past, rma)
est = df[df["Estimated"]].set_index("fips")["Value"]
check("the state adds up to the official total", abs(df["Value"].sum() - 1000.0) < 1e-9, df["Value"].sum())
check("the published counties are exactly as published", df[~df["Estimated"]].set_index("fips")["Value"].to_dict() == {"17001": 300.0, "17003": 200.0, "17005": 100.0})
check("the 400 left is split by the latest earlier share (0.20 in 2024, 0.10 in 2023; the older 17007 figure is not used) and the fallback's (0.25)",
      math.isclose(est["17007"], 400 * 0.20 / 0.55) and math.isclose(est["17009"], 400 * 0.10 / 0.55) and math.isclose(est["17011"], 400 * 0.25 / 0.55), est.to_dict())
check("the info says so", info["estimated"] == {"IL": (3, 400.0)} and info["estimated_n"] == 3 and math.isclose(info["coverage"], 0.6) and info["short"] == [])
check("a county published this year is never filled, even if history has it", hb.fill_withheld(cty([("IL", "17001", 600.0)]), {"IL": 1000.0}, [(cty([("IL", "17001", 900.0), ("IL", "17003", 100.0)]), {"IL": 1000.0})])[0].set_index("fips")["Value"].to_dict() == {"17001": 600.0, "17003": 400.0})
z, zi = hb.fill_withheld(cty([("IL", "17001", 600.0), ("IL", "17003", 0.0)]), {"IL": 1000.0}, [(cty([("IL", "17003", 100.0)]), {"IL": 1000.0})])
check("a (D) county that came through as 0 is withheld, and is filled", z.set_index("fips").loc["17003", "Value"] == 400.0 and bool(z.set_index("fips").loc["17003", "Estimated"]) and zi["estimated_n"] == 1)
s, si = hb.fill_withheld(cty([("IL", "17001", 600.0), ("IA", "19001", 100.0)]), {"IL": 1000.0, "IA": 200.0}, [(cty([("IA", "19003", 100.0)]), {"IA": 200.0})])
check("with nothing to put the remainder on a state stays short and is listed (never smeared over the published counties)", s[s["State"] == "IL"]["Value"].sum() == 600.0 and si["short"] == [("IL", 400.0)])
ts, tsi = hb.fill_withheld(cty([("IL", "17001", 600.0)]), {"IL": 1000.0, "NE": 2000.0, "KS": 900.0}, [])
check("states left short are listed largest first (NE 2000, KS 900, IL 400)", tsi["short"] == [("NE", 2000.0), ("KS", 900.0), ("IL", 400.0)] and len(ts) == 1, tsi["short"])
ne, nei = hb.fill_withheld(cty([("IA", "19001", 100.0)]), {"IA": 100.0, "NE": 800.0}, [(cty([("NE", "31001", 30.0), ("NE", "31003", 10.0)]), {"NE": 100.0})])
check("a state with no published counties is filled from history alone", math.isclose(ne.set_index("fips").loc["31001", "Value"], 600.0) and math.isclose(ne.set_index("fips").loc["31003", "Value"], 200.0) and math.isclose(nei["coverage"], 100 / 900))
check("a complete state (0.4% short) is left alone", len(hb.fill_withheld(cty([("IL", "17001", 996.0)]), {"IL": 1000.0}, [(cty([("IL", "17003", 100.0)]), {"IL": 1000.0})])[0]) == 1)
check("counties over the state total count as fully published, nothing is filled", hb.fill_withheld(cty([("OH", "39001", 1010.0)]), {"OH": 1000.0}, [])[1]["coverage"] == 1.0)
df2, info2 = hb.fill_withheld(cur, {"IL": 1000.0}, past, rma, apply=False)
check("apply=False keeps the published counties only", len(df2) == 3 and not df2["Estimated"].any() and info2["estimated_n"] == 0)
snap = (cur.copy(), rma.copy())
hb.fill_withheld(cur, {"IL": 1000.0}, past, rma)
check("the inputs are not changed", cur.equals(snap[0]) and rma.equals(snap[1]))
empty_df, empty_info = hb.fill_withheld(pd.DataFrame(columns=["State", "County", "fips", "Value"]), {"IL": 1000.0}, [])
check("no published counties and no history: empty, the state listed short", empty_df.empty and empty_info["short"] == [("IL", 1000.0)] and empty_info["coverage"] == 0.0)

print("the production file")


def loader_for(frames):
    asked = []

    def loader(crop, year):
        asked.append((crop, year))
        return frames.get((crop, year), (cty([]), {}))
    loader.asked = asked
    return loader


frames = {
    ("Corn", 2025): (cty([("IL", "17001", 600.0), ("IL", "17003", 0.0)]), {"IL": 1000.0, "OR": 50.0}),
    ("Corn", 2024): (cty([("IL", "17001", 500.0), ("IL", "17003", 300.0), ("IL", "17005", 100.0)]), {"IL": 1000.0}),
    ("Corn", 2023): (cty([("IL", "17005", 900.0)]), {"IL": 1000.0}),
    ("Soybeans", 2025): (cty([("IA", "19001", 70.0)]), {"IA": 100.0}),
    ("Soybeans", 2024): (cty([("IA", "19001", 40.0), ("IA", "19003", 60.0)]), {"IA": 100.0}),
}
ld = loader_for(frames)
rows, meta = hb.build_production(["Corn", "Soybeans"], [2024, 2025], ld, lookback=3, log=lambda *a: None)
check("the loader is asked once per crop and needed year (the targets and three before them)", sorted(set(ld.asked)) == sorted({(c, y) for c in ("Corn", "Soybeans") for y in range(2021, 2026)}) and len(ld.asked) == len(set(ld.asked)), len(ld.asked))
c25 = rows[(rows["crop"] == "Corn") & (rows["year"] == 2025)].set_index("fips")
check("corn 2025: the published county as published, the zero one filled from 2024's shares (17003 .3, 17005 .1 of the 400 left)", c25.loc["17001", "value"] == 600 and c25.loc["17003", "value"] == 300 and c25.loc["17005", "value"] == 100 and c25.loc["17003", "est"] == 1 and c25.loc["17001", "est"] == 0, c25)
check("the state adds up to its official total (600 + 300 + 100 = 1000)", c25["value"].sum() == 1000)
check("a state with a total and no county at all (OR) is not drawn and is on record", meta["Corn"]["2025"]["left_off"] == [["OR", 50]] and "OR" not in "".join(c25.index))
m = meta["Corn"]["2025"]
check("the meta holds what the caption needs", m["official"] == 1050 and m["drawn"] == 1000 and m["counties"] == 3 and m["estimated_counties"] == 2 and m["published_pct"] == round(100 * 600 / 1050, 1) and m["estimated_pct"] == 40.0, m)
check("soybeans 2025 filled from 2024's share of the other county", rows[(rows["crop"] == "Soybeans") & (rows["year"] == 2025)].set_index("fips")["value"].to_dict() == {"19001": 70, "19003": 30})
check("values are whole bushels", str(rows["value"].dtype).startswith("int") and set(rows.columns) == {"crop", "year", "fips", "value", "est"})
r2, m2 = hb.build_production(["Corn"], [2022], loader_for(frames), lookback=1, log=lambda *a: None)
check("a year NASS has no state totals for is skipped, not drawn empty", r2.empty and "Corn" not in m2)

print("writing and reading back")
out = Path(tempfile.mkdtemp())
hb.write_production(rows, meta, out)
lat = pd.DataFrame({"fips": ["17001"] * 2 + ["17003"] + ["17005"] * 2 + ["19001", "19003"], "lon": [-91.4, -91.3, -90.1, -89.9, -89.8, -94.4, -94.0], "lat": [39.5, 39.5, 39.6, 39.7, 39.7, 41.1, 41.2]})
hb.write_lattice(lat, out)
check("the three files are written", {f.name for f in out.glob("*")} == {"county_production.csv.gz", "county_production_meta.json", "county_lattice.csv.gz"})
check("heat_layer reads them", hl.available(out) and hl.years("Corn", out) == [2025, 2024] and hl.years("Soybeans", out) == [2025, 2024])
pts = hl.points("Corn", 2025, out)
check("the heat adds up to the production file: 1000 bushels over the six lattice points", abs(pts["w"].sum() - 1000.0) < 0.5 and len(pts) == 5, pts)
check("the caption's summary is the meta", hl.summary("Corn", 2025, out) == m)
out2 = Path(tempfile.mkdtemp())
hb.write_production(rows, meta, out2)
check("the same data is the same bytes (no timestamp in the gzip): a rebuild shows no diff in git", (out / "county_production.csv.gz").read_bytes() == (out2 / "county_production.csv.gz").read_bytes())

print("the lattice")
if not HAVE_SHAPELY:
    print("  (shapely is not installed: lattice tests skipped; pip install -r requirements-dev.txt)")
else:
    def box(x0, y0, x1, y1):
        return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}

    def feat(state, county, geom):
        return {"type": "Feature", "properties": {"STATE": state, "COUNTY": county, "NAME": county}, "geometry": geom}

    toy = {"features": [feat("17", "001", box(-91.5, 39.5, -91.0, 40.0)), feat("17", "149", box(-91.0, 39.5, -90.5, 40.0)), feat("17", "999", box(-90.30, 39.50, -90.28, 39.52)),
                        feat("19", "001", box(-94.5, 41.0, -94.0, 41.5)), feat("17", "000", {"type": "Banana", "coordinates": []}), feat("17", "002", {"type": "Polygon", "coordinates": []})]}
    lp = hb.lattice_points(toy)
    n = lp.groupby("fips").size().to_dict()
    check("a 0.5 x 0.5 degree county holds 25 lattice points; a sliver gets its one representative point; unreadable and empty polygons are skipped",
          n == {"17001": 25, "17149": 25, "17999": 1, "19001": 25}, n)
    k = ((lp["lon"] - hb.LATTICE_STEP / 2) / hb.LATTICE_STEP)[lp["fips"] != "17999"]
    check("points sit on one shared lattice anchored on multiples of the step (neighbours meet without a seam, and never overlap)",
          (abs(k - k.round()) < 1e-6).all() and not (set(zip(lp[lp.fips == "17001"].lon, lp[lp.fips == "17001"].lat)) & set(zip(lp[lp.fips == "17149"].lon, lp[lp.fips == "17149"].lat))))
    check("the same shapes give the same file", hb.lattice_points(toy).equals(lp))
    d3 = Path(tempfile.mkdtemp())
    hb.write_lattice(lp, d3)
    back = pd.read_csv(d3 / "county_lattice.csv.gz", dtype={"fips": str})
    check("it round-trips with its FIPS codes intact (leading zeros kept)", back["fips"].tolist() == lp["fips"].tolist() and set(back["fips"]) == {"17001", "17149", "17999", "19001"})

print("the command line")
check("year ranges", hb._years("2021-2025") == [2021, 2022, 2023, 2024, 2025] and hb._years("2024") == [2024])
try:
    hb.main(["production", "--crops", "Rye", "--key", "K"])
    stopped = False
except SystemExit as e:
    stopped = "Rye" in str(e)
check("an unknown crop stops the build before any NASS call", stopped)

print()
print("FAILED: %s" % ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
