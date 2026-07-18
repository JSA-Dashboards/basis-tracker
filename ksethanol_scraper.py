"""
Kansas Ethanol (Lyons, KS) cash-bid scraper.

Kansas Ethanol posts corn and milo (grain sorghum) bids through the same
agricharts.com widget platform that powers CGB — the JS endpoint returns a
`var bids = [...]` payload with per-location cashbids (CME symbol, integer-cent
basis, delivery window, basis month).  We reuse that exact approach here.

The plant is an ethanol/bio-fuel facility, so its milo bid is an *ethanol-plant*
sorghum bid — the thing we want to track separately from elevator/rail sorghum.
The agricharts feed labels it "Ethanol/Bio-Fuel"; we normalise that to the app's
"Corn Processing" facility_type so it lands in the ethanol UI grouping.

This module is written to support additional single-tenant agricharts ethanol
plants later: add an entry to ETHANOL_TENANTS.

Usage (standalone test):
    python ksethanol_scraper.py

Returns a list of location dicts ready for parsers/ksethanol_parser.py.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# ── Tenants ────────────────────────────────────────────────────────────────────
# provider     : app-facing provider name
# subdomain    : <subdomain>.agricharts.com
# location     : app-facing location label ("City, ST" convention)
# state        : two-letter state (feed usually supplies it, this is a fallback)
ETHANOL_TENANTS: list[dict] = [
    {"provider": "Kansas Ethanol", "subdomain": "ksethanol",
     "location": "Lyons, KS", "state": "KS"},
]

# agricharts JS endpoint — identical shape to the CGB feed.
_URL_TMPL = (
    "https://{sub}.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=all&location=&commodity="
    "&groupby=location&format=table"
    "&fields=name,delivery_start,delivery_end,basismonth,futures,futureschange,basis,price"
    "&bidsort=commodity&dateformat=%25m/%25d/%25Y&months=11"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/javascript, */*",
}

_BIDS_RE      = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)
REQUEST_DELAY = 0.5  # seconds between tenant requests

# agricharts facility labels → app facility_type
_FACILITY_NORM = {
    "ethanol/bio-fuel": "Corn Processing",
    "ethanol":          "Corn Processing",
    "bio-fuel":         "Corn Processing",
}


def _fetch_tenant(session: requests.Session, tenant: dict, today_utc: str) -> list[dict]:
    url = _URL_TMPL.format(sub=tenant["subdomain"])
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        log.error("Kansas Ethanol fetch failed for %s: %s", tenant["subdomain"], exc)
        return []

    m = _BIDS_RE.search(r.text)
    if not m:
        log.warning("Kansas Ethanol: no 'var bids' for %s (status %s)",
                    tenant["subdomain"], r.status_code)
        return []

    try:
        feed_locs = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("Kansas Ethanol JSON parse error for %s: %s", tenant["subdomain"], exc)
        return []

    out: list[dict] = []
    for loc in feed_locs:
        cashbids = loc.get("cashbids") or []
        if not cashbids:
            continue
        feed_fac = (loc.get("facility_type") or "").strip().lower()
        out.append({
            "provider":      tenant["provider"],
            "location_name": tenant["location"],
            "state":         loc.get("state") or tenant["state"],
            "city":          loc.get("city") or "",
            "facility_type": _FACILITY_NORM.get(feed_fac, "Corn Processing"),
            "timestamp":     today_utc,
            "cashbids": [
                {
                    "bid_id":         str(b.get("id") or ""),
                    "grain":          b.get("name") or "",
                    "symbol":         b.get("symbol") or "",
                    "basis":          int(b.get("basis") or 0),
                    "delivery_start": b.get("delivery_start") or "",
                    "delivery_end":   b.get("delivery_end") or "",
                    "basismonth":     b.get("basismonth") or "",
                }
                for b in cashbids
            ],
        })
    return out


def fetch_ksethanol_bids() -> list[dict]:
    """Scrape every agricharts ethanol tenant and return per-location dicts.

    Each dict matches the shape parsers/ksethanol_parser.parse_ksethanol_location
    expects: provider, location_name, state, city, facility_type, timestamp,
    cashbids[].  Locations with no bids are excluded.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    session   = requests.Session()
    session.headers.update(_HEADERS)

    results: list[dict] = []
    for tenant in ETHANOL_TENANTS:
        results.extend(_fetch_tenant(session, tenant, today_utc))
        time.sleep(REQUEST_DELAY)

    log.info("Kansas Ethanol scrape complete: %d location(s)", len(results))
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
    from parsers.ksethanol_parser import parse_ksethanol_location

    locs = fetch_ksethanol_bids()
    print(f"\n{'='*65}\nTotal locations: {len(locs)}\n{'='*65}")
    total = 0
    for loc in locs:
        snap = parse_ksethanol_location(loc)
        if snap:
            grains = sorted({r.grain for r in snap.rows})
            print(f"  {snap.location:20s} {loc['state']:2s} {loc['facility_type']:16s} "
                  f"{len(snap.rows):2d} row(s)  [{', '.join(grains)}]")
            total += len(snap.rows)
        else:
            print(f"  {loc['location_name']:20s}  (no valid bids)")
    print(f"\nTotal bid rows: {total}")
