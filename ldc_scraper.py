"""
ldc_scraper.py — Scrape LDC (Louis Dreyfus Company) cash bids.

Uses the public (no-auth) JSON API at myldc.com.

Endpoints:
  GET /api/general/facility/public
      → list of all public facilities [{name, shortcut, facilityId, pageType}]
  GET /api/general/facility/client-cashbids/{facilityId}
      → cashbid data for one facility

Commodities:
  Yellow Corn / Corn     → "Corn"       (basis in $/bu → cents)
  Yellow Soybeans / Soybeans → "Soybeans" (basis in $/bu → cents)
  Soft Red Wheat         → "SRW Wheat"  (basis in $/bu → cents)
  Canola / Nexera        → skipped (Canadian crops, no US CME reference)

Symbol conversion: DTN "@CN26" → CME "ZCN26" (strip @, prepend Z).
Canadian populations (isCanadaPopulation=true) are excluded.

Usage (standalone test):
    python ldc_scraper.py
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
import requests

log = logging.getLogger(__name__)

_BASE        = "https://www.myldc.com/api/general/facility"
_FACILITIES  = f"{_BASE}/public?shortcut=grandjunction"
_CASHBIDS    = f"{_BASE}/client-cashbids"
_HEADERS     = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.myldc.com/facility/grandjunction",
}
REQUEST_DELAY = 0.30

# State codes keyed by shortcut slug
# Populated from public-information addresses; update if new facilities are added.
_FACILITY_STATE: dict[str, str] = {
    "burnsharbor":   "IN",
    "cahokia":       "IL",
    "claypool":      "IN",
    "grandjunction": "IA",
    "portallen":     "LA",
    "rosedale":      "MS",
    "uppersandusky": "OH",
    "westmemphis":   "AR",
}


def fetch_ldc_bids() -> list[dict]:
    """
    Fetch LDC cash bids for all US public facilities.

    Returns a list of per-location dicts:
        {
            "location_name": str,   # e.g. "Grand Junction"
            "shortcut":      str,   # e.g. "grandjunction"
            "facility_id":   str,   # UUID
            "state":         str,   # 2-letter state code (empty for unknown)
            "timestamp":     str,   # ISO-8601 UTC date-normalised
            "cashbids": [
                {
                    "commodity": str,   # "Yellow Corn", "Soybeans", "Soft Red Wheat", ...
                    "symbol":    str,   # DTN format, e.g. "@CN26"
                    "bid_type":  str,   # "Basis" | "FlatPrice"
                    "label":     str,   # delivery label, e.g. "FH JUNE", "DEC '26"
                    "basis":     float, # basis in $/bu (negative = discount)
                },
                ...
            ],
        }

    Locations with no bid rows (e.g. Canadian facilities) are excluded.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    session   = requests.Session()
    session.headers.update(_HEADERS)

    # ── Step 1: get facility list ─────────────────────────────────────────────
    try:
        resp = session.get(_FACILITIES, timeout=15)
        resp.raise_for_status()
        facilities: list[dict] = resp.json()
    except Exception as exc:
        log.error("LDC facility list fetch failed: %s", exc)
        return []

    log.info("LDC: found %d public facilities", len(facilities))

    # ── Step 2: fetch bids per facility ──────────────────────────────────────
    results: list[dict] = []
    for fac in facilities:
        shortcut    = fac.get("shortcut", "")
        fac_name    = fac.get("name", shortcut)
        facility_id = fac.get("facilityId", "")

        if not facility_id:
            continue

        url = f"{_CASHBIDS}/{facility_id}"
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            populations: list[dict] = r.json()
        except Exception as exc:
            log.warning("  LDC GET bids failed for %s: %s", shortcut, exc)
            time.sleep(REQUEST_DELAY)
            continue

        cashbids: list[dict] = []
        is_canada = False
        for pop in populations:
            if pop.get("isCanadaPopulation"):
                is_canada = True
                continue
            for comm in pop.get("cashBidCommodities", []):
                commodity = comm.get("commodityName", "")
                for bid in comm.get("cashBids", []):
                    cashbids.append({
                        "commodity": commodity,
                        "symbol":    bid.get("commoditySymbol", ""),
                        "bid_type":  bid.get("cashBidType", ""),
                        "label":     bid.get("label", ""),
                        "basis":     bid.get("basis"),
                    })

        if is_canada and not cashbids:
            log.debug("  %s: Canadian facility — skipping", shortcut)
            time.sleep(REQUEST_DELAY)
            continue

        if not cashbids:
            log.debug("  %s: no bids", shortcut)
            time.sleep(REQUEST_DELAY)
            continue

        results.append({
            "location_name": fac_name,
            "shortcut":      shortcut,
            "facility_id":   facility_id,
            "state":         _FACILITY_STATE.get(shortcut, ""),
            "timestamp":     today_utc,
            "cashbids":      cashbids,
        })
        log.debug("  %-20s %d bids", fac_name, len(cashbids))
        time.sleep(REQUEST_DELAY)

    log.info(
        "LDC scrape complete: %d location(s), %d total bids",
        len(results), sum(len(loc["cashbids"]) for loc in results),
    )
    return results


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
    from parsers.ldc_parser import parse_ldc_location

    locs = fetch_ldc_bids()
    print("=" * 65)
    print(f"Total locations: {len(locs)}")
    print("=" * 65)
    total_rows = 0
    for loc in locs:
        snap = parse_ldc_location(loc)
        if snap:
            print(f"  {snap.location:42s} {loc['state']:2s}  {len(snap.rows):2d} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.grain:20s}  {r.deliveryMonth:22s}  {r.futuresSymbol:8s}  {sign}{r.basisCents}c")
            total_rows += len(snap.rows)
        else:
            print(f"  {loc['location_name']:42s} (no valid bids)")
    print(f"\nTotal rows: {total_rows}")
