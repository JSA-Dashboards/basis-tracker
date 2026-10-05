"""return_to_carry_data.py — turn the app's tables into the inputs return_to_carry needs, and the history out of it.

  obs_from_rail(rows, market)          a rail corridor's weekly nearby bid (the 'Spot' row; once a corridor stopped
                                       posting Spot, the nearest forward period) with the contract it is quoted off
  obs_from_snapshots(snaps, grain, f)  the same for a basis location: its spot row, else its front forward row
  run_history(obs, futs, rate_on)      every crop year the series covers -> [CropYear]
  summary_rows / seasonal_points       the table and the chart's points, for the net or the gross measure

Pure functions over plain data (database access stays in the app): see return_to_carry for the method.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import net_carry as nc
import return_to_carry as rtc

# Weekly futures from the analyst's yearly workbooks for the crop years BEFORE the settlement archive starts
# (1996-97 .. 2006-07): date, symbol, price_cents. The database has everything from late 2006 on.
SHEET_FUTURES_PATH = Path(__file__).parent / "data" / "rtc_futures_1996_2006.csv"


def load_sheet_futures(path: Path = SHEET_FUTURES_PATH) -> dict:
    """{date: {symbol: cents}} from the committed CSV ({} if it is missing)."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                d = date.fromisoformat(r["date"])
                out.setdefault(d, {})[r["symbol"]] = float(r["price_cents"])
    except (OSError, ValueError, KeyError):
        return {}
    return out


def merge_futures(*sources: dict) -> dict:
    """Combine {date: {symbol: cents}} maps; a later source wins where both have a (date, symbol)."""
    out: dict = {}
    for src in sources:
        for d, syms in (src or {}).items():
            out.setdefault(d, {}).update(syms)
    return out


def _parse_date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def obs_from_rail(rows: list[dict], market: str, commodity: str = "Corn") -> list[dict]:
    """One bid per posting date for a corridor: its 'Spot' bid, else the nearest forward period's bid.
    rows = rail_fob rows ({date, market, commodity, period, period_order, futures, bid})."""
    by_date: dict = {}
    for r in rows:
        if r["market"] != market or (r.get("commodity") or "Corn") != commodity or r.get("bid") is None:
            continue
        d = _parse_date(r["date"])
        if d is None:
            continue
        rank = 0 if r.get("period") == "Spot" else 1 + (r.get("period_order") or 0)
        cur = by_date.get(d)
        if cur is None or rank < cur[0]:
            by_date[d] = (rank, float(r["bid"]), r.get("futures"))
    return [{"date": d, "basis": b, "tag": f} for d, (_, b, f) in sorted(by_date.items())]


def obs_from_snapshots(snaps: list, grain: str, grain_disp) -> list[dict]:
    """One bid per calendar day for a basis location (the newest snapshot of the day): the spot row for the grain,
    else its front forward row (net_carry's ladder order, first quote with a basis)."""
    by_day: dict = {}
    for s in snaps:
        d = _parse_date(s.timestamp)
        if d is not None:
            by_day[d] = s                                       # snapshots arrive oldest -> newest
    out = []
    for d, s in sorted(by_day.items()):
        rows = [r for r in s.rows if r.basisCents is not None and grain_disp(r.grain) == grain]
        spot = next((r for r in rows if r.isSpot), None)
        if spot is not None:
            out.append({"date": d, "basis": float(spot.basisCents), "tag": spot.futuresSymbol or None})
            continue
        items = [{"delivery": r.deliveryMonth, "futures": r.futuresSymbol, "basis": r.basisCents}
                 for r in rows if not r.isSpot]
        norm, _ = nc._normalize(items)
        if norm:
            out.append({"date": d, "basis": float(norm[0]["basis"]), "tag": norm[0]["futures"] or None})
    return out


def crop_years_in(obs: list[dict]) -> list[int]:
    """The crop years a series has at least one bid in (Oct 1 .. Jul 31)."""
    ys = set()
    for o in obs:
        d = o["date"]
        if d.month >= 10:
            ys.add(d.year)
        elif d.month <= 7:
            ys.add(d.year - 1)
    return sorted(ys)


MIN_CROP_YEAR = 2004      # the weekly archive's dates are true Wednesdays from Oct 2004; before that they drift a day a year


def repeated_years(results: list) -> set:
    """Crop years whose weekly bids are a copy of the year before's (>= 90% of 20+ shared weeks identical): the
    archive holds 2006-07's bids a second time as 2007-08 — a copy-paste, not a market. They would read as a real
    year, so they are dropped (and reported)."""
    bad = set()
    for prev, cy in zip(results, results[1:]):
        a = {w.idx: w.basis for w in prev.weeks}
        b = {w.idx: w.basis for w in cy.weeks}
        common = [k for k in a if k in b]
        if len(common) >= 20 and sum(1 for k in common if a[k] == b[k]) >= 0.9 * len(common):
            bad.add(cy.crop_year)
    return bad


def run_history_noted(obs: list[dict], futs: dict, rate_on, min_weeks: int = 12, min_year: int = MIN_CROP_YEAR) -> tuple:
    """(results, skipped): every crop year the series covers from `min_year` on, oldest first, without the years that
    only repeat the previous one (`skipped` = their labels). A year needs some weeks to say anything, so one with fewer
    than `min_weeks` of bids is left out unless it is the newest (the one in progress)."""
    ys = [y for y in crop_years_in(obs) if y >= min_year]
    out = []
    for y in ys:
        cy = rtc.build_crop_year(obs, futs, y, rate_on)
        if len(cy.weeks) >= min_weeks or (y == ys[-1] and cy.weeks):
            out.append(cy)
    bad = repeated_years(out)
    return [cy for cy in out if cy.crop_year not in bad], [rtc.crop_label(y) for y in sorted(bad)]


def run_history(obs: list[dict], futs: dict, rate_on, min_weeks: int = 12, min_year: int = MIN_CROP_YEAR) -> list:
    """run_history_noted without the note."""
    return run_history_noted(obs, futs, rate_on, min_weeks, min_year)[0]


def _value(w, measure: str):
    return w.net if measure == "net" else w.gross


def summary_rows(results: list, measure: str = "net") -> list[dict]:
    """One row per crop year for the table: harvest basis, Dec-Jul futures carry, best summer basis, the best return
    on the measure (and its week), and the return at the last week."""
    rows = []
    for cy in results:
        best = cy.best.get(measure)
        last = next((w for w in reversed(cy.weeks) if _value(w, measure) is not None), None)
        rows.append({
            "crop_year": cy.crop_year, "label": cy.label, "weeks": len(cy.weeks), "complete": cy.complete,
            "b0": cy.b0, "b0_weeks": cy.b0_weeks, "carry": cy.dec_jul_carry,
            "summer": None if cy.summer is None else cy.summer.basis,
            "summer_date": None if cy.summer is None else cy.summer.date,
            "best": None if best is None else _value(best, measure),
            "best_date": None if best is None else best.date, "best_tag": None if best is None else best.tag,
            "last": None if last is None else _value(last, measure),
            "last_date": None if last is None else last.date,
        })
    return rows


def seasonal_points(results: list, measure: str = "net") -> list[dict]:
    """Every weekly return as {crop, week (0 = first Wednesday of October), date, value} for the overlay chart."""
    pts = []
    for cy in results:
        for w in cy.weeks:
            v = _value(w, measure)
            if v is not None:
                pts.append({"crop": cy.label, "week": w.idx, "date": w.date, "value": float(v)})
    return pts
