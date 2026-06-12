"""
CHS Illinois Bushel API → NewSnapshotRequest parser.

Converts the JSON response from:
  POST https://api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList
into NewSnapshotRequest objects, one per (location, commodity) group.
"""
from typing import Optional
from models import NewSnapshotRequest, SnapshotRow

# Normalize Bushel commodity display names → canonical grain names
COMMODITY_MAP: dict[str, str] = {
    "corn":       "Corn",
    "soybeans":   "Soybeans",
    "soybean":    "Soybeans",
    "wheat, srw": "Wheat",
    "wheat srw":  "Wheat",
    "wheat hrw":  "Wheat",
    "wheat":      "Wheat",
}

GRAIN_PREFIX: dict[str, str] = {
    "Corn":     "CN",
    "Soybeans": "SB",
    "Wheat":    "WH",
}

# Skip pseudo-locations that aren't real elevators
SKIP_LOCATION_NAMES = {"futures-only"}


def _basis_to_cents(value_str: str) -> Optional[int]:
    """Convert USD/bu string (e.g., '-0.03', '0.24') to integer cents (-3, 24)."""
    try:
        return int(round(float(value_str) * 100))
    except (ValueError, TypeError):
        return None


def _normalize_grain(display_name: str) -> str:
    """Map Bushel displayName to canonical grain, falling back to title-case."""
    key = display_name.strip().lower()
    return COMMODITY_MAP.get(key, display_name.strip().title())


def parse_bids_response(
    bids_data: dict,
    location_ids: set[str],
    timestamp: str,
) -> list[NewSnapshotRequest]:
    """
    Parse the full GetBidsList response into a list of NewSnapshotRequest objects.

    Each (location × commodity-group) becomes one snapshot so grains stay
    separate, matching the convention used by ADM and POET data.

    Args:
        bids_data:    Parsed JSON from GetBidsList endpoint.
        location_ids: Set of UUID strings to include (Illinois filter).
                      Pass an empty set to include all locations.
        timestamp:    ISO-8601 UTC string, e.g. "2026-06-10T00:00:00Z".

    Returns:
        List of NewSnapshotRequest objects (may be empty).
    """
    snapshots: list[NewSnapshotRequest] = []

    for loc in bids_data.get("locations", []):
        loc_id   = loc.get("id", "")
        loc_name = loc.get("name", "").strip()

        # Apply Illinois filter
        if location_ids and loc_id not in location_ids:
            continue

        # Skip pseudo-locations
        if loc_name.lower() in SKIP_LOCATION_NAMES:
            continue

        for group in loc.get("groups", []):
            grain = _normalize_grain(group.get("displayName", ""))

            rows: list[SnapshotRow] = []
            for bid in group.get("bids", []):
                # bid.id is a stable short code like "CNCAR000OXOX" — use as row_id
                bid_id        = bid.get("id", "")
                delivery_month = bid.get("description", "")
                basis_str     = bid.get("basisPrice")
                futures_sym   = bid.get("futuresSymbol") or ""

                basis_cents = _basis_to_cents(basis_str) if basis_str is not None else None

                grain_pfx = GRAIN_PREFIX.get(grain, grain[:2].upper() if grain else "XX")
                row_id    = f"{grain_pfx}_{bid_id}" if bid_id else f"{grain_pfx}_{delivery_month}"

                rows.append(SnapshotRow(
                    id=row_id,
                    grain=grain,
                    deliveryMonth=delivery_month,
                    futuresSymbol=futures_sym,
                    basisCents=basis_cents,
                    isSpot=False,
                ))

            if not rows:
                continue

            snapshots.append(NewSnapshotRequest(
                timestamp=timestamp,
                provider="CHS",
                location=loc_name,
                source="web",
                rows=rows,
            ))

    return snapshots
