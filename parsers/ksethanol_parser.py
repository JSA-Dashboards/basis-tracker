"""
Kansas Ethanol cash-bid parser.

Converts the per-location dicts produced by ksethanol_scraper.fetch_ksethanol_bids()
into NewSnapshotRequest objects for the database.

Grain names are stored raw ("Corn", "Milo") — the app's grain_map canonicalises
"Milo" → "Sorghum" at display time, so no normalisation is needed here.  Milo is
priced off the corn board (symbol ZC…), exactly like the elevator/rail sorghum
feeds we already track.

Delivery month label comes from delivery_start ("07/13/2026" → "Jul 2026").
Basis is already an integer in cents from the agricharts feed.
"""
from datetime import datetime
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

_GRAIN_PFX: dict[str, str] = {
    "Corn": "CN",
    "Milo": "MI",
}


def _delivery_label(delivery_start: str) -> str:
    """'MM/DD/YYYY' → 'Mon YYYY' (e.g. '07/13/2026' → 'Jul 2026')."""
    try:
        return datetime.strptime(delivery_start, "%m/%d/%Y").strftime("%b %Y")
    except (ValueError, TypeError):
        return delivery_start or "Unknown"


def parse_ksethanol_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """Convert one location dict into a NewSnapshotRequest, or None if empty."""
    location_name = (loc.get("location_name") or "").strip()
    provider      = (loc.get("provider") or "Kansas Ethanol").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        grain     = (bid.get("grain") or "").strip().title()
        symbol    = bid.get("symbol") or ""
        basis     = bid.get("basis")
        del_start = bid.get("delivery_start") or ""
        bid_id    = bid.get("bid_id") or ""

        if basis is None or not symbol:
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
        provider  = provider,
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
