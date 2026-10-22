"""Archive weeks that are an ESTIMATE rather than a posted bid carry bid_raw = 'est': never shown as text, tagged "est" next to the number on the board and in the email table, and
counted in a caption / chart subtitle where the 5-yr average and range are built from them.

    python tests/test_estimated_weeks.py

Kolten 2026-10-21: UP Allen Station's 'Allen Station UP' column is real, 'Est Mexico Allen' (Group 3 + 20) is not - "prior to this we need to flag as estimated or even consider removing".
Pure functions and the Vega-Lite spec the emailed chart is handed, and (from 2026-10-22: "hide data that is pure estimate at least for now") the rail archive's loaders, which leave
the flagged weeks out unless asked - on a throwaway SQLite file, never Snowflake.
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL"):
    os.environ[k] = ""

import database  # noqa: E402
import rail_corridors as rc  # noqa: E402
import rail_report  # noqa: E402
import vl_convert  # noqa: E402

database.DB_PATH = Path(tempfile.mkdtemp(prefix="est_hide_")) / "t.db"
assert database._backend() == "sqlite", "refusing to run on a non-sqlite backend"
database.init_db()
os.environ.pop("RAIL_SHOW_ESTIMATES", None)

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


def build(est_years, market=MARKET):
    by_md, mkt_dates = {}, {market: set()}

    def add(d, bid, est):
        by_md[(market, d)] = {"Spot": {"period": "Spot", "bid": bid, "period_order": 0, "commodity": "Corn", "bid_raw": "est" if est else None}}
        mkt_dates[market].add(d)

    for my, lvl in LEVEL.items():
        for wk in range(1, 53):
            add((date(my, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), lvl, my in est_years)
    for wk in range(1, 9):
        add((date(2026, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), -15, False)
    return by_md, mkt_dates


def spec_of(by_md, mkt_dates, market=MARKET):
    with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
        return json.loads(rail_report._seasonal_png(market, by_md, mkt_dates))


by_md, mkt_dates = build({2021, 2022})
check("_est_dates finds the flagged posting dates", len(rail_report._est_dates(MARKET, by_md, mkt_dates)) == 104 and min(rail_report._est_dates(MARKET, by_md, mkt_dates)).startswith("2021-09"))
sp = spec_of(by_md, mkt_dates)
title = sp.get("title")
check("two flagged years of the five: the chart says 104 of 260 weeks", isinstance(title, dict) and "104 of 260 weeks" in title.get("subtitle", ""), title)
check("and why (the corridor's short note)", isinstance(title, dict) and "actual Allen Station bids start May 2023" in title.get("subtitle", ""), title)
check("the title itself is unchanged", isinstance(title, dict) and title.get("text") == "Allen Station (Mex) · Spot Corn Basis Seasonal", title)
by_md2, mkt_dates2 = build(set(), "CSX Columbus")
sp2 = spec_of(by_md2, mkt_dates2, "CSX Columbus")
check("a corridor with no estimates, nothing flagged: a plain title, no subtitle", isinstance(sp2.get("title"), str), sp2.get("title"))
check("a flagged week outside the 5-yr window (2019) does not trigger the estimates subtitle", "estimates" not in json.dumps(spec_of(*build({2019, 2020})).get("title")))

print("\nthe estimated weeks are hidden for now: the chart says why Allen's history is short")
sp3 = spec_of(*build(set()))
t3 = sp3.get("title")
check("Allen Station's chart carries the hidden-weeks note as its subtitle", isinstance(t3, dict) and "actual bids begin May 2023" in t3.get("subtitle", "") and "not shown" in t3.get("subtitle", ""), t3)
check("... and the title itself is unchanged", isinstance(t3, dict) and t3.get("text") == "Allen Station (Mex) \u00b7 Spot Corn Basis Seasonal", t3)
os.environ["RAIL_SHOW_ESTIMATES"] = "1"
t4 = spec_of(*build(set())).get("title")
os.environ.pop("RAIL_SHOW_ESTIMATES")
check("with the estimates shown again that note is gone (nothing flagged = a plain title)", isinstance(t4, str), t4)
check("the note exists for both Allen corridors and no other", rc.estimate_hidden_note("UP Illinois (Mex)") and rc.estimate_hidden_note("UP Illinois (Dom)") and rc.estimate_hidden_note("CSX Columbus") == "")

print("\nthe rail archive's loaders leave the estimated weeks out")
import return_to_carry_data as rtc_data  # noqa: E402


def put(date_, market, period, bid, raw, source="manual"):
    c.execute("INSERT INTO rail_fob (date, source, market, rail, commodity, period, period_order, futures, bid, offer, bid_raw, offer_raw) "
              "VALUES (?, ?, ?, 'UP', 'Corn', ?, 0, NULL, ?, NULL, ?, NULL)", (date_, source, market, period, bid, raw))


conn = database.get_conn()
c = conn.cursor()
put("2022-03-02", "UP Illinois (Mex)", "Spot", 20, "est")         # an estimated week
put("2022-03-02", "BN Hereford", "Spot", 100, None)               # a posted bid the same day
put("2022-03-09", "UP Illinois (Mex)", "Spot", 22, "est")         # a date that holds only an estimate
put("2026-10-14", "UP Illinois (Mex)", "Spot", -5, None)          # an actual bid
put("2026-10-14", "CSX Columbus", "Spot", None, "?")              # the desk's '?' is not an estimate and stays
put("2026-10-14", "UP Illinois (Dom)", "Spot", -10, "est")        # a rule-derived week whose Mex week is an estimate
put("2026-10-14", "CSX Columbus", "Spot", 3, None, source="palmetto")
conn.commit()
conn.close()
rows = database.get_rail_fob_all("manual")
check("default: the three estimated weeks are not returned (3 of 6 rows)", len(rows) == 3 and not any(r["bid_raw"] == "est" for r in rows), [(r["date"], r["market"], r["bid_raw"]) for r in rows])
check("... an ordinary bid, a NULL flag and the desk's '?' all stay", {r["market"] for r in rows} == {"BN Hereford", "UP Illinois (Mex)", "CSX Columbus"} and any(r["bid_raw"] == "?" for r in rows))
check("include_estimates=True brings them back (6 rows)", len(database.get_rail_fob_all("manual", include_estimates=True)) == 6)
os.environ["RAIL_SHOW_ESTIMATES"] = "1"
check("RAIL_SHOW_ESTIMATES=1 does too", len(database.get_rail_fob_all("manual")) == 6 and database.rail_estimates_shown())
check("... and an explicit False still wins over it", len(database.get_rail_fob_all("manual", include_estimates=False)) == 3)
os.environ["RAIL_SHOW_ESTIMATES"] = "0"
check("RAIL_SHOW_ESTIMATES=0 is hidden", len(database.get_rail_fob_all("manual")) == 3 and not database.rail_estimates_shown())
os.environ.pop("RAIL_SHOW_ESTIMATES")
check("posting dates: a date holding only estimates is gone, one that also has a posted bid stays",
      database.get_rail_fob_dates("manual") == ["2026-10-14", "2022-03-02"] and "2022-03-09" in database.get_rail_fob_dates("manual", include_estimates=True))
board = database.get_rail_fob("manual", "2022-03-02")
check("one day's board leaves the estimated row out", [r["market"] for r in board] == ["BN Hereford"] and len(database.get_rail_fob("manual", "2022-03-02", include_estimates=True)) == 2, board)
check("the other source is unaffected", len(database.get_rail_fob_all("palmetto")) == 1)
obs = rtc_data.obs_from_rail(database.get_rail_fob_all("manual"), "UP Illinois (Mex)", "Corn")
check("downstream, Return to Carry / the Spread tab see Allen's actual bids only (one week, 2026-10-14)", [str(o["date"]) for o in obs] == ["2026-10-14"], obs)

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("all estimated-week checks passed")
