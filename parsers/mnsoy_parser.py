"""
MNSP (Minnesota Soybean Processors) cash-bid parser.

Converts per-location dicts from mnsoy_scraper.fetch_mnsoy_bids() into
NewSnapshotRequest objects.  All MNSP bids captured here are Soybeans.
"""
from __future__ import annotations

from typing import Optional

from models import NewSnapshotRequest, SnapshotRow


def parse_mnsoy_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one MNSP location dict into a NewSnapshotRequest.

    Args:
        loc: dict from mnsoy_scraper.fetch_mnsoy_bids():
             {location, timestamp, bids: [{delivery, cme_symbol, basis_cents}]}
    """
    bids = loc.get("bids") or []
    if not bids:
        return None

    rows: list[SnapshotRow] = []
    seen: set[str] = set()

    for bid in bids:
        delivery    = (bid.get("delivery") or "").strip()
        cme_sym     = (bid.get("cme_symbol") or "").strip()
        basis_cents = bid.get("basis_cents")

        if not delivery or not cme_sym or basis_cents is None:
            continue

        del_key = "".join(c for c in delivery.upper() if c.isalnum())
        row_id  = f"SB_{cme_sym}_{del_key}"

        if row_id in seen:
            continue
        seen.add(row_id)

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = "Soybeans",
            deliveryMonth = delivery,
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = loc.get("timestamp", ""),
        provider  = "MNSP",
        location  = loc.get("location", ""),
        source    = "web",
        rows      = rows,
    )
