"""
dtn_playwright_scraper.py — headless-browser scraper for DTN/aghost cash-bid pages
whose basis is COMPUTED CLIENT-SIDE (blank in the served HTML, injected by DTN's JS
after load — no clean data endpoint, unlike VistaComm). We render the page in
headless Chromium, let the JS run, then read the finished grid from the DOM.

This is the deliberate exception to the project's "requests only" rule: DTN's
aghost/ColdFusion widgets (Heron Lake, etc.) leave no JSON to hit. Runs only in
auto_import on the local machine (playwright is a requirements-dev dep), guarded on
its own budget so a slow render never stalls the daily run.

Rows come out as [{delivery, symbol '@C6U', basis '-0.35'}] via one page.evaluate;
`@C{yeardigit}{monthcode}` → CME symbol (shared with vistacomm_scraper helpers).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from models import NewSnapshotRequest, SnapshotRow
from vistacomm_scraper import _fut_symbol, _basis_cents, _PFX, _DTN_RE

log = logging.getLogger(__name__)

# One row per plant page. `grain` is what the page's default commodity table is.
SITES: list[dict] = [
    {"provider": "Heron Lake BioEnergy", "location": "Heron Lake, MN", "state": "MN",
     "facility_type": "Corn Processing", "grain": "Corn",
     "url": "http://www.heronlakebioenergy.com/index.cfm?show=11&mid=8"},
]

# In the rendered DOM each cash-bid row is: <th>delivery</th> <td>futures price</td>
# <td>@C6U</td> <td>basis</td> <td>cash</td>. Pull delivery + the @-symbol + the
# cell right after it (basis). Returns a JSON-able list.
_EXTRACT_JS = r"""
() => {
  const out = [];
  const rows = document.querySelectorAll('tr');
  for (const tr of rows) {
    const cells = [...tr.querySelectorAll('th,td')].map(c => (c.innerText||'').trim());
    if (cells.length < 4) continue;
    const fi = cells.findIndex(c => /^@[A-Z]{1,2}\d[FGHJKMNQUVXZ]$/.test(c));
    if (fi < 0) continue;
    const delivery = cells[0];
    const basis = (cells[fi+1] || '').replace(/[^0-9.\-]/g, '');
    if (delivery && basis !== '') out.push({delivery, symbol: cells[fi], basis});
  }
  return out;
}
"""


def _rows_to_snapshot(cfg: dict, raw: list[dict]) -> NewSnapshotRequest | None:
    grain = cfg["grain"]
    pfx = _PFX.get({"Corn": "ZC", "Soybeans": "ZS"}.get(grain, ""), "XX")
    rows: list[SnapshotRow] = []
    seen: set[str] = set()
    for r in raw:
        if not _DTN_RE.match(r["symbol"] or ""):
            continue
        cme = _fut_symbol(r["symbol"])
        basis = _basis_cents(r["basis"])
        if not cme or basis is None:
            continue
        delivery = (r["delivery"] or "").strip()
        # skip the nested-table wrapper / header rows (a real delivery is short)
        if len(delivery) > 18 or "delivery" in delivery.lower():
            continue
        del_key = "".join(ch for ch in delivery.upper() if ch.isalnum()) or cme
        row_id = f"{pfx}_{cme}_{del_key}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(SnapshotRow(id=row_id, grain=grain, deliveryMonth=delivery,
                                futuresSymbol=cme, basisCents=basis, isSpot=False))
    if not rows:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return NewSnapshotRequest(timestamp=ts, provider=cfg["provider"],
                              location=cfg["location"], source="web", rows=rows)


def fetch_dtn_playwright(timeout_ms: int = 25000) -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Render each SITES page in headless Chromium, read the grid. Returns
    (snapshot requests, location metas). One browser for all sites."""
    from playwright.sync_api import sync_playwright
    reqs, metas = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for cfg in SITES:
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
            try:
                page.goto(cfg["url"], timeout=timeout_ms, wait_until="domcontentloaded")
                # wait for the DTN JS to inject a basis (an @-symbol row with a number)
                page.wait_for_function(
                    "() => [...document.querySelectorAll('td')].some(c => /^@[A-Z]{1,2}\\d[FGHJKMNQUVXZ]$/.test((c.innerText||'').trim()))",
                    timeout=timeout_ms)
                page.wait_for_timeout(1500)      # let the basis cells fill
                raw = page.evaluate(_EXTRACT_JS)
            except Exception as exc:
                log.error("DTN(pw) render failed for %s: %s", cfg["location"], exc)
                page.close()
                continue
            page.close()
            req = _rows_to_snapshot(cfg, raw or [])
            if req is None:
                log.warning("DTN(pw): no bids parsed for %s", cfg["location"])
                continue
            reqs.append(req)
            metas.append({"provider": cfg["provider"], "location": cfg["location"],
                          "state": cfg.get("state"), "facility_type": cfg.get("facility_type")})
        browser.close()
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, metas = fetch_dtn_playwright()
    for req in reqs:
        print(f"  {req.provider} · {req.location} — {len(req.rows)} row(s)")
        for r in req.rows[:8]:
            sign = "+" if (r.basisCents or 0) >= 0 else ""
            print(f"     {r.deliveryMonth:12s} {r.futuresSymbol:7s} {sign}{r.basisCents}c")
