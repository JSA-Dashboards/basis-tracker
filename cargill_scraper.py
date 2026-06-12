"""
Cargill cash-bid scraper.

Fetches bid data from the Barchart WebSol API that powers cargillag.com for
all ~81 Cargill locations defined in cargill_locations.json.

Unlike cargillag.com itself (which is protected by Akamai), the underlying
Barchart API at cargillus.websol.barchart.com is publicly accessible with a
standard browser User-Agent.

Usage (standalone test):
    python cargill_scraper.py

Returns a list of location dicts ready for parsers/cargill_parser.py.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ── Endpoint ──────────────────────────────────────────────────────────────────
_BARCHART_URL   = "https://cargillus.websol.barchart.com/"
_LOCATIONS_JSON = Path(__file__).parent / "cargill_locations.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cargillag.com/",
    "Accept":  "application/json, */*",
}

REQUEST_DELAY = 0.25  # seconds between location requests


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_locations() -> dict:
    """Load the cargill_locations.json static map."""
    with open(_LOCATIONS_JSON, encoding="utf-8") as fh:
        return json.load(fh)


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_cargill_bids(slugs: list[str] | None = None) -> list[dict]:
    """
    Fetch Cargill cash bids from Barchart WebSol for all locations in
    cargill_locations.json (or a subset if ``slugs`` is given).

    Each returned dict has the shape:
        {
            "slug":          str,           # JSON key, e.g. "beardstown-cah"
            "location_name": str,           # clean display name, e.g. "Beardstown"
            "state":         str,           # from Barchart API, e.g. "IL"
            "city":          str,
            "timestamp":     str,           # ISO-8601 UTC
            "cashbids": [
                {
                    "grain":          str,  # raw commodity name, e.g. "Yellow Corn"
                    "barchart_symbol":str,  # Barchart format, e.g. "CN6"
                    "basis_str":      str,  # string USD/bu, e.g. "-0.0200"
                    "delivery_start": str,  # "MM/DD/YYYY"
                    "delivery_end":   str,  # "MM/DD/YYYY"
                },
                ...
            ],
        }

    Locations with no bids are still returned (with empty cashbids list) so
    callers can log the count correctly; the parser returns None for them.
    """
    all_locs = _load_locations()
    if slugs is not None:
        all_locs = {k: v for k, v in all_locs.items() if k in slugs}

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    session   = requests.Session()
    session.headers.update(_HEADERS)

    results: list[dict] = []

    for slug, meta in all_locs.items():
        barchart_id  = meta["barchart_id"]
        display_name = meta["display_name"]
        comm_roots   = meta.get("comm_roots") or ["ZC"]

        # Build comma-separated commRoots param (leading comma is required by API)
        comm_roots_param = "," + ",".join(comm_roots)

        params = {
            "module":               "cashbids",
            "location":             barchart_id,
            "output":               "json",
            "commOverviewByLocation": "1",
            "commRoots":            comm_roots_param,
        }

        try:
            r = session.get(_BARCHART_URL, params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.error(
                "Cargill fetch failed for %s (id=%s): %s",
                slug, barchart_id, exc,
            )
            time.sleep(REQUEST_DELAY)
            continue

        # ── Extract location metadata from API response ────────────────────
        loc_details = (
            data.get("locationDetails", {}).get(str(barchart_id), {}) or {}
        )
        state = loc_details.get("state") or ""
        city  = loc_details.get("city")  or ""

        # ── Extract cashbids from bigGroups ───────────────────────────────
        cashbids: list[dict] = []
        for bg in data.get("bigGroups") or []:
            grain_raw = bg.get("name") or ""
            for bid in bg.get("cashbids") or []:
                basis_str    = bid.get("basis")
                barchart_sym = bid.get("futuresymbol") or ""
                del_start    = bid.get("delivery_start") or ""
                del_end      = bid.get("delivery_end")   or ""

                if basis_str is None or not barchart_sym:
                    continue

                cashbids.append({
                    "grain":          grain_raw,
                    "barchart_symbol": barchart_sym,
                    "basis_str":      str(basis_str),
                    "delivery_start": del_start,
                    "delivery_end":   del_end,
                })

        results.append({
            "slug":          slug,
            "location_name": display_name,
            "state":         state,
            "city":          city,
            "timestamp":     today_utc,
            "cashbids":      cashbids,
        })

        log.info(
            "  Cargill %-40s  %-2s  %d bid(s)",
            display_name, state, len(cashbids),
        )
        time.sleep(REQUEST_DELAY)

    log.info("Cargill scrape complete: %d location(s) returned", len(results))
    return results


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    sys.path.insert(0, str(Path(__file__).parent))
    from parsers.cargill_parser import parse_cargill_location

    locs = fetch_cargill_bids()
    print(f"\n{'='*70}")
    print(f"Total locations fetched: {len(locs)}")
    print(f"{'='*70}")

    total_rows = 0
    no_bids    = 0
    for loc in sorted(locs, key=lambda x: x["location_name"]):
        snap = parse_cargill_location(loc)
        if snap:
            grains = sorted({r.grain for r in snap.rows})
            print(
                f"  {snap.location:42s}  {loc['state']:2s}  "
                f"{len(snap.rows):3d} row(s)  [{', '.join(grains)}]"
            )
            total_rows += len(snap.rows)
        else:
            no_bids += 1
            print(f"  {loc['location_name']:42s}  {loc['state']:2s}  (no valid bids)")

    print(f"\nTotal bid rows : {total_rows}")
    print(f"Locations w/ bids : {len(locs) - no_bids} / {len(locs)}")
