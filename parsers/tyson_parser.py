"""
Tyson Foods (LGS) cash-bid parser.

Converts per-location dicts from tyson_scraper.fetch_tyson_bids() into
NewSnapshotRequest objects.

Futures symbol format:  FuturesMonth="Jul-26" + Product="Corn" → "ZCN26"
  Month codes: Jan=F Feb=G Mar=H Apr=J May=K Jun=M Jul=N Aug=Q Sep=U Oct=V Nov=X Dec=Z
  Product roots: Corn=ZC, Beans=ZS, Wheat=ZW

Basis: Price field is USD/bu float → multiply by 100 → integer cents.
DeliveryPeriod ("Jun 26", "Jul 26 FH", etc.) used directly as deliveryMonth label.
"""
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

_MONTH_TO_CODE: dict[str, str] = {
    "Jan": "F", "Feb": "G", "Mar": "H", "Apr": "J",
    "May": "K", "Jun": "M", "Jul": "N", "Aug": "Q",
    "Sep": "U", "Oct": "V", "Nov": "X", "Dec": "Z",
}

_PRODUCT_TO_CME_ROOT: dict[str, str] = {
    "Corn":  "ZC",
    "Beans": "ZS",
    "Wheat": "ZW",
}

_PRODUCT_TO_GRAIN: dict[str, str] = {
    "Corn":  "Corn",
    "Beans": "Soybeans",
    "Wheat": "Wheat",
}

_GRAIN_PFX: dict[str, str] = {
    "Corn":     "CN",
    "Soybeans": "SB",
    "Wheat":    "WH",
}


def _futures_symbol(product: str, futures_month: str) -> Optional[str]:
    """Convert 'Jul-26' + 'Corn' to 'ZCN26'."""
    parts = futures_month.split("-")
    if len(parts) != 2:
        return None
    mon_abbr, yr2 = parts[0].strip(), parts[1].strip()
    month_code = _MONTH_TO_CODE.get(mon_abbr)
    cme_root   = _PRODUCT_TO_CME_ROOT.get(product)
    if not month_code or not cme_root:
        return None
    return f"{cme_root}{month_code}{yr2}"


def _basis_to_cents(price: float) -> int:
    """Convert USD/bu float to integer cents. 0.65 → 65, -0.10 → -10."""
    return int(round(price * 100))


def parse_tyson_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one Tyson LGS location dict into a NewSnapshotRequest, or None if empty.

    Args:
        loc: dict from tyson_scraper.fetch_tyson_bids() with keys:
             location_name, location_type, city, state, lat, lon, timestamp, cashbids

    Returns:
        NewSnapshotRequest ready for database.upsert_snapshot(), or None.
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    seen_ids: set[str] = set()

    for bid in cashbids:
        product       = bid.get("product", "").strip()
        futures_month = bid.get("futures_month", "").strip()
        del_period    = bid.get("delivery_period", "").strip()
        price         = bid.get("price")

        if price is None or not product or not futures_month:
            continue

        grain = _PRODUCT_TO_GRAIN.get(product)
        if grain is None:
            continue

        cme_sym = _futures_symbol(product, futures_month)
        if not cme_sym:
            continue

        basis_cents = _basis_to_cents(price)
        del_label   = del_period or futures_month

        pfx    = _GRAIN_PFX.get(grain, grain[:2].upper())
        row_id = f"{pfx}_{cme_sym}_{del_label.replace(' ', '_')}"

        if row_id in seen_ids:
            continue
        seen_ids.add(row_id)

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = del_label,
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "Tyson",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
