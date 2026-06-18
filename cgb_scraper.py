"""
CGB Grain cash-bid scraper.

Fetches bid data from the agricharts.com widget that powers cgbgrain.com.
Data is returned per commodity (one HTTP request each for Corn, Soybeans,
Wheat), then merged by location so the parser gets all grains per location.

Note: the cgbgrain.com server sends malformed HTTP headers that httpx rejects
with RemoteProtocolError (multiple Transfer-Encoding headers).  We use the
`requests` library which is more lenient about HTTP compliance.

Usage (standalone test):
    python cgb_scraper.py

Returns a list of location dicts ready for parsers/cgb_parser.py.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# ── Endpoint ──────────────────────────────────────────────────────────────────
# This is the exact URL the browser sends when viewing
#   https://www.cgbgrain.com/Market-Solutions/Cash-Bids?commodity=6086
# The JavaScript widget appends the page's query-string to its own base URL,
# which produces the unusual double-commodity / double-format params at the end.
_CGB_URL_TMPL = (
    "https://cgb.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=all&location=cgblocation&commodity="
    "&groupby=location&width=90%25&showtimestamp=1&showchart=1"
    "&format=table"
    "&fields=name,delivery_start,delivery_end,basismonth,futures,futureschange,basis,price"
    "&groupheading=table&bidsort=commodity"
    "&dateformat=%25m/%25d/%25Y&months=11"
    "&&acCnt=1&format=table&groupby=location&setLocation="
    "&commodity={comm}&commodity={comm}"
)

# Commodity IDs active on cgbgrain.com.
# 6087 (Soybeans by that name) returns 0 results; 6322 ("Beans") is the
# correct ID for CGB soybeans.
CGB_COMMODITY_IDS: list[str] = ["6086", "6322", "6088"]  # Corn, Soybeans, Wheat

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/javascript, */*",
    "Referer": "https://www.cgbgrain.com/",
}

_BIDS_RE  = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)
REQUEST_DELAY = 0.5  # seconds between commodity requests

# CGB's feed mislabels some plants. Force the correct facility_type (keyed by
# the cleaned location_name) so they land in the right UI category.
_FACILITY_OVERRIDES: dict[str, str] = {
    "CGB MT VERNON":   "Soy Processing",     # Mount Vernon, IN — soybean crush plant
    "CGB JOLIET":      "Container Terminal",  # Joliet, IL — container, not river terminal
    "CGB - Falls City": "Rail Terminal",      # Falls City, NE
    "CGB MT CARROLL":  "Country Elevator",   # Mt. Carroll, IL
}

# Some CGB locations have no state in the feed — supply it so region logic works.
_STATE_OVERRIDES: dict[str, str] = {
    "CGB SEMO MILLING": "MO",            # Southeast Missouri
    "CGB - Falls City": "NE",
    "CGB MT CARROLL":   "IL",
}

# CGB locations to drop entirely (FOB / direct-ship pricing points, not terminals).
_SKIP_LOCATIONS: set = {"CGB FULTON", "CGB QTI"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_name(raw: str) -> str:
    """Normalise 'CGB -  FOO BAR' → 'CGB - Foo Bar'."""
    name = re.sub(r"-\s{2,}", "- ", raw.strip())      # collapse extra spaces
    if name.upper().startswith("CGB - "):
        loc_part = name[6:].strip()
        name = "CGB - " + loc_part.title()
    return name


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_cgb_bids(commodity_ids: list[str] | None = None) -> list[dict]:
    """
    Scrape CGB Grain bids for the requested commodity IDs and return merged
    per-location dicts.

    Each returned dict has the shape:
        {
            "location_id":   str,
            "location_name": str,          # cleaned, e.g. "CGB - Friars Point"
            "state":         str,          # e.g. "MS"
            "city":          str,
            "facility_type": str,          # e.g. "River Terminal"
            "timestamp":     str,          # ISO-8601 UTC date-normalised
            "cashbids": [
                {
                    "bid_id":         str,
                    "grain":          str,  # "Corn", "Beans", "Wheat", …
                    "symbol":         str,  # CME symbol e.g. "ZCU26"
                    "basis":          int,  # basis in CENTS
                    "delivery_start": str,  # "MM/DD/YYYY"
                    "delivery_end":   str,  # "MM/DD/YYYY"
                    "basismonth":     str,  # "Sep 2026"
                },
                ...
            ],
        }

    Locations with no bids are excluded.
    """
    if commodity_ids is None:
        commodity_ids = CGB_COMMODITY_IDS

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    session   = requests.Session()
    session.headers.update(_HEADERS)

    # Merge across commodity fetches: location_id → location dict
    by_loc_id: dict[str, dict] = {}

    for comm_id in commodity_ids:
        url = _CGB_URL_TMPL.format(comm=comm_id)
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
        except Exception as exc:
            log.error("CGB fetch failed for commodity %s: %s", comm_id, exc)
            continue

        m = _BIDS_RE.search(r.text)
        if not m:
            log.warning(
                "CGB: no 'var bids' in response for commodity %s (status %s)",
                comm_id, r.status_code,
            )
            continue

        try:
            locations = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            log.error("CGB JSON parse error for commodity %s: %s", comm_id, exc)
            continue

        comm_bids = 0
        for loc in locations:
            loc_id   = str(loc.get("id") or "")
            raw_name = loc.get("name") or ""
            cashbids = loc.get("cashbids") or []

            if not loc_id or not cashbids:
                continue

            # First time seeing this location — initialise its entry
            if loc_id not in by_loc_id:
                _clean = _clean_name(raw_name)
                if _clean in _SKIP_LOCATIONS:
                    continue
                by_loc_id[loc_id] = {
                    "location_id":   loc_id,
                    "location_name": _clean,
                    "state":         _STATE_OVERRIDES.get(_clean, loc.get("state") or ""),
                    "city":          loc.get("city") or "",
                    "facility_type": _FACILITY_OVERRIDES.get(_clean, loc.get("facility_type") or ""),
                    "timestamp":     today_utc,
                    "cashbids":      [],
                }

            for bid in cashbids:
                by_loc_id[loc_id]["cashbids"].append({
                    "bid_id":         str(bid.get("id") or ""),
                    "grain":          bid.get("name") or "",
                    "symbol":         bid.get("symbol") or "",
                    "basis":          int(bid.get("basis") or 0),
                    "delivery_start": bid.get("delivery_start") or "",
                    "delivery_end":   bid.get("delivery_end") or "",
                    "basismonth":     bid.get("basismonth") or "",
                })
                comm_bids += 1

        log.info(
            "CGB commodity %s: %d location(s), %d bid(s) fetched",
            comm_id, len(locations), comm_bids,
        )
        time.sleep(REQUEST_DELAY)

    results = sorted(by_loc_id.values(), key=lambda x: x["location_name"])
    log.info("CGB scrape complete: %d location(s) merged", len(results))
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
    from parsers.cgb_parser import parse_cgb_location

    locs = fetch_cgb_bids()
    print(f"\n{'='*65}")
    print(f"Total locations: {len(locs)}")
    print(f"{'='*65}")

    total_rows = 0
    for loc in locs:
        snap = parse_cgb_location(loc)
        if snap:
            grains = sorted({r.grain for r in snap.rows})
            print(
                f"  {snap.location:45s}  {loc['state']:2s}  "
                f"{len(snap.rows):3d} row(s)  [{', '.join(grains)}]"
            )
            total_rows += len(snap.rows)
        else:
            print(f"  {loc['location_name']:45s}  (no valid bids)")

    print(f"\nTotal bid rows: {total_rows}")
