"""
Basis Tracker · JPSI
Streamlit app — run with: streamlit run app.py
"""
import os
from datetime import date, datetime, timezone
from dotenv import load_dotenv
import streamlit as st

from database import (
    init_db, upsert_snapshot, get_snapshots, delete_snapshot,
    list_locations, get_location_meta, get_all_location_meta, get_map_data,
    get_grain_map, get_bids_filter_data, get_snapshots_bulk,
)

load_dotenv()

# On Streamlit Community Cloud, secrets live in st.secrets rather than .env.
# Inject any secrets that weren't already set by load_dotenv() into os.environ
# so that database.py and other modules can read them via os.getenv().
try:
    for _secret_key in ("DATABASE_URL",):
        if _secret_key in st.secrets and not os.environ.get(_secret_key):
            os.environ[_secret_key] = st.secrets[_secret_key]
except Exception:
    pass  # st.secrets not available (no secrets configured) — fine locally

st.set_page_config(
    page_title="Basis Tracker · JPSI",
    page_icon="📊",
    layout="wide",
)

# ── On-startup init (once per Streamlit process, not per rerun) ───────────────
@st.cache_resource
def _init_db_once():
    init_db()

_init_db_once()

# ── Grain normalization helpers ───────────────────────────────────────────────
@st.cache_data(ttl=3600)
def _cached_grain_map() -> dict:
    return get_grain_map()

_GM: dict = _cached_grain_map()

@st.cache_data(ttl=300)
def _cached_get_bids_filter_data() -> list[dict]:
    return get_bids_filter_data()

@st.cache_data(ttl=300)
def _cached_get_snapshots(provider: str, location: str):
    return get_snapshots(provider, location)

@st.cache_data(ttl=300)
def _cached_list_locations() -> list[dict]:
    return list_locations()

@st.cache_data(ttl=300)
def _cached_get_location_meta(provider: str) -> dict:
    return get_location_meta(provider)

@st.cache_data(ttl=600)
def _cached_get_map_data() -> list[dict]:
    return get_map_data()

def _grain_disp(raw: str) -> str | None:
    """Return canonical display name for a raw grain, or None if inactive."""
    entry = _GM.get(raw)
    if entry is None:
        return raw  # unknown: pass through
    if not entry["is_active"]:
        return None
    cls  = entry.get("wheat_class")
    prot = entry.get("protein")
    base = entry["canonical_grain"]
    if cls:
        return f"{base} ({cls} {prot})" if prot else f"{base} ({cls})"
    return base

def _build_grains(rows) -> list[str]:
    """Build a sorted deduplicated list of canonical grain display names from snapshot rows."""
    seen: set[str] = set()
    result: list[str] = []
    for r in rows:
        if r.isSpot:
            continue
        disp = _grain_disp(r.grain)
        if disp and disp not in seen:
            seen.add(disp)
            result.append(disp)
    return sorted(result)

# ── Location config ───────────────────────────────────────────────────────────
LOCATIONS = [
    {"provider": "ADM", "key": "ADM Decatur",     "label": "Decatur",     "grains": ["Corn","Soybeans"],        "color": "#3b82f6"},
    {"provider": "ADM", "key": "ADM Cedar Rapids", "label": "Cedar Rapids", "grains": ["Corn"],                  "color": "#22c55e"},
    {"provider": "ADM", "key": "ADM St. Louis",   "label": "St. Louis",   "grains": ["Corn","Soybeans","Wheat"], "color": "#a78bfa"},
]

ROLL_ADJ = [
    {"from": "ZSK26", "to": "ZSN26", "adj": -16},
    {"from": "ZCK26", "to": "ZCN26", "adj": -10},
]

_PROVIDER_COLOR: dict[str, str] = {
    "ADM":       "#3b82f6",
    "CHS":       "#16a34a",
    "POET":      "#f97316",
    "CGB":       "#8b5cf6",
    "GPRE":      "#16a34a",
    "Cargill":   "#0ea5e9",
    "Andersons": "#f59e0b",
    "Bunge":     "#dc2626",
    "Scoular":   "#f97316",
    "AGP":       "#22c55e",
    "LDC":       "#3b82f6",
    "Tyson":     "#6b7280",
    "GPC":       "#10b981",
}

MONTH_CODES = {"F":"Jan","G":"Feb","H":"Mar","J":"Apr","K":"May","M":"Jun",
               "N":"Jul","Q":"Aug","U":"Sep","V":"Oct","X":"Nov","Z":"Dec"}

# ── Helpers ───────────────────────────────────────────────────────────────────
def short_sym(s):
    if s and len(s) >= 5:
        return f"{MONTH_CODES.get(s[2], s[2])} '{s[3:]}"
    return s or ""

def fmt_basis(c, is_meal=False):
    if c is None: return "—"
    sign = "+" if c >= 0 else "−"
    if is_meal:
        return f"{sign}${abs(c)/100:.2f}/t"
    return f"{sign}{abs(c)}¢"

def get_adj(from_sym, to_sym):
    if not from_sym or not to_sym or from_sym == to_sym:
        return {"adj": 0, "rolled": False}
    if (len(from_sym) >= 3 and len(to_sym) >= 3
            and from_sym[2] == to_sym[2]
            and from_sym[:2] == to_sym[:2]):
        return {"adj": 0, "rolled": False}
    for r in ROLL_ADJ:
        if r["from"] == from_sym and r["to"] == to_sym:
            return {"adj": r["adj"], "rolled": True}
    return {"adj": None, "rolled": True, "unknown": True}

def diff(entry, cur, cur_sym):
    if not entry:
        return {"val": None, "rolled": False, "unknown": False}
    a = get_adj(entry["sym"], cur_sym)
    if a.get("unknown") or a["adj"] is None:
        return {"val": None, "rolled": True, "unknown": True}
    return {"val": cur - (entry["b"] + a["adj"]), "rolled": a["rolled"], "unknown": False}

def closest(series, target_ms, tol_ms):
    best = None
    for s in series:
        d  = abs(s["ts_ms"] - target_ms)
        bd = abs(best["ts_ms"] - target_ms) if best else float("inf")
        if d < bd and d <= tol_ms:
            best = s
    return best

# Maps CME month letter codes to calendar month numbers
_CME_MONTH_TO_INT = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

def _front_month_row(rows, grain):
    """
    Return the row with the nearest (smallest expiration) futures symbol for `grain`.
    This is the "spot" bid — the front-month contract currently being traded.
    Skips explicit isSpot rows and any rows with unparseable symbols.
    """
    candidates = []
    for r in rows:
        if r.isSpot or _grain_disp(r.grain) != grain:
            continue
        sym = r.futuresSymbol or ""
        if len(sym) < 5:
            continue
        month_code = sym[-3]
        yr2 = sym[-2:]
        if not yr2.isdigit():
            continue
        mon = _CME_MONTH_TO_INT.get(month_code)
        if not mon:
            continue
        year = 2000 + int(yr2)
        candidates.append(((year, mon), r))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def compute_changes(snapshots):
    if not snapshots:
        return {"rows": {}, "spots": {}, "derived_spots": {}}

    latest  = snapshots[-1]
    now_ms  = datetime.fromisoformat(
        latest.timestamp.replace("Z", "+00:00")).timestamp() * 1000
    WEEK    = 7  * 864e5
    MONTH   = 30 * 864e5
    YEAR    = 365 * 864e5

    row_lookup:          dict = {}
    spot_lookup:         dict = {}  # canonical_grain -> list of entries
    derived_spot_lookup: dict = {}  # canonical_grain -> list of entries (front-month per snap)

    for snap in snapshots:
        ts_ms = datetime.fromisoformat(
            snap.timestamp.replace("Z", "+00:00")).timestamp() * 1000
        for r in snap.rows:
            entry = {"ts_ms": ts_ms, "b": r.basisCents, "sym": r.futuresSymbol}
            if r.isSpot:
                g = _grain_disp(r.spotGrain or r.grain)
                if g:
                    spot_lookup.setdefault(g, []).append(entry)
            else:
                if r.id not in row_lookup:
                    row_lookup[r.id] = []
                row_lookup[r.id].append(entry)
        # Build derived spot history: front-month row for each grain in this snapshot
        snap_grains = {_grain_disp(r.grain) for r in snap.rows if not r.isSpot}
        for g in snap_grains:
            if not g:
                continue
            fr = _front_month_row(snap.rows, g)
            if fr and fr.basisCents is not None:
                derived_spot_lookup.setdefault(g, []).append(
                    {"ts_ms": ts_ms, "b": fr.basisCents, "sym": fr.futuresSymbol}
                )

    def calc(series, cur, cur_sym):
        prev = series[-2] if len(series) >= 2 else None
        return {
            "fromPrev":  diff(prev,                                    cur, cur_sym),
            "fromWeek":  diff(closest(series, now_ms - WEEK,  2*864e5), cur, cur_sym),
            "fromMonth": diff(closest(series, now_ms - MONTH, 3*864e5), cur, cur_sym),
            "fromYear":  diff(closest(series, now_ms - YEAR,  5*864e5), cur, cur_sym),
        }

    row_changes = {}
    for r in latest.rows:
        if not r.isSpot:
            row_changes[r.id] = calc(
                row_lookup.get(r.id, []), r.basisCents, r.futuresSymbol)

    spot_changes = {}
    for r in latest.rows:
        if r.isSpot:
            g = _grain_disp(r.spotGrain or r.grain)
            if g and spot_lookup.get(g):
                spot_changes[g] = calc(spot_lookup[g], r.basisCents, r.futuresSymbol)

    derived_spot_changes = {}
    latest_grains = {_grain_disp(r.grain) for r in latest.rows if not r.isSpot}
    for g in latest_grains:
        if not g or g in spot_changes:
            continue  # already have explicit spot change for this grain
        fr = _front_month_row(latest.rows, g)
        if fr and fr.basisCents is not None and derived_spot_lookup.get(g):
            derived_spot_changes[g] = calc(
                derived_spot_lookup[g], fr.basisCents, fr.futuresSymbol
            )

    return {"rows": row_changes, "spots": spot_changes, "derived_spots": derived_spot_changes}

def delta_html(d, is_meal=False):
    if not d:
        return '<span style="color:#94a3b8">—</span>'
    if d.get("unknown"):
        return '<span style="color:#d97706;font-weight:700">⚠ roll</span>'
    val = d.get("val")
    if val is None:
        return '<span style="color:#94a3b8">—</span>'
    if val == 0:
        zero_str = "±$0.00/t" if is_meal else "±0¢"
        return f'<span style="color:#94a3b8;font-weight:600">{zero_str}</span>'
    color  = "#16a34a" if val > 0 else "#dc2626"
    arrow  = "▲" if val > 0 else "▼"
    sign   = "+" if val > 0 else "−"
    adj    = ' <span style="font-size:9px;color:#94a3b8">adj</span>' if d.get("rolled") else ""
    amount = f"${abs(val)/100:.2f}/t" if is_meal else f"{abs(val)}¢"
    return (f'<span style="color:{color};font-weight:700">'
            f'<span style="font-size:9px">{arrow}</span>'
            f'{sign}{amount}{adj}</span>')

def render_table(body_rows, spot_row, changes, spot_chg, loc_color, year_ago_label, is_meal=False):
    th = ("background:#f1f5f9;color:#64748b;font-size:9px;text-transform:uppercase;"
          "letter-spacing:.12em;padding:5px 12px;text-align:left;border-bottom:1px solid #e2e8f0;"
          "font-weight:700;white-space:pre;line-height:1.3;font-family:inherit")
    td_base = "padding:9px 12px;font-family:'IBM Plex Mono',monospace"

    headers = ["Delivery","Futures","Contract","Basis",
               "vs Prev","vs ~1 Wk","vs ~1 Mo",f"vs ~1 Yr\n{year_ago_label}"]

    html = (
        '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono'
        ':wght@400;600;700;800&display=swap" rel="stylesheet">'
        '<table style="width:100%;border-collapse:collapse;font-size:12px;'
        'font-family:\'IBM Plex Mono\',monospace;border:1px solid #e2e8f0;border-radius:6px">'
        "<thead><tr>" +
        "".join(f'<th style="{th}">{h}</th>' for h in headers) +
        "</tr></thead><tbody>"
    )

    # Spot row
    if spot_row and spot_row.basisCents is not None:
        bc    = spot_row.basisCents
        color = "#16a34a" if bc >= 0 else "#dc2626"
        chgs  = spot_chg or {}
        html += (
            f'<tr style="background:#eff6ff">'
            f'<td style="{td_base};border-left:3px solid {loc_color}">'
            f'<div style="font-size:9px;color:{loc_color};text-transform:uppercase;'
            f'letter-spacing:.15em;font-weight:700;margin-bottom:2px">SPOT</div>'
            f'<div style="color:#0f172a;font-weight:800">{spot_row.deliveryMonth}</div></td>'
            f'<td style="{td_base}"><span style="background:#dbeafe;border:1px solid {loc_color};'
            f'color:#1d4ed8;padding:2px 7px;border-radius:3px;font-size:11px;font-weight:800">'
            f'{spot_row.futuresSymbol}</span></td>'
            f'<td style="{td_base};color:#2563eb;font-size:11px">{short_sym(spot_row.futuresSymbol)}</td>'
            f'<td style="{td_base}"><span style="color:{color};font-weight:800;font-size:16px;'
            f'font-variant-numeric:tabular-nums">{fmt_basis(bc, is_meal)}</span></td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromPrev"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromWeek"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromMonth"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromYear"), is_meal)}</td>'
            f'</tr>'
        )
        html += (f'<tr><td colspan="8" style="padding:2px 0">'
                 f'<div style="height:1px;background:#e2e8f0;margin:0 12px"></div></td></tr>')

    # Body rows
    for i, row in enumerate(body_rows):
        bc  = row.basisCents
        chg = changes["rows"].get(row.id, {})
        changed = chg.get("fromPrev", {}).get("val") not in (None, 0)
        dot = (' <span style="display:inline-block;width:5px;height:5px;border-radius:50%;'
               'background:#f59e0b;vertical-align:middle"></span>') if changed else ""
        bg  = "#fefce8" if changed else ("#f8fafc" if i % 2 == 1 else "transparent")
        bc_color = "#16a34a" if (bc or 0) >= 0 else "#dc2626"
        html += (
            f'<tr style="background:{bg}">'
            f'<td style="{td_base};color:#1e293b;font-weight:700">{row.deliveryMonth}{dot}</td>'
            f'<td style="{td_base}"><span style="background:#eff6ff;border:1px solid #bfdbfe;'
            f'color:#1d4ed8;padding:2px 7px;border-radius:3px;font-size:11px;font-weight:700">'
            f'{row.futuresSymbol}</span></td>'
            f'<td style="{td_base};color:#64748b;font-size:11px">{short_sym(row.futuresSymbol)}</td>'
            f'<td style="{td_base}"><span style="color:{bc_color};font-weight:800;font-size:15px;'
            f'font-variant-numeric:tabular-nums">{fmt_basis(bc, is_meal)}</span></td>'
            f'<td style="{td_base}">{delta_html(chg.get("fromPrev"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chg.get("fromWeek"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chg.get("fromMonth"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chg.get("fromYear"), is_meal)}</td>'
            f'</tr>'
        )

    html += "</tbody></table>"
    return html

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700;800&display=swap');
  html, body, [class*="css"] { font-family: 'IBM Plex Mono', monospace !important; }
  /* Hide Streamlit's fixed header so it doesn't overlap content */
  header[data-testid="stHeader"] { display: none !important; }
  #MainMenu { visibility: hidden !important; }
  footer { visibility: hidden !important; }
  .block-container { padding-top: 0.75rem !important; padding-bottom: 1rem !important; }
  div[data-testid="stHorizontalBlock"] { gap: 0 !important; }
  button[kind="secondary"] { font-family: 'IBM Plex Mono', monospace !important; }
  .stTabs [data-baseweb="tab-list"] { gap: 0; background: #ffffff; border-bottom: 1px solid #e2e8f0; }
  .stTabs [data-baseweb="tab"] { color: #64748b; font-size: 12px; padding: 8px 18px;
    font-family: 'IBM Plex Mono', monospace; border-radius: 0; }
  .stTabs [aria-selected="true"] { color: #2563eb !important; font-weight: 700 !important;
    border-bottom: 2px solid #2563eb !important; }
  .stTabs [data-baseweb="tab-panel"] { padding-top: 8px !important; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🌽 ADM / POET / CHS")
    if st.button("Scrape ADM now", key="adm_scrape_btn"):
        from adm_scraper import fetch_adm_bids
        from parsers.adm_parser import parse_instruments as _parse_adm
        with st.spinner("Fetching ADM Gradable (all 151 locations)…"):
            try:
                raw = fetch_adm_bids()
                adm_rows = 0
                adm_locs = 0
                for item in raw:
                    snap = _parse_adm(
                        item["market_id"], item["display_name"],
                        item["instruments_data"], item["timestamp"],
                    )
                    if snap:
                        upsert_snapshot(snap.model_dump())
                        adm_rows += len(snap.rows)
                        adm_locs += 1
                st.success(f"✓ {adm_locs} location(s) — {adm_rows} bid row(s) upserted.")
                st.rerun()
            except Exception as _exc:
                st.error(f"ADM scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --adm-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )
    if st.button("Scrape POET now", key="poet_scrape_btn"):
        from poet_scraper import fetch_poet_bids
        from parsers.poet_parser import parse_instruments as _parse_poet
        with st.spinner("Scraping POET Gradable (all 36 locations)…"):
            try:
                raw = fetch_poet_bids(headless=True)
                poet_imported = 0
                for item in raw:
                    snap = _parse_poet(
                        item["market_id"],
                        item["display_name"],
                        item["instruments_data"],
                        item["timestamp"],
                    )
                    if snap:
                        upsert_snapshot(snap.dict())
                        poet_imported += len(snap.rows)
                st.success(
                    f"✓ {len(raw)} location(s) scraped — "
                    f"{poet_imported} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"POET scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --poet-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape CHS now", key="chs_scrape_btn"):
        from chs_scraper import fetch_chs_bids, CHS_ILLINOIS_IDS
        from parsers.chs_parser import parse_bids_response as _parse_chs
        from datetime import datetime, timezone as _tz
        _ts = datetime.now(_tz.utc).strftime("%Y-%m-%dT00:00:00Z")
        with st.spinner("Fetching CHS Illinois bids…"):
            try:
                raw = fetch_chs_bids()
                snaps = _parse_chs(raw, set(), _ts)  # empty = all locations
                chs_rows = 0
                for s in snaps:
                    upsert_snapshot(s.model_dump())
                    chs_rows += len(s.rows)
                st.success(
                    f"✓ {len(snaps)} snapshot(s) — {chs_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"CHS scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'Both run automatically at 3:45 PM daily.<br>'
        'CLI: <code style="color:#2563eb">python auto_import.py --chs-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape CGB now", key="cgb_scrape_btn"):
        from cgb_scraper import fetch_cgb_bids as _fetch_cgb
        from parsers.cgb_parser import parse_cgb_location as _parse_cgb
        from database import upsert_location_meta as _ulm
        with st.spinner("Fetching CGB Grain bids (86 locations)…"):
            try:
                _locs = _fetch_cgb()
                cgb_rows = 0
                cgb_locs = 0
                for _loc in _locs:
                    _snap = _parse_cgb(_loc)
                    if _snap:
                        upsert_snapshot(_snap.model_dump())
                        _ulm(
                            "CGB", _snap.location,
                            state         = _loc.get("state") or None,
                            facility_type = _loc.get("facility_type") or None,
                        )
                        cgb_rows += len(_snap.rows)
                        cgb_locs += 1
                st.success(
                    f"✓ {cgb_locs} location(s) — {cgb_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"CGB scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --cgb-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 🌾 Cargill")
    if st.button("Scrape Cargill now", key="cargill_scrape_btn"):
        from cargill_scraper import fetch_cargill_bids as _fetch_cargill
        from parsers.cargill_parser import parse_cargill_location as _parse_cargill
        from database import upsert_location_meta as _ulm2
        with st.spinner("Fetching Cargill bids (~81 locations)…"):
            try:
                _clocs = _fetch_cargill()
                cargill_rows = 0
                cargill_locs = 0
                for _cloc in _clocs:
                    _csnap = _parse_cargill(_cloc)
                    if _csnap:
                        upsert_snapshot(_csnap.model_dump())
                        _ulm2(
                            "Cargill", _csnap.location,
                            state         = _cloc.get("state") or None,
                            facility_type = None,
                        )
                        cargill_rows += len(_csnap.rows)
                        cargill_locs += 1
                st.success(
                    f"✓ {cargill_locs} location(s) — {cargill_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Cargill scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --cargill-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 🔴 Bunge")
    if st.button("Scrape Bunge now", key="bunge_scrape_btn"):
        from bunge_scraper import fetch_bunge_bids as _fetch_bunge
        from parsers.bunge_parser import parse_bunge_location as _parse_bunge
        from database import upsert_location_meta as _ulm4
        with st.spinner("Fetching Bunge bids (~20 locations)…"):
            try:
                _blocs = _fetch_bunge()
                bunge_rows = 0
                bunge_locs = 0
                for _bloc in _blocs:
                    _bsnap = _parse_bunge(_bloc)
                    if _bsnap:
                        upsert_snapshot(_bsnap.model_dump())
                        _ulm4(
                            "Bunge", _bsnap.location,
                            state         = _bloc.get("state") or None,
                            facility_type = None,
                        )
                        bunge_rows += len(_bsnap.rows)
                        bunge_locs += 1
                st.success(
                    f"✓ {bunge_locs} location(s) — {bunge_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Bunge scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --bunge-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 🌾 Andersons")
    if st.button("Scrape Andersons now", key="andersons_scrape_btn"):
        from andersons_scraper import fetch_andersons_bids as _fetch_andersons
        from parsers.andersons_parser import parse_andersons_location as _parse_andersons
        from database import upsert_location_meta as _ulm3
        with st.spinner("Fetching The Andersons bids (18 locations)…"):
            try:
                _alocs = _fetch_andersons()
                andersons_rows = 0
                andersons_locs = 0
                for _aloc in _alocs:
                    _asnap = _parse_andersons(_aloc)
                    if _asnap:
                        upsert_snapshot(_asnap.model_dump())
                        _ulm3(
                            "Andersons", _asnap.location,
                            state         = _aloc.get("state") or None,
                            facility_type = None,
                        )
                        andersons_rows += len(_asnap.rows)
                        andersons_locs += 1
                st.success(
                    f"✓ {andersons_locs} location(s) — {andersons_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Andersons scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --andersons-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 🟠 Scoular")
    if st.button("Scrape Scoular now", key="scoular_scrape_btn"):
        from scoular_scraper import fetch_scoular_bids as _fetch_scoular
        from parsers.scoular_parser import parse_scoular_location as _parse_scoular
        from database import upsert_location_meta as _ulm5
        with st.spinner("Fetching Scoular bids (~66 US locations)…"):
            try:
                _slocs = _fetch_scoular()
                scoular_rows = 0
                scoular_locs = 0
                for _sloc in _slocs:
                    _ssnap = _parse_scoular(_sloc)
                    if _ssnap:
                        upsert_snapshot(_ssnap.model_dump())
                        _ulm5(
                            "Scoular", _ssnap.location,
                            state         = _sloc.get("state") or None,
                            facility_type = None,
                        )
                        scoular_rows += len(_ssnap.rows)
                        scoular_locs += 1
                st.success(
                    f"✓ {scoular_locs} location(s) — {scoular_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Scoular scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --scoular-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 🔵 LDC")
    if st.button("Scrape LDC now", key="ldc_scrape_btn"):
        from ldc_scraper import fetch_ldc_bids as _fetch_ldc
        from parsers.ldc_parser import parse_ldc_location as _parse_ldc
        from database import upsert_location_meta as _ulm7
        with st.spinner("Fetching LDC bids (8 US facilities)…"):
            try:
                _ldclocs = _fetch_ldc()
                ldc_rows = 0
                ldc_locs = 0
                for _ldcloc in _ldclocs:
                    _ldcsnap = _parse_ldc(_ldcloc)
                    if _ldcsnap:
                        upsert_snapshot(_ldcsnap.model_dump())
                        _ulm7(
                            "LDC", _ldcsnap.location,
                            state         = _ldcloc.get("state") or None,
                            facility_type = None,
                        )
                        ldc_rows += len(_ldcsnap.rows)
                        ldc_locs += 1
                st.success(
                    f"✓ {ldc_locs} location(s) — {ldc_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"LDC scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --ldc-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 🟢 AGP")
    if st.button("Scrape AGP now", key="agp_scrape_btn"):
        from agp_scraper import fetch_agp_bids as _fetch_agp
        from parsers.agp_parser import parse_agp_location as _parse_agp
        from database import upsert_location_meta as _ulm6
        with st.spinner("Fetching AGP bids (16 locations — Soybeans, Meal, Corn)…"):
            try:
                _agplocs = _fetch_agp()
                agp_rows = 0
                agp_locs = 0
                for _agploc in _agplocs:
                    _agpsnap = _parse_agp(_agploc)
                    if _agpsnap:
                        upsert_snapshot(_agpsnap.model_dump())
                        _ulm6(
                            "AGP", _agpsnap.location,
                            state         = _agploc.get("state") or None,
                            facility_type = None,
                        )
                        agp_rows += len(_agpsnap.rows)
                        agp_locs += 1
                st.success(
                    f"✓ {agp_locs} location(s) — {agp_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"AGP scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --agp-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("### 🌽 GPRE")
    if st.button("Scrape GPRE now", key="gpre_scrape_btn"):
        from gpre_scraper import fetch_gpre_bids as _fetch_gpre
        from parsers.gpre_parser import parse_gpre_location as _parse_gpre
        with st.spinner("Fetching GPRE corn bids (8 locations)…"):
            try:
                _glocs = _fetch_gpre()
                gpre_rows = 0
                gpre_locs = 0
                for _gloc in _glocs:
                    _gsnap = _parse_gpre(_gloc)
                    if _gsnap:
                        upsert_snapshot(_gsnap.model_dump())
                        gpre_rows += len(_gsnap.rows)
                        gpre_locs += 1
                st.success(
                    f"✓ {gpre_locs} location(s) — {gpre_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"GPRE scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python auto_import.py --gpre-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape ZFS now", key="zfs_scrape_btn"):
        from zfs_scraper import fetch_zfs_bids as _fetch_zfs
        from parsers.zfs_parser import parse_zfs_location as _parse_zfs
        with st.spinner("Fetching ZFS soybean bids (Zeeland + Ithaca)…"):
            try:
                _zlocs = _fetch_zfs()
                zfs_rows = 0
                zfs_locs = 0
                for _zloc in _zlocs:
                    _zsnap = _parse_zfs(_zloc)
                    if _zsnap:
                        upsert_snapshot(_zsnap.model_dump())
                        zfs_rows += len(_zsnap.rows)
                        zfs_locs += 1
                st.success(
                    f"✓ {zfs_locs} location(s) — {zfs_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"ZFS scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python zfs_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape MNSP now", key="mnsp_scrape_btn"):
        from mnsoy_scraper import fetch_mnsoy_bids as _fetch_mnsp
        from parsers.mnsoy_parser import parse_mnsoy_location as _parse_mnsp
        with st.spinner("Fetching MNSP soybean bids (Brewster)…"):
            try:
                _mlocs = _fetch_mnsp()
                mnsp_rows = 0
                mnsp_locs = 0
                for _mloc in _mlocs:
                    _msnap = _parse_mnsp(_mloc)
                    if _msnap:
                        upsert_snapshot(_msnap.model_dump())
                        mnsp_rows += len(_msnap.rows)
                        mnsp_locs += 1
                st.success(
                    f"✓ {mnsp_locs} location(s) — {mnsp_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"MNSP scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python mnsoy_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape Primient now", key="primient_scrape_btn"):
        from primient_scraper import fetch_primient_bids as _fetch_pri
        from parsers.primient_parser import parse_primient_location as _parse_pri
        with st.spinner("Fetching Primient bids (17 locations)…"):
            try:
                _plocs = _fetch_pri()
                pri_rows = 0
                pri_locs = 0
                for _ploc in _plocs:
                    _psnap = _parse_pri(_ploc)
                    if _psnap:
                        upsert_snapshot(_psnap.model_dump())
                        pri_rows += len(_psnap.rows)
                        pri_locs += 1
                st.success(
                    f"✓ {pri_locs} location(s) — {pri_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Primient scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python primient_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape Platinum now", key="platinum_scrape_btn"):
        from platinum_scraper import fetch_platinum_bids as _fetch_plat
        from parsers.platinum_parser import parse_platinum_location as _parse_plat
        with st.spinner("Fetching Platinum Crush soybean bids (Alta)…"):
            try:
                _ptlocs = _fetch_plat()
                plat_rows = 0
                plat_locs = 0
                for _ptloc in _ptlocs:
                    _ptsnap = _parse_plat(_ptloc)
                    if _ptsnap:
                        upsert_snapshot(_ptsnap.model_dump())
                        plat_rows += len(_ptsnap.rows)
                        plat_locs += 1
                st.success(
                    f"✓ {plat_locs} location(s) — {plat_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Platinum scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python platinum_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape Shell Rock now", key="shellrock_scrape_btn"):
        from shellrock_scraper import fetch_shellrock_bids as _fetch_sr
        from parsers.shellrock_parser import parse_shellrock_location as _parse_sr
        with st.spinner("Fetching Shell Rock soybean bids…"):
            try:
                _srlocs = _fetch_sr()
                sr_rows = 0
                sr_locs = 0
                for _srloc in _srlocs:
                    _srsnap = _parse_sr(_srloc)
                    if _srsnap:
                        upsert_snapshot(_srsnap.model_dump())
                        sr_rows += len(_srsnap.rows)
                        sr_locs += 1
                st.success(
                    f"✓ {sr_locs} location(s) — {sr_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Shell Rock scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python shellrock_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape White River now", key="whiteriver_scrape_btn"):
        from whiteriver_scraper import fetch_whiteriver_bids as _fetch_wr
        from parsers.whiteriver_parser import parse_whiteriver_location as _parse_wr
        with st.spinner("Fetching White River Soy bids (Seymour)…"):
            try:
                _wrlocs = _fetch_wr()
                wr_rows = 0
                wr_locs = 0
                for _wrloc in _wrlocs:
                    _wrsnap = _parse_wr(_wrloc)
                    if _wrsnap:
                        upsert_snapshot(_wrsnap.model_dump())
                        wr_rows += len(_wrsnap.rows)
                        wr_locs += 1
                st.success(
                    f"✓ {wr_locs} location(s) — {wr_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"White River scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python whiteriver_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape HPPSD now", key="hppsd_scrape_btn"):
        from hppsd_scraper import fetch_hppsd_bids as _fetch_hpp
        from parsers.hppsd_parser import parse_hppsd_location as _parse_hpp
        with st.spinner("Fetching HPPSD soybean bids (Mitchell)…"):
            try:
                _hplocs = _fetch_hpp()
                hpp_rows = 0
                hpp_locs = 0
                for _hploc in _hplocs:
                    _hpsnap = _parse_hpp(_hploc)
                    if _hpsnap:
                        upsert_snapshot(_hpsnap.model_dump())
                        hpp_rows += len(_hpsnap.rows)
                        hpp_locs += 1
                st.success(
                    f"✓ {hpp_locs} location(s) — {hpp_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"HPPSD scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python hppsd_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape Bartlett now", key="bartlett_scrape_btn"):
        from bartlett_scraper import fetch_bartlett_bids as _fetch_brt
        from parsers.bartlett_parser import parse_bartlett_location as _parse_brt
        with st.spinner("Fetching Bartlett Grain bids (17 locations)..."):
            try:
                _brtlocs = _fetch_brt()
                brt_rows = 0
                brt_locs = 0
                for _brtloc in _brtlocs:
                    _brtsnap = _parse_brt(_brtloc)
                    if _brtsnap:
                        upsert_snapshot(_brtsnap.model_dump())
                        brt_rows += len(_brtsnap.rows)
                        brt_locs += 1
                st.success(
                    f"✓ {brt_locs} location(s) — {brt_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Bartlett scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#2563eb">python bartlett_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="margin-bottom:4px">
  <div style="font-size:9px;color:#1d4ed8;letter-spacing:.2em;text-transform:uppercase;
    font-weight:700">JPSI · Cash Grain Basis Monitor</div>
  <div style="font-size:22px;font-weight:800;color:#0f172a;letter-spacing:-.03em;
    line-height:1.2">Basis Tracker</div>
</div>
""", unsafe_allow_html=True)

st.markdown("---")

# ── Provider + Location selector ─────────────────────────────────────────────
prov_col, _ = st.columns([3, 7])
with prov_col:
    provider = st.radio(
        "Provider", ["ADM", "POET", "CHS", "CGB", "Cargill", "GPRE", "Andersons", "Bunge", "Scoular", "AGP", "LDC"],
        horizontal=True, label_visibility="collapsed",
    )

if provider == "CHS":
    chs_db_locs = [r for r in _cached_list_locations() if r["provider"] == "CHS"]
    if not chs_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No CHS data yet.<br><br>'
            'Run <code style="color:#2563eb">python auto_import.py --chs-only</code> '
            'to scrape all CHS locations, then refresh this page.'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # ── Load state / type metadata ────────────────────────────────────────────
    chs_meta = _cached_get_location_meta("CHS")   # {location: {"state": ..., "facility_type": ...}}
    all_chs_names = {r["location"] for r in chs_db_locs}

    def _loc_state(name: str) -> str:
        return chs_meta.get(name, {}).get("state", "") or ""

    def _loc_type(name: str) -> str:
        return chs_meta.get(name, {}).get("facility_type", "") or "Country Elevator"

    states_avail = sorted({_loc_state(n) for n in all_chs_names if _loc_state(n)})

    # ── Filter controls: State | Facility Type ────────────────────────────────
    filt_state_col, filt_type_col = st.columns([2, 5])
    with filt_state_col:
        sel_state = st.selectbox(
            "State",
            options=["All States"] + states_avail,
            key="chs_state_filter",
            label_visibility="collapsed",
        )
    with filt_type_col:
        sel_type = st.radio(
            "Type",
            options=["All", "Corn Processing", "Country Elevator", "Ethanol", "Rail Terminal", "River Terminal", "Soy Crush"],
            horizontal=True,
            key="chs_type_filter",
            label_visibility="collapsed",
        )

    # ── Apply filters → sorted location list ─────────────────────────────────
    filtered_locs = sorted([
        r["location"] for r in chs_db_locs
        if (sel_state == "All States" or _loc_state(r["location"]) == sel_state)
        and (sel_type == "All" or _loc_type(r["location"]) == sel_type)
    ])

    if not filtered_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:20px;font-size:12px">'
            'No CHS locations match the selected filters.'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    sel_chs_loc = st.selectbox(
        "CHS Location",
        options=filtered_locs,
        key="chs_loc_select",
        label_visibility="collapsed",
    )
    loc_key   = sel_chs_loc
    loc_color = "#16a34a"   # green for CHS
    _chs_snaps = _cached_get_snapshots("CHS", loc_key)
    if _chs_snaps:
        grains = _build_grains(_chs_snaps[-1].rows)
    else:
        grains = ["Corn"]

elif provider == "POET":
    # Dynamically load POET locations from whatever's in the database.
    # Each row is {"provider": "POET", "location": "Alexandria, IN"}.
    poet_db_locs = [r for r in _cached_list_locations() if r["provider"] == "POET"]
    if not poet_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No POET data yet.<br><br>'
            'Run <code style="color:#2563eb">python auto_import.py --poet-only</code> '
            'to scrape all 36 POET Gradable locations, then refresh this page.'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    poet_loc_names = [r["location"] for r in poet_db_locs]
    sel_poet_loc = st.selectbox(
        "POET Location",
        options=poet_loc_names,
        key="poet_loc_select",
        label_visibility="collapsed",
    )
    loc_key   = sel_poet_loc
    loc_color = "#f97316"   # orange for POET Grain
    # Detect available grains from the latest snapshot for this location
    _poet_snaps = _cached_get_snapshots("POET", loc_key)
    if _poet_snaps:
        grains = _build_grains(_poet_snaps[-1].rows)
    else:
        grains = ["Corn"]

elif provider == "ADM":
    adm_db_locs = sorted({r["location"] for r in _cached_list_locations() if r["provider"] == "ADM"})
    if not adm_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No ADM data yet.<br><br>'
            'Click <b>Scrape ADM now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --adm-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    sel_adm_loc = st.selectbox(
        "ADM Location", options=adm_db_locs,
        key="adm_loc_select", label_visibility="collapsed",
    )
    loc_key   = sel_adm_loc
    loc_color = "#3b82f6"   # blue for ADM
    _adm_snaps = _cached_get_snapshots("ADM", loc_key)
    if _adm_snaps:
        grains = _build_grains(_adm_snaps[-1].rows)
    else:
        grains = ["Corn"]

elif provider == "CGB":
    cgb_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "CGB"}
    )
    if not cgb_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No CGB data yet.<br><br>'
            'Click <b>Scrape CGB now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --cgb-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    cgb_meta        = _cached_get_location_meta("CGB")  # {name: {"state": ..., "facility_type": ...}}
    cgb_states_avail = sorted({
        v["state"] for v in cgb_meta.values()
        if v.get("state") and v["state"] not in ("", "?", "N/A")
    })

    cgb_state_col, cgb_loc_col = st.columns([2, 6])
    with cgb_state_col:
        sel_cgb_state = st.selectbox(
            "State", options=["All States"] + cgb_states_avail,
            key="cgb_state_filter", label_visibility="collapsed",
        )
    with cgb_loc_col:
        if sel_cgb_state == "All States":
            cgb_filtered = cgb_db_locs
        else:
            cgb_filtered = sorted([
                n for n in cgb_db_locs
                if cgb_meta.get(n, {}).get("state") == sel_cgb_state
            ])
        if not cgb_filtered:
            cgb_filtered = cgb_db_locs  # fallback if meta not yet populated
        sel_cgb_loc = st.selectbox(
            "CGB Location", options=cgb_filtered,
            key="cgb_loc_select", label_visibility="collapsed",
        )
    loc_key   = sel_cgb_loc
    loc_color = "#8b5cf6"   # purple for CGB
    _cgb_snaps = _cached_get_snapshots("CGB", loc_key)
    if _cgb_snaps:
        grains = _build_grains(_cgb_snaps[-1].rows)
    else:
        grains = ["Corn"]

elif provider == "GPRE":
    gpre_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "GPRE"}
    )
    if not gpre_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No GPRE data yet.<br><br>'
            'Click <b>Scrape GPRE now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --gpre-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    sel_gpre_loc = st.selectbox(
        "GPRE Location", options=gpre_db_locs,
        key="gpre_loc_select", label_visibility="collapsed",
    )
    loc_key   = sel_gpre_loc
    loc_color = "#16a34a"   # green for GPRE
    grains    = ["Corn"]    # GPRE is corn-only

elif provider == "Cargill":
    cargill_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "Cargill"}
    )
    if not cargill_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No Cargill data yet.<br><br>'
            'Click <b>Scrape Cargill now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --cargill-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    cargill_meta         = _cached_get_location_meta("Cargill")
    cargill_states_avail = sorted({
        v["state"] for v in cargill_meta.values()
        if v.get("state") and v["state"] not in ("", "?", "N/A")
    })

    cargill_state_col, cargill_loc_col = st.columns([2, 6])
    with cargill_state_col:
        sel_cargill_state = st.selectbox(
            "State", options=["All States"] + cargill_states_avail,
            key="cargill_state_filter", label_visibility="collapsed",
        )
    with cargill_loc_col:
        if sel_cargill_state == "All States":
            cargill_filtered = cargill_db_locs
        else:
            cargill_filtered = sorted([
                n for n in cargill_db_locs
                if cargill_meta.get(n, {}).get("state") == sel_cargill_state
            ])
        if not cargill_filtered:
            cargill_filtered = cargill_db_locs  # fallback if meta not yet populated
        sel_cargill_loc = st.selectbox(
            "Cargill Location", options=cargill_filtered,
            key="cargill_loc_select", label_visibility="collapsed",
        )
    loc_key   = sel_cargill_loc
    loc_color = "#0ea5e9"   # sky blue for Cargill
    _cargill_snaps = _cached_get_snapshots("Cargill", loc_key)  # noqa: F841
    if _cargill_snaps:
        grains = _build_grains(_cargill_snaps[-1].rows)
    else:
        grains = ["Corn"]

elif provider == "Andersons":
    andersons_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "Andersons"}
    )
    if not andersons_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No Andersons data yet.<br><br>'
            'Click <b>Scrape Andersons now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --andersons-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    andersons_meta         = _cached_get_location_meta("Andersons")
    andersons_states_avail = sorted({
        v["state"] for v in andersons_meta.values()
        if v.get("state") and v["state"] not in ("", "?", "N/A")
    })

    andersons_state_col, andersons_loc_col = st.columns([2, 6])
    with andersons_state_col:
        sel_andersons_state = st.selectbox(
            "State", options=["All States"] + andersons_states_avail,
            key="andersons_state_filter", label_visibility="collapsed",
        )
    with andersons_loc_col:
        if sel_andersons_state == "All States":
            andersons_filtered = andersons_db_locs
        else:
            andersons_filtered = sorted([
                n for n in andersons_db_locs
                if andersons_meta.get(n, {}).get("state") == sel_andersons_state
            ])
        if not andersons_filtered:
            andersons_filtered = andersons_db_locs  # fallback if meta not yet populated
        sel_andersons_loc = st.selectbox(
            "Andersons Location", options=andersons_filtered,
            key="andersons_loc_select", label_visibility="collapsed",
        )
    loc_key   = sel_andersons_loc
    loc_color = "#f59e0b"   # amber for The Andersons
    _andersons_snaps = _cached_get_snapshots("Andersons", loc_key)
    if _andersons_snaps:
        grains = _build_grains(_andersons_snaps[-1].rows)
    else:
        grains = ["Corn"]

elif provider == "Bunge":
    bunge_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "Bunge"}
    )
    if not bunge_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No Bunge data yet.<br><br>'
            'Click <b>Scrape Bunge now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --bunge-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    bunge_meta         = _cached_get_location_meta("Bunge")
    bunge_states_avail = sorted({
        v["state"] for v in bunge_meta.values()
        if v.get("state") and v["state"] not in ("", "?", "N/A")
    })

    bunge_state_col, bunge_loc_col = st.columns([2, 6])
    with bunge_state_col:
        sel_bunge_state = st.selectbox(
            "State", options=["All States"] + bunge_states_avail,
            key="bunge_state_filter", label_visibility="collapsed",
        )
    with bunge_loc_col:
        if sel_bunge_state == "All States":
            bunge_filtered = bunge_db_locs
        else:
            bunge_filtered = sorted([
                n for n in bunge_db_locs
                if bunge_meta.get(n, {}).get("state") == sel_bunge_state
            ])
        if not bunge_filtered:
            bunge_filtered = bunge_db_locs
        sel_bunge_loc = st.selectbox(
            "Bunge Location", options=bunge_filtered,
            key="bunge_loc_select", label_visibility="collapsed",
        )
    loc_key   = sel_bunge_loc
    loc_color = "#dc2626"   # red for Bunge
    _bunge_snaps = _cached_get_snapshots("Bunge", loc_key)
    if _bunge_snaps:
        grains = _build_grains(_bunge_snaps[-1].rows)
    else:
        grains = ["Soybeans"]

elif provider == "Scoular":
    scoular_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "Scoular"}
    )
    if not scoular_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No Scoular data yet.<br><br>'
            'Click <b>Scrape Scoular now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --scoular-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    scoular_meta         = _cached_get_location_meta("Scoular")
    scoular_states_avail = sorted({
        v["state"] for v in scoular_meta.values()
        if v.get("state") and v["state"] not in ("", "?", "N/A")
    })

    scoular_state_col, scoular_loc_col = st.columns([2, 6])
    with scoular_state_col:
        sel_scoular_state = st.selectbox(
            "State", options=["All States"] + scoular_states_avail,
            key="scoular_state_filter", label_visibility="collapsed",
        )
    with scoular_loc_col:
        if sel_scoular_state == "All States":
            scoular_filtered = scoular_db_locs
        else:
            scoular_filtered = sorted([
                n for n in scoular_db_locs
                if scoular_meta.get(n, {}).get("state") == sel_scoular_state
            ])
        if not scoular_filtered:
            scoular_filtered = scoular_db_locs  # fallback if meta not yet populated
        sel_scoular_loc = st.selectbox(
            "Scoular Location", options=scoular_filtered,
            key="scoular_loc_select", label_visibility="collapsed",
        )
    loc_key   = sel_scoular_loc
    loc_color = "#f97316"   # orange for Scoular
    _scoular_snaps = _cached_get_snapshots("Scoular", loc_key)
    if _scoular_snaps:
        grains = _build_grains(_scoular_snaps[-1].rows)
    else:
        grains = ["Corn"]

elif provider == "AGP":
    agp_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "AGP"}
    )
    if not agp_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No AGP data yet.<br><br>'
            'Click <b>Scrape AGP now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --agp-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    agp_meta         = _cached_get_location_meta("AGP")
    agp_states_avail = sorted({
        v["state"] for v in agp_meta.values()
        if v.get("state") and v["state"] not in ("", "?", "N/A")
    })

    agp_state_col, agp_loc_col = st.columns([2, 6])
    with agp_state_col:
        sel_agp_state = st.selectbox(
            "State", options=["All States"] + agp_states_avail,
            key="agp_state_filter", label_visibility="collapsed",
        )
    with agp_loc_col:
        if sel_agp_state == "All States":
            agp_filtered = agp_db_locs
        else:
            agp_filtered = sorted([
                n for n in agp_db_locs
                if agp_meta.get(n, {}).get("state") == sel_agp_state
            ])
        if not agp_filtered:
            agp_filtered = agp_db_locs
        sel_agp_loc = st.selectbox(
            "AGP Location", options=agp_filtered,
            key="agp_loc_select", label_visibility="collapsed",
        )
    loc_key   = sel_agp_loc
    loc_color = "#22c55e"   # green for AGP
    _agp_snaps = _cached_get_snapshots("AGP", loc_key)
    if _agp_snaps:
        grains = _build_grains(_agp_snaps[-1].rows)
    else:
        grains = ["Soybeans"]

elif provider == "LDC":
    ldc_db_locs = sorted(
        {r["location"] for r in _cached_list_locations() if r["provider"] == "LDC"}
    )
    if not ldc_db_locs:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            'No LDC data yet.<br><br>'
            'Click <b>Scrape LDC now</b> in the sidebar or run:<br>'
            '<code style="color:#2563eb">python auto_import.py --ldc-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    ldc_meta         = _cached_get_location_meta("LDC")
    ldc_states_avail = sorted({
        v["state"] for v in ldc_meta.values()
        if v.get("state") and v["state"] not in ("", "?", "N/A")
    })

    ldc_state_col, ldc_loc_col = st.columns([2, 6])
    with ldc_state_col:
        sel_ldc_state = st.selectbox(
            "State", options=["All States"] + ldc_states_avail,
            key="ldc_state_filter", label_visibility="collapsed",
        )
    with ldc_loc_col:
        if sel_ldc_state == "All States":
            ldc_filtered = ldc_db_locs
        else:
            ldc_filtered = sorted([
                n for n in ldc_db_locs
                if ldc_meta.get(n, {}).get("state") == sel_ldc_state
            ])
        if not ldc_filtered:
            ldc_filtered = ldc_db_locs
        sel_ldc_loc = st.selectbox(
            "LDC Location", options=ldc_filtered,
            key="ldc_loc_select", label_visibility="collapsed",
        )
    loc_key   = sel_ldc_loc
    loc_color = "#3b82f6"   # blue for LDC
    _ldc_snaps = _cached_get_snapshots("LDC", loc_key)
    if _ldc_snaps:
        grains = _build_grains(_ldc_snaps[-1].rows)
    else:
        grains = ["Corn"]


tab_bids, tab_map, tab_summary = st.tabs(["📋 Bids", "🗺️ Map", "📊 Summary"])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB: BIDS
# ═══════════════════════════════════════════════════════════════════════════════
with tab_bids:
    # ── Cascade filters ───────────────────────────────────────────────────────
    _all_bids_locs = _cached_get_bids_filter_data()

    _flt1, _flt2, _flt3, _flt4 = st.columns([2, 1, 3, 2])

    with _flt1:
        _fac_types = sorted({l["facility_type"] for l in _all_bids_locs if l["facility_type"]})
        _sel_type  = st.selectbox("Location Type", ["All"] + _fac_types, key="bids_flt_type")

    _locs_by_type = [l for l in _all_bids_locs
                     if _sel_type == "All" or l["facility_type"] == _sel_type]

    with _flt2:
        _states    = sorted({l["state"] for l in _locs_by_type if l["state"]})
        _sel_state = st.selectbox("State", ["All"] + _states, key="bids_flt_state")

    _locs_filtered = [l for l in _locs_by_type
                      if _sel_state == "All" or l["state"] == _sel_state]

    # Location picker — default to current sidebar selection when it matches
    bids_provider = provider
    bids_loc_key  = loc_key
    with _flt3:
        if _locs_filtered:
            _loc_opts  = [f"{l['location']} ({l['provider']})" for l in _locs_filtered]
            _def_label = f"{loc_key} ({provider})"
            _def_idx   = _loc_opts.index(_def_label) if _def_label in _loc_opts else 0
            _sel_loc_label = st.selectbox("Location", _loc_opts, index=_def_idx, key="bids_flt_loc")
            _sel_loc_data  = _locs_filtered[_loc_opts.index(_sel_loc_label)]
            bids_provider  = _sel_loc_data["provider"]
            bids_loc_key   = _sel_loc_data["location"]
        else:
            st.caption("No locations match.")

    bids_loc_color = _PROVIDER_COLOR.get(bids_provider, "#64748b")

    # ── Load snapshots ────────────────────────────────────────────────────────
    snapshots = _cached_get_snapshots(bids_provider, bids_loc_key)

    # ── Commodity filter (populated from latest snapshot) ─────────────────────
    with _flt4:
        _avail_grains = _build_grains(snapshots[-1].rows) if snapshots else []
        grain = st.selectbox(
            "Commodity",
            _avail_grains if _avail_grains else ["—"],
            key="bids_flt_grain",
        )

    # ── No-data message ───────────────────────────────────────────────────────
    if not snapshots:
        _p = bids_provider
        if _p == "POET":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --poet-only</code> to scrape this location, then refresh.'
        elif _p == "ADM":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --adm-only</code> or click <b>Scrape ADM now</b> in the sidebar, then refresh.'
        elif _p == "CGB":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --cgb-only</code> or click <b>Scrape CGB now</b> in the sidebar, then refresh.'
        elif _p == "CHS":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --chs-only</code> or click <b>Scrape CHS now</b> in the sidebar, then refresh.'
        elif _p == "Cargill":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --cargill-only</code>, then refresh.'
        elif _p == "GPRE":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --gpre-only</code>, then refresh.'
        elif _p == "Andersons":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --andersons-only</code>, then refresh.'
        elif _p == "Bunge":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --bunge-only</code>, then refresh.'
        elif _p == "Scoular":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --scoular-only</code>, then refresh.'
        elif _p == "AGP":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --agp-only</code>, then refresh.'
        elif _p == "LDC":
            hint = 'Run <code style="color:#2563eb">python auto_import.py --ldc-only</code>, then refresh.'
        else:
            hint = "Run the daily scraper to populate data for this location."
        st.markdown(
            f'<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            f'No snapshots yet for <b>{bids_loc_key}</b>.<br><br>{hint}</div>',
            unsafe_allow_html=True,
        )
    else:
        # ── Date picker ───────────────────────────────────────────────────────
        snap_labels = []
        for s in snapshots:
            d = datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
            lbl = d.strftime("%b %d, %Y") + (" ★ latest" if s is snapshots[-1] else "")
            snap_labels.append(lbl)

        sel_label_snap = st.selectbox(
            "Viewing snapshot",
            options=snap_labels[::-1],
            index=0,
            key=f"snap_pick_{bids_loc_key}",
            label_visibility="visible",
        )
        sel_idx     = snap_labels[::-1].index(sel_label_snap)
        viewing     = snapshots[::-1][sel_idx]
        snaps_up_to = snapshots[: snapshots.index(viewing) + 1]
        changes     = compute_changes(snaps_up_to)

        body_rows      = [r for r in viewing.rows if not r.isSpot and _grain_disp(r.grain) == grain]
        explicit_spot  = next((r for r in viewing.rows
                               if r.isSpot and _grain_disp(r.spotGrain or r.grain) == grain), None)
        derived_spot   = _front_month_row(viewing.rows, grain)
        spot_row       = explicit_spot or derived_spot
        spot_chg       = changes["spots"].get(grain) or changes.get("derived_spots", {}).get(grain)

        moved = sum(1 for r in body_rows
                    if changes["rows"].get(r.id, {}).get("fromPrev", {}).get("val") not in (None, 0))

        # Status bar
        latest_label = datetime.fromisoformat(
            viewing.timestamp.replace("Z", "+00:00")
        ).strftime("%a %b %d, %Y")
        s_col1, s_col2 = st.columns([3, 7])
        with s_col1:
            if moved:
                st.markdown(
                    f'<span style="color:#d97706;font-size:11px;font-weight:600">'
                    f'● {moved} changed vs prior</span>', unsafe_allow_html=True)
            else:
                st.markdown(
                    '<span style="color:#94a3b8;font-size:11px">No changes vs prior</span>',
                    unsafe_allow_html=True)
        with s_col2:
            st.markdown(
                f'<span style="color:#64748b;font-size:10px">as of '
                f'<span style="color:#2563eb;font-weight:700">{latest_label}</span></span>',
                unsafe_allow_html=True)

        # Year-ago label for header
        now_ms = datetime.fromisoformat(
            viewing.timestamp.replace("Z", "+00:00")).timestamp() * 1000
        YEAR = 365 * 864e5
        year_ago_ts = None
        for snap in reversed(snaps_up_to):
            ts_ms = datetime.fromisoformat(
                snap.timestamp.replace("Z", "+00:00")).timestamp() * 1000
            if abs(ts_ms - (now_ms - YEAR)) <= 5 * 864e5:
                year_ago_ts = snap.timestamp
                break
        year_ago_label = (
            datetime.fromisoformat(year_ago_ts.replace("Z", "+00:00")).strftime("%b %d '%y")
            if year_ago_ts else "~1 yr"
        )

        table_html = render_table(
            body_rows, spot_row, changes, spot_chg, bids_loc_color, year_ago_label,
            is_meal=(grain == "Soybean Meal"),
        )
        st.markdown(table_html, unsafe_allow_html=True)

        # Roll adjustment legend
        roll_parts = " &nbsp;|&nbsp; ".join(
            f'<span style="color:#2563eb">{r["from"]}->{r["to"]}</span>'
            f' {r["adj"]}c' for r in ROLL_ADJ)
        st.markdown(
            f'<div style="margin-top:8px;padding:8px 14px;background:#f8fafc;'
            f'border:1px solid #e2e8f0;border-radius:6px;font-size:10px;color:#64748b">'
            f'<span style="color:#94a3b8;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.1em">Roll adj:</span> {roll_parts}'
            f' &nbsp;|&nbsp; <span style="font-size:9px">Same letter diff year = no adj'
            f' | ? = unknown roll</span></div>',
            unsafe_allow_html=True,
        )

        # ── Spot basis history chart ──────────────────────────────────────────
        _spot_pts = []
        for _snap in snapshots:
            _fr = _front_month_row(_snap.rows, grain)
            if _fr and _fr.basisCents is not None:
                _dt = datetime.fromisoformat(_snap.timestamp.replace("Z", "+00:00"))
                _spot_pts.append({
                    "Date":     _dt,
                    "Basis":    _fr.basisCents,
                    "Contract": _fr.futuresSymbol,
                    "Delivery": _fr.deliveryMonth,
                })

        if len(_spot_pts) >= 2:
            import pandas as _pd
            import altair as _alt
            _df_spot = _pd.DataFrame(_spot_pts)
            _spot_color = bids_loc_color
            _zero_rule  = _alt.Chart(_pd.DataFrame({"y": [0]})).mark_rule(
                color="#94a3b8", strokeDash=[4, 4], strokeWidth=1
            ).encode(y="y:Q")
            _spot_line = (
                _alt.Chart(_df_spot)
                .mark_line(point=True, color=_spot_color, strokeWidth=2)
                .encode(
                    x=_alt.X("Date:T", title=None,
                              axis=_alt.Axis(format="%b %d '%y", labelAngle=-30, labelFontSize=10)),
                    y=_alt.Y("Basis:Q", title="Spot Basis (¢)",
                             scale=_alt.Scale(zero=False),
                             axis=_alt.Axis(labelFontSize=10)),
                    tooltip=[
                        _alt.Tooltip("Date:T",     format="%b %d, %Y", title="Date"),
                        _alt.Tooltip("Basis:Q",    title="Basis (¢)"),
                        _alt.Tooltip("Contract:N", title="Futures"),
                        _alt.Tooltip("Delivery:N", title="Delivery"),
                    ],
                )
            )
            st.markdown(
                '<div style="margin-top:16px;margin-bottom:4px;font-size:10px;color:#64748b;'
                'font-weight:700;text-transform:uppercase;letter-spacing:.1em">'
                'Spot Basis History (front-month)</div>',
                unsafe_allow_html=True,
            )
            st.altair_chart((_zero_rule + _spot_line).properties(height=200),
                            use_container_width=True)
        elif len(_spot_pts) == 1:
            st.caption("Only 1 snapshot — scrape more data to see spot history chart.")

    # ── Snapshot history ──────────────────────────────────────────────────────
    if snapshots:
        with st.expander(f"Snapshot history — {bids_loc_key} ({len(snapshots)} records)", expanded=False):
            for snap in reversed(snapshots):
                is_latest  = snap is snapshots[-1]
                is_viewing = snap is viewing
                d_label    = datetime.fromisoformat(
                    snap.timestamp.replace("Z", "+00:00")).strftime("%b %d '%y")
                src_icon    = " [email]" if snap.source == "email" else ""
                badge_color = bids_loc_color if is_viewing else "#e2e8f0"
                c1, c2 = st.columns([9, 1])
                with c1:
                    st.markdown(
                        f'<span style="background:#f8fafc;border:1px solid {badge_color};'
                        f'color:{badge_color};padding:3px 10px;border-radius:3px;'
                        f'font-size:10px;font-weight:{"700" if is_latest else "400"}">'
                        f'{d_label}{src_icon}{"  latest" if is_latest else ""}{"  viewing" if is_viewing and not is_latest else ""}</span>',
                        unsafe_allow_html=True)
                with c2:
                    if not is_latest:
                        if st.button("X", key=f"del_{snap.id}", help="Delete snapshot"):
                            delete_snapshot(snap.id)
                            st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# TAB: MAP
# ═══════════════════════════════════════════════════════════════════════════════
with tab_map:
    import pydeck as pdk
    import pandas as pd

    _MAP_COLORS = {
        "ADM":       [59,  130, 246],
        "POET":      [249, 115, 22],
        "CHS":       [22,  163, 74],
        "CGB":       [139, 92,  246],
        "Cargill":   [239, 68,  68],
        "GPRE":      [6,   182, 212],
        "Andersons": [234, 179, 8],
        "Bunge":     [244, 63,  94],
        "Scoular":   [132, 204, 22],
        "AGP":       [34,  197, 94],
        "LDC":       [96,  165, 250],
    }
    _DEFAULT_COLOR = [148, 163, 184]

    map_rows = _cached_get_map_data()

    if not map_rows:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:60px;font-size:12px">'
            'No geocoded locations yet.<br><br>'
            'Run <code style="color:#2563eb">python geocode_locations.py</code> '
            'to populate coordinates, then refresh.</div>',
            unsafe_allow_html=True,
        )
    else:
        # ── Filters ───────────────────────────────────────────────────────────
        all_ftypes_map    = sorted({r["facility_type"] for r in map_rows if r.get("facility_type")})
        all_providers_map = sorted({r["provider"] for r in map_rows})
        all_states_map    = sorted({r["state"] for r in map_rows if r.get("state")})

        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            sel_ftypes = st.multiselect(
                "Location Type",
                options=all_ftypes_map,
                default=[],
                placeholder="All types",
                key="map_ftype_filter",
            )
        with fc2:
            sel_states = st.multiselect(
                "State",
                options=all_states_map,
                default=[],
                placeholder="All states",
                key="map_state_filter",
            )
        with fc3:
            sel_provs = st.multiselect(
                "Provider",
                options=all_providers_map,
                default=all_providers_map,
                key="map_prov_filter",
            )

        # Pre-filter by location type, state, and provider; downstream dropdowns
        # derive their options from this subset so they stay relevant.
        pre_filtered = [
            r for r in map_rows
            if r["provider"] in sel_provs
            and (not sel_ftypes or r.get("facility_type") in sel_ftypes)
            and (not sel_states or r.get("state") in sel_states)
        ]

        # ── Delivery Zone sub-filter (River Terminals only) ───────────────────
        rt_in_set = [r for r in pre_filtered if r.get("facility_type") == "River Terminal"]
        avail_zones = sorted({r["delivery_zone"] for r in rt_in_set if r.get("delivery_zone")})
        has_unzoned = any(not r.get("delivery_zone") for r in rt_in_set)
        zone_opts   = avail_zones + (["(No Zone)"] if has_unzoned else [])

        if zone_opts:
            sel_zones_list = st.multiselect(
                "Delivery Zone (River Terminals)",
                options=zone_opts,
                default=zone_opts,
                key="map_zone_filter",
                help=(
                    "Illinois Waterway CBOT delivery zones:\n"
                    "Zone 1 — Chicago / Burns Harbor (≥ mile 304)\n"
                    "Zone 2 — Lockport to Seneca (mile 244.6–304)\n"
                    "Zone 3 — Ottawa to Chillicothe (mile 170–244.6)\n"
                    "Zone 4 — Peoria to Pekin (mile 151–170)\n"
                    "Zone 5 — Havana to Grafton + St. Louis district\n"
                    "(No Zone) — Upper/Lower Mississippi and other rivers"
                ),
            )
            sel_zones = set(sel_zones_list)

            def _zone_matches(r: dict) -> bool:
                if r.get("facility_type") != "River Terminal":
                    return True
                rz = r.get("delivery_zone") or "(No Zone)"
                return rz in sel_zones

            zone_filtered = [r for r in pre_filtered if _zone_matches(r)]
        else:
            zone_filtered = pre_filtered

        # ── Commodity dropdown (options from zone-filtered set) ───────────────
        def _base_commodity(g: str) -> str:
            return "Wheat" if (g == "Wheat" or g.startswith("Wheat (")) else g

        avail_base = sorted({_base_commodity(g) for r in zone_filtered for g in r["grains"]})

        sel_commodity = st.selectbox(
            "Commodity",
            options=["All"] + avail_base,
            index=0,
            key="map_grain_filter",
        )
        sel_base_commodities = set(avail_base) if sel_commodity == "All" else {sel_commodity}

        # ── Wheat class sub-filter (shown only when Wheat is selected) ────────
        sel_wheat_classes: set | None = None
        if sel_commodity == "Wheat":
            avail_wheat_classes: set[str] = set()
            for r in zone_filtered:
                for g in r["grains"]:
                    if g == "Wheat":
                        avail_wheat_classes.add("(unclassified)")
                    elif g.startswith("Wheat ("):
                        avail_wheat_classes.add(g[7:g.index(")")].split()[0])

            if len(avail_wheat_classes) > 1:
                avail_wc_sorted = sorted(avail_wheat_classes)
                sel_wc_list = st.multiselect(
                    "Wheat class",
                    options=avail_wc_sorted,
                    default=avail_wc_sorted,
                    key="map_wheat_class_filter",
                )
                sel_wheat_classes = set(sel_wc_list)

        # ── Apply commodity + wheat class filter ──────────────────────────────
        def _pin_matches(r: dict) -> bool:
            for g in r["grains"]:
                base = _base_commodity(g)
                if base not in sel_base_commodities:
                    continue
                if base != "Wheat" or sel_wheat_classes is None:
                    return True
                if g == "Wheat":
                    return "(unclassified)" in sel_wheat_classes
                if g.startswith("Wheat ("):
                    cls = g[7:g.index(")")].split()[0]
                    if cls in sel_wheat_classes:
                        return True
            return False

        filtered = [r for r in zone_filtered if _pin_matches(r)]

        # ── Build DataFrame ───────────────────────────────────────────────────
        def _fmt_basis(cents):
            if cents is None:
                return "—"
            sign = "+" if cents >= 0 else ""
            return f"{sign}{cents}c"

        def _tooltip_text(row):
            grains_str = "  ".join(
                f"{g}: {_fmt_basis(v)}"
                for g, v in sorted(row["grains"].items())
            )
            state_str = f", {row['state']}" if row["state"] else ""
            ft_str    = f" · {row['facility_type']}" if row.get("facility_type") else ""
            dz_str    = f" · {row['delivery_zone']}" if row.get("delivery_zone") else ""
            return f"{row['location']}{state_str} [{row['provider']}]{ft_str}{dz_str}  |  {grains_str}"

        df = pd.DataFrame([
            {
                "lat":      r["lat"],
                "lon":      r["lon"],
                "location": r["location"],
                "provider": r["provider"],
                "state":    r["state"],
                "tooltip":  _tooltip_text(r),
                "color":    _MAP_COLORS.get(r["provider"], _DEFAULT_COLOR),
            }
            for r in filtered
        ])

        layer = pdk.Layer(
            "ScatterplotLayer",
            data=df,
            get_position=["lon", "lat"],
            get_fill_color="color",
            get_radius=8000,
            radius_min_pixels=4,
            radius_max_pixels=18,
            pickable=True,
            auto_highlight=True,
        )

        view = pdk.ViewState(
            latitude=39.5,
            longitude=-98.35,
            zoom=3.8,
            pitch=0,
        )

        deck = pdk.Deck(
            layers=[layer],
            initial_view_state=view,
            tooltip={"text": "{tooltip}"},
            map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        )

        st.pydeck_chart(deck, use_container_width=True)

        # ── Legend ────────────────────────────────────────────────────────────
        legend_parts = []
        for p in all_providers_map:
            c = _MAP_COLORS.get(p, _DEFAULT_COLOR)
            hex_c = "#{:02x}{:02x}{:02x}".format(*c)
            legend_parts.append(
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'margin-right:14px;font-size:11px;color:#374151">'
                f'<span style="width:10px;height:10px;border-radius:50%;'
                f'background:{hex_c};display:inline-block"></span>{p}</span>'
            )
        st.markdown(
            '<div style="padding:8px 0;margin-top:4px">' + "".join(legend_parts) + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(f"{len(filtered)} locations shown  •  geocoding may be incomplete — run `python geocode_locations.py` to fill gaps")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB: SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
with tab_summary:
    from datetime import timedelta
    from collections import Counter as _Counter

    # ── Timestamp helpers ─────────────────────────────────────────────────────
    def _sum_ts(ts: str) -> datetime:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return datetime.min

    def _sum_closest(snaps: list, target: datetime, max_days: float):
        if not snaps:
            return None
        best = min(snaps, key=lambda s: abs((_sum_ts(s.timestamp) - target).total_seconds()))
        diff_days = abs((_sum_ts(best.timestamp) - target).total_seconds()) / 86400
        if diff_days > max_days:
            return None
        return best

    def _sum_extract(snap, grain: str, mode: str):
        """Return (basis_cents, futures_symbol) from a snapshot. mode='spot' or a futures symbol."""
        if snap is None:
            return None, None
        if mode == "spot":
            row = _front_month_row(snap.rows, grain)
        else:
            row = next(
                (r for r in snap.rows
                 if r.futuresSymbol == mode
                 and _grain_disp(r.grain) == grain
                 and not r.isSpot),
                None,
            )
        if row and row.basisCents is not None:
            return row.basisCents, row.futuresSymbol
        return None, None

    # ── Filters row ───────────────────────────────────────────────────────────
    _sl = _cached_get_bids_filter_data()  # [{provider, location, state, facility_type, region}]

    _sf1, _sf2, _sf3 = st.columns([2, 2, 2])
    with _sf1:
        _sfac_types = sorted({l["facility_type"] for l in _sl if l["facility_type"]})
        _ssel_types = st.multiselect(
            "Location Type", _sfac_types,
            default=["Soy Processing"] if "Soy Processing" in _sfac_types else [],
            key="sum_ftype",
        )
    with _sf2:
        _slocs_by_t = [l for l in _sl if not _ssel_types or l["facility_type"] in _ssel_types]
        _sstates    = sorted({l["state"] for l in _slocs_by_t if l["state"]})
        _ssel_states = st.multiselect("State", _sstates, key="sum_state")
    with _sf3:
        _sgrains = ["Soybeans", "Corn", "Wheat", "Soybean Meal", "Soybean Oil"]
        _sgrain  = st.selectbox("Grain", _sgrains, key="sum_grain")

    # ── Apply filters ─────────────────────────────────────────────────────────
    _sfilt = [
        l for l in _sl
        if (not _ssel_types  or l["facility_type"] in _ssel_types)
        and (not _ssel_states or l["state"]         in _ssel_states)
    ]

    if not _sfilt:
        st.info("No locations match the selected filters.")
    else:
        # ── Load snapshots (cached) ───────────────────────────────────────────
        _spairs = tuple((l["provider"], l["location"]) for l in _sfilt)

        @st.cache_data(ttl=300, show_spinner="Loading history…")
        def _load_bulk(pairs):
            return get_snapshots_bulk(list(pairs))

        _sdata = _load_bulk(_spairs)  # {(prov, loc): [Snapshot, ...]}

        # ── Delivery period options ───────────────────────────────────────────
        _avail_syms: dict[str, str] = {}  # symbol → display "ZSN26 (Jul '26)"
        for key, snaps in _sdata.items():
            if not snaps:
                continue
            latest = snaps[-1]
            for r in latest.rows:
                if _grain_disp(r.grain) == _sgrain and not r.isSpot and r.futuresSymbol:
                    sym = r.futuresSymbol
                    if sym not in _avail_syms:
                        _avail_syms[sym] = f"{sym}  ({short_sym(sym)})"

        _deliv_opts = ["Spot (Front Month)"] + sorted(_avail_syms.values())
        _sel_deliv  = st.selectbox("Delivery Period", _deliv_opts, key="sum_delivery")
        _smode      = "spot" if _sel_deliv.startswith("Spot") else _sel_deliv.split()[0]

        # ── Target dates (naive UTC noon) ─────────────────────────────────────
        _now = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
        _TARGETS = [
            ("yr_ago",  _now - timedelta(days=365), 30),
            ("mo_ago",  _now - timedelta(days=30),  15),
            ("wk_ago",  _now - timedelta(days=7),   10),
            # 0.6 = within ~14 hours of target noon; matches same-day midnight
            # snapshots (12 h away) but not next/prior day midnight (36 h away).
            ("d2_ago",  _now - timedelta(days=2),   0.6),
            ("d1_ago",  _now - timedelta(days=1),   0.6),
            # 1.6 allows showing yesterday's data when today's scrape hasn't run yet.
            ("current", _now,                       1.6),
        ]

        # ── Build one data row per location ───────────────────────────────────
        _smeta = {(l["provider"], l["location"]): l for l in _sfilt}
        _srows = []
        for key in _spairs:
            snaps  = _sdata.get(key, [])
            meta   = _smeta.get(key, {})
            rd: dict = {
                "provider": key[0],
                "location": key[1],
                "state":    meta.get("state", ""),
                "region":   meta.get("region", ""),
            }
            for lbl, tgt, max_d in _TARGETS:
                snap = _sum_closest(snaps, tgt, max_d)
                basis, sym = _sum_extract(snap, _sgrain, _smode)
                rd[f"b_{lbl}"] = basis
                rd[f"s_{lbl}"] = sym
                rd[f"d_{lbl}"] = _sum_ts(snap.timestamp).date() if snap else None
            _srows.append(rd)

        # Keep only rows with current data
        _srows = [r for r in _srows if r["b_current"] is not None]

        # Sort: region (empty last) → provider → location
        _srows.sort(key=lambda r: (r["region"] or "zzz", r["provider"], r["location"]))

        if not _srows:
            st.info(f"No {_sgrain} data found for the selected locations.")
        else:
            # ── Reference symbol (most common in current column) ──────────────
            _sym_counts = _Counter(r["s_current"] for r in _srows if r["s_current"])
            _ref_sym    = _sym_counts.most_common(1)[0][0] if _sym_counts else ""
            _ref_disp   = f"{_ref_sym}  ({short_sym(_ref_sym)})" if _ref_sym else "—"

            # ── Column date headers (most common actual date per column) ──────
            def _col_date(lbl: str) -> str:
                dates = [r[f"d_{lbl}"] for r in _srows if r.get(f"d_{lbl}")]
                if not dates:
                    return "—"
                d = _Counter(dates).most_common(1)[0][0]
                return f"{d.day} {d.strftime('%b')}"

            _cdates = {lbl: _col_date(lbl) for lbl, _, _ in _TARGETS}

            # ── Reference info bar ────────────────────────────────────────────
            st.markdown(
                f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:11px;'
                f'color:#64748b;padding:6px 0 10px 0">'
                f'Reference: <span style="color:#2563eb;font-weight:700">{_ref_disp}</span>'
                f'&nbsp;&nbsp;·&nbsp;&nbsp;'
                f'Grain: <span style="font-weight:700;color:#0f172a">{_sgrain}</span>'
                f'&nbsp;&nbsp;·&nbsp;&nbsp;'
                f'{len(_srows)} locations'
                f'</div>',
                unsafe_allow_html=True,
            )

            # ── HTML table styles ─────────────────────────────────────────────
            _TH_BASE = (
                "font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;"
                "color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;"
                "padding:5px 8px;border-bottom:2px solid #e2e8f0;"
                "position:sticky;top:0;background:#fff;white-space:nowrap"
            )
            _TH_R  = _TH_BASE + ";text-align:right"
            _TH_L  = _TH_BASE + ";text-align:left"
            _TD_L  = ("font-family:'IBM Plex Mono',monospace;font-size:11px;"
                      "padding:3px 8px;border-bottom:1px solid #f1f5f9;text-align:left;white-space:nowrap")
            _TD_R  = ("font-family:'IBM Plex Mono',monospace;font-size:11px;"
                      "padding:3px 8px;border-bottom:1px solid #f1f5f9;text-align:right;white-space:nowrap")

            def _bcell(basis, sym) -> str:
                if basis is None:
                    return f'<td style="{_TD_R};color:#cbd5e1">—</td>'
                sign  = "+" if basis >= 0 else ""
                badge = ""
                if sym and sym != _ref_sym:
                    badge = (f'<span style="font-size:9px;color:#f59e0b;'
                             f'margin-left:2px;font-weight:700">{short_sym(sym)}</span>')
                return f'<td style="{_TD_R}">{sign}{basis}{badge}</td>'

            def _ccell(chg) -> str:
                if chg is None:
                    return f'<td style="{_TD_R};color:#cbd5e1">—</td>'
                if chg == 0:
                    return f'<td style="{_TD_R};color:#64748b">—</td>'
                sign  = "+" if chg > 0 else ""
                color = "#16a34a" if chg > 0 else "#dc2626"
                fw    = "700"
                return f'<td style="{_TD_R};color:{color};font-weight:{fw}">{sign}{chg}</td>'

            # ── Build HTML ────────────────────────────────────────────────────
            _COL_META = [
                ("yr_ago",  "Last Year"),
                ("mo_ago",  "Last Mo"),
                ("wk_ago",  "Last Wk"),
                ("d2_ago",  "−2 Days"),
                ("d1_ago",  "Yest"),
                ("current", "Today"),
            ]

            h = (
                '<div style="overflow-x:auto;max-height:72vh;overflow-y:auto;'
                'border:1px solid #e2e8f0;border-radius:6px">'
                '<table style="border-collapse:collapse;width:100%;min-width:900px">'
                '<thead>'
                # Row 1 — group labels
                '<tr style="background:#f8fafc">'
                f'<th colspan="4" style="{_TH_L}"></th>'
                f'<th colspan="3" style="{_TH_L};border-left:1px solid #e2e8f0;'
                f'color:#64748b">Historical</th>'
                f'<th colspan="3" style="{_TH_L};border-left:1px solid #e2e8f0;'
                f'color:#0f172a">Current</th>'
                f'<th colspan="4" style="{_TH_L};border-left:1px solid #e2e8f0;'
                f'color:#64748b">Changes</th>'
                '</tr>'
                # Row 2 — column names
                '<tr>'
                f'<th style="{_TH_L}">Region</th>'
                f'<th style="{_TH_L}">Company</th>'
                f'<th style="{_TH_L}">Location</th>'
                f'<th style="{_TH_R}">St</th>'
            )
            for lbl, label in _COL_META:
                bdr = ";border-left:1px solid #e2e8f0" if lbl in ("yr_ago", "current") else ""
                h  += f'<th style="{_TH_R}{bdr}">{label}</th>'
            h += (
                f'<th style="{_TH_R};border-left:1px solid #e2e8f0">Daily</th>'
                f'<th style="{_TH_R}">Weekly</th>'
                f'<th style="{_TH_R}">Monthly</th>'
                f'<th style="{_TH_R}">Yearly</th>'
                '</tr>'
                # Row 3 — actual dates
                '<tr style="background:#fafafa">'
                f'<th colspan="4" style="{_TH_L};font-weight:400;color:#cbd5e1"></th>'
            )
            for lbl, _ in _COL_META:
                bdr = ";border-left:1px solid #e2e8f0" if lbl in ("yr_ago", "current") else ""
                h  += f'<th style="{_TH_R}{bdr};font-weight:400;color:#64748b">{_cdates[lbl]}</th>'
            h += (
                f'<th colspan="4" style="{_TH_R};border-left:1px solid #e2e8f0;'
                f'font-weight:400;color:#cbd5e1"></th>'
                '</tr>'
                '</thead><tbody>'
            )

            _prev_region = object()  # sentinel
            for r in _srows:
                region = r.get("region") or ""

                # Region divider row
                if region != _prev_region:
                    h += (
                        f'<tr><td colspan="14" style="font-family:\'IBM Plex Mono\',monospace;'
                        f'font-size:9px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
                        f'letter-spacing:.15em;background:#f8fafc;padding:4px 8px;'
                        f'border-top:2px solid #e2e8f0">'
                        f'{region if region else "—"}</td></tr>'
                    )
                    _prev_region = region

                # Highlight row if using non-reference futures symbol
                _row_bg = "background:#fffbeb" if (r["s_current"] and r["s_current"] != _ref_sym) else ""

                h += f'<tr style="{_row_bg}">'
                h += f'<td style="{_TD_L};color:#64748b;font-size:10px">{region}</td>'
                h += f'<td style="{_TD_L};font-weight:700;color:#1e3a5f">{r["provider"]}</td>'
                h += f'<td style="{_TD_L}">{r["location"]}</td>'
                h += f'<td style="{_TD_R};color:#64748b">{r["state"]}</td>'

                for i, (lbl, _) in enumerate(_COL_META):
                    bdr = ";border-left:1px solid #f1f5f9" if lbl in ("yr_ago", "current") else ""
                    cell = _bcell(r[f"b_{lbl}"], r.get(f"s_{lbl}"))
                    # Inject border into the cell's style
                    h += cell.replace(f'style="{_TD_R}', f'style="{_TD_R}{bdr}', 1)

                # Change columns
                _daily  = (r["b_current"] - r["b_d1_ago"])  if (r["b_current"] is not None and r["b_d1_ago"]  is not None) else None
                _weekly = (r["b_current"] - r["b_wk_ago"])  if (r["b_current"] is not None and r["b_wk_ago"]  is not None) else None
                _montly = (r["b_current"] - r["b_mo_ago"])  if (r["b_current"] is not None and r["b_mo_ago"]  is not None) else None
                _yearly = (r["b_current"] - r["b_yr_ago"])  if (r["b_current"] is not None and r["b_yr_ago"]  is not None) else None

                h += _ccell(_daily).replace(f'style="{_TD_R}',  f'style="{_TD_R};border-left:1px solid #f1f5f9"', 1)
                h += _ccell(_weekly)
                h += _ccell(_montly)
                h += _ccell(_yearly)
                h += '</tr>'

            h += '</tbody></table></div>'
            st.markdown(h, unsafe_allow_html=True)
