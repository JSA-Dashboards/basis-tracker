"""
ncga_scraper.py — Northern Canola Growers Association "Daily Cash Prices" (https://northerncanola.com/).

The NCGA home page carries one table of canola cash bids for five buyers by arrival month (Oct .. Feb): a price (US$/cwt), a change and a basis in
CANADIAN dollars per metric tonne ("Basis is in Canadian dollars per metric ton", against ICE canola). Three of the five are read directly from
their own sites (ADM Velva / Enderlin: adm_scraper, CHS Hallock: chs_scraper), so this job takes only the two we had no usable feed for:

    Bunge Altona        no Bunge page carries it (bungeag.com has no Altona location)           -> provider Bunge,   location "Altona, MB"
    Cargill West Fargo  Cargill posts canola as a flat price only: basis "0.0000", no futures   -> provider Cargill, location "West Fargo"
                        month (Barchart WebSol says futuresymbol "n.a."), so its own feed has no basis; NCGA's table is where one exists
                        (NCGA works it out from Cargill's flat price, in steps of 5 — read it as an estimate, not a posted basis)

Basis is stored x100 like every other canola row (see grain_units.py: -25 C$/t is stored -2500 and read back as -25). Like every other scraper the rows
are filed under the day we LOOKED (the fetch date), not the day NCGA stamped its table: NCGA updates during the business day, so a morning import
always meets yesterday's table, and filing that under yesterday would leave Cargill West Fargo's newest snapshot without any canola. NCGA's own
"Last Updated" date is kept in the snapshot's subject line (email_subject / email_date) so a table that was a day behind can always be told. Nothing is
written when the page's units sentence changes (a different unit must be looked at, not ingested), when the table is missing, when no update date can
be read, or when the table is more than MAX_AGE_DAYS old (the page stopped updating: the run reports 'empty' and the operator alert goes out).

Terms (read 2026-10-13): the site's disclaimer is informational / as-is with no reuse or scraping clause and robots.txt allows everything.

Usage (standalone, prints what would be written; writes nothing):
    python ncga_scraper.py
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_URL = "https://northerncanola.com/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
}

# The buyers whose bids we take from this page (the other three have their own feeds), and where each is filed.
TARGETS: dict[str, dict] = {
    "Bunge Altona": {
        "provider": "Bunge", "location": "Altona, MB", "abbr": "BG",
        "meta": {"state": "MB", "facility_type": "Oilseed Processing", "lat": 49.1, "lon": -97.567},
    },
    "Cargill West Fargo": {
        "provider": "Cargill", "location": "West Fargo", "abbr": "CG",
        "meta": {"state": "ND"},
    },
}

MAX_AGE_DAYS = 5                    # a table older than this is not filed (weekends and a holiday fit comfortably inside it)
_UNITS_NOTE = "canadian dollars per metric ton"
_UPDATED_RE = re.compile(r"Last Updated:\s*([A-Za-z]{3},\s*[A-Za-z]{3}\s+\d{1,2}\s+\d{4})")


def _clean(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").replace("\xa0", " ")).strip()


def _price(txt: str) -> float | None:
    t = _clean(txt)
    try:
        return float(t)
    except ValueError:
        return None                                 # '--' = no bid


def _basis(txt: str) -> int | None:
    """'-45' -> -45, '+5' -> 5, '0' -> 0 (a real basis), '--' / '' -> None. A unicode minus is accepted."""
    t = _clean(txt).replace("−", "-")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", t):
        return None
    return int(round(float(t)))


def parse_table(html: str) -> dict:
    """The page's cash-price table as plain data:

        {"updated": date | None,            the page's own 'Last Updated' date
         "units_ok": bool,                  the 'Basis is in Canadian dollars per metric ton' sentence is still there
         "months": ["Oct", ..., "Feb"],     arrival months, in the page's order
         "buyers": {"ADM Velva": [{"month": 10, "label": "Oct", "price": 24.94 | None, "basis": -45 | None}, ...], ...}}

    The table is the desktop one (id cashbids-data-table): a header row ('Arrive to Bid' then Buyer / Change / Basis for each buyer) and a row per
    month. A '--' is no bid (None); a basis of 0 is a basis."""
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ")
    m = _UPDATED_RE.search(text)
    updated = None
    if m:
        try:
            updated = datetime.strptime(_clean(m.group(1)).replace(" ,", ","), "%a, %b %d %Y").date()
        except ValueError:
            updated = None
    out = {"updated": updated, "units_ok": _UNITS_NOTE in _clean(text).lower(), "months": [], "buyers": {}}

    table = soup.find("table", id="cashbids-data-table")
    if table is None:
        return out
    rows = table.find_all("tr")
    if not rows:
        return out
    heads = [_clean(c.get_text(" ")) for c in rows[0].find_all(["th", "td"])]
    cols = []                                       # (buyer, price col, basis col)
    for i in range(1, len(heads) - 2, 3):
        if heads[i + 1].lower() == "change" and heads[i + 2].lower() == "basis":
            cols.append((heads[i], i, i + 2))
        else:
            log.warning("NCGA: unexpected header block at column %d: %s", i, heads[i:i + 3])
    for name, _, _ in cols:
        out["buyers"][name] = []
    for tr in rows[1:]:
        cells = [_clean(c.get_text(" ")) for c in tr.find_all("td")]
        if not cells:
            continue
        label = cells[0]
        try:
            month = datetime.strptime(label[:3], "%b").month
        except ValueError:
            continue
        out["months"].append(label)
        for name, pc, bc in cols:
            if bc >= len(cells):
                continue
            out["buyers"][name].append({"month": month, "label": label, "price": _price(cells[pc]), "basis": _basis(cells[bc])})
    return out


def ice_canola_symbol(year: int, month: int) -> str:
    """The ICE canola contract a delivery month is quoted against, in the tracker's symbol form. The pattern the buyers' own pages use (CHS Hallock; the
    NCGA prices of Bunge Altona step the same way): Oct-Nov vs Nov, Dec-Jan vs Jan, Feb-Mar vs Mar, Apr-May vs May, Jun-Jul vs Jul, Aug-Sep vs the
    new-crop Nov. Dec 2026 is quoted off Jan 2027, so it reads RSF27. An inference for the NCGA rows — ADM quotes Oct-Jan all off Jan."""
    code, bump = {10: ("X", 0), 11: ("X", 0), 12: ("F", 1), 1: ("F", 0), 2: ("H", 0), 3: ("H", 0),
                  4: ("K", 0), 5: ("K", 0), 6: ("N", 0), 7: ("N", 0), 8: ("X", 0), 9: ("X", 0)}[month]
    return f"RS{code}{(year + bump) % 100:02d}"


def _year_for(month: int, updated: date) -> int:
    """The arrival month's year: the page lists the current month and the ones after it, so a month before the update month is next year's."""
    return updated.year if month >= updated.month else updated.year + 1


def _locations(table: dict, filed: date) -> list[dict]:
    """One dict per target buyer, filed under `filed` (the fetch date); the table's own date rides along as `table_date`."""
    updated = table["updated"]
    ts = filed.strftime("%Y-%m-%dT00:00:00Z")
    out = []
    for buyer, spec in TARGETS.items():
        entries = table["buyers"].get(buyer)
        if not entries:
            log.warning("NCGA: %s is not in the table today", buyer)
            continue
        bids = []
        for e in entries:
            if e["basis"] is None:
                continue                           # '--': that month has no bid
            year = _year_for(e["month"], updated)
            bids.append({
                "bid_id":      f"NCGA_{spec['abbr']}_{year}{e['month']:02d}",
                "grain":       "Canola",
                "delivery":    f"{e['label'][:3]} {year}",
                "cme_symbol":  ice_canola_symbol(year, e["month"]),
                "basis_cents": e["basis"] * 100,           # stored x100, like ADM / CHS canola (grain_units.py)
                "price":       e["price"],                # US$/cwt as NCGA shows it (not stored: snapshot rows hold the basis only)
            })
        if bids:
            out.append({"provider": spec["provider"], "location": spec["location"], "timestamp": ts, "table_date": updated.isoformat(),
                        "bids": bids, "meta": dict(spec["meta"])})
    return out


def _get_page(attempts: int = 3) -> str | None:
    """The home page's HTML, trying up to `attempts` times (a connect timeout is common on this small host); None when every try failed."""
    import time
    last = None
    for i in range(attempts):
        try:
            resp = requests.get(_URL, headers=_HEADERS, timeout=(15, 30))
            resp.raise_for_status()
            return resp.text
        except Exception as exc:                    # noqa: BLE001
            last = exc
            log.warning("NCGA: fetch attempt %d of %d failed: %s", i + 1, attempts, str(exc)[:120])
            if i + 1 < attempts:
                time.sleep(3 * (i + 1))
    log.error("NCGA: fetch failed after %d attempts: %s", attempts, str(last)[:200])
    return None


def fetch_ncga_bids(html: str | None = None, today: date | None = None) -> list[dict]:
    """The canola bids to file from NCGA's table: one dict per target buyer, {provider, location, timestamp (the fetch date), table_date (NCGA's own),
    bids: [...], meta: {...}} (see TARGETS). `html` and `today` are for tests (default: fetch the page, today's UTC date). Returns [] — and says why in
    the log — when the page cannot be read, its table or update date is missing, its units sentence is no longer 'Canadian dollars per metric ton',
    or the table is more than MAX_AGE_DAYS old."""
    if html is None:
        html = _get_page()
        if html is None:
            return []
    table = parse_table(html)
    if not table["buyers"]:
        log.error("NCGA: no cash-price table on the page (the layout changed?)")
        return []
    if table["updated"] is None:
        log.error("NCGA: no 'Last Updated' date on the page — nothing filed")
        return []
    if not table["units_ok"]:
        log.error("NCGA: the page no longer says the basis is in Canadian dollars per metric ton — nothing filed until the unit is checked")
        return []
    today = today or datetime.now(timezone.utc).date()
    age = (today - table["updated"]).days
    if age > MAX_AGE_DAYS:
        log.error("NCGA: the table was last updated %s (%d days ago) — the page has stopped updating, nothing filed", table["updated"], age)
        return []
    locs = _locations(table, today)
    log.info("NCGA: table of %s (%d day(s) old), filed as of %s — %s", table["updated"], age, today,
             ", ".join(f"{l['provider']} {l['location']} {len(l['bids'])} bid(s)" for l in locs) or "no target bids")
    return locs


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    for loc in fetch_ncga_bids():
        print("=" * 60)
        print(f"  {loc['provider']} / {loc['location']}   filed as of {loc['timestamp'][:10]} (NCGA table of {loc['table_date']})   meta {loc['meta']}")
        for b in loc["bids"]:
            print(f"    {b['delivery']:<10} {b['cme_symbol']:<7} basis {b['basis_cents'] / 100:+.0f} C$/t (stored {b['basis_cents']:+d})   price {b['price']}")
