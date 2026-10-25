"""stonex_scraper.py — StoneX "Stonehedge" embeddable cash-bids component (River Valley Cooperative).

A co-op's website embeds an iframe

    https://stonehedge.stonex.com/component/bids?key=<EMBED KEY>&cols=...&hideRows=true

and that page is a thin React client over ONE REST call (read from the component's own JavaScript, assets/index-*.js, 2026-10-25):

    GET https://api.stonehedge.stonex.com/settings/v2/delivery-periods        Authorization: key <EMBED KEY>

It answers plain requests (no Origin / Referer, no OAuth - the OAuth + SignalR streaming gateway is the full StoneX app, not this component); the widget re-polls it every 60 s; the page says
"Data provided by StoneX. All prices delayed 10 minutes." The response is {deliveryPeriods: [...], lastUpdatedBy, lastUpdatedDate}. One period = one commodity x one delivery window:

    {name: 'OCT 26' | 'FH Nov 26' | '10/19 - 10/24', start, end (ISO dates), isSpot, lastUpdatedDateUtc, commodity {name: 'Corn'},
     contract {symbol: 'CZ26', maturityMonth, maturityYear, price},   bids: [{location {id, name}, cash, basis}]}      cash and basis in $/bu; basis null = no bid

The board mixes the co-op's OWN elevators with the DESTINATION points it quotes direct-ship bids for (ADM, Cargill, CHS, Gavilon, Marquis, ...). Only the co-op's own elevators are filed (`locations` in the
SITES row). A destination's bid on River Valley's board is River Valley's price delivered there, not that buyer's own posted bid - filing it under ADM would merge two bid streams into one snapshot, the
same reason the H&F sheet's ADM / Cargill rows are not filed (see CLAUDE.md).

Add a site: open its grain page, read the iframe's `key=` (or `document.querySelectorAll('iframe')`), and add a SITES row with the co-op's own locations (StoneX's location name -> label, state, pin).
"""
from __future__ import annotations

import calendar
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

from models import NewSnapshotRequest, SnapshotRow
from vistacomm_scraper import _PFX      # {'ZC': 'CN', 'ZS': 'SB', 'ZW': 'WH'}

log = logging.getLogger(__name__)

_API = "https://api.stonehedge.stonex.com/settings/v2/delivery-periods"
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json"}
MAX_AGE_DAYS = 5        # the newest update across the co-op's own bids; its merchandisers touch the headline months most days, deferred months sit unchanged for a week

_ABBR = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
_ROOT = {"Corn": ("C", "ZC"), "Soybeans": ("S", "ZS")}      # StoneX commodity -> (first letter of its contract symbol, CME root); others are skipped

# River Valley Cooperative (Eldridge IA): rivervalleycoop.com/products-services/grain. The pins and states are the ones its own Locations map carries (the page's LocationDetails JSON, 2026-10-25).
# StoneX location name -> (label, state, lat, lon).
RIVER_VALLEY_LOCATIONS: dict[str, tuple[str, str, float, float]] = {
    "Altona":      ("Altona, IL",      "IL", 41.11217, -90.16264),
    "Bradford":    ("Bradford, IL",    "IL", 41.17211, -89.65626),
    "Cambridge":   ("Cambridge, IL",   "IL", 41.29865, -90.17606),
    "Dewitt":      ("DeWitt, IA",      "IA", 41.81501, -90.53763),
    "Donahue":     ("Donahue, IA",     "IA", 41.69511, -90.67400),     # seasonal
    "Duncan":      ("Duncan, IL",      "IL", 40.98353, -89.79089),     # seasonal (Princeville)
    "Eldridge":    ("Eldridge, IA",    "IA", 41.65631, -90.58382),
    "Galva":       ("Galva, IL",       "IL", 41.16339, -90.02215),
    "Manlius":     ("Manlius, IL",     "IL", 41.45582, -89.67046),
    "Martelle":    ("Martelle, IA",    "IA", 42.02127, -91.35101),
    "Mineral":     ("Mineral, IL",     "IL", 41.38260, -89.83833),
    "Olin":        ("Olin, IA",        "IA", 41.98882, -91.14163),
    "Princeville": ("Princeville, IL", "IL", 40.93207, -89.69402),
    "Putnam":      ("Putnam, IL",      "IL", 41.18741, -89.39498),
    "Ryan":        ("Ryan, IA",        "IA", 42.35062, -91.48055),
    "Sheffield":   ("Sheffield, IL",   "IL", 41.36744, -89.68497),
    "Speer":       ("Speer, IL",       "IL", 40.98903, -89.64851),
    "Sunbury":     ("Sunbury, IA",     "IA", 41.67012, -90.93330),
    "Toulon":      ("Toulon, IL",      "IL", 41.09862, -89.86642),
    "Valley":      ("Valley, IL",      "IL", 41.01009, -89.69826),     # office and elevator, mailing address Wyoming IL
    "Walcott":     ("Walcott, IA",     "IA", 41.58618, -90.77783),     # seasonal
    "Wyoming":     ("Wyoming, IL",     "IL", 41.07708, -89.70222),     # seasonal
}

SITES: list[dict] = [
    {"provider": "River Valley Coop", "key": "P28NGCX7G6BHB9UTU949NUPWIGMVACV7",       # the public embed key in rivervalleycoop.com's iframe URL
     "facility_type": "Country Elevator", "locations": RIVER_VALLEY_LOCATIONS},
]


def _cme(commodity: str, symbol: str) -> str | None:
    """('Corn', 'CZ26') -> 'ZCZ26'; ('Soybeans', 'SF27') -> 'ZSF27'; anything else (an unknown commodity, a symbol that is not that commodity's) -> None."""
    root = _ROOT.get(commodity)
    s = (symbol or "").strip().upper()
    if not root or not re.fullmatch(r"[A-Z][FGHJKMNQUVXZ]\d{2}", s) or s[0] != root[0]:
        return None
    return root[1] + s[1:]


def _label(start, end) -> str | None:
    """The delivery window as a label the app reads: a whole month 'Nov 2026', a half 'FH Nov 2026' / 'LH Nov 2026', any other window 'Oct 19-24 2026'. StoneX ends July at the 30th, so the last
    day of a month is allowed one day short."""
    try:
        s, e = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    mon = _ABBR[s.month]
    last = calendar.monthrange(s.year, s.month)[1]
    if (e.year, e.month) == (s.year, s.month):
        if s.day == 1 and e.day >= last - 1:
            return f"{mon} {s.year}"
        if s.day == 1 and e.day in (14, 15):
            return f"FH {mon} {s.year}"
        if s.day in (15, 16) and e.day >= last - 1:
            return f"LH {mon} {s.year}"
        return f"{mon} {s.day}-{e.day} {s.year}"
    return f"{mon} {s.day}-{_ABBR[e.month]} {e.day} {s.year}"


def _utc(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def unmapped_locations(payload: dict, cfg: dict) -> list[str]:
    """Location names on the board that are not one of the co-op's own (destination points, or a new elevator the SITES row does not know yet)."""
    names = {b["location"]["name"] for p in payload.get("deliveryPeriods") or [] for b in p.get("bids") or []}
    return sorted(names - set(cfg["locations"]))


def build_site(cfg: dict, payload: dict, ts: str, today: datetime | None = None) -> tuple[list[NewSnapshotRequest], list[dict], dict]:
    """One site's payload -> (snapshots, location metas, info). Only the locations in cfg['locations'] are filed. A board whose newest own bid is more than MAX_AGE_DAYS old files nothing."""
    today = today or datetime.now(timezone.utc)
    own = cfg["locations"]
    by_loc: dict[str, list[SnapshotRow]] = {}
    seen: dict[str, set] = {}
    newest: datetime | None = None
    skipped = 0
    for p in payload.get("deliveryPeriods") or []:
        commodity = ((p.get("commodity") or {}).get("name")) or ""
        cme = _cme(commodity, (p.get("contract") or {}).get("symbol"))
        label = _label(p.get("start"), p.get("end"))
        stamp = _utc(p.get("lastUpdatedDateUtc"))
        for b in p.get("bids") or []:
            name = ((b.get("location") or {}).get("name")) or ""
            if name not in own or b.get("basis") is None or b.get("cash") is None:
                continue
            if not cme or not label:
                skipped += 1
                continue
            if stamp and (newest is None or stamp > newest):
                newest = stamp
            rid = f"{_PFX.get(cme[:2], cme[:2])}_{cme}_{re.sub(r'[^A-Z0-9]', '', label.upper())}"
            if rid in seen.setdefault(name, set()):
                continue
            seen[name].add(rid)
            by_loc.setdefault(name, []).append(SnapshotRow(id=rid, grain=commodity, deliveryMonth=label, futuresSymbol=cme, basisCents=int(round(float(b["basis"]) * 100)),
                                                           isSpot=bool(p.get("isSpot"))))
    info = {"newest": newest, "stale": False, "skipped": skipped, "unmapped": unmapped_locations(payload, cfg)}
    if newest is None:
        return [], [], info
    if today - newest > timedelta(days=MAX_AGE_DAYS):
        info["stale"] = True
        log.warning("StoneX %s: the newest own bid is from %s (more than %d days old) - nothing filed", cfg["provider"], newest.date(), MAX_AGE_DAYS)
        return [], [], info
    reqs, metas = [], []
    for name, rows in sorted(by_loc.items()):
        label, state, lat, lon = own[name]
        reqs.append(NewSnapshotRequest(timestamp=ts, provider=cfg["provider"], location=label, source="web", rows=rows))
        metas.append({"provider": cfg["provider"], "location": label, "state": state, "facility_type": cfg.get("facility_type"), "lat": lat, "lon": lon})
    return reqs, metas, info


def _get(cfg: dict) -> dict | None:
    """The delivery-periods payload, or None (3 tries)."""
    for attempt in range(3):
        try:
            r = requests.get(_API, headers=dict(_HEADERS, Authorization="key " + cfg["key"]), timeout=40)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and isinstance(data.get("deliveryPeriods"), list):
                return data
            log.warning("StoneX %s: unexpected payload shape (%s)", cfg["provider"], type(data).__name__)
        except Exception as exc:
            log.warning("StoneX %s: attempt %d failed: %s", cfg["provider"], attempt + 1, exc)
        time.sleep(2 * (attempt + 1))
    return None


def fetch_stonex(payloads: dict | None = None, today: datetime | None = None) -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape every SITES entry. `payloads` ({provider: payload}) replaces the network (tests). Returns (snapshot requests, location metas)."""
    now = today or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT00:00:00Z")
    reqs: list[NewSnapshotRequest] = []
    metas: list[dict] = []
    for cfg in SITES:
        payload = payloads.get(cfg["provider"]) if payloads is not None else _get(cfg)
        if not payload:
            log.error("StoneX %s: no data", cfg["provider"])
            continue
        r, m, info = build_site(cfg, payload, ts, now)
        reqs += r
        metas += m
        log.info("StoneX %s: %d location(s), %d row(s); %d board location(s) not the co-op's own (destination points)%s", cfg["provider"], len(r), sum(len(x.rows) for x in r),
                 len(info["unmapped"]), "; %d row(s) skipped for an unreadable contract or window" % info["skipped"] if info["skipped"] else "")
        if info["unmapped"]:
            log.debug("StoneX %s: not filed: %s", cfg["provider"], ", ".join(info["unmapped"]))
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    rq, mt = fetch_stonex()
    for r in rq:
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows[:6]:
            print(f"   {x.grain:9} {x.deliveryMonth:12} {x.futuresSymbol} {x.basisCents:+d}")
