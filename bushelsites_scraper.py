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
import time
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
        # Restructured 2026-09: the combined /cash-bids/ page (all plants as h2
        # blocks) now shows only ONE plant, so it silently stopped refreshing the
        # others. Each plant now lives on its own per-facility page embedding a
        # single Bushel board at /cashbidssingle-<id>; scrape those three directly.
        # Locations match the existing archive names so history stays continuous.
        "style": "pages", "facility": "Corn Processing",
        "pages": [
            {"url": "https://harvestonelcp.com/cashbidssingle-2582", "loc": ("Harvestone IBEC", "IN", "Corn Processing")},  # Rensselaer, IN
            {"url": "https://harvestonelcp.com/cashbidssingle-1720", "loc": ("Dakota Spirit", "ND", "Corn Processing")},
            {"url": "https://harvestonelcp.com/cashbidssingle-1703", "loc": ("Blue Flint", "ND", "Corn Processing")},
        ],
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
    "Prairie Grain Partners": {
        # Per-facility Bushel pages (cashbidssingle-<id>). The combined /cash-bids page
        # only renders Boyd and its location dropdown switches client-side, so scrape
        # each facility's own page. Boyd kept as "Boyd, MN" for archive continuity;
        # Clarkfield (MN) + the three SD elevators added 2026-09-25.
        "style": "pages", "facility": "Country Elevator",
        "pages": [
            {"url": "https://www.prairiegrainpartners.com/cashbidssingle-1542", "loc": ("Boyd, MN", "MN", "Country Elevator")},
            {"url": "https://www.prairiegrainpartners.com/cashbidssingle-1607", "loc": ("Clarkfield, MN", "MN", "Country Elevator")},
            {"url": "https://www.prairiegrainpartners.com/cashbidssingle-2661", "loc": ("Hitchcock-Tulare, SD", "SD", "Country Elevator")},
            {"url": "https://www.prairiegrainpartners.com/cashbidssingle-2662", "loc": ("Miller, SD", "SD", "Country Elevator")},
            {"url": "https://www.prairiegrainpartners.com/cashbidssingle-2663", "loc": ("Harrold, SD", "SD", "Country Elevator")},
        ],
    },
    "Madison Farmers Elevator": {
        "url": "https://madisonfarmerselevator.com/cash-bids",
        "style": "h2", "facility": "Country Elevator",
        "locmap": {"Madison": ("Madison, MN", "MN", "Country Elevator")},
    },
    # Beachner Grain, Inc. (Parsons KS): 24 elevators in SE Kansas and NE Oklahoma, added 2026-10-27. beachnergrain.com/cash-bids switches location by an ASP.NET postback; each
    # location also has its own GET page, /cashbidssingle-<id> (<id> = the dropdown's option value, also /cbloc-<id> on the Locations page), which is what is read here. The boards
    # are named by commodity CLASS: "Hard Wheat" is Kansas City (c6 'Dec 26 KCBT Red Wheat' -> KE, stored 'Wheat HRW'), "Soft Wheat" Chicago (ZW, 'Wheat SRW'), "Milo" is priced off the
    # corn board. Every bid is "Cash" against a futures month. States and pins: each elevator's street address on its /cbloc page, looked up in Nominatim 2026-10-27 (the 9 with no street
    # match are the town; Harris is the hamlet, its address is a Garnett mailing one; Neosho Valley is at Erie; St. Paul has no street on the page).
    "Beachner Grain": {
        "style": "pages", "facility": "Country Elevator", "delay": 1.0,
        "grains": {"corn": ("Corn", "ZC"), "soybeans": ("Soybeans", "ZS"), "milo": ("Milo", "ZC"), "hard wheat": ("Wheat HRW", "KE"), "soft wheat": ("Wheat SRW", "ZW")},
        "pages": [{"url": f"https://www.beachnergrain.com/cashbidssingle-{_id}", "loc": (f"{_town}, {_st}", _st, "Country Elevator"), "pin": (_lat, _lon)}
                  for _id, _town, _st, _lat, _lon in [
                      (1962, "Afton", "OK", 36.69627, -94.96474), (2037, "Altamont", "KS", 37.19034, -95.29719), (2038, "Bronson", "KS", 37.89787, -95.07211),
                      (2039, "Chanute", "KS", 37.66011, -95.46195), (2040, "Erie", "KS", 37.57589, -95.24351), (2041, "Fredonia", "KS", 37.5445, -95.81843),
                      (2042, "Garnett", "KS", 38.27885, -95.24575), (2043, "Greeley", "KS", 38.36668, -95.12936), (2045, "Harris", "KS", 38.31946, -95.43637),
                      (2046, "Humboldt", "KS", 37.8106, -95.43693), (2047, "Independence", "KS", 37.23185, -95.71498), (2048, "Kincaid", "KS", 38.08002, -95.15188),
                      (2049, "LaCygne", "KS", 38.3475, -94.76279), (2050, "Miami", "OK", 36.87085, -94.87293), (2051, "Mound City", "KS", 38.14277, -94.81274),
                      (2052, "Neodesha", "KS", 37.41443, -95.67999), (2083, "Neosho Valley", "KS", 37.56811, -95.24331), (2053, "Paola", "KS", 38.57417, -94.88743),
                      (2054, "Parsons", "KS", 37.34078, -95.25963), (2055, "Richmond", "KS", 38.40147, -95.25603), (2063, "St. Paul", "KS", 37.51858, -95.17236),
                      (2057, "Thayer", "KS", 37.4886, -95.47264), (2058, "Walnut", "KS", 37.5986, -95.07882), (2059, "Welch", "OK", 36.87014, -95.09292)]],
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


def _fut_symbol(fut_month: str, grain: str, root: str | None = None):
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{2})", fut_month or "")
    if not m:
        return None
    code = _MON.get(m.group(1).lower())
    return f"{root or _ROOT.get(grain, 'ZC')}{code}{m.group(2)}" if code else None


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
    # "pages" style: a fixed list of single-facility pages, each its own URL with a
    # pinned (location, state, facility). Every cbCommodity board on the page takes
    # that location; the board's <h3> only sets the grain. Used where a site moved
    # to one page per plant (Harvestone's /cashbidssingle-<id>).
    # Optional per site: "delay" (seconds between pages, for a site with many), "grains" (board
    # heading -> (raw grain, CME root) for a site whose boards are named by commodity CLASS, e.g.
    # Beachner's "Hard Wheat" = KE / "Soft Wheat" = ZW / "Milo"; a heading it does not list is
    # skipped and logged, never guessed) and, per page, "pin" ((lat, lon) for the map).
    if cfg.get("style") == "pages":
        out = []
        grains = cfg.get("grains")
        sess = requests.Session()
        sess.headers.update(HEADERS)
        failed, last_exc = 0, None
        for k, pg in enumerate(cfg["pages"]):
            if k and cfg.get("delay"):
                time.sleep(cfg["delay"])
            # one reset or timeout must not cost the other pages (Beachner: 24 pages, a connection reset on 2026-10-27): retry, then skip the page and carry on
            for attempt in range(3):
                try:
                    r = sess.get(pg["url"], timeout=40)
                    r.raise_for_status()
                    break
                except requests.RequestException as exc:
                    last_exc = exc
                    r = None
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
            if r is None:
                failed += 1
                log.error("Bushel-sites: %s could not be read (%s): skipped", pg["loc"][0], last_exc)
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            loc, state, ftype = pg["loc"]
            for div in soup.find_all("div", class_="cbCommodity"):
                h = div.find("h3")
                head = h.get_text(strip=True) if h else ""
                rows = _rows_of(div)
                if not rows:
                    continue
                board = {"location": loc, "state": state, "facility_type": ftype, "grain": _grain(head), "rows": rows}
                if grains is not None:
                    hit = grains.get(head.strip().lower())
                    if hit is None:
                        log.warning("Bushel-sites: %s board %r has no entry in the site's grains map: skipped", loc, head)
                        continue
                    board["grain"], board["root"] = hit
                if pg.get("pin"):
                    board["lat"], board["lon"] = pg["pin"]
                out.append(board)
        if failed == len(cfg["pages"]):
            raise last_exc                      # nothing read at all: let the caller log the site as failed, as before
        return out

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
    root = board.get("root")                  # set by a site's "grains" map (class-named boards); else the root follows the grain
    rows = []
    for i, c in enumerate(board["rows"]):
        basis = _cents(c.get("c3", ""))
        sym = _fut_symbol(c.get("c6", ""), grain, root)
        if basis is None or not sym:
            continue
        slug = re.sub(r"[^A-Za-z0-9]", "", c.get("c1", ""))
        # a class-named board can share a contract with another one at the same location (Corn and Milo are both ZC), so its row id carries the grain; every other site keeps its old ids
        rid = f"{sym}_{re.sub(r'[^A-Za-z0-9]', '', grain)}_{i}_{slug}" if root else f"{sym[1]}_{i}_{slug}"
        rows.append(SnapshotRow(
            id=rid,
            grain=grain, deliveryMonth=_delivery(c.get("c1", "")),
            futuresSymbol=sym, basisCents=basis, isSpot=False))
    if not rows:
        return None
    return NewSnapshotRequest(timestamp=ts, provider=provider,
                              location=board["location"], source="web", rows=rows)
