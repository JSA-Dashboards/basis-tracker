"""
primient_scraper.py — Primient grain origination bid scraper.

Uses the Agricharts customer-level API (customer ID 2175) to pull all
Primient locations in a single request.  Returns Corn and Soybean bids.

All 17 Primient locations are fetched in one call:
  Decatur IL, Lafayette IN, Loudon TN, Coles IL, Cowden IL, Darrow IL,
  Findlay IL, Fowler IN, Francesville IN, Heyworth IL, LeRoy IL, Mattoon IL,
  Parnell IL, Pittwood IL, Wapella IL, Watseka IL, West FOB IL.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_API_URL = (
    "https://tateandlylegrain.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=customer&customer=2175&commodity=&groupby=ccommodity"
    "&format=json&fields=name,delivery_start,delivery_end,price"
    ",basismonth,futures,futureschange,basis&months=8"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://primientgrain.com/",
}

_BIDS_RE = re.compile(r"var bids = (\[.*?\]);", re.DOTALL)

_MONTH_NAMES = {
    "01": "January",  "02": "February", "03": "March",    "04": "April",
    "05": "May",      "06": "June",     "07": "July",     "08": "August",
    "09": "September","10": "October",  "11": "November", "12": "December",
}

_GRAIN_MAP = {
    "Corn":     "Corn",
    "Soybeans": "Soybeans",
    "Wheat":    "Wheat",
}


def _delivery_label(delivery_start: str) -> str:
    """Convert 'MM/YYYY' to 'Month YYYY'."""
    parts = delivery_start.split("/")
    if len(parts) == 2:
        mn = _MONTH_NAMES.get(parts[0].zfill(2), parts[0])
        return f"{mn} {parts[1]}"
    return delivery_start


def fetch_primient_bids() -> list[dict]:
    """
    Fetch all Primient cash bids via the Agricharts customer API.

    Returns a list of per-location dicts:
        {
            "location":  str,
            "timestamp": str,   # ISO-8601 UTC date-normalised
            "bids": [
                {
                    "bid_id":      str,   # unique Agricharts bid ID
                    "grain":       str,
                    "delivery":    str,   # "June 2026"
                    "cme_symbol":  str,   # "ZCN26"
                    "basis_cents": int,
                    "notes":       str,
                }
            ],
        }
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    try:
        resp = requests.get(_API_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Primient: fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.error("Primient: could not find 'var bids' in response")
        return []

    try:
        raw_bids = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("Primient: JSON parse error: %s", exc)
        return []

    # Aggregate by location
    by_location: dict[str, list[dict]] = {}

    for commodity_block in raw_bids:
        grain_label = _GRAIN_MAP.get(commodity_block.get("name", ""), "")
        if not grain_label:
            continue

        for bid in commodity_block.get("cashbids", []):
            loc_name    = bid.get("location_name", "").strip()
            symbol      = bid.get("symbol", "").strip()
            basis       = bid.get("basis")
            delivery_s  = bid.get("delivery_start", "").strip()
            bid_id      = str(bid.get("id", ""))
            notes       = (bid.get("notes") or "").strip()

            if not loc_name or not symbol or basis is None or not delivery_s:
                continue

            if loc_name not in by_location:
                by_location[loc_name] = []

            by_location[loc_name].append({
                "bid_id":      bid_id,
                "grain":       grain_label,
                "delivery":    _delivery_label(delivery_s),
                "cme_symbol":  symbol,
                "basis_cents": int(basis),
                "notes":       notes,
            })

    results = []
    for loc_name, bids_list in by_location.items():
        results.append({
            "location":  loc_name,
            "timestamp": today_ts,
            "bids":      bids_list,
        })
        log.info("Primient %-20s  %d bid(s)", loc_name, len(bids_list))

    log.info("Primient scrape complete: %d location(s)", len(results))
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
    from parsers.primient_parser import parse_primient_location

    locs = fetch_primient_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_primient_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows[:4]:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.grain:10s}  {r.deliveryMonth:22s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
