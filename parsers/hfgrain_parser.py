"""
Hollander & Feuerhaken (hfgrain.com) daily bid sheet parser: one filed location -> NewSnapshotRequest.

Rows keep their HF_ id prefix so they can always be told from a buyer's own feed. The snapshot's source is 'hfgrain' and its subject line says which sheet it was (the sheet's own
date, and when the PDF was uploaded, so a late or re-issued sheet can be told); the timestamp is the sheet's date, not the day we looked.
"""
from __future__ import annotations

from typing import Optional

from models import NewSnapshotRequest, SnapshotRow


def parse_hfgrain_location(loc: dict) -> Optional[NewSnapshotRequest]:
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for bid in loc.get("bids") or []:
        grain = (bid.get("grain") or "").strip()
        delivery = (bid.get("delivery") or "").strip()
        sym = (bid.get("cme_symbol") or "").strip()
        basis_cents = bid.get("basis_cents")
        row_id = (bid.get("bid_id") or "").strip()
        if not grain or not delivery or not sym or basis_cents is None or not row_id or row_id in seen:
            continue
        seen.add(row_id)
        rows.append(SnapshotRow(id=row_id, grain=grain, deliveryMonth=delivery, futuresSymbol=sym, basisCents=int(basis_cents), isSpot=False))
    if not rows:
        return None
    sheet_date = loc.get("sheet_date")
    up = loc.get("uploaded")
    subject = None
    if sheet_date:
        subject = f"H&F bids PDF of {sheet_date}" + (" (revised)" if loc.get("revised") else "") + (f", uploaded {up.strftime('%Y-%m-%d %H:%M')} UTC" if up else "")
    return NewSnapshotRequest(
        timestamp=loc.get("timestamp", ""),
        provider=loc.get("provider", ""),
        location=loc.get("location", ""),
        source="hfgrain",
        emailSubject=subject,
        emailDate=sheet_date,
        rows=rows,
    )
