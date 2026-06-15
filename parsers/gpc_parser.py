"""
GPC / Kent Commodities cash-bid parser.

Converts per-location dicts from gpc_scraper.fetch_gpc_bids() into
NewSnapshotRequest objects.

DTN symbol conversion reuses the same logic as the GPRE parser:
  @C6N  →  ZCN26  (Corn July 2026)
  @S6X  →  ZSX26  (Soybeans Nov 2026)
  @W6N  →  ZWN26  (SRW Wheat July 2026)

Basis: stored as USD/bu float → ×100 → integer cents.
Delivery label (e.g. "Jun Wk 2", "FH Jun 26", "Jun 26") used directly.
"""
import re
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow
from parsers.gpre_parser import _convert_dtn_symbol

_GRAIN_PFX: dict[str, str] = {
    "Corn":     "CN",
    "Soybeans": "SB",
    "Wheat":    "WH",
}

_GRAIN_TO_CANONICAL: dict[str, str] = {
    "Corn":     "Corn",
    "Soybeans": "Soybeans",
    "Wheat":    "SRW Wheat",
}


def _basis_to_cents(basis_usd: float) -> int:
    return int(round(basis_usd * 100))


def _delivery_key(label: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", label.upper())


def parse_gpc_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one GPC location dict into a NewSnapshotRequest, or None if empty.

    Args:
        loc: dict from gpc_scraper.fetch_gpc_bids() with keys:
             location_name, state, timestamp,
             cashbids: [{grain, delivery_label, dtn_symbol, basis_usd}]
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    seen_ids: set[str] = set()

    for bid in cashbids:
        grain      = bid.get("grain", "")
        dtn_sym    = bid.get("dtn_symbol", "")
        del_label  = bid.get("delivery_label", "")
        basis_usd  = bid.get("basis_usd")

        if basis_usd is None or not grain or not dtn_sym:
            continue

        cme_sym     = _convert_dtn_symbol(dtn_sym)
        grain_name  = _GRAIN_TO_CANONICAL.get(grain, grain)
        basis_cents = _basis_to_cents(basis_usd)
        pfx         = _GRAIN_PFX.get(grain, grain[:2].upper())
        row_id      = f"{pfx}_{cme_sym}_{_delivery_key(del_label)}"

        if row_id in seen_ids:
            continue
        seen_ids.add(row_id)

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain_name,
            deliveryMonth = del_label,
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "GPC",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
