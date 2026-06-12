"""
ADM Gradable web scraper — pure httpx, no browser needed.

ADM's Gradable instance (adm.gradable.com) returns open JSON from both the
bootstrap and instruments endpoints without authentication.  151 locations
across the US and Canada are available.

Usage (standalone test):
    python adm_scraper.py

Returns a list of dicts ready for parsers/adm_parser.py:
    [
        {
            "market_id":        int,
            "display_name":     str,    # e.g. "Decatur, IL (Corn Processing)"
            "instruments_data": dict,   # raw instruments API response
            "timestamp":        str,    # ISO-8601 UTC, date-normalised
        },
        ...
    ]
"""
import logging
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

ADM_BASE          = "https://adm.gradable.com"
BOOTSTRAP_URL     = f"{ADM_BASE}/api/commodities/merchandising/bootstrap"
INSTRUMENTS_TMPL  = (
    f"{ADM_BASE}/api/commodities/v2/merchandising/instruments"
    "/market/{market_id}?offer_type=public"
)
REQUEST_DELAY     = 0.25   # seconds between instruments requests
_CSRF_PREFIX      = "while(1);"


def _parse_json(response: "httpx.Response") -> dict:
    """Strip the while(1); CSRF prefix and parse JSON."""
    text = response.text
    if text.startswith(_CSRF_PREFIX):
        text = text[len(_CSRF_PREFIX):]
    return __import__("json").loads(text)


_HEADERS = {
    "Accept":     "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def fetch_adm_bids() -> list[dict]:
    """
    Scrape all ADM Gradable locations and return raw bid data.

    Steps:
      1. GET bootstrap  → list of 151 (market_id, display_name) pairs.
      2. GET instruments for each market.
      3. Return list of {market_id, display_name, instruments_data, timestamp}.

    Returns:
        List of location dicts.  Empty list on fatal error.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    results: list[dict] = []

    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as client:
        # ── 1. Bootstrap ──────────────────────────────────────────────────────
        try:
            r = client.get(BOOTSTRAP_URL)
            r.raise_for_status()
            bootstrap = _parse_json(r)
        except Exception as exc:
            log.error("ADM bootstrap failed: %s", exc)
            return []

        markets = bootstrap.get("markets", [])
        log.info("ADM bootstrap: %d markets", len(markets))

        # ── 2. Instruments per market ─────────────────────────────────────────
        for market in markets:
            market_id    = market["id"]
            display_name = market.get("display_name", str(market_id))

            url = INSTRUMENTS_TMPL.format(market_id=market_id)
            try:
                r = client.get(url)
                r.raise_for_status()
                instr_data = _parse_json(r)
            except Exception as exc:
                log.warning("  ✗  %s (id=%s): %s", display_name, market_id, exc)
                continue

            n = len(instr_data.get("instruments", []))
            log.debug("  %s → %d instrument(s)", display_name, n)

            results.append({
                "market_id":        market_id,
                "display_name":     display_name,
                "instruments_data": instr_data,
                "timestamp":        today_utc,
            })

            time.sleep(REQUEST_DELAY)

    log.info("ADM scrape complete: %d location(s)", len(results))
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
    from parsers.adm_parser import parse_instruments

    bids = fetch_adm_bids()
    print(f"\n{'='*60}")
    print(f"Total locations fetched: {len(bids)}")
    print(f"{'='*60}")

    rows_total = 0
    for item in bids:
        snap = parse_instruments(
            item["market_id"],
            item["display_name"],
            item["instruments_data"],
            item["timestamp"],
        )
        if snap:
            rows_total += len(snap.rows)
            grains = sorted({r.grain for r in snap.rows})
            print(f"  {snap.location:45s}  {len(snap.rows):2d} row(s)  [{', '.join(grains)}]")
        else:
            print(f"  {item['display_name']:45s}  (no bids)")

    print(f"\nTotal bid rows: {rows_total}")
