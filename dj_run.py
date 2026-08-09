"""
dj_run.py — driver: OCR a Dow Jones corn-basis PDF (via dj_ocr ensemble), map
each roster location to its (provider, DB-location), and upsert as source=
'dow_jones' snapshots. Rebuilt 2026-08-06 to restore the prune-wiped archive.

Usage:  python dj_run.py "<pdf>" [--load] [--limit N]
Idempotent (upsert). Refuses to load a page flagged low_confidence.
Hereford/PNW are NOT handled here — they live in rail_fob (unaffected by the prune).
"""
from __future__ import annotations
import os, re, sys, time, collections
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)
assert "pvxtarcaowjxcrzfuolo" in os.environ["DATABASE_URL"], "wrong DB"

import fitz
import database as db
from models import NewSnapshotRequest, SnapshotRow
import dj_ocr

DROP = "DROP"
RAIL = "RAIL"
# roster location -> (provider, exact DB location) | DROP | RAIL   (exact names from survivors)
LOCATION_MAP = {
    "Des Moines, IA": ("Heartland Coop", "Des Moines, IA"),
    "Cedar Rapids, IA": ("ADM", "Cedar Rapids, IA"),
    "Keokuk, IA": DROP,
    "Burlington, IA": ("ADM", "Burlington, IA"),
    "Eddyville, IA": ("Cargill", "Eddyville"),
    "Ft. Dodge, IA": ("Cargill", "Fort Dodge"),
    "Chicago, IL": ("Ingredion", "Chicago, IL"),
    "Central Ill.": DROP,
    "Peoria, IL": ("BioUrja", "Peoria, IL"),
    "Decatur, IL": ("ADM", "Decatur, IL (Corn Processing)"),
    "Champaign, IL": ("TGM", "Champaign, IL"),
    "Decatur, IN": DROP,
    "Evansville, IN": ("ADM", "Evansville, IN (Ohio St.)"),
    "Lafayette, IN": ("Cargill", "Lafayette"),
    "Council Bluffs, IA": ("Bunge", "Council Bluffs, IA"),
    "Lincoln, NE": ("ADM", "Lincoln, NE (Elevator)"),
    "Blair, NE": ("Cargill", "Blair"),
    "Hastings, NE": ("Cooperative Producers", "Hastings, NE"),
    "Minneapolis, MN": DROP,
    "Marshall, MN": ("ADM", "Marshall, MN (Corn Processing)"),
    "Toledo, OH": ("Andersons", "Toledo - Kuhlman Dr"),
    "Cincinnati, OH": DROP,
    "St. Louis, MO": ("Cargill", "East St. Louis"),
    "Kansas City, MO": ("Bartlett", "KC - KCT Terminal"),
    "Atchison, KS": ("Bartlett", "Atchison"),
    "Garden City, KS": ("Garden City Coop", "Garden City, KS"),
    "Dalhart, TX": ("Welch Grain", "Dalhart, TX"),
    "Hereford, TX": RAIL,
    "Milwaukee, WI": DROP,
    "Blissfield, MI": ("MAC", "Blissfield, MI"),
    "Mitchell, SD": ("CHS", "Mitchell"),
    "Finley, ND": ("Finley Farmers Elevator", "Finley, ND"),
    "Denver, CO": DROP,
    "Portland, OR / PNW Rail": RAIL,
    "Memphis, TN": ("Cargill", "West Memphis"),
    "Louisville, KY": ("CGB", "CGB LOUISVILLE"),
    "Petersburg, VA": ("Smithfield Grain", "Petersburg, VA"),
    "Rose Hill, NC": ("Smithfield Grain", "Rose Hill, NC"),
}
# Bean map — differs from corn (different companies buy beans); memory-verified
# overrides + the rest default to the corn target. Havana/Osceola unknown -> skip.
BEAN_LOCATION_MAP = {
    "Des Moines, IA": ("ADM", "Des Moines, IA"),
    "Cedar Rapids, IA": ("Cargill", "Cedar Rapids East"),
    "Burlington, IA": ("ADM", "Burlington, IA"),
    "Council Bluffs, IA": ("Bunge", "Council Bluffs, IA"),
    "Sioux City, IA": ("Cargill", "Sioux City"),
    "Chicago, IL": DROP, "Central IL": DROP, "Havana, IL": None,
    "Bloomington, IL": ("Cargill", "Bloomington"),
    "Quincy, IL": ("ADM", "Quincy, IL (Soy Processing)"),
    "Champaign, IL": ("TGM", "Champaign, IL"),
    "Evansville, IN": ("ADM", "Evansville, IN (Ohio St.)"),
    "Indianapolis, IN": DROP,
    "Decatur, IN": ("Bunge", "Decatur, IN"),
    "St. Louis, MO": ("Cargill", "East St. Louis"),
    "Kansas City, MO": ("Cargill", "Kansas City"),
    "St. Joseph, MO": ("AGP", "AGP St. Joseph, MO"),
    "Toledo, OH": ("Andersons", "Toledo - Kuhlman Dr"),
    "Sidney, OH": ("Cargill", "Sidney"),
    "Cincinnati, OH": DROP, "Minneapolis, MN": DROP,
    "Mankato, MN": ("ADM", "Mankato, MN (Soy Processing)"),
    "Brewster, MN": ("MNSP", "Brewster"),
    "Lincoln, NE": ("ADM", "Lincoln, NE (Soy Processing)"),
    "Grand Island, NE": ("Cooperative Producers", "Grand Island, NE"),
    "Hastings, NE": ("AGP", "AGP Hastings, NE - Soy Plant"),
    "Emporia, KS": ("Bunge", "Emporia, KS"),
    "Atchison, KS": ("Bartlett", "Atchison"),
    "Hutchinson, KS": DROP,
    "Volga, SD": ("SDSP", "Volga"),
    "Mitchell, SD": DROP,
    "Finley, ND": ("Finley Farmers Elevator", "Finley, ND"),
    "Portland-PNW rail": RAIL, "Milwaukee, WI": DROP,
    "Blissfield, MI": ("MAC", "Blissfield, MI"),
    "Little Rock, AR": DROP, "Osceola, AR": None,
    "Memphis, TN": ("Cargill", "West Memphis"),
    "Norfolk, VA": ("Perdue", "Norfolk, VA"),
    "Louisville, KY": ("CGB", "CGB LOUISVILLE"),
    "Raleigh, NC": DROP,
}
_MON_NUM = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def page_to_reqs(parsed, loc_map=LOCATION_MAP, root="ZC", prefix="C", grain="Corn"):
    """parsed = dj_ocr.parse_page_ensemble(page). Returns (date_iso, [NewSnapshotRequest])."""
    yr, mo, da, cm = parsed["year"], parsed["month"], parsed["day"], parsed["col_months"]
    if not (yr and mo and da and cm):
        return None, []
    date_iso = f"{yr:04d}-{_MON_NUM[mo]:02d}-{da:02d}"
    ts = date_iso + "T00:00:00Z"
    yy = f"{yr % 100:02d}"
    # per-column mode futures letter (fills cells where OCR missed the letter)
    letters = [collections.Counter() for _ in range(3)]
    for vals in parsed["rows"].values():
        for ci, v in enumerate(vals):
            if v and v[1]:
                letters[ci][v[1]] += 1
    col_letter = [(lc.most_common(1)[0][0] if lc else None) for lc in letters]

    reqs = []
    for roster_loc, vals in parsed["rows"].items():
        tgt = loc_map.get(roster_loc)
        if tgt is None or tgt is DROP or tgt is RAIL:
            continue
        provider, location = tgt
        rows = []
        for ci, v in enumerate(vals):
            if v is None:
                continue
            basis, letter = v
            letter = letter or col_letter[ci]
            if not letter:
                continue
            mon = cm[ci]
            rows.append(SnapshotRow(
                id=f"{prefix}_{mon.upper()}{yy}", grain=grain, deliveryMonth=f"{mon} {yr}",
                futuresSymbol=f"{root}{letter}{yy}", basisCents=basis, isSpot=False))
        if rows:
            reqs.append(NewSnapshotRequest(timestamp=ts, provider=provider,
                                           location=location, source="dow_jones", rows=rows))
    return date_iso, reqs


def _upsert_retry(reqs, tries=6):
    """Resilient upsert — a transient DNS/network blip shouldn't lose a page."""
    for k in range(tries):
        try:
            db.upsert_snapshots([r.model_dump() for r in reqs])
            return True
        except Exception as exc:
            if k == tries - 1:
                print(f"    upsert failed after {tries}: {str(exc)[:80]}", flush=True)
                return False
            time.sleep(5 * (k + 1))


def main():
    pdf = sys.argv[1]
    load = "--load" in sys.argv
    beans = "--beans" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    ym = re.search(r"\b(20\d\d)\b", os.path.basename(pdf))   # year from filename (reliable)
    year = int(ym.group(1)) if ym else None
    roster = dj_ocr.BEAN_ROSTER if beans else dj_ocr.CORN_ROSTER
    cfg = dict(loc_map=BEAN_LOCATION_MAP, root="ZS", prefix="S", grain="Soybeans") if beans else {}
    doc = fitz.open(pdf)
    n_pages = doc.page_count if limit is None else min(limit, doc.page_count)
    if load:
        for k in range(6):
            try:
                db.init_db(); break
            except Exception:
                if k == 5:
                    raise
                time.sleep(5 * (k + 1))
    total, dates, skipped, lowconf = 0, [], 0, 0
    for i in range(n_pages):
        try:
            parsed = dj_ocr.parse_page_ensemble(doc[i], year=year, roster=roster)
        except Exception as exc:
            print(f"  p{i+1}: ERROR {exc}", flush=True)
            continue
        if parsed["low_confidence"]:
            lowconf += 1
            print(f"  p{i+1}: LOW-CONFIDENCE — skipped", flush=True)
            continue
        date_iso, reqs = page_to_reqs(parsed, **cfg)
        if not reqs:
            skipped += 1
            print(f"  p{i+1}: no date/rows — skipped", flush=True)
            continue
        dates.append(date_iso)
        total += len(reqs)
        if load:                                   # flush per page so progress persists
            _upsert_retry(reqs)
        print(f"  p{i+1}: {date_iso}  {len(reqs)} locations{'  (loaded)' if load else ''}", flush=True)
    print(f"\nDONE: {total} snapshots across {len(set(dates))} dates "
          f"({min(dates) if dates else '-'}..{max(dates) if dates else '-'}) "
          f"| low-conf pages: {lowconf} | skipped: {skipped}", flush=True)
    if not load:
        print("(dry run — pass --load to write)", flush=True)


if __name__ == "__main__":
    main()
