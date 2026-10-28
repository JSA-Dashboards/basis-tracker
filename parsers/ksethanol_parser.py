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

    # (year, month) of this scrape, so we can drop stale bids whose delivery window
    # is already in the past (e.g. a lingering 'Sep 2025' milo bid on a 2026 feed —
    # it otherwise creates a phantom nearest-month slot and a bogus roll downstream).
    snap_ym = None
    snap_day = None
    try:
        _st = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
        snap_ym = (_st.year, _st.month)
        snap_day = _st.date()
    except (ValueError, TypeError):
        snap_ym = None

    # `open_windows` (a tenant opts in; agricharts_scraper sets it on the loc): a bid whose window STARTED in an earlier month but has not ended is this month's bid, not a stale one.
    # Without it the start month alone decides, which drops a harvest window ('Sep 1 - Nov 30') for the whole of October and November, the co-op's main bid.
    open_windows = bool(loc.get("open_windows"))
    # Under open_windows two overlapping windows can land on the same month (Garden City posts an 'Old Crop' window since 09/01/2025 AND a 'New Crop 2026' one since 09/01/2026, same basis):
    # one row per (grain, month, contract) is kept, the one whose window started LAST (then the later bid id).
    kept: dict[tuple, tuple] = {}

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        grain     = (bid.get("grain") or "").strip().title()
        symbol    = bid.get("symbol") or ""
        basis     = bid.get("basis")
        del_start = bid.get("delivery_start") or ""
        bid_id    = bid.get("bid_id") or ""
        started   = None

        if basis is None or not symbol:
            continue

        # Skip a bid whose delivery month is entirely before this scrape's month.
        if snap_ym:
            try:
                _dd = datetime.strptime(del_start, "%m/%d/%Y")
                started = _dd
                if (_dd.year, _dd.month) < snap_ym:
                    del_end = bid.get("delivery_end") or ""
                    if not (open_windows and del_end):
                        continue
                    _de = datetime.strptime(del_end, "%m/%d/%Y")
                    if _de.date() < snap_day:
                        continue                                    # the window ended before today: stale (to the day: a window that ends 12/01 is filed on 12/01 and gone on 12/02)
                    del_start = f"{snap_ym[1]:02d}/01/{snap_ym[0]}"  # still open: filed under this month
            except (ValueError, TypeError):
                pass

        pfx    = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id = f"{pfx}_{bid_id}" if bid_id else f"{pfx}_{del_start}"

        row = SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = _delivery_label(del_start),
            futuresSymbol = symbol,
            basisCents    = int(basis),
            isSpot        = False,
        )
        if open_windows:
            key, rank = (grain, row.deliveryMonth, symbol), (started or datetime.min, bid_id)
            if key in kept:
                if rank <= kept[key][0]:
                    continue                                # an overlapping window that started earlier: the later one is already in
                rows[kept[key][1]] = row
                kept[key] = (rank, kept[key][1])
                continue
            kept[key] = (rank, len(rows))
        rows.append(row)

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = provider,
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
