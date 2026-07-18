"""
Western Plains Energy (Oakley, KS) cash-bid scraper.

WPE is an ethanol plant that bids corn and milo (grain sorghum). Unlike the
agricharts plants, WPE hand-posts a tiny "Daily Bid" block on its homepage
(https://wpellc.com/) — no JSON feed, no delivery dates, no cash price. Each
commodity shows a basis figure with a one-letter futures-month suffix, e.g.:

    A/S 2026     Corn +24u   Milo -10u        (u = September corn, ZCU)
    Harvest 26   Corn  -4z   Milo -30z        (z = December  corn, ZCZ)

Milo is priced off the corn board, same as the elevator/rail sorghum feeds. We
parse the visible text, map the month letter to a CME corn symbol for the
nearest matching contract year, and emit one row per (commodity, period).

Caveats: this is a hand-updated HTML widget, so it can be stale (it carries a
"Last Updated" date) and its markup could change. It's a best-effort scrape of a
single high-value milo location; if the block can't be parsed we return nothing
rather than guessing.

Usage (standalone test):
    python wpe_scraper.py
"""
import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

WPE_URL   = "https://wpellc.com/"
PROVIDER  = "Western Plains Energy"
LOCATION  = "Oakley, KS"
STATE     = "KS"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# CME month code → calendar month number
_MONTH_NUM = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
              "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}

# Corn board root for milo (priced vs corn) and corn.
_ROOT = {"Corn": "ZC", "Milo": "ZC"}

# Matches "Corn +24u", "Milo -30z", etc. (basis may be integer or decimal cents)
_BID_RE = re.compile(
    r"\b(Corn|Milo|Soybeans|Wheat)\s+([+\-]?\d+(?:\.\d+)?)\s*([FGHJKMNQUVXZ])\b",
    re.I,
)


def _symbol_for(root: str, letter: str, today: datetime) -> str:
    """Nearest future CME symbol for a month letter, e.g. ('ZC','U') → 'ZCU26'."""
    letter = letter.upper()
    mnum   = _MONTH_NUM[letter]
    year   = today.year
    # If that month has already passed this year, roll to next year's contract.
    if mnum < today.month:
        year += 1
    return f"{root}{letter}{year % 100:02d}"


def _delivery_label(letter: str, today: datetime) -> str:
    """Month letter → 'Mon YYYY' delivery label off the futures month."""
    letter = letter.upper()
    mnum   = _MONTH_NUM[letter]
    year   = today.year + (1 if mnum < today.month else 0)
    return datetime(year, mnum, 1).strftime("%b %Y")


def fetch_wpe_bids() -> list[dict]:
    """Scrape WPE's homepage daily-bid block. Returns a single-location list
    matching parsers/wpe_parser.parse_wpe_location, or [] if unparseable."""
    today = datetime.now()
    try:
        r = requests.get(WPE_URL, headers=_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        log.error("WPE fetch failed: %s", exc)
        return []

    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))

    cashbids: list[dict] = []
    seen: set[tuple] = set()
    for m in _BID_RE.finditer(text):
        grain  = m.group(1).title()
        basis  = float(m.group(2))
        letter = m.group(3).upper()
        root   = _ROOT.get(grain)
        if root is None:
            continue
        symbol = _symbol_for(root, letter, today)
        key    = (grain, symbol)
        if key in seen:            # first (nearest) period wins per symbol
            continue
        seen.add(key)
        cashbids.append({
            "bid_id":         f"{grain[:2].upper()}_{symbol}",
            "grain":          grain,
            "symbol":         symbol,
            "basis":          int(round(basis)),
            "delivery_month": _delivery_label(letter, today),
        })

    if not cashbids:
        log.warning("WPE: no bids parsed from homepage (markup may have changed).")
        return []

    # Optional freshness note from the "Last Updated" stamp.
    upd = re.search(r"Last Updated:\s*([A-Za-z]+ \d{1,2}, \d{4})", text)
    if upd:
        log.info("WPE daily bid last updated: %s", upd.group(1))

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return [{
        "provider":      PROVIDER,
        "location_name": LOCATION,
        "state":         STATE,
        "facility_type": "Corn Processing",
        "timestamp":     today_utc,
        "cashbids":      cashbids,
    }]


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sys.path.insert(0, str(Path(__file__).parent))
    from parsers.wpe_parser import parse_wpe_location

    locs = fetch_wpe_bids()
    for loc in locs:
        snap = parse_wpe_location(loc)
        if snap:
            for row in snap.rows:
                print(f"  {snap.location:12s} {row.grain:6s} {row.deliveryMonth:9s} "
                      f"{row.futuresSymbol:7s} basis {row.basisCents:+d}")
        else:
            print("  (no valid bids)")
