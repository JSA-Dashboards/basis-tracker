"""
alto_scraper.py — Alto Ingredients / ICP ethanol plant, Pekin, IL. Corn only.

A WordPress page whose bid grid is a static `<table>`: Delivery | $Bid | Basis |
FutCode (e.g. 'U26' = Sep 2026). Only the rows whose 2nd cell is a $ price and whose
4th cell is a CME month code are bids — the rest of the table is corn-dump hours, which
are skipped. Basis is posted directly (verified against bid − futures).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

URL = "https://www.altoingredients.com/corn-pricing/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JPSI basis tracker; kpostin@jpsi.com)"}

LOCATION = "Alto Pekin, IL"
STATE = "IL"
FACILITY = "Corn Processing"          # ethanol plant

_BID_RE = re.compile(r"^\$?\s*(\d+\.\d+)$")
_FUT_RE = re.compile(r"^([FGHJKMNQUVXZ])(\d{2})$")


def _cents(s: str):
    try:
        return round(float(s.replace("+", "").strip()) * 100)
    except (ValueError, AttributeError):
        return None


def fetch_alto_bids() -> list[dict]:
    """Return the bid rows: [{delivery, bid, basis, futcode}]."""
    r = requests.get(URL, headers=HEADERS, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    for t in soup.find_all("table"):
        for tr in t.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 4:
                continue
            fut = _FUT_RE.match(cells[3])
            if _BID_RE.match(cells[1]) and fut:
                rows.append({"delivery": cells[0], "bid": cells[1],
                             "basis": cells[2], "futcode": cells[3]})
    return rows


def parse_alto(rows: list[dict]) -> NewSnapshotRequest | None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    out = []
    for i, r in enumerate(rows):
        basis = _cents(r["basis"])
        m = _FUT_RE.match(r["futcode"])
        if basis is None or not m:
            continue
        sym = f"ZC{m.group(1)}{m.group(2)}"
        out.append(SnapshotRow(
            id=f"C_{i}_{re.sub(r'[^A-Za-z0-9]', '', r['delivery'])}",
            grain="Corn", deliveryMonth=r["delivery"].strip(),
            futuresSymbol=sym, basisCents=basis, isSpot=False))
    if not out:
        return None
    return NewSnapshotRequest(timestamp=ts, provider="Alto",
                              location=LOCATION, source="web", rows=out)
