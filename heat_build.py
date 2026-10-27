"""
heat_build.py - builds the data files behind the Map tab's production heat layer (heat_layer.py). Run by hand when NASS publishes a new crop year (the county figures land after the
January Crop Production Annual Summary); the app itself never calls NASS and needs no key, no shapes and no shapely.

    python heat_build.py lattice                                    # data/heat/county_lattice.csv.gz        (once: the county shapes do not change)
    python heat_build.py production [--years 2021-2025] [--crops Corn,Soybeans,Wheat]
                                                                    # data/heat/county_production.csv.gz + county_production_meta.json
    NASS key: --key, else NASS_API_KEY in the environment, else NASS_API_KEY= in this folder's .env.   Needs shapely (requirements-dev.txt) for `lattice`.

What the production file holds. NASS county production in bushels, one number per county, the way the NASS tab of rma-production-map reads it (the all-practices row, else the largest;
wheat summed over its classes). NASS publishes fewer county figures every year - published counties held 90% of the US corn crop in 2022 and 73% in 2024 and 2025 (Illinois 100% -> 63%) -
so a year's published counties are completed with fill_withheld: every published figure is kept as published, and what a state's published counties leave of NASS's official state total
is spread over its withheld counties by their share of the state in the latest of the LOOKBACK earlier years NASS published them. Every state then adds up to NASS's total. (This is a
copy of rma-production-map's heat_map.fill_withheld, tested there; without that app's RMA fallback, which is why LOOKBACK is 5 here, not 3. A state with no county at all stays off
the map and is listed in the meta file.)

How the lattice works. A county's value is divided evenly over a regular 0.1-degree lattice of points inside its own polygon, so the heat follows county shapes and conserves every bushel
(random dots speckle; this does not). The lattice is anchored on multiples of the step, so neighbouring counties share one lattice and meet without a seam.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
OUT = HERE / "data" / "heat"
CACHE = HERE / "state" / "heat_cache"                        # raw QuickStats answers, so a re-run does not ask NASS again (state/ is gitignored)
NASS_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
COUNTY_GEOJSON = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"

LATTICE_STEP = 0.1          # degrees (about 9 x 11 km)
MIN_GAP = 0.005             # a state whose published counties come within 0.5% of its official total counts as complete
LOOKBACK = 5                # earlier years consulted for a withheld county's share of its state
ALL_PRACTICES = "ALL PRODUCTION PRACTICES"
STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())

# QuickStats parameters, as the NASS tab's loaders use them (rma-production-map app.py: NASS_CROP_PARAMS for counties, NASS_CROP_STAT_PARAMS for states)
CROPS = {
    "Corn": {"commodity_desc": "CORN", "util_practice_desc": "GRAIN"},
    "Soybeans": {"commodity_desc": "SOYBEANS"},
    "Wheat": {"commodity_desc": "WHEAT"},                    # county rows come by class (winter, spring, durum): summed
}
STATE_EXTRA = {"Wheat": {"class_desc": "ALL CLASSES"}}
BASE = {"source_desc": "SURVEY", "sector_desc": "CROPS", "statisticcat_desc": "PRODUCTION", "unit_desc": "BU"}


# -- QuickStats ---------------------------------------------------------------------------
def _nass_key(arg: str | None = None) -> str:
    key = arg or os.environ.get("NASS_API_KEY", "")
    if not key and (HERE / ".env").exists():
        m = re.search(r"^NASS_API_KEY\s*=\s*['\"]?([^'\"\s]+)", (HERE / ".env").read_text(encoding="utf-8"), re.M)
        key = m.group(1) if m else ""
    if not key:
        sys.exit("No NASS key: pass --key, set NASS_API_KEY, or put NASS_API_KEY=... in .env")
    return key


def fetch(params: dict, key: str, cache: Path | None = CACHE, pause: float = 0.4) -> list:
    """The QuickStats rows for one query. NASS answers HTTP 400 for 'no such rows': that is []. A throttled or failed call is retried, then raised. The answer is cached on disk by the query
    (the key is not part of it)."""
    sig = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    f = cache / f"{sig}.json" if cache else None
    if f is not None and f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    url = NASS_URL + "?" + urllib.parse.urlencode({**params, "key": key, "format": "JSON"})
    rows = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                rows = json.load(r).get("data", [])
            break
        except urllib.error.HTTPError as e:
            if e.code == 400:
                rows = []
                break
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(5 * (attempt + 1))
    if f is not None:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rows), encoding="utf-8")
    time.sleep(pause)
    return rows


def _num(s: pd.Series) -> pd.Series:
    """QuickStats values are strings: '1,234' is 1234; '(D)' (withheld) and '(Z)' become 0, which the fill treats as withheld."""
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce").fillna(0.0)


def county_frame(crop: str, rows: list) -> pd.DataFrame:
    """[State, fips, Value] for one crop-year from QuickStats county rows: one number per county. A county's all-practices row is its number (else its largest row); wheat, which NASS
    publishes by class, is the county's 'all classes' row when it has one, else the sum of its classes. NASS's 'other counties' catch-alls and county codes 000 / 998 / 999 are not counties."""
    cols = ["State", "fips", "Value"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    for c in ("class_desc", "prodn_practice_desc"):
        if c not in df.columns:
            df[c] = ""
    df = df[~df["county_ansi"].isin(["998", "000", "999"]) & ~df["county_name"].str.strip().str.lower().str.startswith("other") & df["state_alpha"].isin(STATES)].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["Value"] = _num(df["Value"])
    df["State"] = df["state_alpha"].str.strip()
    df["fips"] = df["state_fips_code"].astype(str).str.zfill(2) + df["county_ansi"].astype(str).str.zfill(3)
    df["_all"] = df["prodn_practice_desc"].eq(ALL_PRACTICES)
    df = df.sort_values(["fips", "class_desc", "_all", "Value"], ascending=[True, True, False, False]).drop_duplicates(["fips", "class_desc"])
    is_all_classes = df["class_desc"].eq("ALL CLASSES")
    all_classes_fips = set(df.loc[is_all_classes, "fips"])
    df = df[is_all_classes | ~df["fips"].isin(all_classes_fips)]
    return df.groupby(["State", "fips"], as_index=False)["Value"].sum()[cols]


def state_totals(rows: list) -> dict:
    """{State: NASS's official state total} from QuickStats state rows (domain TOTAL, the annual figure): the all-practices row, the largest if there are several; the 50 states only."""
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    if "prodn_practice_desc" not in df.columns:
        df["prodn_practice_desc"] = ""
    df = df[df["state_alpha"].isin(STATES)].copy()
    df["Value"] = _num(df["Value"])
    df["_all"] = df["prodn_practice_desc"].eq(ALL_PRACTICES)
    df = df.sort_values(["state_alpha", "_all", "Value"], ascending=[True, False, False]).drop_duplicates("state_alpha")
    return {s: float(v) for s, v in zip(df["state_alpha"], df["Value"]) if v > 0}


def load_year(crop: str, year: int, key: str, cache: Path | None = CACHE) -> tuple:
    """(county frame, {State: official total}) for one crop-year, from NASS."""
    county = fetch({**BASE, **CROPS[crop], "agg_level_desc": "COUNTY", "year": str(year)}, key, cache)
    state = fetch({**BASE, **CROPS[crop], **STATE_EXTRA.get(crop, {}), "agg_level_desc": "STATE", "domain_desc": "TOTAL", "reference_period_desc": "YEAR", "year": str(year)}, key, cache)
    return county_frame(crop, county), state_totals(state)


# -- the fill (a copy of rma-production-map/heat_map.py) --------------------------------------------
def gap_states(county_df: pd.DataFrame, state_totals_: dict) -> dict:
    """{State: amount the published counties fall short of the official state total}, for the states short by more than MIN_GAP."""
    sums = county_df.groupby("State")["Value"].sum() if len(county_df) else pd.Series(dtype=float)
    out = {}
    for st_, tot in state_totals_.items():
        if tot and tot > 0:
            short = float(tot) - float(sums.get(st_, 0.0))
            if short > MIN_GAP * float(tot):
                out[st_] = short
    return out


def _shares(county_df: pd.DataFrame, state_totals_: dict) -> dict:
    """{State: {fips: the county's share of its state's official total}} for one year; a county without a positive value, or in a state without a total, is left out."""
    out: dict = {}
    for st_, fips, v in zip(county_df["State"], county_df["fips"], county_df["Value"]):
        t = state_totals_.get(st_)
        if t and t > 0 and v and v > 0:
            d = out.setdefault(st_, {})
            d[fips] = d.get(fips, 0.0) + float(v) / float(t)
    return out


def fill_withheld(county_df: pd.DataFrame, state_totals_: dict, history=(), fallback: pd.DataFrame | None = None, apply: bool = True) -> tuple:
    """Complete a year's counties to NASS's official state totals. The counties NASS published keep their published numbers; what a state's published counties leave of its official total is
    spread over its withheld counties in proportion to the share each held of its state in the latest earlier year NASS published it (the county's usual weight within the state), or, for a
    county NASS never published in those years, its share of the state's `fallback` values. A state with no such county at all is left short and listed in info['short'].
      county_df [State, fips, Value] published this year;  state_totals_ {State: total};  history [(county_df, state_totals), ...] earlier years, most recent first
    Returns (frame [State, fips, Value, Estimated], info) with info = coverage (published share of the official totals, states over 100% count as 100%), estimated {State: (counties filled,
    amount)}, estimated_n, estimated_total, short [(State, amount)] largest first."""
    df = county_df[["State", "fips", "Value"]]
    df = df[df["Value"] > 0].copy()                                           # a (D) comes through as 0: that county is withheld, not empty
    df["Estimated"] = False
    gaps = gap_states(df, state_totals_)
    sums = df.groupby("State")["Value"].sum() if len(df) else pd.Series(dtype=float)
    den = sum(float(t) for t in state_totals_.values() if t and t > 0)
    num = sum(min(float(sums.get(s, 0.0)), float(t)) for s, t in state_totals_.items() if t and t > 0)
    info = {"coverage": (num / den) if den else None, "estimated": {}, "estimated_n": 0, "estimated_total": 0.0, "short": []}
    if not apply or not gaps:
        return df, info
    past = [_shares(c, t) for c, t in history]
    rma: dict = {}
    if fallback is not None and len(fallback):
        fsum = fallback.groupby("State")["Value"].sum()
        for st_, fips, v in zip(fallback["State"], fallback["fips"], fallback["Value"]):
            if v and v > 0 and fsum.get(st_, 0) > 0:
                rma.setdefault(st_, {})[fips] = float(v) / float(fsum[st_])
    have = set(df["fips"])
    rows = []
    for st_, short in gaps.items():
        w: dict = {}
        for src in [h.get(st_, {}) for h in past] + [rma.get(st_, {})]:        # latest earlier year first
            for fips, s in src.items():
                if fips not in have and fips not in w:
                    w[fips] = s
        total_w = sum(w.values())
        if total_w <= 0:
            info["short"].append((st_, short))
            continue
        rows.extend((st_, fips, short * s / total_w, True) for fips, s in w.items())
        info["estimated"][st_] = (len(w), short)
    if rows:
        df = pd.concat([df, pd.DataFrame(rows, columns=["State", "fips", "Value", "Estimated"])], ignore_index=True)
    info["estimated_n"] = len(rows)
    info["estimated_total"] = sum(a for _, a in info["estimated"].values())
    info["short"].sort(key=lambda t: -t[1])
    return df, info


# -- the production file ------------------------------------------------------------------------------
def build_production(crops, years, loader, lookback: int = LOOKBACK, log=print) -> tuple:
    """(rows frame [crop, year, fips, value, est], meta) for every crop and year. `loader(crop, year)` gives (county frame, state totals): NASS in practice, canned in tests. A target year
    NASS has no state totals for yet is skipped."""
    need = sorted({y - k for y in years for k in range(0, lookback + 1)})
    frames: dict = {}
    for crop in crops:
        for y in need:
            frames[(crop, y)] = loader(crop, y)
            log(f"  {crop} {y}: {len(frames[(crop, y)][0])} counties, {len(frames[(crop, y)][1])} states")
    out, meta = [], {}
    for crop in crops:
        for y in years:
            cur, tot = frames[(crop, y)]
            if not tot:
                log(f"  {crop} {y}: no state totals, skipped")
                continue
            past = [frames[(crop, y - k)] for k in range(1, lookback + 1) if (crop, y - k) in frames]
            filled, info = fill_withheld(cur, tot, [(c, t) for c, t in past if len(c) and t])             # every row it returns is a positive number
            drawn = float(filled["Value"].sum())
            out.append(pd.DataFrame({"crop": crop, "year": y, "fips": filled["fips"].values, "value": np.round(filled["Value"].values).astype("int64"),
                                     "est": filled["Estimated"].astype(int).values}))
            est = float(filled.loc[filled["Estimated"], "Value"].sum())
            meta.setdefault(crop, {})[str(y)] = {
                "official": round(sum(float(t) for t in tot.values())), "drawn": round(drawn), "counties": int(len(filled)), "estimated_counties": int(filled["Estimated"].sum()),
                "published_pct": round(100 * info["coverage"], 1) if info["coverage"] is not None else None, "estimated_pct": round(100 * est / drawn, 1) if drawn else 0.0,
                "left_off": [[s, round(a)] for s, a in info["short"]]}
            log(f"  {crop} {y}: {len(filled)} counties drawn, {meta[crop][str(y)]['published_pct']}% published, {meta[crop][str(y)]['estimated_pct']}% estimated, left off {len(info['short'])} states")
    rows = pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["crop", "year", "fips", "value", "est"])
    return rows, meta


def write_production(rows: pd.DataFrame, meta: dict, out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows.sort_values(["crop", "year", "fips"]).to_csv(out / "county_production.csv.gz", index=False, compression={"method": "gzip", "mtime": 0})      # mtime 0: the same data is the same bytes
    (out / "county_production_meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")


# -- the lattice file ------------------------------------------------------------------------------------
def lattice_points(geo: dict, step: float = LATTICE_STEP) -> pd.DataFrame:
    """[fips, lon, lat]: the centres of a regular `step`-degree lattice that fall inside each county of a county GeoJSON (properties STATE + COUNTY). The lattice is anchored on multiples of
    `step`. A county too small to hold a lattice point (an independent city, a sliver) gets its representative point, so it still counts."""
    import shapely
    from shapely.geometry import shape

    fips_l, lon_l, lat_l = [], [], []
    for feat in geo["features"]:
        p = feat["properties"]
        fips = p["STATE"] + p["COUNTY"]
        try:
            g = shape(feat["geometry"])
        except Exception:                                                     # noqa: BLE001 - one bad polygon must not stop the build
            continue
        if g.is_empty:
            continue
        shapely.prepare(g)
        minx, miny, maxx, maxy = g.bounds
        xs = np.arange(math.floor(minx / step) * step + step / 2, maxx, step)
        ys = np.arange(math.floor(miny / step) * step + step / 2, maxy, step)
        keep = None
        if len(xs) and len(ys):
            gx, gy = np.meshgrid(xs, ys)
            gx, gy = gx.ravel(), gy.ravel()
            keep = shapely.contains_xy(g, gx, gy)
        if keep is not None and keep.any():
            lons, lats = gx[keep], gy[keep]
        else:
            rp = g.representative_point()
            lons, lats = np.array([rp.x]), np.array([rp.y])
        fips_l.extend([fips] * len(lons))
        lon_l.append(lons)
        lat_l.append(lats)
    return pd.DataFrame({"fips": fips_l, "lon": np.round(np.concatenate(lon_l), 3), "lat": np.round(np.concatenate(lat_l), 3)})


def write_lattice(df: pd.DataFrame, out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "county_lattice.csv.gz", index=False, compression={"method": "gzip", "mtime": 0})


# -- command line ------------------------------------------------------------------------------------------
def _years(s: str) -> list:
    a, _, b = s.partition("-")
    return list(range(int(a), int(b or a) + 1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the data files behind the Map tab's production heat layer.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("lattice", help="the county lattice (needs shapely; once)")
    pl.add_argument("--geojson", help="a local county GeoJSON (properties STATE, COUNTY); default: download Plotly's")
    pp = sub.add_parser("production", help="NASS county production, withheld counties filled")
    pp.add_argument("--years", default=f"{time.localtime().tm_year - 5}-{time.localtime().tm_year - 1}", help="e.g. 2021-2025 (default: the last five completed years)")
    pp.add_argument("--crops", default=",".join(CROPS))
    pp.add_argument("--key")
    pp.add_argument("--no-cache", action="store_true", help="ask NASS again even where an answer is cached")
    a = ap.parse_args(argv)
    if a.cmd == "lattice":
        if a.geojson:
            geo = json.loads(Path(a.geojson).read_text(encoding="utf-8"))
        else:
            with urllib.request.urlopen(COUNTY_GEOJSON, timeout=120) as r:
                geo = json.load(r)
        df = lattice_points(geo)
        write_lattice(df)
        print(f"lattice: {df['fips'].nunique()} counties, {len(df)} points -> {OUT / 'county_lattice.csv.gz'}")
        return 0
    key = _nass_key(a.key)
    crops = [c.strip() for c in a.crops.split(",") if c.strip()]
    bad = [c for c in crops if c not in CROPS]
    if bad:
        sys.exit(f"unknown crop(s): {bad}; known: {list(CROPS)}")
    cache = None if a.no_cache else CACHE
    rows, meta = build_production(crops, _years(a.years), lambda c, y: load_year(c, y, key, cache))
    write_production(rows, meta)
    print(f"production: {len(rows)} rows -> {OUT / 'county_production.csv.gz'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
