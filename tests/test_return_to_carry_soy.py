"""Return to Carry for SOYBEANS: the engine must reproduce the Research Analyst's bean workbooks.

    python tests/test_return_to_carry_soy.py

Oracle = her `BeanCarry` yearly sheets (Decatur / Des Moines / Hennepin / St. Louis, 2005-06 .. 2022-23): tests/fixtures/rtc_soy_sheets.json
holds a dozen of them — her weekly bids, futures and prime, and the 'Appreciation Less Int Cost' she computed — chosen to cover every
convention (window, interest start, roll rows). Across all 61 usable sheets the engine matches 75% of the comparable weeks to the
hundredth of a cent and the rest are rows where her own sheet is inconsistent (a template cash-price glitch in the last two September
rows, the first interest row with an empty basis cell, a roll spread re-measured mid-year in 2020-21): those weeks are listed per sheet
as `off` and asserted exactly, so any change in the engine shows up. Nothing here touches a database or the network.
"""
import dataclasses
import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import return_to_carry as rtc          # noqa: E402
import return_to_carry_data as rd      # noqa: E402
import return_to_carry_view as vw      # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


D = date.fromisoformat
SOY = rtc.SOY
SHEET = dataclasses.replace(SOY, normalize_tags=False)          # her sheets' own tags, as typed

print("soybeans: the chain, the symbols and the calendar")
check("chain Nov, Jan, Mar, May, Jul, Aug, next Nov; the harvest basis is expressed vs Jan",
      SOY.chain == ("X", "F", "H", "K", "N", "Q", "x") and SOY.base == "F" and SOY.root == "ZS")
check("the six roll pairs: XF FH HK KN NQ Qx", [SOY.pair_key(a, b) for a, b in SOY.pairs] == ["XF", "FH", "HK", "KN", "NQ", "Qx"])
check("contract symbols: Nov and next Nov are the same letter a year apart (2025-26: ZSX25, ZSF26 ... ZSQ26, ZSX26)",
      [rtc.contract_symbol(l, 2025, "ZS") for l in SOY.chain] == ["ZSX25", "ZSF26", "ZSH26", "ZSK26", "ZSN26", "ZSQ26", "ZSX26"])
check("corn's symbols are untouched", [rtc.contract_symbol(l, 2025) for l in rtc.CHAIN] == ["ZCZ25", "ZCH26", "ZCK26", "ZCN26", "ZCU26"])
r19 = rtc.roll_dates(2019, SOY)
check("roll dates = the last Wednesday before Nov, Jan, Mar, May, Jul, Aug 1 (2019-20: Oct 30, Dec 30*, Feb 26, Apr 29, Jun 24, Jul 29; *her sheet's Dec 30 row)",
      r19 == {"XF": D("2019-10-30"), "FH": D("2019-12-30"), "HK": D("2020-02-26"), "KN": D("2020-04-29"), "NQ": D("2020-06-24"), "Qx": D("2020-07-29")}, r19)
r22 = rtc.roll_dates(2022, SOY)
check("...and by the plain rule in a year that follows it (2022-23)",
      r22 == {"XF": D("2022-10-26"), "FH": D("2022-12-28"), "HK": D("2023-02-22"), "KN": D("2023-04-26"), "NQ": D("2023-06-28"), "Qx": D("2023-07-26")}, r22)
check("the sheets' own exceptions are kept: Nov/Jan on Nov 2-3 in 2010-11, Mar/May on Mar 1 2017, Jan/Mar on Dec 26 2013",
      rtc.roll_dates(2010, SOY)["XF"] == D("2010-11-03") and rtc.roll_dates(2011, SOY)["XF"] == D("2011-11-02")
      and rtc.roll_dates(2016, SOY)["HK"] == D("2017-03-01") and rtc.roll_dates(2013, SOY)["FH"] == D("2013-12-26"))
sched = {D(x): rtc.infer_tag(D(x), SOY, 2019) for x in ("2019-10-02", "2019-10-30", "2019-11-06", "2019-12-30", "2020-01-08", "2020-02-26", "2020-03-04",
                                                       "2020-04-29", "2020-05-06", "2020-06-24", "2020-07-01", "2020-07-29", "2020-08-05", "2020-09-23")}
check("the date's contract: Nov in Oct, Jan Nov-Dec, Mar Jan-Feb, May Mar-Apr, Jul May-Jun, Aug in Jul, next Nov Aug-Sep",
      list(sched.values()) == ["X", "X", "F", "F", "H", "H", "K", "K", "N", "N", "Q", "Q", "x", "x"], sched)
check("September of the crop year's first month is the harvest Nov, not the next", rtc.infer_tag(D("2019-09-18"), SOY, 2019) == "X" and rtc.infer_tag(D("2020-09-16"), SOY, 2019) == "x")
check("interest starts at the 5th weekly row (2009-10 on), the 3rd in 2005-06 .. 2008-09; corn's is the 3rd",
      SOY.accrual_overrides.get(2019, SOY.accrual_start) == 4 and SOY.accrual_overrides[2006] == 2 and rtc.CORN.accrual_start == 2)
check("the weeks run to Sep 30 (corn's to Jul 31)", SOY.horizon == (9, 30) and rtc.CORN.horizon == (7, 31))

print("soybeans: which contract a posted tag names (the archive's year digits are not trusted)")
check("a label as is, a symbol by its letter: ZSF19 (a Jan 2020 bid written with the crop-year digit) is Jan",
      rtc.tag_label("F", D("2019-12-04"), 2019, SOY) == "F" and rtc.tag_label("ZSF19", D("2019-12-04"), 2019, SOY) == "F" and rtc.tag_label("ZSF20", D("2019-12-04"), 2019, SOY) == "F")
check("November: the harvest one in Sep-Dec of the crop year, next crop's after that (ZSX19 on Oct 2 -> X; ZSX20 on Aug 5, 2020 -> x)",
      rtc.tag_label("ZSX19", D("2019-10-02"), 2019, SOY) == "X" and rtc.tag_label("ZSX20", D("2020-08-05"), 2019, SOY) == "x" and rtc.tag_label("ZSX27", D("2026-10-01"), 2026, SOY) == "X")
check("Sep (U), blanks and junk name no contract of the chain", all(rtc.tag_label(t, D("2020-09-02"), 2019, SOY) is None for t in ("ZSU20", "", None, "R", "ZS")))

print("soybeans: the analyst's own sheets, run through the engine on THEIR inputs")
FIX = json.load(open(os.path.join(HERE, "fixtures", "rtc_soy_sheets.json"), encoding="utf-8"))


def run_sheet(key):
    f = FIX[key]
    futs = {}
    for ds, syms in f["futs"].items():
        futs[D(ds)] = dict(syms)
    obs = [{"date": D(ds), "basis": b, "tag": t} for ds, b, t in f["obs"]]
    prime = {int(k): v for k, v in f["prime"].items()}
    cy = f["crop_year"]
    res = rtc.build_crop_year(obs, futs, cy, lambda d: prime.get(rtc.week_index(d, cy), 0.0), SHEET)
    S = {int(k): v for k, v in f["S"].items()}
    errs = {w.idx: w.net - S[w.idx] for w in res.weeks if w.net is not None and w.idx in S}
    off = sorted(k for k, e in errs.items() if abs(e) > 0.011)
    return f, res, errs, off


total_ok = total = 0
for key, f in FIX.items():
    f, res, errs, off = run_sheet(key)
    n_ok = sum(1 for k, e in errs.items() if abs(e) <= 0.011)
    total_ok += n_ok
    total += len(errs)
    check("%s (%s): b0 %s, %d of %d weeks to the hundredth, the rest are the sheet's own artefacts %s"
          % (key, f["why"][:60], round(f["b0_sheet"], 3), n_ok, len(errs), off),
          close(res.b0, f["b0_sheet"], 1e-9) and off == f["off"] and len(errs) >= 38, (res.b0, f["b0_sheet"], off, f["off"]))
check("overall: %d of %d comparable weeks reproduce her cached return to within 0.011" % (total_ok, total), total_ok / total > 0.8 and total > 500)

print("soybeans: the weekly return, hand-checked on a synthetic year")
# crop year 2026: Nov 2026 .. next Nov 2027; the chain's spreads (Jan-Nov 10, Mar-Jan 8, May-Mar 6, Jul-May 4, Aug-Jul -3, Nov27-Aug -20)
start = rtc.first_wednesday(2026)                                    # Oct 7, 2026
P = {"ZSX26": 1000.0, "ZSF27": 1010.0, "ZSH27": 1018.0, "ZSK27": 1024.0, "ZSN27": 1028.0, "ZSQ27": 1025.0, "ZSX27": 1005.0}
futs = {}
for k in range(-1, 53):
    futs[start + timedelta(days=7 * k)] = dict(P)
for d in rtc.roll_dates(2026, SOY).values():
    futs.setdefault(d, dict(P)).update(P)
obs = []
for k in range(0, 52):
    d = start + timedelta(days=7 * k)
    obs.append({"date": d, "basis": -20.0 + 0.5 * k})                # no tags: the date's contract
res = rtc.build_crop_year(obs, futs, 2026, lambda d: 6.0, SOY)
tags = {w.idx: w.tag for w in res.weeks}
check("a bid with no tag is quoted off the date's contract all year (Nov 4 F, Dec 30 F, Jan 6 H, Mar 3 K, May 5 N, Jul 7 Q, Aug 4 and Sep 22 next Nov)",
      [tags[k] for k in (4, 5, 12, 13, 21, 30, 39, 43, 50)] == ["F", "F", "F", "H", "K", "N", "Q", "x", "x"], tags)
# harvest basis: the 4 October Wednesdays (Oct 7, 14, 21, 28) are quoted off Nov and moved to Jan by the Nov-Jan spread (-10), then Nov 4, 11, 18 off Jan
want_b0 = ((-20.0 - 10) + (-19.5 - 10) + (-19.0 - 10) + (-18.5 - 10) + (-18.0) + (-17.5) + (-17.0)) / 7
check("b0 = the 7-week average with the October bids moved Nov -> Jan (-10): %.4f" % want_b0, close(res.b0, want_b0, 1e-9) and res.b0_weeks == 7, (res.b0, want_b0))
w_dec = next(w for w in res.weeks if w.idx == 12)                    # Dec 30, 2026: still Jan
check("a Jan-quoted week banks nothing: gross = bid - b0", close(w_dec.carry, 0.0) and close(w_dec.gross, w_dec.basis - res.b0))
w_mar = next(w for w in res.weeks if w.idx == 21)                    # Mar 3, 2027: May
check("a May-quoted week banks Jan->Mar (8) + Mar->May (6) = 14", close(w_mar.carry, 14.0) and close(w_mar.gross, w_mar.basis - res.b0 + 14.0))
w_aug = next(w for w in res.weeks if w.idx == 45)                    # Aug 18, 2027: next Nov
check("an August week banks 8 + 6 + 4 - 3 - 20 = -5 (the new-crop discount)", close(w_aug.carry, -5.0), w_aug.carry)
w4i = next(w for w in res.weeks if w.idx == 4)
w5i = next(w for w in res.weeks if w.idx == 5)
check("interest = cash price x the running prime / 5200, counted from the 5th row: Nov 4 has one week of 6% (992 x 6 / 5200), Nov 11 two",
      close(w4i.interest, (1010.0 - 18.0) * 6.0 / 5200, 1e-9) and close(w5i.interest, (1010.0 - 17.5) * 12.0 / 5200, 1e-9) and all(w.interest is None for w in res.weeks if w.idx < 4),
      (w4i.interest, w5i.interest))
check("the headline carry is Jan -> Jul (8 + 6 + 4 = 18), corn's Dec -> Jul", close(res.season_carry, 18.0) and rtc.CORN.carry_pairs == ("ZH", "HK", "KN") and SOY.carry_pairs == ("FH", "HK", "KN"))
check("the year runs to September: the last week is Sep 29, 2027 and counts as complete", res.weeks[-1].date >= date(2027, 9, 15) and res.complete)

print("soybeans: a bid quoted off another contract is moved to the date's by that day's spread")
obs2 = [dict(o) for o in obs]
for o in obs2:
    if o["date"] in (start + timedelta(days=7 * 4), start + timedelta(days=7 * 5)):
        o["tag"] = "ZSX26"                                           # a Nov bid posted in November (the DJ archive does this)
res2 = rtc.build_crop_year(obs2, futs, 2026, lambda d: 6.0, SOY)
w4 = next(w for w in res2.weeks if w.idx == 4)
check("a Nov-quoted bid in the Jan weeks is re-based to Jan: basis + F(Nov) - F(Jan) = -18 + 1000 - 1010", w4.tag == "F" and close(w4.basis, -28.0), (w4.tag, w4.basis))
res3 = rtc.build_crop_year(obs2, {d: {k: v for k, v in px.items() if k != "ZSX26"} for d, px in futs.items()}, 2026, lambda d: 6.0, SOY)
check("with no Nov price to move it by, the bid stays as posted (and off Nov) instead of being guessed",
      next(w for w in res3.weeks if w.idx == 4).tag == "X")
res_sheet = rtc.build_crop_year(obs2, futs, 2026, lambda d: 6.0, SHEET)
check("the sheets' mode keeps the typed tag (no move)", next(w for w in res_sheet.weeks if w.idx == 4).tag == "X")

print("soybeans: the shipment-by-month table (the report's front page on the bean chain), hand-checked on the same synthetic year")
SHIP_DATES = [D(s) for s in ("2026-11-20", "2026-12-20", "2027-01-20", "2027-02-20", "2027-03-20", "2027-04-20", "2027-05-20", "2027-06-20",
                             "2027-07-20", "2027-08-20")]
LEV = {"X": 1000.0, "F": 1010.0, "H": 1018.0, "K": 1024.0, "N": 1028.0, "Q": 1025.0, "x": 1005.0}      # P by chain label
RATE6 = lambda d: 6.0
ASOF = D("2026-12-09")                                                    # Oct 7 + 9 weeks: all seven weekly harvest bids are in
check("columns Nov .. Aug on the 20th, quoted off Jan, Jan, Mar, Mar, May, May, Jul, Jul, Aug, next Nov; corn keeps its nine",
      SOY.ship_months == (11, 12, 1, 2, 3, 4, 5, 6, 7, 8) and [SOY.ship_letter[m] for m in SOY.ship_months] == list("FFHHKKNNQx")
      and [rtc.ship_date(2026, m) for m in SOY.ship_months] == SHIP_DATES and rtc.SHIP_MONTHS == rtc.CORN.ship_months and len(rtc.CORN.ship_months) == 9)
t, est = rd.shipment_table(obs, [], futs, RATE6, ASOF, "net", SOY)
check("2026-27, ten columns, the seven weekly bids' average (not an estimate)", t.crop_year == 2026 and t.label == "2026-27" and t.spec is SOY and len(t.cols) == 10
      and t.b0_weeks == 7 and not t.b0_est and est is None and close(t.b0, want_b0), (t.b0, t.b0_weeks, t.b0_est))
check("the chain: Nov/Jan was measured Oct 28 (10) so Nov hangs from Jan; the rest are live; the interest base is Jan (1010)",
      t.levels.anchor == "F" and [t.levels.levels[k] for k in "XFHKNQx"] == [LEV[k] for k in "XFHKNQx"] and t.f_base == 1010.0
      and t.levels.spreads["XF"] == (10.0, D("2026-10-28"), True) and t.levels.spreads["FH"][2] is False and t.levels.spreads["FH"][0] == 8.0)
check("'Current Futures' by column: Jan, Jan, Mar, Mar, May, May, Jul, Jul, Aug, next Nov", [c.level for c in t.cols] == [1010.0, 1010.0, 1018.0, 1018.0, 1024.0, 1024.0, 1028.0, 1028.0, 1025.0, 1005.0])
want = [want_b0 - (LEV[c.letter] - 1010.0) + (1010.0 + want_b0) * 0.06 * (sd - D("2026-10-20")).days / 360 for c, sd in zip(t.cols, SHIP_DATES)]
check("Basis Cost = b0 - (F_M - F_Jan) + (F_Jan + b0) x 6% x days / 360, all ten columns (Aug 20 is 304 days out and a 5-cent new-crop discount)",
      len(want) == 10 and all(close(c.cost, w, 1e-9) for c, w in zip(t.cols, want)), [c.cost for c in t.cols])
tg, _ = rd.shipment_table(obs, [], futs, RATE6, ASOF, "gross", SOY)
check("gross = b0 - the futures carry only (Aug: b0 + 5; Jul: b0 - 15)", all(close(c.cost, want_b0 - (LEV[c.letter] - 1010.0), 1e-9) for c in tg.cols)
      and close(tg.cols[9].cost, want_b0 + 5.0) and close(tg.cols[8].cost, want_b0 - 15.0))

print("soybeans: the forward bids of each shipment month (one per month, off the contract it is quoted from, moved to the column's)")
raw = [{"date": ASOF, "label": "Dec", "tag": "ZSF27", "basis": -14.0},          # Dec ships off Jan: as posted
       {"date": ASOF, "label": "JFM", "tag": "ZSH27", "basis": -10.0},          # Jan, Feb off Mar: as posted; the Mar column is in May terms: -10 + 1018 - 1024
       {"date": ASOF, "label": "Jul", "tag": "ZSN27", "basis": -12.0},          # the Jul column is in Aug terms: -12 + 1028 - 1025
       {"date": ASOF, "label": "Aug", "tag": "ZSQ27", "basis": -8.0},           # the Aug column is in next-Nov terms: -8 + 1025 - 1005
       {"date": ASOF, "label": "Spot", "tag": "ZSF27", "basis": -17.0},
       {"date": D("2026-11-25"), "label": "Dec", "tag": "ZSF27", "basis": -9.0}]    # an older, better Dec bid: the best bid YTD, not today's
t2, _ = rd.shipment_table(obs, raw, futs, RATE6, ASOF, "net", SOY)
cm = {c.month: c for c in t2.cols}
check("Dec: as posted (-14), no move; Jan and Feb: the JFM package as posted (-10); Mar: the package moved to May terms (-16, posted -10)",
      cm[12].bid == -14.0 and cm[12].bid_posted is None and cm[1].bid == cm[2].bid == -10.0 and cm[1].bid_posted is None
      and cm[3].bid == -16.0 and cm[3].bid_posted == -10.0 and cm[3].bid_label == "JFM")
check("Jul is moved to Aug terms (-9, posted -12) and Aug to next Nov's (+12, posted -8): the soybean chain's own spreads",
      cm[7].bid == -9.0 and cm[7].bid_posted == -12.0 and cm[8].bid == 12.0 and cm[8].bid_posted == -8.0, (cm[7].bid, cm[8].bid))
check("return = bid - basis cost on every month that has a bid; none where nobody posted (Nov, Apr, May, Jun)",
      all(close(c.ret, c.bid - c.cost) for c in t2.cols if c.bid is not None) and all(cm[m].bid is None and cm[m].ret is None for m in (11, 4, 5, 6)))
check("best on today's bids: the January shipment (-10 against a break-even of about -17)", rtc.best_column(t2, "ret").month == 1 and cm[1].ret > 7.0, cm[1].ret)
check("Best Basis YTD = the highest bid since Oct 20 and when (Dec: the -9 posted Nov 25 beats today's -14)", cm[12].best_bid == -9.0 and cm[12].best_bid_date == D("2026-11-25"))
check("a bid quoted off a symbol with the archive's wrong year digit still names the right contract (ZSF26 is Jan, ZSH26 Mar)",
      rtc.rebase(6.0, "ZSF26", "H", 2026, futs, ASOF, SOY) == (6.0 + 1010.0 - 1018.0, True) and rtc.rebase(6.0, "ZSH26", "K", 2026, futs, ASOF, SOY) == (6.0 + 1018.0 - 1024.0, True))
check("a Nov bid posted in December has no Nov price left to move it by: not guessed (rebase says None)", rtc.rebase(-18.0, "ZSX26", "F", 2026, {ASOF: {"ZSF27": 1010.0}}, ASOF, SOY) is None)
check("'x' (next November) names the next crop's contract (ZSX27) in a table column", rtc.rebase(-8.0, "x", "F", 2026, futs, ASOF, SOY) == (-8.0 + 1005.0 - 1010.0, True))
sq_aug = rd.shipment_quotes([{"date": D("2026-10-02"), "label": "Aug", "tag": "ZSQ27", "basis": -5.0}, {"date": D("2026-10-02"), "label": "Aug 2027", "tag": "ZSQ27", "basis": -6.0}],
                            2026, SOY)
check("Aug is a soybean shipment column (the best of equals wins) and not a corn one", [(q["month"], q["basis"]) for q in sq_aug] == [(8, -5.0)]
      and rd.shipment_quotes([{"date": D("2026-10-02"), "label": "Aug", "tag": "ZSQ27", "basis": -5.0}], 2026) == [])

print("soybeans: the harvest basis before the weekly bids (October's bids and November's, 4 : 3 like the sheets' weeks, vs Jan)")
FE = {D("2026-10-02"): {"ZSX26": 1000.0, "ZSF27": 1012.0}}
rawh = [{"date": D("2026-10-02"), "label": "FH Oct", "tag": "ZSX26", "basis": -30.0}, {"date": D("2026-10-02"), "label": "LH Oct", "tag": "ZSX26", "basis": -34.0},
        {"date": D("2026-10-02"), "label": "FH Nov", "tag": "ZSF27", "basis": -22.0}, {"date": D("2026-10-02"), "label": "LH Nov", "tag": "ZSF27", "basis": -26.0},
        {"date": D("2026-10-02"), "label": "Dec", "tag": "ZSF27", "basis": -10.0}]
e1 = rd.harvest_estimate(rawh, 2026, D("2026-10-04"), FE, spec=SOY)
check("October (-30, -34 off Nov, moved to Jan by the 12-cent spread: -42, -46 -> -44) and November (-22, -26 off Jan -> -24), weighted 4 : 3 = -35.43",
      e1 is not None and close(e1[0], (4 * -44.0 + 3 * -24.0) / 7) and e1[1] == D("2026-10-02") and e1[2] == ["FH Oct", "LH Oct", "FH Nov", "LH Nov"], e1)
check("only October posted -> October's average (-44); only the Oct/Nov package -> its value (re-based to Jan)",
      close(rd.harvest_estimate(rawh[:2], 2026, D("2026-10-04"), FE, spec=SOY)[0], -44.0)
      and close(rd.harvest_estimate([{"date": D("2026-10-02"), "label": "LH Oct / FH Nov", "tag": "ZSF27", "basis": -15.0}], 2026, D("2026-10-04"), FE, spec=SOY)[0], -15.0))
check("a December bid, or nothing recent (older than 14 days), gives no estimate", rd.harvest_estimate([rawh[4]], 2026, D("2026-10-04"), FE, spec=SOY) is None
      and rd.harvest_estimate(rawh, 2026, D("2026-10-30"), FE, spec=SOY) is None)
te, ee = rd.shipment_table([], rawh, FE, RATE6, D("2026-10-04"), "net", SOY)
check("before any weekly bid the table's harvest basis is that estimate and says so", te.b0_est and te.b0_weeks == 0 and close(te.b0, e1[0]) and ee is not None and len(te.cols) == 10)
t_early, _ = rd.shipment_table(obs[:3], [], futs, RATE6, D("2026-10-21"), "net", SOY)
check("Oct 7-21 weekly bids are quoted off Nov and cannot move to Jan until the Nov/Jan spread is measured (Oct 28): no weekly average yet, "
      "and a past as-of date does not borrow that spread from the future", t_early.b0 is None and t_early.b0_weeks == 0)
t_28, _ = rd.shipment_table(obs[:4], [], futs, RATE6, D("2026-10-28"), "net", SOY)
check("on Oct 28 the spread is in: the four October bids (-20 .. -18.5 off Nov) are moved to Jan by it (-30, -29.5, -29, -28.5) and average -29.25 so far",
      t_28.b0_weeks == 4 and close(t_28.b0, (-30.0 - 29.5 - 29.0 - 28.5) / 4) and not t_28.b0_est, (t_28.b0, t_28.b0_weeks))

print("soybeans: the data layer (years, futures history)")
check("a series' crop years run Oct 1 .. Sep 30 (a September bid belongs to the year before; corn's end July 31)",
      rd.crop_years_in([{"date": D("2027-09-15")}, {"date": D("2027-10-06")}], SOY) == [2026, 2027] and rd.crop_years_in([{"date": D("2027-09-15")}, {"date": D("2027-10-06")}]) == [2027])
sf = rd.load_sheet_futures(root="ZS")
cf = rd.load_sheet_futures()
check("the soybean workbook futures (2005-06 .. Oct 2007) are ZS only and corn's stay ZC only",
      sf.get(D("2005-10-05"), {}).get("ZSX05") == 563.5 and sf.get(D("2007-12-26"), {}).get("ZSF08") == 1220.75 and len(sf) > 100
      and all(s.startswith("ZS") for px in sf.values() for s in px) and cf and all(s.startswith("ZC") for px in cf.values() for s in px))


def make_soy_year(y, base, bump=0.4):
    """52 weekly bids (Oct .. Sep) climbing `bump` a week, flat futures with the LEV curve (Jan -> Jul carry 18)."""
    st = rtc.first_wednesday(y)
    px = {rtc.contract_symbol(l, y, "ZS"): v for l, v in LEV.items()}
    fu = {st + timedelta(days=7 * k): dict(px) for k in range(-2, 56)}
    for d in rtc.roll_dates(y, SOY).values():
        fu.setdefault(d, {}).update(px)
    return [{"date": st + timedelta(days=7 * k), "basis": base + bump * k} for k in range(52)], fu


obs_all, futs_all = [], {}
for i, y in enumerate(range(2014, 2026)):
    o, f = make_soy_year(y, -30.0 + 2 * i, 0.4 + 0.05 * i)
    obs_all += o
    for d, px in f.items():
        futs_all.setdefault(d, {}).update(px)
hist = rd.run_history(obs_all, futs_all, lambda d: 5.0, min_year=2005, spec=SOY)
rows_s = rd.summary_rows(hist, "net")
check("twelve soybean years, all complete (52 weeks to the end of September), the headline carry Jan -> Jul = 18",
      len(hist) == 12 and all(cy.complete and len(cy.weeks) == 52 for cy in hist) and all(close(r["carry"], 18.0) for r in rows_s), [(r["label"], r["weeks"], r["carry"]) for r in rows_s][:3])
obs_26, futs_26 = [], {}
for i, y in enumerate(range(2019, 2027)):                          # 2026-27 starts Wednesday Oct 7: its week 52 is already October
    o, f = make_soy_year(y, -30.0 + 2 * i, 0.4)
    obs_26 += o
    for d, px in f.items():
        futs_26.setdefault(d, {}).update(px)
hist26 = rd.run_history(obs_26, futs_26, lambda d: 5.0, min_year=2005, spec=SOY)
tk26, nm26 = vw._month_ticks(hist26, 55, SOY.horizon)
check("the soybean season's month ticks run Oct .. Sep and stop there (no stray 'Oct' past Sep 30 when the year starts Oct 7)",
      hist26[-1].crop_year == 2026 and nm26[0] == "Oct" and nm26[-1] == "Sep" and max(tk26) <= 51 and "Oct" in vw._month_ticks(hist26, 55)[1][1:], (tk26, nm26))
check("history starts at the spec's first crop year (2005), not corn's 2004", rd.run_history(obs_all, futs_all, lambda d: 5.0, spec=SOY)[0].crop_year == 2014
      and rtc.SOY.min_crop_year == 2005 and rtc.CORN.min_crop_year == 2004)

print("soybeans: the view (page-1 table, headline, charts, the words)")
tv, _ = rd.shipment_table(obs, raw, futs, RATE6, ASOF, "net", SOY)
h = vw.shipment_html(tv, None, "fed funds + 2.25%, as in the rest of this tab")
check("titled 2026-27 with ten month columns Nov 20 .. Aug 20", "2026-27 shipment by month" in h and all(("%s 20" % m) in h for m in ("Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug"))
      and h.count("<th ") == 11, h.count("<th "))
check("the cards say Jan: 'JAN FUTURES' 1010.00, the harvest basis vs Jan, the carry Jan->Mar / Mar->May / May->Jul = 18 (and the Nov/Jan spread is not in it)",
      "JAN FUTURES" in h.upper() and "1010.00" in h and "vs Jan" in h and "vs Dec" not in h and "Jan→Mar +8.00" in h and "Mar→May +6.00" in h and "May→Jul +4.00" in h
      and "+18.0¢" in h and "Nov→Jan" not in h, h[:200])
check("the best month is called out (Jan 20) and the moved / package bids are marked", "Best on today" in h and "ship Jan 20" in h and "BEST NOW" in h and "†" in h and "‡" in h and "moved to Aug terms" in h)
check("the Mar 20 break-even's hover text names the contract the carry rolls to (May) and charges the interest on Jan + b0 for 151 days",
      "futures carry banked rolling to May" in h and ("plus interest %.2f (6.00%% x 151 days on %.2f)" % ((1010.0 + want_b0) * 0.06 * 151 / 360, 1010.0 + want_b0)) in h, h.count("title="))
check("the table is as wide as ten columns need", "min-width:830px" in h)
check("balanced markup and no leftovers", h.count("<table") == h.count("</table>") == 1 and h.count("<tr>") == h.count("</tr>") and h.count("<td") == h.count("</td>")
      and h.count("<div") == h.count("</div>") and "None" not in h and "nan" not in h.lower().replace("fund", "").replace("financ", ""))
he = vw.shipment_html(te, ee, "fed funds + 2.25%")
check("an estimated soybean harvest basis is flagged, says why (October's bids need the Oct 28 spread) and lists the quotes it came from",
      "Estimate." in he and "last Wednesday of October" in he and "FH Oct, LH Oct, FH Nov, LH Nov posted Oct 2" in he and "estimate — average of the posted harvest-period bids" in he)
hc = vw.headline_html(rows_s, "net", SOY)
check("headline: harvest basis vs Jan (the first 7 weekly bids), the carry Jan -> Jul", "vs Jan" in hc and "Jan → Jul" in hc and "+18.0¢" in hc and "average of the first 7 weekly bids" in hc, hc[:300])
check("...and corn's headline still says Dec -> Jul", "vs Dec" in vw.headline_html(rows_s, "net") and "Dec → Jul" in vw.headline_html(rows_s, "net"))
sc = vw.seasonal_chart(hist, "net", logo_uri="data:image/png;base64,iVBORw0KGgo=", spec=SOY).to_dict()
xe = sc["layer"][1]["encoding"]["x"]
check("the season chart runs to week 51 (Sep 30) and every month tick lies on it, Oct first and Sep last",
      xe["scale"]["domain"] == [0, 51.5] and max(xe["axis"]["values"]) <= 51 and xe["axis"]["values"][0] == 0 and '"Oct"' in xe["axis"]["labelExpr"]
      and '"Sep"' in xe["axis"]["labelExpr"], (xe["scale"]["domain"], xe["axis"]["values"]))
check("the caption names the bean chain and Sep 30; corn's is unchanged",
      "Nov→Jan→Mar→May→Jul→Aug→Nov" in vw.method_caption(SOY) and "September 30" in vw.method_caption(SOY) and "bean carry workbooks" in vw.method_caption(SOY)
      and "Dec→Mar→May→Jul→Sep" in vw.method_caption() and "July 31" in vw.method_caption() and "Return to Carry workbooks" in vw.method_caption())
hw = vw.how_it_works(SOY)
check("the explanation: Nov-Aug shipment months on Jan futures, October's bids moved Nov -> Jan, interest from the fifth weekly bid, the sheets' exceptions, the 4 : 3 estimate",
      "Nov-Aug" in hw and "Jan futures" in hw and "moved to Jan by the Nov" in hw and "fifth weekly bid" in hw and "2015-16 to 2017-18" in hw and "weighted 4 : 3" in hw
      and "Nov-Jul" in vw.how_it_works() and "Dec futures" in vw.how_it_works() and "third weekly bid" in vw.how_it_works())

print("soybeans: corn is untouched by the generalisation")
check("corn year still runs to Jul 31 on Dec as the base and 'ZH' keys",
      rtc.CORN.base == "Z" and rtc.CORN.horizon == (7, 31) and rtc.roll_dates(2019)["ZH"] == D("2019-11-27") and set(rtc.roll_spreads({}, 2019)) == {"ZH", "HK", "KN", "NU"})

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
