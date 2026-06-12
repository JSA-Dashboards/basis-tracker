"""
AGP (Ag Processing Inc) cash-bid parser.

Converts per-location dicts from agp_scraper.fetch_agp_bids() into
NewSnapshotRequest objects.

Commodities handled:
  Soybeans / YSB / SB    -> "Soybeans"      (basis in ¢/bu, stored as cents)
  Corn / YC / CN         -> "Corn"          (basis in ¢/bu, stored as cents)
  Soybean Meal / SM      -> "Soybean Meal"  (basis in $/ton, stored *100)
  Soybean Hull Pellets   -> SKIPPED (no listed futures)

For Soybean Meal: a basis of "$8.50/ton" is stored as basis_cents=850.
The app uses grain == "Soybean Meal" to display it as "$8.50/t" instead of "850¢".

Symbol derivation:
  "Jul 26 Soybeans"     -> ZSN26
  "Jul 26 Soybean Meal" -> ZMN26
  "Dec 26 Corn"         -> ZCZ26
"""
import re
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

_MONTH_CODE: dict[str, str] = {
    "January":   "F", "Jan": "F",
    "February":  "G", "Feb": "G",
    "March":     "H", "Mar": "H",
    "April":     "J", "Apr": "J",
    "May":       "K",
    "June":      "M", "Jun": "M",
    "July":      "N", "Jul": "N",
    "August":    "Q", "Aug": "Q",
    "September": "U", "Sep": "U",
    "October":   "V", "Oct": "V",
    "November":  "X", "Nov": "X",
    "December":  "Z", "Dec": "Z",
}


def _symbol_from_futures_month(text: str) -> Optional[str]:
    """
    "Jul 26 Soybeans"     -> ZSN26
    "Jul 26 Soybean Meal" -> ZMN26
    "Dec 26 Corn"         -> ZCZ26
    """
    parts = text.strip().split()
    if len(parts) < 3:
        return None

    month_str     = parts[0].title()
    year_str      = parts[1]
    commodity_str = " ".join(parts[2:]).upper()

    month_code = _MONTH_CODE.get(month_str)
    if not month_code:
        return None

    if not year_str.isdigit():
        return None
    year_2d = year_str if len(year_str) == 2 else year_str[-2:]

    if "SOYBEAN MEAL" in commodity_str or "SOY MEAL" in commodity_str:
        cme_root = "ZM"
    elif "SOYBEAN" in commodity_str or "SOY" in commodity_str:
        cme_root = "ZS"
    elif "CORN" in commodity_str:
        cme_root = "ZC"
    else:
        return None

    return f"{cme_root}{month_code}{year_2d}"


_COMM_TO_GRAIN: dict[str, Optional[str]] = {
    "SOYBEANS":             "Soybeans",
    "YSB":                  "Soybeans",
    "SB":                   "Soybeans",
    "SOYBEAN MEAL":         "Soybean Meal",
    "SOY MEAL":             "Soybean Meal",
    "SM":                   "Soybean Meal",
    "CORN":                 "Corn",
    "YC":                   "Corn",
    "CN":                   "Corn",
    "SOYBEAN HULL PELLETS": None,
    "HULL PELLETS":         None,
    "SHP":                  None,
}

_GRAIN_PFX: dict[str, str] = {
    "Soybeans":     "SB",
    "Soybean Meal": "SM",
    "Corn":         "CN",
}


def _norm_grain(comm_text: str) -> Optional[str]:
    """Map commodity text/abbreviation to display grain name, or None to skip."""
    key = comm_text.strip().upper()
    if key in _COMM_TO_GRAIN:
        return _COMM_TO_GRAIN[key]
    if "SOYBEAN MEAL" in key or "SOY MEAL" in key:
        return "Soybean Meal"
    if "HULL PELLETS" in key:
        return None
    if "SOYBEAN" in key or "SOY" in key:
        return "Soybeans"
    if "CORN" in key:
        return "Corn"
    return comm_text.strip().title()


def _delivery_key(delivery_str: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", delivery_str.upper())


def _basis_to_cents(basis_str: str) -> Optional[int]:
    """Convert basis string to integer (stored *100). Works for both ¢/bu and $/ton."""
    try:
        return int(round(float(basis_str) * 100))
    except (ValueError, TypeError):
        return None


def parse_agp_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one AGP location dict into a NewSnapshotRequest, or None if no valid rows.

    Args:
        loc: dict from agp_scraper.fetch_agp_bids():
             {loc_id, location_name, state, city, timestamp,
              cashbids: [{commodity, delivery, bid, basis, futures_month}]}
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        commodity     = bid.get("commodity",     "") or ""
        delivery      = bid.get("delivery",      "") or ""
        basis_str     = bid.get("basis")
        futures_month = bid.get("futures_month", "") or ""

        if not basis_str or not futures_month or not delivery:
            continue

        grain = _norm_grain(commodity)
        if grain is None:
            continue

        basis_cents = _basis_to_cents(basis_str)
        if basis_cents is None:
            continue

        cme_sym = _symbol_from_futures_month(futures_month)
        if not cme_sym:
            continue

        del_key = _delivery_key(delivery)
        pfx     = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id  = f"{pfx}_{cme_sym}_{del_key}"

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = delivery,
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = delivery.strip().upper() == "CASH",
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "AGP",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
