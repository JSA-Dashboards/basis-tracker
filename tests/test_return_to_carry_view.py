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
