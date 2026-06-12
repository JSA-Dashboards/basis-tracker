"""
Bunge AG cash-bid parser.

Converts per-location dicts from bunge_scraper.fetch_bunge_bids() into
NewSnapshotRequest objects.

Symbol format:  @{root}{month_code}{2-digit-year}   (DTN, same layout as Andersons)
  e.g.  @SN26   →  ZSN26    (Soybeans July 2026)
        @CN26   →  ZCN26    (Corn July 2026)
        @WN26   →  ZWN26    (SRW Wheat July 2026)
        @KWN26  →  KEN26    (HRW Wheat July 2026)
        @MWN26  →  MWEN26   (Minneapolis Spring Wheat July 2026)
        @SMN26  →  ZMN26    (Soybean Meal July 2026)
        @SX26   →  ZSX26    (Soybeans Nov 2026)
        @CZ26   →  ZCZ26    (Corn Dec 2026)

Delivery labels (e.g. "By June 26", "LH July", "HARV26") are kept as-is —
they are already human-readable free-text descriptions from the page.

Basis is a string like "0.3000" or "-0.8500" (USD/bu or USD/ton for meal);
converted to integer cents (×100 and rounded).
"""
import re
from typing import Optional

from models import NewSnapshotRequest, SnapshotRow

# ── Symbol conversion ──────────────────────────────────────────────────────────
# Bunge/DTN format:  @ + root + month_code + 2-digit-year
# Strip '@', then identical mapping to Andersons symbols.

_TWO_CHAR_ROOTS: frozenset[str] = frozenset({"KW", "MW", "SM", "BO"})

_ROOT_TO_CME: dict[str, str] = {
    "C":  "ZC",    # Corn (CBOT)
    "S":  "ZS",    # Soybeans (CBOT)
    "W":  "ZW",    # Soft Red Winter Wheat (CBOT)
    "KW": "KE",    # Hard Red Winter Wheat (KC)
    "MW": "MWE",   # Minneapolis Spring Wheat (MGEX)
    "SM": "ZM",    # Soybean Meal (CBOT)
    "BO": "ZL",    # Soybean Oil (CBOT)
    "O":  "ZO",    # Oats (CBOT)
}


def _convert_symbol(sym: str) -> str:
    """
    Convert a Bunge DTN futures symbol to CME format.

    @SN26  →  ZSN26
    @KWN26 →  KEN26
    @MWN26 →  MWEN26
    """
    if not sym:
        return sym

    s = sym.lstrip("@")   # strip leading '@'
    if len(s) < 4:
        return sym

    # Detect 2-char root
    root2 = s[:2].upper()
    if root2 in _TWO_CHAR_ROOTS:
        root       = root2
        month_code = s[2]
        year_2d    = s[3:]
    else:
        root       = s[0].upper()
        month_code = s[1]
        year_2d    = s[2:]

    if not year_2d or not year_2d.isdigit():
        return sym  # malformed

    cme_root = _ROOT_TO_CME.get(root, "Z" + root)
    return f"{cme_root}{month_code}{year_2d}"


# ── Grain normalisation ────────────────────────────────────────────────────────
# Keys are the tab-pane id values from the HTML page.

_TAB_TO_GRAIN: dict[str, str] = {
    "soybeans":                      "Soybeans",
    "high-oleic-soybeans":           "Hi-Oleic Soy",
    "corn":                          "Corn",
    "yellow-corn":                   "Corn",
    "wheat":                         "Wheat",
    "soybean-meal":                  "Soy Meal",
    "soybean-meal-picked-up-marion": "Soy Meal",
    "soybean-hulls":                 "Soy Hulls",
    "soybean-hulls---loose":         "Soy Hulls",
    "soybean-hulls---pellets":       "Soy Hulls (Pellets)",
    "dns-14":                        "DNS Wheat",
    "hrw-11-5":                      "HRW Wheat",
    "ns-dns-14":                     "DNS Wheat",
    "winter-canola":                 "Canola",
}

_GRAIN_PFX: dict[str, str] = {
    "Corn":               "CN",
    "Soybeans":           "SB",
    "Hi-Oleic Soy":       "HO",
    "Wheat":              "WH",
    "Soy Meal":           "SM",
    "Soy Hulls":          "SH",
    "Soy Hulls (Pellets)":"SP",
    "DNS Wheat":          "DN",
    "HRW Wheat":          "HW",
    "Canola":             "CA",
}


def _norm_grain(commodity_tab: str) -> str:
    """Map tab-pane id to display grain name."""
    return _TAB_TO_GRAIN.get(commodity_tab.strip().lower(),
                             commodity_tab.strip().replace("-", " ").title())


def _delivery_key(delivery_str: str) -> str:
    """
    Normalise delivery label for stable row IDs.

    "By June 26"      → "BYJUNE26"
    "Bal June/FH July"→ "BALJUNEFHJULY"
    "HARV26"          → "HARV26"
    "J/J"             → "JJ"
    """
    return re.sub(r"[^A-Z0-9]", "", delivery_str.upper())


def _basis_to_cents(basis_str: str) -> Optional[int]:
    """Convert basis string ('0.3000', '-0.8500') to integer cents (+30, -85)."""
    try:
        return int(round(float(basis_str) * 100))
    except (ValueError, TypeError):
        return None


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_bunge_location(loc: dict) -> Optional[NewSnapshotRequest]:
    """
    Convert one Bunge location dict into a NewSnapshotRequest, or None
    if there are no valid bid rows.

    Args:
        loc: dict produced by bunge_scraper.fetch_bunge_bids():
             {slug, location_name, state, city, timestamp,
              cashbids: [{commodity, delivery, symbol, basis}]}

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
        symbol_raw = bid.get("symbol",    "") or ""
        basis_str  = bid.get("basis")

        if not basis_str or not symbol_raw or not delivery:
            continue

        basis_cents = _basis_to_cents(basis_str)
        if basis_cents is None:
            continue

        grain     = _norm_grain(commodity)
        cme_sym   = _convert_symbol(symbol_raw)
        del_key   = _delivery_key(delivery)
        pfx       = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id    = f"{pfx}_{cme_sym}_{del_key}"

        rows.append(SnapshotRow(
            id            = row_id,
            grain         = grain,
            deliveryMonth = delivery,   # keep free-text label as-is
            futuresSymbol = cme_sym,
            basisCents    = basis_cents,
            isSpot        = False,
        ))

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp = timestamp,
        provider  = "Bunge",
        location  = location_name,
        source    = "web",
        rows      = rows,
    )
