"""seasonal_band.py - the 5-year average and range behind the rail seasonal charts: an Olympic average, a true range.

One freak crop year drags a plain 5-year average: on 2026-10-20 UP Allen Station's 2022/23 (a Sep-Dec average of +59 against -12 to +27 in the other four years of its window) lifted the
October average from -7 to +7; BN Hereford looked the same (Kolten: "I think we need to take an olympic avg approach to make cleaner"). So the AVERAGE is Olympic: for each marketing week the
highest and the lowest year are dropped and it is the mean of what is left. Trimming needs five years in that week (MIN_YEARS): with fewer there is nothing to spare and the plain mean is used.

The RANGE is not trimmed. The first version drew the Olympic range too (lowest to highest of what was left); Kolten 2026-10-26: "take out the ex hi / low on these and make true 5 year
range. Keep the average line as we have it." So the band runs from the lowest to the highest of EVERY year in the window, the freak year included, behind the Olympic average line.

Pure - values in, numbers out. rail_report._five_year_band (the emailed charts: rail and Inco) and app.py's Rail FOB tab call these through pandas groupby().agg().
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


def range_lo(values) -> float:
    """The lowest year of the week: the band's floor. Every year counts - nothing is trimmed (a missing value is not a year)."""
    vs = [float(v) for v in values if v is not None and v == v]
    return min(vs) if vs else float("nan")


def range_hi(values) -> float:
    """The highest year of the week: the band's ceiling."""
    vs = [float(v) for v in values if v is not None and v == v]
    return max(vs) if vs else float("nan")


def is_trimmed(n_years: int) -> bool:
    """Whether a window of n_years completed years gets the Olympic AVERAGE (what a legend or caption may claim; the range is never trimmed)."""
    return n_years >= MIN_YEARS


def full_years(weeks_by_year, years, min_weeks: int = MIN_YEAR_WEEKS) -> list:
    """Of `years`, the marketing years with at least `min_weeks` weekly values in `weeks_by_year` ({year: weeks with a bid}), oldest first. A year with fewer is a partial one:
    with UP Allen Station's estimated weeks hidden its 2022/23 holds only the 14 weeks from its first actual bid (2023-05-31), and counted as a year it would put a few months of
    that one freak year into the average and range of those weeks alone while the legend called the window a whole number of years."""
    return sorted(y for y in years if weeks_by_year.get(y, 0) >= min_weeks)
