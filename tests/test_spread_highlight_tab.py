"""The Spread tab's "highlight a year" picker, driven in the REAL app (app.py, and view_app.py for the client build) with streamlit.testing.v1.AppTest.

    python tests/test_spread_highlight_tab.py

Kolten 2026-11-13: "On the spreads tab I want to be able to highlight a year on the chart (i.e. 2025/26 listed on chart)". Isolated like tests/test_rail_entry_outcome.py:
every Snowflake / Graph setting is blanked and the database is a throwaway SQLite, seeded with weekly spot history for three locations whose spreads are known by
construction: Loc A - Loc B = 5 * (marketing year - 2018) + (marketing week % 7) cents, so a line on the chart can be checked number for number. Loc C only
starts in 2022, which gives a second pair with fewer years. The chart is read back from the Vega-Lite spec and datasets the app hands the browser.

app.py's tabs are lazy (only the open tab runs) and AppTest has no tab API, so run_in_tab() sends the tab strip's widget state, as the browser does.
"""
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

try:
    from streamlit.testing.v1 import AppTest
    import pyarrow as pa
except ImportError:                                           # an old Streamlit has no AppTest
    print("  [SKIP] streamlit.testing.v1 / pyarrow is not available")
    sys.exit(0)

for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_ROLE", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_PRIVATE_KEY_PATH",
          "SNOWFLAKE_PRIVATE_KEY_PWD", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL", "RIVER_DATABASE_URL", "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
          "GRAPH_SENDER", "APP_PASSWORD", "VIEW_ONLY"):
    os.environ[k] = ""

import changes_report    # noqa: E402
import database          # noqa: E402
import river_fob_data    # noqa: E402

database.DB_PATH = Path(tempfile.mkdtemp()) / "throwaway.db"
assert not database._use_sf(), "this test must not touch Snowflake"
database.init_db()

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""), flush=True)
    if not cond:
        FAILS.append(name)


TODAY = date.today()
CUR = TODAY.year if TODAY.month >= 9 else TODAY.year - 1          # the current marketing year's start year (the black line)


def lbl(y):
    return "%d/%02d" % (y, (y + 1) % 100)


def want_spread(y, wk):
    return float(5 * (y - 2018) + wk % 7)                          # Loc A - Loc B, by construction


def weekly(prov, loc, first, spread_fn, base=-30, last=None):
    out, d = [], first
    while d <= (last or TODAY):
        my = d.year if d.month >= 9 else d.year - 1
        wk = max(1, min(52, ((d - date(my, 9, 1)).days // 7) + 1))
        out.append({"timestamp": d.strftime("%Y-%m-%dT00:00:00Z"), "provider": prov, "location": loc, "source": "jsa_history",
                    "rows": [{"id": "S_SPOT", "grain": "Corn", "deliveryMonth": "Spot", "futuresSymbol": "", "basisCents": int(base + spread_fn(my, wk)),
                              "isSpot": True, "spotGrain": "Corn"}]})
        d += timedelta(days=7)
    return out


database.upsert_snapshots(weekly("P1", "Loc A, IL", date(2019, 9, 4), want_spread) + weekly("P2", "Loc B, IL", date(2019, 9, 4), lambda y, wk: 0)
                          + weekly("P3", "Loc C, IL", date(2022, 9, 7), lambda y, wk: 0)
                          + weekly("P5", "Loc E, IL", date(2020, 10, 7), lambda y, wk: 0, last=date(2020, 10, 7)) + weekly("P5", "Loc E, IL", date(2022, 9, 7), lambda y, wk: 0))
A, B, C, E = "P1 · Loc A, IL", "P2 · Loc B, IL", "P3 · Loc C, IL", "P5 · Loc E, IL"
ALL_YEARS = [lbl(y) for y in range(CUR - 1, 2018, -1)]             # 2019/20 .. last year, newest first: what Loc A / Loc B can offer
BLUE, ORANGE, PURPLE, TEAL, BLACK = "#0693e3", "#f97316", "#8b5cf6", "#0d9488", "#111827"

for p in (mock.patch.object(river_fob_data, "list_dates", lambda: []), mock.patch.object(river_fob_data, "latest_date", lambda: None),
          mock.patch.object(river_fob_data, "load_archive", lambda: {}), mock.patch.object(changes_report, "_graph_configured", lambda: False)):
    p.start()


def run_in_tab(at, name):
    """at.run() with the tab whose label contains `name` open (the tab strip's widget state is sent, as the browser does on every rerun)."""
    box, label = next(((n, t.label) for n in at.main if getattr(n, "type", None) == "tab_container" for t in n.children.values() if name in t.label), (None, None))
    assert box is not None, "the app has no tab labelled *%s*" % name
    ws = at._tree.get_widget_states()
    if box.proto.tab_container.id:
        w = ws.widgets.add()
        w.id = box.proto.tab_container.id
        w.string_value = label
    return at._run(ws)


def layers(at):
    """[{type, color, dy, df, y}] for the Spread chart's layers: mark, its colour and dy, its data as a DataFrame, and its y encoding."""
    pr = at.get("vega_lite_chart")[0].proto
    spec = json.loads(pr.spec)
    data = {d.name: pa.ipc.open_stream(d.data.data).read_all().to_pandas() for d in pr.datasets}
    out = []
    for l in spec["layer"]:
        m = l["mark"] if isinstance(l["mark"], dict) else {"type": l["mark"]}
        out.append({"type": m.get("type"), "color": m.get("color"), "dy": m.get("dy"), "df": data.get((l.get("data") or {}).get("name")), "y": (l.get("encoding") or {}).get("y", {})})
    return out


def legend(at):
    return next((m.value for m in at.markdown if "= black" in m.value), "")


def hl_layers(ls):
    cols = {BLUE, ORANGE, PURPLE, TEAL}
    return [(i, l) for i, l in enumerate(ls) if l["color"] in cols]


def domain(ls):
    cur = next(l for l in ls if l["type"] == "line" and l["color"] == BLACK)
    return (cur["y"].get("scale") or {}).get("domain")


def open_spread(at, a=A, b=B):
    at.selectbox(key="spread_a").set_value(a)
    run_in_tab(at, "Spread")
    at.selectbox(key="spread_b").set_value(b)
    return run_in_tab(at, "Spread")


def no_exc(at):
    return [str(e.value)[:160] for e in at.exception]


t0 = time.time()
at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=900)
at.run()
run_in_tab(at, "Spread")
print("the admin app, first runs: %.0f s; exceptions: %s" % (time.time() - t0, no_exc(at) or "none"), flush=True)

print("\nwith nothing picked the chart is as it was")
pick = at.multiselect(key="spread_hl")
check("the picker is there, empty, and offers every earlier marketing year, newest first, without the current one (black)", pick.value == [] and list(pick.options) == ALL_YEARS and lbl(CUR) not in pick.options, (pick.value, list(pick.options)))
check("it says what it is for and gives the newest year as the example", pick.placeholder == "Highlight a year on the chart (e.g. %s)" % ALL_YEARS[0], pick.placeholder)
check("at most four can be picked (the colours there are)", pick.proto.max_selections == 4, pick.proto.max_selections)
ls0 = layers(at)
check("7 layers (logo, zero line, range, avg line, avg label, current line, current label) and none of them a highlight colour", len(ls0) == 7 and not hl_layers(ls0), [(l["type"], l["color"]) for l in ls0])
check("the legend line names no extra year", "= blue" not in legend(at) and "= orange" not in legend(at) and ("%s = black" % lbl(CUR)) in legend(at), legend(at)[-420:])
dom0 = domain(ls0)
check("(the 90% zoom sets a y domain)", isinstance(dom0, list) and len(dom0) == 2, dom0)
check("no exception", not no_exc(at), no_exc(at))

print("\none year picked")
LAST = CUR - 1
at.multiselect(key="spread_hl").set_value([lbl(LAST)])
run_in_tab(at, "Spread")
ls1 = layers(at)
hl = hl_layers(ls1)
check("two layers are added: its line and its label (9 in all), both blue", len(ls1) == 9 and [(l["type"], l["color"]) for _, l in hl] == [("line", BLUE), ("text", BLUE)], [(l["type"], l["color"]) for l in ls1])
line = hl[0][1]["df"].sort_values("MktWeek")
check("the line is that year only: 52 weeks, each the spread the seed gives it (5 x years + week % 7)",
      list(line["MktYear"].unique()) == [lbl(LAST)] and len(line) == 52 and list(line["Spread"]) == [want_spread(LAST, int(w)) for w in line["MktWeek"]], (len(line), list(line["Spread"])[:6]))
lab = hl[1][1]
check("its label is the year, at the line's LAST week and value (like the current year's label)",
      len(lab["df"]) == 1 and lab["df"]["MktYear"].iloc[0] == lbl(LAST) and int(lab["df"]["MktWeek"].iloc[0]) == int(line["MktWeek"].max())
      and float(lab["df"]["Spread"].iloc[0]) == float(line["Spread"].iloc[-1]), lab["df"].to_dict("records"))
check("it is not near the 5-yr avg label, so it stays where it is (dy 0)", lab["dy"] == 0, lab["dy"])
check("the legend line names it: '%s = blue'" % lbl(LAST), "<b style=\"color:%s\">%s = blue</b>" % (BLUE, lbl(LAST)) in legend(at) and legend(at).index("= black") < legend(at).index("= blue") < legend(at).index("5-yr avg"), legend(at)[-420:])
check("the highlight is drawn UNDER the current year's black line (it is later in the layer order)", hl[-1][0] < max(i for i, l in enumerate(ls1) if l["color"] == BLACK), [(i, l["color"]) for i, l in enumerate(ls1)])
check("a year inside the 5-yr window changes nothing about the zoom (its weeks were already counted once)", domain(ls1) == dom0, (domain(ls1), dom0))
check("the other layers are exactly as before (range, avg, current year)", all(ls1[i]["type"] == ls0[i]["type"] and ls1[i]["color"] == ls0[i]["color"] and (ls1[i]["df"] is None or ls1[i]["df"].equals(ls0[i]["df"])) for i in range(5)))
check("no exception, and the pick is remembered by the picker", not no_exc(at) and at.multiselect(key="spread_hl").value == [lbl(LAST)], no_exc(at))

print("\ntwo years: colours follow the pick order, and a label that lands on the 5-yr avg label is nudged")
MID = CUR - 3                      # the avg of the five window years is exactly this year's line (the seed is linear in the year), so its label would sit ON the "5-yr avg" label
at.multiselect(key="spread_hl").set_value([lbl(MID), lbl(LAST)])
run_in_tab(at, "Spread")
ls2 = layers(at)
hl2 = hl_layers(ls2)
check("the first pick is blue, the second orange; each has a line and a label", [(l["type"], l["color"]) for _, l in hl2] == [("line", BLUE), ("text", BLUE), ("line", ORANGE), ("text", ORANGE)], [(l["type"], l["color"]) for _, l in hl2])
check("and the legend lists them in that order", legend(at).index("%s = blue" % lbl(MID)) < legend(at).index("%s = orange" % lbl(LAST)), legend(at)[-420:])
lab_mid, lab_last = hl2[1][1], hl2[3][1]
check("%s's label would sit on the 5-yr avg label (same week, same value) so it is nudged by whole 12 px steps; %s's has room and is not" % (lbl(MID), lbl(LAST)),
      isinstance(lab_mid["dy"], (int, float)) and lab_mid["dy"] != 0 and lab_mid["dy"] % 12 == 0 and lab_last["dy"] == 0, (lab_mid["dy"], lab_last["dy"]))
check("a label is only ever MOVED: it is still at its line's last point", int(lab_mid["df"]["MktWeek"].iloc[0]) == int(hl2[0][1]["df"]["MktWeek"].max()))

print("\na year outside the 5-yr window")
OLD = lbl(CUR - 7)                 # 2019/20: older than the window (2021/22-2025/26)
at.multiselect(key="spread_hl").set_value([OLD])
run_in_tab(at, "Spread")
ls3 = layers(at)
hl3 = hl_layers(ls3)
check("it can be picked and is drawn (blue, its 52 weeks)", len(hl3) == 2 and hl3[0][1]["color"] == BLUE and list(hl3[0][1]["df"]["MktYear"].unique()) == [OLD] and len(hl3[0][1]["df"]) == 52, [(l["type"], l["color"]) for _, l in hl3])
d3 = domain(ls3)
check("the zoom now includes its weeks: the low end of the y axis moved DOWN (its spreads are far under the window's)", d3[0] < dom0[0], (d3, dom0))
check("the range and the average are still the five window years only (the band does not change)", ls3[2]["df"].equals(ls0[2]["df"]) and ls3[3]["df"].equals(ls0[3]["df"]))
check("no exception", not no_exc(at), no_exc(at))

print("\nfour at most")
FIVE = [lbl(y) for y in range(CUR - 1, CUR - 6, -1)]
at.multiselect(key="spread_hl").set_value(FIVE)
run_in_tab(at, "Spread")
hl4 = hl_layers(layers(at))
check("five picks (the browser would stop at four): only the first four are drawn, in the four colours",
      [l["color"] for _, l in hl4 if l["type"] == "line"] == [BLUE, ORANGE, PURPLE, TEAL] and at.multiselect(key="spread_hl").value == FIVE[:4], ([l["color"] for _, l in hl4], at.multiselect(key="spread_hl").value))

print("\nchanging a location keeps the picks that still make sense and drops the rest")
at.multiselect(key="spread_hl").set_value([OLD, lbl(LAST)])
run_in_tab(at, "Spread")
at.selectbox(key="spread_b").set_value(C)
run_in_tab(at, "Spread")
opts_c = list(at.multiselect(key="spread_hl").options)
check("Loc C starts in 2022, so for A - C the picker offers only 2022/23 on", opts_c == [lbl(y) for y in range(CUR - 1, 2021, -1)], opts_c)
check("%s is gone, %s is kept, no exception" % (OLD, lbl(LAST)), at.multiselect(key="spread_hl").value == [lbl(LAST)] and not no_exc(at), (at.multiselect(key="spread_hl").value, no_exc(at)))
hl5 = hl_layers(layers(at))
check("the chart draws just that one (line + label)", [(l["type"], l["color"]) for _, l in hl5] == [("line", BLUE), ("text", BLUE)], [(l["type"], l["color"]) for _, l in hl5])
at.selectbox(key="spread_b").set_value(B)
run_in_tab(at, "Spread")
check("back on A - B the dropped year does not come back by itself", at.multiselect(key="spread_hl").value == [lbl(LAST)] and not no_exc(at), at.multiselect(key="spread_hl").value)

print("\nthe other best-fit levels")
at.multiselect(key="spread_hl").set_value([lbl(MID)])
at.radio(key="spread_seas_fit").set_value("Full")
run_in_tab(at, "Spread")
ls6 = layers(at)
lab6 = [l for _, l in hl_layers(ls6) if l["type"] == "text"]
check("'Full' (no clamped zoom): still drawn, still nudged off the 5-yr avg label, no exception", len(lab6) == 1 and lab6[0]["dy"] != 0 and not no_exc(at), (lab6[0]["dy"] if lab6 else None, no_exc(at)))

print("\na year with a single week of data is a dot (a one-point line cannot be drawn)")
at.radio(key="spread_seas_fit").set_value("90%")
at.selectbox(key="spread_b").set_value(E)
run_in_tab(at, "Spread")
one_wk = lbl(2020)
check("Loc E posted once in 2020/21: that year is on offer", one_wk in list(at.multiselect(key="spread_hl").options), list(at.multiselect(key="spread_hl").options))
at.multiselect(key="spread_hl").set_value([one_wk])
run_in_tab(at, "Spread")
hl7 = hl_layers(layers(at))
check("it is drawn as a blue point plus its label, and nothing breaks", [(l["type"], l["color"]) for _, l in hl7] == [("point", BLUE), ("text", BLUE)] and len(hl7[0][1]["df"]) == 1 and not no_exc(at),
      ([(l["type"], l["color"]) for _, l in hl7], no_exc(at)))

print("\nthe PNG download (it is built from the same layers, so it has the highlight)")
check("the admin build still offers the PNG download under the highlighted chart", any("PNG" in (getattr(b.proto, "label", "") or "") for b in at.get("download_button")), [getattr(b.proto, "label", "") for b in at.get("download_button")])

print("\nthe client build (view_app.py) has the picker too")
atv = AppTest.from_file(os.path.join(ROOT, "view_app.py"), default_timeout=900)
atv.run()
run_in_tab(atv, "Spread")
pv = atv.multiselect(key="spread_hl")
check("the picker is there with the same years", list(pv.options) == ALL_YEARS and not no_exc(atv), (list(pv.options)[:3], no_exc(atv)))
atv.multiselect(key="spread_hl").set_value([lbl(LAST)])
run_in_tab(atv, "Spread")
hlv = hl_layers(layers(atv))
check("and a pick is drawn there as well", [(l["type"], l["color"]) for _, l in hlv] == [("line", BLUE), ("text", BLUE)] and not no_exc(atv), ([(l["type"], l["color"]) for _, l in hlv], no_exc(atv)))

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
