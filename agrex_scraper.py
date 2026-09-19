"""agrex_scraper.py — Agrex cash-bids terminal, now on Bushel's ASP.NET site.

Agrex re-platformed onto Bushel (bushel.agrexinc.com): the old
`cashbidsterminal.aspx?cbcntloc=<id>` GET is gone. The bid grid now loads per
LOCATION via an ASP.NET WebForms postback — pick a location in the dropdown and
the page re-renders that location's boards. We replicate that with requests: GET
the page once for the VIEWSTATE / EVENTVALIDATION tokens, then POST the form per
location with the location dropdown (`changeLocationDdl`) set to its id.

The rendered board is Bushel's standard cbCommodity markup (same shape
bushelsites_scraper parses): `<div class='cbCommodity'>` with an `<h3>` commodity
heading and `<ul class='sevenColumnsBigFirst'>` rows of `<li class='c1'..'c7'>` =
Delivery / Bid / Basis / Futures / Change / Futures Month / Last Trade. Grain and
CME root are read from the `<h3>` (so Hard Red / KCBT → KE, not ZW); basis is col
c3 (dollars → cents); the futures month + year come from c6.

Bushel hosts a few non-Agrex clients on the same terminal (Oracle Pork Nutrition,
Western New York Energy) — kept here under their own provider names.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_PAGE = "https://bushel.agrexinc.com/cash-bids"          # GET → form tokens
_POST = "https://bushel.agrexinc.com/cashBids.aspx?ksc=1"  # form action (postback)
_SEL = "ctl00$MainContent$ctl00$changeLocationDdl_2"       # location dropdown name
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")}

# Dropdown option value (cbcntloc id) -> location metadata. The site's dropdown
# carries these ids; non-Agrex tenants keep their own provider name.
LOCATIONS: dict[str, dict] = {
    "2419": {"provider": "Agrex", "location": "Mobile, AL", "state": "AL",
             "facility_type": "Export Terminal"},
    "2421": {"provider": "Agrex", "location": "Montgomery, AL", "state": "AL",
             "facility_type": "River Terminal"},
    "2422": {"provider": "Agrex", "location": "Superior, NE", "state": "NE",
             "facility_type": "Country Elevator"},
    "2444": {"provider": "Agrex", "location": "Columbus Grove, OH", "state": "OH",
             "facility_type": "Country Elevator"},
    "2449": {"provider": "Oracle Pork Nutrition", "location": "Peru, IN", "state": "IN",
             "facility_type": "Feed Mill"},
    "3575": {"provider": "Western New York Energy", "location": "Medina, NY", "state": "NY",
             "facility_type": "Corn Processing"},
}

_MON = {"jan": "F", "feb": "G", "mar": "H", "apr": "J", "may": "K", "jun": "M",
        "jul": "N", "aug": "Q", "sep": "U", "sept": "U", "oct": "V", "nov": "X", "dec": "Z"}


def _root_grain(comm: str):
    """(CME root, display grain) from a commodity heading like 'Soft Red Wheat'."""
    u = (comm or "").upper()
    if "CORN" in u:
        return "ZC", "Corn"
    if "SOYBEAN" in u or "SOY" in u:
        return "ZS", "Soybeans"
    if "MILO" in u or "SORGHUM" in u:
        return "ZC", "Sorghum"
    if "KCBT" in u or "HARD RED" in u or "HRW" in u or "KC " in u:   # Kansas City HRW
        return "KE", "Wheat"
    if "MGEX" in u or "SPRING" in u:                                # Minneapolis spring
        return "MW", "Wheat"
    if "WHEAT" in u:                                                # Chicago SRW default
        return "ZW", "Wheat"
    return None, None


def _symbol(root: str, fut_month: str):
    """'Jul 27' / 'Jul 27 Wheat' -> '<root>N27'. Root comes from the h3."""
    m = re.search(r"([A-Za-z]{3})[a-z]*\s+(\d{2})", fut_month or "")
    if not m:
        return None
    code = _MON.get(m.group(1).lower())
    return f"{root}{code}{m.group(2)}" if code else None


def _cents(s: str):
    try:
        return int(round(float((s or "").replace("+", "").strip()) * 100))
    except (ValueError, AttributeError):
        return None


def _parse_fields(html: str) -> dict:
    """All form field name->value from a page's HTML: the chunked VIEWSTATE,
    EVENTVALIDATION, app hidden fields, and each select's current value."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError("Agrex: no <form> on the cash-bids page")
    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        n = inp.get("name")
        if n:
            fields[n] = inp.get("value", "")
    for sel in form.find_all("select"):
        n = sel.get("name")
        if n:
            opt = sel.find("option", selected=True) or sel.find("option")
            fields[n] = opt.get("value", "") if opt else ""
    return fields


def _parse_boards(html: str) -> list[SnapshotRow]:
    """Parse the cbCommodity boards from a location's postback HTML."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for div in soup.find_all("div", class_="cbCommodity"):
        h3 = div.find("h3")
        root, grain = _root_grain(h3.get_text(" ", strip=True) if h3 else "")
        if not root:
            continue
        for ul in div.find_all("ul"):
            cells = {li.get("class", ["?"])[0]: li.get_text(" ", strip=True)
                     for li in ul.find_all("li")}
            deliv = cells.get("c1", "")
            if not deliv or deliv.lower() == "delivery":   # header/blank row
                continue
            sym = _symbol(root, cells.get("c6", ""))
            cents = _cents(cells.get("c3", ""))
            if sym is None or cents is None:
                continue
            dkey = "".join(ch for ch in deliv.upper() if ch.isalnum()) or sym
            rid = f"{root}_{sym}_{dkey}"
            if rid in seen:
                continue
            seen.add(rid)
            rows.append(SnapshotRow(id=rid, grain=grain, deliveryMonth=deliv,
                                    futuresSymbol=sym, basisCents=cents, isSpot=False))
    return rows


def _postback(sess: requests.Session, fields: dict, loc_id: str) -> str:
    """POST the location-change postback and return the response HTML."""
    data = dict(fields)
    data["__EVENTTARGET"] = _SEL
    data["__EVENTARGUMENT"] = ""
    data[_SEL] = loc_id
    return sess.post(_POST, data=data, timeout=30).text


def fetch_agrex_bids() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every configured Agrex/Bushel location.

    A FRESH session per location — the site tracks the selected location in
    server-side state, so reusing one session bleeds one location's board into the
    next. And the dropdown's default location needs a sentinel hop (post a different
    location first, then back) because re-posting the already-selected value isn't
    treated as a change and returns an empty board.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    reqs, metas = [], []
    for loc_id, cfg in LOCATIONS.items():
        sess = requests.Session()
        sess.headers.update(_HEADERS)
        try:
            fields = _parse_fields(sess.get(_PAGE, timeout=30).text)
            if loc_id == fields.get(_SEL, ""):        # target is the page default
                sentinel = next((k for k in LOCATIONS if k != loc_id), loc_id)
                fields = _parse_fields(_postback(sess, fields, sentinel))
            html = _postback(sess, fields, loc_id)
        except Exception as exc:
            log.error("Agrex fetch failed for %s: %s", cfg["location"], exc)
            continue
        rows = _parse_boards(html)
        if not rows:
            log.warning("Agrex: no bids parsed for %s", cfg["location"])
            continue
        reqs.append(NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                                       location=cfg["location"], source="web", rows=rows))
        metas.append({"provider": cfg["provider"], "location": cfg["location"],
                      "state": cfg.get("state"), "facility_type": cfg.get("facility_type")})
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, metas = fetch_agrex_bids()
    for req in reqs:
        print(f"  {req.provider} · {req.location} — {len(req.rows)} row(s)")
        for r in req.rows:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"     {r.deliveryMonth:16s} {r.futuresSymbol:7s} {sign}{r.basisCents}c  {r.grain}")
