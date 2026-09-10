"""
mnsoy_scraper.py — Minnesota Soybean Processors (MNSP) soybean bid scraper.

Uses the CIHedging.com cash-bid widget API (customer ID 145642).
The API returns an HTML fragment as a JSON string; we parse the Soybeans
table from that fragment.

Single location: Brewster, MN.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# CIHedging retired the old /origination/cashbids/<id> path (now HTTP 500). The
# live widget is the v2 endpoint (same one cihedging_scraper.py uses), returning
# the bid grid as a JSON-encoded HTML string.
_API        = "https://www.cihedging.com/cih/api/index.cfm/v2/origination/cashbids/{cid}/widget?{qs}"
_COMPANY_ID = 145642

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept":       "application/json, */*",
    "Origin":       "https://mnsoy.com",
    "Referer":      "https://mnsoy.com/",
}

# CME month letter codes
_MONTH_CODES: dict[str, str] = {
    "Jan": "F", "Feb": "G", "Mar": "H", "Apr": "J",
    "May": "K", "Jun": "M", "Jul": "N", "Aug": "Q",
    "Sep": "U", "Oct": "V", "Nov": "X", "Dec": "Z",
}

def _fut_to_cme(text: str, grain_root: str = "ZS") -> str | None:
    """v2 futures cell 'Nov 26 …' → CME symbol 'ZSX26'."""
    m = re.match(r"([A-Za-z]{3})\s+(\d{2})", text.strip())
    if not m:
        return None
    code = _MONTH_CODES.get(m.group(1).title())
    return f"{grain_root}{code}{m.group(2)}" if code else None


def _widget_qs() -> str:
    return urlencode({
        "commodity_ids": "", "custom_commodity_ids": "", "exclude_non_custom": "false",
        "exclude_custom": "false", "address_ids": "", "show_cash_bid_title": "true",
        "show_cash_bid_filters": "true", "show_cash_bid_note": "true",
        "show_location_names": "true", "with_new_chart": "true",
    })


def fetch_mnsoy_bids() -> list[dict]:
    """
    Fetch MNSP soybean bids from the CIHedging v2 widget (company 145642).

    Parses ONLY the "Soybeans" commodity section — the widget also exposes
    "Soybean Meal" ($/ton) and "Soybean Pellets", which must NOT be mixed into the
    ¢/bu soybean basis. Returns a single-location list:
        {"location": "Brewster", "timestamp": str,
         "bids": [{"delivery", "cme_symbol", "basis_cents"}]}
    Empty list on fetch/parse failure.
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    url = _API.format(cid=_COMPANY_ID, qs=_widget_qs())
    try:
        resp = requests.post(url, headers=_HEADERS, timeout=25)
        resp.raise_for_status()
        html: str = resp.json()   # endpoint returns the HTML grid as a JSON string
    except Exception as exc:
        log.error("MNSP: fetch failed: %s", exc)
        return []

    soup = BeautifulSoup(html, "html.parser")
    bids: list[dict] = []
    seen: set[str] = set()
    for com in soup.select("div.cih-com-row[data-commodity-name]"):
        if (com.get("data-commodity-name") or "").strip().lower() != "soybeans":
            continue                                   # skip Soybean Meal / Pellets
        tbl = com.find("table", class_="cih-table")
        if not tbl:
            continue
        for tr in tbl.find_all("tr", attrs={"data-delivery-period-label": True}):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            label = (tr.get("data-delivery-period-label") or "").strip()
            year  = tr.get("data-delivery-year") or ""
            cme   = _fut_to_cme(tds[1].get_text(" ", strip=True), "ZS")
            if not cme:
                continue
            basis_txt = tds[3].get_text(strip=True).replace("+", "")
            try:
                basis_cents = int(round(float(basis_txt) * 100))
            except ValueError:
                continue
            delivery = label if re.search(r"\d{4}", label) else (
                f"{label} {year}".strip() if year else label)
            key = f"{delivery}|{cme}"
            if key in seen:
                continue
            seen.add(key)
            bids.append({"delivery": delivery, "cme_symbol": cme,
                         "basis_cents": basis_cents})

    if not bids:
        log.warning("MNSP: no soybean bids parsed (widget layout may have changed)")
        return []

    log.info("MNSP Brewster  %d soybean bid(s)", len(bids))
    return [{"location": "Brewster", "timestamp": today_ts, "bids": bids}]


if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sys.path.insert(0, str(Path(__file__).parent))
    from parsers.mnsoy_parser import parse_mnsoy_location

    locs = fetch_mnsoy_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_mnsoy_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.deliveryMonth:22s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
