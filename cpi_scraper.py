"""cpi_scraper.py — Cooperative Producers, Inc. (CPI), Nebraska.

CPI (cpicoop.com/grain/bids-futures) posts every location's bids as plain static
HTML tables (a WordPress page, no JS/widget). Each location is an `<h3>` heading
followed by a `<table>` whose header is
    Commodity | Delivery | Basis month | Basis | Futures | Change | Cash price
Basis is already in cents (e.g. "-38", "+5") and the **Basis month** column names
the contract explicitly ("Dec 2026", "Nov 2026"), so no roll-guessing.

CPI wheat is Hard Red Winter (Kansas City) → KE; milo/sorghum price off corn.
The "AGP David City / AGP Hastings" rows are delivered points (other company) —
excluded.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

URL = "https://www.cpicoop.com/grain/bids-futures"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
}

_MON = {"jan": "F", "feb": "G", "mar": "H", "apr": "J", "may": "K", "jun": "M",
        "jul": "N", "aug": "Q", "sep": "U", "oct": "V", "nov": "X", "dec": "Z"}
_PFX = {"ZC": "CN", "ZS": "SB", "ZW": "WH", "KE": "KW"}


def _grain(commodity: str):
    """'Corn (#2 Yellow)' → ('ZC','Corn'); wheat = HRW KC (KE); milo = corn board."""
    c = (commodity or "").lower()
    if "soy" in c:
        return ("ZS", "Soybeans")
    if "milo" in c or "sorghum" in c:
        return ("ZC", "Sorghum")
    if "wheat" in c:
        return ("KE", "Wheat")
    if "corn" in c:
        return ("ZC", "Corn")
    return None


def _symbol(root: str, basis_month: str):
    """'Dec 2026' + root → 'ZCZ26'."""
    m = re.match(r"([A-Za-z]{3})[a-z]*\s+(\d{4})", (basis_month or "").strip())
    if not m:
        return None
    code = _MON.get(m.group(1).lower())
    return f"{root}{code}{m.group(2)[2:]}" if code else None


def _basis_cents(txt: str):
    m = re.search(r"[-+]?\d+", (txt or "").replace(",", ""))
    return int(m.group()) if m else None


def fetch_cpi() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every CPI location table. Returns (snapshot requests, location metas)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    try:
        r = requests.get(URL, headers=_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        log.error("CPI: fetch failed: %s", exc)
        return [], []
    soup = BeautifulSoup(r.text, "html.parser")

    reqs: list[NewSnapshotRequest] = []
    metas: list[dict] = []
    for tbl in soup.find_all("table"):
        header = " ".join(th.get_text(" ", strip=True).lower() for th in tbl.find_all("th"))
        if "basis month" not in header:
            continue
        h = tbl.find_previous(["h1", "h2", "h3", "h4"])
        loc = h.get_text(strip=True) if h else ""
        if not loc or loc.lower().startswith("agp"):     # delivered point / no heading
            continue

        rows: list[SnapshotRow] = []
        seen: set[str] = set()
        for tr in tbl.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 4:
                continue
            gi = _grain(tds[0])
            if not gi:
                continue
            root, grain = gi
            sym = _symbol(root, tds[2])
            basis = _basis_cents(tds[3])
            deliv = tds[1].strip()
            if not sym or basis is None or not deliv:
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
        location = f"{loc}, NE"
        reqs.append(NewSnapshotRequest(timestamp=ts, provider="CPI",
                                       location=location, source="web", rows=rows))
        metas.append({"provider": "CPI", "location": location, "state": "NE",
                      "facility_type": "Country Elevator"})

    log.info("CPI: %d location(s)", len(reqs))
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, _ = fetch_cpi()
    for r in reqs[:6]:
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows[:4]:
            print(f"   {x.grain:9} {x.deliveryMonth:16} {x.futuresSymbol} {x.basisCents:+d}")
    print(f"\nTOTAL locations: {len(reqs)}")
