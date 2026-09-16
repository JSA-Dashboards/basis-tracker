"""dtn_content_scraper.py — DTN "content-services" cash-bids JSON API.

The newest DTN cash-bid widget (distinct from the aghost/ColdFusion pages in
dtn_http_scraper.py and from VistaComm). A site embeds:

    window.dtn.cashBids.createCashBidsTableWidget({apiKey, siteId, ...})

and that widget GETs a clean JSON array from:

    https://api.dtn.com/markets/sites/{siteId}/cash-bids?apikey={apiKey}&units=us

Each element already carries location.name, basisPrice ($/bu), commodityDisplayName,
contractDeliveryLabel, and the DTN futures symbol (e.g. "@C6Z") — the contract is
resolved server-side (so a Sep delivery already shows @C6Z once CU is off the
board). No HTML/JS decode needed; plain requests + JSON.

Add a site: open its bids page, find the createCashBidsTableWidget config in the
embed HTML (the apiKey + siteId), and drop a SITES row. Locations are discovered
from the response, so a multi-location co-op needs no per-location config.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from models import NewSnapshotRequest, SnapshotRow
from vistacomm_scraper import _fut_symbol, _PFX      # DTN "@C6Z" → "ZCZ26"

log = logging.getLogger(__name__)

_API = "https://api.dtn.com/markets/sites/{site}/cash-bids?apikey={key}&units=us"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, */*",
}

# commodityDisplayName (upper) → display grain. Root comes from the @-symbol.
_GRAIN = {
    "CORN": "Corn", "SOYBEANS": "Soybeans", "SOYBEAN": "Soybeans",
    "WHEAT": "Wheat", "SPRING WHEAT": "Wheat", "HRW WHEAT": "Wheat",
    "MILO": "Sorghum", "GRAIN SORGHUM": "Sorghum", "SORGHUM": "Sorghum",
}

# One entry per site. `states` optionally maps a location.name → 2-letter state
# when the API name has no ", XX" suffix; otherwise state is parsed from the name.
SITES: list[dict] = [
    {"provider": "NFP", "site_id": "e0220801",
     "api_key": "bvXb0KenjViuRw8HotpxChPvaAx1R78d",
     "facility_type": "Feed Mill"},
    {"provider": "Central United Co-op", "site_id": "E0200701",
     "api_key": "0T9QFViMwN7qKJBG2VsVcv9yR7HAObJz",
     "facility_type": "Country Elevator"},
]


def _state_from(name: str, override: str | None) -> str | None:
    if override:
        return override
    m = re.search(r",\s*([A-Z]{2})\s*$", name or "")
    return m.group(1) if m else None


def _grain_for(display: str) -> str | None:
    return _GRAIN.get((display or "").strip().upper())


def fetch_dtn_content() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every SITES entry. Returns (snapshot requests, location metas).

    One NewSnapshotRequest per (provider, location); locations are discovered from
    the API response's location.name values.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    reqs: list[NewSnapshotRequest] = []
    metas: list[dict] = []

    for cfg in SITES:
        url = _API.format(site=cfg["site_id"], key=cfg["api_key"])
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=25)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("DTN-content %s: fetch failed: %s", cfg["provider"], exc)
            continue
        if not isinstance(data, list):
            log.warning("DTN-content %s: unexpected payload shape", cfg["provider"])
            continue

        # Group bids by location, building one snapshot per location.
        by_loc: dict[str, list[SnapshotRow]] = {}
        states: dict[str, str | None] = {}
        seen: dict[str, set] = {}
        for b in data:
            loc = ((b.get("location") or {}).get("name") or "").strip()
            grain = _grain_for(b.get("commodityDisplayName"))
            cme = _fut_symbol(b.get("symbol") or "")
            basis = b.get("basisPrice")
            deliv = (b.get("contractDeliveryLabel") or "").strip()
            if not loc or not grain or not cme or basis is None:
                continue
            cents = int(round(float(basis) * 100))
            pfx = _PFX.get(cme[:2], cme[:2])
            del_key = "".join(ch for ch in deliv.upper() if ch.isalnum()) or cme
            rid = f"{pfx}_{cme}_{del_key}"
            s = seen.setdefault(loc, set())
            if rid in s:
                continue
            s.add(rid)
            by_loc.setdefault(loc, []).append(SnapshotRow(
                id=rid, grain=grain, deliveryMonth=deliv,
                futuresSymbol=cme, basisCents=cents, isSpot=False))
            states.setdefault(loc, _state_from(loc, (cfg.get("states") or {}).get(loc)))

        for loc, rows in by_loc.items():
            reqs.append(NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                                           location=loc, source="web", rows=rows))
            metas.append({"provider": cfg["provider"], "location": loc,
                          "state": states.get(loc),
                          "facility_type": cfg.get("facility_type")})
        log.info("DTN-content %s: %d location(s)", cfg["provider"], len(by_loc))

    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, metas = fetch_dtn_content()
    for r in reqs:
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows[:8]:
            print(f"   {x.grain:9} {x.deliveryMonth:10} {x.futuresSymbol} {x.basisCents:+d}")
