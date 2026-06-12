"""
LDC (Louis Dreyfus Company) cash-bid parser.

Converts per-location dicts from ldc_scraper.fetch_ldc_bids() into
NewSnapshotRequest objects.

Commodities handled:
  Yellow Corn / Corn       -> "Corn"        (basis in ¢/bu)
  Yellow Soybeans / Soybeans -> "Soybeans"  (basis in ¢/bu)
  Soft Red Wheat           -> "SRW Wheat"   (basis in ¢/bu)
  Canola / Nexera          -> SKIPPED

Symbol conversion (DTN -> CME):
  "@CN26"  ->  "ZCN26"   (strip @, prepend Z)
  "@SX26"  ->  "ZSX26"
  "@WN26"  ->  "ZWN26"
  "@RSX26" ->  SKIPPED (canola, no US CME reference)

Basis is provided as $/bu float (e.g. -0.10 = -10 cents).
Stored as round(basis * 100) = integer cents.
"""
import re
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

_COMM_TO_GRAIN: dict[str, Optional[str]] = {
    "YELLOW CORN":      "Corn",
    "CORN":             "Corn",
    "YELLOW SOYBEANS":  "Soybeans",
    "SOYBEANS":         "Soybeans",
    "SOFT RED WHEAT":   "SRW Wheat",
    "WHEAT":            "SRW Wheat",
    "CANOLA":           None,
    "NEXERA":           None,
}

_GRAIN_PFX: dict[str, str] = {
    "Corn":      "CN",
    "Soybeans":  "SB",
    "SRW Wheat": "SR",
}


def _norm_grain(commodity: str) -> Optional[str]:
    """Map commodity name to display grain, or None to skip."""
    key = commodity.strip().upper()
    if key in _COMM_TO_GRAIN:
        return _COMM_TO_GRAIN[key]
    if "CORN" in key:
        return "Corn"
    if "SOY" in key:
        return "Soybeans"
    if "WHEAT" in key:
        return "SRW Wheat"
    return None  # skip anything unknown (Canola, Nexera, etc.)


def _dtn_to_cme(symbol: str) -> Optional[str]:
    """
    Convert DTN symbol to CME format.

    "@CN26"  -> "ZCN26"
    "@SX26"  -> "ZSX26"
    "@WN26"  -> "ZWN26"
    "@RSX26" -> None (canola — no US CME equivalent)
    """
    if not symbol or not symbol.startswith("@"):
        return None
    root = symbol[1:]   # strip leading @
    if root.upper().startswith("RS"):
        return None     # canola
    return f"Z{root}"


def _basis_to_cents(basis) -> Optional[int]:
    """Convert float $/bu basis to integer cents. -0.10 -> -10."""
    try:
        return int(round(float(basis) * 100))
    except (TypeError, ValueError):
        return None


def _delivery_key(label: str) -> str:
    """Normalize delivery label for stable row IDs."""
    return re.sub(r"[^A-Z0-9]", "", label.upper())


def parse_ldc_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one LDC location dict into a NewSnapshotRequest, or None if no valid rows.

    Args:
        loc: dict from ldc_scraper.fetch_ldc_bids():
             {location_name, shortcut, facility_id, state, timestamp,
              cashbids: [{commodity, symbol, bid_type, label, basis}]}
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    seen_row_ids: set[str] = set()

    for bid in cashbids:
        # Only process basis bids
        if bid.get("bid_type") != "Basis":
            continue

        commodity = bid.get("commodity", "") or ""
        symbol    = bid.get("symbol",    "") or ""
        label     = bid.get("label",     "") or ""
        basis_val = bid.get("basis")

        if not symbol or not label or basis_val is None:
            continue

        grain = _norm_grain(commodity)
        if grain is None:
            continue

        cme_sym = _dtn_to_cme(symbol)
        if not cme_sym:
            continue

        basis_cents = _basis_to_cents(basis_val)
        if basis_cents is None:
            continue

        del_key = _delivery_key(label)
        pfx     = _GRAIN_PFX.get(grain, grain[:2].upper())
        row_id  = f"{pfx}_{cme_sym}_{del_key}"

        if row_id in seen_row_ids:
            continue
        seen_row_ids.add(row_id)

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = label,
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "LDC",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
