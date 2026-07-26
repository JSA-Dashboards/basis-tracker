"""
bushelsites_scraper.py — generic scraper for Bushel white-label sites
(`*.o.bushelsites.com`) that render their bid grid SERVER-SIDE into
`<div class='cbCommodity'>` blocks (`<li class='c1'..'c7'>` = Delivery / Bid /
Basis / Futures / Change / Futures Month / Last Trade).

Two labeling styles appear on this platform:
  • "h2"    — a `<h2 class='fcControls'>` per location sets the plant name, and each
              following board's `<h3>` is just the grain ("CORN"/"SOYBEANS").
              Multi-plant sites (Big River) list several locations this way.
  • "board" — no location h2; the board `<h3>` itself is "Location Grain"
              (See-Mor's "Badger State Ethanol Corn"). Mapped via `boards`.

Basis is posted directly (col c3, verified vs bid−futures), so no futures math.
Add a site by dropping an entry in SITES — no new code.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JPSI basis tracker; kpostin@jpsi.com)"}

_MON = {"jan": "F", "feb": "G", "mar": "H", "apr": "J", "may": "K", "jun": "M",
        "jul": "N", "aug": "Q", "sep": "U", "oct": "V", "nov": "X", "dec": "Z"}
_ROOT = {"Corn": "ZC", "Soybeans": "ZS", "Wheat": "ZW", "Sorghum": "ZC"}

# ── Site registry ────────────────────────────────────────────────────────────
# provider -> {url, style, facility (default), locmap OR boards}
#   locmap (style "h2"):  raw h2 text -> (location, state, facility?)
#   boards (style "board"): raw h3 text -> (location, state, facility?)
# Anything unmapped is still captured (never dropped) with a derived name and the
# site's default facility, and logged for review.
SITES: dict[str, dict] = {
    "See-Mor": {
        "url": "https://www.seemorgrain.com/cash-bids",
        "style": "board", "facility": "Country Elevator",
        "boards": {
            "See-mor north corn":        ("See-Mor North, WI",        "WI", "Country Elevator"),
            "See-mor soybeans":          ("See-Mor, WI",              "WI", "Country Elevator"),
            "Badger state ethanol corn": ("Badger State Ethanol, WI", "WI", "Corn Processing"),
            "Bunge warren corn":         ("Bunge Warren, IL",         "IL", "Rail Terminal"),  # CN loader
        },
    },
    "Ace": {
        "url": "https://www.aceethanol.com/cash-bids/",
        "style": "h2", "facility": "Corn Processing",
        "locmap": {"Ace Ethanol LLC": ("Ace Ethanol, WI", "WI", "Corn Processing")},
    },
    "One Earth": {
        "url": "https://www.oneearthenergy.com/cash-bids/",
        "style": "h2", "facility": "Corn Processing",
        "locmap": {"__single__": ("One Earth Energy, IL", "IL", "Corn Processing")},
    },
    "Harvestone": {
        "url": "https://www.harvestonelcp.com/cash-bids/",
        "style": "h2", "facility": "Corn Processing",
        "locmap": {"IBEC": ("Harvestone IBEC", None, "Corn Processing")},
    },
    "BioUrja": {
        # akronservices.com is the BioUrja Peoria plant's live bid page (no BioUrja
        # text on it — Kolten's call). Scrape into the EXISTING "Peoria, IL" location
        # (DJ history 2012-2023) to revive it with live data. One "Corn" board.
        "url": "https://akronservices.com/",
        "style": "board", "facility": "Corn Processing",
        "boards": {"Corn": ("Peoria, IL", "IL", "Corn Processing")},
    },
    "Big River": {
        "url": "https://bigriverresources.com/cash-bids/",
        "style": "h2", "facility": "Corn Processing",
        "locmap": {
            "Boyceville":      ("Boyceville, WI",      "WI", "Corn Processing"),
            "Dyersville":      ("Dyersville, IA",      "IA", "Corn Processing"),
            "Galva":           ("Galva, IL",           "IL", "Corn Processing"),
            "W. Burlington":   ("W. Burlington, IA",   "IA", "Corn Processing"),
            "Monmouth":        ("Monmouth, IL",        "IL", "Country Elevator"),
            "Aledo/Edgington": ("Aledo/Edgington, IL", "IL", "Country Elevator"),
        },
    },
}


def _grain(text: str) -> str:
    b = text.lower()
    if "soybean" in b or "bean" in b:
        return "Soybeans"
    if "milo" in b or "sorghum" in b:
        return "Sorghum"
    if "wheat" in b:
        return "Wheat"
    return "Corn"


def _fut_symbol(fut_month: str, grain: str):
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{2})", fut_month or "")
    if not m:
        return None
    code = _MON.get(m.group(1).lower())
    return f"{_ROOT.get(grain, 'ZC')}{code}{m.group(2)}" if code else None


def _delivery(label: str) -> str:
    # Drop day-of-month ranges first ("July 16 - 31" → "July") so the trailing
    # day isn't mistaken for a year; a bare month lets canonical() take the year
    # from the futures symbol.
    lab = re.sub(r"\d+\s*[-–]\s*\d+", " ", label)
    parts = [m[:3].capitalize() for m in re.findall(r"[A-Za-z]{3,}", lab)
             if m[:3].lower() in _MON]
    yr = re.search(r"\b(\d{2})\b\s*$", lab.strip())
    year = f"20{yr.group(1)}" if yr else ""
    return (("/".join(parts) + " " + year).strip()) or label.strip()


def _cents(s: str):
    try:
        return round(float(s.replace("+", "").strip()) * 100)
    except (ValueError, AttributeError):
        return None


def _rows_of(div) -> list[dict]:
    out = []
    for ul in div.find_all("ul"):
        cells = {li.get("class", ["?"])[0]: li.get_text(" ", strip=True)
                 for li in ul.find_all("li")}
        if cells.get("c1", "").lower() == "delivery" or not cells.get("c1"):
            continue
        out.append(cells)
    return out


def scrape_site(cfg: dict) -> list[dict]:
    """Return [{location, state, facility_type, grain, rows}] for one site config."""
    r = requests.get(cfg["url"], headers=HEADERS, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []

    if cfg["style"] == "board":
        for div in soup.find_all("div", class_="cbCommodity"):
            h = div.find("h3")
            if not h:
                continue
            board = h.get_text(strip=True)
            key = board.strip().capitalize()
            loc, state, ftype = cfg["boards"].get(
                key, (re.sub(r"\b(corn|soybeans?|wheat|milo|sorghum)\b", "", board,
                             flags=re.I).strip() or board, None, cfg["facility"]))
            rows = _rows_of(div)
            if rows:
                out.append({"location": loc, "state": state, "facility_type": ftype,
                            "grain": _grain(board), "rows": rows})
        return out

    # style "h2": walk document order, current location from <h2 class=fcControls>.
    # A "__single__" entry means the whole page is ONE location — ignore the h2
    # heading text entirely so an unmatched heading can't blank the location out.
    locmap = cfg.get("locmap", {})
    single = locmap.get("__single__")
    cur = single
    for el in soup.find_all(["h2", "div"]):
        cls = " ".join(el.get("class", []))
        if el.name == "h2" and "fcControls" in cls and not single:
            raw = el.get_text(" ", strip=True)
            cur = locmap.get(raw) or (raw, None, cfg["facility"])   # unmapped: keep it
        elif el.name == "div" and "cbCommodity" in cls and cur:
            h = el.find("h3")
            grain = _grain(h.get_text(strip=True) if h else "")
            rows = _rows_of(el)
            if rows:
                out.append({"location": cur[0], "state": cur[1],
                            "facility_type": cur[2], "grain": grain, "rows": rows})
    return out


def parse_board(board: dict, provider: str) -> NewSnapshotRequest | None:
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
    return NewSnapshotRequest(timestamp=ts, provider=provider,
                              location=board["location"], source="web", rows=rows)
