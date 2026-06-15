"""
hppsd_scraper.py — High Plains Processing (Mitchell, SD) soybean bid scraper.

Uses the Agricharts API at sdsbpjsi.agricharts.com.
Single location: Mitchell, SD.

Note: The Agricharts commodity block is named after the location ("Mitchell"),
not the grain type, so grain is inferred from the CME futures symbol root
(ZS = Soybeans, ZC = Corn, ZW = Wheat).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_API_URL = (
    "https://sdsbpjsi.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=location&location=86080&commodity=17045&groupby=location"
    "&format=json&fields=name,notes,price,basis,basismonth,futures"
    ",futureschange,delivery_start,delivery_end"
    "&bidsort=commodity&months=8&locations=80767"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.hppsd.com/",
}

_BIDS_RE = re.compile(r"var bids = (\[.*?\]);", re.DOTALL)

_MONTH_NAMES = {
    "01": "January",  "02": "February", "03": "March",    "04": "April",
    "05": "May",      "06": "June",     "07": "July",     "08": "August",
    "09": "September","10": "October",  "11": "November", "12": "December",
}

_SYM_TO_GRAIN = {"ZS": "Soybeans", "ZC": "Corn", "ZW": "Wheat"}


def _delivery_label(delivery_start: str) -> str:
    """Convert 'MM/DD/YYYY' or 'MM/YYYY' to 'Month YYYY'."""
    parts = delivery_start.split("/")
    if len(parts) == 3:
        mn = _MONTH_NAMES.get(parts[0].zfill(2), parts[0])
        return f"{mn} {parts[2]}"
    if len(parts) == 2:
        mn = _MONTH_NAMES.get(parts[0].zfill(2), parts[0])
        return f"{mn} {parts[1]}"
    return delivery_start


def fetch_hppsd_bids() -> list[dict]:
    """
    Fetch HPPSD cash bids.

    Returns a list with one entry:
        {"location": "Mitchell", "timestamp": str, "bids": [...]}
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    try:
        resp = requests.get(_API_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.error("HPPSD: fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.error("HPPSD: could not find 'var bids' in response")
        return []

    try:
        raw_bids = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("HPPSD: JSON parse error: %s", exc)
        return []

    bids: list[dict] = []
    seen: set[str] = set()

    for block in raw_bids:
        for bid in block.get("cashbids", []):
            symbol     = bid.get("symbol", "").strip()
            basis      = bid.get("basis")
            delivery_s = bid.get("delivery_start", "").strip()
            bid_id     = str(bid.get("id", ""))

            if not symbol or basis is None or not delivery_s:
                continue

            grain = _SYM_TO_GRAIN.get(symbol[:2], "")
            if not grain:
                continue

            delivery = _delivery_label(delivery_s)
            key = f"{delivery}|{symbol}"
            if key in seen:
                continue
            seen.add(key)

            bids.append({
                "bid_id":      bid_id,
                "grain":       grain,
                "delivery":    delivery,
                "cme_symbol":  symbol,
                "basis_cents": int(basis),
            })

    if not bids:
        log.warning("HPPSD: no bids parsed")
        return []

    log.info("HPPSD Mitchell  %d bid(s)", len(bids))
    return [{"location": "Mitchell", "timestamp": today_ts, "bids": bids}]


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
    from parsers.hppsd_parser import parse_hppsd_location

    locs = fetch_hppsd_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_hppsd_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.deliveryMonth:22s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
