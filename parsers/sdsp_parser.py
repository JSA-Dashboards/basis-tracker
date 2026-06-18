"""
South Dakota Soybean Processors (SDSP) cash-bid parser.
"""
from __future__ import annotations

from typing import Optional

from models import NewSnapshotRequest, SnapshotRow


def parse_sdsp_location(loc: dict) -> Optional[NewSnapshotRequest]:
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

        if not grain or not delivery or not cme_sym or basis_cents is None:
            continue

        row_id = f"SDSP_{bid_id}" if bid_id else f"SDSP_{grain[:3].upper()}_{cme_sym}_{delivery.replace(' ', '')}"

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
        provider  = "SDSP",
        location  = loc.get("location", ""),
        source    = "web",
        rows      = rows,
    )
