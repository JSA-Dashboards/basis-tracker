"""hec_scraper.py — Hopkinsville Elevator Co. (hopelevator.com) cash-basis PDF.

HEC publishes its cash bids as a single PDF ("Bid Sheet" on /todays-cash-bids),
refreshed daily — the sheet header carries the date/time. It's a spatial grid:
each commodity section (Corn / Soybeans / Wheat) has a header row of delivery
periods (each followed by "Basis") and one row per location, where every cell is
`$ <CONTRACT> <basis> [cash…]` (e.g. `CZ26 -0.30`). The contract is explicit
(CZ26 → ZCZ26), so there's no roll-guessing; basis is $/bu → ×100¢.

We parse with pdfplumber word positions: split each section's header by "Basis"
to get the period labels in column order, then map each location row's
(contract, basis) pairs to those columns by x-position.

pdfplumber is a requirements-dev dependency, so like the ADM/Mendota PDF parsers
this runs in the LOCAL auto_import (and is skipped gracefully on Streamlit Cloud).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_PAGE = "https://www.hopelevator.com/todays-cash-bids"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": _PAGE,
}

# HEC locations → state. Mostly SW Kentucky; Clarksville is across the TN line.
_STATE = {"Hopkinsville": "KY", "Casky": "KY", "South Union": "KY", "Skyline": "KY",
          "Russellville": "KY", "Guthrie": "KY", "Planters": "KY", "Clarksville": "TN"}
_LOCS = sorted(_STATE, key=len, reverse=True)          # longest-first (match "South Union" before "South")

# Section commodity → (grain label, CME root). The PDF contract is <letter><mon><yy>.
_COMMODITY = {"CORN": ("Corn", "ZC"), "SOYBEANS": ("Soybeans", "ZS"), "WHEAT": ("Wheat", "ZW")}
_ROOT = {"C": "ZC", "S": "ZS", "W": "ZW"}

_CONTRACT = re.compile(r"^([CSW])([FGHJKMNQUVXZ])(\d{2})$")   # CZ26, SX26, WH27
_DEC = re.compile(r"^-?\d+\.\d{1,2}$")
_SHEET_DATE = re.compile(r"([A-Z][a-z]+day,\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4})")


def _cme(contract: str) -> str | None:
    m = _CONTRACT.match(contract)
    if not m:
        return None
    return f"{_ROOT[m.group(1)]}{m.group(2)}{m.group(3)}"


def _grain_of(contract: str) -> str:
    return {"C": "Corn", "S": "Soybeans", "W": "Wheat"}[contract[0]]


def _clean_period(lbl: str) -> str:
    """Header period label → clean delivery tag (drop 'Basis'/'DP Only')."""
    s = re.sub(r"(?i)\bBasis\b|\bDP Only\b|\bOnly\b", " ", lbl)
    s = s.replace("'", "'")
    return re.sub(r"\s+", " ", s).strip(" -")


def _bid_pdf_url(session: requests.Session) -> str | None:
    """Find the current 'Bid Sheet' PDF link on the cash-bids page (its slug can
    change when re-uploaded), else fall back to the known short link."""
    try:
        html = session.get(_PAGE, timeout=25).text
    except Exception as exc:
        log.error("HEC: cash-bids page fetch failed: %s", exc)
        return None
    # the corn-basis bid sheet — anchor text "Bid Sheet", or a Corn-Basis file
    for pat in (r'href=["\']([^"\']+)["\'][^>]*>\s*Bid Sheet\s*<',
                r'href=["\']([^"\']*Corn-Basis[^"\']*\.pdf)["\']'):
        m = re.search(pat, html, re.I)
        if m:
            href = m.group(1)
            return href if href.startswith("http") else "https://www.hopelevator.com" + href
    return "https://www.hopelevator.com/s/Corn-Basis-update-r7ym.pdf"


def fetch_hec() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape the HEC cash-basis PDF. Returns (snapshot requests, location metas).
    Skips gracefully when pdfplumber is unavailable (e.g. Streamlit Cloud)."""
    try:
        import pdfplumber
    except ImportError:
        log.info("HEC: pdfplumber unavailable (Cloud) — skipping")
        return [], []

    session = requests.Session()
    session.headers.update(_HEADERS)
    url = _bid_pdf_url(session)
    if not url:
        return [], []
    try:
        pdf_bytes = session.get(url, timeout=30, allow_redirects=True).content
    except Exception as exc:
        log.error("HEC: PDF fetch failed: %s", exc)
        return [], []

    import io
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    # market -> {(grain, period): SnapshotRow}
    by_loc: dict[str, dict] = {}

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page = pdf.pages[0]
            page_text = page.extract_text() or ""
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    except Exception as exc:
        log.error("HEC: PDF parse failed: %s", exc)
        return [], []

    # Freshness: the sheet stamps its date; skip if older than 10 days so a stalled
    # page never backfills stale numbers as "today".
    md = _SHEET_DATE.search(page_text)
    if md:
        try:
            sheet_dt = datetime.strptime(md.group(1), "%A, %B %d, %Y").date()
            age = (datetime.now(timezone.utc).date() - sheet_dt).days
            if age > 10:
                log.warning("HEC: sheet dated %s is %d days stale — skipping", sheet_dt, age)
                return [], []
        except ValueError:
            pass

    # Cluster words into rows by top with a 5px tolerance (rows are ~9px apart;
    # fixed bucketing straddled boundaries and split a row's location off its cells).
    line_rows: list[list[tuple[float, str]]] = []
    cur: list[tuple[float, float, str]] = []           # (top, x0, text)
    for w in sorted(words, key=lambda w: w["top"]):
        if cur and w["top"] - cur[0][0] > 5:
            line_rows.append(sorted((x, t) for _, x, t in cur))
            cur = []
        cur.append((w["top"], w["x0"], w["text"]))
    if cur:
        line_rows.append(sorted((x, t) for _, x, t in cur))

    periods: list[tuple[float, str]] = []       # (x, label) per data column
    for toks in line_rows:
        texts = [t for _, t in toks]
        joined = " ".join(texts)
        up = joined.upper()

        # Period header row: contains "Basis" columns. Split by Basis → labels in order,
        # and record each column's x from the "Basis" token positions.
        if up.count("BASIS") >= 2 and not any(texts[0].startswith(l.split()[0]) for l in _LOCS):
            labels = [_clean_period(p) for p in re.split(r"(?i)basis", joined) if _clean_period(p)]
            basis_xs = [x for x, t in toks if t.lower() == "basis"]
            if labels and len(basis_xs) >= len(labels):
                periods = list(zip(basis_xs[:len(labels)], labels))
            continue

        # Data row: starts with a known location.
        loc = next((l for l in _LOCS if joined.startswith(l)), None)
        if not loc or not periods:
            continue

        # (contract, basis) pairs with x-position.
        pairs = []
        for i, (x, t) in enumerate(toks):
            if _CONTRACT.match(t):
                b = next((tt for _, tt in toks[i + 1:i + 3] if _DEC.match(tt)), None)
                if b is not None:
                    pairs.append((x, t, b))
        if not pairs:
            continue

        acc = by_loc.setdefault(loc, {})
        for x, contract, basis in pairs:
            cme = _cme(contract)
            if not cme:
                continue
            # nearest period column by x
            plabel = min(periods, key=lambda pc: abs(pc[0] - x))[1] if periods else contract
            grain = _grain_of(contract)
            try:
                cents = int(round(float(basis) * 100))
            except ValueError:
                continue
            rid = f"{_ROOT[contract[0]].replace('Z','')}_{cme}_{re.sub(r'[^A-Z0-9]','',plabel.upper())}"
            acc[(grain, plabel, cme)] = SnapshotRow(
                id=rid, grain=grain, deliveryMonth=plabel, futuresSymbol=cme,
                basisCents=cents, isSpot=False)

    reqs, metas = [], []
    for loc, cells in by_loc.items():
        rows_out = list(cells.values())
        if not rows_out:
            continue
        state = _STATE.get(loc)
        location = f"{loc}, {state}" if state else loc
        reqs.append(NewSnapshotRequest(timestamp=ts, provider="Hopkinsville Elevator",
                                       location=location, source="web", rows=rows_out))
        metas.append({"provider": "Hopkinsville Elevator", "location": location,
                      "state": state, "facility_type": "Country Elevator"})
    log.info("HEC: %d location(s)", len(reqs))
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, metas = fetch_hec()
    for r in sorted(reqs, key=lambda x: x.location):
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows:
            print(f"   {x.grain:9} {x.deliveryMonth:10} {x.futuresSymbol} {x.basisCents:+d}")
