"""
agricharts_scraper.py — generic scraper for AgriCharts cash-bid feeds hosted on a
tenant's OWN domain (not the *.agricharts.com subdomain the ksethanol scraper
assumes). The `/inc/cashbids/cashbids-js.php` endpoint returns `var bids = [...]`
where each element is a LOCATION with a nested `cashbids[]` (CME symbol, integer
basis, delivery window). Reuses parsers.ksethanol_parser.parse_ksethanol_location,
which is provider-agnostic.

Two tenant shapes:
  • single  — the whole feed is one plant; use the configured location + facility.
  • multi   — the feed lists many locations (JBS feed mills / elevators); use each
              feed location's own name, and derive facility from that name.

Add a tenant to TENANTS — no new code.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

from parsers.ksethanol_parser import parse_ksethanol_location

log = logging.getLogger(__name__)

_QS = ("?filter=all&location=&commodity=&groupby=location&format=table"
       "&fields=name,delivery_start,delivery_end,basismonth,futures,futureschange,basis,price"
       "&bidsort=commodity&dateformat=%25m/%25d/%25Y&months=11")
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JPSI basis tracker; kpostin@jpsi.com)",
            "Accept": "text/javascript, */*"}
_BIDS_RE = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)

# provider -> config. `host` is the site's own domain; the query string is appended.
TENANTS: list[dict] = [
    {"provider": "Mid Missouri", "host": "https://midmissourienergy.agricharts.com",
     "shape": "single", "location": "Malta Bend, MO", "state": "MO",
     "facility": "Corn Processing"},
    {"provider": "JBS", "host": "https://jbslivepork.com",
     "shape": "multi", "state": None},
    {"provider": "Garden City Coop", "host": "https://gccoop.agricharts.com",
     "shape": "multi", "state": "KS"},
]


def _facility_from_name(name: str) -> str:
    n = name.lower()
    if "ethanol" in n or "energy" in n or "bio" in n:
        return "Corn Processing"
    if "feed mill" in n or "mill" in n:
        return "Feed Mill"
    if "elevator" in n:
        return "Country Elevator"
    return "Feed Mill"          # JBS Live Pork sites are feed procurement by default


def _clean_loc(name: str) -> str:
    # "Hedrick, IA Feed Mill" -> "Hedrick, IA"; "Mill 240 at Tarkio, MO Feed Mill" -> keep town
    return re.sub(r"\s+(Feed Mill|Elevator|Mill|Terminal)\s*$", "", name).strip()


def fetch_agricharts_bids() -> list[dict]:
    """Return loc dicts (parser-ready) across every tenant."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    session = requests.Session()
    session.headers.update(_HEADERS)
    out: list[dict] = []
    for t in TENANTS:
        try:
            r = session.get(t["host"] + "/inc/cashbids/cashbids-js.php" + _QS, timeout=30)
            r.raise_for_status()
            m = _BIDS_RE.search(r.text)
            feed = json.loads(m.group(1)) if m else []
        except Exception as exc:
            log.error("AgriCharts fetch failed for %s: %s", t["provider"], exc)
            continue

        for loc in feed:
            # feed-flagged not-for-publication (value is a STRING "0"/"1", so
            # a bare truthiness check would drop everyone).
            if str(loc.get("hide_on_sites_and_apis") or "").strip().lower() in ("1", "true", "yes"):
                continue
            cashbids = loc.get("cashbids") or []
            if not cashbids:
                continue
            raw_name = (loc.get("name") or "").strip()
            if t["shape"] == "single":
                location, state, facility = t["location"], t["state"], t["facility"]
            else:
                if not raw_name or raw_name.lower() in ("futures only", ""):
                    continue                       # skip the futures-only pseudo row
                location = _clean_loc(raw_name)
                state = loc.get("state") or t.get("state")
                # Some feeds (JBS) embed the state in the name ("Hedrick, IA Feed
                # Mill"); others (Garden City Coop) don't — append it so every
                # location reads "Town, ST".
                if state and "," not in location:
                    location = f"{location}, {state}"
                # Prefer the feed's own facility_type (authoritative — e.g. Garden
                # City Coop's "River Valley" has no "Elevator" in its name but the
                # feed labels it Country Elevator); fall back to the name heuristic.
                facility = (loc.get("facility_type") or "").strip() or _facility_from_name(raw_name)
            out.append({
                "provider": t["provider"], "location_name": location,
                "state": state, "city": loc.get("city") or "",
                "facility_type": facility, "timestamp": ts,
                "cashbids": [{
                    "bid_id": str(b.get("id") or ""), "grain": b.get("name") or "",
                    "symbol": b.get("symbol") or "", "basis": int(b.get("basis") or 0),
                    "delivery_start": b.get("delivery_start") or "",
                } for b in cashbids],
            })
        time.sleep(0.5)
    return out
