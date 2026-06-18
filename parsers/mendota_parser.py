"""
ADM Grain Northern Illinois - Closing Bids PDF parser.

PDF structure (3 sections):
  1. ADM Corn Markets
     Cols: NC Rail-Mendota | Morris | Ott North | Sp.Valley-Hennepin |
           Ott South-Lacon | Creve Coeur | Havana | Burlington/Gulfport | Clinton Processing
  2. ADM Bean Markets
     Cols: Morris | Ottawa North | Hennepin | Ott South-Lacon |
           Sp. Valley | Creve Coeur | Havana | Burlington/Gulfport
  3. ADM Wheat Markets
     Cols: Mendota Mill | Mendota In Town | Ott North-Creve Coeur | Morris

Each location column group = 3 sub-cols: Basis | FuturesCode | Cash
Basis is in USD/bu decimal (e.g. -0.20 = -20 cents)
FuturesCode is single letter: N=Jul, U=Sep, Z=Dec, H=Mar, F=Jan, K=May, Q=Aug, X=Nov
"""
import re
import io
from typing import Optional
from datetime import datetime, timedelta

from models import ParsedSnapshot, NewSnapshotRequest

# ── Futures helpers ───────────────────────────────────────────────────────────
MONTH_NUM  = {'F':1,'G':2,'H':3,'J':4,'K':5,'M':6,'N':7,'Q':8,'U':9,'V':10,'X':11,'Z':12}
GRAIN_CODE = {'Corn':'ZC','Soybeans':'ZS','Wheat':'ZW'}
GRAIN_PFX  = {'Corn':'CN','Soybeans':'SB','Wheat':'WH'}

# ── Which column group (0-based) maps to which location ──────────────────────
CORN_LOC  = {2:'Ottawa', 6:'Havana', 7:'Burlington', 8:'Clinton Proc'}  # col 0 (BN Rail) dropped
BEAN_LOC  = {1:'Ottawa', 6:'Havana', 7:'Burlington'}
WHEAT_LOC = {0:'Mendota Mill'}

# ── Delivery month normalisation ──────────────────────────────────────────────
MONTH_NORM = {
    'june':'JUN','july':'JUL','august':'AUG','september':'SEP',
    'october':'OCT','november':'NOV','december':'DEC',
    'january':'JAN','february':'FEB','march':'MAR','april':'APR','may':'MAY',
    'jan':'JAN','feb':'FEB','mar':'MAR','apr':'APR',
    'jun':'JUN','jul':'JUL','aug':'AUG','sep':'SEP',
    'oct':'OCT','nov':'NOV','dec':'DEC',
    'oct/nov':'OCT/NOV','oct/nov 2026':'OCT/NOV',
    'fh june':'FH JUN','lh june':'LH JUN',
    'fh july':'FH JUL','lh july':'LH JUL',
    'fh jun':'FH JUN','lh jun':'LH JUN',
    'fh jul':'FH JUL','lh jul':'LH JUL',
}

def _norm_month(raw):
    s = re.sub(r'\s*20\d\d$','',str(raw).strip()).strip().lower()
    return MONTH_NORM.get(s)

def _is_basis(v):
    return bool(re.match(r'^[+-]?\d+\.\d+$', str(v).strip()))

def _basis_cents(v):
    return int(round(float(str(v).strip()) * 100))

def _is_fut_code(v):
    return str(v).strip().upper() in MONTH_NUM

def _build_sym(letter, grain, yr, mo):
    code = GRAIN_CODE.get(grain,'ZC')
    l    = str(letter).strip().upper()
    fmo  = MONTH_NUM.get(l, 0)
    year = yr if fmo > mo else yr + 1
    return f"{code}{l}{str(year)[2:]}"

def _parse_section(rows, loc_map, grain, yr, mo):
    results  = {loc:[] for loc in loc_map.values()}
    dm_counts = {}
    for row in rows:
        if not row or not row[0]:
            continue
        dm = _norm_month(row[0])
        if not dm:
            continue
        cells = [str(c).strip() if c else '' for c in row[1:]]
        for grp_idx, loc in loc_map.items():
            off = grp_idx * 3
            if off + 1 >= len(cells):
                continue
            basis_raw = cells[off]
            code_raw  = cells[off+1]
            if not _is_basis(basis_raw) or not _is_fut_code(code_raw):
                continue
            key = f"{loc}_{dm}"
            dm_counts[key] = dm_counts.get(key,0) + 1
            cnt = dm_counts[key]
            dm_label = dm if cnt==1 else (f"{dm} FH" if cnt==2 else f"{dm} LH")
            pfx = GRAIN_PFX[grain]
            results[loc].append({
                'id':            f"{pfx}_{dm_label.replace(' ','_').replace('/','_')}",
                'grain':         grain,
                'deliveryMonth': dm_label,
                'futuresSymbol': _build_sym(code_raw, grain, yr, mo),
                'basisCents':    _basis_cents(basis_raw),
                'isSpot':        False,
            })
    return results

def _split_sections(all_rows):
    HDRS = {'adm corn markets':'corn','adm bean markets':'beans','adm wheat markets':'wheat'}
    sections, cur = {}, None
    for row in all_rows:
        if not row:
            continue
        first = str(row[0] or '').strip().lower()
        hit   = next((v for k,v in HDRS.items() if k in first), None)
        if hit:
            cur = hit
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(row)
    return sections

def _as_of(text):
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', text)
    if m:
        return int(m.group(3)), int(m.group(1))
    n = datetime.now()
    return n.year, n.month

def _timestamp(text, received):
    m = re.search(r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)',
                  text, re.IGNORECASE)
    if m:
        for fmt in ('%m/%d/%Y %I:%M:%S %p','%m/%d/%Y %I:%M %p'):
            try:
                dt = datetime.strptime(f"{m.group(1)} {m.group(2).strip()}", fmt)
                return (dt + timedelta(hours=5)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            except ValueError:
                continue
    m2 = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', text)
    if m2:
        try:
            dt = datetime.strptime(m2.group(1),'%m/%d/%Y')
            return dt.strftime('%Y-%m-%dT20:00:00.000Z')
        except ValueError:
            pass
    return received

def parse_mendota_pdf(pdf_bytes, received, email_id, subject):
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
            all_rows  = []
            for page in pdf.pages:
                for tbl in page.extract_tables():
                    all_rows.extend(tbl)
    except Exception as e:
        return ParsedSnapshot(emailId=email_id,emailSubject=subject,emailDate=received,
            provider='Mendota',snapshots=[],needsReview=True,parseError=f'PDF read error: {e}')

    yr, mo    = _as_of(full_text)
    timestamp = _timestamp(full_text, received)
    sections  = _split_sections(all_rows)

    loc_rows = {}
    for section, loc_map, grain in [
        ('corn',  CORN_LOC,  'Corn'),
        ('beans', BEAN_LOC,  'Soybeans'),
        ('wheat', WHEAT_LOC, 'Wheat'),
    ]:
        if section in sections:
            for loc, rows in _parse_section(sections[section], loc_map, grain, yr, mo).items():
                loc_rows.setdefault(loc, []).extend(rows)

    if not loc_rows:
        return ParsedSnapshot(emailId=email_id,emailSubject=subject,emailDate=received,
            provider='Mendota',snapshots=[],needsReview=True,
            parseError='No data extracted. PDF table structure may differ from expected.')

    snaps = []
    for loc, rows in loc_rows.items():
        if not rows:
            continue
        seen = set()
        for r in rows:
            if r['grain'] not in seen:
                r['isSpot'] = True; r['spotGrain'] = r['grain']
                seen.add(r['grain'])
        snaps.append(NewSnapshotRequest(
            timestamp=timestamp, provider='Mendota', location=loc,
            source='email', emailSubject=subject, emailDate=received, rows=rows))

    return ParsedSnapshot(emailId=email_id,emailSubject=subject,emailDate=received,
        provider='Mendota',snapshots=snaps,
        needsReview=True)   # always review first PDF import

def parse_mendota_email(email_id, subject, received, pdf_bytes, html_body):
    if pdf_bytes:
        return parse_mendota_pdf(pdf_bytes, received, email_id, subject)
    return ParsedSnapshot(emailId=email_id,emailSubject=subject,emailDate=received,
        provider='Mendota',snapshots=[],needsReview=True,
        parseError='No PDF attachment found in Mendota email.')
