"""
hfgrain_scraper.py — Hollander & Feuerhaken's daily bid sheet (https://www.hfgrain.com/bids).

H&F is a Northbrook, IL grain brokerage. Its Bids page is one link, "Today's Bid PDF" (/s/HFBids-YYYYMMDD.pdf, which redirects to Squarespace's CDN), to a one-page
landscape sheet built by Access: a block of delivery periods for each buyer it works — period, basis (cents), futures contract, flat price — plus a "Futures Settlements"
panel (corn / soybeans / wheat), the buyers' hours and a USDA calendar. The sheet is dated in its top-right corner and stamped REVISED when it is re-issued.

Every row can be checked: the flat price is the day's settlement of the row's contract plus the basis, so flat - basis must equal that settlement. A row that does not add up
(a mis-read cell, a typo on the sheet) is not filed, and nothing is filed when the settlement panel cannot be read, because then nothing can be checked.

The bids are H&F's own. Where we already scrape the same buyer they differ by a few cents from what the buyer posts (Cargill Hammond, ADM Mendota wheat, CHS Ingredion and
Rochelle on 2026-10-22), so filing them under the buyer would merge two different bid streams into one snapshot. Kolten 2026-10-23: "We are only needing Ingredion, South Bend Ethanol,
Lake side, and Incobrassa on this. Add inco into the existing one we have." So four blocks are taken (TARGETS, enabled True):
    Ingredion, South Bend Ethanol, Lakeside Grain   filed under their own names with source 'hfgrain' (Lakeside Grain = GROWMARK's Calumet River terminal in Chicago)
    Incobrasa                                       NOT a new location: its Oct/Nov/Dec bids (a subset of Inco's own six-month sheet) are folded into the existing INCO / Gilman, IL
                                                    series by inco_bids.fold_hf (the location dict says merge='inco')
The other four (Marquis Hennepin, Cargill Hammond, ADM Mendota wheat, CHS-Rochelle) are read and checked but not filed; flip `enabled` to take one, and add a TARGETS entry when H&F
starts quoting a new buyer (an unknown block is logged, not filed).

Only today's PDF is on the site: every older /s/HFBids-YYYYMMDD.pdf is a 404 (checked 2026-10-23), so there is no history to backfill. Rows are filed under the sheet's own date, not
the day we looked, so a page that has not been updated yet repeats an existing snapshot and adds nothing; the first capture of a day stays (like every other scraper). H&F posted
the 2026-10-22 sheet at 15:39 CT, six minutes before the 15:45 daily run, so a late posting is picked up by the next run (see deploy/run_hfgrain.sh). A sheet more than MAX_AGE_DAYS
old is not filed (the run reports 'empty').

Terms (read 2026-10-23): hfgrain.com has no terms-of-use or copyright page (Home / About / Bids / Contact), the Squarespace robots.txt leaves /bids and /s/ open, and the PDF carries no
restriction. One page and one PDF a day.

Needs pdfplumber (requirements-dev.txt; installed on the droplet, not on Streamlit Cloud, where the scrape is skipped).

Usage (standalone, prints what would be written; writes nothing):
    python hfgrain_scraper.py
"""
from __future__ import annotations

import html as _html
import io
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

_PAGE_URL = "https://www.hfgrain.com/bids"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}
MAX_AGE_DAYS = 5                    # a sheet older than this is not filed (a weekend and a holiday fit inside it)
_MAX_PDF_BYTES = 5_000_000          # the sheet is ~230 KB
_TOLERANCE = 0.011                  # flat - basis against the settlement, in cents

# The buyers on the sheet, keyed by the block header's buyer (lower case, before ' - Corn'). `enabled` blocks are filed under provider / location (or, with merge='inco', folded into the
# existing INCO series); the rest are checked only. Coordinates are town level, like the other location_meta rows; Lakeside's is the Calumet River stretch of Chicago's far south side
# (approximate). Ingredion is the Argo plant (Bedford Park, IL; Kolten 2026-10-23: "Argo"), filed in the existing Ingredion / Chicago, IL series — the Dow Jones Chicago processor basis
# the archive holds from 2010 to 2023-07 — not CHS's own 'Ingredion' location (Indianapolis), which is a different bid stream, 2c under H&F's.
TARGETS: dict[str, dict] = {
    "ingredion": {
        "enabled": True, "provider": "Ingredion", "location": "Chicago, IL", "abbr": "IN",
        "meta": {"state": "IL", "facility_type": "Corn Processing", "lat": 41.77, "lon": -87.81},
    },
    "south bend ethanol": {
        "enabled": True, "provider": "South Bend Ethanol", "location": "South Bend, IN", "abbr": "SB",
        "meta": {"state": "IN", "facility_type": "Corn Processing", "lat": 41.6764, "lon": -86.2520},
    },
    "lakeside grain": {
        "enabled": True, "provider": "Lakeside Grain", "location": "Chicago, IL (Calumet River)", "abbr": "LG",
        "meta": {"state": "IL", "facility_type": "River Terminal", "lat": 41.71, "lon": -87.54},
    },
    "incobrasa": {                                  # folded into the series inco_bids.py keeps (its location_meta is already there)
        "enabled": True, "merge": "inco", "provider": "INCO", "location": "Gilman, IL", "abbr": "IB", "meta": {},
    },
    # Also on the sheet, read and checked but not filed:
    "marquis hennepin": {"enabled": False, "provider": "Marquis Energy", "location": "Hennepin, IL", "abbr": "MQ",
                         "meta": {"state": "IL", "facility_type": "Corn Processing", "lat": 41.2542, "lon": -89.3423}},
    "cargill hammond": {"enabled": False, "provider": "Hollander & Feuerhaken", "location": "Cargill Hammond (H&F sheet)", "abbr": "CH", "meta": {"state": "IN", "facility_type": "Corn Processing"}},
    "adm mendota": {"enabled": False, "provider": "Hollander & Feuerhaken", "location": "ADM Mendota (H&F sheet)", "abbr": "AM", "meta": {"state": "IL", "facility_type": "Wheat Milling"}},
    "chs-rochelle": {"enabled": False, "provider": "Hollander & Feuerhaken", "location": "CHS Rochelle (H&F sheet)", "abbr": "CR", "meta": {"state": "IL", "facility_type": "Corn Processing"}},
}

_CONTRACT_RE = re.compile(r"^[CSW][FGHJKMNQUVXZ]\d{2}$")
_SETTLE_RE = re.compile(r"\b([CSW][FGHJKMNQUVXZ]\d{2})\s+(\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_FILE_DATE_RE = re.compile(r"HFBids[-+ _]?(\d{8})", re.I)
_UPLOAD_MS_RE = re.compile(r"/t/[0-9a-f]+/(\d{13})/")
# CBOT contract letter -> (our symbol root, grain). The H&F wheat is CBOT soft red winter (WZ26 = ZWZ26), the same raw grain ADM Mendota's wheat is filed under.
_ROOT = {"C": ("ZC", "Corn"), "S": ("ZS", "Soybeans"), "W": ("ZW", "Wheat (Soft Red Winter)")}
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_ABBR = {v: k.title() for k, v in _MON.items()}
_HALF = {"FH": "FH", "F/H": "FH", "LH": "LH", "L/H": "LH"}


# ── fetching ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def find_pdf_url(html: str | None, base: str = _PAGE_URL) -> str | None:
    """The 'Today's Bid PDF' link on the Bids page, as an absolute URL: the .pdf link whose address names HFBids, else any .pdf link whose text mentions a bid; None if neither."""
    links = re.findall(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', html or "", flags=re.S | re.I)
    pdfs = [(h, re.sub(r"<[^>]+>", "", t)) for h, t in links if re.search(r"\.pdf(\?|#|$)", h, re.I)]
    best = next((h for h, _ in pdfs if re.search(r"hfbids", h, re.I)), None) or next((h for h, t in pdfs if re.search(r"bid", t, re.I)), None)
    return urljoin(base, _html.unescape(best)) if best else None


def _get(url: str, what: str, attempts: int = 3, timeout=(15, 45)):
    """GET with a few tries (a small host times out now and then); the response, or None when every try failed."""
    last = None
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            return resp
        except Exception as exc:                    # noqa: BLE001
            last = exc
            log.warning("HFGrain: %s fetch attempt %d of %d failed: %s", what, i + 1, attempts, str(exc)[:120])
            if i + 1 < attempts:
                time.sleep(3 * (i + 1))
    log.error("HFGrain: %s fetch failed after %d attempts: %s", what, attempts, str(last)[:200])
    return None


def upload_time(final_url: str | None) -> datetime | None:
    """When the PDF was uploaded to Squarespace: the 13-digit millisecond stamp in its CDN address (.../t/<id>/<ms>/HFBids+20261022.pdf)."""
    m = _UPLOAD_MS_RE.search(final_url or "")
    return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc) if m else None


def fetch_pdf(url: str) -> tuple[bytes | None, datetime | None, str]:
    """(PDF bytes, upload time, final URL); bytes None when it could not be fetched or is not a PDF of a sane size."""
    resp = _get(url, "PDF")
    if resp is None:
        return None, None, url
    data = resp.content
    if not data.startswith(b"%PDF") or len(data) > _MAX_PDF_BYTES:
        log.error("HFGrain: %s is not a PDF of a sane size (%d bytes, starts %r)", resp.url, len(data), data[:8])
        return None, None, resp.url
    return data, upload_time(resp.url), resp.url


# ── reading the sheet (needs pdfplumber) ───────────────────────────────────────────────────────────────────────────────────────────────
def _bid_row(cells) -> dict | None:
    """One bid row [period, basis, contract, flat] as a dict, or None when the cells are not that shape."""
    if len(cells) != 4:
        return None
    period, basis, contract, flat = [re.sub(r"\s+", " ", (c or "").replace("\xa0", " ")).strip() for c in cells]
    basis = basis.replace("−", "-")
    if not period or not _CONTRACT_RE.match(contract) or not _NUM_RE.match(basis) or not _NUM_RE.match(flat.replace(",", "")):
        return None
    return {"period": period, "basis": float(basis), "contract": contract, "flat": float(flat.replace(",", ""))}


def _header_above(words: list[dict], bbox) -> str:
    """The block's title: the line of words just above the table, starting at its left edge (the next block's title starts further right, past this table's right edge)."""
    x0, top, x1, _ = bbox
    near = [w for w in words if w["bottom"] <= top + 2 and top - w["bottom"] < 30 and w["x0"] >= x0 - 4 and w["x0"] < x1 + 8]
    if not near:
        return ""
    line_top = max(round(w["top"]) for w in near)
    return " ".join(w["text"] for w in sorted((w for w in near if round(w["top"]) == line_top), key=lambda w: w["x0"]))


def extract_sheet(pdf_bytes: bytes) -> dict:
    """The sheet as plain data (raises ImportError without pdfplumber):

        {"date": date | None,          the sheet's own date (top right)
         "revised": bool,              the REVISED stamp is on it
         "blocks": [{"header": "South Bend Ethanol - Corn", "rows": [{"period": "OCT", "basis": 10.0, "contract": "CZ26", "flat": 470.5}, ...]}, ...],
         "settle": {"CZ26": 460.5, ...}}   the Futures Settlements panel

    A bid block is a table whose every row is period | basis | contract | flat; the settlement panels are the other tables (a title row, then one cell of 'CZ26 460.50 -2.75' lines)."""
    import pdfplumber
    out = {"date": None, "revised": False, "blocks": [], "settle": {}}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            for w in words:
                if w["top"] < 70 and out["date"] is None and _DATE_RE.match(w["text"]):
                    try:
                        out["date"] = datetime.strptime(w["text"], "%m/%d/%Y").date()
                    except ValueError:
                        pass
                if w["top"] < 70 and w["text"].upper() == "REVISED":
                    out["revised"] = True
            for t in page.find_tables():
                rows = t.extract()
                parsed = [_bid_row(r) for r in rows]
                if parsed and all(parsed):
                    out["blocks"].append({"header": _header_above(words, t.bbox), "rows": parsed})
                else:
                    text = "\n".join((c or "") for r in rows for c in r)
                    for contract, price, _chg in _SETTLE_RE.findall(text):
                        out["settle"][contract] = float(price)
    return out


# ── turning the sheet into bids (pure) ─────────────────────────────────────────────────────────────────────────────────────────────────
def parse_period(text: str, sheet: date) -> str | None:
    """The delivery label to store, which always carries a month name (delivery_period.canonical reads the first month it finds): 'OCT' -> 'Oct 2026', 'LH OCT' -> 'LH Oct 2026',
    'BY 10/25' (deliver by the 25th) -> 'By Oct 25'. A month before the sheet's month is next year's. None for a period it does not know."""
    t = " ".join((text or "").upper().replace("\xa0", " ").split())
    m = re.fullmatch(r"(?:(FH|LH|F/H|L/H)\s+)?([A-Z]{3})", t)
    if m and m.group(2) in _MON:
        mon = _MON[m.group(2)]
        year = sheet.year if mon >= sheet.month else sheet.year + 1
        half = _HALF.get(m.group(1) or "", "")
        return (f"{half} " if half else "") + f"{_ABBR[mon]} {year}"
    m = re.fullmatch(r"BY\s+(\d{1,2})/(\d{1,2})", t)
    if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
        return f"By {_ABBR[int(m.group(1))]} {int(m.group(2))}"
    return None


def _buyer(header: str) -> tuple[str, str | None]:
    """('south bend ethanol', 'corn') from 'South Bend Ethanol - Corn'; a title without a grain ('CHS-Rochelle') gives (title, None)."""
    h = " ".join((header or "").replace("–", "-").replace("—", "-").split())
    m = re.match(r"^(.*?)\s+-\s+(corn|soybeans?|wheat)\s*$", h, re.I)
    return (m.group(1).strip().lower(), m.group(2).lower()) if m else (h.lower(), None)


def build_locations(sheet: dict, filed: date | None = None) -> tuple[list[dict], dict]:
    """(locations, report). One dict per FILED location — {provider, location, timestamp (the sheet's date), sheet_date, revised, bids: [...], meta} — and a report of what the
    sheet held: {'filed': [...], 'checked_only': [...], 'unmapped': [...], 'bad_rows': [...], 'rows_ok': n}. A location with merge='inco' is not a new series: it is folded into INCO / Gilman, IL. Every row is checked against the settlement panel; a row that fails
    (or whose period or contract is unknown) goes into 'bad_rows' and is not filed."""
    sheet_date, settle = sheet["date"], sheet["settle"]
    report = {"filed": [], "checked_only": [], "unmapped": [], "bad_rows": [], "rows_ok": 0}
    by_loc: dict[tuple, dict] = {}
    counters: dict[tuple, int] = {}
    ts = sheet_date.strftime("%Y-%m-%dT00:00:00Z")
    for block in sheet["blocks"]:
        buyer, grain_word = _buyer(block["header"])
        target = TARGETS.get(buyer)
        if target is None:
            report["unmapped"].append(block["header"])
            log.warning("HFGrain: block %r is not in TARGETS — not filed (add it if you want it)", block["header"])
            continue
        good = []
        for r in block["rows"]:
            label = parse_period(r["period"], sheet_date)
            root = r["contract"][0]
            settled = settle.get(r["contract"])
            if label is None or root not in _ROOT:
                report["bad_rows"].append((block["header"], r, "unknown period or contract"))
                continue
            if settled is None or abs((r["flat"] - r["basis"]) - settled) > _TOLERANCE:
                report["bad_rows"].append((block["header"], r, "flat - basis != settlement (%s)" % settled))
                log.error("HFGrain: %s %s %s: flat %.2f - basis %+g does not equal the %s settlement %s — row not filed", block["header"], r["period"], r["contract"],
                          r["flat"], r["basis"], r["contract"], settled)
                continue
            good.append((label, r))
        report["rows_ok"] += len(good)
        if not target["enabled"]:
            report["checked_only"].append(block["header"])
            continue
        key = (target["provider"], target["location"])
        loc = by_loc.setdefault(key, {"provider": target["provider"], "location": target["location"], "timestamp": ts, "sheet_date": sheet_date.isoformat(),
                                      "revised": bool(sheet.get("revised")), "merge": target.get("merge"), "bids": [], "meta": dict(target["meta"])})
        for label, r in good:
            root = r["contract"][0]
            sym, grain = _ROOT[root]
            counters[key + (root,)] = counters.get(key + (root,), 0) + 1
            loc["bids"].append({
                "bid_id": f"HF_{target['abbr']}_{root}{counters[key + (root,)]}",
                "grain": grain,
                "delivery": label,
                "cme_symbol": sym + r["contract"][1:],
                "basis_cents": int(round(r["basis"])),
                "flat": r["flat"],                          # cents/bu as printed (not stored: snapshot rows hold the basis only)
            })
        if good:
            report["filed"].append(block["header"])
    return [l for l in by_loc.values() if l["bids"]], report


def fetch_hfgrain_bids(page_html: str | None = None, pdf_bytes: bytes | None = None, today: date | None = None, uploaded: datetime | None = None) -> list[dict]:
    """The bids to file from today's H&F sheet: one dict per filed location (see build_locations). `page_html`, `pdf_bytes`, `today` and `uploaded` are for tests (default: fetch the
    Bids page and the PDF it links, today's UTC date). Returns [] — and says why in the log — when pdfplumber is missing (Cloud), the page or PDF cannot be read, the sheet has no
    date, no bid blocks or no settlement panel (the layout changed), or it is more than MAX_AGE_DAYS old."""
    try:
        import pdfplumber                           # noqa: F401
    except ImportError:
        log.info("HFGrain: pdfplumber unavailable (Cloud) — skipping")
        return []
    pdf_url = None
    if pdf_bytes is None:
        resp = _get(_PAGE_URL, "Bids page")
        if resp is None:
            return []
        page_html = resp.text
        pdf_url = find_pdf_url(page_html)
        if not pdf_url:
            log.error("HFGrain: no PDF link on the Bids page (the page changed?)")
            return []
        pdf_bytes, uploaded, final_url = fetch_pdf(pdf_url)
        if pdf_bytes is None:
            return []
    elif page_html:
        pdf_url = find_pdf_url(page_html)
    try:
        sheet = extract_sheet(pdf_bytes)
    except Exception as exc:                        # noqa: BLE001
        log.error("HFGrain: PDF read failed: %s", exc)
        return []
    if sheet["date"] is None:
        log.error("HFGrain: no date at the top of the sheet — nothing filed")
        return []
    if not sheet["blocks"]:
        log.error("HFGrain: no bid blocks on the sheet (the layout changed?)")
        return []
    if not sheet["settle"]:
        log.error("HFGrain: no Futures Settlements panel on the sheet — the rows cannot be checked, nothing filed")
        return []
    fm = _FILE_DATE_RE.search(pdf_url or "")
    if fm and fm.group(1) != sheet["date"].strftime("%Y%m%d"):
        log.warning("HFGrain: the PDF's name says %s but the sheet is dated %s — using the sheet's date", fm.group(1), sheet["date"])
    today = today or datetime.now(timezone.utc).date()
    age = (today - sheet["date"]).days
    if age > MAX_AGE_DAYS or age < -2:
        log.error("HFGrain: the sheet is dated %s (%d day(s) from today) — not filed", sheet["date"], age)
        return []
    locs, rep = build_locations(sheet)
    for loc in locs:
        loc["uploaded"] = uploaded
    log.info("HFGrain: sheet of %s%s%s, %d day(s) old — %d row(s) checked ok; filed: %s; checked only: %s; not in TARGETS: %s; bad rows: %d",
             sheet["date"], " (REVISED)" if sheet["revised"] else "", (", uploaded %s UTC" % uploaded.strftime("%Y-%m-%d %H:%M")) if uploaded else "", age, rep["rows_ok"],
             ", ".join(f"{l['provider']} / {l['location']} {len(l['bids'])}" for l in locs) or "nothing", ", ".join(rep["checked_only"]) or "none",
             ", ".join(rep["unmapped"]) or "none", len(rep["bad_rows"]))
    return locs


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                               # noqa: BLE001
        pass
    for loc in fetch_hfgrain_bids():
        print("=" * 60)
        print(f"  {loc['provider']} / {loc['location']}   sheet of {loc['sheet_date']}{' (REVISED)' if loc['revised'] else ''}   meta {loc['meta']}")
        for b in loc["bids"]:
            print(f"    {b['grain']:<24} {b['delivery']:<14} {b['cme_symbol']:<7} basis {b['basis_cents']:+4d}   flat {b['flat']:.2f}")
