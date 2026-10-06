"""Return to Carry, the report's front page: the shipment-by-month table (break-even, current and best bids).

    python tests/test_return_to_carry_ship.py

Oracles = the Research Analyst's own page-1 workbooks (`NNCARRYBA.xlsx`): fed their inputs, the engine reproduces the
Basis Cost row they computed — the 2026 Columbus template, and the 2019-20 and 2024-25 St. Louis sheets (which mix
measured and live roll spreads). Nothing here touches a database or the network.
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

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


def costs(t):
    return [c.cost for c in t.cols]


def all_close(got, want, tol=1e-9):
    return len(got) == len(want) and all(close(a, b, tol) for a, b in zip(got, want))


D = date

print("shipment table: the calendar")
check("the report's columns are Nov .. Jul, shipped on the 20th, bought Oct 20",
      rtc.SHIP_MONTHS == (11, 12, 1, 2, 3, 4, 5, 6, 7) and rtc.ship_date(2026, 11) == D(2026, 11, 20)
      and rtc.ship_date(2026, 1) == D(2027, 1, 20) and rtc.ship_date(2026, 7) == D(2027, 7, 20) and rtc.purchase_date(2026) == D(2026, 10, 20))
check("each month is quoted off its futures month: Nov Dec / Dec-Feb Mar / Mar-Apr May / May-Jul Jul",
      [rtc.SHIP_LETTER[m] for m in rtc.SHIP_MONTHS] == ["Z", "H", "H", "H", "K", "K", "N", "N", "N"])
check("the live crop year: August belongs to the new crop, July to the old",
      rtc.shipment_crop_year(D(2026, 8, 21)) == 2026 and rtc.shipment_crop_year(D(2026, 9, 3)) == 2026 and rtc.shipment_crop_year(D(2026, 10, 4)) == 2026
      and rtc.shipment_crop_year(D(2027, 1, 5)) == 2026 and rtc.shipment_crop_year(D(2026, 7, 31)) == 2025)

print("shipment table: the analyst's own page-1 sheets, run through the engine on THEIR inputs")
# 26COLCARRYBA (Columbus 2026-27 template, current date 8/21/26): b0 -9 typed, prime 6.75%, Dec 508.5 / Mar 523.5 / May 530 / Jul 531.5,
# spreads = the current ones (15, 6.5, 1.5) -> the 'Basis Cost' row the workbook computed
F26 = {D(2026, 8, 21): {"ZCZ26": 508.5, "ZCH27": 523.5, "ZCK27": 530.0, "ZCN27": 531.5}}
t26 = rtc.build_shipment_table(2026, D(2026, 8, 21), -9.0, [], F26, lambda d: 6.75, "net")
W26 = [-6.09665625, -18.28696874999997, -15.383624999999995, -12.480281250000019, -16.357906250000042,
       -13.454562499999952, -12.144874999999956, -9.24153124999998, -6.4318437499999845]
check("2026 Columbus template: all nine Basis Cost figures (every contract live, spreads current)", all_close(costs(t26), W26, 1e-9), costs(t26))
check("...and its 'Current Futures' row (Dec 508.5 / Mar 523.5 x3 / May 530 x2 / Jul 531.5 x3)",
      [c.level for c in t26.cols] == [508.5, 523.5, 523.5, 523.5, 530.0, 530.0, 531.5, 531.5, 531.5] and t26.f_dec == 508.5)
# 24STLCARRYBA (St. Louis 2024-25, current date 4/3/25): Z/H 12.25 and H/K 15.25 were measured (Nov 27, Feb 26), K/N 7.5 is still live;
# Jul 465.5; b0 8.14; 7.5%. Futures below are built to give exactly those spreads.
F24 = {D(2024, 11, 27): {"ZCZ24": 400.0, "ZCH25": 412.25}, D(2025, 2, 26): {"ZCH25": 440.0, "ZCK25": 455.25},
       D(2025, 4, 3): {"ZCK25": 458.0, "ZCN25": 465.5}}
t24 = rtc.build_shipment_table(2024, D(2025, 4, 3), 8.14, [], F24, lambda d: 7.5, "net")
W24 = [10.972883333333334, 1.4643833333333305, 4.297266666666644, 7.130150000000015, -5.561116666666692,
       -2.7282333333333213, -7.486733333333348, -4.653849999999977, -1.9123500000000035]
check("2024-25 St. Louis sheet (two measured spreads + one live): all nine Basis Cost figures", all_close(costs(t24), W24, 1e-9), costs(t24))
check("...hung from May: Dec 430.5 / Mar 442.75 / May 458 / Jul 465.5 and the chain says which spreads are frozen",
      t24.levels.anchor == "K" and [t24.levels.levels[k] for k in "ZHKN"] == [430.5, 442.75, 458.0, 465.5]
      and [t24.levels.spreads[k][2] for k in ("ZH", "HK", "KN")] == [True, True, False]
      and [t24.levels.spreads[k][0] for k in ("ZH", "HK", "KN")] == [12.25, 15.25, 7.5], t24.levels)
# 19STLCARRYBA (St. Louis 2019-20, current date 7/22/20): 10.5, 4 and 10 measured; Jul 325.25; b0 21.02; 3.25%
F19 = {D(2019, 11, 27): {"ZCZ19": 100.0, "ZCH20": 110.5}, D(2020, 2, 26): {"ZCH20": 200.0, "ZCK20": 204.0},
       D(2020, 4, 29): {"ZCK20": 300.0, "ZCN20": 310.0}, D(2020, 7, 22): {"ZCN20": 325.25}}
t19 = rtc.build_shipment_table(2019, D(2020, 7, 22), 21.02, [], F19, lambda d: 3.25, "net")
W19 = [21.920509097222222, 12.291969513888887, 13.192478611111085, 14.09298770833334, 10.935399444444442,
       11.83590854166664, 2.7073689583333476, 3.6078780555555454, 4.479338472222253]
check("2019-20 St. Louis sheet (all three measured): all nine Basis Cost figures", all_close(costs(t19), W19, 1e-9), costs(t19))
check("...Dec 300.75 / Mar 311.25 / May 315.25 / Jul 325.25", [t19.levels.levels[k] for k in "ZHKN"] == [300.75, 311.25, 315.25, 325.25])
check("the Current Basis Return the sheet shows for Jul (bid 19 less the 4.48 break-even = 14.52)",
      close(round(19 - t19.cols[8].cost, 2), 14.52))
# the 6-17-20 report (Columbus, Jul 330.25): the Wednesday settlements the weekly sheet measured its spreads from (10.5, 4, 10)
F20 = {D(2019, 11, 27): {"ZCZ19": 362.75, "ZCH20": 373.25}, D(2020, 2, 26): {"ZCH20": 370.5, "ZCK20": 374.5},
       D(2020, 4, 29): {"ZCK20": 304.5, "ZCN20": 314.5}, D(2020, 6, 17): {"ZCN20": 330.25}}
lv = rtc.chain_levels(F20, 2019, D(2020, 6, 17))
check("6-17-20 chain, off the Jul price and the measured spreads: Dec 305.75 / Mar 316.25 / May 320.25 / Jul 330.25 (the PDF types K/N as 8 where the sheet measured 10)",
      lv.anchor == "N" and [lv.levels[k] for k in "ZHKN"] == [305.75, 316.25, 320.25, 330.25], lv.levels)
check("...and the report's own Basis Cost for Nov and Dec-Apr to within a few hundredths (its Dec is 307.75, ours 305.75)",
      all(abs(c.cost - w) < 0.04 for c, w in zip(rtc.build_shipment_table(2019, D(2020, 6, 17), 39.29, [], F20, lambda d: 3.25, "net").cols[:6],
                                                 [40.26, 30.70, 31.67, 32.64, 29.55, 30.52])))

print("shipment table: the chain follows the roll calendar")
FS = {D(2026, 10, 21): {"ZCZ26": 400.0, "ZCH27": 410.0, "ZCK27": 414.0, "ZCN27": 416.0, "ZCU27": 410.0},
      D(2026, 11, 25): {"ZCZ26": 380.0, "ZCH27": 392.0},                           # Z/H measured here: 12
      D(2026, 12, 2): {"ZCH27": 395.0, "ZCK27": 401.0, "ZCN27": 404.0, "ZCU27": 400.0},
      D(2026, 12, 8): {"ZCH27": 396.0, "ZCK27": 402.0, "ZCN27": 405.0, "ZCU27": 401.0}}
c0 = rtc.chain_levels(FS, 2026, D(2026, 10, 21))
check("before any roll the chain is the live contracts at their settlement", c0.anchor == "Z" and [c0.levels[k] for k in "ZHKN"] == [400.0, 410.0, 414.0, 416.0]
      and all(c0.spreads[k][2] is False for k in ("ZH", "HK", "KN")) and c0.spreads["ZH"][0] == 10.0)
c1 = rtc.chain_levels(FS, 2026, D(2026, 11, 25))
check("ON the roll Wednesday the spread is frozen and Dec hangs from Mar (392 - 12 = 380)", c1.anchor == "H" and c1.levels["Z"] == 380.0 and c1.spreads["ZH"] == (12.0, D(2026, 11, 25), True))
c2 = rtc.chain_levels(FS, 2026, D(2026, 12, 8))
check("after it Dec is no longer read off the board: Mar 396 less the frozen 12 = 384; May/Jul/Sep are live",
      c2.anchor == "H" and c2.levels["Z"] == 384.0 and c2.levels["K"] == 402.0 and c2.levels["N"] == 405.0 and c2.spreads["HK"][2] is False
      and c2.spreads["HK"][0] == 6.0 and c2.spreads["ZH"][2] is True)
FS2 = dict(FS)
FS2[D(2026, 11, 25)] = {"ZCZ26": 380.0}                                              # no Mar price on the roll day: it cannot be measured
check("a roll spread that cannot be measured (a contract has no price that day) is not frozen: Dec is read off the board while it trades",
      rtc.chain_levels(FS2, 2026, D(2026, 11, 26)).anchor == "Z")
check("the levels never use a price from the future", rtc.chain_levels(FS, 2026, D(2026, 10, 20)).levels["Z"] is None)

print("shipment table: a bid is moved to the column's futures month by that day's spread")
check("same contract -> as posted", rtc.rebase(17, "ZCH27", "H", 2026, FS, D(2026, 12, 8)) == (17.0, False))
check("Mar quote in the Mar -> May column: 16 + 396 - 402 = 10", rtc.rebase(16, "ZCH27", "K", 2026, FS, D(2026, 12, 8)) == (10.0, True))
check("a package (tag R), a blank and a junk tag are taken as the column's own month",
      all(rtc.rebase(7, tg, "K", 2026, FS, D(2026, 12, 8)) == (7.0, False) for tg in ("R", "", None, "ZSX26", "??")))
check("a single chain letter names this crop year's contract ('h' -> ZCH27)", rtc.rebase(16, "h", "K", 2026, FS, D(2026, 12, 8)) == (10.0, True))
check("a gap of a few days is stepped over (Dec 12 uses Dec 8's settlements); a contract with no price in 5 days, or none at all, gives no answer — not a guess",
      rtc.rebase(16, "ZCH27", "K", 2026, FS, D(2026, 12, 12)) == (10.0, True) and rtc.rebase(16, "ZCH27", "K", 2026, FS, D(2026, 12, 20)) is None
      and rtc.rebase(16, "ZCH27", "K", 2026, {D(2026, 12, 8): {"ZCH27": 396.0}}, D(2026, 12, 8)) is None)

print("shipment table: current bid, best bid and best return (rate 6% to Nov, 5% after; b0 +10; hand-checked)")
RATE = lambda d: 6.0 if d < D(2026, 12, 1) else 5.0
Q = [{"date": D(2026, 10, 21), "month": 12, "basis": 8.0, "tag": "ZCH27", "label": "Dec"},
     {"date": D(2026, 10, 21), "month": 1, "basis": 10.0, "tag": "ZCH27", "label": "JFM"},
     {"date": D(2026, 11, 25), "month": 12, "basis": 14.0, "tag": "ZCH27", "label": "Dec"},
     {"date": D(2026, 12, 2), "month": 12, "basis": 12.0, "tag": "ZCH27", "label": "Dec"},
     {"date": D(2026, 12, 2), "month": 3, "basis": 16.0, "tag": "ZCH27", "label": "JFM"},
     {"date": D(2026, 12, 8), "month": 12, "basis": 11.0, "tag": "ZCH27", "label": "Dec"},
     {"date": D(2026, 12, 8), "month": 3, "basis": 9.0, "tag": "ZCH27", "label": "JFM"},
     {"date": D(2026, 10, 5), "month": 12, "basis": 99.0, "tag": "ZCH27", "label": "Dec"}]          # before the purchase: never a 'best'
tb = rtc.build_shipment_table(2026, D(2026, 12, 8), 10.0, Q, FS, RATE, "net", b0_weeks=7)
dec, jan, mar, nov = tb.cols[1], tb.cols[2], tb.cols[4], tb.cols[0]
cost_dec = 10.0 - 12.0 + (384.0 + 10.0) * 0.05 * 61 / 360                 # at Dec 8: Dec-equivalent 384, 5%, Oct 20 -> Dec 20 = 61 days, 12c carry
check("Basis Cost today (Dec shipment) = b0 - carry + interest on (Dec + b0)", close(dec.cost, cost_dec, 1e-9), (dec.cost, cost_dec))
check("Current Basis = the latest quote (Dec 8: 11); Current Return = bid - cost", dec.bid == 11.0 and dec.bid_date == D(2026, 12, 8) and close(dec.ret, 11.0 - cost_dec, 1e-9))
check("a month quoted only by an older posting (Jan, Oct 21) has no current bid (10-day limit) but keeps its best", jan.bid is None and jan.ret is None
      and jan.best_bid == 10.0 and jan.best_bid_date == D(2026, 10, 21))
check("a month nobody quoted is empty", nov.bid is None and nov.best_bid is None and nov.best_ret is None and nov.cost is not None)
check("the Mar bid is moved to May terms by that day's Mar-May spread (9 + 396 - 402 = 3) and says so",
      mar.bid == 3.0 and mar.bid_posted == 9.0 and mar.bid_label == "JFM" and dec.bid_posted is None)
check("Best Basis YTD = the highest bid since Oct 20 and when (Dec: 14 on Nov 25 — the 99 posted Oct 5 is before the purchase)",
      dec.best_bid == 14.0 and dec.best_bid_date == D(2026, 11, 25))
# the return of each Dec bid against THAT day's break-even (the roll, the price and the rate all moved)
r_oct21 = 8.0 - (10.0 - 10.0 + (400.0 + 10.0) * 0.06 * 61 / 360)           # Oct 21: nothing rolled yet (Dec-Mar spread 10 live), 6%
r_nov25 = 14.0 - (10.0 - 12.0 + (380.0 + 10.0) * 0.06 * 61 / 360)          # Nov 25: Z/H measured, 6% (it is still November)
r_dec02 = 12.0 - (10.0 - 12.0 + (383.0 + 10.0) * 0.05 * 61 / 360)          # Dec 2: Dec-equiv 395 - 12 = 383, 5%
r_dec08 = 11.0 - cost_dec
best = max((r_oct21, D(2026, 10, 21)), (r_nov25, D(2026, 11, 25)), (r_dec02, D(2026, 12, 2)), (r_dec08, D(2026, 12, 8)))
check("Best Return YTD = the best of bid minus THAT day's break-even, with its date (Nov 25)",
      close(dec.best_ret, best[0], 1e-9) and dec.best_ret_date == best[1] == D(2026, 11, 25), (dec.best_ret, dec.best_ret_date, best))
check("...which is not the day of the best bid by accident: the date follows the maximum, not the latest", r_nov25 > r_dec08 and r_nov25 > r_dec02 and r_nov25 > r_oct21)
check("best_column picks the month with the highest return (today / year to date)",
      rtc.best_column(tb, "ret") is dec and rtc.best_column(tb, "best_ret") is dec)

print("shipment table: gross leaves the interest out; no harvest basis, no break-even")
tg = rtc.build_shipment_table(2026, D(2026, 12, 8), 10.0, Q, FS, RATE, "gross", b0_weeks=7)
check("gross Basis Cost = harvest basis - the futures carry only (Dec 10 - 12 = -2; Nov 10; Jul 10 - (405 - 384) = -11)",
      close(tg.cols[1].cost, -2.0) and close(tg.cols[0].cost, 10.0) and close(tg.cols[8].cost, -11.0), costs(tg))
check("gross returns are larger than net for the same bid", tg.cols[1].ret > tb.cols[1].ret and close(tg.cols[1].ret, 11.0 + 2.0))
tn = rtc.build_shipment_table(2026, D(2026, 12, 8), None, Q, FS, RATE, "net")
check("no harvest basis -> no cost, no return, but the bids still show", all(c.cost is None and c.ret is None for c in tn.cols) and tn.cols[1].bid == 11.0)
tr = rtc.build_shipment_table(2026, D(2026, 12, 8), 10.0, Q, FS, None, "net")
check("no interest rate -> the net break-even cannot be built (gross still can)", all(c.cost is None for c in tr.cols))
check("quotes after the as-of date are ignored", rtc.build_shipment_table(2026, D(2026, 12, 3), 10.0, Q, FS, RATE, "net").cols[1].bid == 12.0)
tpre = rtc.build_shipment_table(2026, D(2026, 10, 4), 10.0, [dict(Q[-1], date=D(2026, 10, 2), basis=15.0)], {D(2026, 10, 2): FS[D(2026, 10, 21)]}, RATE, "net")
check("before the purchase date the table still shows today's break-even and bid, and no year-to-date",
      tpre.cols[1].bid == 15.0 and tpre.cols[1].cost is not None and tpre.cols[1].best_bid is None and tpre.cols[1].best_ret is None)

print("shipment labels: which months a posted period covers (the labels the rail rundown really uses)")
LAB = {"Aug": ((8,), "full"), "FH Oct": ((10,), "fh"), "LH Oct": ((10,), "lh"), "LH Oct / FH Nov": ((10, 11), "bundle"),
       "FH Nov": ((11,), "fh"), "Nov": ((11,), "full"), "Nov (no holiday)": ((11,), "full"), "Nov 1-25": ((11,), "window"),
       "FH Dec": ((12,), "fh"), "Dec 1-20": ((12,), "window"), "Dec": ((12,), "full"), "Dec 2026": ((12,), "full"), "LH Nov": ((11,), "lh"),
       "JFM": ((1, 2, 3), "bundle"), "AMJJ": ((4, 5, 6, 7), "bundle"), "AM": ((4, 5), "bundle"), "DJFM": ((12, 1, 2, 3), "bundle"),
       "Jan-Jul": ((1, 2, 3, 4, 5, 6, 7), "bundle"), "Oct-Mar": ((10, 11, 12, 1, 2, 3), "bundle"), "Mar/Apr": ((3, 4), "bundle"),
       "bid FH Oct": ((10,), "fh"), "Split Oct": ((10,), "full"), "MJJ": ((5, 6, 7), "bundle"), "Nov 1-30": ((11,), "full"),
       "Oct 16-31": ((10,), "lh"), "Jan 2027": ((1,), "full"), "January": ((1,), "full"), "Sept": ((9,), "full"),
       # the rest of the corridors' own spellings (probed on the 10/2 rundowns)
       "Jan July": ((1, 2, 3, 4, 5, 6, 7), "bundle"), "Jan thru Jul": ((1, 2, 3, 4, 5, 6, 7), "bundle"), "no holiday JFM": ((1, 2, 3), "bundle"),
       "LH OND": ((10, 11, 12), "bundle"), "FH OND": ((10, 11, 12), "bundle"), "OND": ((10, 11, 12), "bundle"),
       "LH Nov/LH Dec": ((11, 12), "bundle"), "Nov/Dec": ((11, 12), "bundle"), "Jan Feb March": ((1, 2, 3), "bundle"),
       "Oct, Nov, Dec (Mex. Opt.)": ((10, 11, 12), "bundle"), "Dec/Mar": ((12, 3), "bundle"), "k Nov": ((11,), "full"),
       "Sellers Oct": ((10,), "full"), "FTW FH Oct": ((10,), "fh"), "Fob Champ: Oct": ((10,), "full"), "JJ": ((6, 7), "bundle"),
       "Oct 6-10": ((10,), "fh"), "Nov 1-20": ((11,), "window")}
bad = {k: rd.parse_label(k) for k, (m, kind) in LAB.items() if (lambda p: p is None or (p["months"], p["kind"]) != (m, kind))(rd.parse_label(k))}
check("%d real labels read as expected (months and kind)" % len(LAB), not bad, bad)
check("labels that name no month are not mapped: Spot, Nearby, NC 26, New Crop, R, NoBN, Fall, blank, None",
      all(rd.parse_label(x) is None for x in ("Spot", "Nearby", "NC 26", "New Crop", "R", "NoBN", "Fall", "", None)))
check("nor are stray tokens: 'k JFM' / 'k AM' (the old paste parser's leftovers), 'ex AM', a lone letter, notes with no month",
      all(rd.parse_label(x) is None for x in ("k JFM", "k AM", "ex AM", "ex JJ", "ex", "k", "H", "k?", "Return Trip", "No-BNSF", "Next 10 days dlvd Hugton")))
check("a year in the label is read ('Dec 2026'), the span of a package is its length", rd.parse_label("Dec 2026")["year"] == 2026 and rd.parse_label("JFM")["span"] == 3
      and rd.parse_label("Jan-Jul")["span"] == 7)
check("an ambiguous run of initials is not guessed", rd._expand_initials("MMM") is None and rd._expand_initials("J") is None and rd._expand_initials("XYZ") is None)

print("shipment quotes: one bid per (date, month), the month's own quote before a package, the narrowest package otherwise")
d1, d2 = D(2026, 10, 2), D(2026, 12, 9)
raw = [{"date": d1, "label": "FH Dec", "tag": "ZCH27", "basis": 8.0}, {"date": d1, "label": "Dec 1-20", "tag": "ZCH27", "basis": 10.0},
       {"date": d1, "label": "Dec", "tag": "ZCH27", "basis": 14.0}, {"date": d1, "label": "JFM", "tag": "ZCH27", "basis": 17.0},
       {"date": d1, "label": "AMJJ", "tag": "R", "basis": 20.0}, {"date": d1, "label": "Nov", "tag": "ZCZ26", "basis": 16.0},
       {"date": d1, "label": "FH Nov", "tag": "ZCZ26", "basis": -17.0}, {"date": d1, "label": "LH Oct / FH Nov", "tag": "ZCZ26", "basis": -17.0},
       {"date": d1, "label": "Spot", "tag": "ZCZ26", "basis": 5.0}, {"date": d1, "label": "Feb", "tag": "ZCH27", "basis": 19.0},
       {"date": d1, "label": "Jan-Jul", "tag": "R", "basis": 22.0}, {"date": d1, "label": "Jan", "tag": "ZCH27", "basis": None}]
sq = {(q["date"], q["month"]): q for q in rd.shipment_quotes(raw, 2026)}
check("Dec: the whole-month quote beats the window and the half (14, not 10 or 8)", sq[(d1, 12)]["basis"] == 14.0 and sq[(d1, 12)]["label"] == "Dec")
check("Nov: the whole month (16) beats FH Nov and the Oct/Nov package", sq[(d1, 11)]["basis"] == 16.0)
check("Jan, Mar: the narrowest package that covers them (JFM 17, not Jan-Jul 22)", sq[(d1, 1)]["basis"] == 17.0 and sq[(d1, 3)]["basis"] == 17.0 and sq[(d1, 1)]["kind"] == "bundle")
check("Feb: its own quote (19) beats the package", sq[(d1, 2)]["basis"] == 19.0)
check("Apr-Jul: AMJJ (20) over Jan-Jul (22)", all(sq[(d1, m)]["basis"] == 20.0 and sq[(d1, m)]["tag"] == "R" for m in (4, 5, 6, 7)))
check("Spot and no-basis rows add nothing; Oct is not a column", not any(q["month"] == 10 for q in sq.values()) and len(sq) == 9, sorted(m for (_, m) in sq))
raw2 = [{"date": d2, "label": "Dec", "tag": "ZCH27", "basis": 12.0}, {"date": d2, "label": "Nov", "tag": "ZCZ26", "basis": 11.0},
        {"date": d2, "label": "Jan", "tag": "ZCH27", "basis": 15.0}, {"date": D(2027, 3, 3), "label": "Dec", "tag": "ZCZ27", "basis": 9.0},
        {"date": d2, "label": "Jan 2028", "tag": "ZCH28", "basis": 30.0}]
sq2 = {(q["date"], q["month"]): q for q in rd.shipment_quotes(raw2, 2026)}
check("a month's year comes from the posting date: on Dec 9 'Dec' and 'Jan' are this crop year's, but 'Nov' (already past) means next November; 'Dec' posted in March is next year's",
      (d2, 12) in sq2 and (d2, 1) in sq2 and (D(2027, 3, 3), 12) not in sq2 and (d2, 11) not in sq2, sorted(sq2))
check("a year written in a label is respected ('Jan 2028' is not this crop year's January)", sq2[(d2, 1)]["basis"] == 15.0)
tie = rd.shipment_quotes([{"date": d1, "label": "Nov", "tag": None, "basis": 3.0}, {"date": d1, "label": "Nov (no holiday)", "tag": None, "basis": 5.0}], 2026)
check("of equal quotes the higher bid wins", len(tie) == 1 and tie[0]["basis"] == 5.0)

print("shipment quotes: where they come from")
rows_rail = [{"date": "2026-10-02", "market": "CSX Columbus", "commodity": "Corn", "period": "JFM", "futures": "ZCH27", "bid": 17},
             {"date": "2026-10-02", "market": "CSX Columbus", "commodity": None, "period": "Dec", "futures": "ZCH27", "bid": 14},
             {"date": "2026-10-02", "market": "CSX Columbus", "commodity": "Corn", "period": "Nov (no holiday)", "futures": "ZCZ26", "bid": None},
             {"date": "2026-10-02", "market": "CSX Columbus", "commodity": "Freight", "period": "Dec", "futures": None, "bid": 3000},
             {"date": "2026-10-02", "market": "BN COBO", "commodity": "Corn", "period": "Dec", "futures": "ZCH27", "bid": 99}]
qr = rd.quotes_from_rail(rows_rail, "CSX Columbus", "Corn")
check("rail rows: this corridor and commodity only, a blank commodity counts as corn, no bid -> not a quote",
      [(q["label"], q["tag"], q["basis"]) for q in qr] == [("JFM", "ZCH27", 17.0), ("Dec", "ZCH27", 14.0)] and qr[0]["date"] == D(2026, 10, 2))
mk = lambda ts, rows: SimpleNamespace(timestamp=ts, rows=rows)
rw = lambda g, dm, fs, bc, spot=False: SimpleNamespace(grain=g, deliveryMonth=dm, futuresSymbol=fs, basisCents=bc, isSpot=spot)
snaps = [mk("2026-10-01T09:00:00Z", [rw("Corn", "Dec 2026", "ZCH27", 5)]),
         mk("2026-10-02T09:00:00Z", [rw("Corn", "Dec 2026", "ZCH27", 7)]),
         mk("2026-10-02T17:00:00Z", [rw("Corn", "Dec 2026", "ZCH27", 9), rw("Corn", "Jan 2027", "ZCH27", 12), rw("Soybeans", "Jan 2027", "ZSF27", 40),
                                     rw("Corn", "Spot", "ZCZ26", 1, True), rw("Corn", "Mar 2027", "ZCH27", None)])]
qs = rd.quotes_from_snapshots(snaps, "Corn", lambda g: g)
check("snapshots: the newest of each day, forward rows with a basis, this grain only",
      sorted((q["date"].isoformat(), q["label"], q["basis"]) for q in qs) == [("2026-10-01", "Dec 2026", 5.0), ("2026-10-02", "Dec 2026", 9.0), ("2026-10-02", "Jan 2027", 12.0)], qs)

print("futures rows -> the {date: {symbol: cents}} map")
fm = rd.futures_map([{"date": "2026-10-02", "symbol": "ZCZ26", "price_cents": 502.25}, {"date": D(2026, 10, 2), "symbol": "ZCH27 ", "price_cents": "516.75"},
                     {"date": "2026-10-05", "symbol": "ZCZ26", "price_cents": None}, {"date": "bad", "symbol": "ZCZ26", "price_cents": 1.0},
                     {"date": "2026-10-06", "symbol": "", "price_cents": 2.0}, {"date": "2026-10-07", "symbol": "ZCK27", "price_cents": "n/a"}])
check("dates as strings or dates, symbols trimmed, prices as numbers; rows with no price, no usable date or symbol are dropped",
      fm == {D(2026, 10, 2): {"ZCZ26": 502.25, "ZCH27": 516.75}} and rd.futures_map(None) == {} and rd.futures_map([]) == {}, fm)

print("sheet futures: the corn year 2007-08 has its Dec 2007 front contract")
sf = rd.load_sheet_futures()
check("Dec 2007 corn from the analyst's 07colcry sheet, Oct 3 to Nov 28 (the stored settlements lack that front contract): 9 weeks, 344.5 .. 387.25",
      sf[D(2007, 10, 3)]["ZCZ07"] == 344.5 and sf[D(2007, 11, 28)]["ZCZ07"] == 387.25 and sum(1 for px in sf.values() if "ZCZ07" in px) == 9)
sp07 = rtc.roll_spreads(rd.merge_futures(sf, {D(2007, 11, 28): {"ZCH08": 404.5}}), 2007)      # Mar 404.5 is in both her sheet and the stored settlements
check("...so the Dec/Mar roll of 2007-08 measures: Mar 404.5 - Dec 387.25 = 17.25 on Nov 28", sp07["ZH"] == (17.25, D(2007, 11, 28)), sp07["ZH"])

print("harvest basis before the weekly bids: the posted harvest-period quotes")
FE = {D(2026, 10, 2): {"ZCZ26": 502.25, "ZCH27": 516.75}}
rawh = [{"date": D(2026, 10, 2), "label": "FH Oct", "tag": "ZCZ26", "basis": -10.0}, {"date": D(2026, 10, 2), "label": "LH Oct", "tag": "ZCZ26", "basis": -17.0},
        {"date": D(2026, 10, 2), "label": "FH Nov", "tag": "ZCZ26", "basis": -17.0}, {"date": D(2026, 10, 2), "label": "Nov", "tag": "ZCZ26", "basis": 16.0},
        {"date": D(2026, 10, 2), "label": "Dec", "tag": "ZCH27", "basis": 14.0}]
est = rd.harvest_estimate(rawh, 2026, D(2026, 10, 4), FE)
check("the average of FH Oct, LH Oct and FH Nov (-10, -17, -17) = -14.67, dated and labelled",
      est is not None and close(est[0], -44 / 3) and est[1] == D(2026, 10, 2) and est[2] == ["FH Oct", "LH Oct", "FH Nov"], est)
check("a quote off another month is moved to Dec first (FH Oct tagged Mar: -10 + 516.75 - 502.25 = +4.5)",
      close(rd.harvest_estimate([dict(rawh[0], tag="ZCH27")], 2026, D(2026, 10, 4), FE)[0], 4.5))
check("only the one 'LH Oct / FH Nov' package -> its value",
      close(rd.harvest_estimate([{"date": D(2026, 10, 2), "label": "LH Oct / FH Nov", "tag": "ZCZ26", "basis": -15.0}], 2026, D(2026, 10, 4), FE)[0], -15.0))
check("only whole-month Oct and Nov quotes -> their average (a location that posts no halves)",
      close(rd.harvest_estimate([{"date": D(2026, 10, 2), "label": "Oct", "tag": "ZCZ26", "basis": -12.0}, {"date": D(2026, 10, 2), "label": "Nov", "tag": "ZCZ26", "basis": -4.0}],
                                2026, D(2026, 10, 4), FE)[0], -8.0))
check("nothing recent (older than 14 days), nothing posted, or only later months -> no estimate",
      rd.harvest_estimate(rawh, 2026, D(2026, 10, 30), FE) is None and rd.harvest_estimate([], 2026, D(2026, 10, 4), FE) is None
      and rd.harvest_estimate([rawh[4]], 2026, D(2026, 10, 4), FE) is None)

print("shipment_table(): the weekly bids once they exist, the estimate before")
start = rtc.first_wednesday(2026)
obs3 = [{"date": start + timedelta(days=7 * k), "basis": b, "tag": "ZCZ26"} for k, b in enumerate((-10.0, -12.0, -14.0))]
FW = {start + timedelta(days=7 * k): {"ZCZ26": 500.0 + k, "ZCH27": 514.0 + k, "ZCK27": 521.0, "ZCN27": 525.0} for k in range(-1, 6)}
t_obs, e_obs = rd.shipment_table(obs3, rawh, FW, lambda d: 6.0, start + timedelta(days=14), "net")
check("with weekly harvest bids the harvest basis is their average so far (3 weeks), not an estimate",
      t_obs.b0_est is False and t_obs.b0_weeks == 3 and close(t_obs.b0, -12.0) and e_obs is None and t_obs.label == "2026-27")
t_est, e_est = rd.shipment_table([], rawh, FE, lambda d: 6.0, D(2026, 10, 4), "net")
check("without them it is the posted-quote estimate, and says so", t_est.b0_est is True and t_est.b0_weeks == 0 and close(t_est.b0, -44 / 3) and e_est is not None)
t_aug, _ = rd.shipment_table([], [], {}, lambda d: 6.0, D(2026, 8, 21), "net")
check("August builds the NEW crop's table; with no data at all it is empty, not an error", t_aug.crop_year == 2026 and t_aug.b0 is None and all(c.bid is None for c in t_aug.cols))
t_old, _ = rd.shipment_table(obs3, [], FW, lambda d: 6.0, D(2027, 1, 20), "net")
check("later in the crop year the weekly bids are the harvest basis (window = the first 7 weeks) — 3 weeks of data here", t_old.crop_year == 2026 and t_old.b0_weeks == 3)

print("shipment_table(): the user's OWN harvest basis in place of the calculated one")
asof_o = start + timedelta(days=14)
Qo = [{"date": start + timedelta(days=14), "month": 12, "basis": 5.0, "tag": "ZCH27", "label": "Dec"},       # Dec ships off Mar futures: no move needed
      {"date": start + timedelta(days=14), "month": 3, "basis": 8.0, "tag": "ZCK27", "label": "Mar"}]
t_c, _ = rd.shipment_table(obs3, rawh, FW, lambda d: 6.0, asof_o, "net")                      # calculated: average of 3 weekly bids = -12
t_o, e_o = rd.shipment_table(obs3, rawh, FW, lambda d: 6.0, asof_o, "net", b0_override=-20.0)
check("the default is the calculated method (not flagged own; b0_calc is the number it used)",
      t_c.b0_own is False and close(t_c.b0, -12.0) and close(t_c.b0_calc, -12.0) and t_c.b0_weeks == 3)
check("an own harvest basis is the table's: flagged, the calculated one kept beside it, not an estimate, no estimate handed back",
      t_o.b0_own is True and close(t_o.b0, -20.0) and close(t_o.b0_calc, -12.0) and t_o.b0_est is False and t_o.b0_weeks == 0 and e_o is None)
days = [(c.ship - t_o.purchase).days for c in t_o.cols]
want = [(-20.0 - -12.0) * (1 + 0.06 * d / 360) for d in days]                                 # cost = b0 - carry + (F + b0) * rate * days / 360
check("net break-evens move by (own - calculated) x (1 + rate x days / 360): the interest is charged on the futures PLUS the harvest basis",
      all(close(o.cost - c.cost, w, 1e-9) for o, c, w in zip(t_o.cols, t_c.cols, want)), [(o.cost - c.cost, w) for o, c, w in zip(t_o.cols, t_c.cols, want)][:3])
g_c, _ = rd.shipment_table(obs3, rawh, FW, lambda d: 6.0, asof_o, "gross")
g_o, _ = rd.shipment_table(obs3, rawh, FW, lambda d: 6.0, asof_o, "gross", b0_override=-20.0)
check("gross break-evens (carry only) move by exactly own - calculated (-8)", all(close(o.cost - c.cost, -8.0) for o, c in zip(g_o.cols, g_c.cols)))
t_cq = rtc.build_shipment_table(2026, asof_o, -12.0, Qo, FW, lambda d: 6.0, "net")
t_oq = rtc.build_shipment_table(2026, asof_o, -20.0, Qo, FW, lambda d: 6.0, "net", b0_own=True, b0_calc=-12.0)
check("the same bid returns more against a lower harvest basis (return = bid - break-even): Dec +5 -> by exactly the break-even's move",
      close(t_oq.cols[1].ret - t_cq.cols[1].ret, -(t_oq.cols[1].cost - t_cq.cols[1].cost)) and t_oq.cols[1].ret > t_cq.cols[1].ret and t_oq.cols[1].bid == t_cq.cols[1].bid == 5.0)
t_ee, e_ee = rd.shipment_table([], rawh, FE, lambda d: 6.0, D(2026, 10, 4), "net", b0_override=-5.0)
check("before the weekly bids exist the own number replaces the estimate (the estimate is still what b0_calc shows)",
      close(t_ee.b0, -5.0) and t_ee.b0_est is False and e_ee is None and close(t_ee.b0_calc, -44 / 3) and t_ee.b0_own)
t_nn, _ = rd.shipment_table([], [], {}, lambda d: 6.0, D(2026, 8, 21), "net", b0_override=-5.0)
check("with no data at all the own number still stands (no break-even yet: there are no futures), and there is no calculated one to show",
      close(t_nn.b0, -5.0) and t_nn.b0_own and t_nn.b0_calc is None and all(c.cost is None for c in t_nn.cols))
t_zero, _ = rd.shipment_table(obs3, rawh, FW, lambda d: 6.0, asof_o, "net", b0_override=0.0)
check("an own harvest basis of 0 is a number, not 'unset'", t_zero.b0_own is True and t_zero.b0 == 0.0)

print("shipment view: the table as the user sees it")
tv = rtc.build_shipment_table(2026, D(2026, 12, 8), 10.0, Q, FS, RATE, "net", b0_weeks=7)
h = vw.shipment_html(tv, None, "fed funds + 2.25%, as in the rest of this tab")
check("titled with the crop year and the measure", "2026-27 shipment by month" in h and "net of interest" in h and "bought Oct 20" in h)
check("nine month columns Nov 20 .. Jul 20", all(("%s 20" % m) in h for m in ("Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul")))
check("the report's rows are there", all(r in h for r in ("Futures month", "Futures (current)", "Basis cost (break-even)", "Current basis", "Return today", "Best basis YTD", "Best return YTD")))
check("four cards: harvest basis, Dec futures, interest (with where the rate comes from), futures carry",
      all(t in h.upper() for t in ("HARVEST BASIS", "DEC FUTURES", "INTEREST", "FUTURES CARRY")) and "fed funds + 2.25%, as in the rest of this tab" in h and "384.00" in h)
check("the harvest basis card says it is the 7-week average", "average of the first 7 weekly bids" in h and "Estimate." not in h)
check("the best month on today's bids is called out and tagged in the header", "Best on today" in h and "BEST NOW" in h and h.count("BEST NOW") == 1)
check("a bid moved to the column's futures month is marked and explained", "†" in h and "moved to May terms" in h)
check("a package is marked", "‡" in h and "a package that covers this month" in h)
check("the footnote explains the asterisk, the dagger and the best return", "* Futures spreads measured the last Wednesday" in h and "† A bid quoted off" in h and "Best return = the bid less that day" in h)
check("the best bid carries its date underneath", "Nov 25" in h)
check("no leftovers from a missing value", "None" not in h and "nan" not in h.lower().replace("fund", "").replace("financ", ""))
check("balanced markup", h.count("<table") == h.count("</table>") == 1 and h.count("<tr>") == h.count("</tr>") and h.count("<td") == h.count("</td>")
      and h.count("<th ") == h.count("</th>") and h.count("<div") == h.count("</div>"),
      {t: (h.count("<" + t), h.count("</" + t + ">")) for t in ("table", "tr", "td", "div")})
check("a bid that is both a package and moved to another futures month carries both marks (Mar: 'JFM' moved to May)", "3†‡" in h or "+3†‡" in h)
hg = vw.shipment_html(rtc.build_shipment_table(2026, D(2026, 12, 8), 10.0, Q, FS, RATE, "gross", b0_weeks=7), None, "x")
check("gross is labelled and its row says carry only", "gross (before interest)" in hg and "Basis cost (carry only)" in hg and "break-even)" not in hg.split("Basis cost")[1][:30])
check("the gross view does not show an interest rate it is not charging", "not charged in the gross view" in hg and "5.00%" not in hg and "5.00%" in h)
he = vw.shipment_html(t_est, e_est, "fed funds + 2.25%")
check("an estimated harvest basis is flagged, with the quotes it came from",
      "Estimate." in he and "FH Oct, LH Oct, FH Nov posted Oct 2" in he and "estimate — average of the posted harvest-period bids" in he)
hx = vw.shipment_html(t_est, (-14.7, D(2026, 10, 2), ["<b>x</b> & y"]), "")
check("labels that come from the database are escaped before they reach the page", "&lt;b&gt;x&lt;/b&gt; &amp; y" in hx and "<b>x</b> & y" not in hx)
hp = vw.shipment_html(t_obs, None, "bank prime")
check("a partial weekly average says how many weeks it has", "average of the first 3 weekly bids so far" in hp and "the average of the first 7, so it can still move" in hp)
h1 = vw.shipment_html(rtc.build_shipment_table(2026, D(2026, 12, 8), 10.0, Q, FS, RATE, "net", b0_weeks=1), None, "x")
check("...and one bid is 'the first weekly bid', not 'the average of the first 1'", "the first weekly bid so far, vs Dec" in h1 and "The harvest basis is the first weekly bid so far" in h1 and "average of the first 1" not in h1)
hn = vw.shipment_html(rtc.build_shipment_table(2026, D(2026, 12, 8), None, [], FS, RATE, "net"), None, "")
check("no harvest basis -> it says so and shows dashes, not numbers", "not known yet" in hn and "—" in hn)
hq = vw.shipment_html(rtc.build_shipment_table(2026, D(2026, 12, 8), 10.0, [], FS, RATE, "net", b0_weeks=7), None, "")
check("a break-even with no bids says no forward bids were posted", "No forward bids were posted" in hq and "BEST NOW" not in hq)
check("the break-even cell explains itself on hover: harvest basis, carry, interest", "harvest basis +10.00" in h and "futures carry banked rolling to Mar" in h and "interest" in h)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
