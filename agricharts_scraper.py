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
    # Garden City Coop posts, per commodity, an 'Old Crop' window (since 09/01/2025, to 12/01/2026) AND a 'New Crop 2026' window (since 09/01/2026) with the same basis; the parser's start-month rule dropped
    # BOTH from the first October on (114 of its 134 bids on 2026-10-27: the whole in-season board, HRW wheat / corn / milo, with only the deferred KEN27 wheat left). `open_windows` files each open
    # window under the current month (the pair collapses to one row, the later-starting window); `skip_zero_basis` drops the corn bids at four locations posted at exactly 0 (a posted-but-not-bid
    # placeholder, the same AgriCharts convention as writeBidRow: the other 17 corn bids are +10 to +45).
    {"provider": "Garden City Coop", "host": "https://gccoop.agricharts.com",
     "shape": "multi", "state": "KS", "open_windows": True, "skip_zero_basis": True},
    # Gold Eagle Coop posts its whole co-op; we only track the Goldfield plant.
    # `want` filters a multi feed to specific locations and renames/reclassifies them.
    {"provider": "Gold Eagle Coop", "host": "https://goldeagle.agricharts.com",
     "shape": "multi", "state": "IA",
     "want": {"GOLDFIELD": ("Goldfield, IA", "Corn Processing")}},
    # Meadowland Farmers Co-op (MN) — feed aggregates to one bid sheet (HQ Lamberton).
    {"provider": "Meadowland Co-op", "host": "https://meadsprout2.agricharts.com",
     "shape": "single", "location": "Lamberton, MN", "state": "MN",
     "facility": "Country Elevator"},
    # Alcivia (WI): 14 elevators + a feed mill in one feed (alcivia.com/wisconsin-grain-services/cash-bids-grain-wisconsin-grain/). Its names are ALL CAPS
    # ('FALL CREEK'), so `titlecase`; the feed carries each location's own latitude / longitude, which become the map pins (`pins`).
    # Alcivia's harvest window is 'Oct/Nov 26' (10/01 - 11/30): the start-month rule would drop it for all of November, so `open_windows`. Its wheat 'Storage' / 'Storage Only' windows (07/01/2026 - 02/28/2027,
    # basis -100 to -160, 8 locations) are storage prices, not delivery bids: `skip_notes` keeps them out (the old rule dropped them, by accident; with open_windows they would appear as a nearby wheat bid).
    {"provider": "Alcivia", "host": "https://alciviacoop.agricharts.com",
     "shape": "multi", "state": "WI", "titlecase": True, "pins": True, "open_windows": True, "skip_notes": ["Storage"]},
    # Ag Partners Cooperative (Seneca KS; agpartnerscoop.com/grain/cash-bids embeds this feed): 20 elevators in NE Kansas, one in Nebraska (Rulo) and one in Missouri (Tarkio); the feed gives each
    # location's state and pin. Its 'AGP St Joe' row is the co-op's DIRECT-SHIP bid to AGP's plant (facility_type 'Direct Ship', 'Broker Beans'): AGP's own plant is the AGP provider's
    # location, so it is `skip`ped here (the River Valley / H&F reason: one bid stream, one snapshot). Its wheat is Kansas City hard red winter (KEZ26) under the feed's name 'Red Wheat',
    # which grain_map reads as soft red winter, so `grain_alias` files it as 'Wheat, Hrw'. Its main bids are HARVEST windows ('09/01/2026 - 11/30/2026'): `open_windows` keeps a window that
    # started in an earlier month but has not ended, filed under the current month (the parser's start-month rule alone would drop them from October on; see the parser).
    {"provider": "Ag Partners Coop", "host": "https://agpartners.agricharts.com",
     "shape": "multi", "state": "KS", "pins": True, "skip": ["AGP St Joe"], "grain_alias": {"Red Wheat": "Wheat, Hrw"}, "open_windows": True},
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


def _title_caps(name: str) -> str:
    """'FALL CREEK' -> 'Fall Creek'; a name that is already mixed case ('Four Corners (New Richmond)') is left as it is."""
    return name.title() if name.isupper() else name


def _feed_pin(loc: dict) -> dict:
    """{'lat', 'lon'} from the feed location's own coordinates, or {} when it has none (or 0,0)."""
    try:
        la, lo = float(loc.get("latitude")), float(loc.get("longitude"))
    except (TypeError, ValueError):
        return {}
    if not (-90 <= la <= 90 and -180 <= lo <= 180) or (la, lo) == (0.0, 0.0):
        return {}
    return {"lat": round(la, 5), "lon": round(lo, 5)}


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
            if t.get("skip_zero_basis") or t.get("skip_notes"):
                skip_notes = [s.lower() for s in t.get("skip_notes", ())]
                cashbids = [b for b in cashbids if not (t.get("skip_zero_basis") and not int(b.get("basis") or 0))
                            and not any(s in (b.get("notes") or "").lower() for s in skip_notes)]
            if not cashbids:
                continue
            raw_name = (loc.get("name") or "").strip()
            if t["shape"] == "single":
                location, state, facility = t["location"], t["state"], t["facility"]
            elif t.get("want") is not None:
                # multi feed, but only the configured locations are kept + renamed.
                hit = t["want"].get(raw_name.strip().upper())
                if not hit:
                    continue
                location, facility = hit
                state = t.get("state")
            else:
                if not raw_name or raw_name.lower() in ("futures only", ""):
                    continue                       # skip the futures-only pseudo row
                if raw_name.upper() in {s.upper() for s in t.get("skip", ())}:
                    continue                       # a location of the feed that is another company's bid stream (Ag Partners' direct-ship row to AGP)
                location = _clean_loc(raw_name)
                if t.get("titlecase"):
                    location = _title_caps(location)
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
                **(_feed_pin(loc) if t.get("pins") else {}),
                "cashbids": [{
                    "bid_id": str(b.get("id") or ""), "grain": (t.get("grain_alias") or {}).get(b.get("name") or "", b.get("name") or ""),
                    "symbol": b.get("symbol") or "", "basis": int(b.get("basis") or 0),
                    "delivery_start": b.get("delivery_start") or "", "delivery_end": b.get("delivery_end") or "",
                } for b in cashbids],
                **({"open_windows": True} if t.get("open_windows") else {}),
            })
        time.sleep(0.5)
    return out
