"""Return to Carry: the engine must reproduce the Research Analyst's yearly workbooks and weekly report.

    python tests/test_return_to_carry.py

Oracle = the analyst's own `NNcolcry.xlsx` sheets (tests/fixtures/rtc_sheets.json holds their weekly inputs AND the
cached 'Appreciation less int cost' they computed for 2015-16, 2019-20, 2020-21) and the 6-17-20 report's break-even
row. Nothing here touches a database or the network.
"""
import json
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import return_to_carry as rtc          # noqa: E402
import return_to_carry_data as rd      # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


D = date.fromisoformat

print("return_to_carry: the calendar")
check("first Wednesday of October (the first row of every sheet): 2025 -> Oct 1, 2019 -> Oct 2, 2026 -> Oct 7",
      rtc.first_wednesday(2025) == date(2025, 10, 1) and rtc.first_wednesday(2019) == date(2019, 10, 2)
      and rtc.first_wednesday(2026) == date(2026, 10, 7))
check("week_index: that Wednesday is week 0; a holiday-shifted Tuesday/Thursday still counts for its week",
      rtc.week_index(date(2025, 10, 1), 2025) == 0 and rtc.week_index(date(2025, 10, 8), 2025) == 1
      and rtc.week_index(date(2025, 10, 7), 2025) == 1 and rtc.week_index(date(2025, 10, 9), 2025) == 1
      and rtc.week_index(date(2025, 9, 24), 2025) == -1)
check("crop year starts Sep 1 and is labelled 2025-26", rtc.crop_year_of(date(2026, 8, 31)) == 2025 and rtc.crop_year_of(date(2026, 9, 1)) == 2026
      and rtc.crop_label(2025) == "2025-26" and rtc.crop_label(1999) == "1999-00")
check("contract symbols: Dec is the crop year's own, the rest fall in the next",
      rtc.contract_symbol("Z", 2025) == "ZCZ25" and rtc.contract_symbol("H", 2025) == "ZCH26" and rtc.contract_symbol("U", 2025) == "ZCU26")
check("letter_of reads the month letter; soybeans' X and junk are not in the corn chain",
      rtc.letter_of("ZCH26") == "H" and rtc.letter_of("ZSX26") is None and rtc.letter_of(None) is None and rtc.letter_of("") is None)
check("last Wednesday before the expiring month: Nov 26, Feb 25, Apr 29, Jun 24 (2025-26 rule)",
      rtc.last_wednesday_before(2025, 12) == date(2025, 11, 26) and rtc.last_wednesday_before(2026, 3) == date(2026, 2, 25)
      and rtc.last_wednesday_before(2026, 5) == date(2026, 4, 29) and rtc.last_wednesday_before(2026, 7) == date(2026, 6, 24))
rd22 = rtc.roll_dates(2022)
check("roll dates follow the rule where the sheets did (2022-23)", rd22 == {"ZH": date(2022, 11, 30), "HK": date(2023, 2, 22), "KN": date(2023, 4, 26), "NU": date(2023, 6, 28)}, rd22)
check("...and the sheets' own exceptions are kept (2020-21 K/N a week early; 2025-26 N/U on Jul 1)",
      rtc.roll_dates(2020)["KN"] == date(2021, 4, 21) and rtc.roll_dates(2025)["NU"] == date(2026, 7, 1) and rtc.roll_dates(2020)["ZH"] == date(2020, 11, 25))
check("infer_tag: the contract moves to Mar the first week of Dec, May in March, Jul the last week of April, Sep in July",
      [rtc.infer_tag(d) for d in (date(2025, 10, 1), date(2025, 11, 26), date(2025, 12, 3), date(2026, 2, 25), date(2026, 3, 4),
                                   date(2026, 4, 22), date(2026, 4, 29), date(2026, 6, 24), date(2026, 7, 1), date(2026, 8, 26))]
      == ["Z", "Z", "H", "H", "K", "K", "N", "N", "U", "U"])

print("return_to_carry: futures lookup steps over weekends and gaps")
F = {date(2025, 11, 26): {"ZCZ25": 440.0}, date(2025, 11, 28): {"ZCZ25": 441.0}}
check("exact day", rtc.price_on(F, date(2025, 11, 26), "ZCZ25") == (440.0, date(2025, 11, 26)))
check("a Sunday uses the Friday before; a day with nothing within 5 days is None",
      rtc.price_on(F, date(2025, 11, 30), "ZCZ25") == (441.0, date(2025, 11, 28)) and rtc.price_on(F, date(2025, 12, 9), "ZCZ25") == (None, None)
      and rtc.price_on(F, date(2025, 11, 26), "ZCH26") == (None, None))

print("return_to_carry: the break-even row of the 6-17-20 report (Columbus: b0 39.29, Dec 307.75, 3.25%, bought 10/20/19)")
sp = {"Z": 0.0, "H": 10.5, "K": 14.5, "N": 22.5}
ship = [(date(2019, 11, 20), "Z"), (date(2019, 12, 20), "H"), (date(2020, 1, 20), "H"), (date(2020, 2, 20), "H"), (date(2020, 3, 20), "K"),
        (date(2020, 4, 20), "K"), (date(2020, 5, 20), "N"), (date(2020, 6, 20), "N"), (date(2020, 7, 20), "N")]
want = [40.26, 30.70, 31.67, 32.64, 29.55, 30.52, 23.46, 24.43, 25.37]
got = [rtc.basis_cost(39.29, 307.75, 3.25, date(2019, 10, 20), d, sp[t]) for d, t in ship]
check("all nine Basis Cost figures to the cent", all(abs(g - w) < 0.005 for g, w in zip(got, want)), [round(g, 2) for g in got])
check("current return Jun/Jul = 31 - cost = 6.57 / 5.63", close(round(31 - got[7], 2), 6.57) and close(round(31 - got[8], 2), 5.63))

print("return_to_carry: the analyst's yearly workbooks, run through the engine on THEIR inputs")
FIX = json.load(open(os.path.join(HERE, "fixtures", "rtc_sheets.json"), encoding="utf-8"))


def run_sheet(key):
    f = FIX[key]
    obs = [{"date": D(d), "basis": b, "tag": t} for d, b, t in f["obs"]]
    futs = {D(d): {s: p for s, p in v.items()} for d, v in f["futs"].items()}
    futs = {}
    cy = f["crop_year"]
    for d, v in f["futs"].items():
        futs[D(d)] = dict(v)
    prime = sorted((D(d), v) for d, v in f["prime"].items())

    def rate_on(d):
        last = prime[0][1]
        for x, v in prime:
            if x <= d:
                last = v
        return last
    res = rtc.build_crop_year(obs, futs, cy, rate_on)
    q = {D(d): v for d, v in f["q"].items()}
    diffs = [(w.date, w.net - q[w.date]) for w in res.weeks if w.net is not None and w.date in q]
    return res, diffs, f


res19, d19, f19 = run_sheet("2019-20")
check("2019-20: harvest basis 39.2857 (the report's 39.29 = weeks 3-9 of the sheet)", close(res19.b0, f19["b0_sheet"], 1e-9) and res19.b0_weeks == 7, res19.b0)
check("2019-20: roll spreads Z/H 10.5, H/K 4.0 as in the report (K/N 10.0 on the sheet's own futures)",
      close(res19.spreads["ZH"][0], 10.5) and close(res19.spreads["HK"][0], 4.0) and close(res19.spreads["KN"][0], 10.0))
check("2019-20: every one of 42 weekly returns equals the sheet's, to 0.005 cents",
      len(d19) >= 40 and max(abs(x) for _, x in d19) < 0.005, max(abs(x) for _, x in d19))
check("2019-20: best return 9.09 on Dec 4 (the report's 'Best Basis Return YTD' for Nov-Dec)", close(round(res19.best["net"].net, 2), 9.09) and res19.best["net"].date == date(2019, 12, 4))
res15, d15, f15 = run_sheet("2015-16")
check("2015-16: a 6-week harvest window (the sheet averaged 6 weeks) -> b0 20.333", close(res15.b0, f15["b0_sheet"], 1e-9) and res15.b0_weeks == 6, res15.b0)
check("2015-16: every weekly return equals the sheet's", len(d15) >= 40 and max(abs(x) for _, x in d15) < 0.005, max(abs(x) for _, x in d15))
res20, d20, f20 = run_sheet("2020-21")
check("2020-21: b0 16.286; the sheet's K/N was measured Apr 21 (a week early) and so is ours",
      close(res20.b0, f20["b0_sheet"], 1e-9) and res20.spreads["KN"][1] == date(2021, 4, 21) and close(res20.spreads["KN"][0], -19.0))
check("2020-21: weeks the sheet left without a bid still accrue interest (weekly grid) -> all 38 within 0.3c of the sheet",
      len(d20) >= 36 and max(abs(x) for _, x in d20) < 0.3, max(abs(x) for _, x in d20))

print("return_to_carry: how the weekly return is built (synthetic year)")
START = rtc.first_wednesday(2025)                       # 2025-10-01
FUT = {}
for k in range(0, 45):
    d = START + timedelta(days=7 * k)
    FUT[d] = {"ZCZ25": 400.0, "ZCH26": 410.0, "ZCK26": 414.0, "ZCN26": 416.0, "ZCU26": 410.0}
ROLLS = rtc.roll_dates(2025)
for d in ROLLS.values():                                # make sure every roll Wednesday has prices
    FUT[d] = {"ZCZ25": 400.0, "ZCH26": 410.0, "ZCK26": 414.0, "ZCN26": 416.0, "ZCU26": 410.0}
OBS = [{"date": START + timedelta(days=7 * k), "basis": 10.0} for k in range(0, 43)]      # flat +10 all year, tags inferred
rate = lambda d: 5.2                                    # 5.2% -> 0.1 cent-per-100 per week: /5200 makes the arithmetic clean
syn = rtc.build_crop_year(OBS, FUT, 2025, rate)
check("flat +10 bids: harvest basis is 10 and 7 weeks went into it", close(syn.b0, 10.0) and syn.b0_weeks == 7)
check("the return starts at the THIRD weekly bid (the first two weeks have none)", [w.net for w in syn.weeks[:2]] == [None, None] and syn.weeks[2].net is not None)
w2, w3 = syn.weeks[2], syn.weeks[3]
check("interest = cash x running sum of weekly rate / 5200: week 3 is one week's, week 4 two weeks'",
      close(w2.interest, 410.0 * 5.2 / 5200) and close(w3.interest, 410.0 * 10.4 / 5200), (w2.interest, w3.interest))
check("gross = basis - b0 + banked carry; net = gross - interest", all(close(w.net, w.gross - w.interest) for w in syn.weeks if w.net is not None)
      and close(w2.gross, 0.0))
h = next(w for w in syn.weeks if w.tag == "H")
check("after the Nov roll the bid is quoted off Mar and banks the Dec->Mar spread (410 - 400 = 10)", close(h.carry, 10.0) and close(h.gross, 10.0))
n = next(w for w in syn.weeks if w.tag == "N")
check("by May-Jun it has banked Z/H 10 + H/K 4 + K/N 2 = 16", close(n.carry, 16.0), n.carry)
check("the Dec-Jul futures carry (Z/H + H/K + K/N) is 16", close(syn.dec_jul_carry, 16.0))
check("no bid is read after July 31 (the horizon is 'to July')", all(w.date <= date(2026, 7, 31) for w in syn.weeks))

gap = [o for o in OBS if o["date"] != START + timedelta(days=7 * 10)]                    # one week with no bid
syn_gap = rtc.build_crop_year(gap, FUT, 2025, rate)
a = next(w for w in syn.weeks if w.idx == 12)
b = next(w for w in syn_gap.weeks if w.idx == 12)
check("a week with no bid has no return, but interest still accrued for it: the later weeks' interest is unchanged",
      close(a.interest, b.interest) and close(a.net, b.net) and 10 not in [w.idx for w in syn_gap.weeks])

noprice = {d: dict(v) for d, v in FUT.items()}
for d in list(noprice):
    noprice[d].pop("ZCH26", None)
sy2 = rtc.build_crop_year(OBS, noprice, 2025, rate)
check("a missing roll price: the weeks that need it have no return (None), the earlier ones are untouched",
      sy2.spreads["ZH"] is None and all(w.net is None for w in sy2.weeks if w.tag != "Z") and all(w.net is not None for w in sy2.weeks if w.tag == "Z" and w.idx >= 2))
check("a missing futures price on a bid's own day: that week keeps its basis, loses the interest-based return",
      rtc.build_crop_year(OBS, {}, 2025, rate).weeks[5].net is None and rtc.build_crop_year(OBS, {}, 2025, rate).weeks[5].basis == 10.0)

best = syn.best["net"]
check("best = the highest weekly net; the earlier week wins a tie",
      all(best.net >= w.net - 1e-12 for w in syn.weeks if w.net is not None))
flat_tie = [{"date": START + timedelta(days=7 * k), "basis": 5.0 if k < 7 else 5.0 + (3.0 if k in (8, 9) else 0.0)} for k in range(0, 10)]
ft = rtc.build_crop_year(flat_tie, {d: {"ZCZ25": 400.0} for d in [START + timedelta(days=7 * k) for k in range(0, 10)]}, 2025, lambda d: 0.0)
check("on a zero-interest tie the EARLIER week is the best", ft.best["net"].idx == 8 and close(ft.best["net"].net, 3.0), (ft.best["net"].idx, ft.best["net"].net))
check("the best summer basis = the highest bid quoted off July (the report's blue triangle)",
      syn.summer is not None and syn.summer.tag == "N" and close(syn.summer.basis, 10.0))

print("return_to_carry: the user's own harvest basis replaces the calculated one (synthetic year: calculated b0 = 10)")
own = rtc.build_crop_year(OBS, FUT, 2025, rate, b0_override=4.0)
check("the default is the calculated method: no override -> b0 is the average, b0_calc equals it, nothing is flagged own",
      close(syn.b0, 10.0) and close(syn.b0_calc, 10.0) and syn.b0_own is False and syn.b0_weeks == 7)
check("an override is the harvest basis every return is measured from; the calculated average is kept beside it",
      close(own.b0, 4.0) and own.b0_own is True and close(own.b0_calc, 10.0) and own.b0_weeks == 7 and own.b0_dates == syn.b0_dates)
paired = [(a, b) for a, b in zip(own.weeks, syn.weeks) if b.net is not None]
check("every gross and net return moves by calculated - own (+6): the interest runs on each week's cash price, not on the harvest basis",
      len(paired) >= 38 and all(close(a.gross - b.gross, 6.0) and close(a.net - b.net, 6.0) and close(a.interest, b.interest) and a.idx == b.idx and a.date == b.date
                                for a, b in paired), len(paired))
check("so the best week is the same week, 6 cents higher, on both measures; the summer basis and the futures carry are untouched",
      own.best["net"].idx == syn.best["net"].idx and close(own.best["net"].net - syn.best["net"].net, 6.0)
      and own.best["gross"].idx == syn.best["gross"].idx and close(own.best["gross"].gross - syn.best["gross"].gross, 6.0)
      and own.summer.idx == syn.summer.idx and close(own.dec_jul_carry, syn.dec_jul_carry))
same = rtc.build_crop_year(OBS, FUT, 2025, rate, b0_override=syn.b0)
check("an override equal to the calculated value changes no number (it is only flagged own)",
      same.b0_own is True and all(close(a.net, b.net) for a, b in zip(same.weeks, syn.weeks) if b.net is not None))
late = [o for o in OBS if rtc.week_index(o["date"], 2025) >= 8]                           # nothing in the harvest window: no calculated average
calc_none = rtc.build_crop_year(late, FUT, 2025, rate)
own_late = rtc.build_crop_year(late, FUT, 2025, rate, b0_override=4.0)
check("a location with no bid in the harvest window has no calculated basis and no returns — its own harvest basis gives it returns",
      calc_none.b0 is None and calc_none.b0_calc is None and all(w.net is None for w in calc_none.weeks)
      and own_late.b0_calc is None and close(own_late.b0, 4.0) and own_late.b0_own and any(w.net is not None for w in own_late.weeks)
      and close(own_late.weeks[-1].gross, 10.0 - 4.0 + own_late.weeks[-1].carry))
check("the weekly return is bid - the harvest basis + the banked carry, for any harvest basis (a negative one too)",
      all(close(w.gross, w.basis - (-25.0) + w.carry) for w in rtc.build_crop_year(OBS, FUT, 2025, rate, b0_override=-25.0).weeks if w.gross is not None))
soy_off = rtc.build_crop_year([{"date": date(2025, 10, 1) + timedelta(days=7 * k), "basis": -30.0, "tag": "F"} for k in range(0, 12)], {}, 2025, rate, rtc.SOY, b0_override=-10.0)
check("soybeans take the same override (vs Jan)", soy_off.b0_own and close(soy_off.b0, -10.0) and close(soy_off.b0_calc, -30.0), (soy_off.b0, soy_off.b0_calc))

print("return_to_carry: the user's own window for the calculated harvest basis (synthetic year: the bid climbs 0.5 cents a week, so week k reads 10 + 0.5 k)")
climb = [{"date": START + timedelta(days=7 * k), "basis": 10.0 + 0.5 * k} for k in range(0, 43)]
c_def = rtc.build_crop_year(climb, FUT, 2025, rate)
check("the report's window: the first 7 weeks, 10 + 0.5 x 3 = 11.5; no window of the user's recorded", close(c_def.b0, 11.5) and c_def.b0_weeks == 7 and c_def.b0_window is None)
c_same = rtc.build_crop_year(climb, FUT, 2025, rate, harvest_window=(0, 7))
check("a window of weeks 1 to 7 reproduces the report's exactly: the same average and every week's return, interest and carry",
      close(c_same.b0, c_def.b0) and c_same.b0_window == (0, 7) and [(w.net, w.gross, w.interest, w.carry) for w in c_same.weeks] == [(w.net, w.gross, w.interest, w.carry) for w in c_def.weeks])
c_w = rtc.build_crop_year(climb, FUT, 2025, rate, harvest_window=(3, 4))
check("weeks 4 to 7 average those four bids ((11.5 + 12 + 12.5 + 13) / 4 = 12.25); recorded as the window, not flagged own, and 'calculated' is that average",
      close(c_w.b0, 12.25) and c_w.b0_window == (3, 4) and c_w.b0_weeks == 4 and not c_w.b0_own and close(c_w.b0_calc, 12.25)
      and c_w.b0_dates == (START + timedelta(days=21), START + timedelta(days=42)), (c_w.b0, c_w.b0_dates))
paired = [(a, b) for a, b in zip(c_w.weeks, c_def.weeks) if b.net is not None]
check("only the average moved: the interest, the carry and the weeks are the same, every return shifts by the same amount (11.5 - 12.25), the best week is the same week",
      len(paired) >= 38 and all(a.idx == b.idx and close(a.interest, b.interest) and close(a.carry, b.carry) and close(a.net - b.net, c_def.b0 - c_w.b0) for a, b in paired)
      and c_w.best["net"].idx == c_def.best["net"].idx, len(paired))
check("a window that starts after the interest does keeps the returns of the weeks before it (they are measured from the window's average, as the report's are from its own)",
      [w.net for w in c_w.weeks[:2]] == [None, None] and c_w.weeks[2].net is not None and c_w.weeks[2].idx == 2)
c_one = rtc.build_crop_year(climb, FUT, 2025, rate, harvest_window=(4, 1))
check("a single week is that week's bid (week 5 reads 12.0)", close(c_one.b0, 12.0) and c_one.b0_weeks == 1 and c_one.b0_dates[0] == c_one.b0_dates[1])
part_w = rtc.build_crop_year(climb[:5], FUT, 2025, rate, harvest_window=(2, 5))              # bids to week 5 only; window = weeks 3 to 7
check("a window that has not finished is the average so far (3 of its 5 bids: weeks 3, 4, 5 = 11.0, 11.5, 12.0)", part_w.b0_weeks == 3 and close(part_w.b0, 11.5) and part_w.b0_window == (2, 5))
none_w = rtc.build_crop_year(climb[:3], FUT, 2025, rate, harvest_window=(5, 3))
check("no bid in the window yet: no harvest basis, no returns and no crash", none_w.b0 is None and all(w.net is None for w in none_w.weeks) and none_w.b0_window == (5, 3))
y19 = [{"date": rtc.first_wednesday(2019) + timedelta(days=7 * k), "basis": float(k)} for k in range(0, 43)]
check("a window of the user's replaces the analyst's year-specific one: 2019-20's sheet averaged weeks 3-9 (bids 2..8 = 5.0); the user's weeks 1-7 are bids 0..6 = 3.0",
      close(rtc.build_crop_year(y19, {}, 2019, rate).b0, 5.0) and close(rtc.build_crop_year(y19, {}, 2019, rate, harvest_window=(0, 7)).b0, 3.0))
c_wo = rtc.build_crop_year(climb, FUT, 2025, rate, b0_override=4.0, harvest_window=(3, 4))
check("an own harvest basis still wins; the window's average is what 'calculated' means beside it", close(c_wo.b0, 4.0) and c_wo.b0_own and close(c_wo.b0_calc, 12.25))
check("the window is kept when the study is limited (Ship by)", rtc.limit_crop_year(c_w, 3).b0_window == (3, 4))

print("return_to_carry: a September bid quoted off September futures is rolled to Dec at the spread of the last day before first notice (corn)")
check("the roll day: the weekday before first notice (the last business day of August) — 2026 Fri Aug 28 (notice Mon Aug 31), 2025 Thu Aug 28 (notice Fri Aug 29), 2027 Mon Aug 30, 2020 Fri Aug 28",
      rtc.sept_roll_day(2026) == date(2026, 8, 28) and rtc.sept_roll_day(2025) == date(2025, 8, 28) and rtc.sept_roll_day(2027) == date(2027, 8, 30)
      and rtc.sept_roll_day(2020) == date(2020, 8, 28))
check("...always a weekday, and always before the notice day, 1996 to 2040", all(rtc.sept_roll_day(y).weekday() < 5 and rtc.sept_roll_day(y) < rtc._last_business_day(y, 8) for y in range(1996, 2041)))
FS26 = {date(2026, 8, 28): {"ZCU26": 450.0, "ZCZ26": 470.0}}
check("the spread is Dec - Sep on that day (+20), with the day", rtc.sept_to_dec_spread(FS26, 2026) == (20.0, date(2026, 8, 28)), rtc.sept_to_dec_spread(FS26, 2026))
check("it steps back over a gap, as every futures lookup does (only Aug 27 has prices)", rtc.sept_to_dec_spread({date(2026, 8, 27): {"ZCU26": 450.0, "ZCZ26": 462.5}}, 2026)[0] == 12.5)
check("no price for one of the two contracts: no spread", rtc.sept_to_dec_spread({date(2026, 8, 28): {"ZCZ26": 470.0}}, 2026) is None and rtc.sept_to_dec_spread({}, 2026) is None)
check("an inverted market gives a negative spread (Sep over Dec)", rtc.sept_to_dec_spread({date(2026, 8, 28): {"ZCU26": 500.0, "ZCZ26": 484.25}}, 2026)[0] == -15.75)
S0 = rtc.first_wednesday(2026)                                                   # Oct 7 2026: week index -4 = Sep 9, -3 = Sep 16, -2 = Sep 23, -1 = Sep 30
tags = {-4: "ZCU26", -3: "ZCU26", -2: "ZCZ26", -1: None, 0: "ZCZ26"}
bases = {-4: 30.0, -3: 28.0, -2: 8.0, -1: 6.0, 0: 4.0}
obs_s = [{"date": S0 + timedelta(days=7 * k), "basis": b, "tag": tags[k]} for k, b in bases.items()]
cy_s = rtc.build_crop_year(obs_s, FS26, 2026, rate, harvest_window=(-4, 5))
check("a September-start window: the two bids quoted off CU are rolled (30 - 20 = 10, 28 - 20 = 8); the CZ bid and the untagged one are left alone: (10 + 8 + 8 + 6 + 4) / 5 = 7.2",
      close(cy_s.b0, 7.2) and cy_s.b0_weeks == 5 and cy_s.b0_dates == (S0 - timedelta(days=28), S0) and cy_s.b0_window == (-4, 5), (cy_s.b0, cy_s.b0_weeks))
cy_n = rtc.build_crop_year(obs_s, {}, 2026, rate, harvest_window=(-4, 5))
check("when the spread cannot be measured the CU bids are left out, not guessed: (8 + 6 + 4) / 3 = 6.0 from 3 bids", close(cy_n.b0, 6.0) and cy_n.b0_weeks == 3, (cy_n.b0, cy_n.b0_weeks))
cy_z = rtc.build_crop_year([dict(o, tag=None) for o in obs_s], FS26, 2026, rate, harvest_window=(-4, 5))
check("a bid with no tag is taken as quoted off Dec (every untagged fall bid, and the many locations that quote their fall and new-crop bids off CZ): no roll",
      close(cy_z.b0, (30 + 28 + 8 + 6 + 4) / 5.0) and cy_z.b0_weeks == 5)
check("the report's default window never reaches September, so a CU-tagged September bid cannot change it", close(rtc.build_crop_year(obs_s, FS26, 2026, rate).b0, 4.0))
check("a CU-quoted bid dated Oct of the same year is rolled the same way (the old-crop Sep contract is gone either way)",
      close(rtc.build_crop_year([{"date": S0, "basis": 30.0, "tag": "ZCU26"}], FS26, 2026, rate, harvest_window=(0, 1)).b0, 10.0))
obs_u = [dict(o) for o in OBS]
july = next(o for o in obs_u if o["date"] == date(2026, 7, 1))
july["tag"] = "ZCU26"
w_u = next(w for w in rtc.build_crop_year(obs_u, FUT, 2025, rate).weeks if w.date == date(2026, 7, 1))
check("a Sep tag on a bid of the NEXT summer is the chain's own September (ZCU26 in crop year 2025-26): not rolled, as before",
      w_u.tag == "U" and close(w_u.basis, 10.0) and close(w_u.carry, 10.0), (w_u.tag, w_u.basis, w_u.carry))
check("a full symbol whose year is not the bid's (ZCU27 on a Sep 2026 bid) is a September a year out, not the expiring contract: not rolled",
      rtc.build_crop_year([{"date": S0 - timedelta(days=21), "basis": 30.0, "tag": "ZCU27"}, {"date": S0, "basis": 4.0, "tag": "ZCZ26"}], FS26, 2026, rate,
                          harvest_window=(-3, 4)).b0_weeks in (1, 2))
check("soybeans have no September contract: a 'U' tag is not rolled there",
      rtc.build_crop_year([{"date": S0 + timedelta(days=7 * k), "basis": -20.0 + k, "tag": "ZSX26"} for k in range(0, 12)], {}, 2026, rate, rtc.SOY, harvest_window=(0, 3)).b0_weeks in (0, 3))

sep_only = [o for o in obs_s if o["date"] < S0]                                  # Sep 9, 16, 23, 30 posted; the first October Wednesday has not come
cy_so = rtc.build_crop_year(sep_only, FS26, 2026, rate, harvest_window=(-4, 5))
check("a September window before October's first Wednesday: the average so far (4 of 5 bids: 10, 8, 8, 6 = 8.0), no weekly rows and no crash",
      close(cy_so.b0, 8.0) and cy_so.b0_weeks == 4 and cy_so.weeks == [] and cy_so.best == {} and cy_so.complete is False, (cy_so.b0, cy_so.b0_weeks, len(cy_so.weeks)))
cy_sd = rtc.build_crop_year(sep_only, FS26, 2026, rate)
check("the report's window is unchanged by that: September bids alone are still no crop year (empty)", cy_sd.b0 is None and cy_sd.weeks == [] and cy_sd.b0_weeks == 0)
check("no bids at all with a window of the user's: empty, not an error", rtc.build_crop_year([], FS26, 2026, rate, harvest_window=(-4, 5)).b0 is None)

print("return_to_carry: ship by — a study that stops at the end of a month (synthetic year: flat +10 bids, carry Dec->Mar 10, Mar->May 4, May->Jul 2)")
check("the months a study can end in: November through the month before the season's last (corn: Jun, soybeans: Aug) — the last month is the 'no limit' choice",
      rtc.ship_by_months(rtc.CORN) == (11, 12, 1, 2, 3, 4, 5, 6) and rtc.ship_by_months(rtc.SOY) == (11, 12, 1, 2, 3, 4, 5, 6, 7, 8))
check("study_end = the last day of that month in the crop year (Nov/Dec in the starting year, Jan on in the next; February knows leap years)",
      rtc.study_end(2025, 11) == date(2025, 11, 30) and rtc.study_end(2025, 12) == date(2025, 12, 31) and rtc.study_end(2025, 3) == date(2026, 3, 31)
      and rtc.study_end(2025, 2) == date(2026, 2, 28) and rtc.study_end(2027, 2) == date(2028, 2, 29) and rtc.study_end(2025, 6) == date(2026, 6, 30))
check("no limit: the very same object comes back (nothing is rebuilt, so nothing can change)", rtc.limit_crop_year(syn, None) is syn and rd.limit_history([syn], None)[0] is syn)
end3 = date(2026, 3, 31)
before = (len(syn.weeks), dict(syn.best), syn.summer, syn.season_carry, syn.complete)
lim3 = rtc.limit_crop_year(syn, 3)
kept = [w for w in syn.weeks if w.date <= end3]
check("by the end of March keeps exactly the weeks up to Mar 31 — the very same weekly rows (a week's return never depended on a later week)",
      lim3.window_end == end3 and len(lim3.weeks) == len(kept) < len(syn.weeks) and all(a is b for a, b in zip(lim3.weeks, kept)) and lim3.weeks[-1].date == date(2026, 3, 25))
check("the full-season year is left as it was: a copy was trimmed, not the original",
      syn.window_end is None and (len(syn.weeks), syn.best, syn.summer, syn.season_carry, syn.complete) == before and len(syn.weeks) > len(lim3.weeks))
have = [w for w in kept if w.net is not None]
up = rtc.build_crop_year([{"date": START + timedelta(days=7 * k), "basis": 10.0 + 0.5 * k} for k in range(0, 43)], FUT, 2025, rate)     # a market that keeps climbing
up3 = rtc.limit_crop_year(up, 3)
check("a market that keeps climbing: the season's best comes after March, so a March study's best is lower and earlier (what stopping early costs)",
      up.best["net"].date > end3 and up3.best["net"].date <= end3 and up3.best["net"].net < up.best["net"].net - 1.0, (up.best["net"].date, up.best["net"].net, up3.best["net"].date, up3.best["net"].net))
check("its best return is picked again from those weeks (the earliest wins a tie): it can fall or stay, and never moves later",
      lim3.best["net"] is max(have, key=lambda w: (w.net, -w.idx)) and lim3.best["net"].net <= syn.best["net"].net + 1e-12 and lim3.best["net"].date <= syn.best["net"].date)
check("the gross best is picked again too", lim3.best["gross"] is max([w for w in kept if w.gross is not None], key=lambda w: (w.gross, -w.idx)))
check("no week by March is quoted off July, so there is no 'best summer basis' (the full season has one)", lim3.summer is None and syn.summer is not None)
check("the futures carry is what was banked by the last week: Dec->Mar 10 + Mar->May 4 = 14, quoted off May — not the season's Dec->Jul 16",
      close(lim3.season_carry, 14.0) and lim3.carry_tag == "K" and close(syn.season_carry, 16.0) and syn.carry_tag is None)
check("a year with weeks to within a week of the end is complete", lim3.complete is True)
lim6 = rtc.limit_crop_year(syn, 6)
check("by June the study reaches the July contract: a summer basis, 16 banked, quoted off July", lim6.summer is not None and lim6.summer.tag == "N" and close(lim6.season_carry, 16.0) and lim6.carry_tag == "N")
lim11 = rtc.limit_crop_year(syn, 11)
check("by the end of November nothing has rolled yet: 0 banked, still quoted off Dec", close(lim11.season_carry, 0.0) and lim11.carry_tag == "Z" and lim11.weeks[-1].date == date(2025, 11, 26))
part = rtc.build_crop_year(OBS[:20], FUT, 2025, rate)                                # in progress: the bids stop in mid-February
limp = rtc.limit_crop_year(part, 3)
check("a year still in progress is not complete for the study (its weeks stop before March) and keeps every week it has",
      limp.complete is False and len(limp.weeks) == len(part.weeks) and limp.best["net"] is part.best["net"])
early = rtc.limit_crop_year(rtc.build_crop_year(OBS[:2], FUT, 2025, rate), 11)       # two October bids: no return yet
check("a year with no return yet comes out empty, not broken", early.best == {} and early.summer is None and early.complete is False and len(early.weeks) == 2)
none_wk = rtc.limit_crop_year(rtc.CropYear(2025, "2025-25"), 3)
check("a crop year with no weeks at all stays empty", none_wk.weeks == [] and none_wk.best == {} and none_wk.season_carry is None and none_wk.complete is False)
check("the weekly returns themselves do not change: each retained week equals the full season's on every figure",
      all((a.net, a.gross, a.interest, a.carry, a.cash) == (b.net, b.gross, b.interest, b.carry, b.cash) for a, b in zip(lim3.weeks, syn.weeks)))
for name, res_s in (("2019-20", res19), ("2015-16", res15), ("2020-21", res20)):
    ok, detail = True, ""
    for m in rtc.ship_by_months(rtc.CORN):
        lim = rtc.limit_crop_year(res_s, m)
        end = rtc.study_end(res_s.crop_year, m)
        want = [w for w in res_s.weeks if w.date <= end]
        got = [w for w in want if w.net is not None]
        good = (lim.weeks == want and all(a is b for a, b in zip(lim.weeks, want))
                and (("net" not in lim.best) if not got else (lim.best["net"] is max(got, key=lambda w: (w.net, -w.idx))
                                                              and lim.best["net"].net <= res_s.best["net"].net + 1e-12 and lim.best["net"].date <= res_s.best["net"].date)))
        if not good:
            ok, detail = False, "month %d" % m
    check("%s (the analyst's sheet): for every ship-by month the weeks are the full year's up to it and the best is the best of them — never above the season's, never later" % name, ok, detail)
check("2019-20: the report's best (+9.09 on Dec 4) is still the best when the study ends in December or later, and is not available when it ends in November",
      all(rtc.limit_crop_year(res19, m).best["net"].date == date(2019, 12, 4) for m in (12, 1, 2, 3, 4, 5, 6))
      and rtc.limit_crop_year(res19, 11).best["net"].date <= date(2019, 11, 30) and rtc.limit_crop_year(res19, 11).best["net"].net < 9.09)
check("the study end only ever shortens: more months can only raise the best return (a ladder, 2020-21)",
      all((rtc.limit_crop_year(res20, a).best.get("net") is None or rtc.limit_crop_year(res20, b).best["net"].net >= rtc.limit_crop_year(res20, a).best["net"].net - 1e-12)
          for a, b in zip(rtc.ship_by_months(rtc.CORN), rtc.ship_by_months(rtc.CORN)[1:])))

print("return_to_carry: the analyst's per-year choices are honoured")
syn19 = rtc.build_crop_year([{"date": START.replace(year=2019, month=10, day=2) + timedelta(days=7 * k), "basis": float(k)} for k in range(0, 43)],
                            {}, 2019, rate)
check("2019-20 averages weeks 3-9 (bids 2..8 -> 5.0)", close(syn19.b0, 5.0) and syn19.b0_weeks == 7, syn19.b0)
syn12 = rtc.build_crop_year([{"date": date(2012, 9, 5) + timedelta(days=7 * k), "basis": float(k)} for k in range(0, 50)], {}, 2012, rate)
check("2012-13 averages 9 weeks starting in SEPTEMBER (Sep 5 .. Oct 31: bids 0..8 -> 4.0)", close(syn12.b0, 4.0) and syn12.b0_weeks == 9, (syn12.b0, syn12.b0_dates))
check("a September bid is ignored by every other year's window", rtc.build_crop_year(
    [{"date": date(2025, 9, 24), "basis": 99.0}] + OBS, FUT, 2025, rate).b0 == 10.0)

print("return_to_carry_data: building the weekly series from the app's tables")
rail = [{"date": "2026-10-02", "market": "CSX Columbus", "commodity": "Corn", "period": "FH Oct", "period_order": 1, "futures": "ZCZ26", "bid": -10},
        {"date": "2026-10-02", "market": "CSX Columbus", "commodity": "Corn", "period": "Nov", "period_order": 3, "futures": "ZCZ26", "bid": 16},
        {"date": "2026-08-12", "market": "CSX Columbus", "commodity": "Corn", "period": "Spot", "period_order": 0, "futures": "ZCU26", "bid": 5},
        {"date": "2026-08-12", "market": "CSX Columbus", "commodity": "Corn", "period": "Sep", "period_order": 2, "futures": "ZCU26", "bid": 9},
        {"date": "2026-10-02", "market": "UP Group 3", "commodity": "Corn", "period": "Spot", "period_order": 0, "futures": "ZCZ26", "bid": 99},
        {"date": "2026-10-02", "market": "CSX Columbus", "commodity": "Soybeans", "period": "Spot", "period_order": 0, "futures": "ZSX26", "bid": 77},
        {"date": "2026-10-09", "market": "CSX Columbus", "commodity": None, "period": "Spot", "period_order": 0, "futures": None, "bid": None}]
ob = rd.obs_from_rail(rail, "CSX Columbus", "Corn")
check("a corridor's series: the Spot bid when posted, else the nearest forward period; other corridors/commodities and blank bids ignored",
      ob == [{"date": date(2026, 8, 12), "basis": 5.0, "tag": "ZCU26"}, {"date": date(2026, 10, 2), "basis": -10.0, "tag": "ZCZ26"}], ob)
S = SimpleNamespace
snaps = [S(timestamp="2026-10-01T10:00:00Z", rows=[S(grain="Corn", isSpot=True, basisCents=7, futuresSymbol="", deliveryMonth="Spot")]),
         S(timestamp="2026-10-02T09:00:00Z", rows=[S(grain="Corn", isSpot=False, basisCents=-30, futuresSymbol="ZCZ26", deliveryMonth="Nov"),
                                                    S(grain="Corn", isSpot=False, basisCents=-40, futuresSymbol="ZCZ26", deliveryMonth="Oct"),
                                                    S(grain="Soybeans", isSpot=False, basisCents=5, futuresSymbol="ZSX26", deliveryMonth="Oct")]),
         S(timestamp="2026-10-02T18:00:00Z", rows=[S(grain="Corn", isSpot=False, basisCents=-38, futuresSymbol="ZCZ26", deliveryMonth="Oct")])]
ob2 = rd.obs_from_snapshots(snaps, "Corn", lambda g: g)
check("a basis location's series: its spot row, else its front forward row; one bid per day, the newest snapshot's",
      ob2 == [{"date": date(2026, 10, 1), "basis": 7.0, "tag": None}, {"date": date(2026, 10, 2), "basis": -38.0, "tag": "ZCZ26"}], ob2)
check("crop years a series touches: Oct-Dec belongs to that year, Jan-Jul to the one before, Aug-Sep to neither",
      rd.crop_years_in([{"date": date(2025, 10, 2)}, {"date": date(2026, 2, 4)}, {"date": date(2026, 8, 5)}, {"date": date(2026, 10, 7)}]) == [2025, 2026])

hist = rd.run_history(OBS + [{"date": date(2024, 10, 2) + timedelta(days=7 * k), "basis": 3.0} for k in range(0, 4)] + [{"date": date(2026, 10, 7), "basis": 4.0}],
                      {**FUT, **{date(2026, 10, 7): {"ZCZ26": 420.0}}}, rate)
check("run_history: a year needs 12 weeks to count, except the newest (in progress) — 2024's 4 weeks are dropped, 2026's single week kept",
      [c.crop_year for c in hist] == [2025, 2026], [c.crop_year for c in hist])
rows = rd.summary_rows(hist, "net")
check("summary_rows carry the table's columns", rows[0]["label"] == "2025-26" and rows[0]["b0"] == 10.0 and close(rows[0]["carry"], 16.0) and rows[0]["best"] is not None
      and rows[0]["best_date"] is not None and rows[1]["best"] is None)
gross_rows = rd.summary_rows(hist, "gross")
check("the gross measure never reads lower than net", gross_rows[0]["best"] >= rows[0]["best"])
pts = rd.seasonal_points(hist, "net")
check("seasonal_points: one per week that has a return", len(pts) == len([w for c in hist for w in c.weeks if w.net is not None]) and set(p["crop"] for p in pts) == {"2025-26"})
lim_rows = rd.summary_rows(rd.limit_history(hist, 3), "net")
check("summary_rows of a study that stops at the end of March: its end and the contract the carry was banked to (14, off May); the full-season rows carry neither",
      rows[0]["ship_by"] is None and rows[0]["carry_tag"] is None and lim_rows[0]["ship_by"] == date(2026, 3, 31) and lim_rows[1]["ship_by"] == date(2027, 3, 31)
      and lim_rows[0]["carry_tag"] == "K" and close(lim_rows[0]["carry"], 14.0) and lim_rows[0]["weeks"] < rows[0]["weeks"] and lim_rows[0]["complete"] is True
      and lim_rows[0]["best"] <= rows[0]["best"] + 1e-12 and lim_rows[0]["best_date"] <= rows[0]["best_date"] and lim_rows[1]["complete"] is False, lim_rows[0])
check("limit_history keeps every crop year and their order", [c.crop_year for c in rd.limit_history(hist, 3)] == [c.crop_year for c in hist] and rd.limit_history(hist, None) is hist)

print("return_to_carry_data: a year that merely repeats the one before is dropped (the archive's 2007-08 = 2006-07)")
import random
rng = random.Random(7)
real = [round(rng.uniform(-30, 30)) for _ in range(44)]
def yr_obs(y, vals):
    st0 = rtc.first_wednesday(y)
    return [{"date": st0 + timedelta(days=7 * k), "basis": float(v)} for k, v in enumerate(vals)]
obs_rep2 = yr_obs(2005, real) + yr_obs(2006, real) + yr_obs(2007, [round(rng.uniform(-30, 30)) for _ in range(44)])
res_b, skipped_b = rd.run_history_noted(obs_rep2, {}, rate, min_year=2004)
check("2006-07 repeating 2005-06 is dropped and reported; the others stay", skipped_b == ["2006-07"] and [c.crop_year for c in res_b] == [2005, 2007], (skipped_b, [c.crop_year for c in res_b]))
check("run_history is the same list without the note", [c.crop_year for c in rd.run_history(obs_rep2, {}, rate)] == [2005, 2007])
hobs = yr_obs(2005, real) + yr_obs(2006, [round(rng.uniform(-30, 30)) for _ in range(44)]) + yr_obs(2007, [round(rng.uniform(-30, 30)) for _ in range(44)])
h_calc = rd.run_history(hobs, {}, rate, min_year=2004)
h_own = rd.run_history(hobs, {}, rate, min_year=2004, b0_overrides={2006: -15.0})
check("run_history(b0_overrides={2006: -15}): that crop year is measured from -15 (flagged own, its calculated average kept), the others are untouched",
      [c.crop_year for c in h_own] == [2005, 2006, 2007] and [c.b0_own for c in h_own] == [False, True, False] and close(h_own[1].b0, -15.0)
      and close(h_own[1].b0_calc, h_calc[1].b0) and close(h_own[0].b0, h_calc[0].b0) and close(h_own[2].b0, h_calc[2].b0))
r_calc, r_own = rd.summary_rows(h_calc, "net"), rd.summary_rows(h_own, "net")
check("summary_rows say which year is measured from the user's own number, and what the calculated one would have been",
      [r["b0_own"] for r in r_own] == [False, True, False] and close(r_own[1]["b0"], -15.0) and close(r_own[1]["b0_calc"], r_calc[1]["b0"])
      and all(r["b0_own"] is False and close(r["b0_calc"], r["b0"]) for r in r_calc))
ob_years = [{"date": date(2024, 10, 2)}, {"date": date(2025, 10, 1)}, {"date": date(2026, 10, 7)}]
check("own_b0_map: nothing -> {} (the calculated method everywhere); a number -> the tracked crop year only; the what-if -> every crop year the series covers",
      rd.own_b0_map(None, 2026, ob_years) == {} and rd.own_b0_map(-15, 2026, ob_years) == {2026: -15.0}
      and rd.own_b0_map(-15, 2026, ob_years, every_year=True) == {2024: -15.0, 2025: -15.0, 2026: -15.0}
      and rd.own_b0_map(-15, 2026, [], every_year=True) == {2026: -15.0} and rd.own_b0_map(0, 2026, ob_years) == {2026: 0.0})
check("a year that only partly matches (a handful of equal weeks) is kept", rd.repeated_years(rd.run_history_noted(
    yr_obs(2005, real) + yr_obs(2006, real[:5] + [round(rng.uniform(40, 80)) for _ in range(39)]), {}, rate)[0]) == set())

print("return_to_carry_data: the old sheets' futures (1996-2006) merge under the database's")
sf = rd.load_sheet_futures()
check("the committed CSV loads: ~470 weekly days, Oct 1996 -> Sep 2007", 400 < len(sf) < 600 and min(sf) == date(1996, 10, 2) and "ZCZ96" in sf[date(1996, 10, 2)], (len(sf), min(sf)))
mg = rd.merge_futures({date(2000, 1, 5): {"ZCH00": 1.0, "ZCK00": 2.0}}, {date(2000, 1, 5): {"ZCH00": 9.0}}, None)
check("merge_futures: the later source wins a (date, symbol), the rest survive", mg == {date(2000, 1, 5): {"ZCH00": 9.0, "ZCK00": 2.0}})
check("a missing file is an empty map, not an error", rd.load_sheet_futures("/nonexistent/x.csv") == {})

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
