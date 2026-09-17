"""bushel_powered_scraper.py — the NEWER Bushel platform (bushelpowered.com).

Distinct from the older Bushel white-label (o.bushelsites.com cbCommodity HTML in
bushelsites_scraper.py). Sites embed a React "cash-bids" widget
(`<div id="cash-bids-root" data-slug="…">`, bushel-web-offers) that POSTs to a
JSON aggregator API:

    POST https://api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList
    header  App-Company: <slug>          # the data-slug on the page; the tenant
    body    {}

Response: {locations:[{name, groups:[{displayName(commodity), bids:[{description,
basisPrice($), futuresSymbol("ZCZ26"), bidType, …}]}]}]}. The futuresSymbol is
already a clean CME symbol, so no conversion — basisPrice is dollars → ×100¢.

Add a site: open its bids page, read the `data-slug` on `#cash-bids-root`, drop a
SITES row. Locations are discovered from the response.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_API = "https://api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Content-Type": "application/json",
    "Accept": "application/json, */*",
}
_PFX = {"ZC": "CN", "ZS": "SB", "ZW": "WH", "KE": "KW", "ZM": "ML"}

# One row per site. `state` is applied to every location (the API omits it); use a
# `states` {locname: st} override only for a co-op that spans states.
SITES: list[dict] = [
    {"provider": "Michigan Agricultural Commodities", "slug": "mac", "state": "MI",
     "facility_type": "Country Elevator", "origin": "https://www.michag.com"},
    # CF Grain (Canby, MN) — 5 locations; Astoria is across the SD line.
    {"provider": "CF Grain", "slug": "canby", "state": "MN",
     "states": {"Astoria": "SD"},
     "facility_type": "Country Elevator", "origin": "https://www.cfgrain.com"},
    # PRC Co-op (Prinsburg, MN) — 2 MN locations.
    {"provider": "PRC Co-op", "slug": "prinsburg", "state": "MN",
     "facility_type": "Country Elevator", "origin": "https://www.prccoop.com"},
]


def _grain(name: str) -> str:
    n = (name or "").lower()
    if "soy" in n:
        return "Soybeans"
    if "milo" in n or "sorghum" in n:
        return "Sorghum"
    if "wheat" in n:
        return "Wheat"
    if "corn" in n:
        return "Corn"
    return (name or "").strip() or "Corn"


def fetch_bushel_powered() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every SITES slug. Returns (snapshot requests, location metas)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    reqs: list[NewSnapshotRequest] = []
    metas: list[dict] = []

    for cfg in SITES:
        headers = dict(_HEADERS)
        headers["App-Company"] = cfg["slug"]
        if cfg.get("origin"):
            headers["Origin"] = cfg["origin"]
            headers["Referer"] = cfg["origin"].rstrip("/") + "/"
        try:
            resp = requests.post(_API, headers=headers, data="{}", timeout=25)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("Bushel-powered %s: fetch failed: %s", cfg["provider"], exc)
            continue

        states = cfg.get("states") or {}
        n_loc = 0
        for loc in data.get("locations", []):
            name = (loc.get("name") or "").strip()
            if not name:
                continue
            rows: list[SnapshotRow] = []
            seen: set[str] = set()
            for grp in loc.get("groups", []):
                grain = _grain(grp.get("displayName") or
                               (grp.get("commodity") or {}).get("name"))
                for b in grp.get("bids", []):
                    if (b.get("bidType") or "cash") != "cash":
                        continue
                    sym = (b.get("futuresSymbol") or "").strip()
                    bp = b.get("basisPrice")
                    deliv = (b.get("description") or "").strip()
                    if not sym or bp in (None, "") or not deliv:
                        continue
                    try:
                        basis = int(round(float(bp) * 100))
                    except (TypeError, ValueError):
                        continue
                    dkey = "".join(ch for ch in deliv.upper() if ch.isalnum()) or sym
                    rid = f"{_PFX.get(sym[:2], sym[:2])}_{sym}_{dkey}"
                    if rid in seen:
                        continue
                    seen.add(rid)
                    rows.append(SnapshotRow(id=rid, grain=grain, deliveryMonth=deliv,
                                            futuresSymbol=sym, basisCents=basis, isSpot=False))
            if not rows:
                continue
            location = f"{name}, {states.get(name, cfg.get('state')) or ''}".rstrip(", ")
            reqs.append(NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                                           location=location, source="web", rows=rows))
            metas.append({"provider": cfg["provider"], "location": location,
                          "state": states.get(name, cfg.get("state")),
                          "facility_type": cfg.get("facility_type")})
            n_loc += 1
        log.info("Bushel-powered %s: %d location(s)", cfg["provider"], n_loc)

    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, _ = fetch_bushel_powered()
    for r in reqs:
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows[:5]:
            print(f"   {x.grain:9} {x.deliveryMonth:10} {x.futuresSymbol} {x.basisCents:+d}")
