"""
Cargill cash-bid parser.

Converts the per-location dicts produced by cargill_scraper.fetch_cargill_bids()
into NewSnapshotRequest objects that can be upserted into the database.

Symbol conversion (Barchart → CME format):
  CN6   → ZCN26    (Corn, 1-char root C → ZC, append "2" before year digit)
  SN6   → ZSN26    (Soybeans)
  WN6   → ZWN26    (Soft Red Winter Wheat)
  KWN6  → KEN26    (Hard Red Winter Wheat, KW → KE)
  SMQ6  → ZMQ26    (Soy Meal, SM → ZM)
  MWN6  → MWN26    (Minneapolis Spring Wheat, MW unchanged)
  RSN6  → ZRN26    (Canola, RS → ZR)

Basis is a string like "-0.0200" (USD/bu); converted to integer cents (-2).
"""
from datetime import datetime
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

# ── Symbol conversion ──────────────────────────────────────────────────────────
# Barchart roots that are 2 chars (all others are 1 char)
_TWO_CHAR_ROOTS: frozenset[str] = frozenset({"KW", "SM", "MW", "RS", "BO", "SB"})

# Barchart root  →  CME root
_ROOT_TO_CME: dict[str, str] = {
    "C":  "ZC",   # Corn (CBOT)
    "S":  "ZS",   # Soybeans (CBOT)
    "W":  "ZW",   # Soft Red Winter Wheat (CBOT)
    "KW": "KE",   # Hard Red Winter Wheat (KC)
    "SM": "ZM",   # Soybean Meal (CBOT)
    "BO": "ZL",   # Soybean Oil (CBOT)
    "MW": "MWE",  # Minneapolis Spring Wheat (MGEX)
    "RS": "ZR",   # Canola
    "O":  "ZO",   # Oats (CBOT)
}


def _convert_symbol(barchart_sym: str) -> str:
    """
    Convert a Barchart cashbid futuresymbol to CME format.

    Barchart format: <root><month_code><1-digit-year>
        e.g.  CN6  →  ZCN26
              KWN6 →  KEN26

    CME format: <cme_root><month_code><2-digit-year>
    """
    if not barchart_sym or len(barchart_sym) < 3:
        return barchart_sym

    # Identify root (2-char or 1-char)
    root2 = barchart_sym[:2]
    if root2.upper() in _TWO_CHAR_ROOTS:
        root = root2
        rest = barchart_sym[2:]
    else:
        root = barchart_sym[0]
        rest = barchart_sym[1:]

    if len(rest) < 2:
        return barchart_sym  # malformed — return as-is

    month_code = rest[0]
    year_digit = rest[1]   # single digit e.g. "6"
    year_2d    = "2" + year_digit  # e.g. "26"

    cme_root = _ROOT_TO_CME.get(root.upper(), "Z" + root.upper())
    return f"{cme_root}{month_code}{year_2d}"


# ── Grain normalisation ────────────────────────────────────────────────────────
_GRAIN_NORM: dict[str, str] = {
    "yellow corn":           "Corn",
    "corn":                  "Corn",
    "yellow soybeans":       "Soybeans",
    "soybeans":              "Soybeans",
    "soybean":               "Soybeans",
    "hard red winter wheat": "Wheat",
    "soft red winter wheat": "Wheat",
    "hard red spring wheat": "Wheat",
    "hard red spring":       "Wheat",
    "spring wheat":          "Wheat",
    "winter wheat":          "Wheat",
    "wheat":                 "Wheat",
    "soybean meal":          "Soy Meal",
    "canola":                "Canola",
    "oats":                  "Oats",
    "sorghum":               "Milo",
    "grain sorghum":         "Milo",
    "milo":                  "Milo",
}

_GRAIN_PFX: dict[str, str] = {
    "Corn":     "CN",
    "Soybeans": "SB",
    "Wheat":    "WH",
    "Soy Meal": "SM",
    "Canola":   "CA",
    "Oats":     "OT",
    "Milo":     "MI",
}


def _norm_grain(raw: str) -> str:
    return _GRAIN_NORM.get(raw.strip().lower(), raw.strip().title())


def _delivery_label(delivery_start: str) -> str:
    """Convert 'MM/DD/YYYY' → 'Mon YYYY', e.g. '06/01/2026' → 'Jun 2026'."""
    try:
        dt = datetime.strptime(delivery_start, "%m/%d/%Y")
        return dt.strftime("%b %Y")
    except (ValueError, TypeError):
        return delivery_start or "Unknown"


def _basis_to_cents(basis_str: str) -> Optional[int]:
    """Convert basis string (e.g. '-0.0200') to integer cents (-2)."""
    try:
        return int(round(float(basis_str) * 100))
    except (ValueError, TypeError):
        return None


def parse_cargill_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one Cargill location dict (from cargill_scraper.fetch_cargill_bids)
    into a NewSnapshotRequest, or None if there are no valid bid rows.

    Args:
        loc: dict with keys location_name, state, city, timestamp, cashbids.

    Returns:
        NewSnapshotRequest ready for database.upsert_snapshot(), or None.
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        grain_raw    = bid.get("grain", "") or ""
        grain        = _norm_grain(grain_raw)
        barchart_sym = bid.get("barchart_symbol", "") or ""
        basis_str    = bid.get("basis_str")
        del_start    = bid.get("delivery_start", "") or ""

        if basis_str is None:
            continue

        basis_cents = _basis_to_cents(basis_str)
        if basis_cents is None:
            continue

        cme_sym = _convert_symbol(barchart_sym)
        if not cme_sym:
            continue

        pfx    = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id = f"{pfx}_{cme_sym}_{del_start.replace('/', '')}"

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = _delivery_label(del_start),
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "Cargill",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
