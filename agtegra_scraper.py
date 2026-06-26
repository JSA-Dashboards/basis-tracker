"""
Agtegra Cooperative cash-bid scraper.

agtegra.com/cash-bids is powered by AgriCharts (subdomain `sdwg`), the same platform
as Star of the West / Mennel / CGB. The all-locations feed at
    https://sdwg.agricharts.com/inc/cashbids/cashbids-js.php?filter=all
returns `var bids = [ {location …, cashbids:[…]}, … ];` — every active location and
its bids in one call.

Agtegra (SD/ND/MN co-op) quotes Corn & Soybeans (Chicago ZC/ZS), Spring Wheat
(Minneapolis MW), Winter Wheat (KC KE) and Milo (vs ZC). Sunflowers (Birdseed/Hoss/
NuSuns) and Oats are flat-price with no futures symbol, so they drop out (no basis).
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
    "https://sdwg.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=all&location=&commodity=&groupby=location&showtimestamp=1&format=table"
    "&fields=name,delivery_start,delivery_end,basismonth,futures,futureschange,basis,price"
    "&dateformat=%m/%d/%Y&months=11"
)
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/javascript, */*",
    "Referer": "https://www.agtegra.com/",
}
_BIDS_RE = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)

_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())


def _clean_name(name: str) -> str:
    """Tidy an AgriCharts location name: drop an embedded street address and a
    trailing state suffix ('Aberdeen - SD'→'Aberdeen')."""
    n = " ".join(str(name or "").split())
    n = re.sub(r"\s+\d{2,}\b.*$", "", n)
    m = re.match(r"^(.*?)[\s,\-]+([A-Z]{2})$", n)
    if m and m.group(2) in _US_STATES and len(m.group(1).strip()) > 1:
        n = m.group(1)
    return n.strip()


def _to_cents(val) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return None


def fetch_agtegra_bids() -> list[dict]:
    """Per-location dicts (CGB-compatible shape) for parsers.agtegra_parser."""
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Agtegra fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.warning("Agtegra: no 'var bids' array in response")
        return []
    try:
        locations = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("Agtegra: bad JSON: %s", exc)
        return []

    today = datetime.now(timezone.utc).date().isoformat()
    out: list[dict] = []
    for loc in locations:
        bids = []
        for b in (loc.get("cashbids") or []):
            name   = (b.get("name") or "").strip()
            symbol = (b.get("symbol") or "").strip()
            cents  = _to_cents(b.get("basis"))
            if not symbol or cents is None:          # flat-price (sunflowers/oats) → skip
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
            "location_name": _clean_name(loc.get("name") or ""),
            "city":          (loc.get("city") or "").strip(),
            "state":         (loc.get("state") or "").strip(),
            "facility_type": "Country Elevator",
            "timestamp":     today,
            "cashbids":      bids,
        })

    log.info("Agtegra: %d location(s) with bids", len(out))
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    data = fetch_agtegra_bids()
    print(f"{len(data)} locations")
    from collections import Counter
    gc = Counter()
    for loc in data:
        for b in loc["cashbids"]:
            gc[b["grain"]] += 1
    print("grains:", dict(gc))
    for loc in data[:3]:
        print(f"\n{loc['location_name']}  ({loc['city']}, {loc['state']})  {len(loc['cashbids'])} bids")
        for b in loc["cashbids"][:6]:
            print(f"   {b['grain']:14} {b['symbol']:8} {b['basis']:+4d}c  {b['delivery_start']}")
