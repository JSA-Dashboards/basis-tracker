"""
whiteriver_scraper.py — White River Soy (Seymour, IN) soybean bid scraper.

Uses the Agricharts customer API at whiteriver.agricharts.com.
Single location: Seymour, IN.
Tracks both GMO and Non-GMO (NGMO) soybean bids.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_API_URL = (
    "https://whiteriver.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=all&location=&commodity=&groupby=ccommodity"
    "&format=json&fields=name,delivery_start,delivery_end,basismonth"
    ",futures,futureschange,basis,price,notes&bidsort=delivery&months=8"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.whiteriversoy.com/",
}

_BIDS_RE = re.compile(r"var bids = (\[.*?\]);", re.DOTALL)

_MONTH_NAMES = {
    "01": "January",  "02": "February", "03": "March",    "04": "April",
    "05": "May",      "06": "June",     "07": "July",     "08": "August",
    "09": "September","10": "October",  "11": "November", "12": "December",
}

# Map Agricharts commodity names to internal grain labels
_GRAIN_MAP = {
    "GMO Soybeans":  "Soybeans",
    "NGMO Soybeans": "Soybeans NGMO",
    "Soybeans":      "Soybeans",
}


def _delivery_label(delivery_start: str) -> str:
    """Convert 'MM/DD/YYYY' or 'MM/YYYY' to 'Month YYYY'."""
    parts = delivery_start.split("/")
    if len(parts) == 3:            # MM/DD/YYYY
        mn = _MONTH_NAMES.get(parts[0].zfill(2), parts[0])
        return f"{mn} {parts[2]}"
    if len(parts) == 2:            # MM/YYYY
        mn = _MONTH_NAMES.get(parts[0].zfill(2), parts[0])
        return f"{mn} {parts[1]}"
    return delivery_start


def fetch_whiteriver_bids() -> list[dict]:
    """
    Fetch White River Soy cash bids.

    Returns a list with one entry:
        {"location": "Seymour", "timestamp": str, "bids": [...]}
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    try:
        resp = requests.get(_API_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.error("WhiteRiver: fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.error("WhiteRiver: could not find 'var bids' in response")
        return []

    try:
        raw_bids = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("WhiteRiver: JSON parse error: %s", exc)
        return []

    bids: list[dict] = []
    seen: set[str] = set()

    for commodity_block in raw_bids:
        grain_label = _GRAIN_MAP.get(commodity_block.get("name", ""), "")
        if not grain_label:
            continue

        for bid in commodity_block.get("cashbids", []):
            symbol     = bid.get("symbol", "").strip()
            basis      = bid.get("basis")
            delivery_s = bid.get("delivery_start", "").strip()
            bid_id     = str(bid.get("id", ""))

            if not symbol or basis is None or not delivery_s:
                continue

            delivery = _delivery_label(delivery_s)
            key = f"{grain_label}|{delivery}|{symbol}"
            if key in seen:
                continue
            seen.add(key)

            bids.append({
                "bid_id":      bid_id,
                "grain":       grain_label,
                "delivery":    delivery,
                "cme_symbol":  symbol,
                "basis_cents": int(basis),
            })

    if not bids:
        log.warning("WhiteRiver: no bids parsed")
        return []

    log.info("WhiteRiver Seymour  %d bid(s)", len(bids))
    return [{"location": "Seymour", "timestamp": today_ts, "bids": bids}]


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
    from parsers.whiteriver_parser import parse_whiteriver_location

    locs = fetch_whiteriver_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_whiteriver_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.grain:16s}  {r.deliveryMonth:18s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
