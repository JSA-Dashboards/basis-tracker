"""
zfs_scraper.py — Zeeland Farm Services (ZFS) soybean bid scraper.

ZFS uses a DTN-powered website with per-request obfuscated numbers.
The HTML embeds a "NoScrapeOffset" in a JavaScript comment; all raw
values in the table are offset by that amount.  actual = raw - offset.

Locations scraped:
  • Zeeland      (theLocation=1)
  • ZFS Ithaca   (theLocation=3)
  Paw Paw and Riga are intentionally excluded per user configuration.

Usage:
    python zfs_scraper.py
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_BASE_URL  = "http://dtn.zfsinc.com/index.cfm"
_LOCATIONS = {
    1: "Zeeland",
    3: "ZFS Ithaca",
}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
REQUEST_DELAY = 0.5

# DTN single-char grain code → CME 2-char root
_DTN_GRAIN: dict[str, str] = {
    "S": "ZS",   # Soybeans
    "C": "ZC",   # Corn
    "W": "ZW",   # Wheat
}

_ROW_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December|New Crop)"  # group 1: month name
    r"\s+(\d{4})"                                      # group 2: year
    r".*?"
    r"@([A-Z]\d[A-Z])"                                # group 3: DTN symbol
    r".*?"
    r"displayNumber\(([-\d.]+),2\)",                   # group 4: raw basis
    re.DOTALL,
)


def _dtn_to_cme(dtn: str) -> str | None:
    """
    Convert 3-char DTN grain-futures code to 5-char CME symbol.
    "S6N" → "ZSN26",  "S6X" → "ZSX26",  "S7F" → "ZSF27"
    """
    if len(dtn) != 3:
        return None
    grain_ch, yr_digit, month_code = dtn[0], dtn[1], dtn[2]
    cme_root = _DTN_GRAIN.get(grain_ch)
    if not cme_root or not yr_digit.isdigit():
        return None
    return f"{cme_root}{month_code}2{yr_digit}"


def _parse_page(html: str, loc_name: str) -> list[dict]:
    """
    Return list of {"delivery", "cme_symbol", "basis_cents"} dicts.
    Returns [] on any parse failure.
    """
    offset_m = re.search(r"NoScrapeOffset:\s*([-+]?\d+\.\d+)", html)
    if not offset_m:
        log.warning("  ZFS %s: NoScrapeOffset not found — page structure may have changed", loc_name)
        return []
    offset = float(offset_m.group(1))

    rows = []
    for m in _ROW_RE.finditer(html):
        month_name = m.group(1).strip()
        year       = m.group(2).strip()
        dtn_code   = m.group(3)
        raw_basis  = float(m.group(4))

        delivery    = f"{month_name} {year}"
        cme_sym     = _dtn_to_cme(dtn_code)
        basis_cents = int(round((raw_basis - offset) * 100))

        if cme_sym:
            rows.append({
                "delivery":    delivery,
                "cme_symbol":  cme_sym,
                "basis_cents": basis_cents,
            })

    return rows


def fetch_zfs_bids() -> list[dict]:
    """
    Fetch ZFS soybean bids for Zeeland and ZFS Ithaca.

    Returns a list of per-location dicts:
        {
            "location":  str,
            "timestamp": str,   # ISO-8601 UTC date-normalised
            "bids":      [{"delivery", "cme_symbol", "basis_cents"}],
        }
    Locations with no parseable bids are omitted.
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    results: list[dict] = []

    session = requests.Session()
    session.headers.update(_HEADERS)

    for loc_id, loc_name in _LOCATIONS.items():
        url = f"{_BASE_URL}?show=11&mid=4&theLocation={loc_id}&layout=19"
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("  ZFS GET failed for %s: %s", loc_name, exc)
            time.sleep(REQUEST_DELAY)
            continue

        bids = _parse_page(resp.text, loc_name)
        if not bids:
            log.warning("  ZFS %s: no bids parsed", loc_name)
            time.sleep(REQUEST_DELAY)
            continue

        results.append({
            "location":  loc_name,
            "timestamp": today_ts,
            "bids":      bids,
        })
        log.info("  ZFS %-20s  %d bid(s)", loc_name, len(bids))
        time.sleep(REQUEST_DELAY)

    log.info("ZFS scrape complete: %d location(s)", len(results))
    return results


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
    from parsers.zfs_parser import parse_zfs_location

    locs = fetch_zfs_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_zfs_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.deliveryMonth:22s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
