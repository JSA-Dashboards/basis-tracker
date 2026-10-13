"""CBOT grain trading days (grain_calendar.py) and the import's guard that uses them.

    python tests/test_grain_calendar.py

Pure: no network, no database.
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grain_calendar as gc            # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


D = date.fromisoformat

print("open on the federal holidays CBOT grains trade through")
check("Columbus Day, Mon 2026-10-12, is a trading day", gc.is_trading_day(D("2026-10-12")))
check("Veterans Day, Wed 2026-11-11, is a trading day", gc.is_trading_day(D("2026-11-11")))
check("Columbus Day 2027 (10-11) and Veterans Day 2027 (Thu 11-11) too",
      gc.is_trading_day(D("2027-10-11")) and gc.is_trading_day(D("2027-11-11")))
check("the day after Thanksgiving and Christmas Eve 2026 (early closes) trade",
      gc.is_trading_day(D("2026-11-27")) and gc.is_trading_day(D("2026-12-24")))

print("closed on CME's ten grain holidays")
H2026 = {"2026-01-01": "New Year's Day", "2026-01-19": "Martin Luther King Jr. Day", "2026-02-16": "Presidents Day",
         "2026-04-03": "Good Friday", "2026-05-25": "Memorial Day", "2026-06-19": "Juneteenth",
         "2026-07-03": "Independence Day", "2026-09-07": "Labor Day", "2026-11-26": "Thanksgiving",
         "2026-12-25": "Christmas"}
got = {d.isoformat(): n for d, n in gc.holidays(2026).items()}
check("2026: exactly the ten closures (July 4 on a Saturday is taken Friday 07-03)", got == H2026, got)
check("each 2026 closure is not a trading day", all(not gc.is_trading_day(D(d)) for d in H2026))

# river-fob-portal/check_bidsheet_fresh.py HOLIDAYS: the sure grain closures it lists, 2026-11 .. 2027.
RIVER = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
         "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
ours = {d.isoformat() for y in (2026, 2027) for d in gc.holidays(y) if d >= D("2026-11-01")}
check("agrees with river-fob's HOLIDAYS, plus Juneteenth 2027 (Sat, taken Fri 06-18)",
      ours == RIVER | {"2027-06-18"}, sorted(ours ^ RIVER))
check("Good Friday follows Easter (2027-03-26, 2028-04-14, 2029-03-30)",
      all(gc.holiday_name(D(d)) == "Good Friday" for d in ("2027-03-26", "2028-04-14", "2029-03-30")))
check("New Year's on a Saturday closes no weekday (2022-01-01: Fri 2021-12-31 traded)",
      gc.is_trading_day(D("2021-12-31")) and D("2021-12-31") not in gc.holidays(2021)
      and not any(n == "New Year's Day" for n in gc.holidays(2022).values()))
check("New Year's on a Sunday is taken Monday (2023-01-02)", gc.holiday_name(D("2023-01-02")) == "New Year's Day")
check("no Juneteenth closure before 2022", "Juneteenth" not in gc.holidays(2021).values())
check("weekends are never trading days", not gc.is_trading_day(D("2026-10-10")) and not gc.is_trading_day(D("2026-10-11")))

print("prior_trading_day")
check("before Tue 2026-10-13 is Columbus Day Mon 10-12", gc.prior_trading_day(D("2026-10-13")) == D("2026-10-12"))
check("before Thu 2026-11-12 is Veterans Day Wed 11-11", gc.prior_trading_day(D("2026-11-12")) == D("2026-11-11"))
check("before Mon 2026-11-30 is Fri 11-27 (Thanksgiving skipped on the 2nd step)",
      gc.prior_trading_day(D("2026-11-30")) == D("2026-11-27") and gc.prior_trading_day(D("2026-11-30"), 2) == D("2026-11-25"))
check("before Mon 2026-07-06 is Thu 07-02 (Independence Day observed Fri 07-03)",
      gc.prior_trading_day(D("2026-07-06")) == D("2026-07-02"))

print("auto_import's guard uses it (the source, not imported: auto_import loads every scraper)")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto_import.py"), encoding="utf-8").read()
guard = src[src.index("def _is_trading_day"):src.index("def run_chs")]
check("the guard calls grain_calendar.is_trading_day", "grain_calendar.is_trading_day" in guard)
check("and no longer the federal calendar", "holidays.US" not in guard and "_holidays" not in src)

print()
print("FAILED: %s" % FAILS if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
