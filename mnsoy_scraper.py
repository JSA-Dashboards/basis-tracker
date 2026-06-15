"""
mnsoy_scraper.py — Minnesota Soybean Processors (MNSP) soybean bid scraper.

Uses the CIHedging.com cash-bid widget API (customer ID 145642).
The API returns an HTML fragment as a JSON string; we parse the Soybeans
table from that fragment.

Single location: Brewster, MN.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_API_URL    = "https://www.cihedging.com/cih/api/index.cfm/origination/cashbids/145642"
_COMPANY_ID = 145642

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept":       "application/json, */*",
    "Origin":       "https://mnsoy.com",
    "Referer":      "https://mnsoy.com/",
}

# CME month letter codes
_MONTH_CODES: dict[str, str] = {
    "Jan": "F", "Feb": "G", "Mar": "H", "Apr": "J",
    "May": "K", "Jun": "M", "Jul": "N", "Aug": "Q",
    "Sep": "U", "Oct": "V", "Nov": "X", "Dec": "Z",
}

# Regex to extract rows: delivery, futures label, futures price, change, basis.
# "Jul'26" uses a plain ASCII apostrophe.
_ROW_RE = re.compile(
    r'<span>([A-Z][a-z]+ \d{4})</span>'    # group 1: delivery  "Jun 2026"
    r'.*?'
    r"<span>([A-Z][a-z]+'\d{2})</span>"     # group 2: futures   "Jul'26"
    r'.*?'
    r'cashbid_price">([\d.]+)</td>'          # group 3: futures price
    r'.*?'
    r'cashbid_price">([-+]?[\d.]+)</td>'    # group 4: change
    r'.*?'
    r'cashbid_price">([-+]?[\d.]+)</td>',   # group 5: basis (dollars)
    re.DOTALL,
)


def _futures_label_to_cme(label: str, grain_root: str) -> str | None:
    """
    Convert widget label like "Jul'26" to CME symbol like "ZSN26".
    grain_root: "ZS" for Soybeans.
    """
    m = re.match(r"([A-Z][a-z]+)'(\d{2})$", label)
    if not m:
        return None
    month_abbr, yr = m.group(1), m.group(2)
    code = _MONTH_CODES.get(month_abbr)
    if not code:
        return None
    return f"{grain_root}{code}{yr}"


def _parse_grain_section(section_html: str, grain_root: str) -> list[dict]:
    """Parse one grain section's HTML into a list of bid dicts."""
    rows: list[dict] = []
    seen: set[str] = set()

    for m in _ROW_RE.finditer(section_html):
        delivery    = m.group(1).strip()   # "Jun 2026"
        fut_label   = m.group(2).strip()   # "Jul'26"
        # The change cell uses class "cashbid_price numberNegative/Positive" so
        # cashbid_price"> only matches futures price (g3) and basis (g4).
        basis_usd   = float(m.group(4))    # e.g. -0.10

        cme_sym = _futures_label_to_cme(fut_label, grain_root)
        if not cme_sym:
            log.debug("MNSP: unrecognised futures label %r", fut_label)
            continue

        key = f"{delivery}|{cme_sym}"
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "delivery":    delivery,
            "cme_symbol":  cme_sym,
            "basis_cents": int(round(basis_usd * 100)),
        })

    return rows


def fetch_mnsoy_bids() -> list[dict]:
    """
    Fetch MNSP soybean bids.

    Returns a list with one entry (single location):
        {
            "location":  "Brewster",
            "timestamp": str,   # ISO-8601 UTC date-normalised
            "bids":      [{"delivery", "cme_symbol", "basis_cents"}],
        }
    Returns an empty list on fetch/parse failure.
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    try:
        resp = requests.post(_API_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        html: str = resp.json()   # API returns a JSON-encoded HTML string
    except Exception as exc:
        log.error("MNSP: fetch failed: %s", exc)
        return []

    # Split by grain-section headers; the h2 text may have surrounding whitespace.
    # Each part after split starts with stripped grain name content.
    parts = re.split(r'<h2[^>]*>', html)
    soy_html = ""
    for part in parts:
        if re.match(r'\s*Soybeans\s*</h2>', part):
            soy_html = part
            break
        # Also catch if "Soybeans" appears in first 40 chars (whitespace-padded)
        if "Soybeans" in part[:40] and "Meal" not in part[:40] and "Pellet" not in part[:40]:
            soy_html = part
            break

    if not soy_html:
        log.warning("MNSP: Soybeans section not found in response")
        return []

    bids = _parse_grain_section(soy_html, grain_root="ZS")
    if not bids:
        log.warning("MNSP: no soybean bids parsed")
        return []

    log.info("MNSP Brewster  %d soybean bid(s)", len(bids))
    return [{"location": "Brewster", "timestamp": today_ts, "bids": bids}]


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
    from parsers.mnsoy_parser import parse_mnsoy_location

    locs = fetch_mnsoy_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_mnsoy_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.deliveryMonth:22s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
