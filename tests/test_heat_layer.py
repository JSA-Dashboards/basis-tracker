"""The production heat layer of the Map tab (heat_layer.py): files in, points / layer / caption out. A toy data folder for the rules, then the committed data files (data/heat/) for what ships.

    python tests/test_heat_layer.py

Kolten 2026-10-27: "I am wanting this on the basis dashboard" - NASS county production drawn as a heat surface under the Map tab's basis pins. No database, no network.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

import heat_layer as hl  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + str(detail)[:500]) if (not ok and str(detail)) else ""))
    if not ok:
        FAILS.append(name)


def make_dir(lattice=None, production=None, meta=None, root=None) -> Path:
    d = Path(tempfile.mkdtemp(dir=root))
    lattice = lattice if lattice is not None else pd.DataFrame({"fips": ["17001"] * 4 + ["17003"] + ["19001"] * 2, "lon": [-91.45, -91.35, -91.45, -91.35, -90.0, -94.4, -94.3], "lat": [39.55] * 2 + [39.65] * 2 + [40.0, 41.1, 41.1]})
    production = production if production is not None else pd.DataFrame({
        "crop": ["Corn"] * 4 + ["Corn", "Soybeans"], "year": [2025] * 4 + [2024, 2025], "fips": ["17001", "17003", "19001", "99999", "17001", "17001"],
        "value": [4000, 500, 0, 700, 3000, 1000], "est": [0, 1, 0, 0, 0, 0]})
    lattice.to_csv(d / "county_lattice.csv.gz", index=False)
    production.to_csv(d / "county_production.csv.gz", index=False)
    if meta is not None:
        (d / "county_production_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


print("availability")
d = make_dir()
check("a folder with both files is available", hl.available(d))
check("a missing folder is not (and nothing is raised)", hl.available(Path(tempfile.mkdtemp()) / "nope") is False)
empty = make_dir(production=pd.DataFrame({"crop": [], "year": [], "fips": [], "value": [], "est": []}))
check("a production file with no rows is not", hl.available(empty) is False)
nocol = make_dir(production=pd.DataFrame({"crop": ["Corn"], "year": [2025], "fips": ["17001"]}))
check("a production file without a value column is not", hl.available(nocol) is False)
bad = make_dir()
(bad / "county_production.csv.gz").write_bytes(b"this is not gzip")
check("a damaged file is not (the Map tab hides the control, it does not crash)", hl.available(bad) is False)
check("an empty folder (no files at all) is not", hl.available(Path(tempfile.mkdtemp())) is False)

print("years and points")
check("years are newest first, per crop", hl.years("Corn", d) == [2025, 2024] and hl.years("Soybeans", d) == [2025], hl.years("Corn", d))
check("a crop with no rows has no years", hl.years("Wheat", d) == [])
p = hl.points("Corn", 2025, d)
check("points are [lon, lat, w]", list(p.columns) == ["lon", "lat", "w"], list(p.columns))
a = p[(p["lat"] > 39.5) & (p["lat"] < 39.7) & (p["lon"] < -91)]
check("a county's value is spread evenly over its lattice points: 4000 over 4 points = 1000 each", len(a) == 4 and set(a["w"]) == {1000.0}, a)
check("a one-point county keeps its whole value (500)", 500.0 in set(p["w"]), p)
check("the weights add up to the drawn counties: 4000 + 500 (zero-valued Iowa county and the county with no lattice are left out)", abs(p["w"].sum() - 4500.0) < 1e-6, p["w"].sum())
check("a county NASS valued at zero draws nothing (no Iowa lattice point appears)", not ((p["lat"] > 41).any()))
check("a county the lattice lacks (99999) is left out, not an error", len(p) == 5)
check("another year, another crop", abs(hl.points("Corn", 2024, d)["w"].sum() - 3000.0) < 1e-6 and abs(hl.points("Soybeans", 2025, d)["w"].sum() - 1000.0) < 1e-6)
none = hl.points("Wheat", 2025, d)
check("no such crop-year: an empty frame with the same columns", none.empty and list(none.columns) == ["lon", "lat", "w"])
check("the same data gives the same points", hl.points("Corn", 2025, d).equals(hl.points("Corn", 2025, d)))

print("summary")
meta = {"Corn": {"2025": {"official": 5_000_000, "drawn": 4_500_000, "counties": 2, "estimated_counties": 1, "published_pct": 80.0, "estimated_pct": 11.1, "left_off": [["OR", 17700000]]}}}
dm = make_dir(meta=meta)
check("the meta file's entry is returned as is", hl.summary("Corn", 2025, dm) == meta["Corn"]["2025"])
s = hl.summary("Corn", 2025, d)
check("without meta it is recomputed from the rows: 5200 over 3 counties (the zero-valued one is not counted), 500 of it estimated",
      s["drawn"] == 5200.0 and s["counties"] == 3 and s["estimated_counties"] == 1 and s["official"] is None and s["published_pct"] is None and abs(s["estimated_pct"] - 9.6) < 0.1, s)
check("no such crop-year: None", hl.summary("Wheat", 2025, d) is None)

print("radius")
r = hl.radius_px(150)
check("150 km at the Map tab's opening view is a sensible radius", 25 <= r <= 60, r)
check("more kilometres, more pixels", hl.radius_px(300) > hl.radius_px(100) > hl.radius_px(40))
check("zoomed in, the same kilometres are more pixels", hl.radius_px(100, 40, 6) > hl.radius_px(100, 40, 4))
check("clamped at both ends", hl.radius_px(1) == 12 and hl.radius_px(300, 40, 12) == 220)

print("the pydeck layer")
import pydeck as pdk  # noqa: E402

lay = hl.layer(p, 40, "Green")
check("a HeatmapLayer", lay.type == "HeatmapLayer")
check("the data goes in as records (pydeck cannot serialise a DataFrame under pandas 3)", isinstance(lay.data, list) and isinstance(lay.data[0], dict) and set(lay.data[0]) == {"lon", "lat", "w"})
check("the palette and radius are used; an unknown palette is the heat one", lay.color_range == hl.PALETTES["Green"] and lay.radius_pixels == 40 and hl.layer(p, 40, "Nope").color_range == hl.PALETTES["Heat"])
check("not pickable: the pins above it keep the tooltip", lay.pickable is False)
check("it serialises inside a deck", '"HeatmapLayer"' in pdk.Deck(layers=[lay]).to_json())
check("every palette is six RGBA stops, transparent at the low end", all(len(v) == 6 and v[0][3] == 0 and all(len(c) == 4 for c in v) for v in hl.PALETTES.values()))

print("legend and caption")
lg = hl.legend_html("Heat")
check("the legend names both ends and carries a gradient", "Less concentrated" in lg and "More concentrated" in lg and "linear-gradient" in lg and lg.count("rgba(") == 6)
check("an unknown palette falls back", hl.legend_html("Nope") == lg)
check("amounts read as M / B bushels", hl.fmt_bu(12_345_678) == "12.3M bu" and hl.fmt_bu(16_890_000_000) == "16.89B bu" and hl.fmt_bu(12_345) == "12K bu" and hl.fmt_bu(999) == "999 bu")
cap = hl.describe("Corn", 2025, 150, meta["Corn"]["2025"])
check("the caption says what is drawn and how", "Corn 2025" in cap and "4.5M bu" in cap and "2 counties" in cap and "150 km" in cap, cap)
check("... how much NASS published and how the rest was filled", "published 80%" in cap and "1 counties NASS withheld" in cap and "earlier years" in cap, cap)
check("... and what is left off the map", "Not drawn" in cap and "OR (17.7M bu)" in cap, cap)
check("a mostly-estimated year is warned about, a normal one is not", "mostly an estimate" in hl.describe("Wheat", 2024, 150, {**meta["Corn"]["2025"], "published_pct": 1.9, "estimated_pct": 98.0}) and "mostly an estimate" not in cap)
check("no estimated counties: no 'withheld' sentence", "withheld" not in hl.describe("Soybeans", 2025, 150, {"drawn": 5e9, "counties": 1600, "estimated_counties": 0, "estimated_pct": 0.0, "published_pct": 100.0, "left_off": []}))
check("only the six biggest left-off states are named", hl.describe("Corn", 2025, 150, {**meta["Corn"]["2025"], "left_off": [[c, 1e6 * (9 - i)] for i, c in enumerate("ABCDEFGH")]}).count(" bu)") == 6)
check("no summary: a plain message", hl.describe("Corn", 1999, 150, None) == "No Corn production for 1999.")

print("the files that ship (data/heat/)")
check("they are there and readable", hl.available())
for crop in hl.CROPS:
    ys = hl.years(crop)
    check(f"{crop}: five crop years, newest first", ys == sorted(ys, reverse=True) and len(ys) == 5, ys)
lat_all, prod_all, meta_all = hl._tables(str(hl.DATA))
orphans = prod_all[~prod_all["fips"].isin(set(lat_all["fips"]))]
check("every county with production has a lattice (a county the shapes lack would silently vanish)", orphans["value"].sum() < 0.001 * prod_all["value"].sum(), orphans.head())
bad_years = []
for crop in hl.CROPS:
    for y in hl.years(crop):
        s = hl.summary(crop, y)
        pts = hl.points(crop, y)
        if s is None or not (0.995 * s["drawn"] <= pts["w"].sum() <= 1.0005 * s["drawn"]) or s["official"] is None or not (0.95 * s["official"] <= s["drawn"] <= 1.001 * s["official"]):
            bad_years.append((crop, y, s and s["drawn"], pts["w"].sum(), s and s["official"]))
check("every crop-year: the heat adds up to what the meta says is drawn, and that is within 5% of NASS's official total", not bad_years, bad_years)
c25 = hl.summary("Corn", 2025)
check("US corn 2025: 16.9B bushels over some 1,900 counties, 72% published (the figures the rma-production-map heat tab showed)", 16.5e9 < c25["drawn"] < 17.1e9 and 1800 < c25["counties"] < 2000 and 70 <= c25["published_pct"] <= 74, c25)
check("the 2024 wheat gap is on record: NASS published almost no county figures", hl.summary("Wheat", 2024)["published_pct"] < hl.MOSTLY_ESTIMATED and "mostly an estimate" in hl.describe("Wheat", 2024, 150, hl.summary("Wheat", 2024)))
sizes = sum(f.stat().st_size for f in Path(hl.DATA).glob("*"))
check("the whole data folder is small enough to live in the repo (under 1 MB)", sizes < 1_000_000, sizes)

print()
print("FAILED: %s" % ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
