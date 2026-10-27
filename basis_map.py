"""
basis_map.py - the Basis Maps tab's pictures: AgManager-style maps of the basis (a smooth surface between the bids, ten colour classes, county lines, a legend box and the futures price) and of
how far it is from a baseline.

Kolten 2026-10-27, with a page of AgManager.info maps (Nebraska / Kansas soybean and corn basis, cash price minus nearby futures, and the basis deviation from the 3-year average): "So now I am
wanting to recreate something like this".

Pure: numbers in, a PIL picture / PNG bytes out. No Streamlit, no database; the bids come in as plain dicts (database.get_map_data() rows) and the outlines from data/maps/us_geometry.json.gz
(heat_build.py geometry), so the app needs no shapely.

  nearby_bids / deviation   the points: each location's nearest-delivery basis, kept to ONE futures contract so every number is a basis against the same futures month
  View / interpolate        the surface: a Gaussian-weighted local mean of the nearest bids (the bandwidth follows how far apart they are), clipped to the states picked and left blank beyond
                            gap_km of any bid; computed on a coarse grid and smoothed up to the picture's pixels
  render_panel / compose    the picture: a title, the legend box (ten classes) and the futures-price box on the left, the surface with county and state lines, optional markers where the bids are
Two colour modes. 'level' (the basis itself): classes are deciles of the SURFACE, as in the AgManager legends, so each colour covers a tenth of the mapped area. 'diverging' (a deviation): a
symmetric scale around zero, because 'stronger than the average' must not be coloured as if it were weaker.
"""
from __future__ import annotations

import gzip
import json
import math
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import delivery_period as _dp

HERE = Path(__file__).parent
GEOMETRY_FILE = HERE / "data" / "maps" / "us_geometry.json.gz"
LOGO_FILE = HERE / "assets" / "50 Year logo JSA.png"
KM_PER_DEG = 111.195

# position 0 = weakest / lowest ... 1 = strongest / highest; blue = strong and red = weak as in the AgManager basis maps, purple = better than the average and brown / orange = worse as in theirs
RAMPS = {
    "level": ((0.00, (139, 0, 20)), (0.15, (215, 48, 39)), (0.30, (244, 140, 130)), (0.45, (240, 200, 215)), (0.55, (205, 200, 240)), (0.70, (130, 130, 235)), (0.85, (40, 60, 210)), (1.00, (10, 20, 110))),
    "diverging": ((0.00, (140, 70, 10)), (0.20, (200, 110, 30)), (0.38, (245, 175, 90)), (0.50, (244, 238, 230)), (0.62, (200, 185, 235)), (0.80, (130, 80, 200)), (1.00, (70, 10, 140))),
}
# interpolation presets: 'fine' for a dense network of bids, 'regional' for a few dozen points (a deviation from a long baseline) where the picture can only be a broad one
PRESETS = {
    "fine": {"k": 5, "kmax": 24, "hmin_km": 15.0, "hmax_km": 70.0, "gap_km": 100.0},
    "regional": {"k": 3, "kmax": 12, "hmin_km": 60.0, "hmax_km": 250.0, "gap_km": 220.0},
}
MIN_POINTS = 5                                          # fewer bids than this is not a surface
MONTH_OF = {"F": "Jan", "G": "Feb", "H": "Mar", "J": "Apr", "K": "May", "M": "Jun", "N": "Jul", "Q": "Aug", "U": "Sep", "V": "Oct", "X": "Nov", "Z": "Dec"}


# -- outlines --------------------------------------------------------------------------------------
@lru_cache(maxsize=2)
def geometry(path: str = str(GEOMETRY_FILE)) -> dict:
    """{states: {abbr: [ring]}, county_lines: [line], state_lines: [line]} from the committed outline file."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def available(path=None) -> bool:
    """True when the outline file is there and readable, so the tab can hide itself in a copy of the app without it."""
    try:
        return bool(geometry(str(path or GEOMETRY_FILE)).get("states"))
    except Exception:                                      # noqa: BLE001
        return False


# -- the view: states -> pixels ----------------------------------------------------------------------
@dataclass(frozen=True)
class View:
    """A picture of a lon / lat box: x = (lon - west) * cos(lat) * s, y = (north - lat) * s with s pixels per degree of latitude, so a pixel is the same distance both ways."""
    west: float
    south: float
    east: float
    north: float
    width: int
    height: int
    cos_lat: float

    @property
    def scale(self) -> float:
        return self.width / ((self.east - self.west) * self.cos_lat)

    @property
    def km_per_px(self) -> float:
        return KM_PER_DEG / self.scale

    def xy(self, lon, lat):
        s = self.scale
        return (np.asarray(lon, float) - self.west) * self.cos_lat * s, (self.north - np.asarray(lat, float)) * s


def view_for_states(states, geo: dict, width: int = 640, margin: float = 0.3) -> View | None:
    """The view that fits the states (a little margin round them), `width` pixels wide. None when none of them has an outline."""
    pts = [p for s in states for ring in geo["states"].get(s, []) for p in ring]
    if not pts:
        return None
    a = np.array(pts, float)
    west, south = a.min(axis=0) - margin
    east, north = a.max(axis=0) + margin
    cos_lat = math.cos(math.radians((south + north) / 2))
    v = View(float(west), float(south), float(east), float(north), int(width), 1, cos_lat)
    return View(v.west, v.south, v.east, v.north, v.width, max(1, int(round((v.north - v.south) * v.scale))), cos_lat)


def state_mask(view: View, states, geo: dict) -> np.ndarray:
    """(height, width) bool: True inside the states' outlines."""
    img = Image.new("L", (view.width, view.height), 0)
    d = ImageDraw.Draw(img)
    for s in states:
        for ring in geo["states"].get(s, []):
            xs, ys = view.xy([p[0] for p in ring], [p[1] for p in ring])
            d.polygon(list(zip(xs.tolist(), ys.tolist())), fill=255)
    return np.asarray(img) > 0


# -- the surface ---------------------------------------------------------------------------------------
def _fill_nan(a: np.ndarray) -> np.ndarray:
    """NaNs replaced by the mean of their valid neighbours, again and again until the grid is full: only so the smooth up-scaling has nothing to trip on."""
    a = a.copy()
    for _ in range(60):
        nan = np.isnan(a)
        if not nan.any() or nan.all():
            break
        p = np.pad(a, 1, constant_values=np.nan)
        h, w = a.shape
        stack = np.stack([p[i:i + h, j:j + w] for i in range(3) for j in range(3)])
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)                       # a neighbourhood with nothing valid in it is a NaN, not news
            m = np.nanmean(stack, axis=0)
        a = np.where(nan & np.isfinite(m), m, a)
    if np.isnan(a).any():
        a[np.isnan(a)] = 0.0
    return a


def _upscale(a: np.ndarray, width: int, height: int) -> np.ndarray:
    return np.asarray(Image.fromarray(a.astype(np.float32)).resize((width, height), Image.BILINEAR), dtype=np.float32)


def interpolate(view: View, mask: np.ndarray, lon, lat, value, *, cell: int = 4, k: int = 5, kmax: int = 24, hmin_km: float = 15.0, hmax_km: float = 70.0, gap_km: float = 100.0):
    """(height, width) float surface of the values, NaN outside the mask and further than gap_km from any point; None with fewer than MIN_POINTS points. Each cell is the Gaussian-weighted mean of its
    kmax nearest points, the bandwidth being the distance to its k-th nearest (kept between hmin_km and hmax_km): fine detail where the bids are dense, a broader average where they are far apart."""
    lon, lat, value = (np.asarray(x, float) for x in (lon, lat, value))
    ok = np.isfinite(lon) & np.isfinite(lat) & np.isfinite(value)
    if ok.sum() < MIN_POINTS:
        return None
    px, py = view.xy(lon[ok], lat[ok])
    v = value[ok]
    n = len(v)
    km = view.km_per_px
    H, W = view.height, view.width
    Hc, Wc = -(-H // cell), -(-W // cell)                                         # whole cells that cover the picture
    ys = (np.arange(Hc) + 0.5) * cell
    xs = (np.arange(Wc) + 0.5) * cell
    pad = np.zeros((Hc * cell, Wc * cell), bool)
    pad[:H, :W] = mask
    near_mask = pad.reshape(Hc, cell, Wc, cell).any(axis=(1, 3))                  # a coarse cell is worked out wherever any of its pixels is inside the states
    gx, gy = np.meshgrid(xs, ys)
    sel = near_mask.ravel()
    cx, cy = gx.ravel()[sel], gy.ravel()[sel]
    est = np.full(cx.shape, np.nan)
    kk, kn = min(k, n), min(kmax, n)
    for a in range(0, len(cx), 3000):
        d = np.hypot((cx[a:a + 3000, None] - px[None, :]) * km, (cy[a:a + 3000, None] - py[None, :]) * km)
        idx = np.argpartition(d, kn - 1, axis=1)[:, :kn] if kn < n else np.tile(np.arange(n), (d.shape[0], 1))
        dk = np.take_along_axis(d, idx, 1)
        vk = v[idx]
        order = np.argsort(dk, axis=1)
        dk, vk = np.take_along_axis(dk, order, 1), np.take_along_axis(vk, order, 1)
        h = np.clip(dk[:, kk - 1], hmin_km, hmax_km)[:, None]
        w = np.exp(-0.5 * (dk / h) ** 2)
        sw = w.sum(axis=1)
        m = np.where(sw > 1e-12, (w * vk).sum(axis=1) / np.maximum(sw, 1e-12), vk[:, 0])
        est[a:a + 3000] = np.where(dk[:, 0] <= gap_km, m, np.nan)
    coarse = np.full(Hc * Wc, np.nan)
    coarse[sel] = est
    coarse = coarse.reshape(Hc, Wc)
    valid = np.isfinite(coarse)
    if not valid.any():
        return None
    big = _upscale(_fill_nan(coarse), W, H)
    vbig = _upscale(valid.astype(np.float32), W, H)
    return np.where(mask & (vbig > 0.5), big, np.nan)


# -- classes and colours ------------------------------------------------------------------------------------
def class_edges(values, n: int = 10, mode: str = "level") -> np.ndarray:
    """The n + 1 class bounds. level: the deciles of the values, so each class is a tenth of the mapped area. diverging: n equal steps from -m to +m, m the larger of the 5th and 95th percentiles in
    absolute value, so zero is always the middle of the scale (m is never 0)."""
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return np.linspace(-1.0, 1.0, n + 1)
    if mode == "diverging":
        m = max(abs(float(np.percentile(v, 5))), abs(float(np.percentile(v, 95))), 1e-6)
        return np.linspace(-m, m, n + 1)
    e = np.quantile(v, np.linspace(0, 1, n + 1))
    for i in range(1, len(e)):
        if e[i] <= e[i - 1]:
            e[i] = np.nextafter(e[i - 1], np.inf)                                     # a flat surface: still strictly rising
    return e


def position(values, edges: np.ndarray) -> np.ndarray:
    """Each value's place on the colour ramp, 0 to 1: its class bounds spread evenly (so a decile class is a tenth of the ramp), clipped at the ends."""
    return np.interp(np.asarray(values, float), edges, np.linspace(0.0, 1.0, len(edges)))


def ramp_rgb(pos, mode: str = "level") -> np.ndarray:
    """RGB (uint8, shape (..., 3)) for positions 0-1 on a ramp."""
    stops = RAMPS[mode]
    xs = [s[0] for s in stops]
    p = np.clip(np.asarray(pos, float), 0.0, 1.0)
    return np.stack([np.interp(p, xs, [s[1][c] for s in stops]) for c in range(3)], axis=-1).round().astype(np.uint8)


def colorize(surface: np.ndarray, edges: np.ndarray, mode: str = "level") -> np.ndarray:
    """(height, width, 4) uint8: the ramp colour where the surface has a value, transparent where it has none."""
    out = np.zeros(surface.shape + (4,), np.uint8)
    ok = np.isfinite(surface)
    out[..., :3][ok] = ramp_rgb(position(surface[ok], edges), mode)
    out[..., 3][ok] = 255
    return out


# -- text ------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=24)
def _font(size: int):
    return ImageFont.load_default(size=size)


def _text(draw, xy, s: str, size: int, fill=(25, 25, 25), bold: bool = False, anchor: str = "la"):
    """Text in the one font Pillow carries; bold is a thicker stroke on a big title and a second pass a pixel to the right on small text."""
    if bold and size >= 18:
        draw.text(xy, s, font=_font(size), fill=fill, anchor=anchor, stroke_width=1, stroke_fill=fill)
        return
    draw.text(xy, s, font=_font(size), fill=fill, anchor=anchor)
    if bold:
        draw.text((xy[0] + 1, xy[1]), s, font=_font(size), fill=fill, anchor=anchor)


def fmt_cents(v: float, signed: bool = False, decimals: int = 0) -> str:
    """-18 -> '-18', 9 -> '9' (or '+9' signed, 0 always '0'): whole cents, or that many decimals (a zero after rounding is a plain '0')."""
    n = round(float(v), decimals)
    if n == 0:
        return "0"
    if decimals == 0:
        return f"{int(n):+d}" if signed else f"{int(n):d}"
    return f"{n:+.{decimals}f}" if signed else f"{n:.{decimals}f}"


def edge_labels(edges, signed: bool = False) -> list:
    """The legend's labels, one per class (its lower bound): whole cents, or tenths (then hundredths) of a cent when whole cents would show the same number twice, as they do on a hard red winter month
    that moved by 3 cents (+3 +2 +1 +1 0 -1 -1 -2 -3 -3)."""
    for dec in (0, 1, 2):
        labels = [fmt_cents(e, signed, dec) for e in list(edges)[:-1]]
        if len(set(labels)) == len(labels):
            break
    return labels


EXCHANGE = {"ZC": "CBT", "ZS": "CBT", "ZW": "CBT", "KE": "KC"}              # the market's name in the box: ZW is Chicago soft red winter, KE Kansas City hard red winter


def futures_root(symbol: str | None) -> str:
    """'ZWZ26' -> 'ZW', 'KEZ26' -> 'KE', 'MWEZ26' -> 'MWE': the futures market of a contract symbol (the symbol itself when it does not end in a month letter and two digits)."""
    sy = symbol or ""
    return sy[:-3] if len(sy) >= 5 and sy[-2:].isdigit() and sy[-3:-2] in MONTH_OF else sy


def futures_label(symbol: str | None, cents: float | None) -> str | None:
    """'ZCZ26', 463 -> 'CBT Dec Futures Price: $4.63' (the contract month, as the AgManager boxes name it; 'KC Dec' for hard red winter wheat's KEZ26); None when either is missing. A symbol it cannot
    read is named as it came."""
    if not symbol or cents is None:
        return None
    mon = MONTH_OF.get(symbol[-3:-2]) if len(symbol) >= 5 and symbol[-2:].isdigit() else None
    root = symbol[:-3] if mon else ""
    return f"{EXCHANGE.get(root, root or 'CBT')} {mon or symbol} Futures Price: ${cents / 100:.2f}"


# -- the points ------------------------------------------------------------------------------------------------------
def nearby_bids(rows, grain_ok, *, types=None, states=None, symbol: str | None = None, bid_ok=None) -> dict:
    """Each location's nearest-delivery basis for a commodity, from database.get_map_data() rows: {(provider, location): {lat, lon, state, type, basis, symbol, month}}, plus the contract they are quoted
    against and what was left out. `grain_ok(grain) -> bool` picks the commodity's grains. Only bids quoted off ONE futures contract are kept, the most common one unless `symbol` names it, so every basis is
    against the same futures month and the surface is not mixing December and March bases. `bid_ok(bid) -> bool`, when given, narrows the bids a location may offer BEFORE its nearest delivery is picked
    (wheat's class is the futures market the bid is quoted against, so a location that bids both has its nearest bid of the class asked for). Returns (points, symbol, n_other_contract)."""
    best = {}
    for r in rows:
        if types and (r.get("facility_type") or "") not in types:
            continue
        if states and (r.get("state") or "") not in states:
            continue
        if r.get("lat") is None or r.get("lon") is None:
            continue
        bids = [b for b in r["bids"] if grain_ok(b["grain"]) and b.get("basis") is not None and (bid_ok is None or bid_ok(b))]
        if not bids:
            continue
        b = min(bids, key=lambda x: _dp.deliv_key(x["delivery_month"], x["futures_symbol"]))
        best[(r["provider"], r["location"])] = {"lat": float(r["lat"]), "lon": float(r["lon"]), "state": r.get("state") or "", "type": r.get("facility_type") or "",
                                                "basis": float(b["basis"]), "symbol": b.get("futures_symbol") or "", "month": b.get("delivery_month") or ""}
    if not best:
        return {}, symbol, 0
    sym = symbol or Counter(p["symbol"] for p in best.values()).most_common(1)[0][0]
    kept = {k: p for k, p in best.items() if p["symbol"] == sym}
    return kept, sym, len(best) - len(kept)


def window_bounds(target: date, days: int) -> tuple:
    """ISO timestamps bracketing target +- days: (start included, end excluded), the form database.get_basis_window takes."""
    return (target - timedelta(days=days)).isoformat() + "T00:00:00", (target + timedelta(days=days + 1)).isoformat() + "T00:00:00"


def nearby_from_rows(rows, target: date, *, symbol: str | None = None) -> dict:
    """Each location's nearby basis from the rows of a window (database.get_basis_window): {(provider, location): {basis, n, kind}}. Spot rows (the weekly history files) count as the nearby basis, their mean
    if there are several; with none, the forward rows of the snapshot nearest `target` (the later one on a tie) give it as the earliest delivery month quoted - against `symbol` only, when one is named, so a
    change since last month is a change against the same futures contract. A row without a basis is not a row."""
    by_key: dict = {}
    for r in rows:
        if r.get("basis_cents") is None:
            continue
        if symbol and not r["is_spot"] and r.get("futures_symbol") != symbol:
            continue
        by_key.setdefault((r["provider"], r["location"]), []).append(r)
    out = {}
    for k, rs in by_key.items():
        spot = [float(r["basis_cents"]) for r in rs if r["is_spot"]]
        if spot:
            out[k] = {"basis": sum(spot) / len(spot), "n": len(spot), "kind": "spot"}
            continue
        day = lambda r: date.fromisoformat(str(r["timestamp"])[:10])                   # noqa: E731
        nearest = min({day(r) for r in rs}, key=lambda d: (abs((d - target).days), -d.toordinal()))
        r0 = min((r for r in rs if day(r) == nearest), key=lambda r: _dp.deliv_key(r["delivery_month"], r["futures_symbol"]))
        out[k] = {"basis": float(r0["basis_cents"]), "n": 1, "kind": "forward"}
    return out


def baseline_mean(per_period: dict) -> dict:
    """{key: {basis: the mean over the periods the location has, years: how many, by: {period: basis}}} from {period label: nearby_from_rows(...)}, e.g. {2023: ..., 2024: ..., 2025: ...}."""
    acc: dict = {}
    for label, d in per_period.items():
        for k, v in d.items():
            acc.setdefault(k, {})[label] = v["basis"]
    return {k: {"basis": sum(m.values()) / len(m), "years": len(m), "by": dict(m)} for k, m in acc.items()}


def deviation(current: dict, baseline: dict, min_years: int = 1) -> dict:
    """{key: point with 'basis' replaced by current - baseline} for the locations that have both. `baseline` is {key: {'basis': mean, 'years': n}} (or a plain number); a location whose baseline rests on
    fewer than `min_years` years is left out."""
    out = {}
    for k, p in current.items():
        b = baseline.get(k)
        if b is None:
            continue
        val, yrs = (b["basis"], b.get("years", 1)) if isinstance(b, dict) else (b, 1)
        if yrs < min_years or val is None:
            continue
        out[k] = {**p, "basis": p["basis"] - float(val), "baseline": float(val), "years": yrs}
    return out


# -- the picture ----------------------------------------------------------------------------------------------------------
LEFT_W, HEADER_H, FOOTER_H, PAD = 142, 78, 40, 10


def _lines_overlay(view: View, geo: dict, mask: np.ndarray, ss: int = 3) -> Image.Image:
    """RGBA overlay of the county and state lines, drawn at ss times the size and shrunk (smooth lines), kept to the states picked (and a pixel round them)."""
    W, H = view.width * ss, view.height * ss
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    def draw_lines(lines, color, width):
        for ln in lines:
            xs, ys = view.xy([p[0] for p in ln], [p[1] for p in ln])
            if xs.max() < -2 or ys.max() < -2 or xs.min() > view.width + 2 or ys.min() > view.height + 2:
                continue
            d.line(list(zip((xs * ss).tolist(), (ys * ss).tolist())), fill=color, width=width)

    draw_lines(geo["county_lines"], (30, 30, 30, 120), 1)
    draw_lines(geo["state_lines"], (0, 0, 0, 255), ss)
    ov = ov.resize((view.width, view.height), Image.LANCZOS)
    keep = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))
    a = np.asarray(ov.getchannel("A"), np.float32) * (np.asarray(keep, np.float32) / 255.0)
    ov.putalpha(Image.fromarray(a.astype(np.uint8)))
    return ov


def render_panel(surface, view: View, geo: dict, mask: np.ndarray, *, title: str, subtitle: str = "", legend_title: str = "cents/bu", mode: str = "level", futures: str | None = None,
                 markers=None, footer: str = "", note: str | None = None) -> Image.Image:
    """One AgManager-style panel as a PIL image: title and subtitle on top, the futures-price box and the ten-class legend on the left, the surface with its lines on the right, the footer below.
    `surface` None means there was nothing to interpolate: the panel says so (`note`). `markers` is an iterable of (lon, lat) drawn as small dots."""
    W = LEFT_W + view.width + PAD
    H = HEADER_H + view.height + FOOTER_H
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    _text(d, (PAD, 10), title, 22, bold=True)
    tw = _font(22).getlength(title) + 1
    d.line((PAD, 38, PAD + tw, 38), fill=(25, 25, 25), width=2)
    if subtitle:
        _text(d, (PAD, 46), subtitle, 12, bold=False)
    ox, oy = LEFT_W, HEADER_H
    if surface is not None and np.isfinite(surface).any():
        edges = class_edges(surface, 10, mode)
        rgba = Image.fromarray(colorize(surface, edges, mode), "RGBA")
        img.paste(rgba, (ox, oy), rgba)
        # the legend: ten swatches, strongest (or highest) at the top, each labelled with its lower bound
        lx, ly = PAD, HEADER_H + (52 if futures else 4)
        d.rectangle((lx, ly, LEFT_W - PAD, ly + 24 + 10 * 19 + 8), outline=(40, 40, 40), width=1, fill=(255, 255, 255))
        _text(d, (lx + 8, ly + 6), legend_title, 12, bold=True)
        labels = edge_labels(edges, signed=(mode == "diverging"))
        for i in range(10):
            cls = 9 - i
            y = ly + 26 + i * 19
            d.rectangle((lx + 8, y, lx + 28, y + 15), fill=tuple(int(c) for c in ramp_rgb((cls + 0.5) / 10, mode)), outline=(90, 90, 90))
            _text(d, (lx + 36, y + 1), labels[cls], 13)
        lines_ov = _lines_overlay(view, geo, mask)
        img.paste(lines_ov, (ox, oy), lines_ov)
    else:
        _text(d, (ox + view.width // 2, oy + view.height // 2), note or "Not enough bids to draw a surface", 16, fill=(110, 110, 110), anchor="mm")
        lines_ov = _lines_overlay(view, geo, mask)
        img.paste(lines_ov, (ox, oy), lines_ov)
    if futures:
        bx, by = PAD, HEADER_H + 4
        d.rectangle((bx, by, LEFT_W - PAD, by + 40), outline=(40, 40, 40), width=1, fill=(255, 255, 255))
        head, _, price = futures.partition(" Price: ")
        _text(d, (bx + 8, by + 5), head, 12)
        _text(d, (bx + 8, by + 21), "Price: " + price, 12, bold=True)
    if markers is not None:
        mx, my = view.xy([m[0] for m in markers], [m[1] for m in markers]) if len(markers) else ([], [])
        for x, y in zip(np.atleast_1d(mx).tolist(), np.atleast_1d(my).tolist()):
            if 0 <= x < view.width and 0 <= y < view.height:
                d.ellipse((ox + x - 2.5, oy + y - 2.5, ox + x + 2.5, oy + y + 2.5), fill=(20, 20, 20), outline=(255, 255, 255))
    if footer:
        _text(d, (PAD, H - FOOTER_H + 14), footer, 11, fill=(100, 100, 100))
    try:
        logo = Image.open(LOGO_FILE).convert("RGBA")
        lh = FOOTER_H - 6
        logo = logo.resize((int(logo.width * lh / logo.height), lh), Image.LANCZOS)
        img.paste(logo, (W - logo.width - PAD, H - lh - 3), logo)
    except Exception:                                  # noqa: BLE001 - a picture without the logo is still a picture
        pass
    return img


def compose(panels, gap: int = 28) -> Image.Image:
    """Panels side by side on white, top aligned."""
    panels = list(panels)
    W = sum(p.width for p in panels) + gap * (len(panels) - 1)
    H = max(p.height for p in panels)
    out = Image.new("RGB", (W, H), (255, 255, 255))
    x = 0
    for p in panels:
        out.paste(p, (x, 0))
        x += p.width + gap
    return out


def to_png(img: Image.Image) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def mmddyyyy(d: date) -> str:
    return f"{d.month:02d}-{d.day:02d}-{d.year}"


# -- a whole pair --------------------------------------------------------------------------------------------------------------
REGIONS = {
    "Corn Belt": ["IA", "IL", "IN", "MN", "NE", "SD", "ND", "OH", "MO", "KS", "WI", "MI"],
    "Western Corn Belt": ["IA", "MN", "NE", "SD", "ND", "KS", "MO"],
    "Eastern Corn Belt": ["IL", "IN", "OH", "MI", "WI", "KY"],
    "Nebraska and Kansas": ["NE", "KS"],
    "Soft red winter states": ["IL", "IN", "OH", "MI", "WI", "MO", "KY"],
    "Hard red winter states": ["KS", "NE", "CO", "SD", "OK"],
}
BASELINES = {"3yr": "3-year average (same days)", "1yr": "Last year (same days)", "30d": "30 days ago", "7d": "7 days ago"}
# What can be mapped. Wheat is two commodities because its bids are filed by the futures market they are quoted against, not by class: the tracker's grain map has one generic 'Wheat' (a few 'HWW' and
# durum rows aside), and a bid's contract says which wheat it is - ZW is Chicago soft red winter, KE Kansas City hard red winter (spring wheat, MWE, has five bids: too few to map). `history`: whether
# the weekly history files (the 3-year / last-year baselines) cover it: the tracker holds no wheat before June 2026.
COMMODITIES = {
    "Corn": {"canonical": "Corn", "grain": lambda g: g == "Corn", "root": None, "history": True, "region": "Corn Belt"},
    "Soybeans": {"canonical": "Soybeans", "grain": lambda g: g == "Soybeans", "root": None, "history": True, "region": "Corn Belt"},
    "Soft Red Winter Wheat": {"canonical": "Wheat", "grain": lambda g: g == "Wheat", "root": "ZW", "history": False, "region": "Soft red winter states"},
    "Hard Red Winter Wheat": {"canonical": "Wheat", "grain": lambda g: g == "Wheat", "root": "KE", "history": False, "region": "Hard red winter states"},
}


def commodity_spec(crop: str) -> dict:
    """The entry for a commodity; a name the registry does not know is taken as a grain of that exact name, with history and the Corn Belt."""
    return COMMODITIES.get(crop) or {"canonical": crop, "grain": lambda g, c=crop: g == c, "root": None, "history": True, "region": "Corn Belt"}


def baselines_for(crop: str) -> list:
    """The baselines worth offering for a commodity: all four where the weekly history files cover it, only the 30 / 7-day changes where they do not (wheat)."""
    return list(BASELINES) if commodity_spec(crop)["history"] else ["30d", "7d"]


def default_region(crop: str) -> str:
    """The preset region a commodity opens on."""
    return commodity_spec(crop)["region"]


def _types_phrase(types) -> str:
    if types and len(types) == 1:
        t = next(iter(types)).lower()
        return t[:-1] + "ies" if t.endswith("y") else t + "s"
    return "locations"


def _same_day(d: date, year: int) -> date:
    """The same calendar day in another year (29 Feb becomes 28 Feb)."""
    try:
        return d.replace(year=year)
    except ValueError:
        return d.replace(year=year, day=28)


def build_pair(rows, crop: str, states, types, baseline: str, as_of: date, *, fetch, raw_grains, futures_cents, width: int = 640, window: int = 5, min_years: int = 2) -> dict:
    """The two panels for a commodity and a set of states, side by side: today's basis surface (the bids of `types`, or of every type when empty) and its deviation from a baseline ('3yr', '1yr', '30d' or
    '7d', BASELINES). rows are database.get_map_data() rows; fetch(grains, start_iso, end_iso, spot=None, symbol=None) is database.get_basis_window; raw_grains(crop) the grain map's raw names;
    futures_cents(symbol) the futures price in cents. A multi-year baseline (the weekly history files hold plants and terminals only) uses every type, since history exists for those alone; a 30 / 7-day
    baseline follows `types`. Returns {image, png, symbol, n_level, n_other, n_dev, dev_years, error}; error is set (and image None) when no state has an outline."""
    if baseline not in BASELINES:
        raise ValueError(f"unknown baseline {baseline!r}; one of {', '.join(BASELINES)}")
    geo = geometry()
    states = [s for s in states if s in geo["states"]]
    view = view_for_states(states, geo, width)
    if view is None:
        return {"image": None, "png": None, "error": "none of the states picked has an outline", "symbol": None, "n_level": 0, "n_other": 0, "n_dev": 0, "dev_years": []}
    mask = state_mask(view, states, geo)
    spec = commodity_spec(crop)
    grain_ok = spec["grain"]
    bid_ok = (lambda b: futures_root(b.get("futures_symbol")) == spec["root"]) if spec["root"] else None
    tset = set(types) if types else None
    lvl, sym, n_other = nearby_bids(rows, grain_ok, types=tset, states=set(states), bid_ok=bid_ok)
    fut = futures_label(sym, futures_cents(sym) if sym else None)
    fine = PRESETS["fine"]
    surf1 = interpolate(view, mask, [p["lon"] for p in lvl.values()], [p["lat"] for p in lvl.values()], [p["basis"] for p in lvl.values()], **fine) if lvl else None
    p1 = render_panel(surf1, view, geo, mask, title=f"{crop} Basis, {mmddyyyy(as_of)}", subtitle="Basis = Cash Price - Nearby Futures Price", futures=fut,
                      note="No bids for these states and types", footer=f"JSA Basis Tracker | {len(lvl)} {_types_phrase(tset)} | blank = no bid within {fine['gap_km']:.0f} km")
    # the baseline and the deviation
    dev, preset, years_used, title2, sub2 = {}, "fine", [], f"{crop} Basis Deviation, {mmddyyyy(as_of)}", ""
    note2 = "Not enough locations with a baseline" if sym else "No bids for these states and types"
    if sym:
        grains = raw_grains(spec["canonical"])
        if baseline in ("3yr", "1yr") and not spec["history"]:
            preset, sub2, note2 = "regional", f"Basis Deviation from the {BASELINES[baseline].lower()}", f"No {crop.lower()} history in the tracker before June 2026"
        elif baseline in ("3yr", "1yr"):
            cur, _, _ = nearby_bids(rows, grain_ok, types=None, states=set(states), symbol=sym, bid_ok=bid_ok)
            n_back = 3 if baseline == "3yr" else 1
            per = {}
            for y in range(as_of.year - n_back, as_of.year):
                t = _same_day(as_of, y)
                got = nearby_from_rows(fetch(grains, *window_bounds(t, window)), t)
                if got:
                    per[y] = got
            years_used = sorted(per)
            dev = deviation(cur, baseline_mean(per), min_years=min(min_years, max(1, len(per)))) if per else {}
            preset = "regional"
            ys = ", ".join(str(y) for y in years_used)
            sub2 = (f"Basis Deviation = Current Basis - {len(years_used)} Year Average Basis ({ys})" if baseline == "3yr" else f"Basis Deviation = Current Basis - Last Year's Basis (same days of {ys})")
        else:
            days, w = (30, 3) if baseline == "30d" else (7, 2)
            t = as_of - timedelta(days=days)
            cur, _, _ = nearby_bids(rows, grain_ok, types=tset, states=set(states), symbol=sym, bid_ok=bid_ok)
            then = nearby_from_rows(fetch(grains, *window_bounds(t, w), spot=False, symbol=sym), t, symbol=sym)
            dev = deviation(cur, {k: {"basis": v["basis"], "years": 1} for k, v in then.items()})
            title2 = f"{crop} Basis Change, {mmddyyyy(as_of)}"
            sub2 = f"Basis Change = Current Basis - Basis {days} Days Ago ({mmddyyyy(t)}), same futures contract"
    surf2 = None
    if len(dev) >= MIN_POINTS:
        surf2 = interpolate(view, mask, [p["lon"] for p in dev.values()], [p["lat"] for p in dev.values()], [p["basis"] for p in dev.values()], **PRESETS[preset])
    what = "locations with history (mostly plants and terminals)" if baseline in ("3yr", "1yr") else _types_phrase(tset)
    p2 = render_panel(surf2, view, geo, mask, title=title2, subtitle=sub2, mode="diverging", legend_title="cents/bu", futures=fut,
                      note=note2,
                      markers=[(p["lon"], p["lat"]) for p in dev.values()] if preset == "regional" else None,
                      footer=f"JSA Basis Tracker | {len(dev)} {what}" + (" | dots = those locations" if preset == "regional" else ""))
    img = compose([p1, p2])
    return {"image": img, "png": to_png(img), "symbol": sym, "n_level": len(lvl), "n_other": n_other, "n_dev": len(dev), "dev_years": years_used, "error": None}
