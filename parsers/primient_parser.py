"""
Primient cash-bid parser.

Converts per-location dicts from primient_scraper.fetch_primient_bids()
into NewSnapshotRequest objects.
"""
from __future__ import annotations

from typing import Optional

from models import NewSnapshotRequest, SnapshotRow


def parse_primient_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one Primient location dict into a NewSnapshotRequest.

    Args:
        loc: dict from primient_scraper.fetch_primient_bids():
             {location, timestamp, bids: [{bid_id, grain, delivery,
                                           cme_symbol, basis_cents, notes}]}
    """
    bids = loc.get("bids") or []
    if not bids:
        return None

    rows: list[SnapshotRow] = []
    seen: set[str] = set()

    for bid in bids:
        grain       = (bid.get("grain") or "").strip()
        delivery    = (bid.get("delivery") or "").strip()
        cme_sym     = (bid.get("cme_symbol") or "").strip()
        basis_cents = bid.get("basis_cents")
        bid_id      = (bid.get("bid_id") or "").strip()
        notes       = (bid.get("notes") or "").strip()

        if not grain or not delivery or not cme_sym or basis_cents is None:
            continue

        # Use the Agricharts bid ID as the row identifier — it's globally unique
        # and stable between runs, so re-scraping the same day is idempotent.
        row_id = f"PRI_{bid_id}" if bid_id else f"PRI_{cme_sym}_{delivery.replace(' ','')}"

        if row_id in seen:
            continue
        seen.add(row_id)

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = delivery,
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = loc.get("timestamp", ""),
        provider  = "Primient",
        location  = loc.get("location", ""),
        source    = "web",
        rows      = rows,
    )
