"""
Green Plains Inc. (GPRE) corn-bid scraper.

Fetches all location bids in a single API call to the DTN Content Services API
that powers the cash-bids widget on https://gpreinc.com/corn-bids/.

API details:
  base:    https://api.dtn.com
  path:    /markets/sites/{SITE_ID}/cash-bids
  auth:    ?apikey={API_KEY}   (query-string, lowercase 'k')
  site ID: E0383501
  api key: um6DekZfGnSA4srbtOJR9Y5MmOhhDTPB

Returns all 8 GPRE ethanol plant locations (corn-only).

Usage (standalone test):
    python gpre_scraper.py
"""
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_DTN_URL = (
    "https://api.dtn.com/markets/sites/E0383501/cash-bids"
    "?apikey=um6DekZfGnSA4srbtOJR9Y5MmOhhDTPB"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gpreinc.com/",
    "Accept":  "application/json",
}


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_gpre_bids() -> list[dict]:
    """
    Fetch GPRE corn bids from the DTN API.

    Returns a list of per-location dicts:
        {
            "location_id":   int,
            "location_name": str,          # e.g. "Central City"
            "timestamp":     str,          # ISO-8601 UTC date-normalised
            "cashbids": [
                {
                    "dtn_symbol":     str,  # DTN format e.g. "@C6N"
                    "basis":          float,# USD/bu e.g. -0.19
                    "cash_price":     float,
                    "delivery_start": str,  # ISO-8601
                    "delivery_end":   str,
                },
                ...
            ],
        }

    Locations with no bids are excluded.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    try:
        r = requests.get(_DTN_URL, headers=_HEADERS, timeout=25)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.error("GPRE fetch failed: %s", exc)
        return []

    if not isinstance(data, list):
        log.error("GPRE API returned unexpected format: %s", type(data))
        return []

    # Group flat list → per-location dicts
    by_loc: dict[int, dict] = {}

    for item in data:
        loc      = item.get("location") or {}
        loc_id   = loc.get("id")
        loc_name = loc.get("name", "").strip()
        if not loc_id or not loc_name:
            continue

        if loc_id not in by_loc:
            by_loc[loc_id] = {
                "location_id":   loc_id,
                "location_name": loc_name,
                "timestamp":     today_utc,
                "cashbids":      [],
            }

        basis      = item.get("basisPrice")
        cash_price = item.get("cashPrice")
        dtn_sym    = item.get("symbol", "") or ""
        dp         = item.get("deliveryPeriod") or {}
        del_start  = dp.get("start", "") or ""
        del_end    = dp.get("end",   "") or ""

        if basis is None or not dtn_sym:
            continue

        by_loc[loc_id]["cashbids"].append({
            "dtn_symbol":     dtn_sym,
            "basis":          float(basis),
            "cash_price":     float(cash_price) if cash_price is not None else None,
            "delivery_start": del_start,
            "delivery_end":   del_end,
        })

    results = sorted(by_loc.values(), key=lambda x: x["location_name"])
    log.info("GPRE scrape complete: %d location(s), %d total bids",
             len(results), sum(len(l["cashbids"]) for l in results))
    return results


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
    from parsers.gpre_parser import parse_gpre_location

    locs = fetch_gpre_bids()
    print(f"\n{'='*60}")
    print(f"Total locations: {len(locs)}")
    print(f"{'='*60}")
    total_rows = 0
    for loc in locs:
        snap = parse_gpre_location(loc)
        if snap:
            print(f"  {snap.location:20s}  {len(snap.rows):2d} row(s)")
            for r in snap.rows:
                print(f"    {r.deliveryMonth:10s} {r.futuresSymbol:8s} {r.basisCents:+d}c")
            total_rows += len(snap.rows)
        else:
            print(f"  {loc['location_name']:20s}  (no valid bids)")
    print(f"\nTotal rows: {total_rows}")
