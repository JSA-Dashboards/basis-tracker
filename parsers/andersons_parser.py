"""
The Andersons Grain cash-bid parser.

Converts per-location dicts from andersons_scraper.fetch_andersons_bids()
into NewSnapshotRequest objects.

Symbol conversion (Andersons format → CME format):
  Andersons uses: <root><month_code><2-digit-year>
    e.g.  CN26  →  ZCN26   (Corn July 2026)
          SN26  →  ZSN26   (Soybeans July 2026)
          WN26  →  ZWN26   (SRW Wheat July 2026)
          CZ26  →  ZCZ26   (Corn Dec 2026)
          SX26  →  ZSX26   (Soybeans Nov 2026)
          KWN26 →  KEN26   (HRW Wheat, KW root → KE)
          MWN26 →  MWEN26  (MGEX Wheat, MW root → MWE)

Delivery label:
  "JUN 26"  →  "Jun 2026"
  "ON 26"   →  "Oct/Nov 2026"   (old/new-crop window against Dec board)

Basis string:
  "0.15"  →  +15 cents
  "-0.80" →  -80 cents
"""
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

# ── Symbol conversion ──────────────────────────────────────────────────────────
# Andersons symbols have the year already as 2 digits, so no "2" prefix needed.
# Format: <root><month_code><2-digit-year>

_TWO_CHAR_ROOTS: frozenset[str] = frozenset({"KW", "MW"})

_ROOT_TO_CME: dict[str, str] = {
    "C":  "ZC",    # Corn (CBOT)
    "S":  "ZS",    # Soybeans (CBOT)
    "W":  "ZW",    # Soft Red Winter Wheat (CBOT)
    "KW": "KE",    # Hard Red Winter Wheat (KC)
    "MW": "MWE",   # Minneapolis Spring Wheat (MGEX)
    "O":  "ZO",    # Oats (CBOT)
}


def _convert_symbol(sym: str) -> str:
    """
    Convert an Andersons futures symbol to CME format.

    Andersons:  CN26  →  CME: ZCN26
    Andersons:  KWN26 →  CME: KEN26
    Andersons:  CZ26  →  CME: ZCZ26
    """
    if not sym or len(sym) < 4:
        return sym

    # Detect 2-char root first
    root2 = sym[:2].upper()
    if root2 in _TWO_CHAR_ROOTS:
        root       = root2
        month_code = sym[2]
        year_2d    = sym[3:]
    else:
        root       = sym[0].upper()
        month_code = sym[1]
        year_2d    = sym[2:]

    if not year_2d or not year_2d.isdigit():
        return sym  # malformed — return as-is

    cme_root = _ROOT_TO_CME.get(root, "Z" + root)
    return f"{cme_root}{month_code}{year_2d}"


# ── Grain normalisation ────────────────────────────────────────────────────────
# The Andersons commodity names come from the HTML remove-link href.

_GRAIN_NORM: dict[str, str] = {
    "corn":        "Corn",
    "soybean":     "Soybeans",
    "soybeans":    "Soybeans",
    "red wheat":   "Wheat",
    "wheat":       "Wheat",
    "white wheat": "White Wheat",
    "soft wheat":  "White Wheat",
}

_GRAIN_PFX: dict[str, str] = {
    "Corn":        "CN",
    "Soybeans":    "SB",
    "Wheat":       "WH",
    "White Wheat": "WW",
}

# ── Delivery helpers ───────────────────────────────────────────────────────────
# Andersons displays delivery as "MMM YY", e.g. "JUN 26", "ON 26"
_MONTH_3: dict[str, str] = {
    # Standard months
    "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
    "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
    "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
    # Andersons multi-month delivery windows
    "ON":    "Oct/Nov",   # Oct/Nov new-crop against Dec corn or Nov beans
    "SON":   "Sep-Nov",   # Sep/Oct/Nov harvest window
    "JJ":    "Jun/Jul",   # June or July delivery
    "JA":    "Jul/Aug",   # July or August delivery
    "LHSON": "LH Oct/Nov",  # Late Harvest Oct/Nov (soybean specific)
}


def _delivery_label(delivery_str: str) -> str:
    """
    Convert Andersons delivery string to human-readable label.

    "JUN 26"   → "Jun 2026"
    "ON 26"    → "Oct/Nov 2026"
    "SON 26"   → "Sep-Nov 2026"
    "JJ 26"    → "Jun/Jul 2026"
    "JA 26"    → "Jul/Aug 2026"
    "LHSON 26" → "LH Oct/Nov 2026"
    """
    parts = delivery_str.strip().split()
    if len(parts) != 2:
        return delivery_str
    mon_key  = parts[0].upper()
    yr_short = parts[1]
    mon_lbl  = _MONTH_3.get(mon_key, mon_key.title())
    yr       = "20" + yr_short if len(yr_short) == 2 else yr_short
    return f"{mon_lbl} {yr}"


def _delivery_key(delivery_str: str) -> str:
    """
    Normalise delivery string for use in row IDs (stable across scrapes).

    "JUN 26" → "JUN26"
    "ON 26"  → "ON26"
    """
    return delivery_str.strip().replace(" ", "").upper()


def _basis_to_cents(basis_str: str) -> Optional[int]:
    """Convert basis string ('0.15', '-0.80') to integer cents (+15, -80)."""
    try:
        return int(round(float(basis_str) * 100))
    except (ValueError, TypeError):
        return None


def _norm_grain(commodity: str) -> str:
    return _GRAIN_NORM.get(commodity.strip().lower(), commodity.strip().title())


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_andersons_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one Andersons location dict into a NewSnapshotRequest, or None
    if there are no valid bid rows.

    Args:
        loc: dict produced by andersons_scraper.fetch_andersons_bids():
             {location_name, state, city, timestamp,
              cashbids: [{commodity, delivery, basis, symbol}]}

    Returns:
        NewSnapshotRequest ready for database.upsert_snapshot(), or None.
    """
    location_name = loc.get("location_name", "").strip()
    timestamp     = loc.get("timestamp", "")
    cashbids      = loc.get("cashbids") or []

    rows: list[SnapshotRow] = []
    for bid in cashbids:
        commodity  = bid.get("commodity", "") or ""
        delivery   = bid.get("delivery",  "") or ""
        basis_str  = bid.get("basis")
        symbol_raw = bid.get("symbol",    "") or ""

        if not basis_str or not symbol_raw or not delivery:
            continue

        basis_cents = _basis_to_cents(basis_str)
        if basis_cents is None:
            continue

        grain     = _norm_grain(commodity)
        cme_sym   = _convert_symbol(symbol_raw)
        del_label = _delivery_label(delivery)
        del_key   = _delivery_key(delivery)
        pfx       = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id    = f"{pfx}_{cme_sym}_{del_key}"

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
        provider  = "Andersons",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
