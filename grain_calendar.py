"""grain_calendar.py — the days CBOT grain futures are closed (CME Group's grain holiday calendar).

Why not the US federal calendar: CBOT grains trade on Columbus Day and Veterans Day. The daily import used
`holidays.US()` and so skipped Mon 2026-10-12 entirely (ADM scrape, bids, futures capture) although the market
was open and the Bid Sheet came in. CME closes grains on ten days a year:

    New Year's Day, Martin Luther King Jr. Day, Presidents Day, Good Friday, Memorial Day, Juneteenth,
    Independence Day, Labor Day, Thanksgiving, Christmas

A fixed-date holiday on a Saturday is taken the Friday before and on a Sunday the Monday after (2026-07-03,
2027-06-18, 2027-12-24, 2027-07-05) — except New Year's Day on a Saturday, which closes nothing (the Friday is
the last trading day of the year; 2021-12-31 traded). Early closes (the day after Thanksgiving, Christmas Eve)
are trading days. One-off closures (a national day of mourning) are not predictable: put them in
`EXTRA_CLOSED` when CME announces one.

Cross-check: river-fob-portal's check_bidsheet_fresh.py HOLIDAYS lists the same closures for 2026-11 to 2027,
less Juneteenth 2027 (Sat 06-19, taken Fri 06-18), which it left out as a doubtful date.
"""
from __future__ import annotations

from datetime import date, timedelta

EXTRA_CLOSED: set[date] = set()


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of the month; n = -1 is the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> dict[date, str]:
    """{date: name} of the weekdays CBOT grains are closed in `year`."""
    out = {
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Presidents Day",
        _easter(year) - timedelta(days=2): "Good Friday",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving",
    }
    ny = date(year, 1, 1)
    if ny.weekday() != 5:                     # Saturday: no weekday closure
        out[_observed(ny)] = "New Year's Day"
    for month, day, name in ((6, 19, "Juneteenth"), (7, 4, "Independence Day"), (12, 25, "Christmas")):
        if name == "Juneteenth" and year < 2022:   # CME first closed for it in 2022
            continue
        out[_observed(date(year, month, day))] = name
    for d in EXTRA_CLOSED:
        if d.year == year:
            out.setdefault(d, "Exchange closure")
    return out


def holiday_name(d: date) -> str | None:
    """The holiday that closes grains on `d`, or None."""
    return holidays(d.year).get(d)


def is_trading_day(d: date) -> bool:
    """True when CBOT grains trade on `d`: a weekday that is not a grain-market holiday."""
    return d.weekday() < 5 and holiday_name(d) is None


def prior_trading_day(d: date, n: int = 1) -> date:
    """The n-th trading day before `d`."""
    count = 0
    while True:
        d -= timedelta(days=1)
        if is_trading_day(d):
            count += 1
            if count >= n:
                return d
