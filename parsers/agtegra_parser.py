"""
Agtegra cash-bid parser.

Converts agtegra_scraper.fetch_agtegra_bids() per-location dicts into
NewSnapshotRequest objects for database.upsert_snapshot().

Grain normalisation:
  "Corn"         → Corn
  "Soybeans"     → Soybeans
  "Spring Wheat" → Spring Wheat   (Minneapolis MW / HRS — kept distinct)
  "Winter Wheat" → Winter Wheat   (KC KE / HRW — kept distinct)
  "Milo"         → Milo           (priced vs Chicago corn)
Sunflowers / Oats never reach here (no futures symbol → dropped by the scraper).
"""
from datetime import datetime
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

_GRAIN_PFX: dict[str, str] = {"Corn": "CN", "Soybeans": "SB",
                              "Spring Wheat": "SW", "Winter Wheat": "WW", "Milo": "MI"}


def _norm_grain(raw: str) -> str:
    r = (raw or "").strip().lower()
    if "corn" in r:
        return "Corn"
    if "soybean" in r or "bean" in r:
        return "Soybeans"
    if "spring wheat" in r:
        return "Spring Wheat"
    if "winter wheat" in r:
        return "Winter Wheat"
    if "wheat" in r:
        return "Wheat"
    if "milo" in r or "sorghum" in r:
        return "Milo"
    return (raw or "").strip().title() or "Unknown"


def _delivery_label(delivery_start: str) -> str:
    try:
        return datetime.strptime(delivery_start, "%m/%d/%Y").strftime("%b %Y")
    except (ValueError, TypeError):
        return delivery_start or "Unknown"


def parse_agtegra_location(loc: dict) -> Optional[NewSnapshotRequest]:
    location_name = (loc.get("location_name") or "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        grain  = _norm_grain(bid.get("grain", ""))
        symbol = bid.get("symbol", "") or ""
        basis  = bid.get("basis")
        if basis is None or not symbol:
            continue
        del_start = bid.get("delivery_start", "") or ""
        bid_id    = bid.get("bid_id", "") or ""
        pfx       = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id    = f"{pfx}_{bid_id}" if bid_id else f"{pfx}_{del_start}"

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
        provider  = "Agtegra",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
