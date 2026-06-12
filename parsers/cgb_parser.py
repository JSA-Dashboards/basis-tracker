"""
CGB Grain cash-bid parser.

Converts the per-location dicts produced by cgb_scraper.fetch_cgb_bids()
into NewSnapshotRequest objects that can be upserted into the database.

Grain name normalisation:
  CGB "Beans"  → "Soybeans"   (ID 6322 is CGB's soybeans)
  CGB "HRW"    → "Wheat"
  CGB "Wheat"  → "Wheat"
  CGB "Corn"   → "Corn"

Delivery month label comes from delivery_start ("08/01/2026" → "Aug 2026").
CME futures symbol comes directly from the "symbol" field ("ZCU26").
Basis is already an integer in cents from the agricharts.com API.
"""
from datetime import datetime
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

# ── Grain normalisation ────────────────────────────────────────────────────────
_GRAIN_NORM: dict[str, str] = {
    "corn":     "Corn",
    "beans":    "Soybeans",   # CGB commodity 6322 — their label for soybeans
    "soybeans": "Soybeans",
    "soybean":  "Soybeans",
    "wheat":    "Wheat",
    "hrw":      "Wheat",      # Hard Red Winter — normalise to Wheat
    "srw":      "Wheat",
    "milo":     "Milo",
    "soybean meal": "Soy Meal",
}

_GRAIN_PFX: dict[str, str] = {
    "Corn":     "CN",
    "Soybeans": "SB",
    "Wheat":    "WH",
    "Milo":     "MI",
    "Soy Meal": "SM",
}


def _norm_grain(raw: str) -> str:
    return _GRAIN_NORM.get(raw.strip().lower(), raw.strip().title())


def _delivery_label(delivery_start: str) -> str:
    """Convert 'MM/DD/YYYY' → 'Mon YYYY', e.g. '08/01/2026' → 'Aug 2026'."""
    try:
        dt = datetime.strptime(delivery_start, "%m/%d/%Y")
        return dt.strftime("%b %Y")
    except (ValueError, TypeError):
        return delivery_start or "Unknown"


def parse_cgb_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one location dict (from cgb_scraper.fetch_cgb_bids) into a
    NewSnapshotRequest, or None if there are no valid bid rows.

    Args:
        loc: dict with keys location_name, state, facility_type, timestamp,
             cashbids (list of bid dicts).

    Returns:
        NewSnapshotRequest ready for database.upsert_snapshot(), or None.
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        grain_raw = bid.get("grain", "") or ""
        grain     = _norm_grain(grain_raw)
        symbol    = bid.get("symbol", "") or ""
        basis     = bid.get("basis")        # already int cents from scraper
        del_start = bid.get("delivery_start", "") or ""
        bid_id    = bid.get("bid_id", "") or ""

        if basis is None:
            continue
        if not symbol:
            continue

        pfx    = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id = f"{pfx}_{bid_id}" if bid_id else f"{pfx}_{del_start}"

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = _delivery_label(del_start),
            futuresSymbol = symbol,
            basisCents    = int(basis),
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "CGB",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
