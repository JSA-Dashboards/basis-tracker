"""
CHS Illinois cash bid scraper.

Uses a direct httpx POST to the Bushel aggregator API — no browser/Playwright
needed.  The single magic header is 'app-company: chs'.

The API returns all ~183 CHS locations nationwide; we filter client-side to the
25 Illinois locations listed on chs-illinois.com/grain/cash-bids/.

The Illinois location UUIDs below are stable GUIDs assigned to specific grain
elevators.  If CHS Illinois adds a new elevator, run the one-liner at the bottom
of this file to refresh the list.

Usage (standalone test):
    python chs_scraper.py
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

BIDS_URL = "https://api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList"

_HEADERS = {
    "app-company":  "chs",
    "content-type": "application/json",
    "referer":      "https://www.chs-illinois.com/grain/cash-bids/",
    "user-agent":   (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ── Illinois location UUIDs ────────────────────────────────────────────────────
# Sourced from the Bushel API response as filtered by chs-illinois.com.
# To refresh: python chs_scraper.py --list-ids
CHS_ILLINOIS_IDS: set[str] = {
    "a12278a9-e243-4b2e-bd01-1f162413de19",  # ADM Cedar Rapids
    "43b9a5fa-7b7c-41f9-9f72-5ff68bd878a4",  # ADM Clinton
    "a0256892-a004-4f5b-94d8-e46d3d0e8204",  # Annawan
    "c7d7c0c9-c4fa-4978-9d5b-46a9b3e9f64e",  # CHS Cahokia
    "436226d7-bd02-4e97-aa4a-ff670e0b719d",  # Carrollton/White Hall
    "49419bcb-f3f2-4382-b549-ec8a9b87177a",  # Darien
    "cd751c5a-6f29-4ced-b62e-36550307fd99",  # Davenport
    "49162b99-8080-4170-ab71-4164d4e8bb28",  # Futures-Only
    "4adb8921-8134-42ed-b3bb-6f1d2d0b23c2",  # GPC Muscatine
    "c85b7389-9aeb-4dc0-83b4-9934f576063b",  # Havana/Beardstown
    "1af786d2-9708-4475-a5e4-065081f6bc78",  # Ingredion
    "e67ba8c2-d421-42e5-a3ea-b76db07d6d98",  # Joliet Container Mkt
    "0b3adcef-66ed-4b70-95e9-71e02235c934",  # Lowder
    "cc7dde51-b7f5-4f43-baf2-28cdbf8c49ca",  # Malta
    "0a45fdc0-62ac-45be-a9e8-3510c1561ea0",  # Meredith Rd Elburn
    "8df590fd-be6a-4bef-8c61-e2684c7e2663",  # Morris
    "2e713a1d-5c19-4d4f-bfef-cfa0e4454c21",  # Nestle Davenport
    "49e50433-44c6-4612-8f57-6126ab9fae4c",  # Newark
    "98791c4f-9722-48e4-99cc-9bdc0a0a18dc",  # Ottawa River Mkt
    "b43bcddc-9fab-4f45-97c6-7b89ffa2fbc8",  # Peru Marketstreet
    "bba4240b-02c6-4c07-b10c-d3aa1adb3244",  # Rochelle
    "492bb16e-2c43-4c3d-8f84-01ce728a9a65",  # Rt. 47 Terminal
    "72f972ff-c9ad-4682-a42b-a69ac090aaca",  # Seneca
    "77fb8afc-b44f-4837-88b9-a0cdf8661f90",  # Shipman
    "8a7b86d1-6621-4286-99b6-2a541d81180a",  # Steward
}


def fetch_chs_bids() -> dict:
    """
    Fetch raw bid data from the Bushel API for all CHS Illinois locations.

    Returns:
        The parsed JSON response dict from GetBidsList, already filtered to the
        25 Illinois locations (locations not in CHS_ILLINOIS_IDS are stripped).
        Returns an empty dict on failure.
    """
    try:
        resp = httpx.post(
            BIDS_URL,
            headers=_HEADERS,
            json={},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("CHS Bushel API request failed: %s", exc)
        return {}

    all_locs = data.get("locations", [])
    log.info("CHS API: %d total locations", len(all_locs))
    return data


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, json, argparse
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--list-ids", action="store_true",
                        help="Print all Illinois location IDs (for refreshing the list)")
    args = parser.parse_args()

    if args.list_ids:
        # Reload the page, capture fresh IDs
        from playwright.sync_api import sync_playwright
        import time

        captured = {}
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()

            def _on_resp(resp):
                if "GetBidsList" in resp.url:
                    try:
                        captured["data"] = resp.json()
                    except Exception:
                        pass

            page.on("response", _on_resp)
            page.goto("https://www.chs-illinois.com/grain/cash-bids/",
                      timeout=30000, wait_until="networkidle")
            time.sleep(2)
            browser.close()

        if "data" in captured:
            locs = captured["data"].get("locations", [])
            print(f"# {len(locs)} Illinois locations captured from page")
            print("CHS_ILLINOIS_IDS: set[str] = {")
            for l in locs:
                print(f'    "{l["id"]}",  # {l["name"]}')
            print("}")
        else:
            print("No data captured.")
        sys.exit(0)

    # Normal test: fetch and display
    sys.path.insert(0, str(Path(__file__).parent))
    from parsers.chs_parser import parse_bids_response

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    raw = fetch_chs_bids()

    if not raw:
        print("No data fetched.")
        sys.exit(1)

    snaps = parse_bids_response(raw, CHS_ILLINOIS_IDS, timestamp)
    print(f"\n{'='*60}")
    print(f"CHS Illinois: {len(snaps)} snapshot(s) parsed")
    print(f"{'='*60}")
    for s in snaps:
        row_summary = "  ".join(
            f"{r.deliveryMonth} {r.futuresSymbol} "
            f"{'+' if (r.basisCents or 0) >= 0 else ''}{r.basisCents}c"
            for r in s.rows[:3]
        )
        print(f"  {s.location:30s}  {s.rows[0].grain:10s}  "
              f"{len(s.rows):2d} row(s)  {row_summary}{'...' if len(s.rows)>3 else ''}")
