"""River elevators with no history of their own get one ESTIMATED from the River FOB sheet (river_derived): the gap, the pairing, the
acceptance rules, the derived bids and their flags. Pure — a hand-built archive, no database.

    python tests/test_river_derived.py
"""
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import return_to_carry as rtc             # noqa: E402
import return_to_carry_data as rd         # noqa: E402
import river_carry as rc                  # noqa: E402
import river_derived as rdv               # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


D = date

print("river_derived: which locations, and the safety rules")
check("every mapped elevator sits on a reach the FOB sheet publishes", all(v in rc.LOCATION for v in rdv.FOB_REACH.values()), [v for v in rdv.FOB_REACH.values() if v not in rc.LOCATION])
check("the rules: 40+ pairs, a middle half no wider than 12 cents, a median no larger than 30", (rdv.MIN_PAIRS, rdv.MAX_IQR, rdv.MAX_GAP) == (40, 12.0, 30.0))
check("a location whose own bids start in 2026 needs a history; one with bids from 2022, or none at all, does not",
      rdv.needs_history([{"date": D(2026, 6, 12)}, {"date": D(2026, 6, 19)}]) and not rdv.needs_history([{"date": D(2022, 3, 2)}, {"date": D(2026, 6, 19)}]) and not rdv.needs_history([]))

print("river_derived: the gap between an elevator's bid and the FOB (same day, same delivery month)")
FUT = {D(2026, 10, 2): {"ZCZ26": 500.0, "ZCH27": 515.0, "ZSX26": 1280.0, "ZSF27": 1290.0}}
fob_q = [{"date": D(2026, 10, 2), "label": "Oct 2026", "tag": "ZCZ26", "basis": -10.0},
         {"date": D(2026, 10, 2), "label": "Nov 2026", "tag": "ZCZ26", "basis": 5.0},
         {"date": D(2026, 10, 2), "label": "Jan 2027", "tag": "ZCH27", "basis": 20.0},
         {"date": D(2026, 10, 2), "label": "Dec 2026", "tag": "ZCZ26", "basis": 12.0}]
live_q = [{"date": D(2026, 10, 2), "label": "October", "tag": "ZCZ26", "basis": -14.0},          # same contract: gap +4
          {"date": D(2026, 10, 2), "label": "Nov 2026", "tag": "ZCZ26", "basis": 1.0},           # gap +4
          {"date": D(2026, 10, 2), "label": "Jan 27", "tag": "ZCZ26", "basis": 6.0},            # FOB off Mar, bid off Dec: 20 + 515 - 500 = 35, gap +29
          {"date": D(2026, 10, 2), "label": "FH Nov", "tag": "ZCZ26", "basis": -5.0},           # a half month: not paired
          {"date": D(2026, 10, 2), "label": "JFM", "tag": "ZCH27", "basis": 9.0},               # a package: not paired
          {"date": D(2026, 10, 2), "label": "Feb 2027", "tag": "ZCH27", "basis": 7.0},          # the FOB sheet has no Feb: not paired
          {"date": D(2026, 10, 9), "label": "Nov 2026", "tag": "ZCZ26", "basis": 0.0}]          # a day the FOB sheet did not post: not paired
gp = rdv.gap_pairs(live_q, fob_q, FUT)
check("full-month quotes pair by day and delivery month ('October', 'Nov 2026', 'Jan 27' match the sheet's Oct / Nov / Jan); halves, packages, a month or a day the sheet lacks do not",
      [(d, round(g, 6)) for d, g in gp] == [(D(2026, 10, 2), 4.0), (D(2026, 10, 2), 4.0), (D(2026, 10, 2), 29.0)], gp)
check("a FOB quoted off another contract is moved to the bid's contract by that day's futures spread (Jan: 20 + 515 - 500, less the bid's 6)", close(gp[2][1], 20 + 15 - 6))
check("no price for one of the two contracts that day -> that pair is dropped, not guessed",
      rdv.gap_pairs(live_q, fob_q, {D(2026, 10, 2): {"ZCZ26": 500.0}}) == gp[:2])

print("river_derived: is the gap steady enough to mean something?")
steady = [(D(2026, 7, 1) + timedelta(days=i), 4.0 + (i % 5) * 0.5) for i in range(60)]
cal = rdv.calibrate(steady)
check("60 pairs around 4-6 cents: the median and the middle half, 60 pairs", cal is not None and close(cal[0], 5.0, 0.6) and cal[3] == 60 and cal[1] < cal[0] < cal[2], cal)
check("fewer than 40 pairs is not enough", rdv.calibrate(steady[:39]) is None and rdv.calibrate(steady[:40]) is not None)
check("a middle half wider than 12 cents is a different animal", rdv.calibrate([(D(2026, 7, 1), -20.0 if i % 2 else 25.0) for i in range(60)]) is None)
check("so is a median beyond 30 cents (the quote is of something else)", rdv.calibrate([(D(2026, 7, 1), 55.0 + (i % 3)) for i in range(60)]) is None)


# a hand-built archive: 2019-2026 weekly sheets, a flat CIF and freight, so a Havana FOB of exactly (0.50 - 4.64 x 4.0 / 2000 x 56) x 100 = -2.0 cents less the carry
def archive():
    a = {"calendar": {}, "cif": {}, "freight": {}}
    names = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "June", 7: "July", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
    code = lambda m: "CZ" if m in (10, 11, 12) else "CH" if m in (1, 2, 3) else "CK" if m in (4, 5) else "CN" if m in (6, 7) else "CU"
    d = D(2019, 10, 2)
    while d <= D(2026, 10, 7):
        order = [((d.month - 1 + i) % 12) + 1 for i in range(6)]
        lvl = 0.50 + 0.02 * (d.year - 2019) + 0.01 * ((d.toordinal() // 7) % 5)          # varies a little, week to week and year to year
        a["calendar"][d.isoformat()] = {"Corn": [(names[m], code(m)) for m in order]}
        a["cif"][d.isoformat()] = {"Corn": {names[m]: lvl for m in order}}
        a["freight"][d.isoformat()] = {"IL": {names[m]: 4.0 for m in order}}
        d += timedelta(days=7)
    return a


ARCH = archive()
HAV = rc.LOCATION["Havana"]
fob_week = {o["date"]: o for o in rc.nearby_obs(ARCH, "Havana", "Corn")}
check("the hand-built archive gives Havana a weekly nearby FOB (Wednesdays from 2019-10-02)", len(fob_week) > 360 and D(2019, 10, 2) in fob_week, len(fob_week))
FUTS = {}
for dd in fob_week:
    FUTS[dd] = {sym: 400.0 + i for i, sym in enumerate(("ZCZ19", "ZCH20", "ZCK20", "ZCN20", "ZCU20", "ZCZ20", "ZCH21", "ZCK21", "ZCN21", "ZCU21",
                                                          "ZCZ21", "ZCH22", "ZCK22", "ZCN22", "ZCU22", "ZCZ22", "ZCH23", "ZCK23", "ZCN23", "ZCU23",
                                                          "ZCZ23", "ZCH24", "ZCK24", "ZCN24", "ZCU24", "ZCZ24", "ZCH25", "ZCK25", "ZCN25", "ZCU25",
                                                          "ZCZ25", "ZCH26", "ZCK26", "ZCN26", "ZCU26", "ZCZ26", "ZCH27", "ZCK27", "ZCN27"))}

OWN = D(2026, 6, 10)                                                              # the elevator's own bids start here
GAP = 5.0
live_obs, live_quotes = [], []
d = OWN
while d <= D(2026, 10, 7):
    f = rc.forward_quotes(ARCH, "Havana", "Corn")                                   # (cheap enough for a test)
    live_obs.append({"date": d, "basis": fob_week[d]["basis"] - GAP if d in fob_week else -8.0, "tag": fob_week[d]["tag"] if d in fob_week else "ZCU26"})
    d += timedelta(days=7)
fq_all = rc.forward_quotes(ARCH, "Havana", "Corn")
for q in fq_all:
    if q["date"] >= OWN:
        live_quotes.append({"date": q["date"], "label": q["label"], "tag": q["tag"], "basis": q["basis"] - GAP})       # the bids sit exactly 5 cents under the FOB
live_obs = [o for o in live_obs if o["date"] in fob_week]

print("river_derived: the derived history in front of an elevator's own bids")
obs, dv = rdv.with_derived("ADM", "Havana, IL", "Corn", live_obs, live_quotes, ARCH, FUTS)
check("a steady 5-cent gap is found, with plenty of pairs, and the derivation says which reach and when the own bids begin",
      dv is not None and dv.fob_location == "Havana" and close(dv.gap, 5.0, 1e-6) and dv.pairs >= 40 and dv.own_from == OWN and dv.first == D(2019, 10, 2), dv)
der = [o for o in obs if o.get("derived")]
own = [o for o in obs if not o.get("derived")]
check("every derived bid is flagged, they all come before the own bids, which are returned untouched and in order",
      len(der) == dv.derived_weeks and all(o["date"] < OWN for o in der) and own == live_obs and obs == der + own and [o["date"] for o in obs] == sorted(o["date"] for o in obs))
check("a derived bid is the FOB of that week less the gap, off the contract the FOB sheet maps it to",
      all(close(o["basis"], fob_week[o["date"]]["basis"] - GAP) and o["tag"] == fob_week[o["date"]]["tag"] for o in der))
check("the as_dict() the screen reads carries the numbers", dv.as_dict()["gap"] == dv.gap and dv.as_dict()["own_from"] == OWN and set(dv.as_dict()) >= {"fob_location", "gap", "q1", "q3", "pairs", "own_from", "derived_weeks", "first"})
marks = rd.derived_years(obs)
check("crop years built entirely from derived bids are 'derived', the year the own bids take over is 'part', later years are the location's own",
      marks.get(2019) == "derived" and marks.get(2024) == "derived" and marks.get(2025) == "part" and 2026 not in marks, marks)

print("river_derived: a constant gap cancels out of the return (the interest on the cash price moves by a fraction of a cent)")
hist_d, _ = rd.run_history_noted(obs, FUTS, lambda dd: 6.0, spec=rtc.CORN)
hist_f, _ = rd.run_history_noted(rc.nearby_obs(ARCH, "Havana", "Corn"), FUTS, lambda dd: 6.0, spec=rtc.CORN)
bd = {cy.crop_year: cy for cy in hist_d}
bf = {cy.crop_year: cy for cy in hist_f}
both = [y for y in sorted(bd) if y in bf and marks.get(y) == "derived" and bd[y].best.get("net") is not None]
check("each fully derived crop year's weekly GROSS return is exactly the FOB series' (the gap cancels in bid - harvest basis); only the harvest basis moves, by the gap",
      len(both) >= 4 and all(close(bf[y].b0 - bd[y].b0, GAP, 1e-9)
                              and all(close(a.gross, b.gross, 1e-9) for a, b in zip(bd[y].weeks, bf[y].weeks) if a.gross is not None and b.gross is not None)
                              for y in both), both)
check("...and the NET return differs only by the interest on the 5 cents of cash price: under half a cent a week, at the best week too",
      all(abs(bd[y].best["net"].net - bf[y].best["net"].net) < 0.5
          and all(abs(a.net - b.net) < 0.5 for a, b in zip(bd[y].weeks, bf[y].weeks) if a.net is not None and b.net is not None) for y in both), both)

print("river_derived: when NOT to derive")
check("an unmapped location, wheat, and a location with a history of its own come back as they are, with no derivation",
      rdv.with_derived("ADM", "Decatur, IL (Corn Processing)", "Corn", live_obs, live_quotes, ARCH, FUTS) == (live_obs, None)
      and rdv.with_derived("ADM", "Havana, IL", "Wheat", live_obs, live_quotes, ARCH, FUTS) == (live_obs, None)
      and rdv.with_derived("ADM", "Havana, IL", "Corn", [dict(o, date=o["date"] - timedelta(days=700)) for o in live_obs], live_quotes, ARCH, FUTS)[1] is None)
scattered = [dict(q, basis=q["basis"] + (30 if i % 2 else -30)) for i, q in enumerate(live_quotes)]
check("a gap that is not steady (the middle half wider than 12 cents) is not derived: the location keeps no history rather than a misleading one",
      rdv.with_derived("ADM", "Havana, IL", "Corn", live_obs, scattered, ARCH, FUTS) == (live_obs, None))
check("nor are there pairs to measure: a location whose quotes share no day or month with the sheet",
      rdv.with_derived("ADM", "Havana, IL", "Corn", live_obs, [dict(q, label="Sep 2031") for q in live_quotes], ARCH, FUTS) == (live_obs, None))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
