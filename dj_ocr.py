"""
dj_ocr.py — OCR extractor for the scanned Dow Jones "Corn/Bean Basis Bids" PDFs
(rebuilt 2026-08-06 after the prune wiped source='dow_jones'; see the
project_dj_basis_archive memory for the full recipe/rationale).

Per page: render at fitz Matrix(zoom), optional deskew, Tesseract OCR, then parse
each data line into (location_label, [(basis, futures_letter) x3 shipment cols]).
Column month headers + report date are read from the page header/title.

The recent years (2016-2023) are crisp browser-print rasterizations and OCR nearly
perfectly with a single pass; the 2011-2015 scans are degraded and use the
multi-render ensemble + deskew. A cell is only trusted when >=2 renders agree.
"""
from __future__ import annotations
import re
import io
import fitz
import numpy as np
import pytesseract
from PIL import Image

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

MONTHS = {"jan": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr", "may": "May",
          "jun": "Jun", "jul": "Jul", "aug": "Aug", "sep": "Sep", "oct": "Oct",
          "nov": "Nov", "dec": "Dec"}
MON_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
FUT_LETTERS = set("FGHJKMNQUVXZ")
LETTER_FIX = {"O": "Q", "0": "Q"}   # only used inside a letter slot; rarely needed

BASIS_LIMIT = 400   # cents plausibility guard


def render(page, zoom=3.0, sharpen=False, binarize=False):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    img = deskew(img)
    if binarize:
        arr = np.asarray(img)
        thr = arr.mean() - 10
        img = Image.fromarray((arr > thr).astype(np.uint8) * 255)
    return img


def deskew(img, max_deg=2.0):
    """Rotate to maximise horizontal ink-projection sharpness. Angle is estimated on
    a downsampled copy (fast); the chosen angle is applied once to the full image."""
    small = img.resize((img.width // 4, img.height // 4), Image.BILINEAR)
    arr = 255 - np.asarray(small, dtype=np.float32)
    best_a, best_s = 0.0, -1.0
    for a in np.arange(-max_deg, max_deg + 0.5, 0.5):
        rot = np.asarray(Image.fromarray(arr).rotate(a, resample=Image.BILINEAR))
        s = np.var(np.diff(rot.sum(axis=1)))
        if s > best_s:
            best_s, best_a = s, a
    if abs(best_a) < 0.5:
        return img
    return Image.fromarray(255 - np.asarray(
        Image.fromarray(255 - np.asarray(img)).rotate(best_a, resample=Image.BILINEAR, fillcolor=0)))


def ocr_lines(img):
    txt = pytesseract.image_to_string(img, config="--psm 6")
    return [l for l in txt.splitlines() if l.strip()]


def parse_value(tok):
    """Tolerant: '85.0U'/'85.0uU'/'t10-0U'/'75.00.' -> (val, letter). unq/na -> None."""
    t = tok.strip().replace(" ", "")
    if re.search(r"(unq|una|ung|u nq|nq|na|n/a|hol)", t, re.I):
        return None
    m = re.search(r"([-+]?\d{1,3})(?:\.\d)?", t)
    if not m:
        return None
    val = int(m.group(1))
    if abs(val) > BASIS_LIMIT:
        return None
    tail = t[m.end():]
    letter = next((c.upper() for c in tail if c.upper() in FUT_LETTERS), None)
    return (val, letter)


def _words(img):
    d = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
    out = []
    for i, txt in enumerate(d["text"]):
        if txt.strip():
            out.append({"t": txt.strip(), "x": d["left"][i], "cx": d["left"][i] + d["width"][i] / 2,
                        "top": d["top"][i], "h": d["height"][i]})
    return out


def _column_centers(words):
    """Find the value-column x-centers from the 'Location <M> Chg <M> Chg <M> Chg' header."""
    hdr = [w for w in words if w["t"].lower() == "location"]
    if not hdr:
        return None
    hy = hdr[0]["top"]
    row = sorted([w for w in words if abs(w["top"] - hy) < 25], key=lambda w: w["x"])
    months = [w for w in row if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", w["t"], re.I)]
    if len(months) < 3:
        return None
    return [m["cx"] for m in months[:3]], hdr[0]["x"]


def parse_header_months(lines):
    """Find the 'Location <M> Chg <M> Chg <M> Chg' header row -> [m1,m2,m3]."""
    for l in lines:
        if re.match(r"^\s*location\b", l, re.I):
            found = re.findall(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", l, re.I)
            if len(found) >= 3:
                return [MONTHS[f.lower()] for f in found[:3]]
    return None


def parse_title_date(page_text, year_override=None):
    """Return (year, month, day). Year from the filename override (reliable) or a
    top 'M/D/YY' timestamp; month/day from the '- <Mon> <DD>' title (tolerant of a
    garbled 'Corn Basis Bids'), falling back to a top 'M/D'."""
    yr = year_override
    if yr is None:
        m = re.search(r"\b\d{1,2}/\d{1,2}/(\d{2})\b", page_text)
        if m:
            yr = 2000 + int(m.group(1))
    mo = da = None
    t = re.search(r"[-–]\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s*(\d{1,2})",
                  page_text, re.I)
    if t:
        mo, da = MONTHS[t.group(1)[:3].lower()], int(t.group(2))
    else:
        m2 = re.search(r"\b(\d{1,2})/(\d{1,2})\b", page_text)
        if m2 and 1 <= int(m2.group(1)) <= 12:
            mo, da = MON_ORDER[int(m2.group(1)) - 1], int(m2.group(2))
    return yr, mo, da


def parse_page(page, zoom=3.0, sharpen=False, binarize=False, year=None):
    img = render(page, zoom=zoom, sharpen=sharpen, binarize=binarize)
    words = _words(img)
    text = " ".join(w["t"] for w in words)
    yr, mo, da = parse_title_date(text, year_override=year)

    cc = _column_centers(words)
    if not cc:
        return {"year": yr, "month": mo, "day": da, "col_months": None, "rows": []}
    centers, loc_x = cc
    hdr_top = min(w["top"] for w in words if w["t"].lower() == "location")
    col_months = None
    hdr_row = sorted([w for w in words if abs(w["top"] - hdr_top) < 25], key=lambda w: w["x"])
    ms = [MONTHS[w["t"][:3].lower()] for w in hdr_row
          if re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", w["t"], re.I)]
    if len(ms) >= 3:
        col_months = ms[:3]

    HALF = 115  # value-column half-width (px); Chg column sits just to the right, excluded
    loc_cut = centers[0] - 150
    # group words into rows by vertical position (data rows are below the header)
    data = [w for w in words if w["top"] > hdr_top + 15]
    rowmap = {}
    for w in data:
        key = round(w["top"] / 18)
        rowmap.setdefault(key, []).append(w)

    rows = []
    for _, ws in sorted(rowmap.items()):
        loc = " ".join(w["t"] for w in sorted([w for w in ws if w["cx"] < loc_cut], key=lambda w: w["x"]))
        loc = re.sub(r"\s+", " ", loc).strip().rstrip(".,").strip()
        if not loc or re.match(r"^(location|unq|futures|write|end)", loc, re.I):
            continue
        vals = []
        for c in centers:
            cell = " ".join(w["t"] for w in sorted([w for w in ws if abs(w["cx"] - c) <= HALF], key=lambda w: w["x"]))
            vals.append(parse_value(cell))
        if all(v is None for v in vals):
            continue
        rows.append((loc, vals))
    return {"year": yr, "month": mo, "day": da, "col_months": col_months, "rows": rows}


CORN_ROSTER = [
    "Des Moines, IA", "Cedar Rapids, IA", "Keokuk, IA", "Burlington, IA", "Eddyville, IA",
    "Ft. Dodge, IA", "Chicago, IL", "Central Ill.", "Peoria, IL", "Decatur, IL", "Champaign, IL",
    "Decatur, IN", "Evansville, IN", "Lafayette, IN", "Council Bluffs, IA", "Lincoln, NE",
    "Blair, NE", "Hastings, NE", "Minneapolis, MN", "Marshall, MN", "Toledo, OH", "Cincinnati, OH",
    "St. Louis, MO", "Kansas City, MO", "Atchison, KS", "Garden City, KS", "Dalhart, TX",
    "Hereford, TX", "Milwaukee, WI", "Blissfield, MI", "Mitchell, SD", "Finley, ND", "Denver, CO",
    "Portland, OR / PNW Rail", "Memphis, TN", "Louisville, KY", "Petersburg, VA", "Rose Hill, NC",
]
BEAN_ROSTER = [
    "Des Moines, IA", "Cedar Rapids, IA", "Burlington, IA", "Council Bluffs, IA", "Sioux City, IA",
    "Chicago, IL", "Central IL", "Havana, IL", "Bloomington, IL", "Quincy, IL", "Champaign, IL",
    "Evansville, IN", "Indianapolis, IN", "Decatur, IN", "St. Louis, MO", "Kansas City, MO",
    "St. Joseph, MO", "Toledo, OH", "Sidney, OH", "Cincinnati, OH", "Minneapolis, MN", "Mankato, MN",
    "Brewster, MN", "Lincoln, NE", "Grand Island, NE", "Hastings, NE", "Emporia, KS", "Atchison, KS",
    "Hutchinson, KS", "Volga, SD", "Mitchell, SD", "Finley, ND", "Portland-PNW rail", "Milwaukee, WI",
    "Blissfield, MI", "Little Rock, AR", "Osceola, AR", "Memphis, TN", "Norfolk, VA", "Louisville, KY",
    "Raleigh, NC",
]


def _match_roster(label, roster):
    import difflib
    ln = re.sub(r"[^a-z]", "", label.lower())
    if not ln:
        return None
    best, bs = None, 0.0
    for r in roster:
        rn = re.sub(r"[^a-z]", "", r.lower())
        s = difflib.SequenceMatcher(None, ln, rn).ratio()
        if ln[:5] and ln[:5] in rn:
            s += 0.25
        if s > bs:
            best, bs = r, s
    return best if bs >= 0.55 else None


def parse_page_ensemble(page, year=None, roster=None):
    roster = roster or CORN_ROSTER
    """Run 3 renders, align rows to CORN_ROSTER by label, majority-vote each cell.
    A cell is trusted only when >=2 members agree; else it's dropped (never guessed).
    Returns dict with rows keyed by roster location + low_confidence flag."""
    variants = [
        parse_page(page, zoom=3.0, year=year),
        parse_page(page, zoom=4.2, year=year),
        parse_page(page, zoom=4.0, sharpen=True, binarize=True, year=year),
    ]
    meta = next((v for v in variants if v["col_months"]), variants[0])
    recovered = sum(1 for v in variants if v["rows"])
    # per roster loc -> list of value-triples (one per member that saw it)
    votes = {}
    for v in variants:
        for label, vals in v["rows"]:
            rl = _match_roster(label, roster)
            if rl:
                votes.setdefault(rl, []).append(vals)
    out = {}
    for rl, triples in votes.items():
        cell = []
        for ci in range(3):
            tally = {}
            for tr in triples:
                val = tr[ci]
                if val is not None:
                    tally[val] = tally.get(val, 0) + 1
            winner = max(tally.items(), key=lambda kv: kv[1], default=(None, 0))
            cell.append(winner[0] if winner[1] >= 2 else None)
        if any(c is not None for c in cell):
            out[rl] = cell
    return {"year": meta["year"], "month": meta["month"], "day": meta["day"],
            "col_months": meta["col_months"], "rows": out,
            "low_confidence": recovered < 2}


if __name__ == "__main__":
    import sys
    doc = fitz.open(sys.argv[1])
    pg = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    r = parse_page_ensemble(doc[pg])
    print("date:", r["month"], r["day"], r["year"], "| col_months:", r["col_months"],
          "| rows:", len(r["rows"]), "| low_conf:", r["low_confidence"])
    for rl in CORN_ROSTER:
        if rl in r["rows"]:
            print(f"  {rl:26} {r['rows'][rl]}")
