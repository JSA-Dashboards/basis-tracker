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
from bs4 import BeautifulSoup

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

# WPE redesigned its homepage widget (2026): the terse "Corn +24u" letter-suffix
# text is gone. It's now a <table class="daily-bids__table"> of period rows —
# a "daily-bids__period" label (e.g. "Harvest 26", "Sept 2026") with Corn/Milo
# "daily-bids__price-col" cells holding the basis (or "NB" = no bid, no letter).
# So we read the period label and map it to the corn contract ourselves.

# Delivery-month number → CME corn contract letter (standard corn convention).
_CORN_BY_MONTH = {1: "H", 2: "H", 3: "H", 4: "K", 5: "K", 6: "N",
                  7: "N", 8: "U", 9: "U", 10: "Z", 11: "Z", 12: "Z"}
# Words that appear in a period label → its delivery month. "Harvest"/"fall"/
# "new crop" = the Dec (Z) new-crop bid. Longest keys matched first.
_PERIOD_MONTH = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "sept": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "harvest": 12, "new crop": 12, "fall": 12, "spot": None,
}


def _symbol_for_period(period: str, today: datetime):
    """Map a WPE period label → (corn CME symbol, delivery label). The label's own
    year drives the contract year ('Harvest 26' → ZCZ26, 'Sept 2026' → ZCU26).
    Returns (None, period) if no month can be resolved."""
    lab = period.lower()
    ym = re.search(r"\b(?:20)?(\d{2})\b", period)     # '26' or '2026'
    year = int(ym.group(1)) if ym else today.year % 100
    mnum = None
    for name in sorted(_PERIOD_MONTH, key=len, reverse=True):
        if name in lab:
            mnum = _PERIOD_MONTH[name]
            break
    if mnum is None:                                  # e.g. "Spot" or unknown label
        mnum = today.month
    letter = _CORN_BY_MONTH[mnum]
    return f"ZC{letter}{year % 100:02d}", period


def fetch_wpe_bids() -> list[dict]:
    """Scrape WPE's homepage daily-bid table. Returns a single-location list
    matching parsers/wpe_parser.parse_wpe_location, or [] if unparseable / all NB."""
    today = datetime.now()
    try:
        r = requests.get(WPE_URL, headers=_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        log.error("WPE fetch failed: %s", exc)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.select_one("table.daily-bids__table")
    if table is None:
        log.warning("WPE: daily-bids table not found (markup changed again).")
        return []

    cashbids: list[dict] = []
    seen: set[tuple] = set()
    last_date = None
    for tr in table.select("tr"):
        cls = tr.get("class") or []
        if "daily-bids__period-tr" not in cls:
            dh = tr.select_one(".daily-bids__date-h5")   # a "Last Updated" header row
            if dh:
                last_date = dh.get_text(strip=True)
            continue
        per_el = tr.select_one(".daily-bids__period")
        period = per_el.get_text(strip=True) if per_el else ""
        if not period:
            continue
        symbol, label = _symbol_for_period(period, today)
        if not symbol:
            continue
        for col in tr.select(".daily-bids__price-col"):
            title_el = col.select_one(".daily-bids__price-title")
            grain = (title_el.get_text(strip=True) if title_el else "").title()
            if grain not in ("Corn", "Milo", "Soybeans", "Wheat"):
                continue
            val_txt = col.get_text(" ", strip=True)
            if title_el:
                val_txt = val_txt.replace(title_el.get_text(strip=True), "", 1).strip()
            num = re.search(r"[+\-]?\d+(?:\.\d+)?", val_txt)
            if not num:                                  # "NB" / blank = no bid
                continue
            key = (grain, symbol)
            if key in seen:                              # first (nearest) period per symbol
                continue
            seen.add(key)
            cashbids.append({
                "bid_id":         f"{grain[:2].upper()}_{symbol}",
                "grain":          grain,
                "symbol":         symbol,
                "basis":          int(round(float(num.group()))),
                "delivery_month": label,
            })

    if not cashbids:
        log.warning("WPE: table found but no numeric bids (all NB, or layout changed).")
        return []
    if last_date:
        log.info("WPE daily bid last updated: %s", last_date)

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
