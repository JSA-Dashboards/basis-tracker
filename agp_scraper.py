"""
agp_scraper.py — Scrape AGP (Ag Processing Inc) cash bid pages.

AGP uses Bushel's cashbidssingle-{id} pages at agp.o.bushelsites.com,
the same HTML structure as the Scoular scraper.

Commodities: Soybeans, Soybean Meal, Corn (grain terminals only).
Soybean Hull Pellets are skipped in the parser (no futures reference).

Usage (standalone test):
    python agp_scraper.py
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ── Location registry ─────────────────────────────────────────────────────────
# IDs sourced from the selectLocation dropdown at agp.o.bushelsites.com/cash-bids

AGP_LOCATIONS: dict[int, dict] = {
    2358: {"name": "Aberdeen, SD",             "state": "SD", "city": "Aberdeen"},
    2359: {"name": "Chester, NE",              "state": "NE", "city": "Chester"},
    2360: {"name": "Cowles, NE",               "state": "NE", "city": "Cowles"},
    3335: {"name": "David City, NE",           "state": "NE", "city": "David City"},
    2361: {"name": "Dawson, MN",               "state": "MN", "city": "Dawson"},
    2362: {"name": "Eagle Grove, IA",          "state": "IA", "city": "Eagle Grove"},
    2363: {"name": "Emmetsburg, IA",           "state": "IA", "city": "Emmetsburg"},
    2364: {"name": "Fairfield, NE",            "state": "NE", "city": "Fairfield"},
    2376: {"name": "Hastings, NE - Soy Plant", "state": "NE", "city": "Hastings"},
    2367: {"name": "Hastings, NE - Grain",     "state": "NE", "city": "Hastings"},
    2369: {"name": "Manning, IA",              "state": "IA", "city": "Manning"},
    2370: {"name": "Mason City, IA",           "state": "IA", "city": "Mason City"},
    2439: {"name": "Rosemont, NE",             "state": "NE", "city": "Rosemont"},
    2372: {"name": "Sergeant Bluff, IA",       "state": "IA", "city": "Sergeant Bluff"},
    2373: {"name": "Sheldon, IA",              "state": "IA", "city": "Sheldon"},
    2374: {"name": "St. Joseph, MO",           "state": "MO", "city": "St. Joseph"},
}

_BASE    = "https://agp.o.bushelsites.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
REQUEST_DELAY = 0.30


def _parse_location_page(html: str) -> tuple[str, list[dict]]:
    """
    Parse the .cashBidLocation div from an AGP cashbidssingle page.

    Returns (location_name, commodities) where each commodity is:
        {"commodity": h3_text, "rows": [{"delivery", "bid", "basis", "futures_month"}]}

    Page structure (Bushel standard):
        .cashBidLocation
          h2                       -- location name
          .cbCommodity             -- one per grain type
            h3                     -- commodity text (Soybeans, Soybean Meal, Corn, ...)
            ul.fcControlsSubHdr    -- column headers (skip)
            ul.fcControls1/2/…     -- data rows
              li.c1 span.cashBidsColumnOne -- delivery label
              li.c2 -- bid price
              li.c3 -- basis
              li.c4 -- futures price
              li.c5 -- change
              li.c6 -- futures month text (used to derive CME symbol)
              li.c7 -- last trade time
    """
    soup    = BeautifulSoup(html, "html.parser")
    loc_div = soup.find(class_="cashBidLocation")
    if not loc_div:
        return ("", [])

    h2            = loc_div.find("h2")
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
                continue

            lis = ul.find_all("li")
            if len(lis) < 6:
                continue

            span_c1       = lis[0].find(class_="cashBidsColumnOne")
            delivery      = span_c1.get_text(strip=True) if span_c1 else ""
            bid           = lis[1].get_text(strip=True)
            basis         = lis[2].get_text(strip=True)
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


def fetch_agp_bids(loc_ids: list[int] | None = None) -> list[dict]:
    """
    Fetch AGP cash bids for all (or a subset of) locations.

    Returns a list of per-location dicts:
        {
            "loc_id":        int,
            "location_name": str,
            "state":         str,
            "city":          str,
            "timestamp":     str,   # ISO-8601 UTC date-normalised
            "cashbids": [
                {
                    "commodity":     str,  # h3 text, e.g. "Soybeans", "Soybean Meal"
                    "delivery":      str,  # e.g. "July 2026", "Cash"
                    "bid":           str,
                    "basis":         str,  # decimal string, e.g. "-0.36" or "8.50"
                    "futures_month": str,  # e.g. "Jul 26 Soybeans", "Jul 26 Soybean Meal"
                },
                ...
            ],
        }

    Locations with no bid rows are excluded from the returned list.
    """
    ids_to_fetch = loc_ids or list(AGP_LOCATIONS.keys())
    today_utc    = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    results: list[dict] = []
    seen_names: set[str] = set()

    session = requests.Session()
    session.headers.update(_HEADERS)

    for loc_id in ids_to_fetch:
        meta = AGP_LOCATIONS.get(loc_id, {})
        name = meta.get("name", f"AGP #{loc_id}")
        url  = f"{_BASE}/cashbidssingle-{loc_id}"

        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("  AGP GET failed for id=%s: %s", loc_id, exc)
            time.sleep(REQUEST_DELAY)
            continue

        page_name, commodities = _parse_location_page(resp.text)
        if not commodities:
            log.debug("  id=%s: no bid data", loc_id)
            time.sleep(REQUEST_DELAY)
            continue

        location_name = page_name or name

        if location_name in seen_names:
            log.debug("  id=%s: duplicate page name %r — skipping", loc_id, location_name)
            time.sleep(REQUEST_DELAY)
            continue
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
        "AGP scrape complete: %d location(s), %d total bids",
        len(results), sum(len(loc["cashbids"]) for loc in results),
    )
    return results


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
    from parsers.agp_parser import parse_agp_location

    locs = fetch_agp_bids()
    print("=" * 65)
    print(f"Total locations: {len(locs)}")
    print("=" * 65)
    total_rows = 0
    for loc in locs:
        snap = parse_agp_location(loc)
        if snap:
            print(f"  {snap.location:42s} {loc['state']:2s}  {len(snap.rows):2d} row(s)")
            for r in snap.rows:
                if r.grain == "Soybean Meal":
                    val = (f"+${abs(r.basisCents)/100:.2f}/t"
                           if (r.basisCents or 0) >= 0
                           else f"-${abs(r.basisCents)/100:.2f}/t")
                else:
                    sign = "+" if (r.basisCents or 0) >= 0 else ""
                    val  = f"{sign}{r.basisCents}c"
                print(f"    {r.grain:20s}  {r.deliveryMonth:22s}  {r.futuresSymbol:8s}  {val}")
            total_rows += len(snap.rows)
        else:
            print(f"  {loc['location_name']:42s} (no valid bids)")
    print(f"\nTotal rows: {total_rows}")
