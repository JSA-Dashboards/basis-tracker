"""
Bunge AG cash-bid scraper.

Each Bunge location page (https://www.bungeag.com/locations/{slug}/) contains
server-rendered HTML tables inside a #cashbids div.  No API or session required —
one GET per location.

Usage (standalone test):
    python bunge_scraper.py
"""
import logging
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup, NavigableString

log = logging.getLogger(__name__)

_BASE_URL     = "https://www.bungeag.com/locations/"
_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
REQUEST_DELAY = 0.30   # seconds between GETs

# ── Location metadata ─────────────────────────────────────────────────────────
# Key  = URL slug (after /locations/)
# name = display name  |  state = 2-letter state code  |  city = city name
BUNGE_LOCATIONS: dict[str, dict] = {
    "bellevue-oh":             {"name": "Bellevue, OH",               "state": "OH", "city": "Bellevue"},
    "cairo-il":                {"name": "Cairo, IL",                  "state": "IL", "city": "Cairo"},
    "chester-mt":              {"name": "Chester, MT",                "state": "MT", "city": "Chester"},
    "councilbluffs-ia":        {"name": "Council Bluffs, IA",         "state": "IA", "city": "Council Bluffs"},
    "decatur-al":              {"name": "Decatur, AL",                "state": "AL", "city": "Decatur"},
    "decatur-in":              {"name": "Decatur, IN",                "state": "IN", "city": "Decatur"},
    "delphos-oh":              {"name": "Delphos, OH",                "state": "OH", "city": "Delphos"},
    "emporia-ks":              {"name": "Emporia, KS",                "state": "KS", "city": "Emporia"},
    "fairmontcity-il":         {"name": "Fairmont City, IL",          "state": "IL", "city": "Fairmont City"},
    "griggsville-il":          {"name": "Griggsville, IL",            "state": "IL", "city": "Griggsville"},
    "indianapolis-in":         {"name": "Indianapolis, IN",           "state": "IN", "city": "Indianapolis"},
    "kintyreflats-mt":         {"name": "Kintyre Flats, MT",          "state": "MT", "city": "Kintyre Flats"},
    "longview-wa":             {"name": "Longview, WA",               "state": "WA", "city": "Longview"},
    "maschhoffs":              {"name": "Maschhoff's",                "state": "IL", "city": "Carlyle"},
    "mcleansboro-il":          {"name": "McLeansboro, IL",            "state": "IL", "city": "McLeansboro"},
    "morristown-in-beanplant": {"name": "Morristown, IN - Bean Plant","state": "IN", "city": "Morristown"},
    "morristown-in-grainelev": {"name": "Morristown, IN - Grain Elev","state": "IN", "city": "Morristown"},
    "rosehill-nc":             {"name": "Rose Hill, NC",              "state": "NC", "city": "Rose Hill"},
    "sidney-mt":               {"name": "Sidney, MT",                 "state": "MT", "city": "Sidney"},
    "tunis-mt":                {"name": "Tunis, MT",                  "state": "MT", "city": "Tunis"},
    # River canola barge locations (may have no standard bid tables)
    "lower-mississippi-river": {"name": "Lower Mississippi River",    "state": "",   "city": ""},
    "mid-mississippi-river":   {"name": "Mid-Mississippi River",      "state": "",   "city": ""},
    "ohio-river":              {"name": "Ohio River",                 "state": "",   "city": ""},
    "tennessee-river":         {"name": "Tennessee River",            "state": "",   "city": ""},
}


# ── Private helpers ────────────────────────────────────────────────────────────

def _parse_location_page(html: str) -> list[dict]:
    """
    Parse the #cashbids div from a Bunge location page.

    Returns a list of commodity dicts:
        {
            "commodity":  str,   # tab-pane id, e.g. "soybeans", "dns-14"
            "rows": [
                {
                    "delivery": str,   # e.g. "By June 26", "LH July", "HARV26"
                    "symbol":   str,   # DTN format, e.g. "@SN26", "@MWN26"
                    "basis":    str,   # e.g. "0.3000" or "-0.8500"
                },
                ...
            ],
        }

    Note: the first <td> in each row uses malformed HTML (no closing tag before
    the next <td>), so we extract delivery text from NavigableString nodes only.
    """
    soup     = BeautifulSoup(html, "html.parser")
    cashbids = soup.find(id="cashbids")
    if not cashbids:
        return []

    results = []
    for tab in cashbids.find_all("div", class_="tab-pane"):
        commodity = tab.get("id", "").strip()
        if not commodity:
            continue

        table = tab.find("table")
        if not table:
            continue
        tbody = table.find("tbody")
        if not tbody:
            continue

        row_data = []
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue

            # First td has malformed HTML — grab only direct text nodes
            delivery = "".join(
                c for c in tds[0].contents if isinstance(c, NavigableString)
            ).strip()
            symbol = tds[1].get_text(strip=True)
            basis  = tds[3].get_text(strip=True)

            if delivery and symbol and basis:
                row_data.append({
                    "delivery": delivery,
                    "symbol":   symbol,
                    "basis":    basis,
                })

        if row_data:
            results.append({"commodity": commodity, "rows": row_data})

    return results


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_bunge_bids(slugs: list[str] | None = None) -> list[dict]:
    """
    Fetch Bunge cash bids by scraping each location page.

    Args:
        slugs: list of location slugs to scrape, or None for all locations.

    Returns a list of per-location dicts:
        {
            "slug":          str,           # URL slug
            "location_name": str,           # display name
            "state":         str,           # e.g. "IN"
            "city":          str,
            "timestamp":     str,           # ISO-8601 UTC date-normalised
            "cashbids": [
                {
                    "commodity":  str,      # tab id, e.g. "soybeans", "dns-14"
                    "delivery":   str,      # e.g. "By June 26", "HARV26"
                    "symbol":     str,      # DTN format, e.g. "@SN26"
                    "basis":      str,      # e.g. "0.3000" or "-0.8500"
                },
                ...
            ],
        }

    Locations with no bid rows are excluded.
    """
    if slugs is None:
        slugs = list(BUNGE_LOCATIONS.keys())

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    session   = requests.Session()
    session.headers.update(_HEADERS)

    results = []

    for slug in slugs:
        meta = BUNGE_LOCATIONS.get(slug, {})
        url  = f"{_BASE_URL}{slug}/"

        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
        except Exception as exc:
            log.warning("  Bunge GET failed for %s: %s", slug, exc)
            time.sleep(REQUEST_DELAY)
            continue

        commodities = _parse_location_page(r.text)
        if not commodities:
            log.debug("  %s: no bid tables", slug)
            time.sleep(REQUEST_DELAY)
            continue

        cashbids: list[dict] = []
        for comm in commodities:
            for row in comm["rows"]:
                cashbids.append({
                    "commodity": comm["commodity"],
                    "delivery":  row["delivery"],
                    "symbol":    row["symbol"],
                    "basis":     row["basis"],
                })

        results.append({
            "slug":          slug,
            "location_name": meta.get("name", slug),
            "state":         meta.get("state", ""),
            "city":          meta.get("city", ""),
            "timestamp":     today_utc,
            "cashbids":      cashbids,
        })
        log.debug("  %s: %d bid(s)", slug, len(cashbids))
        time.sleep(REQUEST_DELAY)

    log.info(
        "Bunge scrape complete: %d location(s), %d total bids",
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
    from parsers.bunge_parser import parse_bunge_location

    locs = fetch_bunge_bids()
    print("=" * 60)
    print(f"Total locations: {len(locs)}")
    print("=" * 60)
    total_rows = 0
    for loc in locs:
        snap = parse_bunge_location(loc)
        if snap:
            print(f"  {snap.location:35s}  {loc['state']:2s}  {len(snap.rows):2d} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(
                    f"    {r.grain:18s}  {r.deliveryMonth:22s}  "
                    f"{r.futuresSymbol:8s}  {sign}{r.basisCents}c"
                )
            total_rows += len(snap.rows)
        else:
            print(f"  {loc['location_name']:35s}  (no valid bids)")
    print(f"\nTotal rows: {total_rows}")
