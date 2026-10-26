"""The 5-year AVERAGE behind the rail seasonal charts is Olympic (seasonal_band): each week's highest and lowest year are dropped before the mean, so one freak crop year cannot
drag it. The RANGE is the true one - the lowest to the highest of every year in the window, nothing trimmed.

    python tests/test_seasonal_band.py

Kolten 2026-10-20: UP Allen Station's 2022/23 (a Sep-Dec average of +59 against -12 to +27 in the other four years) set the top of the 5-yr band in 38 weeks of 52, and BN
Hereford looked the same - "I think we need to take an olympic avg approach to make cleaner". 2026-10-26: "take out the ex hi / low on these and make true 5 year range. Keep the
average line as we have it." Pure functions plus the emailed chart's spec; no database.
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
check("the range is the TRUE one - lowest to highest of every year: 0 to 100, not the Olympic 10 to 40", (sb.range_lo([40, 100, 20, 10, 0]), sb.range_hi([40, 100, 20, 10, 0])) == (0.0, 100.0))
check("the old trimmed range functions are gone (nothing can quietly draw ex hi/lo again)", not hasattr(sb, "olympic_lo") and not hasattr(sb, "olympic_hi"))
check("more than five (a six-year week) still drops one at each end", sb.kept([1, 2, 3, 4, 5, 6]) == [2.0, 3.0, 4.0, 5.0])
check("fewer than five years: nothing to spare, the plain mean and the full range",
      sb.kept([1, 2, 3, 100]) == [1.0, 2.0, 3.0, 100.0] and sb.olympic_avg([1, 2, 3, 100]) == 26.5 and (sb.range_lo([1, 2, 3, 100]), sb.range_hi([1, 2, 3, 100])) == (1.0, 100.0))
check("a missing value is not a year (a NaN or None among five leaves four: plain)", sb.kept([1, nan, 3, None, 5, 7]) == [1.0, 3.0, 5.0, 7.0] and sb.olympic_avg([10, nan, 20, 30, 40]) == 25.0)
check("... nor does it enter the range", (sb.range_lo([4, nan, None, 9]), sb.range_hi([4, nan, None, 9])) == (4.0, 9.0))
check("no values, no number", isnan(sb.olympic_avg([])) and isnan(sb.range_lo([])) and isnan(sb.range_hi([nan, None])))
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
check("a full week: the Olympic average 23.3, the true range 0 to 100", abs(band.loc[1, "avg"] - 70 / 3) < 1e-9 and band.loc[1, "lo"] == 0 and band.loc[1, "hi"] == 100, band.loc[1].to_dict())
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
check("the average drops 2022 (23.3, not the plain 34) but the range keeps it: 0 to 100 - the freak year is in the band, not in the line", abs(b["avg"] - 70 / 3) < 1e-6 and b["lo"] == 0 and b["hi"] == 100, b)
legend = json.dumps(spec)
check("the legend names them: '5-yr Olympic avg' and '5-yr range (2021/22–2025/26)' - no 'ex hi/lo'", "5-yr Olympic avg" in legend and "5-yr range (2021/22" in legend and "ex hi/lo" not in legend and "5-yr average" not in legend, legend[:300])

print("the BN corridors look back three years (Kolten 2026-10-21: 'use the last 3 years for the BN corridors')")
import rail_corridors as rc  # noqa: E402

check("band_years: 3 for the BN basis corridors", all(rc.band_years(m) == 3 for m in ("BN Hereford", "BN PNW", "BN PNW CP", "BN PNW BE", "BN COBO")), [(m, rc.band_years(m)) for m in ("BN Hereford", "BN PNW", "BN COBO")])
check("an unregistered 'BN ...' market follows its carrier", rc.band_years("BN Something New") == 3)
check("freight ($/car) and every other carrier keep five", all(rc.band_years(m) == 5 for m in ("BN Freight", "BN 110 Shuttle", "UP Freight", "CPKC Freight", "UP Illinois (Mex)", "UP Group 3", "CSX Columbus", "NS Ft Wayne", "CN 105s", "Nowhere", "", None)))
by_md_b, mkt_dates_b = {}, {"BN Hereford": set()}
for my, lvl in LEVEL.items():
    for wk in range(1, 53):
        d = (date(my, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat()
        by_md_b[("BN Hereford", d)] = {"Spot": {"period": "Spot", "bid": lvl, "period_order": 0, "commodity": "Corn"}}
        mkt_dates_b["BN Hereford"].add(d)
for wk in range(1, 9):
    d = (date(2026, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat()
    by_md_b[("BN Hereford", d)] = {"Spot": {"period": "Spot", "bid": -15, "period_order": 0, "commodity": "Corn"}}
    mkt_dates_b["BN Hereford"].add(d)
with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
    spec_b = json.loads(rail_report._seasonal_png("BN Hereford", by_md_b, mkt_dates_b))
sets_b = [v for v in spec_b["datasets"].values() if v and {"avg", "lo", "hi"} <= set(v[0])]
bb = sets_b[0][10]
check("the band is the last three completed years only (2023-2025 = 20, 10, 0): mean 10, range 0 to 20 — the 2021-22 spike years are out", abs(bb["avg"] - 10.0) < 1e-6 and bb["lo"] == 0 and bb["hi"] == 20, bb)
legend_b = json.dumps(spec_b)
check("nothing is trimmed with three years, so the legend says '3-yr average' and '3-yr range (2023/24–2025/26)', not Olympic",
      "3-yr average" in legend_b and "3-yr range (2023/24" in legend_b and "Olympic" not in legend_b and "ex hi/lo" not in legend_b, legend_b[:300])

print("a partial year is not a year behind the band")
check("years with enough weekly bids are kept, oldest first", sb.full_years({2023: 48, 2022: 14, 2024: 50, 2025: 30}, range(2021, 2026)) == [2023, 2024, 2025])
check("a year with no bids has no weeks", sb.full_years({}, [2024]) == [])
check("exactly the minimum counts, one fewer does not", sb.full_years({2024: sb.MIN_YEAR_WEEKS}, [2024]) == [2024] and sb.full_years({2024: sb.MIN_YEAR_WEEKS - 1}, [2024]) == [])
check("a smaller minimum lets a forward period's part-year through", sb.full_years({2024: 10}, [2024], 1) == [2024])

# UP Allen Station with its estimated weeks hidden: 14 weeks of ACTUAL bids in 2022/23 (+70, from 2023-05-31), then three whole years (20, 10, 0), then this year
MK = "UP Illinois (Mex)"
by_md_a, mkt_dates_a = {}, {MK: set()}


def put_a(d, bid):
    by_md_a[(MK, d)] = {"Spot": {"period": "Spot", "bid": bid, "period_order": 0, "commodity": "Corn"}}
    mkt_dates_a[MK].add(d)


for my, lvl, weeks in ((2022, 70, range(39, 53)), (2023, 20, range(1, 53)), (2024, 10, range(1, 53)), (2025, 0, range(1, 53))):
    for wk in weeks:
        put_a((date(my, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), lvl)
for wk in range(1, 9):
    put_a((date(2026, 9, 1) + timedelta(days=7 * (wk - 1))).isoformat(), -15)
with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
    spec_a = json.loads(rail_report._seasonal_png(MK, by_md_a, mkt_dates_a))
band_a = [v for v in spec_a["datasets"].values() if v and {"avg", "lo", "hi"} <= set(v[0])][0]
w45 = next(r for r in band_a if r["MktWeek"] == 45)
check("the 14-week 2022/23 is left out of the band even in the weeks it covers (week 45 = mean of 20, 10, 0 - not lifted by the +70)", abs(w45["avg"] - 10.0) < 1e-6 and w45["lo"] == 0 and w45["hi"] == 20, w45)
legend_a = json.dumps(spec_a, ensure_ascii=True)
check("... and the legend counts three years, not four", "3-yr average" in legend_a and "3-yr range (2023/24" in legend_a and "4-yr" not in legend_a, legend_a[:300])

# the rule is for a corridor whose estimated weeks are hidden only: the same series on any other corridor keeps its part-year in the band, exactly as before
by_md_c = {("CSX Columbus", d): v for (_m, d), v in by_md_a.items()}
mkt_dates_c = {"CSX Columbus": set(mkt_dates_a[MK])}
with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
    spec_c = json.loads(rail_report._seasonal_png("CSX Columbus", by_md_c, mkt_dates_c))
legend_c = json.dumps(spec_c, ensure_ascii=True)
band_c = [v for v in spec_c["datasets"].values() if v and {"avg", "lo", "hi"} <= set(v[0])][0]
w45c = next(r for r in band_c if r["MktWeek"] == 45)
check("any other corridor keeps a part-year behind its band as before: '4-yr average (2022/23-2025/26)'", "4-yr average" in legend_c and "4-yr range (2022/23" in legend_c, legend_c[:300])
check("... so week 45 still includes the +70 of the part-year (mean of 70, 20, 10, 0 = 25)", abs(w45c["avg"] - 25.0) < 1e-6 and w45c["hi"] == 70, w45c)
# and with the estimates shown again, Allen is back to the old whole-window treatment too
os.environ["RAIL_SHOW_ESTIMATES"] = "1"
with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
    legend_s = json.dumps(json.loads(rail_report._seasonal_png(MK, by_md_a, mkt_dates_a)), ensure_ascii=True)
os.environ.pop("RAIL_SHOW_ESTIMATES")
check("... and Allen with the estimates shown again counts the part-year like every other corridor", "4-yr average" in legend_s, legend_s[:300])

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("all seasonal_band checks passed")
