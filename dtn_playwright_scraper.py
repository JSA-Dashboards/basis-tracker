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

# `layout`: "single" (one plant per page) or "columnar" (one grid, several plants
# side by side — locations read from the header). `grain` = the page's default table.
# EMPTY — every DTN plant is now browser-free (see dtn_http_scraper.py: aghost
# single + columnar decode, and the DTN content-services JSON API for GreenAmerica;
# Heron Lake + Granite Falls went to cihedging_scraper.py). Nothing here needs a
# headless browser anymore. Kept as a hook in case a site reverts to a JS-only
# render — add it back here and fetch_dtn_playwright will pick it up.
SITES: list[dict] = []

# In the rendered DOM each cash-bid row is: <th>delivery</th> <td>futures price</td>
# <td>@C6U</td> <td>basis</td> <td>cash</td>. Pull delivery + the @-symbol + the
# cell right after it (basis). Returns a JSON-able list.
_EXTRACT_JS = r"""
() => {
  const out = [];
  for (const tr of document.querySelectorAll('tr')) {
    const cells = [...tr.querySelectorAll('th,td')].map(c => (c.innerText||'').trim());
    if (cells.length < 4) continue;
    const fi = cells.findIndex(c => /^@[A-Z]{1,2}\d[FGHJKMNQUVXZ]$/.test(c));
    if (fi < 0) continue;
    // delivery = a month/period-looking cell before the symbol (some pages put a
    // commodity name in cell 0, so it isn't always cells[0]).
    const DEL = /\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)\b|FH |LH |NC |Balance|By |Split|OND|\bND\b|JFM|AMJJ/i;
    let delivery = '';
    for (let i = fi - 1; i >= 0; i--) { if (DEL.test(cells[i])) { delivery = cells[i]; break; } }
    if (!delivery) delivery = cells[0];
    // basis column order varies across aghost pages (before OR after the symbol),
    // so pick the small signed decimal (|v|<2) — distinct from cash (~4.xx) and
    // the tick-format futures price (438'2).
    let basis = '';
    for (const c of cells) {
      const m = c.match(/^(-?\d+\.\d\d)$/);
      if (m && Math.abs(parseFloat(m[1])) < 2) { basis = m[1]; break; }
    }
    if (delivery && basis !== '') out.push({delivery, symbol: cells[fi], basis});
  }
  return out;
}
"""


# Columnar grid (several plants side by side): each data row is
# delivery, then repeating [futures price, @sym, basis, cash] per plant. Plant names
# come from a header row of location-looking cells ("GLE - Watertown, SD").
_EXTRACT_COLUMNAR_JS = r"""
() => {
  const tables = [...document.querySelectorAll('table')]
    .filter(t => /@[A-Z]{1,2}\d/.test(t.innerText) && /basis/i.test(t.innerText));
  let best = null, bc = 0;
  for (const t of tables) { const n = (t.innerText.match(/@[A-Z]{1,2}\d[FGHJKMNQUVXZ]/g)||[]).length; if (n > bc) { bc = n; best = t; } }
  if (!best) return {plants: [], rows: []};
  const grid = [...best.querySelectorAll('tr')].map(tr => [...tr.querySelectorAll('th,td')].map(c => (c.innerText||'').trim()));
  // header row = the one with the MOST clean "City, ST" (or "GLE - City, ST") cells
  let plants = [], bestCount = 0;
  for (const r of grid) {
    const locs = r.filter(c => /^([A-Za-z]+\s*-\s*)?[A-Za-z][A-Za-z .]*,\s*[A-Z]{2}$/.test(c));
    if (locs.length > bestCount) { bestCount = locs.length; plants = locs.map(c => c.replace(/^[A-Za-z]+\s*-\s*/, '').trim()); }
  }
  const out = [];
  for (const r of grid) {
    const delivery = r[0];
    if (!delivery || delivery.length > 18 || /futures|basis|delivery/i.test(delivery)) continue;
    let k = 0;
    for (let i = 0; i < r.length; i++) {
      if (/^@[A-Z]{1,2}\d[FGHJKMNQUVXZ]$/.test(r[i])) {
        const basis = (r[i+1] || '').replace(/[^0-9.\-]/g, '');
        const plant = plants[k] || ('col' + k); k++;
        if (basis !== '') out.push({plant, delivery, symbol: r[i], basis});
      }
    }
  }
  return {plants, rows: out};
}
"""


def _build_snapshot(provider: str, location: str, grain: str,
                    raw: list[dict]) -> NewSnapshotRequest | None:
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
    return NewSnapshotRequest(timestamp=ts, provider=provider, location=location,
                              source="web", rows=rows)


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# wait for the DTN JS to inject a basis (an @-symbol row appears in a <td>)
_WAIT_JS = ("() => [...document.querySelectorAll('td')].some(c => "
            "/^@[A-Z]{1,2}\\d[FGHJKMNQUVXZ]$/.test((c.innerText||'').trim()))")


def fetch_dtn_playwright(timeout_ms: int = 40000, attempts: int = 2
                         ) -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Render each SITES page in headless Chromium, read the grid. Returns
    (snapshot requests, location metas). One browser for all sites.

    Robustness (aghost renders are slow/flaky — Heron Lake especially): each site
    gets a generous per-render wait and one retry on a fresh page, and the Chromium
    launch itself is retried once. So a single slow site — or a cold-launch hiccup in
    the non-interactive Task Scheduler context — no longer blanks the whole batch."""
    reqs, metas = [], []
    if not SITES:                      # all DTN plants are browser-free now — skip
        return reqs, metas             # (don't even import/launch playwright)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = None
        for launch_try in range(1, 3):
            try:
                browser = p.chromium.launch(headless=True)
                break
            except Exception as exc:
                log.warning("DTN(pw) Chromium launch attempt %d/2 failed: %s", launch_try, exc)
        if browser is None:
            log.error("DTN(pw) could not launch Chromium — skipping the DTN render batch")
            return reqs, metas
        for cfg in SITES:
            _label = cfg.get("location") or cfg.get("provider")
            raw = None
            for attempt in range(1, attempts + 1):
                page = browser.new_page(user_agent=_UA)
                try:
                    page.goto(cfg["url"], timeout=timeout_ms, wait_until="domcontentloaded")
                    page.wait_for_function(_WAIT_JS, timeout=timeout_ms)
                    page.wait_for_timeout(1500)      # let the basis cells fill
                    raw = page.evaluate(
                        _EXTRACT_COLUMNAR_JS if cfg.get("layout") == "columnar" else _EXTRACT_JS)
                    break
                except Exception as exc:
                    if attempt < attempts:
                        log.warning("DTN(pw) %s attempt %d/%d failed (%s) — retrying",
                                    _label, attempt, attempts, exc)
                    else:
                        log.error("DTN(pw) render failed for %s after %d attempts: %s",
                                  _label, attempts, exc)
                finally:
                    page.close()
            if raw is None:
                continue
            if cfg.get("layout") == "columnar":
                plants = {}
                for row in (raw or {}).get("rows", []):
                    plants.setdefault(row["plant"], []).append(row)
                built = 0
                for plant, rws in plants.items():
                    loc = plant.strip()
                    req = _build_snapshot(cfg["provider"], loc, cfg["grain"], rws)
                    if req is None:
                        continue
                    reqs.append(req)
                    metas.append({"provider": cfg["provider"], "location": loc,
                                  "state": cfg.get("state"), "facility_type": cfg.get("facility_type")})
                    built += 1
                if not built:
                    log.warning("DTN(pw): no bids parsed for %s", cfg["provider"])
            else:
                req = _build_snapshot(cfg["provider"], cfg["location"], cfg["grain"], raw or [])
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
