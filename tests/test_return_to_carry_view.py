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
res_all = rd.run_history(obs_all, futs_all, lambda d: 5.0, min_year=2000, b0_overrides=rd.own_b0_map(-20.0, 2025, obs_all, every_year=True))
ta = vw.table_html(rd.summary_rows(res_all, "net"), "net")
check("the what-if marks every year, and every year's harvest basis reads the user's number", ta.count(">OWN<") == 12
      and all(close(r["b0"], -20.0) for r in rd.summary_rows(res_all, "net")) and ta.count("-20.0") >= 12)
tdo = vw.table_html(rows_own, "net", derived={2014: "derived"})
check("it sits beside the derived tags when both apply (own pill + legend, derived pill + legend)", ">OWN<" in tdo and ">DERIVED<" in tdo and "OWN =" in tdo and "DERIVED / PART DERIVED =" in tdo)
bo = vw.best_bar_chart(rows_own, "net").to_dict()
dso = next(d for d in bo["datasets"].values() if d and "Crop year" in d[0])
check("the bars read the own year's best return and show its harvest basis in the tooltip", close(dso[-1]["Best"], rows_own[-1]["best"]) and close(dso[-1]["Harvest basis"], -20.0))
n1 = vw.own_basis_note_html(-20.0, -12.5, "2025-26")
check("the note under the switch names the number, what it replaces, the crop year and where it is used, and says earlier years keep theirs",
      "Your own harvest basis, -20.0¢ vs Dec" in n1 and "in place of the calculated -12.5¢" in n1 and "measures 2025-26" in n1 and "shipment table" in n1
      and "latest row" in n1 and "Earlier years keep their calculated harvest basis" in n1)
n2 = vw.own_basis_note_html(-20.0, -12.5, "2025-26", every_year=True)
check("the what-if note says so: every year, a what-if, not what the analyst's workbooks computed, the best week stays",
      "What-if" in n2 and "every crop year" in n2 and "-20.0¢</b> vs Dec" in n2 and "not what the analyst" in n2 and "stays where it was" in n2 and "Earlier years" not in n2)
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
check("the 'how it is calculated' text tells the user about the switch", "Harvest basis switch above can use your own number" in hw and "what-if" in hw)

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
check("month names along the bottom start at Oct", "Oct" in json.dumps(ss["layer"][1]["encoding"]["x"]["axis"]["labelExpr"]))
xe = ss["layer"][1]["encoding"]["x"]
check("every month tick lies inside the chart's weeks", max(xe["axis"]["values"]) <= xe["scale"]["domain"][1])
res26, _, _ = build(list(range(2019, 2027)))                       # 2026-27 starts Wednesday Oct 7: its week 43 is already August
tk26, nm26 = vw._month_ticks(res26, 44, rtc.CORN.horizon)
check("the month ticks stop with the season, July for corn, even when the latest year starts Oct 7 (no stray 'Aug' at week 43)",
      nm26[0] == "Oct" and nm26[-1] == "Jul" and max(tk26) <= 42 and "Aug" in vw._month_ticks(res26, 44)[1], (tk26, nm26))
few5 = vw.seasonal_chart(results[-3:], "net").to_dict()
kinds5 = [(l["mark"] if isinstance(l["mark"], str) else l["mark"]["type"]) for l in few5["layer"]]
check("with fewer than 5 completed years there is no band, just the lines", "area" not in kinds5 and "line" in kinds5, kinds5)
check("no weeks -> no chart", vw.seasonal_chart([], "net") is None)
if vlc:
    check("the seasonal chart compiles to Vega", isinstance(vlc.vegalite_to_vega(sc.to_json()), dict))
    check("...in the gross view too, and without a logo", isinstance(vlc.vegalite_to_vega(vw.seasonal_chart(results, "gross").to_json()), dict))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
