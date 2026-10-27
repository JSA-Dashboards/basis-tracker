"""
heat_layer.py - the production heat layer of the Map tab: where a crop is grown, drawn as a heat surface under the basis pins.

Kolten 2026-10-27: "I am wanting this on the basis dashboard" (the production heat map had first been built in rma-production-map's own dashboard), after "Lets start with production
concentration map but we will trend towards the basis on the map concept". This is the production half; basis on the same canvas is the Map tab's own pins, and the next layer.

It runs on small committed files that heat_build.py makes (run by hand when NASS publishes a crop year), so the app needs no NASS key, no county shapes and no shapely:
  data/heat/county_lattice.csv.gz          fips, lon, lat      a 0.1-degree lattice of points inside every county
  data/heat/county_production.csv.gz       crop, year, fips, value, est      NASS production in bushels; counties NASS withheld are filled (heat_build.fill_withheld)
  data/heat/county_production_meta.json    per crop and year: the official total, what was drawn, the published share, the states left off
A county's value is divided evenly over its lattice points, so the glow adds up to the NASS number, follows county shapes, and the same data always gives the same picture.
Everything here is pure (files in, frames / strings out); the Map tab in app.py only places the controls and hands the layer to its deck.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data" / "heat"
CROPS = ("Corn", "Soybeans", "Wheat")
US_VIEW = (39.5, -98.35, 3.8)                                # lat, lon, zoom: the Map tab's own opening view
MOSTLY_ESTIMATED = 50                                        # below this published share (%) the caption warns that the year is mostly an estimate (NASS published 2% of 2024 wheat's counties)

# Low density is transparent so the basemap and the pins show through; six stops is all deck.gl takes.
PALETTES = {
    "Heat":  [[255, 255, 178, 0], [254, 217, 118, 95], [254, 178, 76, 145], [253, 141, 60, 195], [240, 59, 32, 228], [189, 0, 38, 255]],
    "Green": [[225, 240, 230, 0], [190, 225, 200, 85], [130, 200, 160, 145], [70, 165, 120, 195], [30, 120, 85, 228], [10, 75, 55, 255]],
}


@lru_cache(maxsize=4)
def _tables(data_dir: str) -> tuple:
    """(lattice with the number of points per county, production rows, meta) read once per process and folder. Raises if a file is missing or unreadable."""
    d = Path(data_dir)
    lattice = pd.read_csv(d / "county_lattice.csv.gz", dtype={"fips": str})
    lattice["n"] = lattice.groupby("fips")["fips"].transform("size")
    prod = pd.read_csv(d / "county_production.csv.gz", dtype={"fips": str})
    meta_file = d / "county_production_meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    return lattice, prod, meta


def available(data_dir=None) -> bool:
    """True when the data files are there and readable and hold at least one crop. The Map tab hides the control otherwise, so a copy of the app without the files (a portal mirror) still works."""
    try:
        _, prod, _ = _tables(str(data_dir or DATA))
        return len(prod) > 0 and {"crop", "year", "fips", "value"} <= set(prod.columns)
    except Exception:                                         # noqa: BLE001 - a missing / damaged file must not take the map down
        return False


def years(crop: str, data_dir=None) -> list:
    """The crop years held for a crop, newest first."""
    _, prod, _ = _tables(str(data_dir or DATA))
    return sorted((int(y) for y in prod.loc[prod["crop"] == crop, "year"].unique()), reverse=True)


def points(crop: str, year: int, data_dir=None) -> pd.DataFrame:
    """[lon, lat, w]: every county's production spread evenly over its lattice points, w being the point's share in bushels, so the weights add up to the county's value (to the 0.1 bu the
    weights are rounded to). Counties without a lattice (a code the shape file lacks) are left out."""
    lattice, prod, _ = _tables(str(data_dir or DATA))
    v = prod[(prod["crop"] == crop) & (prod["year"] == int(year)) & (prod["value"] > 0)]
    m = lattice.merge(v[["fips", "value"]], on="fips", how="inner")
    if m.empty:
        return pd.DataFrame({"lon": [], "lat": [], "w": []})
    m["w"] = (m["value"] / m["n"]).round(1)
    return m[["lon", "lat", "w"]].reset_index(drop=True)


def summary(crop: str, year: int, data_dir=None) -> dict | None:
    """What a crop-year holds: drawn (bushels on the map), official (NASS's state totals added up), counties, estimated_counties, published_pct, estimated_pct, left_off [[State, bushels]].
    From the meta file; recomputed from the rows (official and published_pct then None) when the file has no entry. None when there is no such crop-year."""
    _, prod, meta = _tables(str(data_dir or DATA))
    m = (meta.get(crop) or {}).get(str(int(year)))
    if m:
        return m
    v = prod[(prod["crop"] == crop) & (prod["year"] == int(year)) & (prod["value"] > 0)]
    if v.empty:
        return None
    drawn = float(v["value"].sum())
    est = float(v.loc[v["est"] == 1, "value"].sum()) if "est" in v.columns else 0.0
    return {"official": None, "drawn": drawn, "counties": int(len(v)), "estimated_counties": int((v["est"] == 1).sum()) if "est" in v.columns else 0,
            "published_pct": None, "estimated_pct": round(100 * est / drawn, 1) if drawn else 0.0, "left_off": []}


# -- drawing ---------------------------------------------------------------------------------------
def radius_px(km: float, lat: float = US_VIEW[0], zoom: float = US_VIEW[2]) -> int:
    """The HeatmapLayer radius, in screen pixels, that smooths over about `km` kilometres at a latitude and zoom. The layer's visible kernel is only about a third of its radius, so a fixed
    pixel radius is far too tight at close zoom (the lattice rows show as stripes): the smoothing is asked for in kilometres and converted here."""
    m_per_px = 78271.517 * math.cos(math.radians(lat)) / (2 ** zoom)         # Web-Mercator metres per pixel on a 512-px-tile map (deck.gl / mapbox-gl)
    return int(max(12, min(220, round(km * 1000 / m_per_px))))


def layer(pts: pd.DataFrame, radius: int, palette: str = "Heat", opacity: float = 0.9):
    """The pydeck HeatmapLayer for the points. The data goes in as records: pydeck cannot serialise a DataFrame under pandas 3."""
    import pydeck as pdk
    return pdk.Layer("HeatmapLayer", data=pts[["lon", "lat", "w"]].to_dict("records"), get_position="[lon, lat]", get_weight="w", radius_pixels=int(radius), intensity=1.0,
                     threshold=0.04, aggregation="SUM", color_range=PALETTES.get(palette, PALETTES["Heat"]), opacity=float(opacity), pickable=False)


def legend_html(palette: str = "Heat") -> str:
    """The 'Less -> More concentrated' gradient bar."""
    ramp = PALETTES.get(palette, PALETTES["Heat"])
    gradient = ", ".join(f"rgba({r},{g},{b},{max(a, 25) / 255:.2f})" for r, g, b, a in ramp)
    return ("<div style='display:flex;align-items:center;gap:10px;margin:2px 0 6px 0;font-size:11px;color:#64748b'><span>Less concentrated</span>"
            f"<div style='flex:0 0 220px;height:10px;border-radius:5px;background:linear-gradient(90deg,{gradient});border:1px solid #d7e2dc'></div><span>More concentrated</span></div>")


def fmt_bu(v: float) -> str:
    """12,345,678 -> '12.3M bu'."""
    a = abs(v)
    s = f"{v / 1e9:.2f}B" if a >= 1e9 else f"{v / 1e6:.1f}M" if a >= 1e6 else f"{v / 1e3:.0f}K" if a >= 1e3 else f"{v:,.0f}"
    return f"{s} bu"


def describe(crop: str, year: int, km: float, s: dict | None) -> str:
    """The caption under the map: what is drawn, how much of it NASS published, how the rest was filled, what is left off."""
    if not s:
        return f"No {crop} production for {year}."
    parts = [f"{crop} {year}: NASS county production, {fmt_bu(s['drawn'])} over {s['counties']:,} counties, blurred about {km} km; the colour is relative to the busiest part of the map."]
    if s.get("estimated_counties"):
        pub = f"NASS published {s['published_pct']:.0f}% of the state totals; " if s.get("published_pct") is not None else ""
        parts.append(f"{pub}the other {s['estimated_pct']:.0f}% sits in {s['estimated_counties']:,} counties NASS withheld, filled in from each county's share of its state in earlier years "
                     "(published figures are as published).")
        if s.get("published_pct") is not None and s["published_pct"] < MOSTLY_ESTIMATED:
            parts.insert(0, f"⚠ NASS published only {s['published_pct']:.0f}% of the county figures for {year}: this map is mostly an estimate from earlier years' county shares scaled to the state totals.")
    if s.get("left_off"):
        parts.append("Not drawn (no county figures at all): " + ", ".join(f"{st} ({fmt_bu(b)})" for st, b in s["left_off"][:6]) + ".")
    return "  \n".join(parts)
