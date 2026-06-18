"""
sdsp_scraper.py — South Dakota Soybean Processors (Volga, SD) bid scraper.

Data source: agricharts widget embedded on https://sdsbp.com/soybean-pricing/
  sdsbpjsi.agricharts.com/inc/cashbids/cashbids-js.php?filter=location&location=59689

Grains captured:
  - Soybeans          (GMO)
  - Non-GMO Soybeans  (certified grower premium)
  - Soybean Meal      (skipped — $/ton unit)

Basis is returned in cents (integer). Delivery labels built from raw ISO dates.

Usage (standalone test):
    python sdsp_scraper.py
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_URL = (
    "https://sdsbpjsi.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=location&location=59689&commodity="
    "&groupby=location&width=&showtimestamp=1&hidenav=1"
    "&format=table"
    "&fields=name,notes,price,basis,basismonth,futures,futureschange"
    "&groupheading=table&bidsort=commodity"
    "&dateformat=%25b+%25y&months=8&acCnt=1"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/javascript, */*",
    "Referer": "https://sdsbp.com/",
}

_BIDS_RE = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)

# Commodity names to import; anything else (Soybean Meal, etc.) is skipped
_GRAIN_MAP: dict[str, str] = {
    "Soybeans":                             "Soybeans",
    "Non-GMO Soybeans - Certified Grower":  "Non-GMO Soybeans",
}

_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March",    4: "April",
    5: "May",     6: "June",     7: "July",      8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def _delivery_label(start_raw: str, end_raw: str) -> str:
    """
    Build a human-readable delivery label from ISO date strings.
    '2026-06-01' / '2026-06-30' → 'June 2026'
    '2026-09-15' / '2026-10-31' → 'Sep-Oct 2026'
    """
    try:
        d_start = datetime.strptime(start_raw[:10], "%Y-%m-%d")
        d_end   = datetime.strptime(end_raw[:10],   "%Y-%m-%d")
        if d_start.month == d_end.month and d_start.year == d_end.year:
            return f"{_MONTH_NAMES[d_start.month]} {d_start.year}"
        start_abbr = _MONTH_NAMES[d_start.month][:3]
        end_abbr   = _MONTH_NAMES[d_end.month][:3]
        return f"{start_abbr}-{end_abbr} {d_start.year}"
    except (ValueError, KeyError):
        return f"{start_raw[:7]} - {end_raw[:7]}"


def fetch_sdsp_bids() -> list[dict]:
    """
    Fetch SDSP soybean bids from the agricharts API.

    Returns a list with one location dict for parse_sdsp_location:
        {"location": "Volga", "state": "SD", "timestamp": str, "bids": [...]}
    """
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.error("SDSP: fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.warning("SDSP: no 'var bids' in response")
        return []

    try:
        locations = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("SDSP: JSON parse error: %s", exc)
        return []

    if not locations:
        log.warning("SDSP: empty bids array")
        return []

    loc      = locations[0]
    cashbids = loc.get("cashbids") or []
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    bids: list[dict] = []
    seen: set[str]   = set()

    for cb in cashbids:
        raw_name    = (cb.get("name") or "").strip()
        grain       = _GRAIN_MAP.get(raw_name)
        if not grain:
            continue  # skip Soybean Meal and any unexpected types

        symbol      = (cb.get("symbol") or "").strip()
        basis_raw   = cb.get("basis")
        start_raw   = cb.get("delivery_start_raw", "")
        end_raw     = cb.get("delivery_end_raw",   "")
        bid_id      = str(cb.get("id", ""))

        if not symbol or basis_raw is None or not start_raw:
            continue

        key = bid_id or f"{grain}|{symbol}|{start_raw[:10]}"
        if key in seen:
            continue
        seen.add(key)

        bids.append({
            "bid_id":      bid_id,
            "grain":       grain,
            "delivery":    _delivery_label(start_raw, end_raw),
            "cme_symbol":  symbol,
            "basis_cents": round(float(basis_raw)),
        })

    log.info("SDSP Volga: %d bid(s)", len(bids))

    if not bids:
        return []

    return [{
        "location":  "Volga",
        "state":     "SD",
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
    from parsers.sdsp_parser import parse_sdsp_location

    locs = fetch_sdsp_bids()
    print("=" * 60)
    for loc in locs:
        snap = parse_sdsp_location(loc)
        if snap:
            print(f"  {snap.provider} / {snap.location}  ({len(snap.rows)} bids)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.grain:<32} {r.deliveryMonth:<22} {r.futuresSymbol:<8} {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']}: no valid bids")
