"""A slot named "Spot" on a pasted rail rundown prices vs the NEAREST futures month that is not past first notice.

    python tests/test_rail_paste_spot.py

Kolten 2026-11-05: "On rail parser for the entry if the slot is 'Spot' just use the nearest by futures month so long as its not past first notice day".
Before that a Spot cell came out with no futures at all (a month-less label), so the board could not roll-adjust it. The rule lives in
rail_corridors.corn_front_contract / soy_front_contract (the First Notice Day itself still counts, as in the existing roll) and rail_paste._futures_for
(a cell's own tag letter still wins). Pure functions: nothing is written and nothing touches the network.
"""
import os
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["USE_SNOWFLAKE"] = ""          # post_rail imports database: keep it off Snowflake

import post_rail as pr         # noqa: E402
import rail_corridors as rc    # noqa: E402
import rail_paste as rp        # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def fut(text, corridor="NS Ft Wayne", as_of=date(2026, 11, 5), commodity="Corn"):
    rows, _ = rp.parse_rundown(text, corridor, as_of, commodity)
    return [(r["period"], r["futures"]) for r in rows]


print("the front corn month: the nearest contract whose first notice day (last business day of the month before) has not passed")
CF = rc.corn_front_contract
check("Feb 27 2026 is CH's first notice day: still CH", CF(date(2026, 2, 27)) == "CH", CF(date(2026, 2, 27)))
check("... and the next business day it is CK", CF(date(2026, 3, 2)) == "CK", CF(date(2026, 3, 2)))
check("Apr 30 is CK's last day, May 1 is CN", CF(date(2026, 4, 30)) == "CK" and CF(date(2026, 5, 1)) == "CN")
check("Jun 30 is CN's last day, Jul 1 is CU", CF(date(2026, 6, 30)) == "CN" and CF(date(2026, 7, 1)) == "CU")
check("Aug 31 is CU's last day, Sep 1 is CZ", CF(date(2026, 8, 31)) == "CU" and CF(date(2026, 9, 1)) == "CZ")
check("Nov 5 is CZ", CF(date(2026, 11, 5)) == "CZ")
check("Nov 30 (CZ's first notice day) is still CZ, Dec 1 is CH", CF(date(2026, 11, 30)) == "CZ" and CF(date(2026, 12, 1)) == "CH")
check("the turn of the year stays CH", CF(date(2026, 12, 31)) == "CH" and CF(date(2027, 1, 4)) == "CH")
check("a datetime works like its date", CF(datetime(2026, 11, 5, 9, 30)) == "CZ")
check("no as_of -> None (first notice cannot be judged)", CF(None) is None and CF() is None)

print("\nthe front soybean month")
SF = rc.soy_front_contract
check("Nov 5 2026: SX is past first notice (Oct 30), so SF", SF(date(2026, 11, 5)) == "SF", SF(date(2026, 11, 5)))
check("Oct 30 is SX's first notice day: still SX", SF(date(2026, 10, 30)) == "SX")
check("Dec 31 is SF's first notice day: still SF; Jan 4 is SH", SF(date(2026, 12, 31)) == "SF" and SF(date(2027, 1, 4)) == "SH")
check("Jul 31 is SQ's first notice day, Aug 3 is SU", SF(date(2026, 7, 31)) == "SQ" and SF(date(2026, 8, 3)) == "SU")
check("no as_of -> None", SF(None) is None)

print("\nthe parser: a Spot slot")
check("Nov 5: Spot 10/15 prices vs ZCZ26, value as posted", fut("Spot 10/15") == [("Spot", "ZCZ26")])
rows, _ = rp.parse_rundown("Spot 10/15", "NS Ft Wayne", date(2026, 11, 5))
check("... the number is untouched (bid 10, offer 15)", rows[0]["bid"] == 10 and rows[0]["offer"] == 15 and rows[0]["period"] == "Spot")
check("Nov 30 is still ZCZ26", fut("Spot 10/15", as_of=date(2026, 11, 30)) == [("Spot", "ZCZ26")])
check("Dec 1 rolls to ZCH27", fut("Spot 10/15", as_of=date(2026, 12, 1)) == [("Spot", "ZCH27")])
check("Sep 1 2026 rolls to ZCZ26 (CU is past first notice)", fut("Spot 10/15", as_of=date(2026, 9, 1)) == [("Spot", "ZCZ26")])
check("Mar 2 2027 rolls to ZCK27", fut("Spot 10/15", as_of=date(2027, 3, 2)) == [("Spot", "ZCK27")])
check("any case: spot / SPOT", fut("spot 10/15") == [("spot", "ZCZ26")] and fut("SPOT 10/15") == [("SPOT", "ZCZ26")])
check("the same on every corridor (CSX Columbus, UP Group 3, BN Hereford)",
      all(fut("Spot 10/15", corridor=c) == [("Spot", "ZCZ26")] for c in ("CSX Columbus", "UP Group 3", "BN Hereford")))
check("a cell's own tag still wins (Spot 10/15h = CH)", fut("Spot 10/15h") == [("Spot", "ZCH27")] and fut("Spot 10/15k") == [("Spot", "ZCK27")], fut("Spot 10/15h"))
check("amid a rundown: Spot and Nov vs ZCZ26, Dec vs CH by the desk's rule, JFM vs CH, order kept",
      fut("Spot 5/8 Nov 4/10 Dec 8/14 JFM 12/18") == [("Spot", "ZCZ26"), ("Nov", "ZCZ26"), ("Dec", "ZCH27"), ("JFM", "ZCH27")], fut("Spot 5/8 Nov 4/10 Dec 8/14 JFM 12/18"))
rows, _ = rp.parse_rundown("Spot 5/8 Nov 4/10 Dec 8/14", "NS Ft Wayne", date(2026, 11, 5))
check("... and period_order follows the listed order", [r["period_order"] for r in rows] == [0, 1, 2])
check("a label that merely contains 'Spot' plus a month is not a Spot slot (Spot Nov -> the month's own rule)", fut("Spot Nov 5/8") == [("Spot Nov", "ZCZ26")] and fut("Spot Mar 5/8") == [("Spot Mar", "ZCH27")], fut("Spot Mar 5/8"))
check("an untouched month still follows the existing roll (Oct 2026 after Sep 1 -> CZ)", fut("Oct 4/10", as_of=date(2026, 9, 1)) == [("Oct", "ZCZ26")])

print("\nsoybeans and other commodities")
check("Nov 5: a bean Spot prices vs ZSF27", fut("Spot 20/30", corridor="CN 105s", commodity="Soybeans") == [("Spot", "ZSF27")], fut("Spot 20/30", corridor="CN 105s", commodity="Soybeans"))
check("Oct 30: the bean Spot is still ZSX26", fut("Spot 20/30", corridor="CN 105s", commodity="Soybeans", as_of=date(2026, 10, 30)) == [("Spot", "ZSX26")])
check("a bean tag wins over the rule (Spot 20/30x = SX, next year's after first notice)", fut("Spot 20/30x", corridor="CN 105s", commodity="Soybeans") == [("Spot", "ZSX27")], fut("Spot 20/30x", corridor="CN 105s", commodity="Soybeans"))
check("wheat is still left blank for the reviewer", fut("Spot 20/30", corridor="BN PNW", commodity="Wheat") == [("Spot", None)])

print("\nfreight keeps no futures")
parsed, _ = rp.parse_multi("BN Freight Spot -150/0 December -200/0", as_of=date(2026, 11, 5))
check("the parser itself reads the freight Spot like any other", [(r["period"], r["futures"]) for r in parsed][:1] == [("Spot", "ZCZ26")], [(r["period"], r["futures"]) for r in parsed])
built = pr.build_corridor_rows(parsed, "Corn")
check("but post_rail strips every freight futures, Spot included", [x["futures"] for x in built["BN Freight"]] == [None, None] and [x["period"] for x in built["BN Freight"]] == ["Spot", "December"], built["BN Freight"])

if FAILS:
    print("\n%d FAILED:" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nall rail Spot checks passed")
