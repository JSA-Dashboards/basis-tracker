"""The Trends tab's three tables above each section of the daily "JSA Daily Basis Changes" email (changes_report.build_changes_email_html).

    python tests/test_changes_trend_cards.py

Kolten 2026-11-12: "can we add in the trends table that is on the trends tab ... above each section show the table of trends avg basis changes weekly, monthly and
yearly changes overall and % east vs west". changes_report mirrors the app's Trends logic without Streamlit (build_trend_rows, _trend_move, get_adj -> _trend_adj,
render_trend_cards -> render_trend_cards_email). A synthetic set of six plants with hand-computed answers pins the numbers; nothing touches the network or a database.
"""
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database

import changes_report as cr            # noqa: E402
import database as db                  # noqa: E402
from models import Snapshot, SnapshotRow   # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="trendcards_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


NOW = datetime(2026, 11, 12, 12)
cr._GM = {}                                   # grain names pass through unchanged
CURVE = {"ZCZ26": 450.0, "ZCH27": 464.0}      # Dec 450, Mar 464: from ZCZ26 to ZCH27 the spread is -14
# For the whole run: no live futures curve (a network call) and no stored spreads, so every check below is hermetic; a check that needs others patches over these.
mock.patch.object(cr, "_futures_curve", lambda: CURVE).start()
mock.patch.object(db, "get_roll_spread", lambda a, b: None).start()


def snap(when, rows, exact=False):
    """A snapshot at `when` whose rows are [(deliveryMonth, futuresSymbol, basis)] forward rows (or ready-made SnapshotRows). Stamped at midnight of the day,
    or at the exact second when `exact` (the tolerance checks below need to sit on a boundary, which a midnight stamp would shift by half a day)."""
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ" if exact else "%Y-%m-%dT00:00:00Z")
    return Snapshot(timestamp=stamp, provider="X", location="Y", source="web",
                    rows=[r if isinstance(r, SnapshotRow) else SnapshotRow(id="r%d" % i, grain="Corn", deliveryMonth=r[0], futuresSymbol=r[1], basisCents=r[2])
                          for i, r in enumerate(rows)])


def plant(prov, loc, state, series):
    """series = {'now': rows, 'wk': rows, 'mo': rows, 'yr': rows}: the plant's snapshots at now / -7d / -30d / -365d (a missing key = no snapshot then)."""
    when = {"now": NOW, "wk": NOW - timedelta(days=7), "mo": NOW - timedelta(days=30), "yr": NOW - timedelta(days=365)}
    snaps = [snap(when[k], rows) for k, rows in series.items()]
    return (prov, loc), {"state": state, "facility_type": "Corn Processing"}, sorted(snaps, key=lambda s: s.timestamp)


# Six plants. Expected moves, spot basis now minus then (cents):
#   A IL East : LW +10-4 = +6   LM +10-0 = +10   LY +10-5 = +5 (ZCZ25 -> ZCZ26: same month, no adjustment)
#   B IL East : LW -5-(-2) = -3 LM -5-(-10) = +5 LY none
#   C IA West : LW 0-3 = -3     LM 0-0 = 0        LY 0-(-4) = +4
#   D NE West : LW 8-8 = 0      LM 8-2 = +6       LY 8-8 = 0
#   E IN East : LW ZCZ26 +30 -> ZCH27 +20 = (20-30) - (450-464) = +4 (rolled: the spread is added back)   LM +20-15 = +5   LY none
#   F OH East : LW ZCZ26 -> ZCK27 has no spread on file: left out  LM 12-8 = +4   LY none
PLANTS = [
    plant("ADM", "A", "IL", {"now": [("Nov 2026", "ZCZ26", 10), ("Jan 2027", "ZCH27", 6)], "wk": [("Nov 2026", "ZCZ26", 4)], "mo": [("Nov 2026", "ZCZ26", 0)], "yr": [("Nov 2025", "ZCZ25", 5)]}),
    plant("ADM", "B", "IL", {"now": [("Nov 2026", "ZCZ26", -5), ("Jan 2027", "ZCH27", -8)], "wk": [("Nov 2026", "ZCZ26", -2)], "mo": [("Nov 2026", "ZCZ26", -10)]}),
    plant("ADM", "C", "IA", {"now": [("Nov 2026", "ZCZ26", 0), ("Jan 2027", "ZCH27", 4)], "wk": [("Nov 2026", "ZCZ26", 3)], "mo": [("Nov 2026", "ZCZ26", 0)], "yr": [("Nov 2025", "ZCZ25", -4)]}),
    plant("ADM", "D", "NE", {"now": [("Nov 2026", "ZCZ26", 8), ("Jan 2027", "ZCH27", 3)], "wk": [("Nov 2026", "ZCZ26", 8)], "mo": [("Nov 2026", "ZCZ26", 2)], "yr": [("Nov 2025", "ZCZ25", 8)]}),
    plant("ADM", "E", "IN", {"now": [("Jan 2027", "ZCH27", 20)], "wk": [("Nov 2026", "ZCZ26", 30)], "mo": [("Jan 2027", "ZCH27", 15)]}),
    plant("ADM", "F", "OH", {"now": [("Jan 2027", "ZCK27", 12)], "wk": [("Nov 2026", "ZCZ26", 9)], "mo": [("Jan 2027", "ZCK27", 8)]}),
]
PAIRS = [p[0] for p in PLANTS]
META = {p[0]: p[1] for p in PLANTS}
DATA = {p[0]: p[2] for p in PLANTS}


def load_stub(_ft):
    return PAIRS, META, DATA, NOW


def rows_for(excluded=()):
    with mock.patch.object(cr, "_load", load_stub), mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
        return [r for r in cr.build_trend_rows("Corn Processing", "Corn", "spot") if (r["provider"], r["location"]) not in set(excluded)]


def moves(rows, win):
    with mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
        return {r["location"]: cr._trend_move(r, win) for r in rows}


print("the roll adjustment (app.get_adj)")
with mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: -3):
    A = cr._trend_adj
    check("the same contract needs none", A("ZCZ26", "ZCZ26") == {"adj": 0, "rolled": False})
    check("the same month of another year needs none (ZCZ25 -> ZCZ26)", A("ZCZ25", "ZCZ26") == {"adj": 0, "rolled": False})
    check("two meal contracts need none", A("ZMQ26", "ZMU26") == {"adj": 0, "rolled": False})
    check("a roll uses the live curve's spread, rounded (450 - 464 = -14)", A("ZCZ26", "ZCH27") == {"adj": -14, "rolled": True}, A("ZCZ26", "ZCH27"))
    check("with no leg on the curve it uses the frozen spread from stored history (-3)", A("ZCK27", "ZCN27") == {"adj": -3, "rolled": True}, A("ZCK27", "ZCN27"))
with mock.patch.object(cr, "_futures_curve", lambda: {"ZCZ26": 450.4, "ZCH27": 464.0}), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
    check("the spread is rounded to whole cents like the app's (450.4 - 464.0 = -13.6 -> -14)", cr._trend_adj("ZCZ26", "ZCH27") == {"adj": -14, "rolled": True}, cr._trend_adj("ZCZ26", "ZCH27"))
with mock.patch.object(cr, "_futures_curve", lambda: {"ZCZ26": 450.0}), mock.patch.object(db, "get_roll_spread", lambda a, b: -3):
    check("a curve with only one leg falls back to the frozen spread, not a half-known one", cr._trend_adj("ZCZ26", "ZCH27") == {"adj": -3, "rolled": True}, cr._trend_adj("ZCZ26", "ZCH27"))
with mock.patch.object(cr, "_futures_curve", lambda: {}), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
    check("the manual fallback covers ZSK26 -> ZSN26 (-16)", cr._trend_adj("ZSK26", "ZSN26") == {"adj": -16, "rolled": True})
    check("... and ZCK26 -> ZCN26 (-10)", cr._trend_adj("ZCK26", "ZCN26") == {"adj": -10, "rolled": True})
    check("a roll with nothing on file is unknown", cr._trend_adj("ZCZ26", "ZCK27") == {"adj": None, "rolled": True, "unknown": True})
    check("no symbols: nothing to adjust", cr._trend_adj(None, "ZCZ26") == {"adj": 0, "rolled": False} and cr._trend_adj("ZCZ26", "") == {"adj": 0, "rolled": False})

print("\nthe futures curve is fetched once per report build, not once per rolled location")
CURVE_CALLS = []
with mock.patch.object(cr, "_futures_curve", lambda: CURVE_CALLS.append(1) or {}), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
    for _ in range(3):
        cr._roll_spread("ZCZ26", "ZCH27")
    plain = len(CURVE_CALLS)
    del CURVE_CALLS[:]
    cr._LOAD_MEMO = {}
    try:
        for _ in range(5):
            cr._roll_spread("ZCZ26", "ZCH27")
        inside = len(CURVE_CALLS)
    finally:
        cr._LOAD_MEMO = None
        cr._CURVE_MEMO = None
check("outside a report build every lookup asks for the curve (3 lookups, 3 fetches)", plain == 3, plain)
check("inside one it is fetched once however many rolled locations ask, an empty answer (a feed down) included (5 lookups, 1 fetch): "
      "futures_spread.ensure_curve would otherwise try the network again for each", inside == 1, inside)

print("\nthe row builder (app.build_trend_rows) on six plants")
R = rows_for()
by = {r["location"]: r for r in R}
check("all six plants have a current basis", sorted(by) == list("ABCDEF"), sorted(by))
check("the nearest delivery month is the 'spot': A is +10 vs ZCZ26 now and +4 a week ago", (by["A"]["b_current"], by["A"]["f_current"], by["A"]["b_wk_ago"]) == (10, "ZCZ26", 4))
check("state -> region: IL / IN / OH East, IA / NE West", {k: by[k]["region"] for k in "ABCDEF"} == {"A": "East", "B": "East", "C": "West", "D": "West", "E": "East", "F": "East"}, {k: by[k]["region"] for k in "ABCDEF"})
check("a window with no snapshot is None (B has no year-ago)", by["B"]["b_yr_ago"] is None)
check("spot > next: A (10 vs 6) and B (-5 vs -8) are inverted, C (0 vs 4) is not, D (8 vs 3) is; E has one month: None",
      (by["A"]["spot_gt_next"], by["B"]["spot_gt_next"], by["C"]["spot_gt_next"], by["D"]["spot_gt_next"], by["E"]["spot_gt_next"]) == (True, True, False, True, None),
      {k: by[k]["spot_gt_next"] for k in "ABCDE"})
far = {k: list(v) for k, v in DATA.items()}
far[("ADM", "A")] = [snap(NOW, [("Nov 2026", "ZCZ26", 10)]), snap(NOW - timedelta(days=13), [("Nov 2026", "ZCZ26", 4)])]
with mock.patch.object(cr, "_load", lambda ft: (PAIRS, META, far, NOW)), mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
    ra = next(r for r in cr.build_trend_rows("Corn Processing", "Corn", "spot") if r["location"] == "A")
check("a snapshot 13 days back is no 'last week': 6 days from the target, outside the 4-day tolerance", ra["b_wk_ago"] is None, ra["b_wk_ago"])

print("\nwhich snapshot counts as 'now', last week, last month, last year (app.build_trend_rows' targets and tolerances)")


def one(series, state="IL", meta_extra=None, grain="Corn", mode="spot"):
    """build_trend_rows for ONE plant whose snapshots sit at the given offsets before NOW, to the second: series = [(days_back, rows)]."""
    snaps = sorted([snap(NOW - timedelta(days=d), rows, exact=True) for d, rows in series], key=lambda s: s.timestamp)
    key = ("ADM", "Z")
    meta = {"state": state, "facility_type": "Corn Processing", **(meta_extra or {})}
    with mock.patch.object(cr, "_load", lambda ft: ([key], {key: meta}, {key: snaps}, NOW)):
        return cr.build_trend_rows("Corn Processing", grain, mode)


def fr(b):
    return [("Nov 2026", "ZCZ26", b)]


g = one([(0, fr(10)), (6, fr(7)), (10, fr(1)), (28, fr(3)), (33, fr(0)), (363, fr(6)), (369, fr(2))])
check("the CLOSEST snapshot to each target is used: -6d for last week (not -10d), -28d for last month (not -33d), -363d for last year (not -369d)",
      g and (g[0]["b_current"], g[0]["b_wk_ago"], g[0]["b_mo_ago"], g[0]["b_yr_ago"]) == (10, 7, 3, 6), g and (g[0]["b_current"], g[0]["b_wk_ago"], g[0]["b_mo_ago"], g[0]["b_yr_ago"]))
h = one([(0, fr(10)), (11, fr(1)), (34, fr(0)), (369, fr(2))])
check("a snapshot exactly 4 days off last week's, last month's and last year's targets is still used (the tolerance is 4 days, inclusive)",
      h and (h[0]["b_wk_ago"], h[0]["b_mo_ago"], h[0]["b_yr_ago"]) == (1, 0, 2), h and (h[0]["b_wk_ago"], h[0]["b_mo_ago"], h[0]["b_yr_ago"]))
H1 = 1 / 24
h2 = one([(0, fr(10)), (11 + H1, fr(1)), (34 + H1, fr(0)), (369 + H1, fr(2))])
check("... one hour further out is not", h2 and (h2[0]["b_wk_ago"], h2[0]["b_mo_ago"], h2[0]["b_yr_ago"]) == (None, None, None), h2 and (h2[0]["b_wk_ago"], h2[0]["b_mo_ago"], h2[0]["b_yr_ago"]))
h3 = one([(0, fr(10)), (3, fr(1)), (26, fr(0)), (361, fr(2))])
check("the same on the near side: 4 days before each target counts too (it is a distance, not a direction)",
      h3 and (h3[0]["b_wk_ago"], h3[0]["b_mo_ago"], h3[0]["b_yr_ago"]) == (1, 0, 2), h3 and (h3[0]["b_wk_ago"], h3[0]["b_mo_ago"], h3[0]["b_yr_ago"]))
check("'now' is the latest date reached, tolerance 1.6 days: a plant whose last snapshot is 1.6 days back is in, an hour further out is not",
      len(one([(1.6, fr(10))])) == 1 and one([(1.6 + H1, fr(10))]) == [])

print("\nthe rest of the row builder")
only_spot = [SnapshotRow(id="s", grain="Corn", deliveryMonth="Spot", futuresSymbol="", basisCents=-12, isSpot=True)]
sp = one([(0, only_spot), (7, only_spot)])
check("a history-only plant (a spot row, no delivery months) still counts: its basis, no contract, no spot > next",
      sp and (sp[0]["b_current"], sp[0]["f_current"], sp[0]["spot_gt_next"]) == (-12, None, None), sp)
nr = one([(0, fr(10))], state="ZZ", meta_extra={"region": "West"})
check("a state with no region falls back to the location's stored region", nr and nr[0]["region"] == "West", nr)
import facility_overrides as fo          # noqa: E402
with mock.patch.dict(fo.FACILITY_GRAIN_OVERRIDES, {("ADM", "Z", "Corn"): "Rail Terminal"}):
    check("a location whose Corn is overridden into another facility type is not in this category", one([(0, fr(10))]) == [])
check("without the override it is", len(one([(0, fr(10))])) == 1)
mixed = [SnapshotRow(id="c", grain="Corn", deliveryMonth="Nov 2026", futuresSymbol="ZCZ26", basisCents=10),
         SnapshotRow(id="s", grain="Soybeans", deliveryMonth="Nov 2026", futuresSymbol="ZSX26", basisCents=-50)]
cg, sg = one([(0, mixed)]), one([(0, mixed)], grain="Soybeans")
check("a snapshot holding two grains: each table uses its own grain's row", bool(cg and sg) and (cg[0]["b_current"], cg[0]["f_current"], sg[0]["b_current"], sg[0]["f_current"]) == (10, "ZCZ26", -50, "ZSX26"), (cg, sg))
check("a location with no row for the grain is not in its table", one([(0, mixed[:1])], grain="Soybeans") == [])
dup = [("Nov 2026", "ZCZ26", 10), ("LH Nov 2026", "ZCZ26", 20), ("Jan 2027", "ZCH27", 15)]
d1 = one([(0, dup)])
check("two rows of one contract month: the first is that month on the curve (10 against January's 15: not inverted)", bool(d1) and d1[0]["spot_gt_next"] is False, d1)
tie = one([(0, [("Nov 2026", "ZCZ26", 10), ("Jan 2027", "ZCH27", 10)])])
check("equal bids in the nearest and the next month are not inverted (it takes strictly above)", bool(tie) and tie[0]["spot_gt_next"] is False, tie)
rev = one([(0, [("Jan 2027", "ZCH27", 5), ("Nov 2026", "ZCZ26", 9)])])
check("the order the rows come in does not matter: November's 9 over January's 5 is inverted", bool(rev) and (rev[0]["b_current"], rev[0]["spot_gt_next"]) == (9, True), rev)

print("\nthe other delivery modes (a month label instead of 'spot')")


def two(a, b):
    return [("Nov 2026", "ZCZ26", a), ("Jan 2027", "ZCH27", b)]


mm = one([(0, two(10, 6)), (7, two(4, 1))], mode="Jan 2027")
check("mode 'Jan 2027': the January bid now and a week ago, each with its own contract",
      bool(mm) and (mm[0]["b_current"], mm[0]["f_current"], mm[0]["b_wk_ago"], mm[0]["f_wk_ago"]) == (6, "ZCH27", 1, "ZCH27"), mm)
check("a plant that does not quote that month is left out", one([(0, two(10, 6))], mode="Mar 2027") == [])

print("\nthe moves (app._trend_move)")
check("LW: A +6, B -3, C -3, D 0, E +4 (rolled, spread added back), F left out (a roll with no spread)", moves(R, "wk_ago") == {"A": 6, "B": -3, "C": -3, "D": 0, "E": 4, "F": None}, moves(R, "wk_ago"))
check("LM: A +10, B +5, C 0, D +6, E +5, F +4", moves(R, "mo_ago") == {"A": 10, "B": 5, "C": 0, "D": 6, "E": 5, "F": 4}, moves(R, "mo_ago"))
check("LY: A +5, C +4, D 0; B, E, F have no year-ago", moves(R, "yr_ago") == {"A": 5, "B": None, "C": 4, "D": 0, "E": None, "F": None}, moves(R, "yr_ago"))

print("\nthe three tables (region section)")
H = cr.render_trend_cards_email(R, "region", ["East", "West"]).replace("&minus;", "-").replace("&mdash;", "~")


def cells(html):
    """The text of every <td>, in document order, nested cells included (the wrapper cell of a card holds a title and a table: the title is its own entry);
    empty cells are skipped. A real HTML parser: a regex cannot tell a wrapper cell from the cell inside it."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.out = [], []

        def handle_starttag(self, tag, attrs):
            if tag == "td":
                self.stack.append([])

        def handle_data(self, data):
            if self.stack:
                self.stack[-1].append(data)

        def handle_endtag(self, tag):
            if tag == "td" and self.stack:
                t = "".join(self.stack.pop()).strip()
                if t:
                    self.out.append(t)

    p = P()
    p.feed(html)
    return p.out


T = cells(H)


def after(label, n):
    i = T.index(label)
    return T[i + 1:i + 1 + n]


check("All Plants: LW +0.8 (4/5), LM +5.0 (30/6), LY +3.0 (9/3)", after("All Plants", 3) == ["+0.8", "+5.0", "+3.0"], after("All Plants", 3))
check("Firmer only: LW +5.0 (6 and 4), LM +6.0, LY +4.5; Weaker only: LW -3.0, LM and LY none", after("Firmer only", 3) == ["+5.0", "+6.0", "+4.5"] and after("Weaker only", 3) == ["-3.0", "~", "~"], (after("Firmer only", 3), after("Weaker only", 3)))
check("the East and West labels are on the Firmer / Weaker rows", sorted(t.split()[0] + " " + t.split()[1] for t in T if t.startswith(("East ", "West ")) and t.split()[1] in ("Firmer", "Weaker")) == ["East Firmer", "East Weaker", "West Firmer", "West Weaker"])
check("East Firmer: LW 67% (2 of 3), LM 100%, LY 100% (1 of 1)", after("East Firmer", 3) == ["67%", "100%", "100%"], after("East Firmer", 3))
check("East Weaker: LW 33%, LM 0%, LY 0%", after("East Weaker", 3) == ["33%", "0%", "0%"], after("East Weaker", 3))
check("West Firmer: LW 0%, LM 50%, LY 50%; West Weaker: LW 50%, LM 0%, LY 0%", after("West Firmer", 3) == ["0%", "50%", "50%"] and after("West Weaker", 3) == ["50%", "0%", "0%"], (after("West Firmer", 3), after("West Weaker", 3)))
check("Spot > Next: East 100% (A and B; E and F have one month), West 50% (C no, D yes)", after("East", 1) == ["100%"] and after("West", 1) == ["50%"], (after("East", 1), after("West", 1)))
check("East comes before West, in the Firmer / Weaker table and in Spot > Next", T.index("East Firmer") < T.index("East Weaker") < T.index("West Firmer") < T.index("West Weaker") and T.index("East") < T.index("West"))
check("the headings are the Trends tab's", all(x in H for x in ("AVG BASIS CHANGE", "FIRMER / WEAKER BY REGION", "SPOT &gt; NEXT", "VS LW", "VS LM", "VS LY", "% INVERTED")))
check("the three window columns run LW, LM, LY", H.index("VS LW") < H.index("VS LM") < H.index("VS LY"))
raw = cr.render_trend_cards_email(R, "region", ["East", "West"])
check("a positive change is green and bold, a negative red and bold, a missing one a dash", '<td align="right" style="color:#16a34a"><b>+5.0</b></td>' in raw and '<td align="right" style="color:#dc2626"><b>&minus;3.0</b></td>' in raw and "&mdash;" in raw)
check("email-safe: nested tables with attributes, no CSS grid / flex / classes", "display:grid" not in raw and "display:flex" not in raw and "class=" not in raw and raw.count("<table") == 4, raw.count("<table"))

print("\nindex outliers are left out, as on the Trends tab")
R2 = rows_for(excluded=[("ADM", "A")])
with mock.patch.object(cr, "_load", load_stub), mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
    H_all = cr._trend_cards_html("Corn Processing", "Corn", "region", "spot", set())
    H_ex = cr._trend_cards_html("Corn Processing", "Corn", "region", "spot", {("ADM", "A")})
T_all = cells(H_all.replace("&minus;", "-"))
T_ex = cells(H_ex.replace("&minus;", "-"))
i = T_ex.index("All Plants")
check("without plant A: All Plants LW -0.5 ((-3-3+0+4)/4), LM +4.0 ((5+0+6+5+4)/5), LY +2.0 ((4+0)/2)", T_ex[i + 1:i + 4] == ["-0.5", "+4.0", "+2.0"], T_ex[i + 1:i + 4])
check("and the table differs from the one with it", T_all != T_ex)
check("the section's own call puts East before West too (the regions come from the rows, in that order)",
      T_all.index("East Firmer") < T_all.index("East Weaker") < T_all.index("West Firmer") < T_all.index("West Weaker"))
with mock.patch.object(cr, "_load", load_stub), mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
    check("every plant excluded -> '' (a section with no locations shows no tables)", cr._trend_cards_html("Corn Processing", "Corn", "region", "spot", set(PAIRS)) == "")

print("\nriver sections: segments, not East / West")
SEG = {("ADM", "A"): "Upper Miss", ("ADM", "B"): "STL", ("ADM", "C"): "STL", ("ADM", "D"): "Ohio"}
with mock.patch.object(cr, "river_segment", lambda loc: SEG.get(("ADM", loc), "Other")), mock.patch.object(cr, "_load", load_stub), mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
    RV = cr.build_trend_rows("Corn Processing", "Corn", "spot")
    HR = cr._trend_cards_html("Corn Processing", "Corn", "segment", "spot", set())
TR = cells(HR.replace("&minus;", "-").replace("&mdash;", "~"))
check("the middle table is 'Avg Basis & Change by Segment' with an Avg Basis column", "AVG BASIS &amp; CHANGE BY SEGMENT" in HR and "AVG BASIS" in TR)
j = TR.index("STL")
check("STL (B and C): avg basis (-5 + 0)/2 = -2.5, LW (-3 + -3)/2 = -3.0, LM (5 + 0)/2 = +2.5, LY +4.0 (C only)", TR[j + 1:j + 5] == ["-2.5", "-3.0", "+2.5", "+4.0"], TR[j + 1:j + 5])
order = [t for t in TR if t in ("Illinois Z1", "Upper Miss", "Mid Miss", "STL", "Lower Miss", "Ohio", "Other")]
check("segments come in the river order (SEGMENT_ORDER), only those present", order[:4] == ["Upper Miss", "STL", "Ohio", "Other"], order)
check("Spot > Next is by segment, with the header 'SEGMENT'", "SEGMENT" in TR and "REGION" not in TR)

print("\nthe email: tables above each section, and a failure there never stops it")
CALLS = []


def fake_rows(ft, gr, mode="spot"):
    CALLS.append((ft, gr, mode))
    if gr == "Soybeans":
        raise RuntimeError("boom: the trend rows could not be built")
    return [dict(r) for r in R]


EMPTY = {"m1_label": "November", "m2_label": "December", "rows": []}
NOSEG = {"m1_label": None, "m2_label": None, "rows": []}


def build_email(rows_fn, excluded=(), mode=None):
    """The whole email with the trend rows from `rows_fn`, the section tables stubbed empty and `excluded` as the Trends tab's outlier list."""
    with mock.patch.object(cr, "build_trend_rows", rows_fn), mock.patch.object(cr, "_index_excludes", lambda: set(excluded)), mock.patch.object(cr, "_table_watermark", lambda: None), \
            mock.patch.object(cr, "build_change_rows", lambda *a, **k: EMPTY), mock.patch.object(cr, "build_segment_change_rows", lambda *a, **k: NOSEG), \
            mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None):
        return cr.build_changes_email_html() if mode is None else cr.build_changes_email_html(mode)


EMAIL = build_email(fake_rows)
titles = [t[0] for t in cr.TREND_CATEGORIES]
pos = [EMAIL.index(">%s<" % t) for t in titles]
check("all 11 sections are still there, in order", pos == sorted(pos) and len(pos) == 11, pos)
sect = [EMAIL[pos[i]:(pos[i + 1] if i + 1 < len(pos) else len(EMAIL))] for i in range(len(pos))]
has = [("AVG BASIS CHANGE" in s) for s in sect]
check("every section but the two Soybeans ones (their rows raised) has the tables: 9 of 11", sum(has) == 9 and [titles[i] for i, h in enumerate(has) if not h] == ["Soy Processing — Soybeans", "River Terminals — Soybeans"], [titles[i] for i, h in enumerate(has) if not h])
check("the tables sit between the heading and the section's own table / message", all(s.index("AVG BASIS CHANGE") < max(s.find("No changes today."), s.find("No data.")) for s in sect if "AVG BASIS CHANGE" in s))
check("a river section gets the segment version, a regional one the East / West version", "FIRMER / WEAKER BY REGION" in sect[0] and "BY SEGMENT" in sect[2] and "FIRMER / WEAKER BY REGION" not in sect[2])
check("one legend line explains LW / LM / LY and the Mississippi split", EMAIL.count("Trend tables (the Trends tab)") == 1 and "(LW)" in EMAIL and "Mississippi River" in EMAIL)
check("the snapshot-load memo is cleared once the report is built", cr._LOAD_MEMO is None)
check("trend rows were asked for every category, in the sections' order, with the default mode 'spot'",
      len(CALLS) == 11 and [c[:2] for c in CALLS] == [t[1:3] for t in cr.TREND_CATEGORIES] and {c[2] for c in CALLS} == {"spot"}, CALLS[:3])
del CALLS[:]
build_email(fake_rows, mode="Dec 2026")
check("a report for another delivery month hands that month to the trend tables too", len(CALLS) == 11 and {c[2] for c in CALLS} == {"Dec 2026"}, CALLS[:3])


def section(email, i):
    p = [email.index(">%s<" % t[0]) for t in cr.TREND_CATEGORIES]
    return email[p[i]:(p[i + 1] if i + 1 < len(p) else len(email))]


E_ex = build_email(lambda ft, gr, mode="spot": [dict(r) for r in R], excluded=[("ADM", "A")])
E_in = build_email(lambda ft, gr, mode="spot": [dict(r) for r in R])
c_ex = cells(section(E_ex, 0).replace("&minus;", "-"))
c_in = cells(section(E_in, 0).replace("&minus;", "-"))
check("the Trends tab's index outliers are left out of the emailed numbers (plant A out: All Plants -0.5 / +4.0 / +2.0; in: +0.8 / +5.0 / +3.0)",
      c_ex[c_ex.index("All Plants") + 1:c_ex.index("All Plants") + 4] == ["-0.5", "+4.0", "+2.0"] and c_in[c_in.index("All Plants") + 1:c_in.index("All Plants") + 4] == ["+0.8", "+5.0", "+3.0"],
      (c_ex[c_ex.index("All Plants") + 1:c_ex.index("All Plants") + 4], c_in[c_in.index("All Plants") + 1:c_in.index("All Plants") + 4]))

E2 = build_email(lambda *a, **k: [])
check("with no trend rows anywhere the email is as it was: no tables and no legend", "AVG BASIS CHANGE" not in E2 and "Trend tables" not in E2)
E3 = build_email(lambda ft, gr, mode="spot": [dict(r) for r in R] if (ft, gr) == ("Corn Processing", "Corn") else [])
check("tables in the first section only: they show there, the legend is there once, and the other ten sections are untouched",
      E3.count("AVG BASIS CHANGE") == 1 and E3.count("Trend tables (the Trends tab)") == 1 and "AVG BASIS CHANGE" in section(E3, 0), (E3.count("AVG BASIS CHANGE"), E3.count("Trend tables")))

print("\nthe snapshot load is shared inside one report build, and only there")
LOADS = []


def counting_load_fresh(ft):
    LOADS.append(ft)
    return PAIRS, META, DATA, NOW


def rows_via_load(ft, gr, mode="spot"):                  # what build_trend_rows / build_change_rows both do first
    cr._load(ft)
    return []


with mock.patch.object(cr, "_load_fresh", counting_load_fresh), mock.patch.object(cr, "build_trend_rows", rows_via_load), mock.patch.object(cr, "_index_excludes", lambda: set()), \
        mock.patch.object(cr, "_table_watermark", lambda: None), \
        mock.patch.object(cr, "build_change_rows", lambda ft, gr, mode="spot": (cr._load(ft), EMPTY)[1]), \
        mock.patch.object(cr, "build_segment_change_rows", lambda ft, gr, mode="spot": (cr._load(ft), {"m1_label": None, "m2_label": None, "rows": []})[1]):
    cr.build_changes_email_html()
kinds = {t[1] for t in cr.TREND_CATEGORIES}
check("one report build: 11 sections, each calling the load twice (trend tables + section table), but each facility type is loaded once",
      sorted(LOADS) == sorted(kinds) and len(LOADS) < 22, (len(LOADS), sorted(LOADS)))
check("... and the memo is gone again afterwards", cr._LOAD_MEMO is None)


def broken(*a, **k):
    cr._trend_curve_once()                     # a build that has already fetched the futures curve ...
    raise RuntimeError("the section table broke")


with mock.patch.object(cr, "build_trend_rows", lambda *a, **k: []), mock.patch.object(cr, "_index_excludes", lambda: set()), mock.patch.object(cr, "_table_watermark", lambda: None), \
        mock.patch.object(cr, "_futures_curve", lambda: {}), mock.patch.object(cr, "build_change_rows", broken):
    try:
        cr.build_changes_email_html()
        raised = False
    except RuntimeError:
        raised = True
check("a build that dies part-way still drops the snapshot memo and the curve memo (a later call must not read its data)",
      raised and cr._LOAD_MEMO is None and cr._CURVE_MEMO is None, (raised, cr._LOAD_MEMO, cr._CURVE_MEMO))
n = []
with mock.patch.object(cr, "_load_fresh", lambda ft: n.append(ft) or ("p", "m", "d", "n")):
    cr._load("Corn Processing")
    cr._load("Corn Processing")
    plain = len(n)
    cr._LOAD_MEMO = {}
    try:
        cr._load("Corn Processing")
        cr._load("Corn Processing")
        cr._load("River Terminal")
        memo = len(n) - plain
    finally:
        cr._LOAD_MEMO = None
check("outside a report build every call loads (2 calls, 2 loads)", plain == 2, plain)
check("inside one, each facility type loads once (3 calls over 2 types, 2 loads)", memo == 2, memo)

print("\nthe index-outlier list")
with mock.patch.object(db, "get_index_excludes", lambda: {("ADM", "A")}):
    check("the Trends tab's list is read from the database", cr._index_excludes() == {("ADM", "A")})


def boom():
    raise RuntimeError("no table")


with mock.patch.object(db, "get_index_excludes", boom):
    check("if it cannot be read, nothing is excluded (and the email still goes)", cr._index_excludes() == set())

if FAILS:
    print("\n%d FAILED:" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nall trend-card checks passed")
