"""
cihedging_scraper.py — generic scraper for CIHedging.com's v2 cash-bid widget.

Several ethanol/processing plants embed CIHedging's JS widget
(`cihedging.com/common/cf/modules/cashbid/widget/CashBidWidget.js`). The widget
POSTs to a v2 endpoint that returns the bid grid as an HTML string wrapped in
JSON:

    POST https://www.cihedging.com/cih/api/index.cfm/v2/origination/cashbids/{companyID}/widget?<params>

(The OLDER `shellrock_scraper.py` hits the NON-v2 `/origination/cashbids/{id}`
path, which 405s for these newer-format companies — hence this separate module.)

Each site is one `companyID`. The returned HTML groups bids by commodity in
`<div class="cih-com-row" data-commodity-name="Corn">` blocks, each holding a
`<table class="cih-table">` whose rows carry data-delivery-* attributes and cells
[Delivery, "MonYY futprice", Change, Basis, Bid]. Basis is a dollar figure taken
straight from the Basis column.

Add a plant by dropping an entry in SITES (find its companyID by viewing the
bids page source: `const tableOptions = { ... companyID: NNNNN ... }`).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_API = "https://www.cihedging.com/cih/api/index.cfm/v2/origination/cashbids/{cid}/widget?{qs}"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, */*",
}

# One entry per physical location. commodity_ids "" = all commodities the widget
# exposes; otherwise mirror the page's tableOptions.commodityIDs (e.g. "1,4").
SITES: list[dict] = [
    {"provider": "Cardinal Ethanol", "location": "Colwich, KS", "state": "KS",
     "facility_type": "Corn Processing", "company_id": 165345, "commodity_ids": "",
     "origin": "https://www.cardinalethanol.com"},
    {"provider": "Cardinal Ethanol", "location": "Union City, IN", "state": "IN",
     "facility_type": "Corn Processing", "company_id": 25755, "commodity_ids": "1,4",
     "origin": "https://www.cardinalethanol.com"},
    {"provider": "Sandhills Renewables", "location": "Atkinson, NE", "state": "NE",
     "facility_type": "Corn Processing", "company_id": 146145, "commodity_ids": "",
     "origin": "https://www.sandhillsrenewables.com"},
]

_MONTH_CODES = {"Jan": "F", "Feb": "G", "Mar": "H", "Apr": "J", "May": "K", "Jun": "M",
                "Jul": "N", "Aug": "Q", "Sep": "U", "Oct": "V", "Nov": "X", "Dec": "Z"}

# Commodity name (upper) → (CME root, display grain). Milo/sorghum price vs corn.
_COMMODITY = {
    "CORN": ("ZC", "Corn"),
    "SOYBEANS": ("ZS", "Soybeans"), "SOYBEAN": ("ZS", "Soybeans"),
    "WHEAT": ("ZW", "Wheat"),
    "MILO": ("ZC", "Sorghum"), "SORGHUM": ("ZC", "Sorghum"),
}
# Short id prefix per root, so a row id is stable + unique within a location.
_PFX = {"ZC": "CN", "ZS": "SB", "ZW": "WH"}

_FUT_RE = re.compile(r"([A-Za-z]{3})\s+(\d{2})")


def _fut_symbol(root: str, futures_text: str) -> str | None:
    """'Sep 26 4.4000' → 'ZCU26' (root from the commodity section)."""
    m = _FUT_RE.match(futures_text.strip())
    if not m:
        return None
    code = _MONTH_CODES.get(m.group(1).title())
    if not code:
        return None
    return f"{root}{code}{m.group(2)}"


def _commodity(cname: str):
    up = (cname or "").upper()
    for key, val in _COMMODITY.items():
        if key in up:
            return val
    return None


def _fetch_widget(company_id: int, commodity_ids: str, origin: str) -> str:
    qs = urlencode({
        "commodity_ids": commodity_ids,
        "custom_commodity_ids": "",
        "exclude_non_custom": "false",
        "exclude_custom": "false",
        "address_ids": "",
        "show_cash_bid_title": "true",
        "show_cash_bid_filters": "true",
        "show_cash_bid_note": "true",
        "show_location_names": "true",
        "with_new_chart": "true",
    })
    headers = dict(_HEADERS)
    if origin:
        headers["Origin"] = origin
        headers["Referer"] = origin.rstrip("/") + "/"
    r = requests.post(_API.format(cid=company_id, qs=qs), headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()          # the endpoint returns the HTML grid as a JSON string


def parse_site(cfg: dict) -> NewSnapshotRequest | None:
    """Fetch + parse one SITES entry into a snapshot request (None if no bids)."""
    try:
        html = _fetch_widget(cfg["company_id"], cfg.get("commodity_ids", ""),
                             cfg.get("origin", ""))
    except Exception as exc:
        log.error("CIHedging fetch failed for %s (company %s): %s",
                  cfg["location"], cfg["company_id"], exc)
        return None

    soup = BeautifulSoup(html, "html.parser")
    rows: list[SnapshotRow] = []
    seen: set[str] = set()

    for com in soup.select("div.cih-com-row[data-commodity-name]"):
        info = _commodity(com.get("data-commodity-name"))
        if not info:
            continue
        root, grain = info
        pfx = _PFX.get(root, root)
        tbl = com.find("table", class_="cih-table")
        if not tbl:
            continue
        for tr in tbl.find_all("tr", attrs={"data-delivery-period-label": True}):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            label = (tr.get("data-delivery-period-label") or "").strip()
            year  = tr.get("data-delivery-year") or ""
            cme   = _fut_symbol(root, tds[1].get_text(" ", strip=True))
            if not cme:
                continue
            basis_txt = tds[3].get_text(strip=True).replace("+", "")
            try:
                basis_cents = int(round(float(basis_txt) * 100))
            except ValueError:
                continue
            # Keep a 4-digit year in the delivery label; half-month labels
            # ("Fh Aug"/"Lh Aug") gain the year from the row's data attribute.
            delivery = label if re.search(r"\d{4}", label) else (
                f"{label} {year}".strip() if year else label)
            del_key = "".join(c for c in delivery.upper() if c.isalnum())
            row_id  = f"{pfx}_{cme}_{del_key}"
            if row_id in seen:
                continue
            seen.add(row_id)
            rows.append(SnapshotRow(
                id=row_id, grain=grain, deliveryMonth=delivery,
                futuresSymbol=cme, basisCents=basis_cents, isSpot=False))

    if not rows:
        log.warning("CIHedging: no bids parsed for %s (company %s)",
                    cfg["location"], cfg["company_id"])
        return None

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                              location=cfg["location"], source="web", rows=rows)


def fetch_cihedging() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every SITES entry. Returns (snapshot requests, location metas)."""
    reqs: list[NewSnapshotRequest] = []
    metas: list[dict] = []
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
    reqs, metas = fetch_cihedging()
    print("=" * 60)
    for req in reqs:
        print(f"  {req.provider} · {req.location}  —  {len(req.rows)} row(s)")
        by_grain: dict[str, int] = {}
        for r in req.rows:
            by_grain[r.grain] = by_grain.get(r.grain, 0) + 1
        for g, n in by_grain.items():
            print(f"      {g:10s} {n}")
        for r in req.rows[:4]:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"        {r.deliveryMonth:10s} {r.futuresSymbol:7s} {sign}{r.basisCents}c")
