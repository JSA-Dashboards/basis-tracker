"""
Scoular cash-bid parser.

Converts per-location dicts from scoular_scraper.fetch_scoular_bids() into
NewSnapshotRequest objects.

Symbol derivation: "Futures Month" column text -> CME format
  "Jul 26 Soybeans"            -> ZSN26
  "Nov 26 Soybeans mini-siz"   -> ZSX26
  "Dec 26 Corn"                -> ZCZ26
  "Jul 26 KCBT Red Wheat"      -> KEN26   (HRW Wheat, KC Board of Trade)
  "Jul 26 MIAX Spring Wheat"   -> MWEN26  (Minneapolis Spring Wheat)
  "Jul 26 Wheat"               -> ZWN26   (SRW Wheat)

Commodity abbreviation (h3) -> display grain name:
  YSB          -> "Soybeans"
  YC           -> "Corn"
  HRWW         -> "HRW Wheat"
  DNSW         -> "Spring Wheat"
  SOR          -> "Sorghum"
  SRWW         -> "SRW Wheat"
  IP Soybeans  -> "IP Soybeans"

Delivery labels are kept as-is (already human-readable):
  "Cash", "June 2026", "New Crop 2026", "Fall 2026", "Dlvd Logan"
"""
import re
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

# ── Month name -> CME month code ──────────────────────────────────────────────
_MONTH_CODE: dict[str, str] = {
    "January":   "F",  "Jan": "F",
    "February":  "G",  "Feb": "G",
    "March":     "H",  "Mar": "H",
    "April":     "J",  "Apr": "J",
    "May":       "K",
    "June":      "M",  "Jun": "M",
    "July":      "N",  "Jul": "N",
    "August":    "Q",  "Aug": "Q",
    "September": "U",  "Sep": "U",
    "October":   "V",  "Oct": "V",
    "November":  "X",  "Nov": "X",
    "December":  "Z",  "Dec": "Z",
}


def _symbol_from_futures_month(text: str) -> Optional[str]:
    """
    Derive a CME futures symbol from Scoular's "Futures Month" column text.

    Format:  "<Month> <YY> <Commodity description...>"

    Examples:
      "Jul 26 Soybeans"          -> ZSN26
      "Nov 26 Soybeans mini-siz" -> ZSX26
      "Dec 26 Corn"              -> ZCZ26
      "Jul 26 KCBT Red Wheat"    -> KEN26
      "Jul 26 MIAX Spring Wheat" -> MWEN26
      "Jul 26 Wheat"             -> ZWN26
      "Jul 27 KCBT Red Wheat"    -> KEN27
    """
    parts = text.strip().split()
    if len(parts) < 3:
        return None

    month_str     = parts[0].title()           # "Jul", "Nov", ...
    year_str      = parts[1]                   # "26", "27", "2026"
    commodity_str = " ".join(parts[2:]).upper() # "SOYBEANS MINI-SIZ", "KCBT RED WHEAT"

    month_code = _MONTH_CODE.get(month_str)
    if not month_code:
        return None

    if not year_str.isdigit():
        return None
    year_2d = year_str if len(year_str) == 2 else year_str[-2:]

    # Determine CME root from commodity description keywords
    if "KCBT" in commodity_str or "RED WHEAT" in commodity_str:
        cme_root = "KE"          # HRW Wheat (Kansas City)
    elif "MIAX" in commodity_str or "SPRING WHEAT" in commodity_str:
        cme_root = "MWE"         # Minneapolis Spring Wheat
    elif "WHEAT" in commodity_str:
        cme_root = "ZW"          # SRW Wheat (CBOT)
    elif "SOYBEAN" in commodity_str:
        cme_root = "ZS"          # Soybeans (CBOT)
    elif "CORN" in commodity_str or "SORGHUM" in commodity_str:
        cme_root = "ZC"          # Corn (CBOT); sorghum priced vs corn board
    else:
        return None

    return f"{cme_root}{month_code}{year_2d}"


# ── Commodity abbreviation -> grain display name ──────────────────────────────
_COMM_TO_GRAIN: dict[str, str] = {
    "YSB":         "Soybeans",
    "YC":          "Corn",
    "HRWW":        "HRW Wheat",
    "DNSW":        "Spring Wheat",
    "SOR":         "Sorghum",
    "SRWW":        "SRW Wheat",
    "IP SOYBEANS": "IP Soybeans",
    # less common abbreviations
    "SB":          "Soybeans",
    "CORN":        "Corn",
    "HRW":         "HRW Wheat",
    "DNS":         "Spring Wheat",
    "SRW":         "SRW Wheat",
}

_GRAIN_PFX: dict[str, str] = {
    "Soybeans":    "SB",
    "Corn":        "CN",
    "HRW Wheat":   "HW",
    "Spring Wheat":"SW",
    "Sorghum":     "SO",
    "SRW Wheat":   "SR",
    "IP Soybeans": "IS",
}


def _norm_grain(comm_abbrev: str) -> str:
    """Map commodity abbreviation to display grain name."""
    key = comm_abbrev.strip().upper()
    return _COMM_TO_GRAIN.get(key, comm_abbrev.strip().title())


def _delivery_key(delivery_str: str) -> str:
    """
    Normalise delivery label for stable row IDs.

    "June 2026"       -> "JUNE2026"
    "Cash"            -> "CASH"
    "New Crop 2026"   -> "NEWCROP2026"
    "Fall 2026"       -> "FALL2026"
    "Dlvd Logan"      -> "DLVDLOGAN"
    "New Crop 2027"   -> "NEWCROP2027"
    """
    return re.sub(r"[^A-Z0-9]", "", delivery_str.upper())


def _basis_to_cents(basis_str: str) -> Optional[int]:
    """Convert basis string ('-0.36', '0.15') to integer cents (-36, +15)."""
    try:
        return int(round(float(basis_str) * 100))
    except (ValueError, TypeError):
        return None


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_scoular_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one Scoular location dict into a NewSnapshotRequest, or None
    if there are no valid bid rows.

    Args:
        loc: dict produced by scoular_scraper.fetch_scoular_bids():
             {loc_id, location_name, state, city, timestamp,
              cashbids: [{commodity, delivery, bid, basis, futures_month}]}

    Returns:
        NewSnapshotRequest ready for database.upsert_snapshot(), or None.
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

        basis_cents = _basis_to_cents(basis_str)
        if basis_cents is None:
            continue

        cme_sym = _symbol_from_futures_month(futures_month)
        if not cme_sym:
            continue

        grain   = _norm_grain(commodity)
        del_key = _delivery_key(delivery)
        pfx     = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id  = f"{pfx}_{cme_sym}_{del_key}"

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = delivery,       # already human-readable
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = delivery.strip().upper() == "CASH",
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "Scoular",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
