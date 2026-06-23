"""
palmetto_rail_scraper.py — Rail FOB bids/offers from palmettograin.com/raildivision.

The "Daily Rail Basis" grid is hand-maintained in the page's static HTML (no JS
needed). Columns are delivery periods (JUNE, JULY, …). Each cell is:

    bid only      "SN+20"        -> futures SN, bid +20
    bid / offer   "CN+18/+24"    -> futures CN, bid +18, offer +24

FUT is a short futures code: commodity letter (S/C/W) + CME month code. The
"Singles" rows are all N/A and are skipped. Rail carrier is inferred from the
market (EVILLE / COL = CSX, NS … = Norfolk Southern).

Usage (standalone test):
    python palmetto_rail_scraper.py
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

_URL = "https://www.palmettograin.com/raildivision"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}

_CELL_RE = re.compile(r"^([A-Z][FGHJKMNQUVXZ])\s*([+-]\d+)\s*(?:/\s*([+-]\d+))?$")


def _parse_cell(text: str) -> Optional[dict]:
    """'CN+18/+24' -> {fut, bid, offer}; 'SN+20' -> offer None; 'N/A' -> None."""
    m = _CELL_RE.match(text.strip().upper())
    if not m:
        return None
    return {
        "futures": m.group(1),
        "bid":     int(m.group(2)),
        "offer":   int(m.group(3)) if m.group(3) else None,
    }


def _rail_of(location: str) -> str:
    return "NS" if location.upper().lstrip().startswith("NS") else "CSX"


def _commodity_of(location: str) -> Optional[str]:
    u = location.upper()
    if "BEAN" in u:
        return "Soybeans"
    if "CORN" in u:
        return "Corn"
    if "WHEAT" in u:
        return "Wheat"
    return None


def _cells(row_html: str) -> list[str]:
    cs = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
    out = []
    for c in cs:
        v = re.sub(r"<[^>]+>", " ", c)
        v = v.replace("&rsquo;", "'").replace("&nbsp;", " ").replace("&amp;", "&")
        out.append(re.sub(r"\s+", " ", v).strip())
    return out


def fetch_rail_fob() -> Optional[dict]:
    """
    Scrape the Palmetto rail FOB grid.

    Returns:
        {
          "updated": "6/12/2026 9:00 AM" | None,
          "periods": ["JUNE", "JULY", ...],
          "rows": [
            {"location", "rail", "commodity",
             "cells": [{"period", "futures", "bid", "offer"}, ...]},
            ...
          ],
        }
        or None on fatal error.
    """
    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("Palmetto rail fetch failed: %s", exc)
        return None

    html = resp.text
    idx = html.find("COL, OH")
    if idx < 0:
        log.warning("Palmetto rail: grain table not found")
        return None
    start = html.rfind("<table", 0, idx)
    end   = html.find("</table>", idx)
    table = html[start:end + 8]

    updated: Optional[str] = None
    periods: list[str] = []
    rows: list[dict] = []

    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        v = _cells(row_html)
        if not any(v):
            continue
        joined = " ".join(v)
        if "Updated" in joined and len(v) == 1:
            m = re.search(r"Updated\s+([\d/]+\s+[\d:]+\s*[AP]M)", joined)
            if m:
                updated = m.group(1)
            continue
        if v[0].upper() == "FOB" and len(v) > 1:
            periods = v[1:]
            continue

        loc = v[0]
        if not loc or "single" in loc.lower():   # skip the "Singles" rows
            continue

        cells, has_bid = [], False
        for i, cv in enumerate(v[1:]):
            period = periods[i] if i < len(periods) else f"col{i + 1}"
            pc = _parse_cell(cv)
            if pc is None:
                cells.append({"period": period, "futures": None, "bid": None, "offer": None})
            else:
                cells.append({"period": period, **pc})
                has_bid = True
        if not has_bid:    # all N/A
            continue

        rows.append({
            "location":  loc,
            "rail":      _rail_of(loc),
            "commodity": _commodity_of(loc),
            "cells":     cells,
        })

    log.info("Palmetto rail FOB: %d market(s), updated %s", len(rows), updated)
    return {"updated": updated, "periods": periods, "rows": rows}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    data = fetch_rail_fob()
    if not data:
        print("no data")
        raise SystemExit(1)
    print(f"Updated: {data['updated']}")
    print("Periods:", data["periods"])
    for r in data["rows"]:
        print(f"\n{r['location']}  [{r['rail']} · {r['commodity']}]")
        for c in r["cells"]:
            if c["futures"]:
                off = f" / offer {c['offer']:+d}" if c["offer"] is not None else ""
                print(f"   {c['period']:18} {c['futures']}  bid {c['bid']:+d}{off}")
