"""Archive weeks that are an ESTIMATE rather than a posted bid carry bid_raw = 'est': never shown as text, tagged "est" next to the number on the board and in the email table, and
counted in a caption / chart subtitle where the 5-yr average and range are built from them.

    python tests/test_estimated_weeks.py

Kolten 2026-10-21: UP Allen Station's 'Allen Station UP' column is real, 'Est Mexico Allen' (Group 3 + 20) is not - "prior to this we need to flag as estimated or even consider removing".
Pure functions and the Vega-Lite spec the emailed chart is handed; no database.
"""
import json
import os
import sys
from datetime import date, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL"):
    os.environ[k] = ""

import rail_corridors as rc  # noqa: E402
import rail_report  # noqa: E402
import vl_convert  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


print("what the flag looks like")
check("'est' is never printed as the cell's text: the number shows", rail_report._disp(-12, "est") == "-12" and rail_report._disp(5, "est") == "+5")
check("a desk '?' still shows as '?', a plain number as before", rail_report._disp(None, "?") == "?" and rail_report._disp(7, None) == "+7" and rail_report._disp(None, None) is None)
TDR = "x"
est_cell = {"bid": 25, "bid_raw": "est", "offer": None, "offer_raw": None}
html_bid = rail_report._bo_cell(est_cell, False, TDR)
check("the email table shows the bid with a small 'est' tag", ">+25 <span" in html_bid and ">est</span>" in html_bid, html_bid)
check("a normal bid has no tag, and the offer column never does", "est" not in rail_report._bo_cell({"bid": 25, "bid_raw": None}, False, TDR) and "est" not in rail_report._bo_cell(est_cell, True, TDR))
check("the corridor notes", "Allen Station UP" in rc.estimate_note("UP Illinois (Mex)") and rc.estimate_note("CSX Columbus") == "" and "2023" in rc.estimate_short("UP Illinois (Mex)"))
check("Allen (Dom)'s note says it is Mexico less 5 cents from 2023 (a rule, not posted bids)",
      "less 5" in rc.estimate_note("UP Illinois (Dom)") and "not posted" in rc.estimate_note("UP Illinois (Dom)") and "less 5c" in rc.estimate_short("UP Illinois (Dom)"))

print("the emailed chart")
MARKET = "UP Illinois (Mex)"
LEVEL = {2019: 5, 2020: 30, 2021: 40, 2022: 100, 2023: 20, 2024: 10, 2025: 0}


def build(est_years):
    by_md, mkt_dates = {}, {MARKET: set()}

    def add(d, bid, est):
        by_md[(MARKET, d)] = {"Spot": {"period": "Spot", "bid": bid, "period_order": 0, "commodity": "Corn", "bid_raw": "est" if est else None}}
        mkt_dates[MARKET].add(d)

    for my, lvl in LEVEL.items():
        for wk in range(1, 53):
            add((date(my, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), lvl, my in est_years)
    for wk in range(1, 9):
        add((date(2026, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), -15, False)
    return by_md, mkt_dates


def spec_of(by_md, mkt_dates):
    with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
        return json.loads(rail_report._seasonal_png(MARKET, by_md, mkt_dates))


by_md, mkt_dates = build({2021, 2022})
check("_est_dates finds the flagged posting dates", len(rail_report._est_dates(MARKET, by_md, mkt_dates)) == 104 and min(rail_report._est_dates(MARKET, by_md, mkt_dates)).startswith("2021-09"))
sp = spec_of(by_md, mkt_dates)
title = sp.get("title")
check("two flagged years of the five: the chart says 104 of 260 weeks", isinstance(title, dict) and "104 of 260 weeks" in title.get("subtitle", ""), title)
check("and why (the corridor's short note)", isinstance(title, dict) and "actual Allen Station bids start May 2023" in title.get("subtitle", ""), title)
check("the title itself is unchanged", isinstance(title, dict) and title.get("text") == "Allen Station (Mex) · Spot Corn Basis Seasonal", title)
by_md2, mkt_dates2 = build(set())
sp2 = spec_of(by_md2, mkt_dates2)
check("nothing flagged: a plain title, no subtitle", isinstance(sp2.get("title"), str) and "estimates" not in json.dumps(sp2.get("title")), sp2.get("title"))
check("a flagged week outside the 5-yr window (2019) does not trigger it", "estimates" not in json.dumps(spec_of(*build({2019, 2020})).get("title")))

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("all estimated-week checks passed")
