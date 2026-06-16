"""
norfolkcrush_scraper.py — Norfolk Crush (Norfolk, NE) soybean bid scraper.

Page: https://norfolkcrush.com/bid-offers/soybeans/
Built with Elementor; each bid is a div.elementor-widget-text-editor
containing a <p> with <b>/<strong> labels separated by <br/>.

Basis is displayed in dollars (e.g. -0.35) → stored as cents (-35).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_URL = "https://norfolkcrush.com/bid-offers/soybeans/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

_MONTH_CODES = {
    "jan": "F", "feb": "G", "mar": "H", "apr": "J",
    "may": "K", "jun": "M", "jul": "N", "aug": "Q",
    "sep": "U", "oct": "V", "nov": "X", "dec": "Z",
}

_MONTH_NAMES = {
    "jan": "January",  "feb": "February", "mar": "March",    "apr": "April",
    "may": "May",      "jun": "June",     "jul": "July",     "aug": "August",
    "sep": "September","oct": "October",  "nov": "November", "dec": "December",
}


def _cme_symbol(futures_text: str) -> str | None:
    """
    Parse CME symbol from text like "Nov '26 | $10.50" → "ZSX26".
    Handles curly apostrophes and 2- or 4-digit years.
    """
    txt = futures_text.replace("’", "'").replace("‘", "'")
    m = re.search(r"([A-Za-z]+)\s*'(\d{2,4})", txt)
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


def _basis_cents(basis_text: str) -> int | None:
    """
    Parse basis from dollar-denominated text like "-0.35" → -35 cents.
    Returns None for TBD / missing values.
    """
    txt = basis_text.strip()
    if not txt or txt.upper() in ("TBD", "N/A", "-"):
        return None
    m = re.search(r"([+-]?\d+\.?\d*)", txt)
    if not m:
        return None
    try:
        return round(float(m.group(1)) * 100)
    except ValueError:
        return None


def _delivery_label(raw: str) -> str:
    """
    Normalize delivery text: "SEP/OCT '26" → "Sep/Oct 2026",
    "NOV '26" → "November 2026".
    """
    txt = raw.strip().replace("’", "'").replace("‘", "'")
    # Replace 2-digit year suffix with 4-digit
    txt = re.sub(r"'(\d{2})\b", lambda m: str(2000 + int(m.group(1))), txt)
    # Title-case month names but keep "/" separator
    parts = txt.split("/")
    result_parts = []
    for part in parts:
        # Check if we can expand the month abbreviation
        tok = part.strip().split()
        if tok:
            mon_key = tok[0].lower()[:3]
            if mon_key in _MONTH_NAMES and len(tok[0]) <= 4:
                tok[0] = _MONTH_NAMES[mon_key]
            result_parts.append(" ".join(tok))
        else:
            result_parts.append(part)
    return "/".join(result_parts)


def fetch_norfolkcrush_bids() -> list[dict]:
    """
    Fetch Norfolk Crush soybean bids.
    Returns a list with one location dict for parse_norfolkcrush_location.
    """
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        log.error("NorfolkCrush: fetch failed: %s", exc)
        return []

    soup  = BeautifulSoup(resp.text, "html.parser")
    bids: list[dict] = []
    seen: set[str]   = set()

    for widget in soup.select("div.elementor-widget-text-editor"):
        p = widget.find("p")
        if not p:
            continue

        # Collapse all text, normalising whitespace and smart-quotes
        raw = p.get_text(separator=" ")
        raw = raw.replace("’", "'").replace("‘", "'").replace("\xa0", " ")
        raw = re.sub(r"\s+", " ", raw).strip()

        if "Delivery" not in raw or "Futures" not in raw:
            continue

        def _field(label: str, stop: str) -> str:
            pat = rf"{label}:\s*(.+?)(?:{stop}:|$)"
            m   = re.search(pat, raw, re.IGNORECASE)
            return m.group(1).strip() if m else ""

        delivery_txt = _field("Delivery", "Futures")
        futures_txt  = _field("Futures",  "Change")
        basis_txt    = _field("Basis",    "Bid")

        if not delivery_txt or not futures_txt or not basis_txt:
            continue

        sym   = _cme_symbol(futures_txt)
        cents = _basis_cents(basis_txt)

        if not sym or cents is None:
            log.debug("NorfolkCrush: skip %r — sym=%s cents=%s", delivery_txt, sym, cents)
            continue

        key = f"{sym}|{delivery_txt}"
        if key in seen:
            continue
        seen.add(key)

        delivery = _delivery_label(delivery_txt)
        bids.append({
            "delivery":    delivery,
            "cme_symbol":  sym,
            "basis_cents": cents,
        })
        log.debug("  NorfolkCrush: %s  %s  %+d¢", delivery, sym, cents)

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
                print(f"    {r.deliveryMonth:25s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}¢")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
