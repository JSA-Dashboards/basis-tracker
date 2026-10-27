"""Colouring the Map tab's pins by basis (map_basis.py): a scale relative to the pins shown, purple for weaker through grey to green for stronger, a faint pin where there is no bid.

    python tests/test_map_basis.py

Kolten 2026-10-27: "we will trend towards the basis on the map concept" - the pins coloured by basis level over the production heat. Pure functions: numbers in, colours and strings out.
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import map_basis as mb  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + str(detail)[:500]) if (not ok and str(detail)) else ""))
    if not ok:
        FAILS.append(name)


nan = float("nan")
close = lambda a, b, t=1e-9: abs(a - b) < t  # noqa: E731

print("the scale")
s = mb.scale(list(range(1, 11)))
check("the 10th, 50th and 90th percentiles of 1..10 are 1.9, 5.5 and 9.1", close(s["lo"], 1.9) and close(s["mid"], 5.5) and close(s["hi"], 9.1) and s["n"] == 10 and s["min"] == 1.0 and s["max"] == 10.0, s)
check("fewer than five bids say nothing about strong and weak: no scale", mb.scale([1, 2, 3, 4]) is None and mb.scale([]) is None)
check("five is enough", mb.scale([1, 2, 3, 4, 5]) is not None and mb.scale([1, 2, 3, 4, 5])["n"] == 5)
check("a missing bid is not a value (None and NaN are left out before counting)", mb.scale([1, 2, 3, 4, None, nan]) is None and mb.scale([1, 2, 3, 4, 5, None, nan])["n"] == 5)
e = mb.scale([-20] * 6)
check("all the same: the three percentiles are one number", e["lo"] == e["mid"] == e["hi"] == -20.0, e)
check("integers (cents as stored) and floats mix", mb.scale([-5, -10.5, -20, -15, -12])["n"] == 5)

print("where a value sits")
s = {"lo": -40.0, "mid": -20.0, "hi": -10.0, "n": 100, "min": -80.0, "max": 30.0}
check("the median is the middle, the 10th percentile the weak end, the 90th the strong end", mb.position(-20, s) == 0.5 and mb.position(-40, s) == 0.0 and mb.position(-10, s) == 1.0)
check("halfway from the median to either end is a quarter / three quarters (the two halves have their own spans)", close(mb.position(-30, s), 0.25) and close(mb.position(-15, s), 0.75))
check("beyond the ends it is clipped, not extrapolated", mb.position(-80, s) == 0.0 and mb.position(30, s) == 1.0)
check("it rises with the basis", all(mb.position(a, s) <= mb.position(b, s) for a, b in zip(range(-60, 20), range(-59, 21))))
d = {"lo": -20.0, "mid": -20.0, "hi": -5.0, "n": 9, "min": -20.0, "max": 0.0}
check("when the median is also the 10th percentile: below it is the weak end, equal stays in the middle, above climbs toward green", mb.position(-25, d) == 0.0 and mb.position(-20, d) == 0.5 and 0.5 < mb.position(-10, d) < 1.0)
d2 = {"lo": -40.0, "mid": -10.0, "hi": -10.0, "n": 9, "min": -40.0, "max": 0.0}
check("when the median is also the 90th percentile: above it is the strong end, equal stays in the middle", mb.position(-5, d2) == 1.0 and mb.position(-10, d2) == 0.5 and 0.0 < mb.position(-30, d2) < 0.5)
same = mb.scale([-20] * 6)
check("a scale of one number: equal is the middle, lower the weak end, higher the strong end", mb.position(-20, same) == 0.5 and mb.position(-21, same) == 0.0 and mb.position(-19, same) == 1.0)

print("colours")
stops = dict(mb.STOPS)
check("the weak end is the first stop, the strong end the last, the median the grey in the middle",
      mb.color(-40, s)[:3] == list(stops[0.0]) and mb.color(-10, s)[:3] == list(stops[1.0]) and mb.color(-20, s)[:3] == list(stops[0.5]))
check("a quarter of the way is the second stop exactly; between stops it is the average", mb.color(-30, s)[:3] == list(stops[0.25]) and mb.color(-35, s)[:3] == [round((a + b) / 2) for a, b in zip(stops[0.0], stops[0.25])])
check("beyond the ends is the end colour", mb.color(-500, s) == mb.color(-40, s) and mb.color(500, s) == mb.color(-10, s))
check("a pin's colour is r, g, b and an alpha, all whole numbers 0-255", all(len(mb.color(v, s)) == 4 and all(isinstance(c, int) and 0 <= c <= 255 for c in mb.color(v, s)) for v in range(-90, 40, 7)))
check("weak is purple (blue over green), strong is green (green over blue and red)", mb.color(-40, s)[2] > mb.color(-40, s)[1] and mb.color(-10, s)[1] > mb.color(-10, s)[2] and mb.color(-10, s)[1] > mb.color(-10, s)[0])
mid = mb.color(-20, s)
check("the middle is a neutral grey that still shows on the light basemap (equal channels, neither white nor dark)", mid[0] == mid[1] == mid[2] and 150 <= mid[0] <= 230, mid)
check("no bid (None, NaN): the faint grey", mb.color(None, s) == mb.NO_BID and mb.color(nan, s) == mb.NO_BID)
c = mb.color(None, s)
c[0] = 0
check("the faint grey is handed out as a copy: changing it does not change the constant", mb.NO_BID[0] == 185)
check("the faint grey is see-through (alpha under the pins')", mb.NO_BID[3] < mb.ALPHA)

vals = [-45, -38, -30, -28, -25, -22, -20, -18, -15, -12, -10, -4]
base = [mb.color(v, mb.scale(vals)) for v in vals]
check("the scale is relative: the same pins 100 cents higher get the same colours", [mb.color(v + 100, mb.scale([x + 100 for x in vals])) for v in vals] == base)
check("... and the same pins twice as spread out too", [mb.color(v * 2, mb.scale([x * 2 for x in vals])) for v in vals] == base)
sub = [-30, -28, -25, -22, -20, -18]
check("filtering changes the scale: a -30 bid is among the weakest of these pins but purple only relative to its peers", mb.color(-30, mb.scale(sub)) != mb.color(-30, mb.scale(vals)))

print("numbers and words")
check("basis reads like the tooltip: '-18c', '+5c', '+0c', rounded to a whole cent", mb.fmt(-18) == "-18c" and mb.fmt(5) == "+5c" and mb.fmt(0) == "+0c" and mb.fmt(-12.6) == "-13c" and mb.fmt(4.4) == "+4c")
check("canola, quoted in C$/t, keeps its unit with a space like the tooltip", mb.fmt(5, " C$/t") == "+5 C$/t" and mb.fmt(-12, " C$/t") == "-12 C$/t")
lg = mb.legend_html({"lo": -35.0, "mid": -18.0, "hi": 5.0, "n": 50, "min": -60.0, "max": 20.0})
check("the legend names what its ends and middle stand for", "Weaker: -35c or less" in lg and "+5c or more: Stronger" in lg and "median -18c" in lg, lg)
check("... with a five-stop gradient", "linear-gradient" in lg and lg.count("rgb(") == 5)
check("... and the canola unit", "Weaker: -35 C$/t or less" in mb.legend_html({"lo": -35.0, "mid": -18.0, "hi": 5.0, "n": 50, "min": 0.0, "max": 0.0}, " C$/t"))
cap = mb.describe({"lo": -35.0, "mid": -18.0, "hi": 5.0, "n": 1204, "min": -60.0, "max": 20.0}, "Corn", "Spot (Front Month)", 0)
check("the caption: the commodity and month, how many bids (with a thousands separator), the median, the percentiles, that it is relative",
      "Corn basis, Spot (Front Month)" in cap and "1,204 bids" in cap and "-18c (grey)" in cap and "-35c and +5c" in cap and "relative to the pins shown" in cap, cap)
check("no 'faint grey' sentence when every location has a bid", "faint grey" not in cap)
check("one location without a bid, then several (singular / plural)",
      "1 location with no bid for this month is faint grey" in mb.describe({"lo": 0.0, "mid": 1.0, "hi": 2.0, "n": 9, "min": 0.0, "max": 2.0}, "Corn", "Dec 2026", 1)
      and "12 locations with no bid for this month are faint grey" in mb.describe({"lo": 0.0, "mid": 1.0, "hi": 2.0, "n": 9, "min": 0.0, "max": 2.0}, "Corn", "Dec 2026", 12))
check("a count over 999 gets its thousands separator", "1,234 locations with no bid for this month are faint grey" in mb.describe({"lo": 0.0, "mid": 1.0, "hi": 2.0, "n": 9, "min": 0.0, "max": 2.0}, "Corn", "Dec 2026", 1234))
few = mb.describe(None, "Wheat", "Dec 2026", 3)
check("too few bids: it says so and says what to do", "Too few Wheat bids" in few and "location-type colours" in few and "Dec 2026" in few and str(mb.MIN_BIDS) in few, few)

print()
print("FAILED: %s" % ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
