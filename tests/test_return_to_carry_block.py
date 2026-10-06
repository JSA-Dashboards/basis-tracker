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
blk.render(obs=obs, quotes=[], asof=ASOF, grain="Corn", measure=st.session_state["measure"], tab_rate_pct=cr.rate_for(ASOF, None).rate_pct,
           load_futures=lambda root: futs, load_prime=lambda: None, load_fed_funds=lambda: None,
           scope=st.session_state["scope"], location="Test Elevator")
'''.replace("__ROOT__", ROOT.replace("\\", "/"))

ASOF = date(2026, 2, 4)
CROP = 2025


def build_expected(overrides=None, measure="net"):
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
    res = rd.run_history(obs, futs, rate, spec=rtc.CORN, b0_overrides=overrides)
    return rd.summary_rows(res, measure), obs


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
      rb is not None and rb.value.startswith("Calculated") and "first 7 weekly bids" in rb.value and rb.options[-1] == "My own" and rb.key == key("mode"), getattr(rb, "value", None))
check("...the Interest switch is still there beside it", any(r.key == "nc_rtc_rate2" for r in at.radio))
check("no number box and no what-if box until My own is chosen", not at.number_input and not [c for c in at.checkbox if "what-if" in c.label])
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
      not at.number_input and ">OWN<" not in html and close(num(rows[0][4]), calc_best, 0.06) and not any("Your own harvest basis" in m.value for m in at.markdown))
at.radio(key=key("mode")).set_value("My own").run()
check("...and the number the user typed is still there if they come back to My own (same location)", close(at.number_input(key=key("val")).value, -20.0))

print("block: a number typed for one location does not follow the user to another")
at.session_state["scope"] = "loc-B"
at.run()
rb2 = next(r for r in at.radio if r.label == "Harvest basis")
rows, html = history_rows(at)
check("another location starts on the calculated method again (the widget keys carry the location)",
      rb2.key == key("mode", "loc-B") and rb2.value.startswith("Calculated") and not at.number_input and ">OWN<" not in html)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
