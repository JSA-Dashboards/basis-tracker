"""
ndsp_scraper.py — North Dakota Soybean Processors (Casselton, ND) bid scraper.

Data source: agricharts widget embedded on https://ndsoy.com/cash-bids/
  cgb1.agricharts.com/inc/cashbids/cashbids-js.php?filter=location&location=84359

Soybeans only.  Basis is returned in cents (may include half-cents like -14.5).

Usage (standalone test):
    python ndsp_scraper.py
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_URL = (
    "https://cgb1.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=location&location=84359&commodity="
    "&groupby=location&width=&showtimestamp=1&hidenav=1&showchart=1"
    "&enableScrollIntoView=1&format=table"
    "&fields=name%2Cdelivery_start%2Cdelivery_end%2Cbasismonth%2Cfutures"
    "%2Cfutureschange%2Cbasis%2Cprice"
    "&groupheading=table&bidsort=commodity"
    "&dateformat=%25m%2F%25d%2F%25Y&months=8"
    "&acCnt=1"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/javascript, */*",
    "Referer": "https://ndsoy.com/",
}

_BIDS_RE = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)

_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March",    4: "April",
    5: "May",     6: "June",     7: "July",      8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def _delivery_label(start: str, end: str) -> str:
    """
    Convert delivery window to a human label.
    '06/16/2026' / '06/30/2026' → 'June 16-30 2026'
    '07/01/2026' / '07/15/2026' → 'July 1-15 2026'
    """
    try:
        d_start = datetime.strptime(start, "%m/%d/%Y")
        d_end   = datetime.strptime(end,   "%m/%d/%Y")
        month = _MONTH_NAMES[d_start.month]
        year  = d_start.year
        return f"{month} {d_start.day}-{d_end.day} {year}"
    except (ValueError, KeyError):
        return f"{start} - {end}"


def fetch_ndsp_bids() -> list[dict]:
    """
    Fetch NDSP soybean bids from the agricharts API.

    Returns a list with one location dict for parse_ndsp_location:
        {"location": "Casselton", "state": "ND", "timestamp": str, "bids": [...]}
    """
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.error("NDSP: fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.warning("NDSP: no 'var bids' in response")
        return []

    try:
        locations = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("NDSP: JSON parse error: %s", exc)
        return []

    if not locations:
        log.warning("NDSP: empty bids array")
        return []

    loc      = locations[0]
    cashbids = loc.get("cashbids") or []
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    bids: list[dict] = []
    seen:  set[str]  = set()

    for cb in cashbids:
        symbol       = (cb.get("symbol") or "").strip()
        basis_raw    = cb.get("basis")
        delivery_start = cb.get("delivery_start", "")
        delivery_end   = cb.get("delivery_end",   "")
        bid_id         = str(cb.get("id", ""))

        if not symbol or basis_raw is None or not delivery_start:
            continue

        key = bid_id or f"{symbol}|{delivery_start}"
        if key in seen:
            continue
        seen.add(key)

        basis_cents = round(float(basis_raw))
        delivery    = _delivery_label(delivery_start, delivery_end)

        bids.append({
            "bid_id":      bid_id,
            "grain":       "Soybeans",
            "delivery":    delivery,
            "cme_symbol":  symbol,
            "basis_cents": basis_cents,
        })

    log.info("NDSP Casselton: %d soybean bid(s)", len(bids))

    if not bids:
        return []

    return [{
        "location":  "Casselton",
        "state":     "ND",
        "timestamp": timestamp,
        "bids":      bids,
    }]


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
    from parsers.ndsp_parser import parse_ndsp_location

    locs = fetch_ndsp_bids()
    print("=" * 60)
    for loc in locs:
        snap = parse_ndsp_location(loc)
        if snap:
            print(f"  {snap.provider} / {snap.location}  ({len(snap.rows)} bids)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.grain:<12} {r.deliveryMonth:<25} {r.futuresSymbol:<8} {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']}: no valid bids")
