"""georges_scraper.py — Georges Inc. (georgesinc.com) #2 Yellow Corn basis PDF.

Georges (poultry feed) publishes a single "BID-SHEET.pdf" of #2 Yellow Corn cash
basis for its four feed mills (Springdale AR, Cassville MO, Harrisonburg VA, Mt
Jackson VA). Each row is a delivery period (e.g. `Sep-26`) with an explicit option
month (MAR/MAY/JUL/SEP/DEC → CH/CK/CN/CU/CZ) and WEEK-1..5 basis columns in $/bu.
The option month + delivery year give the exact contract, so there's no roll math.

The sheet stamps its own date; it is NOT updated daily (feed-mill sheet), so a
freshness guard skips anything older than 10 days rather than backfill stale
numbers as "today". pdfplumber is a dev dep → runs in the local auto_import and
skips cleanly on Cloud.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

import requests

from models import NewSnapshotRequest, SnapshotRow

log = logging.getLogger(__name__)

_PDF_URL = "https://www.georgesinc.com/wp-content/uploads/2026/04/BID-SHEET.pdf"
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")}
_MAX_AGE_DAYS = 10

# ALL-CAPS "CITY, ST" header → (clean location, state, feed-mill facility).
_LOCATIONS = {
    "SPRINGDALE, AR": ("Springdale, AR", "AR"),
    "CASSVILLE, MO": ("Cassville, MO", "MO"),
    "HARRISONBURG, VA": ("Harrisonburg, VA", "VA"),
    "MT JACKSON, VA": ("Mt Jackson, VA", "VA"),
}
_OPT = {"MAR": ("H", 3), "MAY": ("K", 5), "JUL": ("N", 7), "SEP": ("U", 9), "DEC": ("Z", 12)}
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

_LOC_RE = re.compile(r"\b([A-Z][A-Z .]+,\s*[A-Z]{2})\b")
_DELIV_RE = re.compile(r"\b([A-Z][a-z]{2})-(\d{2})\b\s+(MAR|MAY|JUL|SEP|DEC)\b")
_BASIS_RE = re.compile(r"\$?\s*(-?\d+\.\d{3,4})")
_SHEET_DATE = re.compile(r"([A-Z][a-z]+day,\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4})")


def _contract(opt: str, deliv_mon: str, deliv_yy: int) -> str:
    """Option month + delivery period → CME corn symbol (ZCU26 …). The contract
    year is the option month's occurrence at/after the delivery month."""
    code, opt_m = _OPT[opt]
    dmon = _MON[deliv_mon.upper()]
    year = 2000 + deliv_yy + (1 if opt_m < dmon else 0)
    return f"ZC{code}{year % 100:02d}"


def fetch_georges() -> tuple[list[NewSnapshotRequest], list[dict]]:
    """Scrape the Georges corn bid sheet. Returns (snapshot requests, location metas).
    Skips when pdfplumber is unavailable (Cloud) or the sheet is stale."""
    try:
        import pdfplumber
    except ImportError:
        log.info("Georges: pdfplumber unavailable (Cloud) — skipping")
        return [], []
    try:
        pdf_bytes = requests.get(_PDF_URL, headers=_HEADERS, timeout=30).content
    except Exception as exc:
        log.error("Georges: PDF fetch failed: %s", exc)
        return [], []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page = pdf.pages[0]
            text = page.extract_text() or ""
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    except Exception as exc:
        log.error("Georges: PDF parse failed: %s", exc)
        return [], []

    md = _SHEET_DATE.search(text)
    if md:
        try:
            sheet_dt = datetime.strptime(md.group(1), "%A, %B %d, %Y").date()
            age = (datetime.now(timezone.utc).date() - sheet_dt).days
            if age > _MAX_AGE_DAYS:
                log.warning("Georges: sheet dated %s is %d days stale — skipping "
                            "(feed-mill sheet updated infrequently)", sheet_dt, age)
                return [], []
        except ValueError:
            pass

    # Cluster words into rows (each row starts at a location's delivery period; the
    # ALL-CAPS "CITY, ST" header rides on the block's first row).
    line_rows: list[list[tuple[float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    for w in sorted(words, key=lambda w: w["top"]):
        if cur and w["top"] - cur[0][0] > 5:
            line_rows.append(sorted((x, t) for _, x, t in cur))
            cur = []
        cur.append((w["top"], w["x0"], w["text"]))
    if cur:
        line_rows.append(sorted((x, t) for _, x, t in cur))

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    by_loc: dict[str, list[SnapshotRow]] = {}
    seen: dict[str, set] = {}
    location = None

    for toks in line_rows:
        line = " ".join(t for _, t in toks)
        lm = _LOC_RE.search(line)
        if lm and lm.group(1).strip() in _LOCATIONS:
            location = _LOCATIONS[lm.group(1).strip()]
        dm = _DELIV_RE.search(line)
        if not dm or location is None:
            continue
        deliv_mon, deliv_yy, opt = dm.group(1), int(dm.group(2)), dm.group(3)
        # basis = first $/bu value after the option month
        after = line[dm.end():]
        bm = _BASIS_RE.search(after)
        if not bm:
            continue
        cents = int(round(float(bm.group(1)) * 100))
        if cents == 0:
            continue
        cme = _contract(opt, deliv_mon, deliv_yy)
        deliv = f"{deliv_mon} {2000 + deliv_yy}"
        loc_name = location[0]
        s = seen.setdefault(loc_name, set())
        if deliv in s:
            continue
        s.add(deliv)
        by_loc.setdefault(loc_name, []).append(SnapshotRow(
            id=f"CN_{cme}_{deliv_mon.upper()}{deliv_yy}", grain="Corn",
            deliveryMonth=deliv, futuresSymbol=cme, basisCents=cents, isSpot=False))

    reqs, metas = [], []
    for loc_name, rows in by_loc.items():
        state = next((st for (ln, st) in _LOCATIONS.values() if ln == loc_name), None)
        reqs.append(NewSnapshotRequest(timestamp=ts, provider="Georges",
                                       location=loc_name, source="web", rows=rows))
        metas.append({"provider": "Georges", "location": loc_name,
                      "state": state, "facility_type": "Feed Mill"})
    log.info("Georges: %d location(s)", len(reqs))
    return reqs, metas


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    reqs, _ = fetch_georges()
    for r in reqs:
        print(f"\n{r.provider} · {r.location}  ({len(r.rows)} rows)")
        for x in r.rows:
            print(f"   {x.grain:5} {x.deliveryMonth:9} {x.futuresSymbol} {x.basisCents:+d}")
