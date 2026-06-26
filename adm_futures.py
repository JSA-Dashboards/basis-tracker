"""
adm_futures.py — Futures curve harvested from ADM's Gradable feed (free, no API key).

Every ADM Gradable instrument already carries the underlying futures price and
symbol, e.g.:

    "futures": {
        "pricing": {"delayed": {"price": 4.115, ...}},   # dollars/bushel
        "symbols": {"fbn": "ZCN26", ...}                 # our internal symbol
    }

Unioning the futures across a couple of corn + soybean ADM locations yields a
{symbol -> price_in_cents} curve covering the standard CME contract months for
both commodities — enough to anchor forward basis curves without a paid
futures-quote provider. Prices are CME "delayed" quotes (~10-min lag).
"""
from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger(__name__)

ADM_BASE      = "https://adm.gradable.com"
BOOTSTRAP_URL = f"{ADM_BASE}/api/commodities/merchandising/bootstrap"
INSTR_TMPL    = (f"{ADM_BASE}/api/commodities/v2/merchandising/instruments"
                 "/market/{mid}?offer_type=public")
_CSRF         = "while(1);"
_HEADERS = {
    "Accept":     "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}
# Corn/soy processing plants each quote the full forward curve, so a couple of each
# suffices.
_HARVEST = ("corn processing", "soy processing")
# Wheat-milling locations quote ZW* but each only lists a few months (some none), so
# harvest ALL of them and union — Mendota/St. Louis carry the deferred 2027 contracts.
_HARVEST_ALL = ("wheat milling",)


def _parse(text: str) -> dict:
    if text.startswith(_CSRF):
        text = text[len(_CSRF):]
    return json.loads(text)


def fetch_futures_curve(per_type: int = 2) -> dict[str, float]:
    """Return {fbn_symbol -> futures_price_cents} for corn, soybeans & wheat.

    Harvests `per_type` corn-processing, soy-processing and wheat-milling ADM
    locations and unions their futures quotes. Empty dict on fatal error.
    """
    curve: dict[str, float] = {}
    with httpx.Client(headers=_HEADERS, timeout=30, follow_redirects=True) as c:
        try:
            markets = _parse(c.get(BOOTSTRAP_URL).text).get("markets", [])
        except Exception as exc:
            log.error("ADM futures bootstrap failed: %s", exc)
            return {}

        targets: list[dict] = []
        for kw in _HARVEST:
            targets += [m for m in markets
                        if kw in (m.get("display_name") or "").lower()][:per_type]
        for kw in _HARVEST_ALL:        # sparse per-location → take all and union
            targets += [m for m in markets
                        if kw in (m.get("display_name") or "").lower()]

        for mk in targets:
            try:
                data = _parse(c.get(INSTR_TMPL.format(mid=mk["id"])).text)
            except Exception as exc:
                log.warning("ADM futures fetch failed for %s: %s",
                            mk.get("display_name"), exc)
                continue
            for ins in data.get("instruments", []):
                fut = ins.get("futures") or {}
                sym = (fut.get("symbols") or {}).get("fbn")
                px  = ((fut.get("pricing") or {}).get("delayed") or {}).get("price")
                if sym and px is not None and sym not in curve:
                    curve[sym] = round(px * 100.0, 4)   # dollars/bu → cents

    log.info("ADM futures curve: %d contracts", len(curve))
    return curve


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    cur = fetch_futures_curve()
    for s in sorted(cur):
        print(f"  {s:7} {cur[s]:9.2f}c")
