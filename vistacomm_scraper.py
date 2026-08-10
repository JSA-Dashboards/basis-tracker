"""
vistacomm_scraper.py — scraper for DTN cash bids served through VistaComm's
WordPress "vc-dtn" widget. Those sites (e.g. Fox River Valley Energy) render the
bid grid CLIENT-SIDE, so the served HTML is empty — but the widget just POSTs to a
plain JSON proxy that returns the grid as an HTML string, gated only by a per-site
`License-Key` header (embedded in the page's `spalicensekey` JS var):

    POST https://spacentral.vistacomm.com/api/v1/dtn
    headers: License-Key: <site license key>
    body (JSON): {type:"cashbids", locationid:<int>, commodity:"corn",
                  columns:[...], location:<name>, ...}   ← columns MUST be an array,
                  locationid an int, formatting/charts booleans (string forms 500).

Response .result is an HTML table: Delivery | Cash Price | Futures Month (@C6U) |
Basis | Futures Price. Futures month is DTN format `@C{yeardigit}{monthcode}`.

To add a site: capture its `spalicensekey` (page source) + its location id
(GET-equivalent: POST /api/v1/dtn-locations with the License-Key) → one SITES row.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, date, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_API = "https://spacentral.vistacomm.com/api/v1/dtn"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json, */*",
}
_COLUMNS = ["cash-price:Cash Price", "basis-month:Futures Month",
            "basis:Basis", "futures-price:Futures Price"]

# One row per physical plant. `commodities` = [(DTN commodity param, our grain)].
SITES: list[dict] = [
    {"provider": "Fox River Valley Energy", "location": "Oshkosh, WI", "state": "WI",
     "facility_type": "Corn Processing",
     "license_key": "e4365706-376f-530c-8aa5-5daeeac32206",
     "location_id": 13508, "location_name": "FRV ETHANOL",
     "commodities": [("corn", "Corn")]},
]

_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
_ROOT = {"C": "ZC", "S": "ZS", "W": "ZW", "KW": "KE", "MW": "MW"}
_PFX = {"ZC": "CN", "ZS": "SB", "ZW": "WH"}
# DTN futures token: @C6U  → root C, year-digit 6, month U.
_DTN_RE = re.compile(r"@([A-Z]{1,2})(\d)([FGHJKMNQUVXZ])")


def _fut_symbol(token: str) -> str | None:
    """'@C6U' → 'ZCU26' (year digit resolved to the nearest non-past year)."""
    m = _DTN_RE.match(token.strip())
    if not m:
        return None
    root = _ROOT.get(m.group(1))
    if not root:
        return None
    yd = int(m.group(2))
    yr = 2020 + yd
    while yr < date.today().year:      # single digit → bump to the future decade
        yr += 10
    return f"{root}{m.group(3)}{yr % 100:02d}"


def _basis_cents(txt: str) -> int | None:
    txt = txt.replace("+", "").strip()
    try:
        return int(round(float(txt) * 100))
    except ValueError:
        return None


def _parse_grid(html: str, grain: str) -> list[SnapshotRow]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    pfx = _PFX.get({"Corn": "ZC", "Soybeans": "ZS"}.get(grain, ""), "XX")
    for tr in soup.find_all("tr"):
        tds = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(tds) < 4:
            continue
        # find the DTN futures token cell; basis is the next numeric cell after it
        fi = next((i for i, c in enumerate(tds) if _DTN_RE.match(c)), None)
        if fi is None:
            continue
        cme = _fut_symbol(tds[fi])
        delivery = tds[0]
        # Skip the outer wrapper / header rows: a real delivery label is short
        # (e.g. "Aug", "Oct/Nov"), not the whole concatenated grid or a header.
        if len(delivery) > 18 or re.search(r"delivery date|cash price|futures", delivery, re.I):
            continue
        basis = None
        for c in tds[fi + 1:]:
            basis = _basis_cents(c)
            if basis is not None:
                break
        if not cme or basis is None:
            continue
        del_key = "".join(ch for ch in delivery.upper() if ch.isalnum()) or cme
        row_id = f"{pfx}_{cme}_{del_key}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(SnapshotRow(id=row_id, grain=grain, deliveryMonth=delivery,
                                futuresSymbol=cme, basisCents=basis, isSpot=False))
    return rows


def parse_site(cfg: dict) -> NewSnapshotRequest | None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    headers = dict(_HEADERS)
    headers["License-Key"] = cfg["license_key"]
    if cfg.get("origin"):
        headers["Origin"] = cfg["origin"]
        headers["Referer"] = cfg["origin"].rstrip("/") + "/"
    all_rows: list[SnapshotRow] = []
    for commodity, grain in cfg.get("commodities", [("corn", "Corn")]):
        body = {"layout": "normal", "formatting": False, "charts": True,
                "disablelocations": "", "columns": _COLUMNS,
                "location": cfg["location_name"], "locationid": int(cfg["location_id"]),
                "showlocationsselect": True, "showcommodityselect": True,
                "commodity": commodity, "dtnusername": "", "dtnpassword": "",
                "type": "cashbids"}
        try:
            r = requests.post(_API, headers=headers, data=json.dumps(body), timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.error("VistaComm fetch failed for %s (%s): %s", cfg["location"], commodity, exc)
            continue
        if (data.get("status") or "").upper() != "OK":
            log.warning("VistaComm %s %s: %s", cfg["location"], commodity, data.get("status"))
            continue
        all_rows.extend(_parse_grid(data.get("result") or "", grain))
    if not all_rows:
        return None
    return NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                              location=cfg["location"], source="web", rows=all_rows)


def fetch_vistacomm() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every SITES entry. Returns (snapshot requests, location metas)."""
    reqs, metas = [], []
    for cfg in SITES:
        req = parse_site(cfg)
        if req is None:
            continue
        reqs.append(req)
        metas.append({"provider": cfg["provider"], "location": cfg["location"],
                      "state": cfg.get("state"), "facility_type": cfg.get("facility_type")})
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, metas = fetch_vistacomm()
    for req in reqs:
        print(f"  {req.provider} · {req.location} — {len(req.rows)} row(s)")
        for r in req.rows[:6]:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"     {r.deliveryMonth:10s} {r.futuresSymbol:7s} {sign}{r.basisCents}c")
