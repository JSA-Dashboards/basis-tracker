"""
seemor_scraper.py — See-Mor Grain (Darlington, WI) cash bids.

See-Mor runs a Bushel white-label CMS. Despite the ASP.NET `.axd` bundles, the bid
grid is SERVER-RENDERED into the HTML (no separate bids API to call), inside
`<div class='cbCommodity'>` blocks whose `<h3 class='fcControls'>` is the board name
and whose `<li class='c1'..'c7'>` cells are Delivery / Bid / Basis / Futures / Change /
Futures Month / Last Trade.

One page carries FOUR boards: the elevator's own corn + soybeans, plus DELIVERED bids
at Badger State Ethanol and Bunge Warren — so two processors' basis comes along for
free. Basis is posted directly (col c3), verified against bid − futures, so no futures
math is needed here.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

URL = "https://www.seemorgrain.com/cash-bids"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JPSI basis tracker; kpostin@jpsi.com)"}

_MON = {"jan": "F", "feb": "G", "mar": "H", "apr": "J", "may": "K", "jun": "M",
        "jul": "N", "aug": "Q", "sep": "U", "oct": "V", "nov": "X", "dec": "Z"}
_ROOT = {"Corn": "ZC", "Soybeans": "ZS", "Wheat": "ZW", "Sorghum": "ZC"}

# board heading -> (location, state, facility_type). Grain is derived from the heading.
# Badger State is an ethanol plant and Bunge Warren a processor -> Corn Processing;
# See-Mor's own boards are the elevator. Unknown boards fall back (see _board_meta).
_BOARDS = {
    "See-mor north corn":        ("See-Mor North, WI",        "WI", "Country Elevator"),
    "See-mor soybeans":          ("See-Mor, WI",              "WI", "Country Elevator"),
    "Badger state ethanol corn": ("Badger State Ethanol, WI", "WI", "Corn Processing"),
    "Bunge warren corn":         ("Bunge Warren, IL",         "IL", "Corn Processing"),
}


def _grain(board: str) -> str:
    b = board.lower()
    if "soybean" in b or "bean" in b:
        return "Soybeans"
    if "milo" in b or "sorghum" in b:
        return "Sorghum"
    if "wheat" in b:
        return "Wheat"
    return "Corn"


def _board_meta(board: str):
    key = board.strip().capitalize()
    if key in _BOARDS:
        return _BOARDS[key]
    # Unknown board — keep it (never silently drop), tag no facility for review.
    loc = re.sub(r"\b(corn|soybeans?|wheat|milo|sorghum)\b", "", board, flags=re.I).strip()
    return (loc or board, None, None)


def _fut_symbol(fut_month: str, grain: str):
    """'Sep 26 Corn' -> 'ZCU26'."""
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{2})", fut_month or "")
    if not m:
        return None
    code = _MON.get(m.group(1).lower())
    if not code:
        return None
    return f"{_ROOT.get(grain, 'ZC')}{code}{m.group(2)}"


def _delivery(label: str) -> str:
    """'JULY 26' -> 'Jul 2026'; 'OCT/NOV 26' -> 'Oct/Nov 2026'; 'DEC26' -> 'Dec 2026'."""
    yr = re.search(r"(\d{2})\s*$", label.strip())
    year = f"20{yr.group(1)}" if yr else ""
    mons = re.findall(r"[A-Za-z]{3,}", label)
    parts = [m[:3].capitalize() for m in mons if m[:3].lower() in _MON]
    return (("/".join(parts) + " " + year).strip()) or label.strip()


def _cents(s: str):
    try:
        return round(float(s.replace("+", "").strip()) * 100)
    except (ValueError, AttributeError):
        return None


def fetch_seemor_bids() -> list[dict]:
    """Return one dict per board: {board, location, state, facility_type, grain, rows}.
    rows are the raw c1..c7 cell dicts."""
    r = requests.get(URL, headers=HEADERS, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for div in soup.find_all("div", class_="cbCommodity"):
        h = div.find("h3")
        if not h:
            continue
        board = h.get_text(strip=True)
        loc, state, ftype = _board_meta(board)
        grain = _grain(board)
        rows = []
        for ul in div.find_all("ul"):
            cells = {li.get("class", ["?"])[0]: li.get_text(" ", strip=True)
                     for li in ul.find_all("li")}
            if cells.get("c1", "").lower() == "delivery":     # header row
                continue
            if cells.get("c1"):
                rows.append(cells)
        if rows:
            out.append({"board": board, "location": loc, "state": state,
                        "facility_type": ftype, "grain": grain, "rows": rows})
    return out


def parse_seemor_board(board: dict) -> NewSnapshotRequest | None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    grain = board["grain"]
    rows = []
    for i, c in enumerate(board["rows"]):
        basis = _cents(c.get("c3", ""))
        sym = _fut_symbol(c.get("c6", ""), grain)
        if basis is None or not sym:
            continue
        rows.append(SnapshotRow(
            id=f"{sym[1]}_{i}_{re.sub(r'[^A-Za-z0-9]', '', c.get('c1', ''))}",
            grain=grain, deliveryMonth=_delivery(c.get("c1", "")),
            futuresSymbol=sym, basisCents=basis, isSpot=False))
    if not rows:
        return None
    return NewSnapshotRequest(timestamp=ts, provider="See-Mor",
                              location=board["location"], source="web", rows=rows)
