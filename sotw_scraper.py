"""
Star of the West cash-bid scraper.

starofthewest.com embeds an AgriCharts (Cargill-powered) cash-bids widget on each
location page; the same widget exposes an all-locations feed at
    https://sotw.agricharts.com/inc/cashbids/cashbids-js.php?filter=all
which returns `var bids = [ {location …, cashbids:[…]}, … ];`. One GET returns every
active location and its bids (same shape CGB uses), so no per-location requests.

Grains: Corn, Soybeans, Red Wheat, White Wheat (both → Wheat), plus a Food-Grade
soybean placeholder (skipped — its basis is a non-price "premium not included").
Basis is already in whole cents; symbols are CME format ("ZCN26").
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

_URL = (
    "https://sotw.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=all&location=&commodity=&groupby=location&showtimestamp=1&format=table"
    "&fields=name,delivery_start,delivery_end,basismonth,futures,futureschange,basis,price"
    "&dateformat=%m/%d/%Y&months=11"
)
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/javascript, */*",
    "Referer": "https://www.starofthewest.com/",
}
_BIDS_RE = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)


def _to_cents(val) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return None


def fetch_sotw_bids() -> list[dict]:
    """Return per-location dicts (CGB-compatible shape) for parsers.sotw_parser:
    {location_id, location_name, city, state, facility_type, timestamp, cashbids:[…]}."""
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Star of the West fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.warning("Star of the West: no 'var bids' array in response")
        return []
    try:
        locations = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("Star of the West: bad JSON: %s", exc)
        return []

    today = datetime.now(timezone.utc).date().isoformat()
    out: list[dict] = []
    for loc in locations:
        cashbids_raw = loc.get("cashbids") or []
        if not cashbids_raw:
            continue
        bids = []
        for b in cashbids_raw:
            name   = (b.get("name") or "").strip()
            symbol = (b.get("symbol") or "").strip()
            cents  = _to_cents(b.get("basis"))
            if not symbol or cents is None:
                continue
            if "not included" in name.lower():     # food-grade placeholder, no real price
                continue
            bids.append({
                "bid_id":         str(b.get("id") or ""),
                "grain":          name,
                "symbol":         symbol,
                "basis":          cents,
                "delivery_start": b.get("delivery_start") or "",
                "delivery_end":   b.get("delivery_end") or "",
            })
        if not bids:
            continue
        out.append({
            "location_id":   str(loc.get("id") or ""),
            "location_name": (loc.get("name") or "").strip(),
            "city":          (loc.get("city") or "").strip(),
            "state":         (loc.get("state") or "").strip(),
            "facility_type": "Country Elevator",
            "timestamp":     today,
            "cashbids":      bids,
        })

    log.info("Star of the West: %d location(s) with bids", len(out))
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    data = fetch_sotw_bids()
    print(f"{len(data)} locations")
    for loc in data:
        print(f"\n{loc['location_name']}  ({loc['city']}, {loc['state']})  {len(loc['cashbids'])} bids")
        for b in loc["cashbids"]:
            print(f"   {b['grain']:14} {b['symbol']:8} {b['basis']:+4d}c  {b['delivery_start']}")
