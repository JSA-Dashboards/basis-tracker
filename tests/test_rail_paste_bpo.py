"""A rail rundown written one quote per line, "bid PERIOD offer", is read by the parser.

    python tests/test_rail_paste_bpo.py

Kolten 2026-11-09 pasted the CSX Columbus and CSX Evansville sheet with the bid on the LEFT, the delivery period in the MIDDLE and the offer on
the RIGHT ("30z Nov 40z", "25z Nov 1-20 ?"). The parser had only read "PERIOD bid/offer" cells: it put "OH" (from the header "Columbus, OH - CSX")
in a period, slid every value onto the wrong row and merged "Nov 1-20" into "Nov" (its date-window strip), so two rows would have shared a key.
rail_paste._bpo_matches decides the layout per corridor block and _parse_bpo reads it; everything else keeps the original reading. Pure functions:
nothing is written and nothing touches the network.
"""
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["USE_SNOWFLAKE"] = ""          # post_rail imports database: keep it off Snowflake

import post_rail as pr         # noqa: E402
import rail_paste as rp        # noqa: E402

FAILS = []
D = date(2026, 11, 9)


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:600]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def tab(rows):
    """(period, futures, bid, offer, bid_raw, offer_raw) per row."""
    return [(r["period"], r["futures"], r["bid"], r["offer"], r["bid_raw"], r["offer_raw"]) for r in rows]


def parse(text, corridor="CSX Columbus", as_of=D, commodity="Corn"):
    return rp.parse_rundown(text, corridor, as_of, commodity)


SHEET = """Columbus, OH - CSX

25z Nov 1-20 ?
30z Nov 40z
17z FH Dec 26z
24z Dec ?
24h JFM 29h
25r AMJJ 32r
25r Jan - Jul 31r

Evansville - CSX

32z FH Nov ?
35z Nov 45z
17z FH Dec 26z
25z Dec 31z
24h JFM 30h
25r AMJJ 32r
"""
EXPECT = {
    "CSX Columbus": [("Nov 1-20", "ZCZ26", 25, None, None, "?"), ("Nov", "ZCZ26", 30, 40, None, None),
                     ("FH Dec", "ZCZ26", 17, 26, None, None), ("Dec", "ZCZ26", 24, None, None, "?"),
                     ("JFM", "ZCH27", 24, 29, None, None), ("AMJJ", "R", 25, 32, None, None), ("Jan-Jul", "R", 25, 31, None, None)],
    "CSX Evansville": [("FH Nov", "ZCZ26", 32, None, None, "?"), ("Nov", "ZCZ26", 35, 45, None, None),
                       ("FH Dec", "ZCZ26", 17, 26, None, None), ("Dec", "ZCZ26", 25, 31, None, None),
                       ("JFM", "ZCH27", 24, 30, None, None), ("AMJJ", "R", 25, 32, None, None)],
}

print("the CSX sheet as pasted on 2026-11-09 (two corridors, bid | period | offer)")
rows, warns = rp.parse_multi(SHEET, as_of=D)
by = {}
for r in rows:
    by.setdefault(r["market"], []).append(r)
check("both corridors are found, in order", list(by) == ["CSX Columbus", "CSX Evansville"], list(by))
check("Columbus: every period, contract, bid and offer as posted", tab(by.get("CSX Columbus", [])) == EXPECT["CSX Columbus"], tab(by.get("CSX Columbus", [])))
check("Evansville: every period, contract, bid and offer as posted", tab(by.get("CSX Evansville", [])) == EXPECT["CSX Evansville"], tab(by.get("CSX Evansville", [])))
check("no warnings", warns == [], warns)
check("period_order follows the listed order in each corridor", all([r["period_order"] for r in v] == list(range(len(v))) for v in by.values()))
check("the header's 'OH' and 'CSX' never reach a period", not any(w in r["period"].upper().split() for r in rows for w in ("OH", "CSX", "COLUMBUS", "EVANSVILLE")))
check("'Nov 1-20' keeps its window and stays apart from 'Nov' (one key each)",
      [r["period"] for r in by["CSX Columbus"]][:2] == ["Nov 1-20", "Nov"] and len({r["period"] for r in by["CSX Columbus"]}) == 7)
check("'Jan - Jul' reads as the package label already on the board ('Jan-Jul')", "Jan-Jul" in [r["period"] for r in by["CSX Columbus"]])
check("a pending offer is kept as '?' (None + raw), a real one is a number", by["CSX Columbus"][0]["offer"] is None and by["CSX Columbus"][0]["offer_raw"] == "?"
      and by["CSX Columbus"][1]["offer"] == 40 and by["CSX Columbus"][1]["offer_raw"] is None)
check("every row is a CSX corn row", all(r["rail"] == "CSX" and r["commodity"] == "Corn" for r in rows))

print("\nthe rows post_rail saves from it")
built = pr.build_corridor_rows(rows, "Corn")
check("same corridors, same cells", {k: [(x["period"], x["futures"], x["bid"], x["offer"]) for x in v] for k, v in built.items()}
      == {k: [(t[0], t[1], t[2], t[3]) for t in v] for k, v in EXPECT.items()})
check("'?' survives as the raw flag on the right side only", [(x["bid_raw"], x["offer_raw"]) for x in built["CSX Columbus"]]
      == [(None, "?"), (None, None), (None, None), (None, "?"), (None, None), (None, None), (None, None)])
check("period_order 0..n-1 and rail CSX", all([x["period_order"] for x in v] == list(range(len(v))) and all(x["rail"] == "CSX" for x in v) for v in built.values()))

print("\nwhich blocks count as this layout")
two = "25z Nov 40z\n24h JFM 29h"
check("two quote lines: yes", rp._bpo_matches(two) is not None and len(rp._bpo_matches(two)[0]) == 2)
check("a header remnant with no number is ignored", rp._bpo_matches(", OH - CSX\n\n" + two) is not None and rp._bpo_matches(", OH - CSX\n\n" + two)[1] == [])
check("one quote line alone is not enough", rp._bpo_matches("25z Nov 40z") is None)
check("the original grammar never matches (it starts with a period)", rp._bpo_matches("Nov 4/10  Dec 8/14z  JFM 12/18") is None and rp._bpo_matches("Spot 10/15") is None
      and rp._bpo_matches("Nov 4/10\nDec 8/14z\nJFM 12/18") is None)
check("two quote lines among three numbered lines is a mix: the original reading", rp._bpo_matches("25 Nov 30\n24 Dec 29\nJFM 12/18") is None)
found = rp._bpo_matches("25 Nov 30\n24 Dec 29\n23 Jan 28\nJFM 12/18")
check("three of four is this layout, the odd line is reported", found is not None and len(found[0]) == 3 and found[1] == ["JFM 12/18"], found)
rws, w = parse("25 Nov 30\n24 Dec 29\n23 Jan 28\nJFM 12/18")
check("... and the parse says it skipped that line", len(rws) == 3 and len(w) == 1 and "JFM 12/18" in w[0], (len(rws), w))

print("\ntags, values and notes")
rws, w = parse("25z Dec ?\n? Dec 26z\n24h Dec 29z")
check("the bid's tag picks the contract; with no tag on the bid, the offer's does",
      [r["futures"] for r in rws] == ["ZCZ26", "ZCZ26", "ZCH27"], [r["futures"] for r in rws])
check("two different tags: a warning, and the bid's tag wins", len(w) == 1 and "bid is tagged h and the offer z" in w[0], w)
rws, _ = parse("25 Dec 30\n25z Dec 30")
check("untagged December is vs CH, tagged z is vs CZ (the desk's rule holds here too)", [r["futures"] for r in rws] == ["ZCH27", "ZCZ26"], [r["futures"] for r in rws])
rws, _ = parse("25z AMJJ 32z\n25 Jan-Jul 31")
check("a tag never collapses a spanning package: it stays R", [r["futures"] for r in rws] == ["R", "R"], [r["futures"] for r in rws])
rws, _ = parse("-5 Nov flat\n3 Dec 9")
check("negatives are real and 'flat' is 0", (rws[0]["bid"], rws[0]["offer"]) == (-5, 0) and (rws[1]["bid"], rws[1]["offer"]) == (3, 9), tab(rws))
rws, _ = parse("? Nov ?\n27? Dec 30")
check("a bare '?' on both sides is a pending row; a number plus '?' keeps the number",
      (rws[0]["bid"], rws[0]["offer"], rws[0]["bid_raw"], rws[0]["offer_raw"]) == (None, None, "?", "?") and (rws[1]["bid"], rws[1]["bid_raw"]) == (27, None), tab(rws))
rws, _ = parse("* Nov 1-20 *\n30z Nov 40z\n24h JFM 29h")
check("a quote with '*' on both sides is no row, and it does not use up a period_order slot",
      [r["period"] for r in rws] == ["Nov", "JFM"] and [r["period_order"] for r in rws] == [0, 1], [(r["period"], r["period_order"]) for r in rws])
rws, _ = parse("25r Jan – Jul 31r\n24 Nov 30")
check("an en dash in 'Jan – Jul' reads as 'Jan-Jul'", rws[0]["period"] == "Jan-Jul", tab(rws))
rws, _ = parse("25pk Nov 30wtx\n31 Dec 40")
check("desk notes abutting a number are dropped (25pk / 30wtx)", (rws[0]["period"], rws[0]["bid"], rws[0]["offer"]) == ("Nov", 25, 30), tab(rws))
rws, _ = parse("  25z\tNov\t40z  \r\n\r\n24h JFM 29h\r\n")
check("tabs, spaces, blank lines and Windows line endings do not matter", tab(rws) == [("Nov", "ZCZ26", 25, 40, None, None), ("JFM", "ZCH27", 24, 29, None, None)], tab(rws))

print("\nrouting")
rws, _ = parse("20 Nov 30\n25 Jan 35", corridor="CN 105s", commodity="Soybeans")
check("beans on a dual corridor go to their own card", {r["market"] for r in rws} == {"CN 105s Beans"} and {r["commodity"] for r in rws} == {"Soybeans"}, tab(rws))
rws, _ = parse("10 Nov mex 20\n5 Nov dom 12", corridor="UP Illinois (Dom)")
check("UP Illinois: a mex / dom note in the period routes the row to its own market", [r["market"] for r in rws] == ["UP Illinois (Mex)", "UP Illinois (Dom)"] and [r["period"] for r in rws] == ["Nov", "Nov"],
      [(r["market"], r["period"]) for r in rws])

print("\nthe original layout is untouched")
rws, _ = parse("Sep -5/+3  Oct 0/6  Nov 4/10  Dec 8/14z  JFM 12/18  AMJJ 17/25", corridor="NS Ft Wayne", as_of=date(2026, 8, 12))
check("'PERIOD bid/offer' cells read as they always did", [(r["period"], r["bid"], r["offer"]) for r in rws]
      == [("Sep", -5, 3), ("Oct", 0, 6), ("Nov", 4, 10), ("Dec", 8, 14), ("JFM", 12, 18), ("AMJJ", 17, 25)] and rws[3]["futures"] == "ZCZ26", tab(rws))
rws, _ = parse("Sep 25-30 90/*  Oct 80/85", corridor="NS Ft Wayne", as_of=date(2026, 8, 12))
check("there a day window is still stripped from the label (Sep 25-30 -> Sep, bid 90 not 25)", [(r["period"], r["bid"], r["offer"]) for r in rws] == [("Sep", 90, None), ("Oct", 80, 85)], tab(rws))
rws, _ = parse("Nov 4/10\nDec 8/14z\nJFM 12/18")
check("one 'PERIOD bid/offer' cell per line is still that layout", [(r["period"], r["bid"], r["offer"]) for r in rws] == [("Nov", 4, 10), ("Dec", 8, 14), ("JFM", 12, 18)], tab(rws))
rws, _ = rp.parse_multi("NS Ft Wayne  Sep -5/+3  Oct 0/6\nCol  Sep 2/9z  Oct 5/12\nEville  FH Sep -6/0z  Oct 3/9", as_of=date(2026, 8, 12))
check("a multi-corridor paste in the old layout still splits and parses", [(r["market"], r["period"], r["bid"]) for r in rws]
      == [("NS Ft Wayne", "Sep", -5), ("NS Ft Wayne", "Oct", 0), ("CSX Columbus", "Sep", 2), ("CSX Columbus", "Oct", 5), ("CSX Evansville", "FH Sep", -6), ("CSX Evansville", "Oct", 3)],
      [(r["market"], r["period"], r["bid"]) for r in rws])

print("\ntwo layouts in one paste: each corridor block is judged on its own")
mixed = "NS Ft Wayne  Sep -5/+3  Oct 0/6  Nov 4/10\n\nColumbus, OH - CSX\n25z Nov 1-20 ?\n30z Nov 40z\n"
rws, w = rp.parse_multi(mixed, as_of=D)
check("Ft Wayne in the old layout, Columbus in the new", [(r["market"], r["period"], r["bid"], r["offer"]) for r in rws]
      == [("NS Ft Wayne", "Sep", -5, 3), ("NS Ft Wayne", "Oct", 0, 6), ("NS Ft Wayne", "Nov", 4, 10), ("CSX Columbus", "Nov 1-20", 25, None), ("CSX Columbus", "Nov", 30, 40)]
      and w == [], ([(r["market"], r["period"], r["bid"], r["offer"]) for r in rws], w))

if FAILS:
    print("\n%d FAILED:" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nall bid-period-offer checks passed")
