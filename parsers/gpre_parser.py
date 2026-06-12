"""
Green Plains Inc. (GPRE) corn-bid parser.

Converts per-location dicts from gpre_scraper.fetch_gpre_bids() into
NewSnapshotRequest objects.

DTN symbol format: @{root}{year_digit}{month_code}
  e.g.  @C6N  →  ZCN26  (Corn July 2026)
        @C7H  →  ZCH27  (Corn March 2027)

Delivery label comes from delivery_start ISO-8601 string.
Basis is a float (USD/bu); converted to integer cents.
"""
from datetime import datetime, timezone
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

# ── DTN symbol → CME symbol conversion ────────────────────────────────────────
# DTN format:  @ + root + year_digit + month_code
# CME format:  cme_root + month_code + 2-digit_year

_DTN_ROOT_TO_CME: dict[str, str] = {
    "C":  "ZC",   # Corn
    "S":  "ZS",   # Soybeans
    "W":  "ZW",   # SRW Wheat
    "KW": "KE",   # HRW Wheat
    "SM": "ZM",   # Soy Meal
    "BO": "ZL",   # Soy Oil
    "MW": "MWE",  # Minneapolis Spring Wheat
    "O":  "ZO",   # Oats
}

_TWO_CHAR_DTN_ROOTS: frozenset[str] = frozenset({"KW", "SM", "BO", "MW"})


def _convert_dtn_symbol(dtn_sym: str) -> str:
    """
    Convert a DTN futures symbol to CME format.

    DTN:  @C6N  →  CME: ZCN26
    DTN:  @C7H  →  CME: ZCH27
    """
    if not dtn_sym:
        return ""

    # Strip leading @ if present
    s = dtn_sym.lstrip("@")

    if len(s) < 3:
        return dtn_sym

    # Detect 2-char root
    root2 = s[:2]
    if root2 in _TWO_CHAR_DTN_ROOTS:
        root       = root2
        year_digit = s[2]
        month_code = s[3] if len(s) > 3 else ""
    else:
        root       = s[0]
        year_digit = s[1]
        month_code = s[2] if len(s) > 2 else ""

    if not month_code:
        return dtn_sym

    year_2d  = "2" + year_digit   # "6" → "26"
    cme_root = _DTN_ROOT_TO_CME.get(root.upper(), "Z" + root.upper())
    return f"{cme_root}{month_code}{year_2d}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _delivery_label(iso_start: str) -> str:
    """Convert ISO-8601 start → 'Mon YYYY', e.g. '2026-06-01T05:00:00Z' → 'Jun 2026'."""
    try:
        dt = datetime.fromisoformat(iso_start.replace("Z", "+00:00"))
        return dt.strftime("%b %Y")
    except (ValueError, TypeError):
        return iso_start[:7] if iso_start else "Unknown"


def _basis_to_cents(basis_float: float) -> int:
    """Convert USD/bu float (-0.19) to integer cents (-19)."""
    return int(round(basis_float * 100))


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_gpre_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one GPRE location dict into a NewSnapshotRequest, or None if empty.

    Args:
        loc: dict with keys location_name, timestamp, cashbids.

    Returns:
        NewSnapshotRequest ready for database.upsert_snapshot(), or None.
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        dtn_sym   = bid.get("dtn_symbol", "") or ""
        basis     = bid.get("basis")
        del_start = bid.get("delivery_start", "") or ""

        if basis is None or not dtn_sym:
            continue

        cme_sym     = _convert_dtn_symbol(dtn_sym)
        basis_cents = _basis_to_cents(basis)
        del_label   = _delivery_label(del_start)

        # Row ID: stable across daily scrapes for same delivery/contract
        row_id = f"CN_{cme_sym}_{del_start[:10]}"

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = "Corn",
            deliveryMonth = del_label,
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "GPRE",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
