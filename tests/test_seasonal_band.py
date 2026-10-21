"""The 5-year average and range behind the rail seasonal charts are Olympic (seasonal_band): each week's highest and lowest year are dropped before the average and the
range are taken, so one freak crop year cannot drag either.

    python tests/test_seasonal_band.py

Kolten 2026-10-20: UP Allen Station's 2022/23 (a Sep-Dec average of +59 against -12 to +27 in the other four years) set the top of the 5-yr band in 38 weeks of 52, and BN
Hereford looked the same - "I think we need to take an olympic avg approach to make cleaner". Pure functions plus the emailed chart's spec; no database.
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

import seasonal_band as sb  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


nan = float("nan")
isnan = lambda x: x != x  # noqa: E731

print("the Olympic rule")
check("five years: the highest and the lowest are dropped (0 10 20 40 100 -> 10 20 40)", sb.kept([40, 100, 20, 10, 0]) == [10.0, 20.0, 40.0], sb.kept([40, 100, 20, 10, 0]))
check("the average is the mean of the middle three: 23.3, not the plain 34", abs(sb.olympic_avg([40, 100, 20, 10, 0]) - 70 / 3) < 1e-9, sb.olympic_avg([40, 100, 20, 10, 0]))
check("the range runs from the lowest to the highest of what is left: 10 to 40, not 0 to 100", (sb.olympic_lo([40, 100, 20, 10, 0]), sb.olympic_hi([40, 100, 20, 10, 0])) == (10.0, 40.0))
check("more than five (a six-year week) still drops one at each end", sb.kept([1, 2, 3, 4, 5, 6]) == [2.0, 3.0, 4.0, 5.0])
check("fewer than five years: nothing to spare, the plain mean and the full range",
      sb.kept([1, 2, 3, 100]) == [1.0, 2.0, 3.0, 100.0] and sb.olympic_avg([1, 2, 3, 100]) == 26.5 and (sb.olympic_lo([1, 2, 3, 100]), sb.olympic_hi([1, 2, 3, 100])) == (1.0, 100.0))
check("a missing value is not a year (a NaN or None among five leaves four: plain)", sb.kept([1, nan, 3, None, 5, 7]) == [1.0, 3.0, 5.0, 7.0] and sb.olympic_avg([10, nan, 20, 30, 40]) == 25.0)
check("no values, no number", isnan(sb.olympic_avg([])) and isnan(sb.olympic_lo([])) and isnan(sb.olympic_hi([nan, None])))
check("negative basis works the same way (-30 -20 -17 -5 +65 -> -20 -17 -5)", sb.kept([-30, -20, -17, -5, 65]) == [-20.0, -17.0, -5.0])
check("a tie at the top or bottom drops one copy only", sb.kept([5, 5, 5, 5, 5]) == [5.0, 5.0, 5.0] and sb.kept([1, 1, 2, 3, 3]) == [1.0, 2.0, 3.0])
check("is_trimmed: five years or more", sb.is_trimmed(5) and sb.is_trimmed(6) and not sb.is_trimmed(4) and not sb.is_trimmed(0))

print("the band the emailed chart draws")
import pandas as pd  # noqa: E402

import rail_report  # noqa: E402

rows = []
for wk in (1, 2, 3):
    for my, v in ((2021, 40), (2022, 100), (2023, 20), (2024, 10), (2025, 0)):
        rows.append({"MktWeek": wk, "MyNum": my, "Bid": v})
rows += [{"MktWeek": 4, "MyNum": 2021, "Bid": 40.0}, {"MktWeek": 4, "MyNum": 2022, "Bid": 100.0}, {"MktWeek": 4, "MyNum": 2023, "Bid": 20.0}, {"MktWeek": 4, "MyNum": 2024, "Bid": 10.0}]  # four years
band = rail_report._five_year_band(pd.DataFrame(rows)).set_index("MktWeek")
check("a full week: avg 23.3, range 10 to 40", abs(band.loc[1, "avg"] - 70 / 3) < 1e-9 and band.loc[1, "lo"] == 10 and band.loc[1, "hi"] == 40, band.loc[1].to_dict())
check("a week with only four years is not trimmed: avg 42.5, range 10 to 100", band.loc[4, "avg"] == 42.5 and band.loc[4, "lo"] == 10 and band.loc[4, "hi"] == 100, band.loc[4].to_dict())
check("the columns the chart reads", sorted(rail_report._five_year_band(pd.DataFrame(rows)).columns) == ["MktWeek", "avg", "hi", "lo"])

print("the emailed chart itself (the Vega-Lite spec the renderer is handed)")
import vl_convert  # noqa: E402

MARKET = "UP Illinois (Dom)"
LEVEL = {2019: 5, 2020: 30, 2021: 40, 2022: 100, 2023: 20, 2024: 10, 2025: 0}          # the window is 2021-2025; 2022 is the freak year
by_md, mkt_dates = {}, {MARKET: set()}


def add(d, bid):
    by_md[(MARKET, d)] = {"Spot": {"period": "Spot", "bid": bid, "period_order": 0, "commodity": "Corn"}}
    mkt_dates[MARKET].add(d)


for my, lvl in LEVEL.items():
    for wk in range(1, 53):
        add((date(my, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), lvl)
for wk in range(1, 9):
    add((date(2026, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), -15)
with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
    spec = json.loads(rail_report._seasonal_png(MARKET, by_md, mkt_dates))
band_sets = [v for v in spec["datasets"].values() if v and {"avg", "lo", "hi"} <= set(v[0])]
check("the chart carries the band data", len(band_sets) >= 1 and len(band_sets[0]) == 52, [len(v) for v in band_sets])
b = band_sets[0][10]
check("the band drops 2022: range 10 to 40 (not 0 to 100), average 23.3 (not the plain 34)", abs(b["avg"] - 70 / 3) < 1e-6 and b["lo"] == 10 and b["hi"] == 40, b)
legend = json.dumps(spec)
check("the legend names it: '5-yr Olympic avg' and '5-yr range ex hi/lo (2021/22–2025/26)'", "5-yr Olympic avg" in legend and "5-yr range ex hi/lo (2021/22" in legend and "5-yr average" not in legend, legend[:300])

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("all seasonal_band checks passed")
