"""
Western Plains Energy cash-bid parser.

Converts the single-location dict from wpe_scraper.fetch_wpe_bids() into a
NewSnapshotRequest. The scraper already resolves the CME symbol and delivery
label from WPE's terse "basis + month-letter" homepage widget, so this parser
just maps fields onto SnapshotRow. Grain is stored raw ("Corn"/"Milo"); the
grain_map canonicalises "Milo" → "Sorghum" at display time.
"""
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow


def parse_wpe_location(loc: dict) -> Optional[NewSnapshotRequest]:
    location_name = (loc.get("location_name") or "").strip()
    provider      = (loc.get("provider") or "Western Plains Energy").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        grain  = (bid.get("grain") or "").strip().title()
        symbol = bid.get("symbol") or ""
        basis  = bid.get("basis")
        if basis is None or not symbol:
            continue
        rows.append(SnapshotRow(
            id            = bid.get("bid_id") or f"{grain[:2].upper()}_{symbol}",
            grain         = grain,
            deliveryMonth = bid.get("delivery_month") or "Unknown",
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
