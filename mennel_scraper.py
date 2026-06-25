"""
Mennel (The Mennel Milling Company) cash-bid scraper.

mennel.com is a Craft CMS site whose location pages load bids via AgriCharts
(Cargill-powered), the same platform as Star of the West and CGB. The all-locations
feed at mennelmilling.agricharts.com/inc/cashbids/cashbids-js.php?filter=all returns
`var bids = [ {location …, cashbids:[…]}, … ];` — every active location + bids in one
call.

Mennel is a Soft Red Winter wheat miller (OH/IN/MI/NC), so their generically-labelled
"Wheat" is SRW (the parser maps it to Soft Red Winter). One known bad row (Atlanta
Grain quotes a "Soybeans" bid against a ZW wheat symbol) is dropped by the
name↔symbol sanity check below.
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
    "https://mennelmilling.agricharts.com/inc/cashbids/cashbids-js.php"
    "?filter=all&location=&commodity=&groupby=location&showtimestamp=1&format=table"
    "&fields=name,delivery_start,delivery_end,basismonth,futures,futureschange,basis,price"
    "&dateformat=%m/%d/%Y&months=11"
)
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/javascript, */*",
    "Referer": "https://mennel.com/",
}
_BIDS_RE = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)

# Commodity name → expected CME symbol root, to reject mislabelled rows.
_SYM_ROOT = {"corn": "ZC", "soybeans": "ZS", "soybean": "ZS", "wheat": "ZW"}


def _to_cents(val) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return None


def _grain_matches_symbol(name: str, symbol: str) -> bool:
    """True unless the commodity name clearly contradicts the symbol root
    (e.g. a 'Soybeans' bid quoted against a ZW wheat symbol)."""
    root = _SYM_ROOT.get((name or "").strip().lower())
    return (root is None) or symbol.upper().startswith(root)


def fetch_mennel_bids() -> list[dict]:
    """Per-location dicts (CGB-compatible shape) for parsers.mennel_parser."""
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Mennel fetch failed: %s", exc)
        return []

    m = _BIDS_RE.search(resp.text)
    if not m:
        log.warning("Mennel: no 'var bids' array in response")
        return []
    try:
        locations = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.error("Mennel: bad JSON: %s", exc)
        return []

    today = datetime.now(timezone.utc).date().isoformat()
    dropped = 0
    out: list[dict] = []
    for loc in locations:
        bids = []
        for b in (loc.get("cashbids") or []):
            name   = (b.get("name") or "").strip()
            symbol = (b.get("symbol") or "").strip()
            cents  = _to_cents(b.get("basis"))
            if not symbol or cents is None:
                continue
            if not _grain_matches_symbol(name, symbol):
                dropped += 1
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
            "location_name": (loc.get("name") or "").strip(),
            "city":          (loc.get("city") or "").strip(),
            "state":         (loc.get("state") or "").strip(),
            "facility_type": "Country Elevator",
            "timestamp":     today,
            "cashbids":      bids,
        })

    log.info("Mennel: %d location(s) with bids%s",
             len(out), f"  ({dropped} bad row(s) dropped)" if dropped else "")
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    data = fetch_mennel_bids()
    print(f"{len(data)} locations")
    for loc in data:
        print(f"\n{loc['location_name']}  ({loc['city']}, {loc['state']})  {len(loc['cashbids'])} bids")
        for b in loc["cashbids"]:
            print(f"   {b['grain']:12} {b['symbol']:8} {b['basis']:+4d}c  {b['delivery_start']}")
