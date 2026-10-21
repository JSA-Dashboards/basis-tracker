"""One number per delivery month for the forward curves (forward_month): a month's FULL bid is the month; its halves count only when there is no
full bid, and then the midpoint of FH and LH if both are posted; a package fills only a month nothing of its own names.

    python tests/test_forward_month.py

Kolten 2026-10-20, looking at the emailed CSX Columbus chart: December's bid was +16 and the forward curve drew +15 ("for months that show a full
month but also a FH or LH just default to the full month indication, only take the midpoint if its a FH and LH separate bids"). Pure functions;
nothing here touches a database.
"""
import os
import sys
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL"):
    os.environ[k] = ""

import forward_month as fm  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


# ── what a month stands for ───────────────────────────────────────────────────────────────────────────
print("month_value: the rule")
mv = fm.month_value
check("a full bid beats its half (Dec +16, FH Dec +13 -> +16, not 14.5)", mv([("fh", 13), ("full", 16)]) == 16.0, mv([("fh", 13), ("full", 16)]))
check("order does not matter", mv([("full", 16), ("fh", 13)]) == 16.0)
check("a full bid beats BOTH halves", mv([("fh", -20), ("lh", -15), ("full", 5)]) == 5.0)
check("FH and LH posted separately, no full bid: the midpoint (-20 / -15 -> -17.5)", mv([("fh", -20), ("lh", -15)]) == -17.5)
check("a lone FH stands for its month", mv([("fh", -12)]) == -12.0)
check("a lone LH stands for its month", mv([("lh", 7)]) == 7.0)
check("a day window that opens the month counts as its first part ('Oct 15-25' + 'Oct 26-31' -> the midpoint)", mv([("window", -5), ("lh", 0)]) == -2.5)
check("a full bid beats a window too", mv([("window", -5), ("full", 3)]) == 3.0)
check("two full bids (a Dom and a Mex line, say) are averaged", mv([("full", 10), ("full", 20)]) == 15.0)
check("a bid of None is not a quote", mv([("full", None), ("fh", 4)]) == 4.0 and mv([("full", None)]) is None)
check("no quotes, no value", mv([]) is None)
check("a package fold is the last resort, never over a month's own quote", mv([("bundle", 22), ("fh", -12)]) == -12.0 and mv([("bundle", 22)]) == 22.0)
check("an unknown kind is read as a full bid", mv([("something", 9), ("fh", 1)]) == 9.0)
check("pick_months groups by key and keeps the order first seen",
      list(fm.pick_months([("b", "full", 1), ("a", "fh", 2), ("a", "lh", 4), ("b", "fh", 99)]).items()) == [("b", 1.0), ("a", 3.0)])

# ── how a label reads ─────────────────────────────────────────────────────────────────────────────────
print("label_info: which months a period covers")
li = fm.label_info
check("'Dec' / 'December 2026' are a full month", li("Dec") == ("full", (12,)) and li("December 2026") == ("full", (12,)), (li("Dec"), li("December 2026")))
check("'FH Dec' and 'LH Oct' are halves", li("FH Dec") == ("fh", (12,)) and li("LH Oct") == ("lh", (10,)), (li("FH Dec"), li("LH Oct")))
check("a day window: 'Oct 15-25' is a window, 'Oct 26-31' the last part, 'Oct 1-31' the whole month",
      li("Oct 15-25")[0] == "window" and li("Oct 26-31")[0] == "lh" and li("Oct 1-31")[0] == "full", (li("Oct 15-25"), li("Oct 26-31"), li("Oct 1-31")))
check("packages name every month they span ('JFM', 'AMJJ', 'Nov-Jul', 'Oct/Nov')",
      li("JFM") == ("bundle", (1, 2, 3)) and li("AMJJ") == ("bundle", (4, 5, 6, 7)) and li("Nov-Jul")[1] == (11, 12, 1, 2, 3, 4, 5, 6, 7) and li("Oct/Nov")[1] == (10, 11),
      (li("JFM"), li("AMJJ"), li("Nov-Jul"), li("Oct/Nov")))
check("labels that name no month are not months ('Spot', 'Return Trip', 'NC 26')", all(li(x) == (None, ()) for x in ("Spot", "Return Trip", "NC 26", "")), [li(x) for x in ("Spot", "Return Trip", "NC 26")])
check("kind_of reads a month-less label as a full bid", fm.kind_of("New Crop") == "full" and fm.kind_of("FH Nov") == "fh")
check("LP / FP are the last / first half and MP a partial window, as the seasonal chart reads them (parse_label alone would say full)",
      li("LP Nov") == ("lh", (11,)) and li("FP Dec") == ("fh", (12,)) and li("MP Jan") == ("window", (1,)), (li("LP Nov"), li("FP Dec"), li("MP Jan")))
check("the freight sheets' F/H and L/H are halves too ('F/H November (bid is FP)', 'L/H October', 'F.H. Nov')",
      li("F/H November (bid is FP)") == ("fh", (11,)) and li("L/H October") == ("lh", (10,)) and li("F.H. Nov")[0] == "fh", (li("F/H November (bid is FP)"), li("L/H October"), li("F.H. Nov")))
check("a Dom / Mex suffix or a year does not change the kind (the board's market line already splits Dom / Mex)",
      li("Dec Dom")[0] == "full" and li("FH Nov Mex")[0] == "fh" and li("Split Nov")[0] == "full" and li("OCTOBER")[0] == "full", (li("Dec Dom"), li("FH Nov Mex"), li("Split Nov"), li("OCTOBER")))

# ── the rail posting behind the emailed chart ─────────────────────────────────────────────────────────
print("rail_forward: CSX Columbus, 2026-10-20 (the posting in Kolten's screenshot)")
CSX = [("Oct", -10), ("FH Nov", -12), ("Nov", 19), ("FH Dec", 13), ("Dec", 16), ("JFM", 19), ("AMJJ", 20), ("Nov-Jul", 22)]
fwd = fm.rail_forward(CSX)
check("October is its own bid", fwd[10] == -10.0, fwd)
check("November is the Nov bid (+19), not the average of FH Nov, Nov and the Nov-Jul package (+9.7, drawn '+10')", fwd[11] == 19.0, fwd[11])
check("December is the Dec bid (+16), not the average with FH Dec (+14.5, drawn '+15')", fwd[12] == 16.0, fwd[12])
check("JFM fills January, February and March, spread 2c a month around its +19 (17 / 19 / 21)", (fwd[1], fwd[2], fwd[3]) == (17.0, 19.0, 21.0), (fwd[1], fwd[2], fwd[3]))
check("AMJJ fills April to July (17 / 19 / 21 / 23)", (fwd[4], fwd[5], fwd[6], fwd[7]) == (17.0, 19.0, 21.0, 23.0), (fwd[4], fwd[5], fwd[6], fwd[7]))
check("the Nov-Jul package fills nothing: every month it names has a bid of its own or a narrower package", sorted(fwd) == [1, 2, 3, 4, 5, 6, 7, 10, 11, 12], sorted(fwd))
only_pkg = fm.rail_forward([("Nov-Jul", 22)])
check("alone, Nov-Jul spreads over its nine months around +22 (Nov 14 ... Mar 22 ... Jul 30)",
      sorted(only_pkg) == [1, 2, 3, 4, 5, 6, 7, 11, 12] and only_pkg[11] == 14.0 and only_pkg[3] == 22.0 and only_pkg[7] == 30.0, only_pkg)
check("halves with no full bid: the midpoint, and a package behind them does not move it",
      fm.rail_forward([("FH Nov", -20), ("LH Nov", -15), ("OND", 50)])[11] == -17.5)
check("a lone FH stands for its month, and the package behind it fills the others",
      fm.rail_forward([("FH Dec", 13), ("OND", 50)])[12] == 13.0 and fm.rail_forward([("FH Dec", 13), ("OND", 50)])[10] == 48.0)
check("the narrowest package wins a month (Jan from JFM, not from the wider Jan-Jul)",
      fm.rail_forward([("JFM", 5), ("Jan-Jul", 40)])[1] == 3.0 and fm.rail_forward([("JFM", 5), ("Jan-Jul", 40)])[7] > 40.0)
two_am = fm.rail_forward([("AM", 1), ("AM", 3)])
check("of two equal packages the higher bid wins (AM +3 spread 2 / 4, not AM +1)", two_am[4] == 2.0 and two_am[5] == 4.0, two_am)
check("a bid of None, 'Spot' and 'Return Trip' add nothing", fm.rail_forward([("Dec", None), ("Spot", 5), ("Return Trip", 100)]) == {})

# ── the emailed chart's curve (rail_report._fwd_curve) ────────────────────────────────────────────────
print("the emailed chart's forward curve")
import rail_report  # noqa: E402


class Day(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 20)


def cells(rows, d="2026-10-20", market="CSX Columbus"):
    by_md = {(market, d): {p: {"period": p, "bid": b} for p, b in rows}}
    return by_md, {market: {d}}


by_md, md = cells(CSX)
with mock.patch.object(rail_report, "date", Day):
    curve = rail_report._fwd_curve("CSX Columbus", by_md, md)
pts = {int(w): float(b) for w, b in zip(curve["MktWeek"], curve["Bid"])}
WK = rail_report._MON_WK
check("the curve starts in November (October's week has passed on Oct 20)", min(pts) == WK["Nov"], pts)
check("November reads +19 and December +16, as on the board", pts[WK["Nov"]] == 19.0 and pts[WK["Dec"]] == 16.0, pts)
check("January to July are the JFM / AMJJ spread (17 19 21 17 19 21 23)", [pts[WK[m]] for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul")] == [17.0, 19.0, 21.0, 17.0, 19.0, 21.0, 23.0], pts)
check("nine points, one per month Nov-Jul", len(pts) == 9, pts)
by_md2, md2 = cells([("Dec", 16)])
with mock.patch.object(rail_report, "date", Day):
    check("fewer than two points is no curve", rail_report._fwd_curve("CSX Columbus", by_md2, md2) is None)
by_md3, md3 = cells([("OND", 10), ("Return Trip", 100), ("ONDJFM", 50), ("Dec", 4)])
with mock.patch.object(rail_report, "date", Day):
    c3 = rail_report._fwd_curve("CSX Columbus", by_md3, md3)
check("a package the chart never tracked (ONDJFM) and a label with no month stay off the curve (Nov from OND = 10, Dec its own 4)",
      c3 is not None and len(c3) == 2 and sorted(float(b) for b in c3["Bid"]) == [4.0, 10.0], str(c3))

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("all forward_month checks passed")
