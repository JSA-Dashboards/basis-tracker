"""
map_basis.py - colouring the Map tab's pins by basis: where basis is strong and where it is weak, on the same canvas as the production heat (heat_layer.py).

Kolten 2026-10-27: "we will trend towards the basis on the map concept"; asked whether to start with the pins coloured by basis level over the production heat: "Yes after the rest is done."

The pins already carry the basis in their tooltip (the commodity and delivery month picked above the map). Here the same bid sets the pin's COLOUR: purple where it is weaker than most of the pins
shown, green where it is stronger, grey in the middle. Basis is not comparable across commodities, delivery months or even location types (a river terminal and a country elevator sit at
different levels), so the scale is always RELATIVE TO THE PINS SHOWN: the median of the bids on the map is the grey middle, the 10th percentile is the fullest purple and the 90th the fullest
green (anything beyond is clipped). The Location Type and State filters therefore compare a place with its peers, and the legend says which numbers those are. A location with no bid for the chosen
month is a faint grey pin. Purple / green (not red / green) so the pins read on top of the heat layer's yellow-to-red and for colour-blind readers.
Pure: numbers in, colours and strings out.
"""
from __future__ import annotations

import math

import numpy as np

MIN_BIDS = 5                                  # fewer bids than this and there is nothing to compare: the pins keep their location-type colours
LOW_Q, HIGH_Q = 10, 90                        # the percentiles of the fullest purple and green
ALPHA = 235
# position on the scale (0 = weakest, 0.5 = the median, 1 = strongest) -> RGB
STOPS = ((0.0, (118, 42, 131)), (0.25, (170, 135, 190)), (0.5, (205, 205, 205)), (0.75, (120, 190, 118)), (1.0, (27, 120, 55)))
NO_BID = [185, 185, 185, 110]                 # a location with no bid for the chosen month: faint


def _is_number(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def scale(values) -> dict | None:
    """{lo, mid, hi, n, min, max} from the basis values on the map: the 10th, 50th and 90th percentiles (linear interpolation) of the numbers (a missing value is not one). None when there are
    fewer than MIN_BIDS, because three or four bids say nothing about strong and weak."""
    v = np.array([float(x) for x in values if _is_number(x)], dtype=float)
    if len(v) < MIN_BIDS:
        return None
    lo, mid, hi = np.percentile(v, [LOW_Q, 50, HIGH_Q])
    return {"lo": float(lo), "mid": float(mid), "hi": float(hi), "n": int(len(v)), "min": float(v.min()), "max": float(v.max())}


def position(v: float, sc: dict) -> float:
    """Where a basis value sits on the diverging scale: the median is 0.5, the 10th percentile 0 and the 90th 1, clipped beyond. When the median is also the 10th (or 90th) percentile, a value
    below (above) it goes straight to the end of the scale and a value equal to the median stays in the middle."""
    lo, mid, hi = sc["lo"], sc["mid"], sc["hi"]
    if v <= mid:
        p = 0.5 * (v - lo) / (mid - lo) if mid > lo else (0.5 if v >= mid else 0.0)
    else:
        p = 0.5 + 0.5 * (v - mid) / (hi - mid) if hi > mid else 1.0
    return min(1.0, max(0.0, p))


def _rgb(p: float) -> tuple:
    for (p0, c0), (p1, c1) in zip(STOPS, STOPS[1:]):
        if p <= p1:
            t = 0.0 if p1 == p0 else (p - p0) / (p1 - p0)
            return tuple(int(round(a + (b - a) * t)) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def color(v, sc: dict) -> list:
    """[r, g, b, a] for a pin whose bid has this basis; the faint grey for a location with no bid."""
    if not _is_number(v):
        return list(NO_BID)
    return [*_rgb(position(float(v), sc)), ALPHA]


def fmt(v: float, unit: str = "c") -> str:
    """-18 -> '-18c', 5 -> '+5c'; unit ' C$/t' for canola, which is quoted that way."""
    n = int(round(v))
    return f"{'+' if n >= 0 else '-'}{abs(n)}{unit}"


def legend_html(sc: dict, unit: str = "c") -> str:
    """The purple-to-green bar with the numbers its ends and its middle stand for."""
    ramp = ", ".join(f"rgb({r},{g},{b})" for _, (r, g, b) in STOPS)
    return ("<div style='display:flex;align-items:center;gap:10px;margin:2px 0 6px 0;font-size:11px;color:#64748b'>"
            f"<span>Weaker: {fmt(sc['lo'], unit)} or less</span>"
            f"<div style='flex:0 0 220px;height:10px;border-radius:5px;background:linear-gradient(90deg,{ramp});border:1px solid #d7e2dc'></div>"
            f"<span>{fmt(sc['hi'], unit)} or more: Stronger</span><span style='margin-left:6px'>median {fmt(sc['mid'], unit)}</span></div>")


def describe(sc: dict | None, commodity: str, delivery: str, n_nobid: int, unit: str = "c") -> str:
    """The caption under the legend: what the colours mean, against what, and how many locations have no bid."""
    if sc is None:
        return (f"Too few {commodity} bids to compare (fewer than {MIN_BIDS} with a bid for {delivery}): the pins keep their location-type colours. "
                "Pick another delivery month or widen the Location Type / State filters.")
    parts = [f"Pins are coloured by {commodity} basis, {delivery}: the median of the {sc['n']:,} bids on the map is {fmt(sc['mid'], unit)} (grey), green is stronger and purple weaker, "
             f"the fullest colours at the 10th and 90th percentile ({fmt(sc['lo'], unit)} and {fmt(sc['hi'], unit)}). The scale is relative to the pins shown, so the Location Type and State "
             "filters compare a place with its peers."]
    if n_nobid:
        parts.append(f"{n_nobid:,} location{'s' if n_nobid != 1 else ''} with no bid for this month {'are' if n_nobid != 1 else 'is'} faint grey.")
    return "  \n".join(parts)
