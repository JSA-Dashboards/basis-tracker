"""The Return to Carry tracker's headline, table and charts (pure builders; no Streamlit).

    python tests/test_return_to_carry_view.py
"""
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ── synthetic crop years: weekly bids that climb through the year, flat futures with a 10/4/2 carry ───────────
def make_year(y, base, weeks=44, bump=0.5):
    start = rtc.first_wednesday(y)
    obs = [{"date": start + timedelta(days=7 * k), "basis": base + bump * k} for k in range(weeks)]
    futs = {}
    for k in range(-2, 46):
        futs[start + timedelta(days=7 * k)] = {rtc.contract_symbol("Z", y): 400.0, rtc.contract_symbol("H", y): 410.0,
                                                rtc.contract_symbol("K", y): 414.0, rtc.contract_symbol("N", y): 416.0,
                                                rtc.contract_symbol("U", y): 410.0}
    for d in rtc.roll_dates(y).values():
        futs[d] = dict(futs.get(d, {}), **{rtc.contract_symbol("Z", y): 400.0, rtc.contract_symbol("H", y): 410.0,
                                           rtc.contract_symbol("K", y): 414.0, rtc.contract_symbol("N", y): 416.0,
                                           rtc.contract_symbol("U", y): 410.0})
    return obs, futs


def build(years, last_weeks=None):
    obs, futs = [], {}
    for i, y in enumerate(years):
        o, f = make_year(y, base=-5.0 + 3 * i, weeks=(last_weeks if (last_weeks and y == years[-1]) else 44), bump=0.4 + 0.05 * i)
        obs += o
        futs.update(f)
    return rd.run_history(obs, futs, lambda d: 5.0, min_year=2000), obs, futs


YEARS = list(range(2014, 2026))
results, _, _ = build(YEARS)
rows = rd.summary_rows(results, "net")

print("return_to_carry_view: the headline strip")
check("12 synthetic crop years, all complete", len(results) == 12 and all(cy.complete for cy in results))
h = vw.headline_html(rows, "net")
check("names the latest crop year and its state", "2025-26 crop year" in h and "complete" in h and "net of interest" in h)
check("four cards: best return so far, latest, harvest basis, futures carry",
      all(t in h.upper() for t in ("BEST RETURN SO FAR", "LATEST", "HARVEST BASIS", "FUTURES CARRY")))
check("the best return is printed signed, in cents, with its week and rank among the years",
      ("%+.1f¢" % rows[-1]["best"]) in h and "#1 of 12 years" in h, h[:400])
check("harvest basis card says how it is built", "average of the first 7 weekly bids, vs Dec" in h)
check("the futures-carry card shows the Dec-Jul carry (10 + 4 + 2 = 16)", "+16.0¢" in h)
hg = vw.headline_html(vw.__dict__["rd"].summary_rows(results, "gross") if "rd" in vw.__dict__ else rd.summary_rows(results, "gross"), "gross")
check("gross view is labelled, and shows a larger best return than net", "gross (before interest)" in hg and "before interest" in hg)
check("no rows -> nothing", vw.headline_html([], "net") == "")
inprog, _, _ = build(YEARS, last_weeks=9)
hp = vw.headline_html(rd.summary_rows(inprog, "net"), "net")
check("a year in progress says how many weeks it has", "9 weeks in" in hp and "2025-26 crop year" in hp, hp[:200])
one, _, _ = build(YEARS, last_weeks=1)
check("a harvest basis built from fewer than 7 bids says so ('the first weekly bid so far', not 'the first 1 weekly bids')",
      "the first weekly bid so far, vs Dec" in vw.headline_html(rd.summary_rows(one, "net"), "net") and "average of the first 7 weekly bids, vs Dec" in vw.headline_html(rows, "net")
      and "average of the first 3 weekly bids so far" in vw.headline_html(rd.summary_rows(build(YEARS, last_weeks=3)[0], "net"), "net"))
check("...and one week is '1 week in'", "(1 week in)" in vw.headline_html(rd.summary_rows(one, "net"), "net"), vw.headline_html(rd.summary_rows(one, "net"), "net")[:200])

print("return_to_carry_view: the by-year table")
t = vw.table_html(rows, "net")
check("one line per crop year, newest first", t.index("2025-26") < t.index("2024-25") < t.index("2014-15"))
check("the latest is highlighted and tagged", t.count("LATEST") == 1 and "background:#fff4e5" in t)
check("the year with the highest best return is picked out in green", t.count("color:#0a7f3f;font-weight:700") == 1)
check("with 3+ completed years an average and a median line follow", "Average of 12 completed years" in t and "Median" in t)
check("seven columns", t.count("<th ") == 7)
few = vw.table_html(rd.summary_rows(results[-2:], "net"), "net")
check("with fewer than 3 completed years there is no average line", "Average of" not in few)
ipt = vw.table_html(rd.summary_rows(inprog, "net"), "net")
check("a year in progress is tagged IN PROGRESS and left out of the average", "IN PROGRESS" in ipt and "Average of 11 completed years" in ipt)
check("empty -> empty", vw.table_html([], "net") == "")

print("return_to_carry_view: history that is ESTIMATED from another series is marked")
DV = {"fob_location": "Havana", "gap": 3.04, "q1": 0.9, "q3": 5.4, "pairs": 249, "own_from": date(2026, 6, 12), "derived_weeks": 36, "first": date(2006, 9, 6)}
bn = vw.derived_banner_html("ADM Havana, IL", DV)
check("the banner says whose basis it is not, the FOB reach, the gap and how it was measured, and that it is derived, not actual history",
      "Derived history" in bn and "ADM Havana, IL" in bn and "<b>Havana</b>" in bn and "3.0" in bn and "249" in bn and "0.9 to 5.4" in bn and "Jun 12, 2026" in bn
      and "derived through the historical FOB river values, not actual basis history" in bn, bn[:300])
check("it names the year that is a mixture (the own bids start in June 2026: 2025-26, 36 derived weeks)", "2025-26 is a mixture (36 derived weeks" in bn)
check("the mixed year counts ITS derived weeks (the block passes mixed_weeks), not the whole history's", "2025-26 is a mixture (34 derived weeks" in vw.derived_banner_html("ADM Havana, IL", dict(DV, mixed_weeks=34)))
check("a name from the database is escaped before it reaches the page", "&lt;b&gt;" in vw.derived_banner_html("<b>x</b>", DV) and "<b>x</b>" not in vw.derived_banner_html("<b>x</b>", DV))
mk = {2014: "derived", 2015: "derived", 2025: "part"}
td = vw.table_html(rows, "net", derived=mk)
check("the by-year table tags the derived years DERIVED, the mixed one PART DERIVED, and explains the tags under the table",
      td.count(">DERIVED<") == 2 and td.count(">PART DERIVED<") == 1 and "built from the River FOB sheet" in td and td.index("2014-15") > td.index("2015-16"), (td.count(">DERIVED<"), td.count(">PART DERIVED<")))
check("a location's own history carries no tag and no legend", "DERIVED" not in vw.table_html(rows, "net") and "DERIVED" not in vw.table_html(rows, "net", derived={}))
bdv = vw.best_bar_chart(rows, "net", derived=mk).to_dict()
dsd = next(d for d in bdv["datasets"].values() if d and "Crop year" in d[0])
check("the bars carry their source and a legend, so the derived ones are drawn lighter", {r["Crop year"]: r["Source"] for r in dsd} == dict(
      [(r["label"], "derived" if r["crop_year"] in (2014, 2015) else "partly derived" if r["crop_year"] == 2025 else "own bids") for r in rows]) and "legend" in json.dumps(bdv["layer"][0]["encoding"]["color"]))
check("without derived years the bars are the plain blue / orange ones", "Source" not in json.dumps(vw.best_bar_chart(rows, "net").to_dict()["layer"][0]["encoding"]["color"]))
check("derived_years: 'derived' = every bid of the crop year is flagged, 'part' = some, nothing flagged = nothing",
      rd.derived_years([{"date": date(2024, 10, 2), "derived": True}, {"date": date(2025, 1, 8), "derived": True}, {"date": date(2025, 10, 1), "derived": True}, {"date": date(2025, 12, 3)}])
      == {2024: "derived", 2025: "part"} and rd.derived_years([{"date": date(2025, 10, 1)}]) == {} and rd.derived_years([]) == {})

print("return_to_carry_view: the user's OWN harvest basis is marked wherever it is used")
_, obs_all, futs_all = build(YEARS)
res_own = rd.run_history(obs_all, futs_all, lambda d: 5.0, min_year=2000, b0_overrides={2025: -20.0})
rows_own = rd.summary_rows(res_own, "net")
shift = rows[-1]["b0"] - (-20.0)
check("the tracked year's best return moves by calculated - own, the other years' rows are untouched",
      rows_own[-1]["b0_own"] and close(rows_own[-1]["b0"], -20.0) and close(rows_own[-1]["b0_calc"], rows[-1]["b0"]) and close(rows_own[-1]["best"] - rows[-1]["best"], shift)
      and all(close(a["best"], b["best"]) and not a["b0_own"] for a, b in zip(rows_own[:-1], rows[:-1])), (rows_own[-1]["best"], rows[-1]["best"], shift))
ho = vw.headline_html(rows_own, "net")
check("the headline's harvest-basis card says it is your own and what the calculated one was",
      "your own, vs Dec · calculated %+.1f" % rows[-1]["b0"] in ho and "-20.0¢" in ho and "average of the first 7 weekly bids" not in ho, ho[-700:])
check("...and without an override it says what it always said", "your own" not in h and "average of the first 7 weekly bids, vs Dec" in h)
to = vw.table_html(rows_own, "net")
check("the by-year table marks the own year with an OWN pill and says what it means under the table",
      to.count(">OWN<") == 1 and "OWN = measured from the harvest basis you entered" in to and to.index(">OWN<") < to.index("2024-25"), to.count(">OWN<"))
check("no pill and no legend when every year is calculated", ">OWN<" not in t and "OWN =" not in t)
tdo = vw.table_html(rows_own, "net", derived={2014: "derived"})
check("it sits beside the derived tags when both apply (own pill + legend, derived pill + legend)", ">OWN<" in tdo and ">DERIVED<" in tdo and "OWN =" in tdo and "DERIVED / PART DERIVED =" in tdo)
bo = vw.best_bar_chart(rows_own, "net").to_dict()
dso = next(d for d in bo["datasets"].values() if d and "Crop year" in d[0])
check("the bars read the own year's best return and show its harvest basis in the tooltip", close(dso[-1]["Best"], rows_own[-1]["best"]) and close(dso[-1]["Harvest basis"], -20.0))
n1 = vw.own_basis_note_html(-20.0, -12.5, "2025-26")
check("the note under the switch names the number, what it replaces, the crop year (only) and where it is used, and says every other year keeps its "
      "calculated basis from the harvest window",
      "Your own harvest basis, -20.0¢ vs Dec" in n1 and "in place of the calculated -12.5¢" in n1 and "measures 2025-26 only" in n1 and "shipment table" in n1
      and "latest row" in n1 and "Every other crop year keeps its calculated harvest basis (the average of the harvest window above)" in n1 and "What-if" not in n1)
n2 = vw.own_basis_note_html(-20.0, -12.5, "2025-26", rtc.CORN, False)
check("...a positional call (spec, in_history) still works now that the every-year what-if is gone", "shipment table (that year has no weekly bids" in n2)
n3 = vw.own_basis_note_html(-20.0, None, "2026-27", in_history=False)
check("a tracked year with no weekly bids yet: the number serves the shipment table only, and there is no 'calculated' to compare with",
      "shipment table (that year has no weekly bids to draw yet)" in n3 and "calculated" not in n3.replace("calculated harvest basis", "") and "headline" not in n3)
n4 = vw.own_basis_note_html(-30.0, -35.0, "2026-27", rtc.SOY)
check("soybeans read vs Jan", "-30.0¢ vs Jan" in n4)
check("balanced markup", all(n.count("<div") == n.count("</div>") == 1 for n in (n1, n2, n3, n4)))
sh_c = rtc.build_shipment_table(2026, date(2026, 10, 21), -12.0, [], {date(2026, 10, 21): {"ZCZ26": 500.0, "ZCH27": 514.0, "ZCK27": 521.0, "ZCN27": 525.0}}, lambda d: 6.0, "net", b0_weeks=3)
sh_o = rtc.build_shipment_table(2026, date(2026, 10, 21), -20.0, [], {date(2026, 10, 21): {"ZCZ26": 500.0, "ZCH27": 514.0, "ZCK27": 521.0, "ZCN27": 525.0}}, lambda d: 6.0, "net",
                                b0_own=True, b0_calc=-12.0)
hs = vw.shipment_html(sh_o, None, "bank prime")
check("the shipment table's harvest-basis card and note say it is yours, with the calculated number beside it, and never call it an estimate or an average",
      "your own, vs Dec · calculated -12.0" in hs and "Your own harvest basis</b> is in use" in hs and "Estimate." not in hs
      and "weekly bids so far" not in hs and "-20.0¢" in hs)
hc = vw.shipment_html(sh_c, None, "bank prime")
check("...and the calculated table is unchanged", "your own" not in hc and "Your own harvest basis" not in hc and "average of the first 3 weekly bids so far" in hc)
hs0 = vw.shipment_html(rtc.build_shipment_table(2026, date(2026, 10, 21), -5.0, [], {}, lambda d: 6.0, "net", b0_own=True, b0_calc=None), None, "")
check("no calculated number to compare with -> the card just says it is yours", "your own, vs Dec" in hs0 and "· calculated" not in hs0 and "in place of the calculated" not in hs0)
hw = vw.how_it_works()
check("the 'how it is calculated' text tells the user about the switch: this year only, every other year calculated, no what-if",
      "switch for this year's harvest basis above can use your own number" in hw and "measures only the crop year being tracked" in hw and "what-if" not in hw)
check("...and that a window opening before the report's moves the purchase (lines can begin in September), a later one does not",
      "moves the purchase with it" in hw and "can begin in September" in hw and "interest still starts at the report's third weekly bid" in hw)

print("return_to_carry_view: ship by — a study that stops at the end of March")
import re  # noqa: E402
lim_res = rd.limit_history(results, 3)
lim_rows = rd.summary_rows(lim_res, "net")
hl = vw.headline_html(lim_rows, "net")
check("the headline says where the study ends and the best-return card is 'Best return by Mar 31' (the year is complete)",
      "shipped by Mar 31" in hl and "Best return by Mar 31" in hl and "Best return so far" not in hl and "complete" in hl, hl[:300])
check("its best return is the March study's (lower than the season's), printed signed with the week it happened",
      ("%+.1f¢" % lim_rows[-1]["best"]) in hl and lim_rows[-1]["best"] < rows[-1]["best"] and lim_rows[-1]["best_date"] <= date(2026, 3, 31))
check("the futures-carry card says what was banked by the last week and to which contract (14 = Dec->Mar 10 + Mar->May 4, quoted off May)",
      "+14.0¢" in hl and "Dec → May, banked by Mar 25" in hl and "Dec → Jul" not in hl, hl[-500:])
check("the full-season headline is as it was", "Best return so far" in h and "shipped by" not in h and "Dec → Jul, rolled the last Wednesday before each month" in h)
hl_in = vw.headline_html(rd.summary_rows(rd.limit_history(inprog, 3), "net"), "net")
check("a year still in progress reads 'Best so far, by Mar 31'", "Best so far, by Mar 31" in hl_in and "9 weeks in" in hl_in, hl_in[:260])
hl11 = vw.headline_html(rd.summary_rows(rd.limit_history(results, 11), "net"), "net")
check("a study that ends before the first roll says nothing has rolled yet", "none yet — still quoted off Dec" in hl11, hl11[-400:])
tl = vw.table_html(lim_rows, "net")
body_rows = re.findall(r"<tr>(.*?)</tr>", tl.split("<tbody>")[1], flags=re.S)
check("the by-year table says where the study ends and drops the 'Best summer basis' column (no week by March is quoted off July)",
      "Study ends at the end of March" in tl and "Best summer basis" not in tl and tl.count("<th ") == 6 and "Best summer basis" in t and t.count("<th ") == 7, tl.count("<th "))
check("every line, average and median included, has the six cells of the narrower table", body_rows and all(r.count("<td") == 6 for r in body_rows) and len(body_rows) == 14, [r.count("<td") for r in body_rows])
tl6 = vw.table_html(rd.summary_rows(rd.limit_history(results, 6), "net"), "net")
check("a June study reaches July's contract, so the summer column stays; the note names June", "Best summer basis" in tl6 and "Study ends at the end of June" in tl6 and tl6.count("<th ") == 7)
check("the full-season table has no such note", "Study ends" not in t)
bl = vw.best_bar_chart(lim_rows, "net").to_dict()
check("the bar chart's title says where the study ends, the bars are the limited best returns", "shipped by end of March" in bl["title"]["text"]
      and all(close(r["Best"], x["best"]) for r, x in zip(next(d for d in bl["datasets"].values() if d and "Crop year" in d[0]), lim_rows)))
check("the full-season bar chart's title is as it was", vw.best_bar_chart(rows, "net").to_dict()["title"]["text"] == "Best return by crop year — net of interest")
sl = vw.seasonal_chart(lim_res, "net").to_dict()
xl = next(l["encoding"]["x"] for l in sl["layer"] if "scale" in l.get("encoding", {}).get("x", {}))
xfull = next(l["encoding"]["x"] for l in vw.seasonal_chart(results, "net").to_dict()["layer"] if "scale" in l.get("encoding", {}).get("x", {}))
tk_l, nm_l = vw._month_ticks(lim_res, 30, rtc.CORN.horizon, 3)
check("the season chart stops at the end of March: a shorter week axis (to week ~30 from the first Wednesday of September, not 44+), month ticks "
      "Sep .. Mar, the title says so",
      xl["scale"]["domain"][1] <= 31 and xl["scale"]["domain"][1] < xfull["scale"]["domain"][1] and nm_l[0] == "Sep" and nm_l[-1] == "Mar"
      and "weeks through the end of March" in json.dumps(sl["title"]["subtitle"]), (xl["scale"]["domain"], nm_l))
check("...and plots no week after it", max(r["week"] for d in sl["datasets"].values() for r in d if d and "week" in d[0] and "crop" in d[0]) <= 30)
check("the full-season chart's subtitle is unchanged (two lines)", len(vw.seasonal_chart(results, "net").to_dict()["title"]["subtitle"]) == 2)
check("a November study's month ticks end in Nov (the stop is in the starting year, not the next)", vw._month_ticks(lim_res, 12, rtc.CORN.horizon, 11)[1][-1] == "Nov")
try:
    import vl_convert as _vlc
    check("the limited seasonal and bar charts compile to Vega", isinstance(_vlc.vegalite_to_vega(vw.seasonal_chart(lim_res, "net").to_json()), dict)
          and isinstance(_vlc.vegalite_to_vega(vw.best_bar_chart(lim_rows, "net").to_json()), dict))
except ImportError:
    print("  [SKIP] vl_convert is not installed")
check("the method caption says where the weeks stop (and that the season runs on); unchanged without a limit",
      "Weeks run to March 31 (the ship-by limit; the season runs to July 31)." in vw.method_caption(rtc.CORN, stop=date(2026, 3, 31))
      and vw.method_caption(rtc.CORN).endswith("Weeks run to July 31.") and vw.method_caption(rtc.SOY).endswith("Weeks run to September 30."))
check("the how-it-works text explains the Ship by box, with the default (no limit) and what is re-picked",
      "**Ship by**" in vw.how_it_works() and "no limit" in vw.how_it_works() and "through July" in vw.how_it_works() and "through September" in vw.how_it_works(rtc.SOY)
      and "picked again from those weeks" in vw.how_it_works())

print("return_to_carry_view: the user's WINDOW for the harvest basis is used in every year and marked wherever it shows")
res_win = rd.run_history(obs_all, futs_all, lambda d: 5.0, min_year=2000, harvest_window=(6, 4))
rows_win = rd.summary_rows(res_win, "net")
yi = {y: i for i, y in enumerate(YEARS)}
row0 = {y: 6 - rtc.report_start_week(y) for y in YEARS}           # the weekly row week 6 falls on: 2 (Oct 15-ish), 1 in a year whose September has five Wednesdays
check("the synthetic years have both kinds of September: five Wednesdays in 2015, 2020 and 2021, four in the rest",
      sorted(y for y in YEARS if rtc.report_start_week(y) == 5) == [2015, 2020, 2021])
check("every crop year's harvest basis is the average of weeks 6 to 9 counted from September (rows 2-5, or 1-4 after a five-Wednesday September: base + "
      "(row + 1.5) x the weekly climb): the same weeks in every year, the analyst's 2015 and 2019 windows included",
      len(rows_win) == 12 and all(r["b0_window"] == (6, 4) and r["b0_weeks"] == 4 and not r["b0_own"]
                                  and close(r["b0"], -5 + 3 * yi[r["crop_year"]] + (row0[r["crop_year"]] + 1.5) * (0.4 + 0.05 * yi[r["crop_year"]])) for r in rows_win),
      [(r["crop_year"], r["b0"]) for r in rows_win][:3])
check("the report's own rows record no window", all(r["b0_window"] is None for r in rows))
by_start = {sw: {r["crop_year"]: r for r in rd.summary_rows(rd.run_history(obs_all, futs_all, lambda d: 5.0, min_year=2000, harvest_window=(sw, 7)), "net")}
            for sw in (4, 5)}
rows_d7 = [by_start[rtc.report_start_week(r["crop_year"])][r["crop_year"]] for r in rows]
same = [(a, b) for a, b in zip(rows_d7, rows) if a["crop_year"] not in (2015, 2019)]
check("7 weeks from the year's first Wednesday of October (week 4, or 5) are the report's window in every year the sheets did not change: the same "
      "harvest basis, best return, best week and carry",
      len(same) == 10 and all(close(a["b0"], b["b0"]) and close(a["best"], b["best"]) and a["best_date"] == b["best_date"] and close(a["carry"], b["carry"]) for a, b in same))
diff = {a["crop_year"]: (a["b0"], b["b0"]) for a, b in zip(rows_d7, rows) if a["crop_year"] in (2015, 2019)}
check("...while 2015-16 and 2019-20, whose sheets used another window, read differently (the user's window replaces theirs)", len(diff) == 2 and all(not close(a, b) for a, b in diff.values()), diff)
check("a window moves each year's best return by exactly (the report's average - the window's) and leaves the best week where it was",
      all(close(w["best"] - r["best"], r["b0"] - w["b0"]) and w["best_date"] == r["best_date"] and close(w["carry"], r["carry"]) for w, r in zip(rows_win, rows)))
hwn = vw.headline_html(rows_win, "net")
check("the headline's harvest-basis card names the window's weeks and the tracked year's dates, never 'the first 7 weekly bids'",
      "average of weeks 6 to 9 (Oct 15 – Nov 5), vs Dec" in hwn and "average of the first 7 weekly bids" not in hwn and ("%+.1f¢" % rows_win[-1]["b0"]) in hwn, hwn[-650:])


def win_rows(last_weeks, w=(6, 4)):
    _, o_, f_ = build(YEARS, last_weeks=last_weeks)
    return rd.summary_rows(rd.run_history(o_, f_, lambda d: 5.0, min_year=2000, harvest_window=w), "net")


r4 = win_rows(4)
check("a year part-way through its window says so ('2 of 4 bids') and the card still carries the average so far",
      r4[-1]["b0_weeks"] == 2 and "average of weeks 6 to 9 (Oct 15 – Nov 5) so far (2 of 4 bids), vs Dec" in vw.headline_html(r4, "net"), vw.headline_html(r4, "net")[-500:])
r2 = win_rows(2)
h2 = vw.headline_html(r2, "net")
check("a year whose window has not opened yet needs its bids: no harvest basis, no best return, no crash",
      r2[-1]["b0"] is None and r2[-1]["best"] is None and "needs the bids of weeks 6 to 9 (Oct 15 – Nov 5)" in h2 and "2025-26 crop year" in h2, h2[-500:])
r1 = win_rows(9, (8, 1))
check("a one-week window is that week's bid ('week 8 (Oct 29)'), and its average is the bid itself (base + 4 x the climb)",
      r1[-1]["b0_weeks"] == 1 and close(r1[-1]["b0"], -5 + 3 * 11 + 4 * (0.4 + 0.05 * 11)) and "average of week 8 (Oct 29), vs Dec" in vw.headline_html(r1, "net"), vw.headline_html(r1, "net")[-400:])
twn = vw.table_html(rows_win, "net")
check("the by-year table explains the window under it: the weeks, the tracked year's dates, the same weeks (from September) in every year, in place of the report's 7 and its year-specific windows",
      "Harvest basis = the average of your window, weeks 6 to 9 (Oct 15 – Nov 5, the tracked year's dates): the same weeks, counted from the first Wednesday of September, in every crop year" in twn
      and "its year-specific windows" in twn and twn.count("<th ") == 7, twn[-420:])
check("...and the report's own table says nothing of a window", "your window" not in t and "your window" not in vw.table_html(rows_own, "net"))
check("the window and an own number can sit on one page (the own pill and its legend are still drawn)",
      ">OWN<" in vw.table_html(rd.summary_rows(rd.run_history(obs_all, futs_all, lambda d: 5.0, min_year=2000, b0_overrides={2025: -20.0}, harvest_window=(6, 4)), "net"), "net"))
hw_corn, hw_soy = vw.how_it_works(), vw.how_it_works(rtc.SOY)
check("the how-it-works text explains Start week / Number of weeks: week 0 = the first Wednesday of September, the report's start (week 4 or 5), the same weeks in every year",
      "**Start week**" in hw_corn and "**Number of weeks**" in hw_corn and "week 0 is the first Wednesday of September" in hw_corn
      and "first Wednesday of October, week 4, or week 5 when September has five Wednesdays" in hw_corn
      and "the same weeks, counted from September, in every crop year" in hw_corn and "**Start week**" in hw_soy)
check("...that the interest still starts at the report's third (corn) / fifth (soybeans) weekly bid whatever the window",
      "interest still starts at the report's third weekly bid" in hw_corn and "interest still starts at the report's fifth weekly bid" in hw_soy)
check("...and, for corn only, the September roll: bids quoted off CU move to Dec at the Dec - Sep spread of the last day before first notice; bids already off Dec stay",
      "(CU) futures is rolled to Dec" in hw_corn and "last trading day before that contract's first notice day" in hw_corn and "left as it is" in hw_corn and "(CU)" not in hw_soy)

print("return_to_carry_view: helpers")
check("completed() keeps finished years that have a best return", len(vw.completed(rows)) == 12 and len(vw.completed(rd.summary_rows(inprog, "net"))) == 11)
check("rank_of_latest: the newest synthetic year climbs the most, so it ranks first of 12", vw.rank_of_latest(rows) == (1, 12), vw.rank_of_latest(rows))
check("rank_of_latest needs two years and a best return", vw.rank_of_latest(rows[:1]) is None and vw.rank_of_latest([]) is None)

print("return_to_carry_view: the best-by-year bar chart")
bc = vw.best_bar_chart(rows, "net")
spec = bc.to_dict()
data = next(d for d in spec["datasets"].values() if d and "Crop year" in d[0])
check("one bar per crop year, the latest flagged", len(data) == 12 and sum(1 for r in data if r["latest"]) == 1 and data[-1]["Crop year"] == "2025-26")
check("the bar values are the best returns", all(close(r["Best"], x["best"]) for r, x in zip(data, rows)))
kinds = [(l["mark"] if isinstance(l["mark"], str) else l["mark"]["type"]) for l in spec["layer"]]
check("bars + a dashed average line with its label + the zero line", kinds == ["bar", "rule", "text", "rule"], kinds)
check("no best returns -> no chart", vw.best_bar_chart(rd.summary_rows([], "net"), "net") is None)
check("title names the measure", "net of interest" in spec["title"]["text"] and "gross" in vw.best_bar_chart(rd.summary_rows(results, "gross"), "gross").to_dict()["title"]["text"])
try:
    import vl_convert as vlc
    check("the bar chart compiles to Vega", isinstance(vlc.vegalite_to_vega(bc.to_json()), dict))
except ImportError:
    print("  [SKIP] vl_convert is not installed")
    vlc = None

print("return_to_carry_view: the seasonal chart")
sc = vw.seasonal_chart(results, "net", logo_uri="data:image/png;base64,iVBORw0KGgo=")
ss = sc.to_dict()
kinds = [(l["mark"] if isinstance(l["mark"], str) else l["mark"]["type"]) for l in ss["layer"]]
check("watermark, 2 band areas, median line, the two latest years, the best dot + halo + label, zero line",
      kinds == ["image", "area", "area", "line", "line", "point", "text", "text", "rule"], kinds)
dsets = list(ss["datasets"].values())
lines = next(d for d in dsets if d and "crop" in d[0] and "Date" in d[0])
check("the two lines are the latest year and the one before", {r["crop"] for r in lines} == {"2025-26", "2024-25"})
bands = next(d for d in dsets if d and "p50" in d[0])
check("the band is the completed years' middle 50% and 80% by week, only where 5+ years have a value",
      all(r["n"] >= 5 for r in bands) and all(r["p10"] <= r["p25"] <= r["p50"] <= r["p75"] <= r["p90"] for r in bands))
check("subtitle (two short lines, for a phone) says how many years the shading is", "12 completed years" in json.dumps(ss["title"]) and len(ss["title"]["subtitle"]) == 2)
check("the best week is labelled 'Best +x.x¢ · Mon d'", any(str(r.get("label", "")).startswith("Best +") for d in dsets for r in d))
check("month names along the bottom start at Sep (Kolten 2026-10-19: 'start this chart in Sep'), then Oct",
      ss["layer"][1]["encoding"]["x"]["axis"]["labelExpr"].startswith('["Sep", "Oct"') and ss["layer"][1]["encoding"]["x"]["axis"]["values"][0] == 0)
best_pt = next(r for d in dsets for r in d if str(r.get("label", "")).startswith("Best +"))
check("the best dot sits on its week counted from September (row + 4 in 2025-26), on the latest year's line",
      best_pt["week"] == results[-1].best["net"].idx + 4 and any(r["crop"] == "2025-26" and r["week"] == best_pt["week"] for r in lines), best_pt)
xe = ss["layer"][1]["encoding"]["x"]
check("every month tick lies inside the chart's weeks", max(xe["axis"]["values"]) <= xe["scale"]["domain"][1])
res26, _, _ = build(list(range(2019, 2027)))                       # 2026-27 starts Wednesday Oct 7: its week 43 is already August
tk26, nm26 = vw._month_ticks(res26, 49, rtc.CORN.horizon)
check("the month ticks start in September (week 0 = Sep 2, 2026) and stop with the season, July for corn (no stray 'Aug' when the year runs past Jul 31)",
      nm26[0] == "Sep" and tk26[0] == 0 and nm26[-1] == "Jul" and max(tk26) <= 47 and "Aug" in vw._month_ticks(res26, 49)[1], (tk26, nm26))
few5 = vw.seasonal_chart(results[-3:], "net").to_dict()
kinds5 = [(l["mark"] if isinstance(l["mark"], str) else l["mark"]["type"]) for l in few5["layer"]]
check("with fewer than 5 completed years there is no band, just the lines", "area" not in kinds5 and "line" in kinds5, kinds5)
check("no weeks -> no chart", vw.seasonal_chart([], "net") is None)
if vlc:
    check("the seasonal chart compiles to Vega", isinstance(vlc.vegalite_to_vega(sc.to_json()), dict))
    check("...in the gross view too, and without a logo", isinstance(vlc.vegalite_to_vega(vw.seasonal_chart(results, "gross").to_json()), dict))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
