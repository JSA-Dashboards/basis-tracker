"""
agrex_scraper.py — scraper for the Agrex "FarmCentric" cash-bids terminal
(agrexinc.com/cashbidsterminal.aspx?cbcntloc=<id>), which also powers a handful of
non-Agrex clients (Western New York Energy, Oracle Pork Nutrition).

The terminal is server-rendered ASP.NET: each bid row is a
`<ul class='sevenColumnsBigFirst …'>` with `<li class='cN'>` cells —
c1 delivery · c2 cash · c3 BASIS · c4 futures · c5 change · c6 FUTURES MONTH · c7 last.
The c6 cell carries the exact contract AND commodity ("Sep 26 Corn",
"Jul 27 Wheat", "Sep 26 KCBT Red Wheat"), so the futures symbol is read directly
rather than guessed from a cycle. Basis is a decimal-dollar literal → cents.

Locations come from the JS list on agrexinc.com/cash-bids (the `bids:'y'` ones).
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
_URL = "https://www.agrexinc.com/cashbidsterminal.aspx?cbcntloc={}"

# cbcntloc ids + display info (only the actively-bidding locations from the hub).
SITES: list[dict] = [
    {"provider": "Agrex", "location": "Mobile, AL", "state": "AL",
     "facility_type": "Export Terminal", "cbcntloc": "2419"},
    {"provider": "Agrex", "location": "Montgomery, AL", "state": "AL",
     "facility_type": "River Terminal", "cbcntloc": "2421"},
    {"provider": "Agrex", "location": "Superior, NE", "state": "NE",
     "facility_type": "Country Elevator", "cbcntloc": "2422"},
    {"provider": "Oracle Pork Nutrition", "location": "Peru, IN", "state": "IN",
     "facility_type": "Feed Mill", "cbcntloc": "2449"},
    {"provider": "Western New York Energy", "location": "Medina, NY", "state": "NY",
     "facility_type": "Corn Processing", "cbcntloc": "3575"},
]

_ROW = re.compile(r"sevenColumnsBigFirst[^>]*'>(.*?)</ul>", re.S)
_LI  = re.compile(r"<li class='c(\d)'>(.*?)</li>", re.S)
_TAG = re.compile(r"<[^>]+>")
_DEC = re.compile(r"^-?\d+(?:\.\d+)?$")
_FM  = re.compile(r"^([A-Za-z]+)\s+(\d{2})\s+(.*)$")

_MON_CODE = {"JAN": "F", "FEB": "G", "MAR": "H", "APR": "J", "MAY": "K", "JUN": "M",
             "JUL": "N", "AUG": "Q", "SEP": "U", "SEPT": "U", "OCT": "V", "NOV": "X",
             "DEC": "Z"}


def _root_grain(comm: str):
    """(2-char CME root, display grain) from the commodity text in the c6 cell."""
    u = comm.upper()
    if "CORN" in u:
        return "ZC", "Corn"
    if "SOYBEAN" in u or "SOY" in u:
        return "ZS", "Soybeans"
    if "MILO" in u or "SORGHUM" in u:
        return "ZC", "Sorghum"
    if "KCBT" in u or "HARD RED" in u or "HRW" in u:      # Kansas City HRW
        return "KE", "Wheat"
    if "MGEX" in u or "SPRING" in u:                      # Minneapolis spring
        return "MW", "Wheat"
    if "WHEAT" in u:                                      # Chicago SRW default
        return "ZW", "Wheat"
    return None, None


def _fut_symbol(fm_cell: str):
    """'Sep 26 Corn' → ('ZCU26', 'Corn', 'ZC'); None if unparseable."""
    m = _FM.match(fm_cell.strip())
    if not m:
        return None
    code = _MON_CODE.get(m.group(1).upper())
    root, grain = _root_grain(m.group(3))
    if not code or not root:
        return None
    return f"{root}{code}{m.group(2)}", grain, root


def parse_site(cfg: dict) -> NewSnapshotRequest | None:
    try:
        html = requests.get(_URL.format(cfg["cbcntloc"]), headers=_HEADERS, timeout=25).text
    except Exception as exc:
        log.error("Agrex fetch failed for %s: %s", cfg["location"], exc)
        return None
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for block in _ROW.findall(html):
        cells = {int(c): _TAG.sub("", v).strip() for c, v in _LI.findall(block)}
        deliv, basis_s, fm = cells.get(1, ""), cells.get(3, ""), cells.get(6, "")
        if not deliv or not fm or not _DEC.match(basis_s):   # skips header/blank rows
            continue
        fut = _fut_symbol(fm)
        if not fut:
            continue
        cme, grain, root = fut
        try:
            cents = int(round(float(basis_s) * 100))
        except ValueError:
            continue
        del_key = "".join(ch for ch in deliv.upper() if ch.isalnum()) or cme
        row_id = f"{root}_{cme}_{del_key}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(SnapshotRow(id=row_id, grain=grain, deliveryMonth=deliv,
                                futuresSymbol=cme, basisCents=cents, isSpot=False))
    if not rows:
        log.warning("Agrex: no bids parsed for %s", cfg["location"])
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                              location=cfg["location"], source="web", rows=rows)


def fetch_agrex_bids() -> tuple[list[NewSnapshotRequest], list[dict]]:
    reqs, metas = [], []
    for cfg in SITES:
        req = parse_site(cfg)
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
    reqs, metas = fetch_agrex_bids()
    for req in reqs:
        print(f"  {req.provider} · {req.location} — {len(req.rows)} row(s)")
        for r in req.rows:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"     {r.deliveryMonth:16s} {r.futuresSymbol:7s} {sign}{r.basisCents}c  {r.grain}")
