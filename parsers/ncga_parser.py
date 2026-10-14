"""
Northern Canola Growers Association (NCGA) cash-table parser: Bunge Altona and Cargill West Fargo canola bids -> NewSnapshotRequest.

Rows keep their NCGA_ id prefix so they can always be told from a buyer's own feed. The snapshot's source is 'ncga' and its subject line says which of
NCGA's tables it was; when the day's snapshot for the location already exists (Cargill West Fargo, written by the Cargill scraper) the rows are added
to it and its source stays as it was.
"""
from __future__ import annotations

from typing import Optional

from models import NewSnapshotRequest, SnapshotRow


def parse_ncga_location(loc: dict) -> Optional[NewSnapshotRequest]:
    bids = loc.get("bids") or []
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for bid in bids:
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
    table_date = loc.get("table_date")                  # NCGA's own 'Last Updated' date: kept so a table that was a day behind can be told
    return NewSnapshotRequest(
        timestamp=loc.get("timestamp", ""),
        provider=loc.get("provider", ""),
        location=loc.get("location", ""),
        source="ncga",
        emailSubject=f"NCGA cash table of {table_date}" if table_date else None,
        emailDate=table_date,
        rows=rows,
    )
