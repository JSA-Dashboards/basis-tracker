"""
heartland_scraper.py — Heartland Co-op (Iowa) cash bids, Fairfield location only.

myaccount.heartlandcoop.com/bids.htm renders server-side custom tables: one per
grain ("CORN BIDS", "SOYBEANS BIDS", plus PROCESSOR variants), locations down the
rows and futures contracts across the header (e.g. "CU26", "CZ26"). Each cell is
"<cash price> <basis>" — the basis is taken directly (2nd token).

Kolten wants ONLY the Fairfield row for now; widening to more locations is just a
change to _WANT. Provider "Heartland Coop" already exists (Des Moines, DJ history);
Fairfield joins it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

URL = "https://myaccount.heartlandcoop.com/bids.htm"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JPSI basis tracker; kpostin@jpsi.com)"}

# Which (location, state) rows to keep, keyed by the upper-cased row label.
_WANT = {"FAIRFIELD": ("Fairfield, IA", "IA", "Country Elevator")}

_ROOT = {"C": "ZC", "S": "ZS", "W": "ZW"}
_GRAIN = {"C": "Corn", "S": "Soybeans", "W": "Wheat"}
_MON = {"F": "Jan", "G": "Feb", "H": "Mar", "J": "Apr", "K": "May", "M": "Jun",
        "N": "Jul", "Q": "Aug", "U": "Sep", "V": "Oct", "X": "Nov", "Z": "Dec"}
_CONTRACT = re.compile(r"^([CSW])([FGHJKMNQUVXZ])(\d{2})$")


def _parse_contract(hdr: str):
    """'/ CU26' → ('ZCU26', 'Sep 2026', 'Corn')."""
    tok = re.sub(r"[^A-Z0-9]", "", hdr.upper())
    m = _CONTRACT.match(tok)
    if not m:
        return None
    comm, mon, yr = m.groups()
    return (f"{_ROOT[comm]}{mon}{yr}", f"{_MON[mon]} 20{yr}", _GRAIN[comm])


def _basis_cents(cell: str):
    """'4.44 -0.20' → -20 (basis is the 2nd number, dollars → cents)."""
    nums = re.findall(r"-?\d+\.\d+", cell)
    if len(nums) < 2:
        return None
    try:
        return round(float(nums[1]) * 100)
    except ValueError:
        return None


def fetch_heartland_bids() -> list[dict]:
    """Return [{location, state, facility_type, grain, rows:[(symbol, delivery, cents)]}]."""
    r = requests.get(URL, headers=HEADERS, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out: dict[tuple, dict] = {}
    for tb in soup.find_all("table"):
        trs = tb.find_all("tr")
        if not trs:
            continue
        hdr = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
        if "BIDS" not in (hdr[0].upper() if hdr else ""):
            continue
        if "PROCESSOR" in hdr[0].upper():
            continue                     # Kolten wants the co-op's own Fairfield row
        contracts = [_parse_contract(h) for h in hdr[1:]]
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            key = cells[0].strip().upper()
            if key not in _WANT:
                continue
            loc, state, ftype = _WANT[key]
            seen = set()
            for ci, con in enumerate(contracts, start=1):
                if not con or ci >= len(cells):
                    continue
                sym, deliv, grain = con
                if sym in seen:
                    continue             # header repeats CU26 twice; keep one
                cents = _basis_cents(cells[ci])
                if cents is None:
                    continue
                seen.add(sym)
                bucket = out.setdefault((loc, grain),
                                        {"location": loc, "state": state,
                                         "facility_type": ftype, "grain": grain, "rows": []})
                bucket["rows"].append((sym, deliv, cents))
    return list(out.values())


def parse_heartland(board: dict) -> NewSnapshotRequest | None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    rows = [SnapshotRow(id=f"{sym[1]}_{sym}", grain=board["grain"],
                        deliveryMonth=deliv, futuresSymbol=sym,
                        basisCents=cents, isSpot=False)
            for sym, deliv, cents in board["rows"]]
    if not rows:
        return None
    return NewSnapshotRequest(timestamp=ts, provider="Heartland Coop",
                              location=board["location"], source="web", rows=rows)
