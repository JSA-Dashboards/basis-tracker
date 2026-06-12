"""
POET Gradable web scraper.

Uses Playwright with real Chrome to bypass CloudFront bot detection, then
intercepts POET's REST API to pull bid data for all 36 locations in one pass.

The site is a React SPA backed by FBN infrastructure and served via CloudFront.
Headless Chromium is fingerprinted and blocked; real Chrome (channel='chrome')
passes through cleanly.  API responses are CSRF-prefixed with 'while(1);' which
we strip before JSON parsing.

Usage (standalone test):
    python poet_scraper.py

Returns a list of dicts ready for parsers/poet_parser.py:
    [
        {
            "market_id":       int,
            "display_name":    str,     # e.g. "Alexandria, IN"
            "instruments_data": dict,  # raw instruments API response
            "timestamp":       str,    # ISO-8601 UTC, date-normalized
        },
        ...
    ]
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

POET_BASE          = "https://poet.gradable.com"
BOOTSTRAP_URL      = f"{POET_BASE}/api/commodities/merchandising/bootstrap"
INSTRUMENTS_TMPL   = (
    f"{POET_BASE}/api/commodities/v2/merchandising/instruments"
    "/market/{market_id}?offer_type=public"
)
# Any valid market page — we just need to load one page to get auth cookies
ENTRY_URL          = f"{POET_BASE}/market/Alexandria--IN"
# Delay between instruments requests to be polite (seconds)
REQUEST_DELAY      = 0.25


def _strip_csrf(text: str) -> str:
    """Strip the 'while(1);' CSRF prefix that POET prepends to API responses."""
    if text.startswith("while(1);"):
        return text[9:]
    return text


def _parse_response(text: str) -> Optional[dict]:
    """Strip CSRF prefix and parse JSON; returns None on error."""
    try:
        return json.loads(_strip_csrf(text))
    except Exception:
        return None


def fetch_poet_bids(headless: bool = True) -> list[dict]:
    """
    Scrape all POET Gradable locations and return raw bid data.

    Steps:
      1. Launch real Chrome with automation flags disabled.
      2. Load POET entry page so CloudFront sets session cookies.
      3. Capture the bootstrap API response (intercepted during page load).
         If not captured, fetch it directly using page.request (uses same cookies).
      4. For each of the 36 markets, GET the instruments endpoint.
      5. Return list of {market_id, display_name, instruments_data, timestamp}.

    Args:
        headless: Run Chrome headlessly (default True). Set False to debug.

    Returns:
        List of location dicts.  Empty list on fatal error.
    """
    from playwright.sync_api import sync_playwright

    # Normalise timestamp to today's UTC date at midnight so duplicate
    # daily runs produce the same (timestamp, provider, location) key
    # and INSERT OR IGNORE deduplicates cleanly.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    results: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # ── Intercept API responses during page load ───────────────────────
        intercepted: dict[str, dict] = {}

        def _on_response(response):
            url = response.url
            if ("commodities/merchandising/bootstrap" in url or
                    "commodities/v2/merchandising/instruments" in url):
                try:
                    data = _parse_response(response.text())
                    if data is not None:
                        intercepted[url] = data
                except Exception:
                    pass

        page.on("response", _on_response)

        # ── Step 1: Load entry page (establishes session / cookies) ────────
        log.info("Loading POET entry page…")
        try:
            page.goto(ENTRY_URL, timeout=35_000, wait_until="networkidle")
        except Exception:
            # Timeout from 'networkidle' is common on heavy SPAs — that's fine.
            pass

        # ── Step 2: Get bootstrap (all 36 market IDs + display names) ──────
        bootstrap: Optional[dict] = None
        for url, data in intercepted.items():
            if "bootstrap" in url:
                bootstrap = data
                break

        if bootstrap is None:
            log.info("Bootstrap not captured during page load — fetching directly…")
            try:
                resp = page.request.get(
                    BOOTSTRAP_URL,
                    headers={"Accept": "application/json"},
                    timeout=30_000,
                )
                bootstrap = _parse_response(resp.text())
            except Exception as exc:
                log.error("Failed to fetch POET bootstrap: %s", exc)
                browser.close()
                return []

        if not bootstrap:
            log.error("Bootstrap is empty — cannot continue.")
            browser.close()
            return []

        markets = bootstrap.get("markets", [])
        log.info("POET bootstrap: %d markets found", len(markets))

        # Build ordered list: [(market_id, display_name), ...]
        market_list = [
            (m["id"], m.get("display_name", str(m["id"])))
            for m in markets
        ]

        # ── Step 3: Fetch instruments for each market ──────────────────────
        for market_id, display_name in market_list:
            instr_url = INSTRUMENTS_TMPL.format(market_id=market_id)

            # Use already-intercepted data if available (saves a request)
            instr_data = intercepted.get(instr_url)

            if instr_data is None:
                try:
                    resp = page.request.get(
                        instr_url,
                        headers={"Accept": "application/json"},
                        timeout=20_000,
                    )
                    instr_data = _parse_response(resp.text())
                except Exception as exc:
                    log.warning(
                        "  ✗  %s (id=%s): request failed — %s",
                        display_name, market_id, exc,
                    )
                    continue

                if instr_data is None:
                    log.warning(
                        "  ✗  %s (id=%s): empty/invalid response",
                        display_name, market_id,
                    )
                    continue

                # Small delay to be polite to the server
                time.sleep(REQUEST_DELAY)

            n_instr = len(instr_data.get("instruments", []))
            log.debug("  %s → %d instrument(s)", display_name, n_instr)

            results.append({
                "market_id":        market_id,
                "display_name":     display_name,
                "instruments_data": instr_data,
                "timestamp":        today_utc,
            })

        browser.close()

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

    bids = fetch_poet_bids(headless=True)
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
