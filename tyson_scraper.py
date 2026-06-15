"""
Tyson Foods grain bid scraper — Local Grain Services (LGS).

Fetches all Tyson grain buying locations from the LGS public API.
6 Elevators post public bids; Feed Mills are visible but bids require portal login.

Returns a list of per-location dicts with keys:
    location_name, location_type, city, state, lat, lon, timestamp, cashbids
"""
import json
import logging
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_API_URL = "https://www.localgrainservices.com/api/lgs/locations"


def fetch_tyson_bids() -> list[dict]:
    """
    Fetch all Tyson LGS locations and their public bids.

    FeedMill locations are included in the returned list but have empty cashbids
    (their bids require portal.bushelpowered.com login).

    Returns:
        List of dicts: {location_name, location_type, city, state,
                        lat, lon, timestamp, cashbids}
    """
    now_ts = datetime.now(timezone.utc).isoformat()

    req = urllib.request.Request(
        _API_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json, */*",
            "Referer": "https://www.localgrainservices.com/locations-and-pricing",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    # API returns {"locations": [...], "#cache": ...}
    entries = data["locations"] if isinstance(data, dict) else data

    results = []
    for entry in entries:
        loc      = entry.get("Location") or entry
        name     = (loc.get("Name") or "").strip()
        loc_type = loc.get("Type") or ""
        city     = loc.get("City") or None
        state    = loc.get("State") or None
        lat      = loc.get("Latitude")
        lon      = loc.get("Longitude")

        # BidRows is a list of lists (one inner list per product, each with delivery rows)
        bid_row_groups = entry.get("BidRows") or []
        cashbids = []
        for row_group in bid_row_groups:
            if not isinstance(row_group, list):
                continue
            for bid in row_group:
                if not isinstance(bid, dict):
                    continue
                price = bid.get("Price")
                if not price:
                    continue
                cashbids.append({
                    "product":         bid.get("Product", ""),
                    "delivery_period": bid.get("DeliveryPeriod", ""),
                    "futures_month":   bid.get("FuturesMonth", ""),
                    "price":           price,
                })

        results.append({
            "location_name": name,
            "location_type": loc_type,
            "city":          city,
            "state":         state,
            "lat":           lat,
            "lon":           lon,
            "timestamp":     now_ts,
            "cashbids":      cashbids,
        })

    log.info(
        "Tyson LGS: fetched %d locations (%d with public bids)",
        len(results),
        sum(1 for r in results if r["cashbids"]),
    )
    return results
