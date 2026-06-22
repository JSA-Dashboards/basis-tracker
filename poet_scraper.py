"""
POET Gradable web scraper — pure httpx, no browser needed.

POET runs on the same Gradable platform as ADM (poet.gradable.com). Both the
bootstrap and instruments endpoints return open JSON (CSRF-prefixed with
'while(1);') to a normal browser User-Agent — no auth/cookies required — so this
mirrors adm_scraper.py and runs fast (no Playwright / browser binaries).

Usage (standalone test):
    python poet_scraper.py

Returns a list of dicts ready for parsers/poet_parser.py:
    [
        {
            "market_id":        int,
            "display_name":     str,    # e.g. "Alexandria, IN"
            "instruments_data": dict,   # raw instruments API response
            "timestamp":        str,    # ISO-8601 UTC, date-normalized
        },
        ...
    ]
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)

POET_BASE         = "https://poet.gradable.com"
BOOTSTRAP_URL     = f"{POET_BASE}/api/commodities/merchandising/bootstrap"
INSTRUMENTS_TMPL  = (
    f"{POET_BASE}/api/commodities/v2/merchandising/instruments"
    "/market/{market_id}?offer_type=public"
)
REQUEST_DELAY     = 0.25   # seconds between instruments requests
_CSRF_PREFIX      = "while(1);"

_HEADERS = {
    "Accept":     "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _parse_response(text: str) -> Optional[dict]:
    """Strip the 'while(1);' CSRF prefix and parse JSON; None on error."""
    if text.startswith(_CSRF_PREFIX):
        text = text[len(_CSRF_PREFIX):]
    try:
        return json.loads(text)
    except Exception:
        return None


def fetch_poet_bids(headless: bool = True) -> list[dict]:
    """
    Scrape all POET Gradable locations via the open JSON API (no browser).

    Steps:
      1. GET bootstrap  → list of (market_id, display_name) pairs.
      2. GET instruments for each market.
      3. Return list of {market_id, display_name, instruments_data, timestamp}.

    Args:
        headless: accepted for backward compatibility (ignored — no browser).

    Returns:
        List of location dicts. Empty list on fatal error.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    results: list[dict] = []

    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as client:
        # ── 1. Bootstrap ──────────────────────────────────────────────────────
        try:
            r = client.get(BOOTSTRAP_URL)
            r.raise_for_status()
            bootstrap = _parse_response(r.text)
        except Exception as exc:
            log.error("POET bootstrap failed: %s", exc)
            return []

        if not bootstrap:
            log.error("POET bootstrap empty/invalid — cannot continue.")
            return []

        markets = bootstrap.get("markets", [])
        log.info("POET bootstrap: %d markets", len(markets))

        # ── 2. Instruments per market ─────────────────────────────────────────
        for market in markets:
            market_id    = market["id"]
            display_name = market.get("display_name", str(market_id))

            url = INSTRUMENTS_TMPL.format(market_id=market_id)
            try:
                r = client.get(url)
                r.raise_for_status()
                instr_data = _parse_response(r.text)
            except Exception as exc:
                log.warning("  ✗  %s (id=%s): %s", display_name, market_id, exc)
                continue

            if instr_data is None:
                log.warning("  ✗  %s (id=%s): empty/invalid response",
                            display_name, market_id)
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

    log.info("POET scrape complete: %d location(s) fetched", len(results))
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
    from parsers.poet_parser import parse_instruments

    bids = fetch_poet_bids()
    print(f"\n{'='*60}")
    print(f"Total locations fetched: {len(bids)}")
    print(f"{'='*60}")

    for item in bids:
        snap = parse_instruments(
            item["market_id"],
            item["display_name"],
            item["instruments_data"],
            item["timestamp"],
        )
        if snap:
            row_summary = ", ".join(
                f"{r.deliveryMonth} {r.futuresSymbol} "
                f"{'+' if (r.basisCents or 0) >= 0 else ''}{r.basisCents}¢"
                for r in snap.rows
            )
            print(f"  {snap.location:30s}  {len(snap.rows):2d} row(s)  {row_summary}")
        else:
            print(f"  {item['display_name']:30s}  (no instruments)")
