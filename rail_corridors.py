"""
rail_corridors.py — Registry + parsing helpers for manually-fed rail FOB corridors.

These are corn rail FOB bid/offer rundowns that can't be scraped; the user pastes
each corridor's table (~2x/week) and it's archived in the `rail_fob` table
(source='manual'), kept separate from the live Palmetto feed.

Cells look like Palmetto's: '{FUT}{bid}' or '{FUT}{bid}/{offer}'. When a cell omits
the futures code, it's inferred from the delivery period via the standard CME corn
convention. Periods may be single months or packages (e.g. JFM = Jan/Feb/Mar).
"""
from __future__ import annotations

import re
from typing import Optional

# Corridor registry (display order) → rail carrier. All corn.
CORRIDORS = [
    ("CSX Columbus",          "CSX"),
    ("CSX Evansville",        "CSX"),
    ("NS Ft Wayne",           "NS"),
    ("UP Allen Station (IL)", "UP"),
    ("UP Group 3 (NE)",       "UP"),
    ("UP Interior IA (IA)",   "UP"),
    ("BN Hereford",           "BNSF"),   # aka "BNCN Sellers"
    ("BN PNW",                "BNSF"),
    ("BN COBO",               "BNSF"),
]
RAIL_BY_CORRIDOR = {n: r for n, r in CORRIDORS}
CORRIDOR_ORDER   = {n: i for i, (n, _) in enumerate(CORRIDORS)}
_ALIASES = {"bncn sellers": "BN Hereford"}


def canonical_corridor(name: str) -> str:
    """Normalize a corridor name/alias to its registry name."""
    key = " ".join(str(name).strip().split())
    return _ALIASES.get(key.lower(), key)


# Standard CME corn contract by calendar month (matches the Palmetto convention).
_CORN_BY_MONTH = {1: "CH", 2: "CH", 3: "CH", 4: "CK", 5: "CK", 6: "CN",
                  7: "CN", 8: "CU", 9: "CU", 10: "CZ", 11: "CZ", 12: "CZ"}
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
# 3-letter month-initial packages (JFM = Jan/Feb/Mar) → first delivery month.
_PACKAGES = {"JFM": 1, "FMA": 2, "MAM": 3, "AMJ": 4, "MJJ": 5, "JJA": 6,
             "JAS": 7, "ASO": 8, "SON": 9, "OND": 10, "NDJ": 11, "DJF": 12}


def period_start_month(period: str) -> Optional[int]:
    """First delivery month (1-12) implied by a period label, else None."""
    p = period.strip().upper()
    if p in _PACKAGES:
        return _PACKAGES[p]
    found = [(p.find(tok), m) for tok, m in _MONTHS.items() if tok in p]
    if found:
        found.sort()
        return found[0][1]
    return None


def corn_futures_for_period(period: str) -> Optional[str]:
    """Standard CME corn contract (e.g. 'CN') for a delivery period, else None."""
    m = period_start_month(period)
    return _CORN_BY_MONTH.get(m) if m else None


def period_order(period: str, anchor_month: Optional[int] = None) -> int:
    """Sort key for a period within a posting. Months before `anchor_month` wrap to
    next year so e.g. Jun..Dec then JFM order correctly. Falls back to month number."""
    m = period_start_month(period)
    if m is None:
        return 99
    if anchor_month and m < anchor_month:
        return m + 12
    return m


_CELL_RE = re.compile(r"^\s*([A-Z][FGHJKMNQUVXZ])?\s*([+-]?\d+)\s*(?:/\s*([+-]?\d+))?\s*$")


def parse_cell(text: str, period: Optional[str] = None) -> Optional[dict]:
    """Parse a rail FOB cell into {futures, bid, offer}.

    Handles 'CN+18/+24', '+18/+24', '+20', 'CN+20', '-8/+2'. Futures is inferred
    from `period` when the cell omits it. Returns None for blanks / N/A.
    """
    t = str(text).strip().upper()
    if not t or t in ("N/A", "NA", "-", "—", "TBD"):
        return None
    m = _CELL_RE.match(t)
    if not m:
        return None
    fut = m.group(1) or (corn_futures_for_period(period) if period else None)
    return {"futures": fut, "bid": int(m.group(2)),
            "offer": int(m.group(3)) if m.group(3) else None}
