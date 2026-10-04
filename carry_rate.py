"""carry_rate.py — the annual interest rate behind the Cost of Carry sheet's interest cost.

The Net Carry tab used to charge a flat, hand-set 9%. The Cost of Carry calculator
(cost-of-carry-calculator/app.py) prices interest as

    interest = price × annual_rate × days / 360        annual_rate = fed funds + 2.25%

so this module supplies that rate for any as-of date: the **effective fed funds rate on
the day plus the same fixed spread**. `FED_FUNDS_SPREAD_PCT` and `FALLBACK_ANNUAL_RATE_PCT`
mirror that app's constants of the same names — it is the source of truth, so change them
there first.

Source of the history: Board of Governors of the Federal Reserve System (US), Federal
Funds Effective Rate [DFF], via FRED (https://fred.stlouisfed.org/series/DFF). Public
domain, citation requested. DFF is published for every calendar day (weekends carry the
prior business day). The live FRED download is preferred; if it is unreachable the
committed snapshot in data/fed_funds_dff.csv is used instead, and if that is missing too
the 5.89% fallback applies. Refresh the snapshot with `python carry_rate.py`.

One small convention difference from the Cost of Carry app: there, *today's* rate is the
front-month ZQ fed-funds FUTURES price (100 − price) from the Massive API; its history
(and this module, always) uses the EFFECTIVE rate. They normally agree to within a few
hundredths of a point, and the rate box on the Net Carry tab stays editable.
"""
from __future__ import annotations

import csv
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from io import StringIO
from pathlib import Path

import requests

# Mirrors cost-of-carry-calculator/app.py (FED_FUNDS_SPREAD_PCT / FALLBACK_ANNUAL_RATE_PCT).
FED_FUNDS_SPREAD_PCT = 2.25
FALLBACK_ANNUAL_RATE_PCT = 5.89

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF&cosd=2006-01-01"
SNAPSHOT_PATH = Path(__file__).parent / "data" / "fed_funds_dff.csv"


class FedFunds:
    """Daily effective fed funds (percent), ascending by date."""

    def __init__(self, dates: list[date], values: list[float], source: str):
        self.dates, self.values, self.source = dates, values, source

    def __len__(self) -> int:
        return len(self.dates)

    def on(self, d: date) -> tuple[date, float] | None:
        """(observation date, rate %) in force on `d`: the latest observation ON OR
        BEFORE it (a date past the end carries the last value forward). None if `d`
        is before the first observation or there is no data."""
        i = bisect_right(self.dates, d) - 1
        return (self.dates[i], self.values[i]) if i >= 0 else None


def parse_dff(csv_text: str) -> tuple[list[date], list[float]]:
    """FRED's two-column CSV (observation_date, DFF) → ascending dates and values.
    FRED marks a missing observation with '.', which is dropped, never read as zero."""
    pairs = {}
    for row in csv.reader(StringIO(csv_text)):
        if len(row) < 2:
            continue
        try:
            d = date.fromisoformat(row[0].strip())
            v = float(row[1])
        except ValueError:            # header row, '.', blank
            continue
        pairs[d] = v
    ds = sorted(pairs)
    return ds, [pairs[d] for d in ds]


def load_fed_funds(timeout: float = 10.0) -> FedFunds:
    """FRED first, then the committed snapshot, then an empty series (→ fallback rate)."""
    try:
        resp = requests.get(FRED_URL, timeout=timeout)
        resp.raise_for_status()
        ds, vs = parse_dff(resp.text)
        if ds:
            return FedFunds(ds, vs, "fred")
    except Exception:
        pass                          # fall through to the snapshot
    try:
        ds, vs = parse_dff(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if ds:
            return FedFunds(ds, vs, "snapshot")
    except Exception:
        pass
    return FedFunds([], [], "none")


@dataclass
class CarryRate:
    rate_pct: float                   # the annual rate to charge, percent
    fed_funds_pct: float | None       # the fed funds component (None when on the fallback)
    obs_date: date | None             # the observation date that value is from
    source: str                       # 'fred' | 'snapshot' | 'fallback'


def rate_for(as_of: date, fed_funds: FedFunds | None,
             spread_pct: float = FED_FUNDS_SPREAD_PCT,
             fallback_pct: float = FALLBACK_ANNUAL_RATE_PCT) -> CarryRate:
    """The Cost of Carry convention for `as_of`: effective fed funds that day + spread."""
    hit = fed_funds.on(as_of) if fed_funds is not None and len(fed_funds) else None
    if hit is None:
        return CarryRate(fallback_pct, None, None, "fallback")
    obs, ff = hit
    return CarryRate(round(ff + spread_pct, 4), ff, obs, fed_funds.source)


def refresh_snapshot() -> int:
    """Rewrite data/fed_funds_dff.csv from FRED. Returns the number of rows written."""
    resp = requests.get(FRED_URL, timeout=30)
    resp.raise_for_status()
    ds, vs = parse_dff(resp.text)
    if not ds:
        raise RuntimeError("FRED returned no usable rows")
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SNAPSHOT_PATH.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["observation_date", "DFF"])
        for d, v in zip(ds, vs):
            w.writerow([d.isoformat(), v])
    return len(ds)


if __name__ == "__main__":
    print(f"wrote {refresh_snapshot():,} rows to {SNAPSHOT_PATH}")
