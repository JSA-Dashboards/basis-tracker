"""
The Andersons Grain cash-bid scraper.

Uses a session-based approach with ASP.NET anti-forgery (CSRF) tokens to POST
each location — with an empty SelectedCommodity, which causes the site to add
ALL commodities for that location at once.  One POST per location = 18 total.

Base URL: https://www.andersonsgrain.com/locations/cash-bids/

Usage (standalone test):
    python andersons_scraper.py
"""
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_BASE_URL     = "https://www.andersonsgrain.com/locations/cash-bids/"
_ADD_URL      = "https://www.andersonsgrain.com/locations/cash-bids/Add"
_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": _BASE_URL,
}
REQUEST_DELAY = 0.25   # seconds between POSTs

# ── Location metadata ─────────────────────────────────────────────────────────
# Keys are the numeric location IDs used by the ASP.NET form.
ANDERSONS_LOCATIONS: dict[str, dict] = {
    "40":    {"name": "Toledo - Edwin Dr",   "state": "OH", "city": "Toledo"},
    "948":   {"name": "Toledo - Kuhlman Dr", "state": "OH", "city": "Toledo"},
    "1009":  {"name": "Metamora",            "state": "OH", "city": "Metamora"},
    "1027":  {"name": "Greenville",          "state": "OH", "city": "Greenville"},
    "1052":  {"name": "Auburn",              "state": "IN", "city": "Auburn"},
    "1062":  {"name": "Hemlock",             "state": "MI", "city": "Hemlock"},
    "1069":  {"name": "Oakley",              "state": "MI", "city": "Oakley"},
    "1076":  {"name": "Standish",            "state": "MI", "city": "Standish"},
    "1091":  {"name": "Reading",             "state": "MI", "city": "Reading"},
    "1098":  {"name": "White Pigeon",        "state": "MI", "city": "White Pigeon"},
    "1108":  {"name": "Dunkirk",             "state": "OH", "city": "Dunkirk"},
    "1118":  {"name": "Oakville",            "state": "MI", "city": "Oakville"},
    "1138":  {"name": "Delphi",              "state": "IN", "city": "Delphi"},
    "1145":  {"name": "Clymers",             "state": "IN", "city": "Clymers"},
    "1162":  {"name": "Denison",             "state": "IA", "city": "Denison"},
    "1396":  {"name": "Maumee",              "state": "OH", "city": "Maumee"},
    "12194": {"name": "Albion",              "state": "MI", "city": "Albion"},
    "55689": {"name": "Port William",        "state": "OH", "city": "Port William"},
}


# ── Private helpers ────────────────────────────────────────────────────────────

def _get_csrf_token(session: requests.Session) -> str:
    """GET the page and return the ASP.NET anti-forgery token value."""
    r = session.get(_BASE_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    m = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
        r.text,
    )
    if not m:
        raise ValueError("__RequestVerificationToken not found on page")
    return m.group(1)


def _parse_tables(html: str) -> list[dict]:
    """
    Parse all <table class="styled-table"> elements from the accumulated page HTML.

    Each table's caption contains a remove link like:
        /locations/cash-bids/remove?location=40&commodity=Red+Wheat

    Returns a list of table dicts:
        {
            "location_id": str,
            "commodity":   str,   # e.g. "Corn", "Soybean", "Red Wheat"
            "rows": [
                {
                    "delivery": str,   # e.g. "JUN 26", "ON 26"
                    "bid":      str,   # e.g. "4.2400"
                    "basis":    str,   # e.g. "0.15" or "-0.80"
                    "futures":  str,   # e.g. "4.0900"
                    "symbol":   str,   # e.g. "CN26", "CZ26"
                },
            ],
        }
    """
    soup    = BeautifulSoup(html, "html.parser")
    tables  = soup.find_all("table", class_="styled-table")
    results = []

    for t in tables:
        # Identify location_id and commodity from the remove-link href
        a_tag = t.find("a", href=lambda h: h and "location=" in h)
        if not a_tag:
            continue

        href   = a_tag["href"]
        loc_m  = re.search(r"location=(\d+)", href)
        comm_m = re.search(r"commodity=([^&]+)", href)
        if not loc_m:
            continue

        loc_id    = loc_m.group(1)
        commodity = unquote_plus(comm_m.group(1).strip()) if comm_m else "Unknown"

        tbody = t.find("tbody")
        if not tbody:
            continue

        row_data = []
        for tr in tbody.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            # Expected columns: Delivery, Bid, Basis, Futures, Chg, Symbol, Last Trade
            if len(cells) < 6:
                continue
            row_data.append({
                "delivery": cells[0],
                "bid":      cells[1],
                "basis":    cells[2],
                "futures":  cells[3],
                "symbol":   cells[5],
            })

        if row_data:
            results.append({
                "location_id": loc_id,
                "commodity":   commodity,
                "rows":        row_data,
            })

    return results


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_andersons_bids(loc_ids: list[str] | None = None) -> list[dict]:
    """
    Fetch The Andersons cash bids for all (or specified) locations.

    Makes one GET (CSRF token) + one POST per location.  POSTing with
    SelectedCommodity="" adds ALL commodities for that location at once,
    so the final page accumulates all tables.

    Args:
        loc_ids: list of location-ID strings to scrape, or None for all 18.

    Returns a list of per-location dicts:
        {
            "location_id":   str,
            "location_name": str,          # e.g. "Auburn"
            "state":         str,          # e.g. "IN"
            "city":          str,
            "timestamp":     str,          # ISO-8601 UTC date-normalised
            "cashbids": [
                {
                    "commodity":  str,     # raw name e.g. "Corn", "Red Wheat"
                    "delivery":   str,     # e.g. "JUN 26", "ON 26"
                    "basis":      str,     # e.g. "0.15" or "-0.80"
                    "symbol":     str,     # e.g. "CN26", "CZ26"
                },
                ...
            ],
        }

    Locations with no bid rows are excluded from the results.
    """
    if loc_ids is None:
        loc_ids = list(ANDERSONS_LOCATIONS.keys())

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    session = requests.Session()
    session.headers.update(_HEADERS)

    # ── Get initial CSRF token ────────────────────────────────────────────────
    try:
        token = _get_csrf_token(session)
    except Exception as exc:
        log.error("Andersons: failed to fetch CSRF token: %s", exc)
        return []

    log.info("Andersons: posting %d location(s)…", len(loc_ids))

    last_html = ""

    for loc_id in loc_ids:
        try:
            r = session.post(
                _ADD_URL,
                data={
                    "__RequestVerificationToken": token,
                    "SelectedLocation":           loc_id,
                    "SelectedCommodity":          "",   # empty = all commodities
                },
                headers={"Referer": _BASE_URL},
                timeout=25,
                allow_redirects=True,
            )
            r.raise_for_status()

            # ASP.NET rotates the anti-forgery token on each response; capture it
            m = re.search(
                r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
                r.text,
            )
            if m:
                token = m.group(1)
            last_html = r.text

            loc_meta = ANDERSONS_LOCATIONS.get(loc_id, {})
            log.debug("  posted loc %s (%s)", loc_id, loc_meta.get("name", "?"))

        except Exception as exc:
            log.warning("  Andersons POST failed for loc %s: %s", loc_id, exc)

        time.sleep(REQUEST_DELAY)

    if not last_html:
        log.error("Andersons: no HTML received from any POST")
        return []

    # ── Parse all accumulated tables ──────────────────────────────────────────
    all_tables = _parse_tables(last_html)

    # Group by location_id
    by_loc: dict[str, dict] = {}
    for tbl in all_tables:
        lid = tbl["location_id"]
        if lid not in by_loc:
            meta = ANDERSONS_LOCATIONS.get(lid, {})
            by_loc[lid] = {
                "location_id":   lid,
                "location_name": meta.get("name", f"Location {lid}"),
                "state":         meta.get("state", ""),
                "city":          meta.get("city", ""),
                "timestamp":     today_utc,
                "cashbids":      [],
            }
        for row in tbl["rows"]:
            by_loc[lid]["cashbids"].append({
                "commodity": tbl["commodity"],
                "delivery":  row["delivery"],
                "basis":     row["basis"],
                "symbol":    row["symbol"],
            })

    results = [v for v in by_loc.values() if v["cashbids"]]
    log.info(
        "Andersons scrape complete: %d location(s), %d total bids",
        len(results), sum(len(l["cashbids"]) for l in results),
    )
    return results


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    sys.path.insert(0, str(Path(__file__).parent))
    from parsers.andersons_parser import parse_andersons_location

    locs = fetch_andersons_bids()
    print("=" * 60)
    print(f"Total locations: {len(locs)}")
    print("=" * 60)
    total_rows = 0
    for loc in locs:
        snap = parse_andersons_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {loc['state']:2s}  {len(snap.rows):2d} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(
                    f"    {r.grain:12s}  {r.deliveryMonth:12s}  "
                    f"{r.futuresSymbol:8s}  {sign}{r.basisCents}c"
                )
            total_rows += len(snap.rows)
        else:
            print(f"  {loc['location_name']:25s}  (no valid bids)")
    print(f"\nTotal rows: {total_rows}")
