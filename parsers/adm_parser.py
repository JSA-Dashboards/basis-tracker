"""
ADM cash bid email parser.
Handles the labeled-block format used by adm@notifications.adm.com:

  Cash Bids as of MM/DD/YYYY HH:MM AM/PM CT
  Decatur
  US BEANS
  ...
  Delivery:
   JUN
  Delivery Start:
   6/1/2026
  ...
  Basis:
   0.20
  USD/BU
  Futures Month:
   ZSN26
"""
import re
import io
from typing import Optional
from datetime import datetime, timedelta

from models import ParsedSnapshot, NewSnapshotRequest

# ── Location mapping ──────────────────────────────────────────────────────────
LOCATION_MAP = {
    "decatur":      "ADM Decatur",
    "cedar rapids": "ADM Cedar Rapids",
    "st. louis":    "ADM St. Louis",
    "st louis":     "ADM St. Louis",
}

# ── Grain mapping ─────────────────────────────────────────────────────────────
GRAIN_MAP = {
    "us beans":  "Soybeans",
    "us corn":   "Corn",
    "soybeans":  "Soybeans",
    "soybean":   "Soybeans",
    "corn":      "Corn",
    "wheat":     "Wheat",
    "srw wheat": "Wheat",
    "srw":       "Wheat",
}

GRAIN_PREFIX = {"Soybeans": "SB", "Corn": "CN", "Wheat": "WH"}


def _basis_to_cents(value_str: str) -> Optional[int]:
    """Convert '0.20' or '-0.10' (USD/bu) to integer cents (20, -10)."""
    try:
        return int(round(float(value_str.strip()) * 100))
    except (ValueError, TypeError):
        return None


def _parse_as_of_date(text: str) -> Optional[str]:
    """Extract ISO timestamp from 'Cash Bids as of MM/DD/YYYY HH:MM AM/PM CT'."""
    m = re.search(
        r'Cash Bids as of\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)',
        text, re.IGNORECASE)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2).strip()}", "%m/%d/%Y %I:%M %p")
        dt_utc = dt + timedelta(hours=5)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except ValueError:
        return None


def _detect_location(text: str) -> Optional[str]:
    tl = text.lower()
    for key, loc in LOCATION_MAP.items():
        if key in tl:
            return loc
    return None


# Regex patterns for splitting body by location section header
_LOC_HEADER_PATTERNS = {
    "ADM Decatur":      r"Decatur",
    "ADM Cedar Rapids": r"Cedar\s+Rapids",
    "ADM St. Louis":    r"St\.?\s*Louis",
}
_ALL_LOC_RE = re.compile(
    r'(?:^|\n)(Decatur|Cedar\s+Rapids|St\.?\s*Louis)\s*\n',
    re.IGNORECASE | re.MULTILINE,
)


def _extract_location_section(text: str, location: str) -> str:
    """
    Return only the portion of the email body that belongs to `location`.
    Each ADM email body contains data for multiple locations separated by
    a location-name line (e.g. 'Cedar Rapids').  We split on those headers
    and return the block that matches.
    """
    target_re = re.compile(
        _LOC_HEADER_PATTERNS.get(location, "NOMATCH"),
        re.IGNORECASE,
    )
    parts = _ALL_LOC_RE.split(text)
    # parts = [pre, loc1, text1, loc2, text2, ...]
    for i in range(1, len(parts), 2):
        if target_re.search(parts[i]):
            return parts[i + 1] if i + 1 < len(parts) else ""
    return text  # fallback: return full text if no section header found


def _split_grain_sections(text: str) -> list:
    """
    Split text into [(grain_name, section_text), ...].
    Only takes the FIRST occurrence of each grain type so we don't accidentally
    parse other locations' data that may appear later in the same email.
    """
    pattern = re.compile(
        r'(US BEANS|US CORN|SOYBEANS?|CORN|SRW WHEAT|SRW|WHEAT)',
        re.IGNORECASE)
    parts = pattern.split(text)
    sections = []
    seen_grains = set()
    i = 1
    while i < len(parts):
        grain_raw = parts[i].strip().lower()
        grain = next((v for k, v in GRAIN_MAP.items() if k in grain_raw), None)
        content = parts[i + 1] if i + 1 < len(parts) else ""
        if grain and grain not in seen_grains:
            sections.append((grain, content))
            seen_grains.add(grain)
        i += 2
    return sections


def _parse_labeled_blocks(section_text: str, grain: str) -> list:
    """Parse the labeled-block rows from the email body."""
    pattern = re.compile(
        r'Delivery:\s*\n\s*([^\n]+?)\s*\n'
        r'Delivery Start:\s*\n?\s*([\d/]+)\s*\n'
        r'Delivery End:\s*\n?\s*([\d/]+)\s*\n'
        r'Cash Price:\s*\n?\s*([\d.]+)\s*\n'
        r'USD/BU\s*\n'
        r'Basis:\s*\n?\s*([+-]?[\d.]+)\s*\n'
        r'USD/BU\s*\n'
        r'Futures Month:\s*\n?\s*(\w+)',
        re.MULTILINE,
    )

    month_counts = {}
    rows = []
    prefix = GRAIN_PREFIX.get(grain, "XX")

    for m in pattern.finditer(section_text):
        dm_raw = m.group(1).strip()
        basis  = _basis_to_cents(m.group(5))
        symbol = m.group(6).strip()

        month_counts[dm_raw] = month_counts.get(dm_raw, 0) + 1
        count = month_counts[dm_raw]

        if count == 1:
            dm = dm_raw
        elif count == 2:
            dm = f"{dm_raw} FH"
        else:
            dm = f"{dm_raw} LH"

        row_id = f"{prefix}_{dm.replace(' ', '_')}"
        rows.append({
            "id":            row_id,
            "grain":         grain,
            "deliveryMonth": dm,
            "futuresSymbol": symbol,
            "basisCents":    basis,
            "isSpot":        False,
        })

    return rows


def _parse_compact_table(section_text: str, grain: str) -> list:
    """
    Fallback parser for compact table format where each row is 8 consecutive lines:
      Delivery month
      Start date  (MM/DD/YYYY)
      End date    (MM/DD/YYYY)
      Cash price  (e.g. 4.40)
      Basis       (e.g. 0.15 or -0.10)
      Futures sym (e.g. ZCN26)
      Futures price
      Futures change
    """
    pattern = re.compile(
        r'^([A-Z][A-Z/0-9 ]+?)\s*\n'       # delivery month (JUN, N/C 26, OCT/NOV …)
        r'(\d{1,2}/\d{1,2}/\d{4})\s*\n'    # start date
        r'(\d{1,2}/\d{1,2}/\d{4})\s*\n'    # end date
        r'([\d.]+)\s*\n'                    # cash price
        r'([+-]?[\d.]+)\s*\n'              # basis
        r'(Z[CSW][A-Z]\d{2})',             # futures symbol
        re.MULTILINE,
    )

    month_counts = {}
    rows = []
    prefix = GRAIN_PREFIX.get(grain, "XX")

    for m in pattern.finditer(section_text):
        dm_raw = m.group(1).strip()
        basis  = _basis_to_cents(m.group(5))
        symbol = m.group(6).strip()

        month_counts[dm_raw] = month_counts.get(dm_raw, 0) + 1
        count = month_counts[dm_raw]
        if count == 1:
            dm = dm_raw
        elif count == 2:
            dm = f"{dm_raw} FH"
        else:
            dm = f"{dm_raw} LH"

        row_id = f"{prefix}_{dm.replace(' ', '_').replace('/', '_')}"
        rows.append({
            "id":            row_id,
            "grain":         grain,
            "deliveryMonth": dm,
            "futuresSymbol": symbol,
            "basisCents":    basis,
            "isSpot":        False,
        })

    return rows


def _parse_rows(section_text: str, grain: str) -> list:
    """Try labeled-block parser first, fall back to compact table."""
    rows = _parse_labeled_blocks(section_text, grain)
    if not rows:
        rows = _parse_compact_table(section_text, grain)
    return rows


def _parse_body_text(text: str, received: str, email_id: str, subject: str) -> ParsedSnapshot:
    # Use subject first (unambiguous), fall back to body
    location = _detect_location(subject) or _detect_location(text)
    if not location:
        return ParsedSnapshot(
            emailId=email_id, emailSubject=subject, emailDate=received,
            provider="ADM", snapshots=[], needsReview=True,
            parseError="Could not detect ADM location from subject or body.")

    timestamp = _parse_as_of_date(text) or received
    # Extract only the section of the body belonging to this location
    loc_text  = _extract_location_section(text, location)
    sections  = _split_grain_sections(loc_text)

    if not sections:
        return ParsedSnapshot(
            emailId=email_id, emailSubject=subject, emailDate=received,
            provider="ADM", snapshots=[], needsReview=True,
            parseError="Could not find grain sections (US BEANS / CORN / WHEAT).")

    all_rows = []
    for grain, section_text in sections:
        rows = _parse_rows(section_text, grain)
        all_rows.extend(rows)

    if not all_rows:
        return ParsedSnapshot(
            emailId=email_id, emailSubject=subject, emailDate=received,
            provider="ADM", snapshots=[], needsReview=True,
            parseError="Grain sections found but no rows parsed.")

    # Mark first row of each grain as spot
    grain_seen = set()
    for r in all_rows:
        if r["grain"] not in grain_seen:
            r["isSpot"]    = True
            r["spotGrain"] = r["grain"]
            grain_seen.add(r["grain"])

    snap = NewSnapshotRequest(
        timestamp=timestamp, provider="ADM", location=location,
        source="email", emailSubject=subject, emailDate=received,
        rows=all_rows,
    )
    return ParsedSnapshot(
        emailId=email_id, emailSubject=subject, emailDate=received,
        provider="ADM", snapshots=[snap], needsReview=False,
    )


def _parse_pdf(pdf_bytes: bytes, received: str, email_id: str, subject: str) -> ParsedSnapshot:
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        return _parse_body_text(text, received, email_id, subject)
    except Exception as e:
        return ParsedSnapshot(
            emailId=email_id, emailSubject=subject, emailDate=received,
            provider="ADM", snapshots=[], needsReview=True,
            parseError=f"PDF parse error: {e}")


# ── Web scraper parser (ADM Gradable API) ─────────────────────────────────────

# Normalise ADM's crm_display_name values → canonical grain labels
_GRAIN_NORM: dict[str, str] = {
    "corn":                    "Corn",
    "soybeans":                "Soybeans",
    "soybean":                 "Soybeans",
    "wheat":                   "Wheat",
    "soft red winter wheat":   "Wheat",
    "hard red winter wheat":   "Wheat",
    "hard red spring wheat":   "Wheat",
    "white wheat":             "Wheat",
    "soybean meal":            "Soy Meal",
    "sunflowers":              "Sunflowers",
    "milo":                    "Milo",
    "edible beans":            "Edible Beans",
    "oats":                    "Oats",
    "barley":                  "Barley",
    "canola":                  "Canola",
}
_GRAIN_PFX: dict[str, str] = {
    "Corn": "CN", "Soybeans": "SB", "Wheat": "WH", "Soy Meal": "SM",
    "Sunflowers": "SF", "Milo": "MI", "Edible Beans": "EB",
    "Oats": "OT", "Barley": "BA", "Canola": "CA",
}


def canadian_basis(instr: dict, usdcad: Optional[float]):
    """What to store for an instrument's basis: ('as_posted', None) | ('converted', US$ per bushel) | ('skip', None).

    ADM posts its Ontario corn, soybeans and wheat as a Canadian-dollar CASH price (`cash_info.in_ext_units`: currency 'cad', bushels) against futures in US dollars (`futures.currency`
    'usd'), and its `basis_bid` is simply the first minus the second: Blenheim corn C$5.785 - US$4.4875 = 1.2975, which looked like a +130 cent basis (soybeans +379). The real basis in US
    terms is cash / USDCAD - futures (USDCAD = Canadian dollars per US dollar): 5.785 / 1.405 - 4.4875 = -0.37. Canola is CAD against ICE canola futures in CAD per tonne: both sides are
    the same currency, so its posted basis is already a basis (stored as C$/t, grain_units.py). A US market (cash in USD) is untouched.
    A Canadian-dollar-against-US-futures instrument that cannot be converted exactly (no rate, no futures price, a unit other than bushels) is SKIPPED, never stored as the mixed number."""
    cash = (instr.get("cash_info") or {}).get("in_ext_units") or {}
    cash_cur = (cash.get("currency") or instr.get("currency") or "").lower()
    if cash_cur != "cad":
        return "as_posted", None
    fut = instr.get("futures") or {}
    if (fut.get("currency") or "").lower() != "usd":
        return "as_posted", None
    basis_unit = (((instr.get("basis_info") or {}).get("in_ext_units") or {}).get("quantity_unit") or "").lower()
    price = (((fut.get("pricing") or {}).get("delayed") or {}).get("price"))
    cash_bid = instr.get("cash_bid")
    if (not usdcad or usdcad <= 0 or price is None or cash_bid is None
            or basis_unit != "bushels" or (cash.get("quantity_unit") or "").lower() != "bushels" or (fut.get("quantity_unit") or "").lower() not in ("bushels", "bushel")):
        return "skip", None
    return "converted", float(cash_bid) / float(usdcad) - float(price)


def parse_instruments(
    market_id: int,
    display_name: str,
    instruments_data: dict,
    timestamp: str,
    usdcad: Optional[float] = None,
) -> Optional["NewSnapshotRequest"]:
    """
    Parse one ADM Gradable instruments response → NewSnapshotRequest.

    Used by adm_scraper.py and auto_import.py. `usdcad` (Canadian dollars per US dollar, fx_rates.py) converts Canadian-dollar-against-US-futures bids to US cents per bushel
    (canadian_basis); without it those instruments are skipped. Returns None if the location has no active bid rows.
    """
    # Build crop_id → grain name from this response's crops list
    crop_map: dict[int, str] = {}
    for crop in instruments_data.get("crops", []):
        cid = crop.get("crop_id")
        raw = crop.get("crm_display_name", "")
        if cid is not None:
            crop_map[cid] = _GRAIN_NORM.get(raw.strip().lower(), raw.strip().title())

    rows = []
    for instr in instruments_data.get("instruments", []):
        if instr.get("deleted"):
            continue
        basis_bid = instr.get("basis_bid")
        if basis_bid is None:
            continue

        instr_id       = instr.get("id")
        delivery_month = instr.get("display_name", "")
        grain          = crop_map.get(instr.get("commodity_id"), "Unknown")
        futures_sym    = (
            (instr.get("futures") or {}).get("symbols", {}).get("fbn", "") or ""
        )
        how, usd_basis = canadian_basis(instr, usdcad)
        if how == "skip":
            continue                                                  # a C$ price against US futures we cannot convert: not stored as the mixed number
        try:
            basis_cents = int(round(float(usd_basis if how == "converted" else basis_bid) * 100))
        except (TypeError, ValueError):
            continue

        pfx    = _GRAIN_PFX.get(grain, grain[:2].upper() if grain else "XX")
        row_id = f"{pfx}_{instr_id}" if instr_id else f"{pfx}_{delivery_month}"

        rows.append({
            "id":            row_id,
            "grain":         grain,
            "deliveryMonth": delivery_month,
            "futuresSymbol": futures_sym,
            "basisCents":    basis_cents,
            "isSpot":        False,
        })

    if not rows:
        return None

    return NewSnapshotRequest(
        timestamp=timestamp,
        provider="ADM",
        location=display_name,
        source="web",
        rows=rows,
    )


def parse_adm_email(
    email_id: str, subject: str, received: str,
    pdf_bytes: Optional[bytes], html_body: Optional[str],
) -> ParsedSnapshot:
    from bs4 import BeautifulSoup
    if pdf_bytes:
        return _parse_pdf(pdf_bytes, received, email_id, subject)
    if html_body:
        text  = BeautifulSoup(html_body, "html.parser").get_text(separator="\n")
        lines = [l for l in text.splitlines() if l.strip()]
        clean = "\n".join(lines)
        return _parse_body_text(clean, received, email_id, subject)
    return ParsedSnapshot(
        emailId=email_id, emailSubject=subject, emailDate=received,
        provider="ADM", snapshots=[], needsReview=True,
        parseError="No email content found.")
