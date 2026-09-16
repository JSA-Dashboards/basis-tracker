"""fse_scraper.py — Farm Service Elevator (fsemn.com), Willmar + Swanville, MN.

FSE runs an ASP.NET "cashbids" platform (NOT standard Bushel/AgriCharts). Each
location's bids render server-side in `<div class='cbCommodity'>` grids of
`<ul><li class='cN'>` cells: c1 delivery, c2 cash, c3 basis, c4 futures price,
c5 change, c6 (blank). There is **no futures-symbol cell**, so we derive the corn
contract from the delivery month with the roll-aware corn cycle
(rail_corridors.corn_futures_for_period → post-FND, e.g. mid-Sep "Sep" = CZ, not
CU). FSE is corn-only (feed mill).

Willmar (LocationID 3261) is the default page — plain GET. Swanville (3642) is
only reachable via an ASP.NET __doPostBack on the location dropdown, so we open a
session, carry __VIEWSTATE/__EVENTVALIDATION forward, and POST the ddl change.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow
from rail_corridors import corn_futures_for_period, _CORN_CM
from rail_paste import _to_full
from delivery_period import _M3, _MONTH_RE

log = logging.getLogger(__name__)

BASE = "https://www.fsemn.com/"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
}
# The location dropdown's ASP.NET UniqueID (name attr) — also the postback target.
_DDL = "ctl00$MainContent$fcHostContent$ctl00$ctl00$ctl00$_sideContent_1$ctl00$LocationsDdl"

# One entry per location. `loc_id` is the CashBids LocationID; the default page is
# Willmar, so it has no postback.
SITES: list[dict] = [
    {"provider": "Farm Service Elevator", "location": "Willmar, MN", "state": "MN",
     "facility_type": "Country Elevator", "loc_id": "3261", "default": True},
    {"provider": "Farm Service Elevator", "location": "Swanville, MN", "state": "MN",
     "facility_type": "Country Elevator", "loc_id": "3642", "default": False},
]


def _corn_symbol(delivery: str, as_of: date) -> str | None:
    """Delivery label ('Sep-26', 'Dec-27') → roll-aware corn CME symbol.

    Contract code comes from the roll-aware cycle (so mid-Sep 'Sep' → CZ, not the
    expired CU); the contract YEAR comes from the label's own year so deferred
    same-month deliveries don't collapse (Dec-26 → ZCZ26, Dec-27 → ZCZ27)."""
    short = corn_futures_for_period(delivery, as_of)   # 'CH'/'CZ'/… (FND-aware) or None
    if not short:
        return None
    mm = _MONTH_RE.search((delivery or "").lower())
    ym = re.search(r"(\d{2,4})\s*$", (delivery or "").strip())
    if mm and ym:
        dmon = _M3[mm.group(1)]
        yr = int(ym.group(1))
        yr = 2000 + yr if yr < 100 else yr
        cyear = yr if _CORN_CM[short] >= dmon else yr + 1   # code month before delivery → next yr
        return f"ZC{short[1]}{cyear % 100:02d}"
    return _to_full(short, as_of)                      # no explicit year → as-of based


def _form_fields(soup: BeautifulSoup) -> dict:
    """All hidden inputs of the ASP.NET form — ViewState is picky, so carry the
    whole set (missing fields 500 the postback), not just __VIEWSTATE."""
    out = {}
    for el in soup.find_all("input", attrs={"type": "hidden"}):
        name = el.get("name")
        if name:
            out[name] = el.get("value", "")
    return out


def _parse_corn_grid(soup: BeautifulSoup, as_of: date) -> list[SnapshotRow]:
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for div in soup.find_all("div", class_="cbCommodity"):
        h = div.find("h3")
        if not h or h.get_text(strip=True).lower() != "corn":
            continue
        for ul in div.find_all("ul"):
            cells = {li.get("class", ["?"])[0]: li.get_text(" ", strip=True)
                     for li in ul.find_all("li")}
            deliv = (cells.get("c1") or "").strip()
            btxt = (cells.get("c3") or "").replace("+", "").strip()
            if not deliv or deliv.lower() == "delivery":
                continue
            try:
                basis = int(round(float(btxt) * 100))
            except ValueError:
                continue
            cme = _corn_symbol(deliv, as_of)
            if not cme:
                continue
            dkey = "".join(ch for ch in deliv.upper() if ch.isalnum())
            rid = f"CN_{cme}_{dkey}"
            if rid in seen:
                continue
            seen.add(rid)
            rows.append(SnapshotRow(id=rid, grain="Corn", deliveryMonth=deliv,
                                    futuresSymbol=cme, basisCents=basis, isSpot=False))
    return rows


def fetch_fse() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape FSE Willmar (GET) + Swanville (ASP.NET postback). Returns
    (snapshot requests, location metas)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    as_of = date.today()
    reqs: list[NewSnapshotRequest] = []
    metas: list[dict] = []

    sess = requests.Session()
    sess.headers.update(_HEADERS)
    try:
        r0 = sess.get(BASE, timeout=30)
        r0.raise_for_status()
    except Exception as exc:
        log.error("FSE: initial GET failed: %s", exc)
        return [], []
    soup0 = BeautifulSoup(r0.text, "html.parser")

    for cfg in SITES:
        if cfg["default"]:
            soup = soup0
        else:
            # ASP.NET postback: switch the location dropdown to this loc_id.
            data = _form_fields(soup0)
            data.update({"__EVENTTARGET": _DDL, "__EVENTARGUMENT": "",
                         _DDL: cfg["loc_id"]})
            try:
                rp = sess.post(BASE, data=data, timeout=30,
                               headers={"Referer": BASE})
                rp.raise_for_status()
                soup = BeautifulSoup(rp.text, "html.parser")
            except Exception as exc:
                log.error("FSE %s: postback failed: %s", cfg["location"], exc)
                continue

        rows = _parse_corn_grid(soup, as_of)
        if not rows:
            log.warning("FSE %s: no corn rows parsed", cfg["location"])
            continue
        reqs.append(NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                                       location=cfg["location"], source="web", rows=rows))
        metas.append({"provider": cfg["provider"], "location": cfg["location"],
                      "state": cfg["state"], "facility_type": cfg["facility_type"]})
        log.info("FSE %s: %d corn row(s)", cfg["location"], len(rows))

    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, _ = fetch_fse()
    for r in reqs:
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows:
            print(f"   {x.deliveryMonth:10} {x.futuresSymbol} {x.basisCents:+d}")
