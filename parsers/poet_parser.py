"""
POET Gradable instruments API → NewSnapshotRequest parser.

Converts the JSON response from:
  GET /api/commodities/v2/merchandising/instruments/market/{id}?offer_type=public
into a NewSnapshotRequest suitable for upsert_snapshot().
"""
from typing import Optional
from models import NewSnapshotRequest, SnapshotRow

# Map POET commodity codes → canonical grain names used in the app
COMMODITY_MAP: dict[str, str] = {
    "CN":       "Corn",
    "CORN":     "Corn",
    "SB":       "Soybeans",
    "SOY":      "Soybeans",
    "SOYBEAN":  "Soybeans",
    "SOYBEANS": "Soybeans",
    "SW":       "Wheat",
    "WH":       "Wheat",
    "HRW":      "Wheat",
    "WHEAT":    "Wheat",
}

GRAIN_PREFIX: dict[str, str] = {
    "Corn":     "CN",
    "Soybeans": "SB",
    "Wheat":    "WH",
}


def _basis_to_cents(value: float) -> Optional[int]:
    """Convert USD/bu float (e.g., 0.25 or -0.10) to integer cents (25, -10)."""
    try:
        return int(round(float(value) * 100))
    except (ValueError, TypeError):
        return None


def parse_instruments(
    market_id: int,
    display_name: str,
    instruments_data: dict,
    timestamp: str,
) -> Optional[NewSnapshotRequest]:
    """
    Parse one location's instruments API response into a NewSnapshotRequest.

    Args:
        market_id:        POET numeric market ID (used to build stable row IDs).
        display_name:     Human-readable location name, e.g. "Alexandria, IN".
        instruments_data: Parsed JSON dict from the instruments endpoint.
        timestamp:        ISO-8601 UTC string, e.g. "2026-06-10T00:00:00Z".

    Returns:
        NewSnapshotRequest with all active instruments, or None if none found.
    """
    instruments = instruments_data.get("instruments", [])
    crops_list  = instruments_data.get("crops", [])

    # Build crop_id → grain mapping from the crops array
    crop_grain: dict[int, str] = {}
    for crop in crops_list:
        code  = (crop.get("commodity") or "").upper()
        grain = COMMODITY_MAP.get(code)
        if grain and crop.get("crop_id") is not None:
            crop_grain[crop["crop_id"]] = grain

    rows: list[SnapshotRow] = []
    for instr in instruments:
        if instr.get("deleted"):
            continue

        # ── Resolve grain ────────────────────────────────────────────────────
        commodity_code = (instr.get("ext_commodity_id") or "").upper()
        grain = COMMODITY_MAP.get(commodity_code)
        if not grain:
            grain = crop_grain.get(instr.get("commodity_id"))
        if not grain:
            grain = commodity_code or "Unknown"

        # ── Basis ────────────────────────────────────────────────────────────
        basis_bid   = instr.get("basis_bid")
        basis_cents = _basis_to_cents(basis_bid) if basis_bid is not None else None

        # ── Delivery month label ─────────────────────────────────────────────
        delivery_month = instr.get("display_name", "")  # e.g. "June 26"

        # ── Futures symbol ───────────────────────────────────────────────────
        futures_sym = ""
        futures = instr.get("futures") or {}
        if futures:
            symbols = futures.get("symbols") or {}
            # Prefer the long barchart/fbn symbol (e.g. "ZCN26") over display ("ZCN6")
            futures_sym = (
                symbols.get("fbn")
                or symbols.get("barchart")
                or symbols.get("cme_globex")
                or instr.get("option_month")
                or ""
            )

        # ── Stable row ID ────────────────────────────────────────────────────
        # POET's numeric instrument ID is stable across days for the same
        # delivery slot, so it can be used for change-tracking across snapshots.
        instr_id    = instr.get("id", "")
        grain_pfx   = GRAIN_PREFIX.get(grain, grain[:2].upper() if grain else "XX")
        row_id      = f"{grain_pfx}_{instr_id}"

        rows.append(SnapshotRow(
            id=row_id,
            grain=grain,
            deliveryMonth=delivery_month,
            futuresSymbol=futures_sym,
            basisCents=basis_cents,
            isSpot=False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp=timestamp,
        provider="POET",
        location=display_name,
        source="web",
        rows=rows,
    )
