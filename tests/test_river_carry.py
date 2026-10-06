"""The River FOB sheet archive as Net Carry / Return to Carry inputs (river_carry): column placement, the FOB arithmetic, the
nearby series and the forward quotes. Pure — a hand-built archive, no database.

    python tests/test_river_carry.py
"""
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import river_carry as rc                  # noqa: E402
import return_to_carry as rtc             # noqa: E402
import return_to_carry_data as rd         # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


D = date

print("river_carry: contract symbols (the year is the occurrence of the month letter nearest the delivery month)")
check("Oct 2026 corn off CZ is ZCZ26; Jan 2027 off CH is ZCH27 (the March of the same crop year); Dec 2026 soybeans off SF is ZSF27",
      rc.contract_symbol("Corn", "CZ", (2026, 10)) == "ZCZ26" and rc.contract_symbol("Corn", "CH", (2027, 1)) == "ZCH27"
      and rc.contract_symbol("Soybeans", "SF", (2026, 12)) == "ZSF27")
check("a September soybean delivery off SX is the November of that year; an August corn delivery off CU is September",
      rc.contract_symbol("Soybeans", "SX", (2026, 9)) == "ZSX26" and rc.contract_symbol("Corn", "CU", (2026, 8)) == "ZCU26")
check("the same code resolves by delivery year: Jan 2008 off CZ (a 2006 sheet's mapping) is Dec 2007, not Dec 2008",
      rc.contract_symbol("Corn", "CZ", (2008, 1)) == "ZCZ07")
check("wheat has a root too; an unknown letter or commodity gives nothing", rc.contract_symbol("Wheat", "WH", (2027, 2)) == "ZWH27"
      and rc.contract_symbol("Corn", "CB", (2026, 10)) is None and rc.contract_symbol("Rye", "RZ", (2026, 10)) is None and rc.contract_symbol("Corn", None, (2026, 10)) is None)
check("labels: 'Oct 2026' and 'Spot Oct 2026'", rc.delivery_label((2026, 10)) == "Oct 2026" and rc.delivery_label((2026, 10), True) == "Spot Oct 2026")
check("every spelling a sheet used reads as a month; Spot and junk do not", [rc.month_number(x) for x in ("Oct", "June", "July", "Sept", "Sep.", "December")] == [10, 6, 7, 9, 9, 12]
      and rc.month_number("Spot") is None and rc.month_number("junk") is None and rc.month_number(None) is None)

print("river_carry: placing a sheet's columns")
cols = [("Oct", "CZ"), ("Nov", "CZ"), ("Dec", "CZ"), ("Jan", "CH"), ("Feb", "CH")]
pl = rc.place_columns("Corn", D(2026, 10, 7), cols)
check("a normal sheet: Oct 2026 .. Dec 2026, then Jan 2027 and Feb 2027 (the year turns at January) with their contracts",
      [(c[1], c[3]) for c in pl] == [((2026, 10), "ZCZ26"), ((2026, 11), "ZCZ26"), ((2026, 12), "ZCZ26"), ((2027, 1), "ZCH27"), ((2027, 2), "ZCH27")], pl)
sp = rc.place_columns("Corn", D(2025, 10, 8), [("Spot", "CZ")] + cols)
check("a leading 'Spot' is dropped when the sheet also has that month's column (a month is never quoted twice)", [c[0] for c in sp] == ["Oct", "Nov", "Dec", "Jan", "Feb"], sp)
sp2 = rc.place_columns("Corn", D(2025, 10, 8), [("Spot", "CZ"), ("Nov", "CZ"), ("Dec", "CZ")])
check("...and kept, as the sheet's own month, when it has no column for it", [(c[0], c[1], c[4]) for c in sp2] == [("Spot", (2025, 10), True), ("Nov", (2025, 11), False), ("Dec", (2025, 12), False)], sp2)
check("a repeated label keeps its first column ('July' twice on the 2020-10-06 sheet)",
      [c[0] for c in rc.place_columns("Corn", D(2020, 10, 6), cols + [("June", "CN"), ("July", "CN"), ("July", "CN")])] == ["Oct", "Nov", "Dec", "Jan", "Feb", "June", "July"])
check("a sheet dated early September may start at October (one month ahead); two months ahead is still fine, three is not",
      rc.place_columns("Corn", D(2006, 9, 7), cols)[0][1] == (2006, 10) and rc.place_columns("Corn", D(2006, 8, 2), cols)[0][1] == (2006, 10)
      and rc.place_columns("Corn", D(2006, 7, 5), cols) == [])
check("a sheet whose first month is already behind its date (stale headers) is set aside, not guessed", rc.place_columns("Corn", D(2025, 11, 5), cols) == [])
check("an unreadable label sets the sheet aside; an empty calendar gives nothing", rc.place_columns("Corn", D(2026, 10, 7), [("Oct", "CZ"), ("Qtr", "CZ")]) == [] and rc.place_columns("Corn", D(2026, 10, 7), None) == [])
wrap = rc.place_columns("Soybeans", D(2026, 12, 2), [("Dec", "SF"), ("Jan", "SF"), ("Feb", "SH"), ("Mar", "SH")])
check("soybeans across the new year: Dec 2026 off ZSF27, Jan 2027 off ZSF27, Feb 2027 off ZSH27", [(c[1], c[3]) for c in wrap] == [((2026, 12), "ZSF27"), ((2027, 1), "ZSF27"), ((2027, 2), "ZSH27"), ((2027, 3), "ZSH27")], wrap)

print("river_carry: the FOB arithmetic (CIF less tariff x freight / 2000 x bushel weight)")
IL = rc.LOCATION["Hennepin"]
check("Hennepin sits on the Illinois River with the 5.07 tariff", IL.region == "IL" and abs(IL.factor - 5.07) < 1e-12 and rc.LOCATION["STL"].region == "STL")
ARCH = {"calendar": {"2006-09-07": {"Corn": [("Oct", "CZ"), ("Nov", "CZ"), ("Dec", "CZ"), ("Jan", "CZ")], "Soybeans": [("Oct", "SX"), ("Nov", "SX"), ("Dec", "SF")]}},
        "cif": {"2006-09-07": {"Corn": {"Oct": 0.60, "Nov": 0.60, "Dec": 0.63, "Jan": 0.52}, "Soybeans": {"Oct": 0.61, "Nov": 0.69, "Dec": 0.59}}},
        "freight": {"2006-09-07": {"IL": {"Oct": 6.1, "Nov": 5.15, "Dec": 4.55, "Jan": 0.0}, "STL": {"Oct": 5.6, "Nov": 4.55, "Dec": 4.2, "Jan": 4.2}}}}
want_oct = (0.60 - 5.07 * 6.1 / 2000 * 56) * 100
check("FOB Hennepin corn Oct 2006: CIF 0.60 - 5.07 x 6.10 / 2000 x 56 = -26.60 cents", close(rc.fob_cents(ARCH, "2006-09-07", "Hennepin", "Corn", "Oct"), want_oct, 1e-9), rc.fob_cents(ARCH, "2006-09-07", "Hennepin", "Corn", "Oct"))
check("soybeans use the 60-pound bushel", close(rc.fob_cents(ARCH, "2006-09-07", "Hennepin", "Soybeans", "Oct"), (0.61 - 5.07 * 6.1 / 2000 * 60) * 100, 1e-9))
check("STL draws on its own freight reach (3.99 x 5.60)", close(rc.fob_cents(ARCH, "2006-09-07", "STL", "Corn", "Oct"), (0.60 - 3.99 * 5.6 / 2000 * 56) * 100, 1e-9))
check("a zero freight cell (river closed) is no FOB, not a FOB at the no-freight level; a missing CIF or an unknown location is none either",
      rc.fob_cents(ARCH, "2006-09-07", "Hennepin", "Corn", "Jan") is None and rc.fob_cents(ARCH, "2006-09-07", "Hennepin", "Corn", "Feb") is None
      and rc.fob_cents(ARCH, "2006-09-07", "Nowhere", "Corn", "Oct") is None and rc.fob_cents(ARCH, "1999-01-01", "Hennepin", "Corn", "Oct") is None)

print("river_carry: one sheet's forward curve (what the Net Carry tab reads)")
it = rc.curve_items(ARCH, "2006-09-07", "Hennepin", "Corn")
check("the months that have a FOB, with the year spelled out and the sheet's contract (Jan is dropped: closed)",
      [(i["delivery"], i["futures"]) for i in it] == [("Oct 2006", "ZCZ06"), ("Nov 2006", "ZCZ06"), ("Dec 2006", "ZCZ06")]
      and close(it[0]["basis"], want_oct, 1e-3), it)
check("a date the archive does not have, or a commodity it has no calendar for, gives an empty curve", rc.curve_items(ARCH, "2020-01-01", "Hennepin", "Corn") == [] and rc.curve_items(ARCH, "2006-09-07", "Hennepin", "Wheat") == [])

print("river_carry: the weekly nearby FOB and the forward quotes")
A2 = {"calendar": {}, "cif": {}, "freight": {}}


def sheet(iso, commodity, cols, cif, frt_il):
    A2["calendar"].setdefault(iso, {})[commodity] = cols
    A2["cif"].setdefault(iso, {})[commodity] = cif
    A2["freight"].setdefault(iso, {}).setdefault("IL", {}).update(frt_il)


sheet("2025-10-08", "Corn", [("Spot", "CZ"), ("Oct", "CZ"), ("Nov", "CZ"), ("Dec", "CZ")], {"Spot": 0.50, "Oct": 0.52, "Nov": 0.55, "Dec": 0.58}, {"Spot": 5.0, "Oct": 5.0, "Nov": 5.0, "Dec": 5.0})
sheet("2025-10-15", "Corn", [("Spot", "CZ"), ("Oct", "CZ"), ("Nov", "CZ")], {"Spot": None, "Oct": 0.53, "Nov": 0.56}, {"Spot": 5.0, "Oct": 5.0, "Nov": 5.0})
sheet("2026-01-14", "Corn", [("Jan", "CH"), ("Feb", "CH"), ("Mar", "CH")], {"Jan": 0.70, "Feb": 0.71, "Mar": 0.72}, {"Jan": 0.0, "Feb": 6.0, "Mar": 6.0})
sheet("2025-12-03", "Corn", [("Nov", "CZ"), ("Dec", "CH")], {"Nov": 0.60, "Dec": 0.61}, {"Nov": 5.0, "Dec": 5.0})      # stale: first month behind the date
ob = rc.nearby_obs(A2, "Hennepin", "Corn")
fob = lambda cif, f: (cif - 5.07 * f / 2000 * 56) * 100
check("one bid per usable sheet, oldest first: Oct 8 (the Oct column, the month wins over Spot), Oct 15 (Spot has no CIF so the month is used)",
      [(o["date"], o["tag"]) for o in ob][:2] == [(D(2025, 10, 8), "ZCZ25"), (D(2025, 10, 15), "ZCZ25")]
      and close(ob[0]["basis"], fob(0.52, 5.0), 1e-9) and close(ob[1]["basis"], fob(0.53, 5.0), 1e-9), ob)
check("a sheet with no FOB for its own month (Jan 14: the river is closed) gives no bid that week and never borrows a later month", D(2026, 1, 14) not in [o["date"] for o in ob])
check("a stale-header sheet (Dec 3 starting at November) is set aside: no bid", D(2025, 12, 3) not in [o["date"] for o in ob] and len(ob) == 2)
fq = rc.forward_quotes(A2, "Hennepin", "Corn")
check("every month with a FOB is a quote, labelled with its year, off its contract (Oct 8: Oct, Nov, Dec; Oct 15: Oct, Nov; Jan 14: Feb, Mar)",
      [(q["date"], q["label"]) for q in fq] == [(D(2025, 10, 8), "Oct 2025"), (D(2025, 10, 8), "Nov 2025"), (D(2025, 10, 8), "Dec 2025"), (D(2025, 10, 15), "Oct 2025"), (D(2025, 10, 15), "Nov 2025"),
                                                (D(2026, 1, 14), "Feb 2026"), (D(2026, 1, 14), "Mar 2026")], [(q["date"], q["label"]) for q in fq])
check("a Spot column becomes a Spot label only when it is the sheet's only quote for that month", all(not q["label"].startswith("Spot") for q in fq))
sq = {(q["date"], q["month"]): q for q in rd.shipment_quotes(fq, 2025)}
check("the shipment table picks them up by month and year: Nov 2025 and Dec 2025 on Oct 8 (Oct is not a column), Feb and Mar 2026 on Jan 14",
      (D(2025, 10, 8), 11) in sq and (D(2025, 10, 8), 12) in sq and (D(2026, 1, 14), 2) in sq and (D(2026, 1, 14), 3) in sq and not any(m == 10 for (_d, m) in sq), sorted(sq))
check("...with the sheet's contract as the quote's tag", sq[(D(2026, 1, 14), 2)]["tag"] == "ZCH26" and sq[(D(2025, 10, 8), 12)]["tag"] == "ZCZ25")

print("river_carry: a synthetic archive runs through the tracker")
B = {"calendar": {}, "cif": {}, "freight": {}}
FUT = {}
for y in (2020, 2021, 2022):
    st = rtc.first_wednesday(y)
    for k in range(-3, 45):
        d = st + timedelta(days=7 * k)
        m = d.month
        # the sheet's window starts at the as-of month; October-July weeks only (corn)
        order = [((m - 1 + i) % 12) + 1 for i in range(6)]
        names = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "June", 7: "July", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
        code = lambda mm: "CZ" if mm in (10, 11, 12) else "CH" if mm in (1, 2, 3) else "CK" if mm in (4, 5) else "CN" if mm in (6, 7) else "CU"
        iso = d.isoformat()
        B["calendar"][iso] = {"Corn": [(names[mm], code(mm)) for mm in order]}
        B["cif"][iso] = {"Corn": {names[mm]: 0.50 + 0.02 * (y - 2020) for mm in order}}      # a different level each crop year (identical years are dropped as copies)
        B["freight"][iso] = {"IL": {names[mm]: 4.0 for mm in order}}
        FUT[d] = {rtc.contract_symbol("Z", y): 400.0, rtc.contract_symbol("H", y): 410.0, rtc.contract_symbol("K", y): 414.0, rtc.contract_symbol("N", y): 416.0, rtc.contract_symbol("U", y): 410.0}
    for dd in rtc.roll_dates(y).values():
        FUT.setdefault(dd, {}).update(FUT.get(st, {}))
flat = lambda y: (0.50 + 0.02 * (y - 2020) - 5.07 * 4.0 / 2000 * 56) * 100       # the FOB of every week of a crop year: flat, -6.78 cents in 2020-21
obs_b = rc.nearby_obs(B, "Hennepin", "Corn")
check("a flat sheet gives a flat nearby series, one bid a week, all with a contract tag",
      len(obs_b) > 140 and all(o["tag"] for o in obs_b) and all(close(o["basis"], flat(2020 if o["date"] < D(2021, 9, 1) else 2021 if o["date"] < D(2022, 9, 1) else 2022), 1e-9) for o in obs_b), len(obs_b))
hist = rd.run_history(obs_b, FUT, lambda d: 5.0, min_year=2020)
check("three complete crop years come out of it, each harvest basis is that year's flat FOB, and the headline carry is the futures' Dec -> Jul (10 + 4 + 2 = 16)",
      len(hist) == 3 and all(cy.complete for cy in hist) and all(close(cy.b0, flat(cy.crop_year), 1e-9) for cy in hist) and all(close(cy.season_carry, 16.0) for cy in hist),
      [(cy.label, cy.b0, cy.season_carry) for cy in hist])
yr = hist[0]
last = yr.weeks[-1]
check("a flat basis earns exactly the banked futures carry less the interest: gross at the end = 16 (the Jul-quoted bids), net = 16 less cash x the running rate / 5200",
      close(last.gross, 16.0, 1e-9) and last.net is not None and last.net < last.gross, (last.gross, last.net))

print("river_carry: the archive's locations")
check("the sheet's current locations, in its order, without discontinued Gulfport",
      rc.CURRENT_LOCATIONS[0] == "Greenville" and "STL" in rc.CURRENT_LOCATIONS and "Hennepin" in rc.CURRENT_LOCATIONS and "Havana" in rc.CURRENT_LOCATIONS and "Gulfport" not in rc.CURRENT_LOCATIONS
      and len(rc.CURRENT_LOCATIONS) == len(set(rc.CURRENT_LOCATIONS)), rc.CURRENT_LOCATIONS)
check("dates() lists the archive newest first, and with a commodity only the sheets that can be placed for it",
      rc.dates(A2)[0] == "2026-01-14" and rc.dates(A2, "Corn") == ["2026-01-14", "2025-10-15", "2025-10-08"] and "2025-12-03" in rc.dates(A2) and rc.dates(A2, "Soybeans") == [])

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
