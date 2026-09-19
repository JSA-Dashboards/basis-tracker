"""landus_scraper.py — Landus Cooperative (Iowa), landus.ag.

Landus is a custom Next.js app; its grain-bids widget calls same-origin API routes:
    GET /api/locations                 -> [{locationName, locationNumber, state}]
    GET /api/cash-bids?location=<num>   -> {asOfDateTime, cashBids:[{commodity,
                                            bids:[{basisPrice($), basisMonth("Dec 2026"),
                                            currentBid, bidChange, deliveryDate}]}]}
basisMonth names the contract explicitly (no roll-guessing); basisPrice is dollars
→ ×100¢; deliveryDate is the delivery label.

The API sits behind Vercel bot-protection that rejects bare requests (returns an
HTML challenge), but a full browser-like header set passes it — so this stays a
plain-requests scraper (no headless browser needed).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import requests

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_BASE = "https://www.landus.ag"
# Vercel's protection passes with a realistic browser header set (bare UA → HTML challenge).
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.landus.ag/businesses/grain/grain-bids",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

_MON = {"jan": "F", "feb": "G", "mar": "H", "apr": "J", "may": "K", "jun": "M",
        "jul": "N", "aug": "Q", "sep": "U", "oct": "V", "nov": "X", "dec": "Z"}
_PFX = {"ZC": "CN", "ZS": "SB", "ZW": "WH", "KE": "KW"}


def _grain(commodity: str):
    c = (commodity or "").lower()
    if "soy" in c:
        return ("ZS", "Soybeans")
    if "milo" in c or "sorghum" in c:
        return ("ZC", "Sorghum")
    if "wheat" in c:
        return ("ZW", "Wheat")
    if "corn" in c:
        return ("ZC", "Corn")
    return None


def _symbol(root: str, basis_month: str):
    m = re.match(r"([A-Za-z]{3})[a-z]*\s+(\d{4})", (basis_month or "").strip())
    if not m:
        return None
    code = _MON.get(m.group(1).lower())
    return f"{root}{code}{m.group(2)[2:]}" if code else None


def fetch_landus() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every Landus location. Returns (snapshot requests, location metas)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    sess = requests.Session()
    sess.headers.update(_HEADERS)
    # Landus's Vercel bot-protection blocks datacenter IPs — the DigitalOcean Droplet
    # gets an HTML challenge instead of JSON. Route through a proxy when LANDUS_PROXY
    # (or a generic SCRAPER_PROXY) is set, e.g. "http://user:pass@host:port". Unset =
    # direct connection (works from a normal/office IP such as the desktop).
    proxy = (os.environ.get("LANDUS_PROXY") or os.environ.get("SCRAPER_PROXY") or "").strip()
    if proxy:
        sess.proxies.update({"http": proxy, "https": proxy})
        log.info("Landus: routing requests through configured proxy")
    try:
        locs = sess.get(f"{_BASE}/api/locations", timeout=25).json()
    except Exception as exc:
        log.error("Landus: /api/locations failed: %s", exc)
        return [], []
    if not isinstance(locs, list):
        log.warning("Landus: unexpected /api/locations payload")
        return [], []

    reqs: list[NewSnapshotRequest] = []
    metas: list[dict] = []
    for loc in locs:
        num = str(loc.get("locationNumber") or "").strip()
        name = (loc.get("locationName") or "").strip()
        state = (loc.get("state") or "").strip() or None
        if not num or not name:
            continue
        try:
            data = sess.get(f"{_BASE}/api/cash-bids", params={"location": num}, timeout=25).json()
        except Exception as exc:
            log.warning("Landus %s: cash-bids failed: %s", name, exc)
            continue

        rows: list[SnapshotRow] = []
        seen: set[str] = set()
        for grp in (data or {}).get("cashBids", []):
            gi = _grain(grp.get("commodity"))
            if not gi:
                continue
            root, grain = gi
            for b in grp.get("bids", []):
                bp = b.get("basisPrice")
                sym = _symbol(root, b.get("basisMonth"))
                deliv = (b.get("deliveryDate") or "").strip()
                if bp in (None, "") or not sym or not deliv:
                    continue
                try:
                    basis = int(round(float(bp) * 100))
                except (TypeError, ValueError):
                    continue
                dkey = "".join(ch for ch in deliv.upper() if ch.isalnum()) or sym
                rid = f"{_PFX.get(root, root)}_{sym}_{dkey}"
                if rid in seen:
                    continue
                seen.add(rid)
                rows.append(SnapshotRow(id=rid, grain=grain, deliveryMonth=deliv,
                                        futuresSymbol=sym, basisCents=basis, isSpot=False))
        if not rows:
            continue
        location = f"{name}, {state}" if state else name
        reqs.append(NewSnapshotRequest(timestamp=ts, provider="Landus",
                                       location=location, source="web", rows=rows))
        metas.append({"provider": "Landus", "location": location, "state": state,
                      "facility_type": "Country Elevator"})

    log.info("Landus: %d location(s)", len(reqs))
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, _ = fetch_landus()
    print(f"locations with data: {len(reqs)}")
    for r in reqs[:5]:
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows[:4]:
            print(f"   {x.grain:9} {x.deliveryMonth:12} {x.futuresSymbol} {x.basisCents:+d}")
