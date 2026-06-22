"""
norfolkcrush_scraper.py — Norfolk Crush (Norfolk, NE) soybean bid scraper.

Page: https://norfolkcrush.com/bid-offers/soybeans/ — its cash-bid table is a
CI Hedging (cihedging.com) widget. The widget POSTs to the company endpoint and
gets back rendered HTML, so we hit that endpoint directly with requests (no
browser needed):

    POST https://www.cihedging.com/cih/api/index.cfm/origination/cashbids/132225

(132225 is Norfolk Crush's CashBid_JS company id, set on the page.) The response
is escaped HTML; columns per data row: Delivery | Futures | Futures Price |
Change | Basis | Bid. Basis is dollar notation (0.25 = +25¢, -0.55 = -55¢).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_COMPANY_ID = "132225"
_API_URL    = f"https://www.cihedging.com/cih/api/index.cfm/origination/cashbids/{_COMPANY_ID}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://norfolkcrush.com/",
    "Accept":  "text/html, */*",
}

_MONTH_CODES = {
    "jan": "F", "feb": "G", "mar": "H", "apr": "J",
    "may": "K", "jun": "M", "jul": "N", "aug": "Q",
    "sep": "U", "oct": "V", "nov": "X", "dec": "Z",
}


def _cme_symbol(futures_text: str) -> str | None:
    """Parse "Jul'26" / "Nov'27" → "ZSN26" / "ZSX27"."""
    txt = futures_text.replace("’", "'").replace("‘", "'").strip()
    m = re.match(r"([A-Za-z]+)'(\d{2,4})", txt)
    if not m:
        return None
    mon = m.group(1).lower()[:3]
    yr  = int(m.group(2))
    if yr > 100:
        yr = yr % 100
    code = _MONTH_CODES.get(mon)
    return f"ZS{code}{yr:02d}" if code else None


def _basis_cents(text: str) -> int | None:
    """Convert dollar-format basis ("0.25", "-0.55") to integer cents."""
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
    Fetch Norfolk Crush soybean bids from the CI Hedging endpoint (no browser).
    Returns a list with one location dict for parse_norfolkcrush_location.
    """
    try:
        resp = requests.post(_API_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("NorfolkCrush: fetch failed: %s", exc)
        return []

    # The endpoint returns JSON-style escaped HTML — unescape it.
    html = (resp.text
            .replace('\\"', '"').replace('\\/', '/')
            .replace('\\t', '\t').replace('\\n', '\n').replace('\\r', ''))

    bids: list[dict] = []
    seen: set[str]   = set()

    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)  # data rows use <td>
        if len(cells) < 6:
            continue
        vals = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in cells]
        delivery_txt, futures_txt, basis_txt = vals[0], vals[1], vals[4]

        sym   = _cme_symbol(futures_txt)
        cents = _basis_cents(basis_txt)
        if not sym or cents is None:
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
