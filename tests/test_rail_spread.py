"""A rail corridor on one side of the Spread tab's A − B spread: which corridors are offered, the spot series, the forward curve by delivery
month, and the forward spread between two sides — priced in cash, so a corridor that quotes December off March lines up with an elevator
that quotes it off December.

    python tests/test_rail_spread.py

Pure functions over plain rows: nothing here touches a database.
"""
import os
import sys
from collections import namedtuple
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rail_spread as rs  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def row(d, market, period, bid, fut="ZCZ26", order=0, commodity="Corn", offer=None):
    return {"date": d, "market": market, "commodity": commodity, "period": period, "period_order": order,
            "futures": fut, "bid": bid, "offer": offer}


# ── which corridors are offered ───────────────────────────────────────────────────────────────────────
print("the pick-list")
ROWS = []
for d in ("2026-09-02", "2026-09-09", "2026-09-16"):
    ROWS += [row(d, "UP Illinois (Dom)", "Spot", -20), row(d, "CSX Columbus", "Spot", -10), row(d, "UP Group 3", "Spot", -30),
             row(d, "BN Hereford", "Spot", 120), row(d, "ZZ New Corridor", "Spot", 5),
             row(d, "CSX Freight", "Return Trip", 3000, fut=None, commodity="Freight"), row(d, "CSX Freight", "Spot", 250, fut=None),
             row(d, "BN Freight", "Spot", 200, fut=None), row(d, "UP 110 Shuttle", "Spot", 150, fut=None),
             row(d, "BN COBO Buyers", "Sep", 40), row(d, "COL, OH Corn 90's", "SEPTEMBER", -8, fut="CU"),
             row(d, "COL, OH Beans 90's", "SEPTEMBER", 30, fut="SX", commodity="Soybeans"),
             row(d, "UP Illinois (Mex)", "Spot", None)]          # no bid at all
ROWS += [row("2026-09-16", "One Date Only", "Spot", 1)]
ROWS += [row("2026-09-02", "CN 105s", "Spot", 5), row("2026-09-09", "CN 105s", "Spot", 6),
         row("2026-09-02", "CN 105s", "Spot", 50, commodity="Soybeans")]              # beans on one date only
opts = rs.options(ROWS)
byname = {o["market"]: o for o in opts}
check("UP Illinois (Dom) is offered as 'UP Allen Station (Dom)' with the rail prefix",
      byname["UP Illinois (Dom)"]["label"] == "🚂 Rail · UP Allen Station (Dom)", byname.get("UP Illinois (Dom)"))
check("freight rates and shuttle lines are not (they are $/car, not a basis)",
      not any(rs.is_freight(m) for m in byname) and "CSX Freight" not in byname and "UP 110 Shuttle" not in byname, list(byname))
check("the dead side-market and the Palmetto corn markets are left off, as on the Rail FOB board",
      "BN COBO Buyers" not in byname and "COL, OH Corn 90's" not in byname, list(byname))
check("the Palmetto beans line is offered as 'CSX Columbus Beans', soybeans only",
      byname["COL, OH Beans 90's"]["label"] == "🚂 Rail · CSX Columbus Beans" and byname["COL, OH Beans 90's"]["commodities"] == ["Soybeans"])
check("a corridor with no bids, or a single posting date, is not offered", "One Date Only" not in byname and "UP Illinois (Mex)" not in byname, list(byname))
check("a commodity posted on one date only is not listed for a corridor that has another", byname["CN 105s"]["commodities"] == ["Corn"], byname["CN 105s"])
order = [o["market"] for o in opts]
check("the order is the Rail FOB board's (CSX, UP, BN ...), an unknown corridor last",
      order.index("CSX Columbus") < order.index("UP Group 3") < order.index("UP Illinois (Dom)") < order.index("BN Hereford") < order.index("ZZ New Corridor"), order)
check("is_freight reads the commodity too", rs.is_freight("CSX Freight", "Freight") and rs.is_freight("Anything", "Freight") and not rs.is_freight("CSX Columbus", "Corn"))
check("plain() drops the emoji for a title or a file name", rs.plain(rs.label("BN PNW")) == "Rail · BN PNW" and rs.plain("ADM · Decatur") == "ADM · Decatur")
check("CP PNW keeps the Rail FOB board's name", rs.label("BN PNW CP") == "🚂 Rail · CP PNW")

# ── the spot series ───────────────────────────────────────────────────────────────────────────────────
print("the spot series")
SP = [row("2026-07-29", "UP Illinois (Dom)", "Spot", -22, fut="ZCU26"),
      row("2026-07-29", "UP Illinois (Dom)", "Aug", -30, fut="ZCU26", order=0),            # the Spot row wins on its date
      row("2026-10-06", "UP Illinois (Dom)", "Oct", -20, order=0), row("2026-10-06", "UP Illinois (Dom)", "Nov", -9, order=1),
      row("2026-10-08", "UP Illinois (Dom)", "Oct", None, order=0, offer=0), row("2026-10-08", "UP Illinois (Dom)", "OND", -10, order=1),
      row("2026-10-08", "UP Illinois (Dom)", "Nov", -7, order=2),
      row("2026-10-08", "UP Illinois (Dom)", "Spot", 99, commodity="Soybeans"),                # another commodity: not this series
      row("2026-10-08", "UP Group 3", "Spot", -33)]                                           # another corridor
ser = rs.spot_series(SP, "UP Illinois (Dom)", "Corn")
check("one point per posting date, oldest first, as floats", ser == [(date(2026, 7, 29), -22.0), (date(2026, 10, 6), -20.0), (date(2026, 10, 8), -10.0)], ser)
check("a live posting with no Spot row reads its first period that has a bid", ser[-1] == (date(2026, 10, 8), -10.0), ser)
check("other corridors and other commodities are not mixed in", len(ser) == 3 and rs.spot_series(SP, "UP Illinois (Dom)", "Soybeans") == [(date(2026, 10, 8), 99.0)])
check("an unknown corridor has no series", rs.spot_series(SP, "Nowhere", "Corn") == [])
check("latest_posting is the newest date with a bid", rs.latest_posting(SP, "UP Illinois (Dom)", "Corn") == date(2026, 10, 8) and rs.latest_posting(SP, "Nowhere") is None)

# ── naming the contract ───────────────────────────────────────────────────────────────────────────────
print("the contract a tag names")
P = date(2026, 10, 8)
check("a full symbol is kept (any case)", rs.resolve_tag("ZCH27", P) == "ZCH27" and rs.resolve_tag(" zcz26 ", P) == "ZCZ26")
check("a short corn code takes the next such contract (CZ -> Dec 2026, CU is past its notice day -> Sep 2027)",
      rs.resolve_tag("CZ", P) == "ZCZ26" and rs.resolve_tag("CU", P) == "ZCU27", (rs.resolve_tag("CZ", P), rs.resolve_tag("CU", P)))
check("a short soybean code too (SX -> Nov 2026, SF -> Jan 2027)", rs.resolve_tag("SX", P) == "ZSX26" and rs.resolve_tag("SF", P) == "ZSF27",
      (rs.resolve_tag("SX", P), rs.resolve_tag("SF", P)))
check("'R' (the respective option month), a blank and nonsense name no contract", all(rs.resolve_tag(t, P) is None for t in ("R", "", None, "XYZ", "k")))

# ── the forward curve of a corridor ───────────────────────────────────────────────────────────────────
print("a corridor's forward curve by delivery month")
D = "2026-10-08"
ALLEN = [row("2026-10-06", "UP Illinois (Dom)", "Oct", -99, order=0),                     # an older posting: ignored
         row(D, "UP Illinois (Dom)", "Oct", -15, order=0, offer=0), row(D, "UP Illinois (Dom)", "OND", -10, order=1),
         row(D, "UP Illinois (Dom)", "FH Nov", None, order=2, offer=10), row(D, "UP Illinois (Dom)", "Nov", -7, order=3),
         row(D, "UP Illinois (Dom)", "FH Dec", None, fut="ZCH27", order=4, offer=20), row(D, "UP Illinois (Dom)", "Dec", 2, fut="ZCH27", order=5),
         row(D, "UP Illinois (Dom)", "JFM", 2, fut="ZCH27", order=6), row(D, "UP Illinois (Dom)", "Jan-July", None, fut="R", order=7, offer=15)]
posted, q = rs.forward_quotes(ALLEN, "UP Illinois (Dom)", "Corn")
got = {x["ym"]: (x["basis"], x["tag"]) for x in q}
check("the latest posting date is reported", posted == date(2026, 10, 8), posted)
check("a month's own bid is used (Oct, Nov, Dec) and the OND package does not override them",
      got[(2026, 10)] == (-15.0, "ZCZ26") and got[(2026, 11)] == (-7.0, "ZCZ26") and got[(2026, 12)] == (2.0, "ZCH27"), got)
check("JFM fills January, February and March, quoted off March", all(got[(2027, m)] == (2.0, "ZCH27") for m in (1, 2, 3)), got)
check("nothing else: no bid (offer-only cells), no 'R' package, no older posting", sorted(got) == [(2026, 10), (2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)], sorted(got))

R2 = [row(D, "X", "Oct", -10, order=0), row(D, "X", "OND", 50, order=1),                    # an own bid beats a (better) package
      row(D, "X", "JFM", 5, fut="ZCH27", order=2), row(D, "X", "FM", 8, fut="ZCH27", order=3),           # the narrower package wins its months
      row(D, "X", "Jan-Jul", 40, fut="ZCH27", order=4),                                       # tagged here, so it counts — but it is the widest
      row(D, "X", "AM", 1, fut="ZCK27", order=5), row(D, "X", "AM", 3, fut="ZCK27", order=6)]   # equals: the higher bid
_, q2 = rs.forward_quotes(R2, "X", "Corn")
g2 = {}
for x in q2:
    g2.setdefault(x["ym"], []).append(x["basis"])
check("an own bid beats a package that pays more", g2[(2026, 10)] == [-10.0], g2)
check("OND still fills the months it names that have no bid of their own", g2[(2026, 11)] == [50.0] and g2[(2026, 12)] == [50.0], g2)
check("the narrowest package wins: Jan from JFM, Feb and Mar from FM", g2[(2027, 1)] == [5.0] and g2[(2027, 2)] == [8.0] and g2[(2027, 3)] == [8.0], g2)
check("of two equal packages the higher bid wins", g2[(2027, 4)] == [3.0] and g2[(2027, 5)] == [3.0], g2)
check("the widest package fills what nothing narrower names (June, July)", g2[(2027, 6)] == [40.0] and g2[(2027, 7)] == [40.0], g2)

R3 = [row(D, "Y", "FH Nov", -20, order=0), row(D, "Y", "LH Nov", -15, order=1), row(D, "Y", "Split Nov", -17, order=2), row(D, "Y", "Dec 1-20", 4, fut="ZCH27", order=3),
      row(D, "Y", "Dec 2027", 9, fut="ZCH28", order=4), row(D, "Y", "Mar 2027", 6, fut="ZCH27", order=5), row(D, "Y", "NDJF", 7, order=6)]
_, q3 = rs.forward_quotes(R3, "Y", "Corn")
nov = [x["basis"] for x in q3 if x["ym"] == (2026, 11)]
check("every own quote of a month is kept (forward_spread reads one value from them, in cash)", sorted(nov) == [-20.0, -17.0, -15.0], nov)
check("each carries its kind: FH Nov 'fh', LH Nov 'lh', Split Nov a whole-month bid ('full')",
      sorted((x["label"], x["kind"]) for x in q3 if x["ym"] == (2026, 11)) == [("FH Nov", "fh"), ("LH Nov", "lh"), ("Split Nov", "full")], q3)
check("LP / FP / MP read as the seasonal chart reads them: last half, first half, a partial window",
      {x["label"]: x["kind"] for x in rs.forward_quotes([row(D, "Q", "LP Nov", 1, order=0), row(D, "Q", "FP Dec", 2, order=1), row(D, "Q", "MP Jan", 3, order=2)], "Q", "Corn")[1]}
      == {"LP Nov": "lh", "FP Dec": "fh", "MP Jan": "window"})
check("a day window ('Dec 1-20') is its month's own bid", any(x["ym"] == (2026, 12) and x["basis"] == 4.0 for x in q3), q3)
check("an explicit year is honoured ('Dec 2027', 'Mar 2027'); a bare month is the next one from the posting date",
      any(x["ym"] == (2027, 12) and x["basis"] == 9.0 for x in q3) and any(x["ym"] == (2027, 3) and x["basis"] == 6.0 for x in q3), q3)
check("a package that wraps the year (NDJF) is Nov, Dec, Jan, Feb — and fills only what has no bid of its own",
      any(x["ym"] == (2027, 1) and x["basis"] == 7.0 for x in q3) and any(x["ym"] == (2027, 2) and x["basis"] == 7.0 for x in q3)
      and not any(x["ym"] == (2026, 11) and x["basis"] == 7.0 for x in q3), q3)

R4 = [row(D, "Z", "Spot", -5, order=0), row(D, "Z", "Return Trip", 100, fut=None, order=1), row(D, "Z", "No-Holiday", 3, fut=None, order=2),
      row(D, "Z", "Nov", -9, fut="R", order=3), row(D, "Z", "Dec", 1, fut=None, order=4), row(D, "Z", "OND", 2, fut="CZ", order=5)]
_, q4 = rs.forward_quotes(R4, "Z", "Corn")
check("labels that name no month are not months, and a quote with no nameable contract is left out",
      [(x["ym"], x["basis"], x["tag"]) for x in q4 if x["ym"] in ((2026, 11), (2026, 12))] == [((2026, 11), 2.0, "ZCZ26"), ((2026, 12), 2.0, "ZCZ26")]
      and not any(x["label"] in ("Spot", "Return Trip", "No-Holiday") for x in q4), q4)
check("a short code on a package is resolved (OND tagged 'CZ' is quoted off ZCZ26)", any(x["label"] == "OND" and x["tag"] == "ZCZ26" for x in q4), q4)
check("an unknown corridor has no curve", rs.forward_quotes(ALLEN, "Nowhere", "Corn") == (None, []))
beans = [row(D, "COL, OH Beans 90's", "OCTOBER", 30, fut="SX", commodity="Soybeans"), row(D, "COL, OH Beans 90's", "DECEMBER", 40, fut="SF", commodity="Soybeans"),
         row(D, "COL, OH Beans 90's", "OCT/NOV", 35, fut="SX", commodity="Soybeans")]
_, qb = rs.forward_quotes(beans, "COL, OH Beans 90's", "Soybeans")
gb = {x["ym"]: (x["basis"], x["tag"]) for x in qb}
check("the Palmetto soybean line's all-caps months and short codes resolve (OCTOBER/SX -> ZSX26, DECEMBER/SF -> ZSF27 for Dec 2026)",
      gb[(2026, 10)] == (30.0, "ZSX26") and gb[(2026, 12)][1] == "ZSF27" and gb[(2026, 11)] == (35.0, "ZSX26"), gb)

# ── a basis location's forward rows ───────────────────────────────────────────────────────────────────
print("a basis location's forward rows")
SnapRow = namedtuple("SnapRow", "isSpot basisCents futuresSymbol grain deliveryMonth")
snap = [SnapRow(False, -20, "ZCZ26", "Corn", "FH November"), SnapRow(False, -15, "ZCZ26", "Corn", "LH November"), SnapRow(False, 0, "ZCZ26", "Corn", "Dec 26"),
        SnapRow(False, -6, "ZCH27", "Corn", "February 2027"), SnapRow(False, -38, "ZCZ26", "Corn", "October"),
        SnapRow(True, -40, "ZCZ26", "Corn", "Spot"), SnapRow(False, None, "ZCZ26", "Corn", "Nov"), SnapRow(False, 5, None, "Corn", "Jan"),
        SnapRow(False, 9, "ZSX26", "Soybeans", "Nov")]
sq = rs.snapshot_quotes(snap, "Corn", lambda g: g)
check("forward rows only: not the spot row, nothing without a basis or a futures symbol, nothing of another grain",
      sorted((x["ym"], x["basis"]) for x in sq) == [((2026, 10), -38.0), ((2026, 11), -20.0), ((2026, 11), -15.0), ((2026, 12), 0.0), ((2027, 2), -6.0)], sq)
check("each keeps the contract it is quoted off", {x["tag"] for x in sq} == {"ZCZ26", "ZCH27"})
check("each carries its kind (FH November 'fh', LH November 'lh', a whole month 'full')",
      {x["label"]: x["kind"] for x in sq} == {"FH November": "fh", "LH November": "lh", "Dec 26": "full", "February 2027": "full", "October": "full"}, sq)

# ── the forward spread, in cash ───────────────────────────────────────────────────────────────────────
print("the forward spread")
CURVE = {"ZCZ26": 500.25, "ZCH27": 515.25, "ZCK27": 522.25}
elev = [{"ym": (2026, 10), "basis": -38.0, "tag": "ZCZ26"}, {"ym": (2026, 11), "basis": -20.0, "tag": "ZCZ26"}, {"ym": (2026, 11), "basis": -15.0, "tag": "ZCZ26"},
        {"ym": (2026, 12), "basis": 0.0, "tag": "ZCZ26"}, {"ym": (2027, 1), "basis": -12.0, "tag": "ZCH27"}, {"ym": (2027, 4), "basis": -8.0, "tag": "ZCK27"}]
rail = [{"ym": (2026, 10), "basis": -15.0, "tag": "ZCZ26"}, {"ym": (2026, 11), "basis": -7.0, "tag": "ZCZ26"}, {"ym": (2026, 12), "basis": 2.0, "tag": "ZCH27"},
        {"ym": (2027, 1), "basis": 2.0, "tag": "ZCH27"}, {"ym": (2027, 2), "basis": 2.0, "tag": "ZCH27"}]
fs = {p["ym"]: p for p in rs.forward_spread(elev, rail, CURVE)}
check("the months both sides quote, no others (Feb and Apr have one side only)", sorted(fs) == [(2026, 10), (2026, 11), (2026, 12), (2027, 1)], sorted(fs))
check("same contract: the spread is just the difference of the two bases (Oct -38 - -15 = -23)", fs[(2026, 10)]["Spread"] == -23.0, fs[(2026, 10)])
check("halves are averaged (Nov -17.5 against -7 = -10.5)", fs[(2026, 11)]["Spread"] == -10.5, fs[(2026, 11)])
check("December: the corridor's +2 is off March, the elevator's 0 off December — in cash that is -17, not the raw -2",
      fs[(2026, 12)]["Spread"] == -17.0 and (0 - 2) != fs[(2026, 12)]["Spread"], fs[(2026, 12)])
check("January, both off March: -12 against +2 = -14", fs[(2027, 1)]["Spread"] == -14.0, fs[(2027, 1)])
check("each point carries its marketing year and week (Oct 2026: year 2026 week 5; Jan 2027: year 2026 week 18)",
      (fs[(2026, 10)]["MktYearNum"], fs[(2026, 10)]["MktWeek"]) == (2026, 5) and (fs[(2027, 1)]["MktYearNum"], fs[(2027, 1)]["MktWeek"]) == (2026, 18), (fs[(2026, 10)], fs[(2027, 1)]))
check("a month quoted off one contract by both sides does not depend on that contract's price",
      all(p["Spread"] == q["Spread"] for p, q in zip(rs.forward_spread(elev[:1], rail[:1], {"ZCZ26": 100.0}), rs.forward_spread(elev[:1], rail[:1], {"ZCZ26": 900.0}))))
check("a contract the curve cannot price drops its month instead of guessing it", [p["ym"] for p in rs.forward_spread(elev, rail, {"ZCZ26": 500.25, "ZCH27": 515.25})] == [(2026, 10), (2026, 11), (2026, 12), (2027, 1)]
      and (2026, 12) not in {p["ym"] for p in rs.forward_spread(elev, rail, {"ZCZ26": 500.25})})
check("no curve, no forward spread", rs.forward_spread(elev, rail, {}) == [] and rs.forward_spread(elev, rail, None) == [])
check("spreads are rounded to a tenth of a cent", rs.forward_spread([{"ym": (2026, 10), "basis": 1.0, "tag": "ZCZ26"}], [{"ym": (2026, 10), "basis": 0.0, "tag": "ZCH27"}],
                                                                  {"ZCZ26": 500.0, "ZCH27": 500.333})[0]["Spread"] == 0.7)

# a month is ONE value per side (forward_month, Kolten 2026-10-20: "for months that show a full month but also a FH or LH just default to the
# full month indication, only take the midpoint if its a FH and LH separate bids")
print("one value per month: the full-month bid, else the midpoint of FH and LH")
fa = [{"ym": (2026, 12), "basis": 16.0, "tag": "ZCH27", "kind": "full"}, {"ym": (2026, 12), "basis": 13.0, "tag": "ZCH27", "kind": "fh"},
      {"ym": (2026, 11), "basis": -20.0, "tag": "ZCZ26", "kind": "fh"}, {"ym": (2026, 11), "basis": -15.0, "tag": "ZCZ26", "kind": "lh"},
      {"ym": (2026, 10), "basis": -12.0, "tag": "ZCZ26", "kind": "fh"}]
fb = [{"ym": (2026, 12), "basis": 0.0, "tag": "ZCZ26"}, {"ym": (2026, 11), "basis": -7.0, "tag": "ZCZ26"}, {"ym": (2026, 10), "basis": -15.0, "tag": "ZCZ26"}]
fk = {p["ym"]: p for p in rs.forward_spread(fa, fb, CURVE)}
check("a full-month bid is the month: Dec +16 and FH Dec +13 (both off March) against an elevator's 0 off December = 31.0 in cash, not the 29.5 of an average",
      fk[(2026, 12)]["Spread"] == 31.0 and fk[(2026, 12)]["A"] == 531.25, fk[(2026, 12)])
check("FH and LH posted separately and no full bid: the midpoint (Nov -17.5 against -7 = -10.5)", fk[(2026, 11)]["Spread"] == -10.5, fk[(2026, 11)])
check("a lone half stands for its month (Oct FH -12 against -15 = +3.0)", fk[(2026, 10)]["Spread"] == 3.0, fk[(2026, 10)])
check("the elevator side follows the same rule (a location's 'Dec 26' beats its 'FH Dec 26')",
      rs.forward_spread([{"ym": (2026, 12), "basis": 0.0, "tag": "ZCZ26", "kind": "full"}, {"ym": (2026, 12), "basis": -30.0, "tag": "ZCZ26", "kind": "fh"}],
                        [{"ym": (2026, 12), "basis": -10.0, "tag": "ZCZ26"}], CURVE)[0]["Spread"] == 10.0)

print("marketing year and week")
ref_year = lambda y, m: y if m >= 9 else y - 1                                                   # noqa: E731 - the Spread tab's own formulas
ref_week = lambda y, m: max(1, min(52, ((date(y, m, 1) - date(ref_year(y, m), 9, 1)).days // 7) + 1))      # noqa: E731
check("market_year / market_week are the Spread tab's formulas for every month of two years",
      all(rs.market_year(y, m) == ref_year(y, m) and rs.market_week(y, m) == ref_week(y, m) for y in (2026, 2027) for m in range(1, 13)))
check("September opens week 1 of its year; the first of August is week 48 (the weeks run 7 days from Sep 1, capped at 52)",
      rs.market_week(2026, 9) == 1 and rs.market_week(2027, 8) == 48, (rs.market_week(2026, 9), rs.market_week(2027, 8)))

print("comparable")
check("curves within ten days are compared, further apart or missing are not",
      rs.comparable(date(2026, 10, 8), date(2026, 10, 9)) and rs.comparable(date(2026, 10, 1), date(2026, 10, 11)) and not rs.comparable(date(2026, 10, 1), date(2026, 10, 12))
      and not rs.comparable(None, date(2026, 10, 1)) and not rs.comparable(date(2026, 10, 1), None))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
