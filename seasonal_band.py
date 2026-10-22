"""seasonal_band.py - the 5-year average and range behind the rail seasonal charts, Olympic style.

One freak crop year drags a plain 5-year average and blows out its range: on 2026-10-20 UP Allen Station's 2022/23 (a Sep-Dec average of +59 against -12 to +27 in
the other four years of its window) set the top of the band in 38 weeks of 52 and lifted the October average from -7 to +7; BN Hereford looked the same (Kolten: "I think we need to take
an olympic avg approach to make cleaner"). For each marketing week the highest and the lowest year are dropped; the average is the mean of what is left and the band
runs from the lowest to the highest of what is left. Trimming needs five years in that week (MIN_YEARS): with fewer there is nothing to spare, and the plain mean and
min / max are used.

Pure - values in, numbers out. rail_report._five_year_band (the emailed chart) and app.py's Rail FOB tab call these through pandas groupby().agg().
"""
from __future__ import annotations

MIN_YEARS = 5      # a week's values are trimmed only when there are at least this many years to trim from
MIN_YEAR_WEEKS = 26   # a prior marketing year is one of the years behind a spot band only when it has this many weekly values (of 52)


def kept(values):
    """The week's values, sorted, with the highest and the lowest dropped when there are MIN_YEARS or more (a missing value is not a year)."""
    vs = sorted(float(v) for v in values if v is not None and v == v)
    return vs[1:-1] if len(vs) >= MIN_YEARS else vs


def olympic_avg(values) -> float:
    k = kept(values)
    return sum(k) / len(k) if k else float("nan")


def olympic_lo(values) -> float:
    k = kept(values)
    return k[0] if k else float("nan")


def olympic_hi(values) -> float:
    k = kept(values)
    return k[-1] if k else float("nan")


def is_trimmed(n_years: int) -> bool:
    """Whether a window of n_years completed years gets the Olympic treatment (what a legend or caption may claim)."""
    return n_years >= MIN_YEARS


def full_years(weeks_by_year, years, min_weeks: int = MIN_YEAR_WEEKS) -> list:
    """Of `years`, the marketing years with at least `min_weeks` weekly values in `weeks_by_year` ({year: weeks with a bid}), oldest first. A year with fewer is a partial one:
    with UP Allen Station's estimated weeks hidden its 2022/23 holds only the 14 weeks from its first actual bid (2023-05-31), and counted as a year it would put a few months of
    that one freak year into the average and range of those weeks alone while the legend called the window a whole number of years."""
    return sorted(y for y in years if weeks_by_year.get(y, 0) >= min_weeks)
