"""return_to_carry_data.py — turn the app's tables into the inputs return_to_carry needs, and the history out of it.

  obs_from_rail(rows, market)          a rail corridor's weekly nearby bid (the 'Spot' row; once a corridor stopped
                                       posting Spot, the nearest forward period) with the contract it is quoted off
  obs_from_snapshots(snaps, grain, f)  the same for a basis location: its spot row, else its front forward row
  run_history(obs, futs, rate_on)      every crop year the series covers -> [CropYear]   (spec=rtc.SOY for soybeans)
  summary_rows / seasonal_points       the table and the chart's points, for the net or the gross measure
  quotes_from_rail / _from_snapshots   every posted forward period of a corridor / location, with its futures tag
  parse_label / shipment_quotes        which months a period covers ('JFM', 'FH Dec', 'Dec 1-20') -> one bid per shipment month
  shipment_table(...)                  the report's page-1 table (break-even, current and best bids by shipment month)
  own_b0_map / b0_override(s)          the user's OWN harvest basis in place of the calculated average (shipment_table, run_history)

Pure functions over plain data (database access stays in the app): see return_to_carry for the method.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import net_carry as nc
import return_to_carry as rtc

# Weekly futures from the analyst's yearly workbooks for the crop years BEFORE the settlement archive starts
# (corn 1996-97 .. 2006-07, plus Dec 2007 for Oct 3 - Nov 28 2007 from the 07colcry sheet: a front contract the stored
# settlements lack that autumn; soybeans 2005-06 .. Oct 2007): date, symbol, price_cents. The database has everything from
# late 2006 on except those front contracts.
SHEET_FUTURES_PATH = Path(__file__).parent / "data" / "rtc_futures_1996_2006.csv"
SHEET_FUTURES_PATHS = {"ZC": SHEET_FUTURES_PATH, "ZS": Path(__file__).parent / "data" / "rtc_futures_soy_2005_2007.csv"}


def load_sheet_futures(path: Path | None = None, root: str = "ZC") -> dict:
    """{date: {symbol: cents}} from the committed CSV of a commodity ({} if it is missing)."""
    out: dict = {}
    try:
        with open(path or SHEET_FUTURES_PATHS[root], encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                d = date.fromisoformat(r["date"])
                out.setdefault(d, {})[r["symbol"]] = float(r["price_cents"])
    except (OSError, ValueError, KeyError):
        return {}
    return out


def futures_map(rows) -> dict:
    """{date: {symbol: cents}} from stored settlement rows [{'date', 'symbol', 'price_cents'}] (a SELECT of futures_prices): the shape the
    tracker and the shipment table read. Rows with no usable date or price are skipped; a date may arrive as a date or an ISO string."""
    out: dict = {}
    for r in rows or []:
        d = _parse_date(r.get("date"))
        sym, px = r.get("symbol"), r.get("price_cents")
        if d is None or not sym or px is None:
            continue
        try:
            out.setdefault(d, {})[str(sym).strip()] = float(px)
        except (TypeError, ValueError):
            continue
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


# ── the forward quotes behind the report's page 1 ───────────────────────────────────────────────────
_MON = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
        "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}
_NAME = re.compile(r"\b(" + "|".join(sorted(_MON, key=len, reverse=True)) + r")\b")
_NOISE = re.compile(r"\b(bid|offer|split|late|early|bal|balance|thru|through|no holiday)\b")
_RING = "JFMAMJJASOND" * 2


def _expand_initials(tok: str):
    """'JFM' -> (1, 2, 3), 'AMJJ' -> (4, 5, 6, 7): a run of month initials, if it spells exactly one run of months."""
    s = tok.upper()
    n = len(s)
    if n < 2 or n > 12 or not re.fullmatch(r"[JFMASOND]+", s):
        return None
    starts = [i for i in range(12) if _RING[i:i + n] == s]
    return tuple((starts[0] + k) % 12 + 1 for k in range(n)) if len(starts) == 1 else None


@lru_cache(maxsize=4096)
def parse_label(label: str | None) -> dict | None:
    """What delivery window a posted period covers: {'months': (m, ...), 'kind', 'span', 'year'}, or None when it names
    no month ('Spot', 'Nearby', 'NC 26', note rows). (Cached — a few hundred distinct labels cover years of postings;
    the result is shared, so treat it as read-only.)

    kind   'full'    a whole month ('Dec', 'Dec 2026', 'Nov (no holiday)')
           'window'  a month's first weeks to about the 20th ('Dec 1-20', 'Nov 1-25')
           'lh'/'fh' the last / first half of a month ('LH Nov', 'FH Dec')
           'bundle'  several months at one price ('JFM', 'AMJJ', 'DJFM', 'Jan-Jul', 'Oct/Nov', 'LH Oct / FH Nov')"""
    t = (label or "").lower()
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\s+", " ", _NOISE.sub(" ", t)).strip()
    if not t:
        return None
    ym = re.search(r"(?<!\d)(20\d\d)(?!\d)", t)
    year = int(ym.group(1)) if ym else None
    names = list(_NAME.finditer(t))
    if not names:
        # a run of initials ('JFM', 'LH OND'): the one word that is not a half-month qualifier must spell the months —
        # a stray letter ('k JFM', left by the old paste parser) or an unknown word ('ex AM') is not a package
        core = [w for w in t.split() if w not in ("fh", "lh", "mp", "lp")]
        months = _expand_initials(core[0]) if len(core) == 1 else None
        return {"months": months, "kind": "bundle", "span": len(months), "year": None} if months else None
    seq, prev_end = [], 0
    for i, m in enumerate(names):
        pre = t[prev_end:m.start()]
        post = t[m.end():(names[i + 1].start() if i + 1 < len(names) else len(t))]
        q = None
        if re.search(r"\b(fh|f\.h\.|first half)\s*$", pre) or re.match(r"\s*(fh|first half)\b", post):
            q = "fh"
        elif re.search(r"\b(lh|l\.h\.|last half)\s*$", pre) or re.match(r"\s*(lh|last half)\b", post):
            q = "lh"
        seq.append((_MON[m.group(1)], q, post))
        prev_end = m.end()
    if len(seq) == 1:
        month, q, post = seq[0]
        kind = q or "full"
        w = re.match(r"\s*(?:20\d\d\s*)?(\d{1,2})\s*-\s*(\d{1,2})\b", post)
        if w and q is None:
            s, e = int(w.group(1)), int(w.group(2))
            kind = "full" if (s <= 1 and e >= 28) else "lh" if s >= 16 else "fh" if e <= 15 else "window"
        return {"months": (month,), "kind": kind, "span": 1, "year": year}
    months = [seq[0][0]]
    for i in range(len(seq) - 1):
        a, b = seq[i][0], seq[i + 1][0]
        j = re.sub(r"\b(?:fh|lh|first half|last half)\b|\d+", " ", t[names[i].end():names[i + 1].start()])
        if re.search(r"-|\bto\b", j) or not re.sub(r"[().:\s]", "", j):       # Jan-Jul, Oct-Mar, 'Jan July': every month in between
            months += [(a - 1 + k) % 12 + 1 for k in range(1, ((b - a) % 12) + 1)]
        else:                                                                # Oct/Nov, LH Oct / FH Nov, 'Nov, Dec': just those two
            months.append(b)
    months = tuple(dict.fromkeys(months))
    return {"months": months, "kind": "bundle", "span": len(months), "year": None}


_KIND_RANK = {"full": 0, "window": 1, "lh": 2, "fh": 3}


def shipment_quotes(raw: list[dict], crop_year: int, spec: rtc.Spec = rtc.CORN) -> list[dict]:
    """One bid per (posting date, shipment month Nov .. Jul — soybeans Nov .. Aug) of a crop year, picked from everything posted that day.
    raw = [{'date', 'label', 'tag', 'basis'}]. A month's own quote beats a package that covers it (full month, then a
    window to the 20th, then the last half, then the first), the narrowest package fills a month with no quote of its
    own ('JFM' for Jan, Feb and Mar; 'AMJJ' for Apr-Jul — as in the analyst's template), and of equals the higher bid wins.
    A label's year is read from its posting date (the next such month), so a 'Dec' posted in March is next year's."""
    pick: dict = {}
    lo, hi = date(crop_year, 8, 1), date(crop_year + 1, *spec.horizon)   # postings that can speak for this table
    for q in raw:
        if q.get("basis") is None or not (lo <= q["date"] <= hi):
            continue
        info = parse_label(q.get("label"))
        if info is None:
            continue
        d = q["date"]
        for m in info["months"]:
            if m not in spec.ship_months:
                continue
            y = info["year"] if (info["year"] is not None and info["kind"] != "bundle") else (d.year if m >= d.month else d.year + 1)
            if y != (crop_year if m >= 10 else crop_year + 1):
                continue
            rank = (_KIND_RANK.get(info["kind"], 4 + info["span"] / 100.0), -float(q["basis"]))
            cur = pick.get((d, m))
            if cur is None or rank < cur[0]:
                pick[(d, m)] = (rank, {"date": d, "month": m, "basis": float(q["basis"]), "tag": q.get("tag"),
                                       "label": q.get("label"), "kind": info["kind"]})
    return sorted((v[1] for v in pick.values()), key=lambda r: (r["date"], r["month"]))


def quotes_from_rail(rows: list[dict], market: str, commodity: str = "Corn") -> list[dict]:
    """Every posted period of a corridor with a bid: [{'date', 'label', 'tag', 'basis'}] (rail_fob rows)."""
    out = []
    for r in rows:
        if r["market"] != market or (r.get("commodity") or "Corn") != commodity or r.get("bid") is None:
            continue
        d = _parse_date(r["date"])
        if d is not None:
            out.append({"date": d, "label": r.get("period") or "", "tag": r.get("futures"), "basis": float(r["bid"])})
    return out


def quotes_from_snapshots(snaps: list, grain: str, grain_disp) -> list[dict]:
    """Every forward row of a basis location's newest snapshot of each day, for the grain."""
    by_day: dict = {}
    for s in snaps:
        d = _parse_date(s.timestamp)
        if d is not None:
            by_day[d] = s                                       # snapshots arrive oldest -> newest
    out = []
    for d, s in sorted(by_day.items()):
        for r in s.rows:
            if r.isSpot or r.basisCents is None or grain_disp(r.grain) != grain:
                continue
            out.append({"date": d, "label": r.deliveryMonth or "", "tag": r.futuresSymbol or None, "basis": float(r.basisCents)})
    return out


def harvest_estimate(raw: list[dict], crop_year: int, asof: date, futs: dict, max_age_days: int = 14, spec: rtc.Spec = rtc.CORN):
    """The harvest basis before the weekly bids exist (the first lands the first Wednesday of October), against the spec's base
    contract (corn Dec, soybeans Jan). Corn: the average of the latest posted FH Oct, LH Oct and FH Nov bids — else the one
    'LH Oct / FH Nov' package, else the average of the whole-month Oct and Nov bids. Soybeans: the October bids and the November
    bids (halves averaged within each month), weighted 4 : 3 like the 4-5 October and 2-3 November weeks the sheets average.
    (value, posting date, [labels]) or None."""
    wanted = {((10,), "fh"), ((10,), "lh"), ((11,), "fh")}
    for d in sorted({q["date"] for q in raw if q["date"] <= asof and (asof - q["date"]).days <= max_age_days}, reverse=True):
        vals, labels, pack, monthly = [], [], None, {}
        by_month: dict = {}
        for q in raw:
            if q["date"] != d or q.get("basis") is None:
                continue
            info = parse_label(q.get("label"))
            if info is None or (info["year"] not in (None, crop_year)):
                continue
            moved = rtc.rebase(q["basis"], q.get("tag"), spec.base, crop_year, futs, d, spec)
            if moved is None:
                continue
            if spec.key == "soy":
                if info["months"] in ((10,), (11,)) and info["kind"] in ("full", "window", "fh", "lh"):
                    by_month.setdefault(info["months"][0], []).append((moved[0], q["label"]))
                elif info["kind"] == "bundle" and info["months"] == (10, 11):
                    pack = (moved[0], q["label"])
                continue
            if (info["months"], info["kind"]) in wanted:
                vals.append(moved[0])
                labels.append(q["label"])
            elif info["kind"] == "bundle" and info["months"] == (10, 11):
                pack = (moved[0], q["label"])
            elif info["kind"] in ("full", "window") and info["months"] in ((10,), (11,)):
                m = info["months"][0]
                if m not in monthly or moved[0] > monthly[m][0]:
                    monthly[m] = (moved[0], q["label"])
        if spec.key == "soy" and by_month:
            avg = {m: sum(v for v, _ in xs) / len(xs) for m, xs in by_month.items()}
            wt = {10: 4.0, 11: 3.0}
            tot = sum(wt[m] for m in avg)
            return (sum(avg[m] * wt[m] for m in avg) / tot, d, [lab for m in sorted(by_month) for _, lab in by_month[m]])
        if vals:
            return sum(vals) / len(vals), d, labels
        if pack is not None:
            return pack[0], d, [pack[1]]
        if monthly:
            return sum(v for v, _ in monthly.values()) / len(monthly), d, [lab for _, lab in monthly.values()]
    return None


def shipment_table(obs: list[dict], raw: list[dict], futs: dict, rate_on, asof: date, measure: str = "net",
                   spec: rtc.Spec = rtc.CORN, b0_override: float | None = None):
    """The report's page-1 table on `asof` for the crop year that is live: (ShipTable, estimate) — `estimate` is the
    (value, date, labels) the harvest basis was taken from while the weekly bids are not in yet, else None.

    b0_override  the user's OWN harvest basis (cents vs the spec's base contract) in place of the calculated one: the break-even,
                 the returns and the interest all use it, `tbl.b0_own` is True and `tbl.b0_calc` keeps what the calculated method
                 gives (the average so far, or the estimate before the weekly bids) to show beside it; no estimate is returned then."""
    crop_year = rtc.shipment_crop_year(asof)
    known = {d: px for d, px in futs.items() if d <= asof}      # a past as-of date must not see a roll spread measured after it
    cy = rtc.build_crop_year([o for o in obs if o["date"] <= asof], known, crop_year, rate_on, spec)
    b0, est = cy.b0, None
    if b0 is None:
        est = harvest_estimate(raw, crop_year, asof, futs, spec=spec)
        b0 = est[0] if est else None
    calc = b0                                                   # what the calculated method gives: the average so far, else the estimate
    own = b0_override is not None
    if own:
        b0, est = float(b0_override), None
    tbl = rtc.build_shipment_table(crop_year, asof, b0, shipment_quotes(raw, crop_year, spec), futs, rate_on, measure,
                                   b0_weeks=cy.b0_weeks if (cy.b0 is not None and not own) else 0, b0_est=est is not None, spec=spec,
                                   b0_own=own, b0_calc=calc)
    return tbl, est


def own_b0_map(value: float | None, crop_year: int, obs: list[dict], spec: rtc.Spec = rtc.CORN, every_year: bool = False) -> dict:
    """{crop year: harvest basis} to hand run_history_noted: the user's own harvest basis for the crop year being tracked — or, as a what-if
    ('what would storing have paid had I always bought at -15'), for every crop year the series covers (and the tracked one). {} when
    `value` is None, i.e. the calculated method everywhere."""
    if value is None:
        return {}
    years = (set(crop_years_in(obs, spec)) | {crop_year}) if every_year else {crop_year}
    return {y: float(value) for y in years}


def crop_years_in(obs: list[dict], spec: rtc.Spec = rtc.CORN) -> list[int]:
    """The crop years a series has at least one bid in (Oct 1 .. the spec's horizon: Jul 31 for corn, Sep 30 for soybeans)."""
    ys = set()
    for o in obs:
        d = o["date"]
        if d.month >= 10:
            ys.add(d.year)
        elif d.month <= spec.horizon[0]:
            ys.add(d.year - 1)
    return sorted(ys)


def derived_years(obs: list[dict]) -> dict:
    """{crop year: 'derived' | 'part'} for the crop years whose weekly series holds bids flagged `'derived': True` (history estimated from
    another series, river_derived): 'derived' when none of the year's bids are the location's own, 'part' when its own bids take over
    part-way. A crop year runs October to the next September. Empty when nothing is flagged."""
    tot: dict = {}
    der: dict = {}
    for o in obs:
        d = o["date"]
        cy = d.year if d.month >= 10 else d.year - 1
        tot[cy] = tot.get(cy, 0) + 1
        if o.get("derived"):
            der[cy] = der.get(cy, 0) + 1
    return {cy: ("derived" if n == tot[cy] else "part") for cy, n in der.items()}


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


def run_history_noted(obs: list[dict], futs: dict, rate_on, min_weeks: int = 12, min_year: int | None = None,
                      spec: rtc.Spec = rtc.CORN, b0_overrides: dict | None = None) -> tuple:
    """(results, skipped): every crop year the series covers from `min_year` (default: the spec's first) on, oldest first,
    without the years that only repeat the previous one (`skipped` = their labels). A year needs some weeks to say anything,
    so one with fewer than `min_weeks` of bids is left out unless it is the newest (the one in progress).
    `b0_overrides` = {crop year: the user's own harvest basis} (see own_b0_map()): those years are measured from it, the rest
    from their calculated average."""
    min_year = spec.min_crop_year if min_year is None else min_year
    ys = [y for y in crop_years_in(obs, spec) if y >= min_year]
    out = []
    for y in ys:
        cy = rtc.build_crop_year(obs, futs, y, rate_on, spec, b0_override=(b0_overrides or {}).get(y))
        if len(cy.weeks) >= min_weeks or (y == ys[-1] and cy.weeks):
            out.append(cy)
    bad = repeated_years(out)
    return [cy for cy in out if cy.crop_year not in bad], [rtc.crop_label(y) for y in sorted(bad)]


def run_history(obs: list[dict], futs: dict, rate_on, min_weeks: int = 12, min_year: int | None = None,
                spec: rtc.Spec = rtc.CORN, b0_overrides: dict | None = None) -> list:
    """run_history_noted without the note."""
    return run_history_noted(obs, futs, rate_on, min_weeks, min_year, spec, b0_overrides)[0]


def _value(w, measure: str):
    return w.net if measure == "net" else w.gross


def summary_rows(results: list, measure: str = "net") -> list[dict]:
    """One row per crop year for the table: harvest basis, the headline futures carry, best summer basis, the best return
    on the measure (and its week), and the return at the last week."""
    rows = []
    for cy in results:
        best = cy.best.get(measure)
        last = next((w for w in reversed(cy.weeks) if _value(w, measure) is not None), None)
        rows.append({
            "crop_year": cy.crop_year, "label": cy.label, "weeks": len(cy.weeks), "complete": cy.complete,
            "b0": cy.b0, "b0_weeks": cy.b0_weeks, "b0_own": cy.b0_own, "b0_calc": cy.b0_calc, "carry": cy.season_carry,
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
