"""
Star of the West cash-bid parser.

Converts the per-location dicts from sotw_scraper.fetch_sotw_bids() into
NewSnapshotRequest objects for database.upsert_snapshot().

Grain normalisation (substring match on the AgriCharts product name):
  "Corn"        → Corn
  "Soybeans"    → Soybeans
  "Red Wheat"   → Red Wheat     (kept distinct — SRW, very different basis)
  "White Wheat" → White Wheat   (soft white)

Delivery label comes from delivery_start ("07/01/2026" → "Jul 2026"); the CME
symbol comes straight from the feed ("ZCN26"); basis is already int cents.
"""
from datetime import datetime
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

_GRAIN_PFX: dict[str, str] = {"Corn": "CN", "Soybeans": "SB",
                              "Red Wheat": "RW", "White Wheat": "WW", "Wheat": "WH"}


def _norm_grain(raw: str) -> str:
    r = (raw or "").strip().lower()
    if "corn" in r:
        return "Corn"
    if "soybean" in r or "bean" in r:
        return "Soybeans"
    if "red wheat" in r:
        return "Red Wheat"
    if "white wheat" in r:
        return "White Wheat"
    if "wheat" in r:
        return "Wheat"
    return (raw or "").strip().title() or "Unknown"


def _delivery_label(delivery_start: str) -> str:
    """'MM/DD/YYYY' → 'Mon YYYY', e.g. '07/01/2026' → 'Jul 2026'."""
    try:
        return datetime.strptime(delivery_start, "%m/%d/%Y").strftime("%b %Y")
    except (ValueError, TypeError):
        return delivery_start or "Unknown"


def parse_sotw_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """Convert one Star of the West location dict into a NewSnapshotRequest, or None."""
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
        provider  = "Star of West",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
