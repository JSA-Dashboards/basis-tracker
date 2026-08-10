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
        info = _COMMODITY.get((a[0] or "").strip().upper())
        if not info:
            continue
        root, grain = info
        try:
            basis = int(round(float(a[1])))
        except (ValueError, TypeError):
            continue
        m = _D_RE.search(a[13] or "")
        if not m:
            continue
        month, year = _MON[m.group(1)], 2000 + int(m.group(2))
        cme = _fut_for(root, month, year)
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


def fetch_agricharts_md() -> tuple[list[NewSnapshotRequest], list[dict]]:
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
    reqs, metas = fetch_agricharts_md()
    for req in reqs:
        print(f"  {req.provider} · {req.location} — {len(req.rows)} row(s)")
        for r in req.rows[:8]:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"     {r.deliveryMonth:10s} {r.futuresSymbol:7s} {sign}{r.basisCents}c")
