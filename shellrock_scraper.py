"""
shellrock_scraper.py — Shell Rock Soy Processing (Shell Rock, IA) bid scraper.

Uses the CIHedging.com cash-bid widget API (customer ID 113526).
Single location: Shell Rock, IA.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_API_URL = "https://www.cihedging.com/cih/api/index.cfm/origination/cashbids/113526"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept":       "application/json, */*",
    "Origin":       "https://shellrocksoyprocessing.com",
    "Referer":      "https://shellrocksoyprocessing.com/",
}

_MONTH_CODES: dict[str, str] = {
    "Jan": "F", "Feb": "G", "Mar": "H", "Apr": "J",
    "May": "K", "Jun": "M", "Jul": "N", "Aug": "Q",
    "Sep": "U", "Oct": "V", "Nov": "X", "Dec": "Z",
}

_ROW_RE = re.compile(
    r'<span>([A-Z][a-z]+ \d{4})</span>'
    r'.*?'
    r"<span>([A-Z][a-z]+'\d{2})</span>"
    r'.*?'
    r'cashbid_price">([\d.]+)</td>'
    r'.*?'
    r'cashbid_price">([-+]?[\d.]+)</td>',
    re.DOTALL,
)


def _futures_label_to_cme(label: str) -> str | None:
    m = re.match(r"([A-Z][a-z]+)'(\d{2})$", label)
    if not m:
        return None
    code = _MONTH_CODES.get(m.group(1))
    if not code:
        return None
    return f"ZS{code}{m.group(2)}"


def fetch_shellrock_bids() -> list[dict]:
    """
    Fetch Shell Rock Soy Processing soybean bids.

    Returns a list with one entry:
        {"location": "Shell Rock", "timestamp": str, "bids": [...]}
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    try:
        resp = requests.post(_API_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        html: str = resp.json()
    except Exception as exc:
        log.error("ShellRock: fetch failed: %s", exc)
        return []

    parts = re.split(r'<h2[^>]*>', html)
    soy_html = ""
    for part in parts:
        if "Soybeans" in part[:40] and "Meal" not in part[:40] and "Pellet" not in part[:40]:
            soy_html = part
            break

    if not soy_html:
        log.warning("ShellRock: Soybeans section not found")
        return []

    bids = []
    seen: set[str] = set()
    for m in _ROW_RE.finditer(soy_html):
        delivery  = m.group(1).strip()
        fut_label = m.group(2).strip()
        basis_usd = float(m.group(4))

        cme_sym = _futures_label_to_cme(fut_label)
        if not cme_sym:
            continue

        key = f"{delivery}|{cme_sym}"
        if key in seen:
            continue
        seen.add(key)

        bids.append({
            "delivery":    delivery,
            "cme_symbol":  cme_sym,
            "basis_cents": int(round(basis_usd * 100)),
        })

    if not bids:
        log.warning("ShellRock: no bids parsed")
        return []

    log.info("ShellRock  %d soybean bid(s)", len(bids))
    return [{"location": "Shell Rock", "timestamp": today_ts, "bids": bids}]


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
    from parsers.shellrock_parser import parse_shellrock_location

    locs = fetch_shellrock_bids()
    print("=" * 55)
    for loc in locs:
        snap = parse_shellrock_location(loc)
        if snap:
            print(f"  {snap.location:25s}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.deliveryMonth:22s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:25s}  (no valid bids)")
