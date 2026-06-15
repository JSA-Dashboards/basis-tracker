"""
GPC / Kent Commodities grain bid scraper.

3 locations on the DTN Portal site at kentcommodities.com:
  GPC Muscatine  — loc 37 — Corn, IP Corn (Muscatine, IA)
  GPC Washington — loc 38 — Corn (Washington, IA)
  Kent Grain     — loc 39 — Corn, Soybeans, Wheat (Plainville, IN)

Anti-scraping obfuscation: prices are stored as raw values + a random per-request
offset. The offset is disclosed in an HTML comment:
  // NoScrapeOffset: -140.0564
Actual price = raw_value - NoScrapeOffset

Returns list of per-location dicts matching the standard scraper contract:
  {location_name, state, lat, lon, timestamp, cashbids: [{grain, delivery_label,
                                                          dtn_symbol, basis_usd}]}
"""
import logging
import re
import urllib3
from datetime import datetime, timezone

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

_BASE_URL = "http://www.kentcommodities.com/index.cfm"

_LOCATIONS = [
    {
        "location_id":   37,
        "location_name": "GPC Muscatine",
        "state":         "IA",
        "lat":           41.4245,
        "lon":           -91.0429,
    },
    {
        "location_id":   38,
        "location_name": "GPC Washington",
        "state":         "IA",
        "lat":           41.3000,
        "lon":           -91.6921,
    },
    {
        "location_id":   39,
        "location_name": "Kent Grain",
        "state":         "IN",
        "lat":           38.8084,
        "lon":           -87.1578,
    },
]

_COMMODITY_MAP = {
    "CORN":     "Corn",
    "SOYBEANS": "Soybeans",
    "WHEAT":    "Wheat",
    # IP Corn is a specialty product — skip to keep bids consistent with standard grains
}

_DELIVERY_RE = re.compile(
    r"^((?:FH |LH )?"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[\s\w]*?)\s*$"
)


def _fetch_html(loc_id: int) -> str:
    resp = requests.get(
        _BASE_URL,
        params={"show": 11, "mid": 6, "theLocation": loc_id, "layout": 19, "cmid": 0},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer":    "http://www.kentcommodities.com/",
        },
        verify=False,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def _parse_location(html: str, loc_cfg: dict, timestamp: str) -> dict:
    """Parse one location's HTML. Returns standard location dict."""
    loc_name = loc_cfg["location_name"]

    nso_match = re.search(r"NoScrapeOffset:\s*([-+]?\d+\.\d+)", html)
    if not nso_match:
        log.warning("%s: NoScrapeOffset not found in HTML", loc_name)
        return {**loc_cfg, "timestamp": timestamp, "cashbids": []}
    offset = float(nso_match.group(1))

    lines     = html.splitlines()
    cashbids  = []
    current_commodity = None

    for i, line in enumerate(lines):
        s = line.strip()

        if s.upper() in _COMMODITY_MAP:
            current_commodity = s.upper()
            continue

        if current_commodity and _DELIVERY_RE.match(s):
            block    = "\n".join(lines[i: i + 80])
            dn_vals  = re.findall(r"displayNumber\(([-+]?\d+\.\d+),\s*\d+\)", block)
            sym_m    = re.search(r"@([A-Z]+\d[A-Z])", block)

            if len(dn_vals) >= 2 and sym_m:
                basis_usd = round(float(dn_vals[1]) - offset, 4)
                cashbids.append({
                    "grain":          _COMMODITY_MAP[current_commodity],
                    "delivery_label": s,
                    "dtn_symbol":     "@" + sym_m.group(1),
                    "basis_usd":      basis_usd,
                })

    return {**loc_cfg, "timestamp": timestamp, "cashbids": cashbids}


def fetch_gpc_bids() -> list[dict]:
    """
    Scrape all 3 GPC / Kent Grain locations.
    Returns list of per-location dicts (empty cashbids list on fetch error).
    """
    now_ts  = datetime.now(timezone.utc).isoformat()
    results = []

    for loc in _LOCATIONS:
        try:
            html   = _fetch_html(loc["location_id"])
            parsed = _parse_location(html, loc, now_ts)
            results.append(parsed)
            log.info(
                "GPC  %-22s  %s  %d bid(s)",
                loc["location_name"], loc["state"], len(parsed["cashbids"]),
            )
        except Exception as exc:
            log.error("GPC scrape failed for %s: %s", loc["location_name"], exc)
            results.append({**loc, "timestamp": now_ts, "cashbids": []})

    return results
