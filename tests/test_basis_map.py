"""The Basis Maps tab's pictures (basis_map.py): the surface, the colour classes, the panel, the baselines, and the two database readers behind them (get_raw_grains, get_basis_window, on a throwaway SQLite).

    python tests/test_basis_map.py

Kolten 2026-10-27, with a page of AgManager.info maps: "So now I am wanting to recreate something like this". Pure functions and synthetic bids; the shipped outline file (data/maps/) is the real one.
"""
import io
import math
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL"):
    os.environ[k] = ""

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import basis_map as bm  # noqa: E402
import database  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + str(detail)[:500]) if (not ok and str(detail)) else ""))
    if not ok:
        FAILS.append(name)


geo = bm.geometry()
close = lambda a, b, t=1e-6: abs(a - b) < t  # noqa: E731

print("the outlines")
check("the shipped outline file is there and has the lower 48", bm.available() and len(geo["states"]) == 49 and {"IA", "NE", "KS", "IL", "DC"} <= set(geo["states"]), len(geo["states"]))
check("a missing file is not available (and nothing is raised)", bm.available(Path(tempfile.mkdtemp()) / "nope.json.gz") is False)
check("every state has a ring of at least four points, in the lower 48's box", all(len(r) >= 4 and all(-125.5 < x < -66.5 and 24 < y < 49.6 for x, y in r) for rs in geo["states"].values() for r in rs))
check("county lines and state lines are lists of lines of at least two points", all(len(ln) >= 2 for ln in geo["county_lines"][:500]) and all(len(ln) >= 2 for ln in geo["state_lines"][:500]) and len(geo["county_lines"]) > 10000)

print("the view and the mask")
v = bm.view_for_states(["IA"], geo, 640)
check("Iowa fits a 640-pixel-wide view with its margin round it", v.width == 640 and 330 < v.height < 520 and v.west < -96.85 and v.east > -89.85 and v.south < 40.15 and v.north > 43.7, v)
check("a pixel is the same distance both ways (about a kilometre across Iowa), at the cosine of the middle of the view", 0.9 < v.km_per_px < 1.3 and close(v.cos_lat, math.cos(math.radians(41.94)), 0.003), (v.km_per_px, v.cos_lat))
x0, y0 = v.xy(v.west, v.north)
x1, y1 = v.xy(v.east, v.south)
check("the box's corners are the picture's corners", close(float(x0), 0) and close(float(y0), 0) and close(float(x1), v.width) and abs(float(y1) - v.height) < 1.0, (x1, y1))
dx = float(v.xy(v.west + 0.01, v.north)[0] - x0) * v.km_per_px
dy = float(v.xy(v.west, v.north - 0.01)[1] - y0) * v.km_per_px
check("0.01 degree of latitude is 1.11 km on the picture, and of longitude 1.11 x cos(lat)", close(dy, 1.11195, 0.001) and close(dx, 1.11195 * v.cos_lat, 0.001), (dx, dy))
check("states without an outline are no view; known ones still work beside them", bm.view_for_states(["XX"], geo) is None and bm.view_for_states([], geo) is None and bm.view_for_states(["XX", "IA"], geo).width == 640)
m = bm.state_mask(v, ["IA"], geo)
ix, iy = v.xy(-93.5, 42.0)
nx, ny = v.xy(-93.0, 40.15)
check("the mask is Iowa and only Iowa: Des Moines in, northern Missouri (inside the same view) out", m.shape == (v.height, v.width) and m[int(iy), int(ix)] and not m[int(ny), int(nx)], (nx, ny))
area = m.sum() * v.km_per_px ** 2
check("its area is Iowa's (145,746 km2) to within 6%", 0.94 * 145746 < area < 1.06 * 145746, area)
m2 = bm.state_mask(bm.view_for_states(["IA", "MN"], geo, 640), ["IA"], geo)
check("a view of two states masks only the one asked for", 0 < m2.mean() < 0.6)

print("the surface")
vv = bm.view_for_states(["IA"], geo, 320)
mm = bm.state_mask(vv, ["IA"], geo)
rng = np.random.default_rng(1)
cand_lon = rng.uniform(-96.4, -90.3, 400)
cand_lat = rng.uniform(40.5, 43.4, 400)
cx_, cy_ = vv.xy(cand_lon, cand_lat)
inside = mm[np.clip(cy_.astype(int), 0, vv.height - 1), np.clip(cx_.astype(int), 0, vv.width - 1)]
lon, lat = cand_lon[inside][:60], cand_lat[inside][:60]
assert len(lon) == 60
flat = bm.interpolate(vv, mm, lon, lat, np.full(60, 5.0))
check("a constant field stays constant, inside the mask, and is empty outside it", flat.shape == (vv.height, vv.width) and np.nanmax(np.abs(flat[mm & np.isfinite(flat)] - 5.0)) < 1e-3 and np.isnan(flat[~mm]).all())
grad = bm.interpolate(vv, mm, lon, lat, (lon + 96.5) * 10.0)
gx = np.nanmean(grad, axis=0)
cols = np.where(np.isfinite(gx))[0]
check("a field that rises to the east is a surface that rises to the east", np.corrcoef(cols, gx[cols])[0, 1] > 0.97 and gx[cols[-1]] > gx[cols[0]] + 30)
px, py = vv.xy(lon[:5], lat[:5])
at = [grad[int(round(y)), int(round(x))] for x, y in zip(px, py)]
want = (lon[:5] + 96.5) * 10.0
check("right at a bid the surface is that bid (within a few cents)", all(np.isfinite(a) and abs(a - w) < 6 for a, w in zip(at, want)), (at, want))
check("fewer than five bids is not a surface", bm.interpolate(vv, mm, lon[:4], lat[:4], np.ones(4)) is None and bm.interpolate(vv, mm, [], [], []) is None)
check("a missing or infinite value is not a bid", bm.interpolate(vv, mm, np.append(lon[:4], [-93.0, -92.0]), np.append(lat[:4], [41.5, 42.5]), np.array([1, 2, 3, 4, np.nan, np.inf])) is None)
west = np.array([-96.0, -95.8, -95.6, -95.9, -95.7, -95.5])
wl = np.array([41.0, 41.5, 42.0, 42.5, 41.7, 42.2])
sw = bm.interpolate(vv, mm, west, wl, np.arange(6.0) + 10, gap_km=100.0)
ex, ey = vv.xy(-91.0, 41.5)
wx, wy = vv.xy(-95.7, 41.8)
check("no bid within gap_km leaves the cell blank (the east of Iowa from bids in the far west), the ground near the bids is coloured",
      np.isnan(sw[int(ey), int(ex)]) and np.isfinite(sw[int(wy), int(wx)]))
check("the same bids give the same surface", np.array_equal(np.nan_to_num(grad, nan=-9999), np.nan_to_num(bm.interpolate(vv, mm, lon, lat, (lon + 96.5) * 10.0), nan=-9999)))
ring_lon = np.array([-93.01, -92.99, -93.01, -92.99, -92.0, -94.0])
ring_lat = np.array([42.01, 42.01, 41.99, 41.99, 41.0, 43.0])
ring = bm.interpolate(vv, mm, ring_lon, ring_lat, np.array([0.0, 40.0, 40.0, 0.0, 20.0, 20.0]))
sx, sy = vv.xy(-93.0, 42.0)
check("four bids a kilometre round a spot that disagree (0 and 40) are blended into their mean (20) there, not copied from the nearest", abs(ring[int(round(sy)), int(round(sx))] - 20.0) < 3, ring[int(round(sy)), int(round(sx))])

print("classes and colours")
skew = np.arange(1000.0) ** 2
e = bm.class_edges(skew)
check("level classes are deciles of the values (not equal steps): 11 bounds, strictly rising, the median in the middle, from the minimum to the maximum",
      len(e) == 11 and (np.diff(e) > 0).all() and e[0] == 0 and e[-1] == 999 ** 2 and close(e[5], float(np.quantile(skew, 0.5)), 1e-6) and e[5] < 0.4 * e[-1], e)
check("a flat surface still has strictly rising bounds", (np.diff(bm.class_edges(np.full(50, 7.0))) > 0).all())
d = bm.class_edges(np.concatenate([np.full(95, 3.0), np.linspace(-20, 40, 100)]), mode="diverging")
check("diverging classes are symmetric about zero, ten equal steps out to the larger of the 5th / 95th percentiles", len(d) == 11 and close(d[0], -d[-1]) and close(d[5], 0.0) and close(np.diff(d).std(), 0.0, 1e-9) and d[-1] > 20, d)
check("all-zero (or empty) deviations never give a zero-width scale", bm.class_edges(np.zeros(20), mode="diverging")[-1] > 0 and len(bm.class_edges([], mode="diverging")) == 11 and len(bm.class_edges([])) == 11)
pos = bm.position(np.array([-5.0, 0.0, 5.0, 50.0]), np.array([0.0, 1.0, 2.0, 3.0]))
check("a value's place on the ramp is its class position, clipped at the ends", close(pos[0], 0.0) and close(pos[1], 0.0) and close(pos[2], 1.0) and close(pos[3], 1.0))
stops = bm.RAMPS["level"]
check("the ends of the ramps are their first and last stops; strong is blue and weak is red on the basis ramp",
      tuple(bm.ramp_rgb(0.0)) == stops[0][1] and tuple(bm.ramp_rgb(1.0)) == stops[-1][1] and bm.ramp_rgb(0.0)[0] > bm.ramp_rgb(0.0)[2] and bm.ramp_rgb(1.0)[2] > bm.ramp_rgb(1.0)[0])
dv = bm.RAMPS["diverging"]
check("on the deviation ramp above-baseline is purple (blue over green), below is brown / orange (red over blue), and the middle is near-white",
      bm.ramp_rgb(1.0, "diverging")[2] > bm.ramp_rgb(1.0, "diverging")[1] and bm.ramp_rgb(0.0, "diverging")[0] > bm.ramp_rgb(0.0, "diverging")[2] and min(bm.ramp_rgb(0.5, "diverging")) > 220, dv)
surf = np.array([[1.0, np.nan], [2.0, 3.0]])
col = bm.colorize(surf, bm.class_edges(surf), "level")
check("a colourised surface is opaque where it has a value and transparent where it has none", col.shape == (2, 2, 4) and col.dtype == np.uint8 and col[0, 1, 3] == 0 and (col[[0, 1, 1], [0, 0, 1], 3] == 255).all())
check("... the lowest value gets the weakest colour and the highest the strongest", tuple(col[0, 0, :3]) == stops[0][1] and tuple(col[1, 1, :3]) == stops[-1][1], col)

print("words")
check("whole cents, a plus sign only where asked, zero is just 0", bm.fmt_cents(-18.4) == "-18" and bm.fmt_cents(9.6) == "10" and bm.fmt_cents(9.6, True) == "+10" and bm.fmt_cents(-3, True) == "-3" and bm.fmt_cents(0.2, True) == "0")
check("the futures box names the contract month and the price", bm.futures_label("ZCZ26", 463.0) == "CBT Dec Futures Price: $4.63" and bm.futures_label("ZSX26", 1229) == "CBT Nov Futures Price: $12.29")
check("... and says nothing when it cannot", bm.futures_label(None, 463) is None and bm.futures_label("ZCZ26", None) is None and bm.futures_label("", 1) is None)
check("a contract it cannot read is named as it came", bm.futures_label("ZC", 100) == "CBT ZC Futures Price: $1.00")
check("dates read month-day-year", bm.mmddyyyy(date(2026, 10, 7)) == "10-07-2026")

print("the points")


def loc(p, name, lat, lon, bids, state="IA", typ="Country Elevator"):
    return {"provider": p, "location": name, "state": state, "facility_type": typ, "region": "", "lat": lat, "lon": lon, "bids": bids}


def bid(grain, dm, sym, basis):
    return {"grain": grain, "delivery_month": dm, "futures_symbol": sym, "basis": basis}


rows = [
    loc("A", "one", 42.0, -93.0, [bid("Corn", "Nov 2026", "ZCZ26", -30), bid("Corn", "Oct 2026", "ZCZ26", -20), bid("Soybeans", "Oct 2026", "ZSX26", 5)]),
    loc("A", "two", 41.0, -92.0, [bid("Corn", "Dec 2026", "ZCZ26", -10)], typ="Corn Processing"),
    loc("A", "three", 40.8, -91.0, [bid("Corn", "Mar 2027", "ZCH27", -40)]),
    loc("B", "four", 43.0, -94.0, [bid("Corn", "Oct 2026", "ZCZ26", None)]),
    loc("B", "five", None, None, [bid("Corn", "Oct 2026", "ZCZ26", -5)]),
    loc("B", "six", 42.5, -95.0, [bid("Corn", "Oct 2026", "ZCZ26", -25)], state="NE"),
    loc("B", "seven", 41.5, -93.5, []),
]
pts, sym, n_other = bm.nearby_bids(rows, lambda g: g == "Corn")
check("each location's nearest delivery month is its basis (Oct -20, not Nov -30)", pts[("A", "one")]["basis"] == -20 and pts[("A", "one")]["month"] == "Oct 2026")
check("the most common futures contract is the one kept (ZCZ26), the March bid is counted as left out", sym == "ZCZ26" and n_other == 1 and ("A", "three") not in pts and len(pts) == 3, (sym, n_other, list(pts)))
check("no basis, no pin and no bid are no point", ("B", "four") not in pts and ("B", "five") not in pts and ("B", "seven") not in pts)
soy = bm.nearby_bids(rows, lambda g: g == "Soybeans")[0]
check("another commodity's bid is not this one's", set(soy) == {("A", "one")} and soy[("A", "one")]["basis"] == 5 and soy[("A", "one")]["symbol"] == "ZSX26")
check("a type filter keeps only those types", set(bm.nearby_bids(rows, lambda g: g == "Corn", types={"Corn Processing"})[0]) == {("A", "two")})
check("a state filter keeps only those states", set(bm.nearby_bids(rows, lambda g: g == "Corn", states={"NE"})[0]) == {("B", "six")})
p3, s3, o3 = bm.nearby_bids(rows, lambda g: g == "Corn", symbol="ZCH27")
check("a contract can be named: then only its bids are kept", set(p3) == {("A", "three")} and s3 == "ZCH27" and o3 == 3)
check("nothing matches: no points, no contract", bm.nearby_bids(rows, lambda g: g == "Oats") == ({}, None, 0))

print("baselines")
check("a window is the target day +- n days, the end excluded", bm.window_bounds(date(2025, 10, 27), 5) == ("2025-10-22T00:00:00", "2025-11-02T00:00:00"))
check("29 February in another year is 28 February", bm._same_day(date(2024, 2, 29), 2025) == date(2025, 2, 28) and bm._same_day(date(2026, 10, 27), 2025) == date(2025, 10, 27))


def wr(p, l, ts, sp, dm, sym, b):
    return {"provider": p, "location": l, "timestamp": ts, "is_spot": sp, "delivery_month": dm, "futures_symbol": sym, "basis_cents": b}


T = date(2026, 9, 27)
win = [wr("A", "one", "2026-09-26T15:45:00", False, "Oct 2026", "ZCZ26", -30), wr("A", "one", "2026-09-26T15:45:00", False, "Nov 2026", "ZCZ26", -15),
       wr("A", "one", "2026-09-20T15:45:00", False, "Oct 2026", "ZCZ26", -99),
       wr("A", "two", "2026-09-28T15:45:00", False, "Oct 2026", "ZCZ26", -10), wr("A", "two", "2026-09-26T15:45:00", False, "Oct 2026", "ZCZ26", -50),
       wr("A", "three", "2026-09-27T15:45:00", False, "Oct 2026", "ZCH27", -5),
       wr("B", "spot", "2025-10-24T00:00:00", True, "Spot", "ZCZ25", -10), wr("B", "spot", "2025-10-29T00:00:00", True, "Spot", "ZCZ25", -20),
       wr("B", "both", "2026-09-27T00:00:00", True, "Spot", "ZCZ26", -12), wr("B", "both", "2026-09-27T00:00:00", False, "Oct 2026", "ZCZ26", -40),
       wr("B", "none", "2026-09-27T00:00:00", False, "Oct 2026", "ZCZ26", None)]
got = bm.nearby_from_rows(win, T)
check("forward rows: the snapshot nearest the target day, then its earliest delivery month (Sep 26 beats Sep 20; Oct beats Nov)", got[("A", "one")]["basis"] == -30 and got[("A", "one")]["kind"] == "forward")
check("two snapshots equally near: the later one", got[("A", "two")]["basis"] == -10)
check("spot rows (the weekly history files) are the nearby basis, their mean when there are several", got[("B", "spot")] == {"basis": -15.0, "n": 2, "kind": "spot"})
check("spot beats forward when a location has both", got[("B", "both")]["basis"] == -12 and got[("B", "both")]["kind"] == "spot")
check("a row without a basis is not a row", ("B", "none") not in got)
check("with a contract named, forward rows against other contracts are not used, spot rows are", ("A", "three") in got and ("A", "three") not in bm.nearby_from_rows(win, T, symbol="ZCZ26") and bm.nearby_from_rows(win, T, symbol="ZCZ26")[("B", "spot")]["basis"] == -15.0)
bmn = bm.baseline_mean({2023: {("A", "x"): {"basis": -10.0}, ("A", "y"): {"basis": 4.0}}, 2024: {("A", "x"): {"basis": -20.0}}, 2025: {("A", "x"): {"basis": -30.0}}})
check("the baseline is the mean over the years a location has, with how many and which", bmn[("A", "x")] == {"basis": -20.0, "years": 3, "by": {2023: -10.0, 2024: -20.0, 2025: -30.0}} and bmn[("A", "y")]["years"] == 1 and bmn[("A", "y")]["basis"] == 4.0)
cur = {("A", "x"): {"basis": -5.0, "lat": 1}, ("A", "y"): {"basis": 10.0}, ("A", "z"): {"basis": 0.0}}
dv = bm.deviation(cur, bmn, min_years=2)
check("deviation is current minus baseline, for the locations that have both, with the years it rests on (one-year baselines left out when two are asked)",
      set(dv) == {("A", "x")} and dv[("A", "x")]["basis"] == 15.0 and dv[("A", "x")]["baseline"] == -20.0 and dv[("A", "x")]["years"] == 3 and dv[("A", "x")]["lat"] == 1)
check("a plain number is a baseline of one year, and min_years 1 keeps one-year locations", bm.deviation(cur, {("A", "z"): -3.0})[("A", "z")]["basis"] == 3.0 and set(bm.deviation(cur, bmn, min_years=1)) == {("A", "x"), ("A", "y")})

print("a panel and a pair")
vp = bm.view_for_states(["IA"], geo, 400)
mp = bm.state_mask(vp, ["IA"], geo)
grid_lon, grid_lat = np.meshgrid(np.linspace(-96.2, -90.6, 8), np.linspace(40.7, 43.2, 6))
sf = bm.interpolate(vp, mp, grid_lon.ravel(), grid_lat.ravel(), (grid_lon.ravel() + 96.5) * 8 - 60)
img = bm.render_panel(sf, vp, geo, mp, title="Corn Basis, 10-27-2026", subtitle="Basis = Cash Price - Nearby Futures Price", futures="CBT Dec Futures Price: $4.49", markers=[(-93.0, 42.0)], footer="test")
check("a panel is as wide as the legend column, the map and its margin, and as tall as header, map and footer", img.size == (bm.LEFT_W + vp.width + bm.PAD, bm.HEADER_H + vp.height + bm.FOOTER_H), img.size)
arr = np.asarray(img)
mx, my = vp.xy(-93.5, 41.9)
cell = arr[bm.HEADER_H + int(my), bm.LEFT_W + int(mx)]
check("the surface is coloured inside the state and the page is white outside the picture", not (cell > 250).all() and tuple(arr[2, 2]) == (255, 255, 255))
edges = bm.class_edges(sf)
sw_top = arr[bm.HEADER_H + 52 + 26 + 7, bm.PAD + 8 + 10]
check("the legend's top swatch is the strongest class's colour", tuple(int(c) for c in sw_top) == tuple(int(c) for c in bm.ramp_rgb(0.95)), (sw_top, bm.ramp_rgb(0.95)))
mk = arr[bm.HEADER_H + int(vp.xy(-93.0, 42.0)[1]), bm.LEFT_W + int(vp.xy(-93.0, 42.0)[0])]
check("a marker is a dark dot where the location is", int(mk.sum()) < 200, mk)
v2 = bm.view_for_states(["IA", "MN"], geo, 500)
ov = bm.state_mask(v2, ["IA"], geo)
lines_alpha = np.asarray(bm._lines_overlay(v2, geo, ov).getchannel("A"))
mnx, mny = v2.xy(-94.5, 45.3)
iax, iay = v2.xy(-93.5, 41.9)
check("county and state lines are drawn only in the states picked: none in Minnesota, some in Iowa, of a two-state view",
      lines_alpha[int(mny) - 40:int(mny) + 40, int(mnx) - 40:int(mnx) + 40].max() == 0 and lines_alpha[int(iay) - 40:int(iay) + 40, int(iax) - 40:int(iax) + 40].max() > 0)
blank = bm.render_panel(None, vp, geo, mp, title="Nothing", note="No bids here")
check("with nothing to interpolate the panel still draws (with its note) and keeps the same size", blank.size == img.size and np.asarray(blank).std() > 5)
pair = bm.compose([img, img], gap=30)
check("two panels side by side with a gap", pair.size == (img.width * 2 + 30, img.height))
png = bm.to_png(pair)
check("a PNG that decodes to the same picture", png[:8] == b"\x89PNG\r\n\x1a\n" and Image.open(io.BytesIO(png)).size == pair.size)

print("the whole pair, with stand-ins for the database")
IA = [("A%02d" % i, 40.8 + (i // 8) * 0.45, -96.0 + (i % 8) * 0.8) for i in range(48)]
big = [loc("P", n, la, lo, [bid("Corn", "Oct 2026", "ZCZ26", -60 + (lo + 96) * 8)]) for n, la, lo in IA]
big += [loc("P", "plant", 41.6, -93.6, [bid("Corn", "Oct 2026", "ZCZ26", 20)], typ="Corn Processing"), loc("P", "march", 42.0, -92.5, [bid("Corn", "Mar 2027", "ZCH27", -10)])]
calls = []


def fetch(grains, start, end, spot=None, symbol=None):
    calls.append((tuple(grains), start, end, spot, symbol))
    yr = int(start[:4])
    return [wr("P", n, f"{yr}-10-27T00:00:00", True, "Spot", "ZCZ", -70 + (lo + 96) * 8 + (yr - 2023) * 3) for n, la, lo in IA[:12]] + [wr("P", "plant", f"{yr}-10-27T00:00:00", True, "Spot", "ZCZ", -5 + (yr - 2023))]


common = dict(fetch=fetch, raw_grains=lambda c: [c, "Yellow " + c], futures_cents=lambda s: 449.0 if s == "ZCZ26" else None)
r3 = bm.build_pair(big, "Corn", ["IA"], ["Country Elevator"], "3yr", date(2026, 10, 27), width=300, **common)
check("a pair comes back as a PNG with the numbers for its captions", r3["error"] is None and r3["png"][:4] == b"\x89PNG" and r3["symbol"] == "ZCZ26" and r3["image"].width > 600)
check("the basis map counts the elevators (48, not the plant) and the bid against another contract", r3["n_level"] == 48 and r3["n_other"] == 1, (r3["n_level"], r3["n_other"]))
check("a 3-year baseline asks for the same days of each of the last three years, spot and forward rows alike, and names the years it found",
      [c[1][:4] for c in calls] == ["2023", "2024", "2025"] and all(c[3] is None and c[4] is None for c in calls) and calls[0][1] == "2023-10-22T00:00:00" and r3["dev_years"] == [2023, 2024, 2025], calls)
check("its deviation uses every type (history is plants and terminals): the 12 elevators and the plant that have history, not the 48 elevators", r3["n_dev"] == 13, r3["n_dev"])
calls.clear()
r1 = bm.build_pair(big, "Corn", ["IA"], ["Country Elevator"], "1yr", date(2026, 10, 27), width=300, **common)
check("last year is one window, one year", [c[1][:4] for c in calls] == ["2025"] and r1["dev_years"] == [2025] and r1["n_dev"] == 13)
calls.clear()


def fetch_recent(grains, start, end, spot=None, symbol=None):
    calls.append((start, end, spot, symbol))
    return [wr("P", n, "2026-09-26T15:45:00", False, "Oct 2026", "ZCZ26", -75 + (lo + 96) * 8) for n, la, lo in IA]


r30 = bm.build_pair(big, "Corn", ["IA"], ["Country Elevator"], "30d", date(2026, 10, 27), width=300, **{**common, "fetch": fetch_recent})
check("30 days ago is one window round 27 September, forward rows only, against the contract in use", calls == [("2026-09-24T00:00:00", "2026-10-01T00:00:00", False, "ZCZ26")], calls)
check("and follows the type filter: the 48 elevators have a bid then, so 48 locations are compared", r30["n_dev"] == 48 and r30["dev_years"] == [])
r7 = bm.build_pair(big, "Corn", ["IA"], ["Country Elevator"], "7d", date(2026, 10, 27), width=300, **{**common, "fetch": fetch_recent})
check("7 days ago works the same way", r7["n_dev"] == 48 and calls[-1][0] == "2026-10-18T00:00:00", calls[-1])
rn = bm.build_pair(big, "Corn", ["IA"], ["Country Elevator"], "3yr", date(2026, 10, 27), width=300, **{**common, "fetch": lambda *a, **k: []})
check("a baseline with no history gives a panel that says so, not a crash", rn["error"] is None and rn["n_dev"] == 0 and rn["dev_years"] == [] and rn["png"][:4] == b"\x89PNG")
re_ = bm.build_pair(big, "Corn", ["XX"], [], "3yr", date(2026, 10, 27), **common)
check("states without an outline are an error message, not a crash", re_["error"] and re_["png"] is None)
ro = bm.build_pair(big, "Oats", ["IA"], [], "3yr", date(2026, 10, 27), width=300, **common)
check("a commodity nobody bids: two empty panels, no crash", ro["error"] is None and ro["n_level"] == 0 and ro["symbol"] is None and ro["png"][:4] == b"\x89PNG")
try:
    bm.build_pair(big, "Corn", ["IA"], [], "5d", date(2026, 10, 27), width=300, **common)
    bad = False
except ValueError:
    bad = True
check("an unknown baseline is refused", bad)
check("the region presets are all real states", all(s in geo["states"] for ss in bm.REGIONS.values() for s in ss))

print("wheat")
check("tenths of a cent where asked, a plain 0 whenever the rounded value is zero (never '-0.0')", bm.fmt_cents(0.64, True, 1) == "+0.6" and bm.fmt_cents(-0.64, True, 1) == "-0.6" and bm.fmt_cents(-0.04, True, 1) == "0"
      and bm.fmt_cents(2.456, False, 2) == "2.46" and bm.fmt_cents(-18.4, False, 0) == "-18")
wide = np.linspace(-44.0, 44.0, 11)
narrow = np.linspace(-3.0, 3.0, 11)
check("the legend is in whole cents while whole cents are all different (a wide scale), in tenths when they would repeat (a hard red winter month that moved 3 cents), and says zero as 0",
      bm.edge_labels(wide, True) == ["-44", "-35", "-26", "-18", "-9", "0", "+9", "+18", "+26", "+35"]
      and bm.edge_labels(narrow, True) == ["-3.0", "-2.4", "-1.8", "-1.2", "-0.6", "0", "+0.6", "+1.2", "+1.8", "+2.4"], (bm.edge_labels(wide, True), bm.edge_labels(narrow, True)))
check("a level scale of deciles that are whole cents apart keeps whole cents", bm.edge_labels(np.array([-104.0, -89, -83, -70, -51, -45, -37, -31, -27, -20, 5]), False)[:3] == ["-104", "-89", "-83"])
check("the futures box names the market: Chicago for soft red winter, KC for hard red winter, a three-letter root (Minneapolis) as it is",
      bm.futures_label("ZWZ26", 512.0) == "CBT Dec Futures Price: $5.12" and bm.futures_label("KEZ26", 530.0) == "KC Dec Futures Price: $5.30"
      and bm.futures_label("ZWN27", 600.0) == "CBT Jul Futures Price: $6.00" and bm.futures_label("MWEZ26", 600.0) == "MWE Dec Futures Price: $6.00", bm.futures_label("MWEZ26", 600.0))
check("the market of a contract symbol is its root; one that does not end in a month and two digits is returned as it is",
      [bm.futures_root(x) for x in ("ZWZ26", "KEZ26", "MWEZ26", "ZCH27", "ZC", "", None, "@MW")] == ["ZW", "KE", "MWE", "ZC", "ZC", "", "", "@MW"])
check("the registry has corn, soybeans and the two winter wheats; every one opens on a real region", list(bm.COMMODITIES) == ["Corn", "Soybeans", "Soft Red Winter Wheat", "Hard Red Winter Wheat"]
      and all(spec["region"] in bm.REGIONS for spec in bm.COMMODITIES.values()))
check("wheat's regions are the soft red winter states and the hard red winter states, real states all", all(s_ in geo["states"] for k in ("Soft red winter states", "Hard red winter states") for s_ in bm.REGIONS[k])
      and bm.default_region("Soft Red Winter Wheat") == "Soft red winter states" and bm.default_region("Hard Red Winter Wheat") == "Hard red winter states" and bm.default_region("Corn") == "Corn Belt")
check("a name the registry does not know is a grain of that name (with history, on the Corn Belt)", bm.commodity_spec("Oats")["grain"]("Oats") and not bm.commodity_spec("Oats")["grain"]("Corn")
      and bm.commodity_spec("Oats")["history"] and bm.default_region("Oats") == "Corn Belt")
check("corn and soybeans offer all four comparisons, wheat only 30 and 7 days (the tracker holds no wheat before June 2026)",
      bm.baselines_for("Corn") == list(bm.BASELINES) and bm.baselines_for("Soybeans") == list(bm.BASELINES) and bm.baselines_for("Hard Red Winter Wheat") == ["30d", "7d"] and bm.baselines_for("Soft Red Winter Wheat") == ["30d", "7d"])
check("wheat is the grain map's plain 'Wheat': a hard white winter or durum row is not soft or hard red winter", all(bm.COMMODITIES[c]["grain"]("Wheat") and not bm.COMMODITIES[c]["grain"]("Wheat (HWW)")
      and not bm.COMMODITIES[c]["grain"]("Wheat (Durum)") and not bm.COMMODITIES[c]["grain"]("Corn") for c in ("Soft Red Winter Wheat", "Hard Red Winter Wheat")))
wrows = [
    loc("W", "both", 39.0, -90.0, [bid("Wheat", "Oct 2026", "ZWZ26", -55), bid("Wheat", "Nov 2026", "KEZ26", -70)], state="IL"),
    loc("W", "kc", 38.5, -98.0, [bid("Wheat", "Oct 2026", "KEZ26", -60)], state="KS"),
    loc("W", "kclate", 38.0, -97.0, [bid("Wheat", "Oct 2026", "ZWZ26", -40), bid("Wheat", "Nov 2026", "KEZ26", -80)], state="KS"),
    loc("W", "deferred", 40.0, -89.0, [bid("Wheat", "Jul 2027", "ZWN27", -30)], state="IL"),
    loc("W", "white", 39.5, -91.0, [bid("Wheat (HWW)", "Oct 2026", "ZWZ26", -10)], state="IL"),
]
srw = lambda **k: bm.nearby_bids(wrows, bm.COMMODITIES["Soft Red Winter Wheat"]["grain"], bid_ok=lambda b: bm.futures_root(b["futures_symbol"]) == "ZW", **k)
hrw = lambda **k: bm.nearby_bids(wrows, bm.COMMODITIES["Hard Red Winter Wheat"]["grain"], bid_ok=lambda b: bm.futures_root(b["futures_symbol"]) == "KE", **k)
ps, ss, os_ = srw()
ph, sh, oh = hrw()
check("soft red winter is the bids quoted against Chicago: a location that bids both is in it with its Chicago bid (-55), against the Dec contract; the deferred July bid is left out and counted",
      set(ps) == {("W", "both"), ("W", "kclate")} and ss == "ZWZ26" and ps[("W", "both")]["basis"] == -55 and os_ == 1 and ("W", "kc") not in ps, (list(ps), ss, os_))
check("hard red winter is the bids quoted against Kansas City: the same location's KC bid (-70) is its nearest KC bid even though its Chicago bid is earlier, and a location whose only nearby is Chicago's is not in it",
      set(ph) == {("W", "both"), ("W", "kc"), ("W", "kclate")} and sh == "KEZ26" and ph[("W", "both")]["basis"] == -70 and ph[("W", "kclate")]["basis"] == -80 and oh == 0, (list(ph), sh, ph.get(("W", "both"))))
check("the filter works before the nearest month is picked: 'kclate' is soft red winter -40 (Oct) and hard red winter -80 (Nov)", ps[("W", "kclate")]["basis"] == -40 and ph[("W", "kclate")]["basis"] == -80)

W_IL = [("S%02d" % i, 38.0 + (i // 4) * 0.8, -91.0 + (i % 4) * 0.9) for i in range(12)]
W_KS = [("K%02d" % i, 37.2 + (i // 4) * 0.6, -100.0 + (i % 4) * 0.9) for i in range(12)]
wbig = ([loc("W", n, la, lo, [bid("Wheat", "Oct 2026", "ZWZ26", -60 + i)], state="IL") for i, (n, la, lo) in enumerate(W_IL)]
        + [loc("W", f"late{i}", 39.0 + i * 0.3, -90.0, [bid("Wheat", "Jul 2027", "ZWN27", -35)], state="IL") for i in range(4)]
        + [loc("W", n, la, lo, [bid("Wheat", "Oct 2026", "KEZ26", -70 + i)], state="KS") for i, (n, la, lo) in enumerate(W_KS)]
        + [loc("W", "ilkc", 39.0, -90.3, [bid("Wheat", "Oct 2026", "KEZ26", -90)], state="IL")])
wcalls, wgrains, wfut = [], [], []


def wfetch(grains, start, end, spot=None, symbol=None):
    wcalls.append((start, end, spot, symbol))
    nm = W_IL if symbol == "ZWZ26" else W_KS
    return [wr("W", n, "2026-09-26T15:45:00", False, "Oct 2026", symbol, -70 + i) for i, (n, la, lo) in enumerate(nm)]


wcommon = dict(fetch=wfetch, raw_grains=lambda c: (wgrains.append(c) or [c]), futures_cents=lambda sy: (wfut.append(sy) or 512.0))
rs = bm.build_pair(wbig, "Soft Red Winter Wheat", ["IL"], ["Country Elevator"], "30d", date(2026, 10, 27), width=300, **wcommon)
check("a soft red winter pair: the 12 Oct elevators against ZWZ26 (the Chicago bid of the KC-only location is not one), the 4 deferred-only ones counted as another contract",
      rs["error"] is None and rs["symbol"] == "ZWZ26" and rs["n_level"] == 12 and rs["n_other"] == 4 and rs["png"][:4] == b"\x89PNG", {k: v for k, v in rs.items() if k not in ("image", "png")})
check("... its 30-day baseline reads the Chicago contract's forward rows only, and the history readers are asked for the grain map's 'Wheat'; the futures box asks for the Chicago price",
      wcalls == [("2026-09-24T00:00:00", "2026-10-01T00:00:00", False, "ZWZ26")] and wgrains == ["Wheat"] and wfut == ["ZWZ26"], (wcalls, wgrains, wfut))
check("... and the 12 locations that had a bid then and now are compared", rs["n_dev"] == 12 and rs["dev_years"] == [])
wcalls.clear()
rh = bm.build_pair(wbig, "Hard Red Winter Wheat", ["KS"], ["Country Elevator"], "7d", date(2026, 10, 27), width=300, **wcommon)
check("a hard red winter pair works the same way against KEZ26, seven days back", rh["error"] is None and rh["symbol"] == "KEZ26" and rh["n_level"] == 12 and rh["n_dev"] == 12 and wcalls == [("2026-10-18T00:00:00", "2026-10-23T00:00:00", False, "KEZ26")], (rh["symbol"], rh["n_level"], wcalls))
wcalls.clear()
rm = bm.build_pair(wbig, "Soft Red Winter Wheat", ["IL"], ["Country Elevator"], "3yr", date(2026, 10, 27), width=300, **wcommon)
check("a multi-year baseline for wheat is not attempted (no history to read): no database call, no deviation, the panel says so, nothing crashes",
      rm["error"] is None and wcalls == [] and rm["n_dev"] == 0 and rm["dev_years"] == [] and rm["png"][:4] == b"\x89PNG" and rm["n_level"] == 12)
rall = bm.build_pair(wbig, "Soft Red Winter Wheat", ["IL", "KS"], [], "30d", date(2026, 10, 27), width=300, **wcommon)
check("in a region with both wheats each class keeps to its own market: of Illinois and Kansas together the soft red winter map holds the 12 Illinois elevators that quote Chicago, not the 12 Kansas elevators or the Illinois one that quotes Kansas City",
      rall["n_level"] == 12 and rall["symbol"] == "ZWZ26", rall["n_level"])

# a location that posts a NEARER Chicago month and a LATER Kansas City month: the market is chosen before the nearest month in the comparison's bids too
W_BOTH = [("B%02d" % i, 37.4 + (i // 4) * 0.6, -99.5 + (i % 4) * 0.9) for i in range(8)]
wboth = [loc("W", n, la, lo, [bid("Wheat", "Oct 2026", "ZWZ26", -50), bid("Wheat", "Nov 2026", "KEZ26", -75 + i)], state="KS") for i, (n, la, lo) in enumerate(W_BOTH)]
wbcalls = []


def wfetch_both(grains, start, end, spot=None, symbol=None):
    wbcalls.append((start[:4], spot, symbol))
    yr = int(start[:4])
    if spot is False:                                                                          # the 30 / 7-day windows: the forward rows of the contract asked for
        return [wr("W", n, "2026-10-20T15:45:00", False, "Nov 2026", symbol, -80 + i) for i, (n, la, lo) in enumerate(W_BOTH)]
    return [wr("W", n, f"{yr}-10-27T00:00:00", True, "Spot", "KE", -80 + i + (yr - 2023)) for i, (n, la, lo) in enumerate(W_BOTH)]   # the weekly history files: spot rows


wbcommon = dict(fetch=wfetch_both, raw_grains=lambda c: [c], futures_cents=lambda sy: 530.0)
rb = bm.build_pair(wboth, "Hard Red Winter Wheat", ["KS"], ["Country Elevator"], "7d", date(2026, 10, 27), width=300, **wbcommon)
check("a location whose Chicago month is nearer than its Kansas City month is in the hard red winter change map with its Kansas City bid (+5 on last week's -80 + i), not dropped: the market comes before the nearest month in the comparison too",
      rb["symbol"] == "KEZ26" and rb["n_level"] == 8 and rb["n_dev"] == 8, {k: v for k, v in rb.items() if k not in ("image", "png")})
hist = bm.COMMODITIES["Hard Red Winter Wheat"]
hist["history"] = True                                                                         # the day the tracker holds three years of wheat
wbcalls.clear()
try:
    offered = bm.baselines_for("Hard Red Winter Wheat")
    ry = bm.build_pair(wboth, "Hard Red Winter Wheat", ["KS"], [], "3yr", date(2026, 10, 27), width=300, **wbcommon)
finally:
    hist["history"] = False
check("... and the day wheat has history it is compared like corn: all four baselines on offer, the three years asked for, the same 8 locations' Kansas City bids compared (not dropped for their nearer Chicago month)",
      offered == list(bm.BASELINES) and [c[0] for c in wbcalls] == ["2023", "2024", "2025"] and ry["dev_years"] == [2023, 2024, 2025] and ry["n_dev"] == 8, ({k: v for k, v in ry.items() if k not in ("image", "png")}, wbcalls))
check("... and once the flag is back to False the multi-year baseline is not attempted again", bm.baselines_for("Hard Red Winter Wheat") == ["30d", "7d"])

print("the two database readers")
database.DB_PATH = Path(tempfile.mkdtemp(prefix="bm_")) / "t.db"
assert database._backend() == "sqlite", "refusing to run on a non-sqlite backend"
database.init_db()
con = sqlite3.connect(database.DB_PATH)
con.execute("DELETE FROM grain_map")                                                       # init_db seeds the map: start from ours
con.executemany("INSERT INTO grain_map (raw_grain, canonical_grain, is_active) VALUES (?, ?, ?)", [("Corn", "Corn", 1), ("Yellow Corn", "Corn", 1), ("Old Corn", "Corn", 0), ("Soybeans", "Soybeans", 1)])


def add(provider, location, ts, rws):
    cur_ = con.execute("INSERT INTO snapshots (timestamp, provider, location, source) VALUES (?, ?, ?, 't')", (ts, provider, location))
    for i, (g, dm, sy, b, sp) in enumerate(rws):
        con.execute("INSERT INTO snapshot_rows (snapshot_id, row_id, grain, delivery_month, futures_symbol, basis_cents, is_spot) VALUES (?, ?, ?, ?, ?, ?, ?)", (cur_.lastrowid, f"r{i}", g, dm, sy, b, sp))


add("A", "one", "2025-10-24T10:00:00", [("Corn", "Spot", "ZCZ25", -20, 1), ("Soybeans", "Spot", "ZSX25", 5, 1)])
add("A", "one", "2025-11-02T00:00:00", [("Corn", "Spot", "ZCZ25", -99, 1)])                          # the window's end is not in it
add("A", "one", "2025-10-22T00:00:00", [("Corn", "Spot", "ZCZ25", -21, 1)])                          # its start is
add("A", "two", "2025-10-30T12:00:00", [("Yellow Corn", "Oct 2025", "ZCZ25", -15, 0), ("Old Corn", "Oct 2025", "ZCZ25", -1, 0), ("Corn", "Mar 2026", "ZCH26", -40, 0)])
con.commit()
con.close()
check("the grain map's raw names for a canonical grain (inactive ones left out)", set(database.get_raw_grains("Corn")) == {"Corn", "Yellow Corn"} and database.get_raw_grains("Soybeans") == ["Soybeans"])
check("a grain the map does not know comes back as itself", database.get_raw_grains("Rye") == ["Rye"])
w = database.get_basis_window(["Corn", "Yellow Corn"], "2025-10-22T00:00:00", "2025-11-02T00:00:00")
check("the window is start included, end excluded, for the grains asked", sorted((r["location"], r["basis_cents"]) for r in w) == [("one", -21), ("one", -20), ("two", -40), ("two", -15)], w)
check("each row carries what the reducers read", {"provider", "location", "timestamp", "is_spot", "delivery_month", "futures_symbol", "basis_cents"} <= set(w[0]) and isinstance(w[0]["is_spot"], bool) and isinstance(w[0]["timestamp"], str))
check("spot rows only, forward rows only", {r["is_spot"] for r in database.get_basis_window(["Corn"], "2025-10-22T00:00:00", "2025-11-02T00:00:00", spot=True)} == {True}
      and {r["is_spot"] for r in database.get_basis_window(["Corn", "Yellow Corn"], "2025-10-22T00:00:00", "2025-11-02T00:00:00", spot=False)} == {False})
check("one futures contract", [r["basis_cents"] for r in database.get_basis_window(["Corn", "Yellow Corn"], "2025-10-22T00:00:00", "2025-11-02T00:00:00", symbol="ZCH26")] == [-40])
check("no grains, or a window with nothing in it: no rows", database.get_basis_window([], "2025-10-22T00:00:00", "2025-11-02T00:00:00") == [] and database.get_basis_window(["Corn"], "2020-01-01T00:00:00", "2020-02-01T00:00:00") == [])
got2 = bm.nearby_from_rows(w, date(2025, 10, 27))
check("and they feed the reducer end to end: A/one's spot mean (-20.5), A/two's earliest forward row (-15)", got2[("A", "one")]["basis"] == -20.5 and got2[("A", "two")]["basis"] == -15.0, got2)

print()
print("FAILED: %s" % ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
