"""
norfolkcrush_scraper.py — Norfolk Crush (Norfolk, NE) soybean bid scraper.

Page: https://norfolkcrush.com/bid-offers/soybeans/
Live bids are rendered by JavaScript into tr.cashbid_tr rows; the static
HTML holds stale 2024 placeholder content, so Playwright is required.

Table columns per row: Delivery | Futures | Futures Price | Change | Basis | Cash Bid
Basis is in dollar notation (0.25 = +25¢, -0.55 = -55¢).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_URL = "https://norfolkcrush.com/bid-offers/soybeans/"

_MONTH_CODES = {
    "jan": "F", "feb": "G", "mar": "H", "apr": "J",
    "may": "K", "jun": "M", "jul": "N", "aug": "Q",
    "sep": "U", "oct": "V", "nov": "X", "dec": "Z",
}


def _cme_symbol(futures_text: str) -> str | None:
    """
    Parse CME symbol from text like "Jul'26" or "Nov'27" → "ZSN26" / "ZSX27".
    """
    txt = futures_text.replace("’", "'").replace("‘", "'").strip()
    m = re.match(r"([A-Za-z]+)'(\d{2,4})", txt)
    if not m:
        return None
    mon  = m.group(1).lower()[:3]
    yr   = int(m.group(2))
    if yr > 100:
        yr = yr % 100
    code = _MONTH_CODES.get(mon)
    if not code:
        return None
    return f"ZS{code}{yr:02d}"


def _basis_cents(text: str) -> int | None:
    """Convert dollar-format basis string ("0.25", "-0.55") to integer cents."""
    txt = text.strip()
    if not txt or txt.upper() in ("TBD", "N/A", ""):
        return None
    m = re.search(r"([+-]?\d+\.?\d*)", txt)
    if not m:
        return None
    try:
        return round(float(m.group(1)) * 100)
    except ValueError:
        return None


def fetch_norfolkcrush_bids() -> list[dict]:
    """
    Fetch Norfolk Crush soybean bids using Playwright (JS-rendered table).
    Returns a list with one location dict for parse_norfolkcrush_location.
    """
    from playwright.sync_api import sync_playwright

    bids: list[dict] = []
    seen: set[str]   = set()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page()
            page.goto(_URL, wait_until="networkidle", timeout=30000)

            rows = page.query_selector_all("tr.cashbid_tr")
            for row in rows:
                tds = row.query_selector_all("td")
                vals = [td.inner_text().strip() for td in tds]
                # Skip header / empty rows; expect 6 columns
                if len(vals) < 6 or not vals[0]:
                    continue

                delivery_txt = vals[0]   # "Jun 2026"
                futures_txt  = vals[1]   # "Jul'26"
                basis_txt    = vals[4]   # "0.25" or "-0.55"

                sym   = _cme_symbol(futures_txt)
                cents = _basis_cents(basis_txt)

                if not sym or cents is None:
                    log.debug("NorfolkCrush: skip %r — sym=%s cents=%s",
                              delivery_txt, sym, cents)
                    continue

                key = f"{sym}|{delivery_txt}"
                if key in seen:
                    continue
                seen.add(key)

                bids.append({
                    "delivery":    delivery_txt,
                    "cme_symbol":  sym,
                    "basis_cents": cents,
                })
                log.debug("  NorfolkCrush: %s  %s  %+d¢", delivery_txt, sym, cents)

            browser.close()

    except Exception as exc:
        log.error("NorfolkCrush: fetch failed: %s", exc)
        return []

    if not bids:
        log.warning("NorfolkCrush: no bids parsed")
        return []

    log.info("NorfolkCrush Norfolk  %d soybean bid(s)", len(bids))

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return [{
        "location":  "Norfolk",
        "state":     "NE",
        "timestamp": timestamp,
        "bids":      bids,
    }]


if __name__ == "__main__":
    import sys
    from pathlib import Path
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sys.path.insert(0, str(Path(__file__).parent))
    from parsers.norfolkcrush_parser import parse_norfolkcrush_location

    locs = fetch_norfolkcrush_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_norfolkcrush_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.deliveryMonth:22s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
