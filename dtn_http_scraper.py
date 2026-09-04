"""dtn_http_scraper.py — browser-free replacement for dtn_playwright_scraper.

DTN/aghost cash-bid pages hide the basis/cash behind a per-load `displayNumber(x)`
JS obfuscation: the real value = x run through a sequence of `x = <±const>` lines
(constants randomized every request) then rounded. Both the args and the decoder
are in the served HTML, so we fetch over HTTP, rebuild displayNumber() in Python,
and decode — no Chromium.

Row layout (single): <th>delivery</th> displayNumber(basis) displayNumber(cash)
@symbol futures-price futures-change. So per bid row the FIRST displayNumber arg
is the basis. Same output shape as dtn_playwright_scraper.fetch_dtn_playwright so
auto_import can swap it in.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from models import NewSnapshotRequest, SnapshotRow
from vistacomm_scraper import _fut_symbol, _basis_cents, _PFX, _DTN_RE

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# aghost SINGLE-layout sites (same platform / displayNumber obfuscation) — fully
# browser-free here. Not handled by this module (see fetch_dtn_playwright fallback):
#   * Glacial Lakes (corn.glaciallakesenergy.com) — same obfuscation but COLUMNAR
#     (3 plants side-by-side); needs a per-column parser (TODO).
#   * GreenAmerica (greenamericabiofuels.com) — a NEWER DTN "content-services"
#     JS widget, not aghost; different crack entirely.
#   * Heron Lake (heronlakebioenergy.com/index.cfm) — URL now 404s (site moved).
SITES: list[dict] = [
    {"provider": "E Energy", "location": "Adams, NE", "state": "NE",
     "facility_type": "Corn Processing", "grain": "Corn",
     "url": "https://corn.eenergyadams.com/index.cfm?show=11&mid=6"},
    {"provider": "Dakota Ethanol", "location": "Wentworth, SD", "state": "SD",
     "facility_type": "Corn Processing", "grain": "Corn",
     "url": "https://www.dakotaethanol.com/index.cfm?show=11&mid=3"},
    {"provider": "Pennsylvania Grain Processing", "location": "Clymer, PA", "state": "PA",
     "facility_type": "Corn Processing", "grain": "Corn",
     "url": "http://dtn.pagrain.com/index.cfm?show=11&mid=3"},
]

_SYM_RE = re.compile(r"@[A-Z]{1,2}\d[FGHJKMNQUVXZ]")
_ARITH_RE = re.compile(r"^[\dx.+\-()\s]+$")
_DELIV_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)\b|FH |LH |NC |Balance|"
    r"By |Split|OND|\bND\b|JFM|AMJJ", re.I)


_COND_RE = re.compile(r"^[\dx.+\-()\s><=]+$")


def _fn_body(html: str) -> str | None:
    """Brace-match the displayNumber function body."""
    i = html.find("function displayNumber")
    if i < 0:
        return None
    b = html.find("{", i)
    if b < 0:
        return None
    depth = 0
    for j in range(b, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[b + 1:j]
    return None


def _build_decoder(html: str):
    """Rebuild displayNumber(x) from the page's own JS. The obfuscation is a series of
    optionally-`if( A >= B )`-guarded `x = <arithmetic>` statements (both the guards
    and the constants are randomized per load), ending in document.write. We parse it
    into (condition, expr) ops and interpret them. Returns a decode fn or None."""
    body = _fn_body(html)
    if body is None:
        return None
    ops = []
    for stmt in body.split(";"):
        s = stmt.strip()
        if not s or s == "x = x" or s.replace(" ", "") == "x=x":
            continue
        if "document.write" in s or "return" in s:
            break
        m = re.match(r"if\s*\((.+?)\)\s*x\s*=\s*(.+)$", s, re.S)      # guarded assignment
        if m:
            cond, expr = m.group(1).strip(), m.group(2).strip()
            if not _COND_RE.match(cond) or not _ARITH_RE.match(expr):
                return None
            ops.append((cond, expr))
            continue
        m = re.match(r"x\s*=\s*(.+)$", s, re.S)                       # plain assignment
        if m:
            expr = m.group(1).strip()
            if not _ARITH_RE.match(expr):
                return None
            ops.append((None, expr))
    if not ops:
        return None

    def decode(arg) -> float | None:
        try:
            x = float(arg)
            for cond, expr in ops:
                if cond is None or eval(cond, {"__builtins__": {}}, {"x": x}):
                    x = eval(expr, {"__builtins__": {}}, {"x": x})
            return round(x, 2)
        except Exception:
            return None

    return decode


_DN_RE = re.compile(r"displayNumber\(([-0-9.]+)\s*,\s*\d\)")
# delivery label anywhere: opt FH/LH, then a month (abbrev or full, any case) or
# "New Crop", then a 4-digit year. Handles Sept/SEPT/September 2026, New Crop 2027.
_DELIV_RE_HTML = re.compile(
    r"(?:FH |LH )?(?:New Crop|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?)"
    r"\s*20\d\d", re.I)


def _parse_site(html: str, cfg: dict) -> NewSnapshotRequest | None:
    decode = _build_decoder(html)
    if decode is None:
        log.warning("DTN(http) %s: no displayNumber decoder found", cfg["location"])
        return None
    pfx = _PFX.get({"Corn": "ZC", "Soybeans": "ZS"}.get(cfg["grain"], ""), "XX")

    # Position-based, DOM-structure-agnostic: displayNumber args run [basis, cash] per
    # row in document order, so even-indexed calls are the basis. For each, the row's
    # delivery is the nearest label BEFORE it and the futures symbol is the nearest @
    # AFTER it. Works whether the markup is one lumped table or clean per-row <tr>s.
    dns = [(m.start(), m.group(1)) for m in _DN_RE.finditer(html)]
    delivs = [(m.start(), re.sub(r"\s+", " ", m.group(0)).strip())
              for m in _DELIV_RE_HTML.finditer(html)]
    syms = [(m.start(), m.group(0)) for m in _SYM_RE.finditer(html)]

    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for idx in range(0, len(dns) - 1, 2):             # each row = a (basis, cash) pair
        (pos, a0), (_, a1) = dns[idx], dns[idx + 1]
        # basis vs cash order flips across sites — basis is the small one (|v| < 2),
        # cash is the ~4-6 flat price. Pick the small member of the pair as basis.
        cands = [v for v in (decode(a0), decode(a1)) if v is not None and abs(v) < 2]
        if not cands:
            continue
        bv = min(cands, key=abs)
        delivery = next((d for p, d in reversed(delivs) if p < pos), "")
        # symbol: the one physically CLOSEST to this basis cell (same row) — its column
        # sits before the DN on some sites, after on others, and repeats per row.
        sym = min(syms, key=lambda ps: abs(ps[0] - pos))[1] if syms else ""
        if not delivery or not sym:
            continue
        cme = _fut_symbol(sym)
        basis = _basis_cents(f"{bv:.2f}")
        if not cme or basis is None:
            continue
        del_key = "".join(ch for ch in delivery.upper() if ch.isalnum()) or cme
        row_id = f"{pfx}_{cme}_{del_key}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(SnapshotRow(id=row_id, grain=cfg["grain"], deliveryMonth=delivery,
                                futuresSymbol=cme, basisCents=basis, isSpot=False))
    if not rows:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                              location=cfg["location"], source="web", rows=rows)


def fetch_dtn_http(timeout: int = 25) -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Fetch the aghost cash-bid sites over HTTP (no browser). Returns
    (snapshot requests, location metas), matching fetch_dtn_playwright."""
    reqs, metas = [], []
    with httpx.Client(headers={"user-agent": _UA}, timeout=timeout,
                      follow_redirects=True) as client:
        for cfg in SITES:
            try:
                html = client.get(cfg["url"]).text
            except Exception as exc:
                log.warning("DTN(http) %s fetch failed: %s", cfg["location"], exc)
                continue
            req = _parse_site(html, cfg)
            if req is None:
                log.warning("DTN(http): no bids parsed for %s", cfg["location"])
                continue
            reqs.append(req)
            metas.append({"provider": cfg["provider"], "location": cfg["location"],
                          "state": cfg.get("state"), "facility_type": cfg.get("facility_type")})
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, metas = fetch_dtn_http()
    for req in reqs:
        print(f"\n  {req.provider} · {req.location} — {len(req.rows)} row(s)")
        for r in req.rows[:14]:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"     {r.deliveryMonth:14s} {r.futuresSymbol:7s} {sign}{r.basisCents}c")
