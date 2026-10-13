"""The Return to Carry block's Harvest basis switch (Streamlit, headless): the calculated method by default, the user's own number on request.

    python tests/test_return_to_carry_block.py

Drives return_to_carry_block.render() with `streamlit.testing.v1.AppTest` over a synthetic series (no database, no network): eight
crop years of weekly bids that climb through the year, flat futures with a 10 / 4 / 2 carry, the newest year in progress on Feb 4 2026.
"""
import os
import re
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from streamlit.testing.v1 import AppTest
except ImportError:                                           # an old Streamlit has no AppTest
    print("  [SKIP] streamlit.testing.v1 is not available")
    sys.exit(0)

import carry_rate as cr                    # noqa: E402
import return_to_carry as rtc              # noqa: E402
import return_to_carry_data as rd          # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ── the page under test ──────────────────────────────────────────────────────────────────────────
SCRIPT = '''
import sys
sys.path.insert(0, "__ROOT__")
from datetime import date, timedelta
import streamlit as st
import carry_rate as cr
import return_to_carry as rtc
import return_to_carry_block as blk

ASOF = date(2026, 2, 4)
SYMS = {"Z": 400.0, "H": 410.0, "K": 414.0, "N": 416.0, "U": 410.0}
obs, futs = [], {}
for i, y in enumerate(range(2018, 2026)):
    start = rtc.first_wednesday(y)
    for k in range(0, 44):
        obs.append({"date": start + timedelta(days=7 * k), "basis": -5.0 + 3 * i + (0.4 + 0.05 * i) * k})
    for k in range(-2, 46):
        futs[start + timedelta(days=7 * k)] = {rtc.contract_symbol(c, y): px for c, px in SYMS.items()}
    for d in rtc.roll_dates(y).values():
        futs[d] = dict(futs.get(d, {}), **{rtc.contract_symbol(c, y): px for c, px in SYMS.items()})
st.session_state.setdefault("scope", "loc-A")
st.session_state.setdefault("measure", "net")
st.session_state.setdefault("message", None)
blk.render(obs=obs, quotes=[], asof=ASOF, grain="Corn", measure=st.session_state["measure"], tab_rate_pct=cr.rate_for(ASOF, None).rate_pct,
           load_futures=lambda root: futs, load_prime=lambda: None, load_fed_funds=lambda: None,
           scope=st.session_state["scope"], location="Test Elevator", message=st.session_state["message"])
'''.replace("__ROOT__", ROOT.replace("\\", "/"))

ASOF = date(2026, 2, 4)
CROP = 2025


def build_expected(overrides=None, measure="net", ship_by=None, window=None):
    """The same series, run directly through the engine (what the page should show)."""
    syms = {"Z": 400.0, "H": 410.0, "K": 414.0, "N": 416.0, "U": 410.0}
    obs, futs = [], {}
    for i, y in enumerate(range(2018, 2026)):
        start = rtc.first_wednesday(y)
        for k in range(0, 44):
            obs.append({"date": start + timedelta(days=7 * k), "basis": -5.0 + 3 * i + (0.4 + 0.05 * i) * k})
        for k in range(-2, 46):
            futs[start + timedelta(days=7 * k)] = {rtc.contract_symbol(c, y): px for c, px in syms.items()}
        for d in rtc.roll_dates(y).values():
            futs[d] = dict(futs.get(d, {}), **{rtc.contract_symbol(c, y): px for c, px in syms.items()})
    obs = [o for o in obs if o["date"] <= ASOF]
    rate = lambda d: cr.rate_for(d, None).rate_pct
    res = rd.run_history(obs, futs, rate, spec=rtc.CORN, b0_overrides=overrides, harvest_window=window)
    return rd.summary_rows(rd.limit_history(res, ship_by), measure), obs


def strip(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def history_rows(at):
    """The by-year table: [[cells as text]], newest first, plus the table's HTML."""
    html = next((m.value for m in at.markdown if "<table" in m.value and "Crop year" in m.value), None)
    if html is None:
        return None, None
    body = re.findall(r"<tbody>(.*?)</tbody>", html, flags=re.S)[0]
    rows = [[strip(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)] for tr in body.split("</tr>") if "<td" in tr]
    return [r for r in rows if re.match(r"\d{4}-\d{2}", r[0])], html


def num(s):
    m = re.search(r"[+-]?\d+(?:\.\d+)?", s.replace("−", "-"))
    return float(m.group()) if m else None


def own_boxes(at):
    """The Harvest basis number box (the window's Start week / Number of weeks boxes are number inputs too)."""
    return [n for n in at.number_input if "Your harvest basis" in n.label]


def ship_html(at):
    return next((m.value for m in at.markdown if "shipment by month" in m.value), "")


def key(kind, scope="loc-A"):
    return "nc_rtc_b0_%s_%s|Corn|%d" % (kind, scope, CROP)


def fresh():
    at = AppTest.from_string(SCRIPT, default_timeout=120)
    at.run()
    return at


exp_rows, _ = build_expected()
calc_b0 = exp_rows[-1]["b0"]
calc_best = exp_rows[-1]["best"]

print("block: the Harvest basis switch starts on the calculated method")
at = fresh()
check("the page renders without an exception", not at.exception, [str(e.value)[:200] for e in at.exception])
rb = next((r for r in at.radio if r.label == "Harvest basis"), None)
check("a Harvest basis switch sits in the block, on 'Calculated' (the report's method) by default, with My own as the other choice",
      rb is not None and rb.value.startswith("Calculated") and "the report uses the first 7" in rb.value and rb.options[-1] == "My own" and rb.key == key("mode"), getattr(rb, "value", None))
check("...the Interest switch is still there beside it", any(r.key == "nc_rtc_rate2" for r in at.radio))
check("no number box and no what-if box until My own is chosen", not own_boxes(at) and not [c for c in at.checkbox if "what-if" in c.label])
rows, html = history_rows(at)
check("the by-year table is the calculated history: the tracked year's harvest basis = the average of its first 7 bids, no OWN pill",
      rows is not None and rows[0][0].startswith("2025-26") and close(num(rows[0][1]), calc_b0, 0.06) and ">OWN<" not in html, (rows or [None])[0])
sh = ship_html(at)
check("the shipment table says 'average of the first 7 weekly bids' and is not flagged own", "average of the first 7 weekly bids, vs Dec" in sh and "your own" not in sh)
check("no own-basis note is shown", not any("Your own harvest basis" in m.value for m in at.markdown))

print("block: choosing My own")
at.radio(key=key("mode")).set_value("My own").run()
check("no exception", not at.exception, [str(e.value)[:200] for e in at.exception])
nb = at.number_input(key=key("val"))
check("a number box appears, in cents vs Dec, starting at the CALCULATED harvest basis (so the user edits from it)",
      "Your harvest basis" in nb.label and "vs Dec" in nb.label and close(nb.value, round(calc_b0, 2)), (nb.label, nb.value, calc_b0))
ck = at.checkbox(key=key("all"))
check("and a what-if box, off", "every crop year" in ck.label and ck.value is False)
rows, html = history_rows(at)
check("with the number untouched the tracked year reads the same harvest basis, now flagged OWN (and the best return is unchanged)",
      html.count(">OWN<") == 1 and close(num(rows[0][1]), round(calc_b0, 2), 0.06) and close(num(rows[0][4]), calc_best, 0.06), rows[0])
check("the own-basis note names it and says earlier years keep theirs",
      any("Your own harvest basis" in m.value and "Earlier years keep their calculated harvest basis" in m.value for m in at.markdown))

print("block: typing the user's own harvest basis (-20)")
at.number_input(key=key("val")).set_value(-20.0).run()
check("no exception", not at.exception, [str(e.value)[:200] for e in at.exception])
own_rows, _ = build_expected({CROP: -20.0})
rows, html = history_rows(at)
check("the tracked year is measured from -20: its harvest basis reads -20.0 with an OWN pill, its best return moved by calculated - own, the best week is the same",
      rows[0][0].startswith("2025-26") and close(num(rows[0][1]), -20.0) and html.count(">OWN<") == 1 and close(num(rows[0][4]), own_rows[-1]["best"], 0.06)
      and close(own_rows[-1]["best"] - calc_best, calc_b0 + 20.0, 1e-6)
      and rows[0][5] == "%s %d" % (exp_rows[-1]["best_date"].strftime("%b"), exp_rows[-1]["best_date"].day), (rows[0], own_rows[-1]["best"]))
check("every earlier year is untouched (same harvest basis and best return as the calculated history)",
      all(close(num(r[1]), e["b0"], 0.06) and close(num(r[4]), e["best"], 0.06) for r, e in zip(rows[1:], reversed(exp_rows[:-1]))), rows[1])
sh = ship_html(at)
check("the shipment table is measured from -20 and says it is yours, with the calculated number beside it",
      "your own, vs Dec · calculated %+.1f" % calc_b0 in sh and "-20.0¢" in sh and "Your own harvest basis</b> is in use" in sh and "average of the first" not in sh)
check("the headline's harvest-basis card says the same", any("your own, vs Dec" in m.value and "Harvest basis" in m.value and "Best return so far" in m.value for m in at.markdown))
check("the table's legend explains OWN", "OWN = measured from the harvest basis you entered" in html)

print("block: the what-if (every crop year)")
at.checkbox(key=key("all")).check().run()
check("no exception", not at.exception, [str(e.value)[:200] for e in at.exception])
all_rows, _ = build_expected({y: -20.0 for y in range(2018, 2026)})
rows, html = history_rows(at)
check("every crop year is measured from -20 and flagged OWN; each best return is what the engine gives for that basis",
      html.count(">OWN<") == len(rows) == len(all_rows) and all(close(num(r[1]), -20.0) and close(num(r[4]), e["best"], 0.06) for r, e in zip(rows, reversed(all_rows))), (len(rows), html.count(">OWN<")))
check("the note says it is a what-if, not the analyst's computation", any("What-if" in m.value and "not what the analyst" in m.value for m in at.markdown))
at.checkbox(key=key("all")).uncheck().run()
rows, html = history_rows(at)
check("unticking it puts the earlier years back on their calculated harvest basis", html.count(">OWN<") == 1 and close(num(rows[1][1]), exp_rows[-2]["b0"], 0.06))

print("block: gross view, and back to calculated")
at.session_state["measure"] = "gross"
at.run()
g_rows, _ = build_expected({CROP: -20.0}, "gross")
rows, html = history_rows(at)
check("the gross view reads the same own basis (its best is the gross best for -20)", close(num(rows[0][1]), -20.0) and close(num(rows[0][4]), g_rows[-1]["best"], 0.06), rows[0])
at.session_state["measure"] = "net"
at.radio(key=key("mode")).set_value(at.radio(key=key("mode")).options[0]).run()
rows, html = history_rows(at)
check("choosing Calculated again drops the number box, every pill and the note — the history is the calculated one again",
      not own_boxes(at) and ">OWN<" not in html and close(num(rows[0][4]), calc_best, 0.06) and not any("Your own harvest basis" in m.value for m in at.markdown))
at.radio(key=key("mode")).set_value("My own").run()
check("...and the number the user typed is still there if they come back to My own (same location)", close(at.number_input(key=key("val")).value, -20.0))

print("block: a number typed for one location does not follow the user to another")
at.session_state["scope"] = "loc-B"
at.run()
rb2 = next(r for r in at.radio if r.label == "Harvest basis")
rows, html = history_rows(at)
check("another location starts on the calculated method again (the widget keys carry the location)",
      rb2.key == key("mode", "loc-B") and rb2.value.startswith("Calculated") and not own_boxes(at) and ">OWN<" not in html)

print("block: the Ship by box — no limit by default, or the study stops at the end of a month")
SB = "nc_rtc_ship_by_corn"
MONTHS_OPT = ["End of " + m for m in ("November", "December", "January", "February", "March", "April", "May", "June")]
at = fresh()
sb = next((s for s in at.selectbox if s.label == "Ship by"), None)
check("a Ship by box sits in the block, on 'No limit' (the best week of the whole season) by default, with November to June after it",
      sb is not None and sb.key == SB and sb.value.startswith("No limit") and "through July" in sb.value and sb.options[1:] == MONTHS_OPT, getattr(sb, "options", None))
rows0, html0 = history_rows(at)
ship0 = ship_html(at)
check("with no limit the page is the whole-season one: seven columns including the summer basis, and nothing about a study end anywhere",
      html0.count("<th ") == 7 and "Best summer basis" in html0 and "Study ends" not in html0 and "shipping by" not in ship0 and "Shipments through" not in ship0
      and not any("shipped by" in m.value for m in at.markdown) and not [c for c in at.caption if "Only shipments through" in c.value or "ship-by limit" in c.value])

at.selectbox(key=SB).set_value("End of March").run()
check("choosing End of March: no exception", not at.exception, [str(e.value)[:200] for e in at.exception])
lim_rows, _ = build_expected(ship_by=3)
rows3, html3 = history_rows(at)
check("the by-year table says the study ends at the end of March and loses the summer-basis column (no week by March is quoted off July)",
      "Study ends at the end of March" in html3 and html3.count("<th ") == 6 and "Best summer basis" not in html3, html3.count("<th "))
check("every crop year's carry, best return, best week and return at the end are the engine's for a March study",
      len(rows3) == len(lim_rows) == 8 and all(close(num(r[2]), e["carry"], 0.06) and close(num(r[3]), e["best"], 0.06)
                                               and r[4] == "%s %d" % (e["best_date"].strftime("%b"), e["best_date"].day) and close(num(r[5]), e["last"], 0.06)
                                               for r, e in zip(rows3, reversed(lim_rows))), (rows3[1], lim_rows[-2]))
check("...and no year's best is higher, or later, than the whole season's, and some years' are lower (stopping early costs something)",
      all(e["best"] <= f["best"] + 1e-9 and e["best_date"] <= f["best_date"] for e, f in zip(lim_rows, exp_rows))
      and any(e["best"] < f["best"] - 1.0 for e, f in zip(lim_rows, exp_rows)), [(round(e["best"], 1), round(f["best"], 1)) for e, f in zip(lim_rows, exp_rows)])
check("the tracked year (in progress on Feb 4, before the limit) reads exactly as it did: the same best, 'IN PROGRESS'", close(num(rows3[0][3]), num(rows0[0][4]), 0.06)
      and "IN PROGRESS" in html3)
check("the headline says the year is shipped by Mar 31 and is 'Best so far, by Mar 31' (it has not reached March)",
      any("shipped by Mar 31" in m.value and "Best so far, by Mar 31" in m.value and "Futures carry" in m.value for m in at.markdown))
sh3 = ship_html(at)
check("the shipment table is cut to Nov 20 .. Mar 20 and says so", "shipping by Mar 31" in sh3 and "Shipments through Mar 31 only" in sh3 and "Mar 20" in sh3
      and not any(("%s 20" % m) in sh3 for m in ("Apr", "May", "Jun", "Jul")), sh3[:200])
caps = [c.value for c in at.caption]
check("the box says what the limit does, and the method caption says where the weeks stop",
      any("Only shipments through the end of March count" in c for c in caps) and any("Weeks run to March 31 (the ship-by limit; the season runs to July 31)" in c for c in caps), caps[-4:])
at.selectbox(key=SB).set_value(at.selectbox(key=SB).options[0]).run()
rows_b, html_b = history_rows(at)
check("choosing No limit again gives back the whole-season page byte for byte (the by-year table and the shipment table)", html_b == html0 and ship_html(at) == ship0)

print("block: the Ship by choice is the user's constraint, so it follows them from one location to the next")
at.selectbox(key=SB).set_value("End of March").run()
at.session_state["scope"] = "loc-B"
at.run()
check("on another location the box still says End of March and the table is the March study",
      at.selectbox(key=SB).value == "End of March" and "Study ends at the end of March" in history_rows(at)[1], at.selectbox(key=SB).value)
at.session_state["message"] = "This line is a freight rate, not an FOB bid, so there is no storage return to track."
at.run()
check("a location the block cannot draw (a message in its place) shows no box", not [s for s in at.selectbox if s.key == SB] and any("freight rate" in i.value for i in at.info))
at.session_state["message"] = None
at.run()
check("...and the choice is still End of March when the block is drawn again (the widget's own state was dropped meanwhile)",
      at.selectbox(key=SB).value == "End of March" and "Study ends at the end of March" in history_rows(at)[1], at.selectbox(key=SB).value)
at.session_state["measure"] = "gross"
at.run()
g_lim, _ = build_expected(ship_by=3, measure="gross")
rows_g, html_g = history_rows(at)
check("the gross view reads the same limit (its best returns are the gross best of a March study)",
      "Study ends at the end of March" in html_g and all(close(num(r[3]), e["best"], 0.06) for r, e in zip(rows_g, reversed(g_lim))), rows_g[1])
at.session_state["measure"] = "net"
at.selectbox(key=SB).set_value("End of June").run()
rows6, html6 = history_rows(at)
l6, _ = build_expected(ship_by=6)
check("End of June keeps the summer-basis column (the study reaches July's contract) and reads the June study's best returns",
      html6.count("<th ") == 7 and "Study ends at the end of June" in html6 and all(close(num(r[4]), e["best"], 0.06) for r, e in zip(rows6, reversed(l6))), html6.count("<th "))
check("a longer study never reads lower than a shorter one, year by year (June >= March)", all(num(a[4]) >= num(b[3]) - 0.06 for a, b in zip(rows6, rows3)))

print("block: the harvest window — Start week / Number of weeks, the report's 1 and 7 by default")
WS, WN, WK = "nc_rtc_win_corn_start", "nc_rtc_win_corn_n", "nc_rtc_win_corn_kept"


def set_window(at, start, n):
    at.number_input(key=WS).set_value(start)
    at.number_input(key=WN).set_value(n)
    at.run()


at = fresh()
bs = next((b for b in at.number_input if b.label == "Start week"), None)
bn = next((b for b in at.number_input if b.label == "Number of weeks"), None)
check("two boxes sit under the Harvest basis switch, Start week 1 and Number of weeks 7: the report's window",
      bs is not None and bn is not None and (bs.key, bn.key) == (WS, WN) and (bs.value, bn.value) == (1, 7) and bs.min == -3, (getattr(bs, "value", None), getattr(bn, "value", None)))
caps = [c.value for c in at.caption]
check("the caption beside them names the default window with the tracked year's dates", any("Weeks 1 to 7 (Oct 1 – Nov 12) for 2025-26 — the report's window" in c for c in caps), caps[:3])
rows_d, html_d = history_rows(at)
check("at the default the whole history is the report's, every year with its own sheet's window: harvest basis and best return match the engine",
      len(rows_d) == len(exp_rows) and all(close(num(r[1]), e["b0"], 0.06) and close(num(r[4]), e["best"], 0.06) for r, e in zip(rows_d, reversed(exp_rows))) and "your window" not in html_d)
check("...and nothing on the page mentions a window of the user's", not any("your window" in m.value for m in at.markdown) and not any("Interest still starts" in c for c in caps))

set_window(at, 3, 4)
check("weeks 3 to 6: no exception", not at.exception, [str(e.value)[:200] for e in at.exception])
win_rows, _ = build_expected(window=(2, 4))
rows_w, html_w = history_rows(at)
check("weeks 3 to 6 are averaged in EVERY crop year (2019-20's own exception included): each year's harvest basis and best return are the engine's for that window",
      len(rows_w) == len(win_rows) and all(close(num(r[1]), e["b0"], 0.06) and close(num(r[4]), e["best"], 0.06) for r, e in zip(rows_w, reversed(win_rows))), (rows_w[0], win_rows[-1]["b0"]))
check("...so the numbers really moved from the report's (every year's harvest basis changed), while the best weeks stayed where they were: only the average moved",
      all(abs(num(a[1]) - num(b[1])) > 0.1 for a, b in zip(rows_w, rows_d)) and all(a[5] == b[5] for a, b in zip(rows_w, rows_d)), [(a[1], b[1], a[5], b[5]) for a, b in zip(rows_w, rows_d)][:3])
check("the by-year table explains the window under it", "Harvest basis = the average of your window, weeks 3 to 6 (Oct 15 – Nov 5, the tracked year's dates)" in html_w)
sh_w = ship_html(at)
check("the shipment table's card and note say the same, and it is not an estimate",
      "average of weeks 3 to 6 (Oct 15 – Nov 5), vs Dec" in sh_w and "not the report's first 7 weekly bids" in sh_w and "average of the first" not in sh_w and "Estimate." not in sh_w, sh_w[:120])
check("the headline's harvest-basis card says it too", any("Best return so far" in m.value and "average of weeks 3 to 6 (Oct 15 – Nov 5), vs Dec" in m.value for m in at.markdown))
caps = [c.value for c in at.caption]
check("the caption names the window, says it applies to every crop year and replaces the report's, and (the window opens before the interest does) says nothing of the interest",
      any(c.startswith("Weeks 3 to 6 (Oct 15 – Nov 5) for 2025-26: the same weeks in every crop year, in place of the report's first 7 and its year-specific windows.") for c in caps)
      and not any("Interest still starts" in c for c in caps), caps[:3])
check("the Calculated choice is still selected, and nothing is flagged OWN",
      next(r for r in at.radio if r.label == "Harvest basis").value.startswith("Calculated") and ">OWN<" not in html_w and not own_boxes(at))

set_window(at, 5, 3)
caps = [c.value for c in at.caption]
check("a window that opens after the interest starts says when the interest does start (the report's third weekly bid, Oct 15), so it is clear the clock is not moved",
      any("Interest still starts at the report's third weekly bid (Oct 15), before this window opens." in c for c in caps), caps[:3])

set_window(at, 3, 4)
at.selectbox(key=SB).set_value("End of March").run()
lim_w, _ = build_expected(window=(2, 4), ship_by=3)
rows_lw, html_lw = history_rows(at)
check("the window and Ship by work together: a March study of the window's harvest basis",
      len(rows_lw) == len(lim_w) and "Study ends at the end of March" in html_lw
      and all(close(num(r[1]), e["b0"], 0.06) and close(num(r[3]), e["best"], 0.06) for r, e in zip(rows_lw, reversed(lim_w))), rows_lw[0])
at.selectbox(key=SB).set_value(at.selectbox(key=SB).options[0]).run()

print("block: the window beside My own — the number box starts at the window's average, and earlier years keep using the window")
at.radio(key=key("mode")).set_value("My own").run()
check("My own with a window: no exception", not at.exception, [str(e.value)[:200] for e in at.exception])
nbw = at.number_input(key=key("val"))
check("the window boxes stay (the earlier years still use them), and the number box starts at the WINDOW's average for the tracked year",
      [b.key for b in at.number_input if b.key in (WS, WN)] == [WS, WN] and close(nbw.value, round(win_rows[-1]["b0"], 2)), (nbw.value, win_rows[-1]["b0"]))
at.number_input(key=key("val")).set_value(-20.0).run()
own_w, _ = build_expected({CROP: -20.0}, window=(2, 4))
rows_ow, html_ow = history_rows(at)
check("the tracked year is measured from -20 (OWN); every earlier year from the window's average",
      close(num(rows_ow[0][1]), -20.0) and html_ow.count(">OWN<") == 1 and all(close(num(r[1]), e["b0"], 0.06) and close(num(r[4]), e["best"], 0.06) for r, e in zip(rows_ow, reversed(own_w))), rows_ow[:2])
check("the shipment table shows -20 as the user's own, with the window's average beside it as the calculated number",
      "your own, vs Dec · calculated %+.1f" % win_rows[-1]["b0"] in ship_html(at))
at.radio(key=key("mode")).set_value(at.radio(key=key("mode")).options[0]).run()
check("back to Calculated: the number box and the OWN pills go, the window stays", not own_boxes(at) and ">OWN<" not in history_rows(at)[1] and (at.number_input(key=WS).value, at.number_input(key=WN).value) == (3, 4))

print("block: the window is the user's way of establishing a basis, so it follows them from one location to the next")
at.session_state["scope"] = "loc-B"
at.run()
check("on another location the boxes still read 3 and 4 and the table is the window's",
      (at.number_input(key=WS).value, at.number_input(key=WN).value) == (3, 4) and "average of weeks 3 to 6 (Oct 15 – Nov 5)" in ship_html(at))
at.session_state["message"] = "This line is a freight rate, not an FOB bid, so there is no storage return to track."
at.run()
check("a location the block cannot draw (a message in its place) shows no boxes", not [b for b in at.number_input if b.key in (WS, WN)] and any("freight rate" in i.value for i in at.info))
at.session_state["message"] = None
at.run()
check("...and the window is still 3 and 4 when the block is drawn again (the boxes' own state was dropped meanwhile)",
      (at.number_input(key=WS).value, at.number_input(key=WN).value) == (3, 4) and at.session_state[WK] == (3, 4))
set_window(at, 1, 7)
rows_b2, html_b2 = history_rows(at)
check("back at Start week 1 and 7 weeks the page is the report's again, byte for byte (the by-year table and the shipment table)", html_b2 == html0 and ship_html(at) == ship0)

print("block: a window that starts in September — bids quoted off CU are rolled to Dec at the Dec - Sep spread of the last day before first notice")
SCRIPT_SEP = SCRIPT.replace('st.session_state.setdefault("scope", "loc-A")', '''futs[date(2025, 8, 28)] = {"ZCU25": 450.0, "ZCZ25": 470.0}
for k, (b, t) in zip((-3, -2, -1), ((30.0, "ZCU25"), (28.0, "ZCU25"), (26.0, "ZCZ25"))):
    obs.append({"date": rtc.first_wednesday(2025) + timedelta(days=7 * k), "basis": b, "tag": t})
st.session_state.setdefault("scope", "loc-A")''')
assert SCRIPT_SEP != SCRIPT
at_s = AppTest.from_string(SCRIPT_SEP, default_timeout=120)
at_s.run()
rows_s0, _ = history_rows(at_s)
at_s.number_input(key=WS).set_value(-2)
at_s.number_input(key=WN).set_value(5)
at_s.run()
check("a September start: no exception", not at_s.exception, [str(e.value)[:200] for e in at_s.exception])
rows_s, html_s = history_rows(at_s)
want_sep = (30.0 - 20 + 28.0 - 20 + 26.0 + (-5.0 + 3 * 7) + (-5.0 + 3 * 7 + 0.75)) / 5
check("the tracked year's harvest basis = the five bids Sep 10 .. Oct 8 with the two CU-quoted ones rolled by the +20 spread and the CZ one left alone: (10 + 8 + 26 + 16 + 16.75) / 5 = %.2f" % want_sep,
      close(num(rows_s[0][1]), want_sep, 0.06) and rows_s[0][0].startswith("2025-26"), (rows_s[0], want_sep))
check("...and the report's own window is not moved by September bids (the default page's harvest basis for the year is the same as without them)", close(num(rows_s0[0][1]), num(rows_d[0][1]), 0.06), (rows_s0[0], rows_d[0]))
caps_s = [c.value for c in at_s.caption]
check("the caption names the September weeks, and a note under it says how CU bids are moved, from which day, and the level (+20.00 -> a +10 vs Sep bid reads -10.00 vs Dec)",
      any(c.startswith("Weeks -2 to 2 (Sep 10 – Oct 8) for 2025-26: the same weeks in every crop year") for c in caps_s)
      and any("moved to Dec by the Dec − Sep spread of the last trading day before the contract's first notice day (Aug 28)" in c and "2025-26: Dec − Sep = +20.00¢, so a basis of +10 vs Sep reads -10.00 vs Dec." in c
              and "bids already quoted off Dec" in c for c in caps_s), caps_s[:4])
at_n = fresh()
at_n.number_input(key=WS).set_value(-2)
at_n.number_input(key=WN).set_value(5)
at_n.run()
caps_n = [c.value for c in at_n.caption]
check("with no Aug 28 price in the futures history the note says the CU-quoted bids are left out (no guess), and the page still draws",
      not at_n.exception and any("That spread is not in the futures history for 2025-26, so its CU-quoted September bids are left out of the average." in c for c in caps_n), caps_n[:4])
set_window(at_n, 1, 7)
check("a window that stays in October carries no September note", not any("September (CU) futures" in c.value for c in at_n.caption))

SOYWIN_SCRIPT = '''
import sys
sys.path.insert(0, "__ROOT__")
import streamlit as st
import return_to_carry as rtc
import return_to_carry_block as blk
st.session_state["picked"] = blk._harvest_window_choice(rtc.SOY, 2026)
'''.replace("__ROOT__", ROOT.replace("\\", "/"))
at_w = AppTest.from_string(SOYWIN_SCRIPT, default_timeout=60)
at_w.run()
check("soybeans: their own pair of boxes (a separate key from corn's), 1 and 7, and the report's window comes back as None",
      [b.key for b in at_w.number_input] == ["nc_rtc_win_soy_start", "nc_rtc_win_soy_n"] and [b.value for b in at_w.number_input] == [1, 7] and at_w.session_state["picked"] is None)
at_w.number_input(key="nc_rtc_win_soy_start").set_value(7)
at_w.number_input(key="nc_rtc_win_soy_n").set_value(3)
at_w.run()
check("...a window of weeks 7 to 9 comes back as (6, 3) — Start week less one, then the count — and the caption names the bean interest clock (the fifth weekly bid, Nov 4)",
      tuple(at_w.session_state["picked"]) == (6, 3)
      and any("Interest still starts at the report's fifth weekly bid (Nov 4), before this window opens." in c.value for c in at_w.caption), [c.value for c in at_w.caption])

SOY_SCRIPT = '''
import sys
sys.path.insert(0, "__ROOT__")
import streamlit as st
import return_to_carry as rtc
import return_to_carry_block as blk
st.session_state["picked"] = blk._ship_by_choice(rtc.SOY)
'''.replace("__ROOT__", ROOT.replace("\\", "/"))
at_s = AppTest.from_string(SOY_SCRIPT, default_timeout=60)
at_s.run()
sbs = at_s.selectbox[0]
check("soybeans: no limit runs through September; the months to choose from are November to August, and no limit is None",
      sbs.key == "nc_rtc_ship_by_soy" and sbs.options[0].startswith("No limit") and "through September" in sbs.options[0] and sbs.options[-1] == "End of August"
      and len(sbs.options) == 11 and at_s.session_state["picked"] is None, (sbs.key, sbs.options[:2], sbs.options[-1]))
at_s.selectbox[0].set_value("End of August").run()
check("...and a month comes back as its number (8), the corn and soybean boxes being separate", at_s.session_state["picked"] == 8 and at_s.selectbox[0].key != SB)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
