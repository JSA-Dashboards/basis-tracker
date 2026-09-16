"""
agricharts_md_scraper.py — scraper for AgriCharts "marketdata" cash-bid pages
(a THIRD AgriCharts variant, distinct from cashbids-js.php and the tenant feed).
These pages (e.g. Homeland Energy via farmerswin.com/markets/cash.php) print each
bid SERVER-SIDE as a `writeBidRow(name, basis, …, chartsym, …)` JS call — the basis
(integer cents) and delivery month are literals in the HTML, so plain requests get
them. Futures are fetched separately by the page from agricharts jsquote.php; we
derive the reference contract from the delivery month (corn H/K/N/U/Z cycle).

writeBidRow arg order: name(commodity), basis, manual, eod, incwt, rounding, start,
end, location, group, notes, weight, rowclass, chartsym(…&d=<MonYY>), …
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")}

# provider config. `url` is the marketdata cash page for the location.
SITES: list[dict] = [
    {"provider": "Homeland Energy", "location": "Lawler, IA", "state": "IA",
     "facility_type": "Corn Processing",
     "url": "https://www.farmerswin.com/markets/cash.php?location_filter=12602"},
    {"provider": "KAAPA", "location": "Aurora, NE", "state": "NE", "facility_type": "Corn Processing",
     "url": "https://kaapagrains.agricharts.com/markets/cash.php?location_filter=83493"},
    {"provider": "KAAPA", "location": "Minden, NE", "state": "NE", "facility_type": "Corn Processing",
     "url": "https://kaapagrains.agricharts.com/markets/cash.php?location_filter=2893"},
    {"provider": "KAAPA", "location": "Ravenna, NE", "state": "NE", "facility_type": "Corn Processing",
     "url": "https://kaapagrains.agricharts.com/markets/cash.php?location_filter=46768"},
    {"provider": "Siouxland Energy", "location": "Sioux Center, IA", "state": "IA",
     "facility_type": "Corn Processing",
     "url": "https://www.siouxlandenergy.com/markets/cash.php"},
    {"provider": "Plymouth Energy", "location": "Merrill, IA", "state": "IA",
     "facility_type": "Corn Processing",
     "url": "https://cvacoop365.agricharts.com/markets/cash.php?location_filter=83175"},
    {"provider": "WGM", "location": "Adair, IL", "state": "IL", "facility_type": "Rail Terminal",
     "url": "https://www.westerngrainmarketing.com/markets/cash.php?location_filter=86697"},
    {"provider": "West-Con", "location": "Holloway, MN", "state": "MN",
     "facility_type": "Country Elevator",
     "url": "https://www.west-con.com/markets/cash.php?location_filter=1631"},
    {"provider": "West-Con", "location": "Appleton, MN", "state": "MN",
     "facility_type": "Country Elevator",
     "url": "https://www.west-con.com/markets/cash.php?location_filter=1635"},
    {"provider": "West-Con", "location": "Milbank, SD", "state": "SD",
     "facility_type": "Country Elevator",
     "url": "https://www.west-con.com/markets/cash.php?location_filter=1632"},
    {"provider": "Wheaton Dumont Co-Op", "location": "Wheaton, MN", "state": "MN",
     "facility_type": "Country Elevator",
     "url": "https://www.wdcoop.com/markets/cash.php"},
    # New Vision Co-op (SW MN) — own elevators via newvision.agricharts.com
    *[{"provider": "New Vision Co-op", "location": f"{_town}, MN", "state": "MN",
       "facility_type": "Country Elevator",
       "url": f"https://newvision.agricharts.com/markets/cash.php?location_filter={_id}"}
      for _town, _id in [
          ("Adrian", 46899), ("Beaver Creek", 18652), ("Brewster", 46894),
          ("Dundee", 46900), ("Ellsworth", 46901), ("Heron Lake", 46895),
          ("Hills Terminal", 18650), ("Jeffers", 46902), ("Magnolia", 20359),
          ("Miloma", 41203), ("Mountain Lake", 46896), ("Reading", 46904),
          ("Wilmont", 46905), ("Windom", 46898), ("Worthington", 46897)]],
    # Glacial Plains Cooperative (MN) via glacialplains.agricharts.com
    *[{"provider": "Glacial Plains Cooperative", "location": f"{_town}, MN", "state": "MN",
       "facility_type": "Country Elevator",
       "url": f"https://glacialplains.agricharts.com/markets/cash.php?location_filter={_id}"}
      for _town, _id in [
          ("Benson", 20326), ("DeGraff", 20330), ("Milan", 20325), ("Murdock", 31737)]],
    # Farmward Cooperative (MN) via farmward.agricharts.com
    *[{"provider": "Farmward Cooperative", "location": f"{_town}, MN", "state": "MN",
       "facility_type": "Country Elevator",
       "url": f"https://farmward.agricharts.com/markets/cash.php?location_filter={_id}"}
      for _town, _id in [
          ("Clements", 87431), ("Comfrey", 87432), ("Danube East", 87424),
          ("Morgan", 87428), ("Morton", 87429), ("Olivia", 87426),
          ("Renville", 87423), ("Sacred Heart", 87425), ("Springfield", 87430),
          ("Wood Lake", 87427)]],
    # Cooperative Farmers Elevator (CFE) — NW IA / SW MN via coopfe.agricharts.com
    # (coopfe.com itself is WAF-blocked; the agricharts data host is not).
    *[{"provider": "Cooperative Farmers Elevator", "location": f"{_town}, {_st}",
       "state": _st, "facility_type": "Country Elevator",
       "url": f"https://coopfe.agricharts.com/markets/cash.php?location_filter={_id}"}
      for _town, _id, _st in [
          ("Allendorf", 87548, "IA"), ("Alvord", 87549, "IA"), ("Ashton", 87550, "IA"),
          ("Bigelow", 87551, "MN"), ("George", 87552, "IA"),
          ("George Downtown", 88029, "IA"), ("Germantown", 87566, "MN"),
          ("Harris", 87553, "IA"), ("Hartley", 87554, "IA"), ("Hawarden", 87555, "IA"),
          ("Hudson", 87556, "SD"), ("Inwood", 87557, "IA"), ("Lake Park", 87559, "IA"),
          ("Larchwood", 87558, "IA"), ("Lester", 87560, "IA"), ("Ocheyedan", 87561, "IA"),
          ("Rock Rapids", 87562, "IA"), ("Rock Valley", 87563, "IA"),
          ("Rushmore", 87564, "MN"), ("Sibley", 87565, "IA")]],
    # AgKota (SD) via agkotagrain.com
    *[{"provider": "AgKota", "location": f"{_town}", "state": _st,
       "facility_type": "Country Elevator",
       "url": f"https://www.agkotagrain.com/markets/cash.php?location_filter={_id}"}
      for _town, _id, _st in [
          ("Plankinton, SD", 86551, "SD"), ("Colton, SD", 86552, "SD"),
          ("Hartford, SD", 86553, "SD")]],
    # Kokomo Grain (IN) via kokomograin.com — own elevators (ADM Fkt / T-L Laf are
    # delivered points, excluded)
    *[{"provider": "Kokomo Grain", "location": f"{_town}, IN", "state": "IN",
       "facility_type": "Country Elevator",
       "url": f"https://www.kokomograin.com/markets/cash.php?location_filter={_id}"}
      for _town, _id in [
          ("Amboy", 20018), ("Edinburgh", 20020), ("Kokomo", 20021),
          ("Winamac", 20023), ("Emporia", 31805), ("Anderson", 36292)]],
]

# A FOURTH AgriCharts variant: the "/bidlist" template. Instead of writeBidRow it
# prints each row via a run of document.write() calls — commodity, delivery
# start/end (MM/DD/YYYY), and the basis as a bare decimal-dollar literal
# (document.write('-0.08')). The futures contract is the `quote = quotevarNNN['ZCU26']`
# assignment emitted just before each row. Parsed by parse_bidlist_site().
BIDLIST_SITES: list[dict] = [
    {"provider": "Lincolnland Agri-Energy", "location": "Palestine, IL", "state": "IL",
     "facility_type": "Corn Processing",
     "url": "https://www.lincolnlandagrienergy.com/bidlist"},
]

_COMMODITY = {"CORN": ("ZC", "Corn"), "SOYBEANS": ("ZS", "Soybeans"),
              "SOYBEAN": ("ZS", "Soybeans"), "WHEAT": ("ZW", "Wheat"),
              "MILO": ("ZC", "Sorghum"), "SORGHUM": ("ZC", "Sorghum")}
_PFX = {"ZC": "CN", "ZS": "SB", "ZW": "WH"}
_MON = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9,
        "V": 10, "X": 11, "Z": 12}
_MON_NAME = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul",
             8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
# delivery month → nearest corn futures contract in its cycle
_FUT_CYCLE = {1: "H", 2: "H", 3: "H", 4: "K", 5: "K", 6: "N", 7: "N",
              8: "U", 9: "U", 10: "Z", 11: "Z", 12: "Z"}
# soybean cycle F,H,K,N,Q,U,X (new-crop X for fall)
_SOY_CYCLE = {1: "F", 2: "H", 3: "H", 4: "K", 5: "K", 6: "N", 7: "N",
              8: "Q", 9: "X", 10: "X", 11: "X", 12: "F"}
_WHEAT_CYCLE = {m: c for m, c in zip(range(1, 13), "HHHKKNNUUZZZ")}

_CALL_RE = re.compile(r"writeBidRow\((.*?)\);", re.S)
_ARG_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|\s*([^,]+?)\s*(?:,|$)')
_D_RE = re.compile(r"[?&]d=([FGHJKMNQUVXZ])(\d{2})")


def _fut_for(root: str, month: int, year: int) -> str | None:
    cyc = {"ZC": _FUT_CYCLE, "ZS": _SOY_CYCLE, "ZW": _WHEAT_CYCLE}.get(root)
    if not cyc:
        return None
    code = cyc[month]
    fy = year
    # if the mapped contract month is before the delivery month, it's next year
    if _MON[code] < month:
        fy += 1
    return f"{root}{code}{fy % 100:02d}"


def _args(call: str) -> list[str]:
    return [a or b or c for a, b, c in _ARG_RE.findall(call)]


def _commodity_of(name: str):
    """Match a bid's commodity by substring ('#2 Yellow Corn' → Corn)."""
    up = (name or "").upper()
    for key, val in _COMMODITY.items():
        if key in up:
            return val
    return None


def parse_site(cfg: dict) -> NewSnapshotRequest | None:
    try:
        html = requests.get(cfg["url"], headers=_HEADERS, timeout=25).text
    except Exception as exc:
        log.error("AgriCharts-MD fetch failed for %s: %s", cfg["location"], exc)
        return None
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for call in _CALL_RE.findall(html):
        a = _args(call)
        if len(a) < 14:
            continue
        info = _commodity_of(a[0])
        if not info:
            continue
        root, grain = info
        try:
            basis = int(round(float(a[1])))
        except (ValueError, TypeError):
            continue
        if basis == 0:
            continue          # these pages post 0 for months a plant isn't bidding
        m = _D_RE.search(a[13] or "")
        if not m:
            continue
        month, year = _MON[m.group(1)], 2000 + int(m.group(2))
        # The page quotes each bid against a specific futures contract (e.g. an arg
        # like quotes['ZCZ26']). TRUST that actual contract over the delivery-month
        # cycle — otherwise a nearby bid a plant has already rolled to Dec gets
        # mislabeled against the expiring Sep board (KAAPA 2026-08: Aug/Sep vs CZ).
        qsym = None
        for _x in a[13:]:
            mq = re.search(r"([A-Z]{2}[FGHJKMNQUVXZ]\d{2})", _x or "")
            if mq and mq.group(1)[:2] == root:
                qsym = mq.group(1)
                break
        cme = qsym or _fut_for(root, month, year)
        if not cme:
            continue
        delivery = f"{_MON_NAME[month]} {year}"
        pfx = _PFX.get(root, "XX")
        row_id = f"{pfx}_{cme}_{delivery.replace(' ', '').upper()}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(SnapshotRow(id=row_id, grain=grain, deliveryMonth=delivery,
                                futuresSymbol=cme, basisCents=basis, isSpot=False))
    if not rows:
        log.warning("AgriCharts-MD: no bids parsed for %s", cfg["location"])
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                              location=cfg["location"], source="web", rows=rows)


# ── "/bidlist" variant (document.write rows) ────────────────────────────────────
_TR_SPLIT = "document.write('<tr>')"
_DW_RE   = re.compile(r"document\.write\('((?:\\.|[^'])*)'\)")
_TAG_RE  = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_DEC_RE  = re.compile(r"^[+-]?\d+\.\d+$")
_SYM_RE  = re.compile(r"quotevar\d+\['([A-Z]{2}[FGHJKMNQUVXZ]\d{2})'\]")
_ROOT_GRAIN = {"ZC": "Corn", "ZS": "Soybeans", "ZW": "Wheat"}


def parse_bidlist_site(cfg: dict) -> NewSnapshotRequest | None:
    try:
        html = requests.get(cfg["url"], headers=_HEADERS, timeout=25).text
    except Exception as exc:
        log.error("AgriCharts-bidlist fetch failed for %s: %s", cfg["location"], exc)
        return None
    segs = html.split(_TR_SPLIT)
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for j in range(1, len(segs)):
        seg = segs[j]
        writes = [m.group(1) for m in _DW_RE.finditer(seg)]

        grain = None
        for w in writes:                                   # commodity from first <td>text</td>
            info = _commodity_of(_TAG_RE.sub("", w).strip())
            if info:
                grain = info[1]
                break
        if grain is None:
            continue

        dm = _DATE_RE.search(seg)                          # delivery-start month/year
        if not dm:
            continue
        month, year = int(dm.group(1)), int(dm.group(3))
        if month not in _MON_NAME:
            continue

        basis = None                                       # bare decimal-dollar literal
        for w in writes:
            ws = w.strip()
            if _DEC_RE.match(ws) and abs(float(ws)) < 3:
                basis = int(round(float(ws) * 100))
                break
        if basis is None:
            continue

        prev_syms = _SYM_RE.findall(segs[j - 1])           # futures = quote set just before this row
        cme = prev_syms[-1] if prev_syms else None
        if not cme:
            continue
        root = cme[:2]
        grain = _ROOT_GRAIN.get(root, grain)               # trust the actual quoted contract
        delivery = f"{_MON_NAME[month]} {year}"
        pfx = _PFX.get(root, "XX")
        row_id = f"{pfx}_{cme}_{delivery.replace(' ', '').upper()}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(SnapshotRow(id=row_id, grain=grain, deliveryMonth=delivery,
                                futuresSymbol=cme, basisCents=basis, isSpot=False))
    if not rows:
        log.warning("AgriCharts-bidlist: no bids parsed for %s", cfg["location"])
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                              location=cfg["location"], source="web", rows=rows)


def fetch_agricharts_md() -> tuple[list[NewSnapshotRequest], list[dict]]:
    reqs, metas = [], []
    for cfg, parser in ([(c, parse_site) for c in SITES]
                        + [(c, parse_bidlist_site) for c in BIDLIST_SITES]):
        req = parser(cfg)
        if req is None:
            continue
        reqs.append(req)
        metas.append({"provider": cfg["provider"], "location": cfg["location"],
                      "state": cfg.get("state"), "facility_type": cfg.get("facility_type")})
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, metas = fetch_agricharts_md()
    for req in reqs:
        print(f"  {req.provider} · {req.location} — {len(req.rows)} row(s)")
        for r in req.rows[:8]:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"     {r.deliveryMonth:10s} {r.futuresSymbol:7s} {sign}{r.basisCents}c")
