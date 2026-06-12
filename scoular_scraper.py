"""
Scoular cash-bid scraper.

Each Scoular location page (https://www.scoularview.com/cashbidssingle-{id})
contains server-rendered HTML in a .cashBidLocation div (Bushel-powered).
No API or session required -- one GET per location ID.

Usage (standalone test):
    python scoular_scraper.py
"""
import logging
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_BASE_URL     = "https://www.scoularview.com/cashbidssingle-"
_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
REQUEST_DELAY = 0.30   # seconds between GETs

# ── Location metadata ─────────────────────────────────────────────────────────
# Key = numeric Bushel location ID
# name = display fallback  |  state = 2-letter code  |  city = city name
SCOULAR_LOCATIONS: dict[int, dict] = {
    # Illinois
    1889: {"name": "Andres, IL",             "state": "IL", "city": "Andres"},
    1941: {"name": "Waverly, IL - Shuttle",  "state": "IL", "city": "Waverly"},
    1942: {"name": "Pisgah, IL",             "state": "IL", "city": "Pisgah"},
    1943: {"name": "Palmyra, IL",            "state": "IL", "city": "Palmyra"},
    1944: {"name": "Waverly, IL - Elevator", "state": "IL", "city": "Waverly"},
    # Kentucky
    1894: {"name": "Owensboro, KY",          "state": "KY", "city": "Owensboro"},
    2084: {"name": "Adairville, KY",         "state": "KY", "city": "Adairville"},
    # Colorado
    1918: {"name": "Haswell, CO",            "state": "CO", "city": "Haswell"},
    1916: {"name": "Idalia, CO",             "state": "CO", "city": "Idalia"},
    1911: {"name": "Julesburg, CO",          "state": "CO", "city": "Julesburg"},
    # South Carolina
    1939: {"name": "Charleston, SC",         "state": "SC", "city": "Charleston"},
    # North Carolina
    2117: {"name": "Wilmington, NC",         "state": "NC", "city": "Wilmington"},
    # Florida
    2492: {"name": "Lake City, FL",          "state": "FL", "city": "Lake City"},
    # Idaho
    1934: {"name": "Jerome, ID",             "state": "ID", "city": "Jerome"},
    # Montana
    1935: {"name": "Butte, MT",              "state": "MT", "city": "Butte"},
    2631: {"name": "Logan, MT",              "state": "MT", "city": "Logan"},
    # Iowa
    1950: {"name": "Corley, IA",             "state": "IA", "city": "Corley"},
    1952: {"name": "Emerson, IA",            "state": "IA", "city": "Emerson"},
    1953: {"name": "Griswold, IA",           "state": "IA", "city": "Griswold"},
    1954: {"name": "Hancock, IA",            "state": "IA", "city": "Hancock"},
    1955: {"name": "Portsmouth, IA",         "state": "IA", "city": "Portsmouth"},
    3508: {"name": "AGP - Manning",          "state": "IA", "city": "Manning"},
    3509: {"name": "Bunge - Council Bluffs", "state": "IA", "city": "Council Bluffs"},
    # Kansas
    1945: {"name": "Ada & Minneapolis, KS",  "state": "KS", "city": "Minneapolis"},
    2519: {"name": "Brewster, KS",           "state": "KS", "city": "Brewster"},
    1913: {"name": "Cheyenne County, KS",    "state": "KS", "city": "Cheyenne County"},
    1912: {"name": "Coolidge, KS",           "state": "KS", "city": "Coolidge"},
    2813: {"name": "Cullison, KS",           "state": "KS", "city": "Cullison"},
    1914: {"name": "Downs, KS",              "state": "KS", "city": "Downs"},
    1915: {"name": "Goodland, KS",           "state": "KS", "city": "Goodland"},
    2147: {"name": "Goodland Crush, KS",     "state": "KS", "city": "Goodland"},
    2814: {"name": "Greensburg, KS",         "state": "KS", "city": "Greensburg"},
    2526: {"name": "Lincoln, KS",            "state": "KS", "city": "Lincoln"},
    2520: {"name": "Monument, KS",           "state": "KS", "city": "Monument"},
    2518: {"name": "Oakley, KS",             "state": "KS", "city": "Oakley"},
    1929: {"name": "Pittsburg, KS",          "state": "KS", "city": "Pittsburg"},
    1920: {"name": "Pratt, KS",              "state": "KS", "city": "Pratt"},
    1921: {"name": "Salina, KS",             "state": "KS", "city": "Salina"},
    1925: {"name": "SW Plains, KS",          "state": "KS", "city": ""},
    2815: {"name": "Trousdale, KS",          "state": "KS", "city": "Trousdale"},
    1923: {"name": "Wellington, KS",         "state": "KS", "city": "Wellington"},
    1924: {"name": "Winona, KS",             "state": "KS", "city": "Winona"},
    1922: {"name": "Tribune - Main, KS",     "state": "KS", "city": "Tribune"},
    1947: {"name": "Tribune - Inland, KS",   "state": "KS", "city": "Tribune"},
    1949: {"name": "Tribune - Horace, KS",   "state": "KS", "city": "Tribune"},
    1948: {"name": "Tribune - NW Yard, KS",  "state": "KS", "city": "Tribune"},
    # Minnesota
    2344: {"name": "Mentor, MN",             "state": "MN", "city": "Mentor"},
    # Missouri
    1926: {"name": "Adrian, MO",             "state": "MO", "city": "Adrian"},
    1928: {"name": "Butterfield, MO",        "state": "MO", "city": "Butterfield"},
    1927: {"name": "Harrisonville, MO",      "state": "MO", "city": "Harrisonville"},
    # Nebraska
    1907: {"name": "Fremont, NE",            "state": "NE", "city": "Fremont"},
    1910: {"name": "Sidney, NE",             "state": "NE", "city": "Sidney"},
    1899: {"name": "Lamar, NE",              "state": "NE", "city": "Lamar"},
    1900: {"name": "North Grant, NE",        "state": "NE", "city": "Grant"},
    1901: {"name": "South Grant, NE",        "state": "NE", "city": "Grant"},
    1896: {"name": "Venango, NE",            "state": "NE", "city": "Venango"},
    1903: {"name": "Grainton, NE",           "state": "NE", "city": "Grainton"},
    1904: {"name": "Hershey, NE",            "state": "NE", "city": "Hershey"},
    1902: {"name": "Madrid, NE",             "state": "NE", "city": "Madrid"},
    3329: {"name": "Madrid Ethanol, NE",     "state": "NE", "city": "Madrid"},
    1905: {"name": "Paxton, NE",             "state": "NE", "city": "Paxton"},
    1906: {"name": "Wallace, NE",            "state": "NE", "city": "Wallace"},
    # Ohio
    2347: {"name": "Covington, OH",          "state": "OH", "city": "Covington"},
    # Virginia
    1937: {"name": "Port of Richmond, VA",   "state": "VA", "city": "Richmond"},
    1938: {"name": "Weyers Cave, VA",        "state": "VA", "city": "Weyers Cave"},
    1940: {"name": "Windsor, VA",            "state": "VA", "city": "Windsor"},
}


# ── Private helpers ────────────────────────────────────────────────────────────

def _parse_location_page(html: str) -> tuple[str, list[dict]]:
    """
    Parse the .cashBidLocation div from a Scoular cashbidssingle page.

    Returns:
        (location_name, commodities)

    commodities is a list of dicts:
        {
            "commodity": str,   # h3 abbreviation, e.g. "YSB", "HRWW", "YC"
            "rows": [
                {
                    "delivery":      str,  # e.g. "June 2026", "Cash", "New Crop 2026"
                    "bid":           str,  # e.g. "10.78"
                    "basis":         str,  # e.g. "-0.36"
                    "futures_month": str,  # e.g. "Jul 26 Soybeans", "Jul 26 KCBT Red Wheat"
                },
                ...
            ],
        }

    Page structure:
        .cashBidLocation
          h2                   -- location name
          .cbCommodity         -- one per grain type
            h3                 -- commodity abbreviation (YSB, HRWW, YC, ...)
            ul.fcControlsSubHdr -- column headers (skip)
            ul.fcControls1/2/… -- data rows
              li.c1  span.cashBidsColumnOne -- delivery label
              li.c2  -- bid price
              li.c3  -- basis
              li.c4  -- futures price
              li.c5  -- change
              li.c6  -- futures month text (used to derive CME symbol)
              li.c7  -- last trade time
    """
    soup    = BeautifulSoup(html, "html.parser")
    loc_div = soup.find(class_="cashBidLocation")
    if not loc_div:
        return ("", [])

    h2 = loc_div.find("h2")
    location_name = h2.get_text(strip=True) if h2 else ""

    commodities = []
    for cb in loc_div.find_all(class_="cbCommodity"):
        h3   = cb.find("h3")
        comm = h3.get_text(strip=True) if h3 else ""
        if not comm:
            continue

        rows = []
        for ul in cb.find_all("ul"):
            ul_classes = " ".join(ul.get("class") or [])
            if "SubHdr" in ul_classes:
                continue          # skip the header row

            lis = ul.find_all("li")
            if len(lis) < 6:
                continue

            # c1: delivery via .cashBidsColumnOne span
            span_c1 = lis[0].find(class_="cashBidsColumnOne")
            delivery = span_c1.get_text(strip=True) if span_c1 else ""

            bid          = lis[1].get_text(strip=True)
            basis        = lis[2].get_text(strip=True)
            futures_month = lis[5].get_text(strip=True)

            if delivery and basis and futures_month:
                rows.append({
                    "delivery":      delivery,
                    "bid":           bid,
                    "basis":         basis,
                    "futures_month": futures_month,
                })

        if rows:
            commodities.append({"commodity": comm, "rows": rows})

    return (location_name, commodities)


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_scoular_bids(loc_ids: list[int] | None = None) -> list[dict]:
    """
    Fetch Scoular cash bids by scraping each location's cashbidssingle page.

    Args:
        loc_ids: list of numeric location IDs to scrape, or None for all.

    Returns a list of per-location dicts:
        {
            "loc_id":        int,
            "location_name": str,    # scraped from page h2
            "state":         str,    # from SCOULAR_LOCATIONS metadata
            "city":          str,
            "timestamp":     str,    # ISO-8601 UTC date-normalised
            "cashbids": [
                {
                    "commodity":     str,  # h3 abbreviation, e.g. "YSB", "HRWW"
                    "delivery":      str,  # e.g. "June 2026", "Cash", "New Crop 2026"
                    "bid":           str,  # e.g. "10.78"
                    "basis":         str,  # e.g. "-0.36"
                    "futures_month": str,  # e.g. "Jul 26 Soybeans", "Jul 26 KCBT Red Wheat"
                },
                ...
            ],
        }

    Locations with no bid rows are excluded from the returned list.
    """
    if loc_ids is None:
        loc_ids = list(SCOULAR_LOCATIONS.keys())

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    session   = requests.Session()
    session.headers.update(_HEADERS)

    results   = []
    seen_names: set[str] = set()   # deduplicate by page name within a run

    for loc_id in loc_ids:
        meta = SCOULAR_LOCATIONS.get(loc_id, {})
        url  = f"{_BASE_URL}{loc_id}"

        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
        except Exception as exc:
            log.warning("  Scoular GET failed for id=%s: %s", loc_id, exc)
            time.sleep(REQUEST_DELAY)
            continue

        page_name, commodities = _parse_location_page(r.text)
        if not commodities:
            log.debug("  id=%s: no bid data", loc_id)
            time.sleep(REQUEST_DELAY)
            continue

        # Use the page-scraped h2 name; fall back to metadata
        location_name = page_name or meta.get("name", str(loc_id))

        # Skip duplicate page names — Bushel sometimes routes multiple IDs to the
        # same page (e.g. retired sub-locations redirect to a sibling location).
        if location_name and location_name in seen_names:
            log.debug("  id=%s: duplicate page name %r — skipping", loc_id, location_name)
            time.sleep(REQUEST_DELAY)
            continue
        if location_name:
            seen_names.add(location_name)

        cashbids: list[dict] = []
        for comm in commodities:
            for row in comm["rows"]:
                cashbids.append({
                    "commodity":     comm["commodity"],
                    "delivery":      row["delivery"],
                    "bid":           row["bid"],
                    "basis":         row["basis"],
                    "futures_month": row["futures_month"],
                })

        results.append({
            "loc_id":        loc_id,
            "location_name": location_name,
            "state":         meta.get("state", ""),
            "city":          meta.get("city", ""),
            "timestamp":     today_utc,
            "cashbids":      cashbids,
        })
        log.debug("  id=%s %-40s %d bid(s)", loc_id, location_name, len(cashbids))
        time.sleep(REQUEST_DELAY)

    log.info(
        "Scoular scrape complete: %d location(s), %d total bids",
        len(results), sum(len(loc["cashbids"]) for loc in results),
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
    from parsers.scoular_parser import parse_scoular_location

    locs = fetch_scoular_bids()
    print("=" * 65)
    print(f"Total locations: {len(locs)}")
    print("=" * 65)
    total_rows = 0
    for loc in locs:
        snap = parse_scoular_location(loc)
        if snap:
            print(f"  {snap.location:42s} {loc['state']:2s}  {len(snap.rows):2d} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(
                    f"    {r.grain:20s}  {r.deliveryMonth:22s}  "
                    f"{r.futuresSymbol:8s}  {sign}{r.basisCents}c"
                )
            total_rows += len(snap.rows)
        else:
            print(f"  {loc['location_name']:42s} (no valid bids)")
    print(f"\nTotal rows: {total_rows}")
