"""
Basis Tracker · JPSI
Streamlit app — run with: streamlit run app.py
"""
import os
from datetime import date, datetime, timezone, timedelta
from dotenv import load_dotenv
import streamlit as st

from adm_names import adm_city_from_name
from regions import region_from_state
from river_segments import river_segment, SEGMENT_ORDER
import delivery_period as _dp

from database import (
    init_db, upsert_snapshot, get_snapshots, delete_snapshot,
    list_locations, get_location_meta, get_all_location_meta, get_map_data,
    get_grain_map, get_bids_filter_data, get_snapshots_bulk,
    grain_counts_by_facility,
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

@st.cache_data(ttl=600)
def _cached_grain_counts_by_facility() -> list[tuple]:
    return grain_counts_by_facility()

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
    {"provider": "ADM", "key": "ADM Decatur",     "label": "Decatur",     "grains": ["Corn","Soybeans"],        "color": "#0693e3"},
    {"provider": "ADM", "key": "ADM Cedar Rapids", "label": "Cedar Rapids", "grains": ["Corn"],                  "color": "#22c55e"},
    {"provider": "ADM", "key": "ADM St. Louis",   "label": "St. Louis",   "grains": ["Corn","Soybeans","Wheat"], "color": "#a78bfa"},
]

ROLL_ADJ = [
    {"from": "ZSK26", "to": "ZSN26", "adj": -16},
    {"from": "ZCK26", "to": "ZCN26", "adj": -10},
]

_PROVIDER_COLOR: dict[str, str] = {
    "ADM":       "#0693e3",
    "CHS":       "#16a34a",
    "POET":      "#f97316",
    "CGB":       "#8b5cf6",
    "GPRE":      "#16a34a",
    "Cargill":   "#0ea5e9",
    "Andersons": "#f59e0b",
    "Bunge":     "#dc2626",
    "Scoular":   "#f97316",
    "AGP":       "#22c55e",
    "LDC":       "#0693e3",
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
            f'color:#0578bd;padding:2px 7px;border-radius:3px;font-size:11px;font-weight:800">'
            f'{spot_row.futuresSymbol}</span></td>'
            f'<td style="{td_base};color:#0693e3;font-size:11px">{short_sym(spot_row.futuresSymbol)}</td>'
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
            f'color:#0578bd;padding:2px 7px;border-radius:3px;font-size:11px;font-weight:700">'
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
  @import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700;800&display=swap');
  /* JPSI brand sans-serif everywhere (overrides inline mono); tabular figures keep
     basis numbers aligned in the data tables. */
  html, body, [class*="css"], .stApp, button, input, select, textarea,
  table, td, th, .stMarkdown, [data-testid="stMetricValue"] {
    font-family: 'Open Sans', system-ui, -apple-system, sans-serif !important; }
  table td, table th { font-variant-numeric: tabular-nums; }
  /* Hide Streamlit's fixed header so it doesn't overlap content */
  header[data-testid="stHeader"] { display: none !important; }
  #MainMenu { visibility: hidden !important; }
  footer { visibility: hidden !important; }
  .block-container { padding-top: 0.75rem !important; padding-bottom: 1rem !important; }
  div[data-testid="stHorizontalBlock"] { gap: 0 !important; }
  a { color: #0693e3; }
  /* Tabs — JPSI blue active indicator */
  .stTabs [data-baseweb="tab-list"] { gap: 0; background: #ffffff; border-bottom: 1px solid #e2e8f0; }
  .stTabs [data-baseweb="tab"] { color: #5b6470; font-size: 13px; padding: 8px 18px;
    font-weight: 600; border-radius: 0; }
  .stTabs [aria-selected="true"] { color: #0693e3 !important; font-weight: 700 !important;
    border-bottom: 3px solid #0693e3 !important; }
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
        'CLI: <code style="color:#0693e3">python auto_import.py --adm-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --poet-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --chs-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --cgb-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --cargill-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --bunge-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --andersons-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --scoular-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --ldc-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --agp-only</code>'
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
        'CLI: <code style="color:#0693e3">python auto_import.py --gpre-only</code>'
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
        'CLI: <code style="color:#0693e3">python zfs_scraper.py</code>'
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
        'CLI: <code style="color:#0693e3">python mnsoy_scraper.py</code>'
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
        'CLI: <code style="color:#0693e3">python primient_scraper.py</code>'
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
        'CLI: <code style="color:#0693e3">python platinum_scraper.py</code>'
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
        'CLI: <code style="color:#0693e3">python shellrock_scraper.py</code>'
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
        'CLI: <code style="color:#0693e3">python whiteriver_scraper.py</code>'
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
        'CLI: <code style="color:#0693e3">python hppsd_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

    if st.button("Scrape Norfolk Crush now", key="norfolkcrush_scrape_btn"):
        from norfolkcrush_scraper import fetch_norfolkcrush_bids as _fetch_nfc
        from parsers.norfolkcrush_parser import parse_norfolkcrush_location as _parse_nfc
        with st.spinner("Fetching Norfolk Crush soybean bids (Norfolk, NE)…"):
            try:
                _nfclocs = _fetch_nfc()
                nfc_rows = 0
                nfc_locs = 0
                for _nfcloc in _nfclocs:
                    _nfcsnap = _parse_nfc(_nfcloc)
                    if _nfcsnap:
                        upsert_snapshot(_nfcsnap.model_dump())
                        nfc_rows += len(_nfcsnap.rows)
                        nfc_locs += 1
                st.success(
                    f"✓ {nfc_locs} location(s) — {nfc_rows} bid row(s) upserted."
                )
                st.rerun()
            except Exception as _exc:
                st.error(f"Norfolk Crush scrape failed: {_exc}")
    st.markdown(
        '<div style="font-size:9px;color:#94a3b8;padding-top:4px">'
        'CLI: <code style="color:#0693e3">python norfolkcrush_scraper.py</code>'
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
        'CLI: <code style="color:#0693e3">python bartlett_scraper.py</code>'
        '</div>',
        unsafe_allow_html=True,
    )

# ── Header ────────────────────────────────────────────────────────────────────
# ── Branded header (JPSI / John Stewart & Associates) ────────────────────────
import base64 as _b64
from pathlib import Path as _Path
_hdr_logo = _Path(__file__).parent / "assets" / "50 Year logo JSA.png"
_hdr_logo_img = ""
if _hdr_logo.exists():
    _hdr_logo_uri = "data:image/png;base64," + _b64.b64encode(_hdr_logo.read_bytes()).decode()
    _hdr_logo_img = (f'<img src="{_hdr_logo_uri}" alt="John Stewart &amp; Associates" '
                     f'style="height:50px;display:block">')

st.markdown(f"""
<div style="display:flex;align-items:center;gap:18px;padding:8px 2px 12px;
     border-bottom:3px solid #0693e3;margin-bottom:12px">
  {_hdr_logo_img}
  <div style="line-height:1.15">
    <div style="font-size:10px;color:#0693e3;letter-spacing:.16em;text-transform:uppercase;
      font-weight:700">Commodity &amp; Ag Risk Management Specialists</div>
    <div style="font-size:24px;font-weight:800;color:#32373c;letter-spacing:-.02em">
      Cash Grain Basis Tracker</div>
  </div>
</div>
""", unsafe_allow_html=True)


# ══ Location-type trend stats (used by the Trends tab) ═══════════════════════
def _trend_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.min


def _trend_extract(snap, grain, mode="spot"):
    """Basis for grain at delivery `mode` ('spot' = nearest), with isSpot fallback."""
    if snap is None:
        return None
    if mode == "spot":
        cands = [r for r in snap.rows
                 if not r.isSpot and _grain_disp(r.grain) == grain
                 and r.basisCents is not None and r.futuresSymbol]
        if cands:
            return min(cands, key=lambda r: _dp.deliv_key(r.deliveryMonth, r.futuresSymbol)).basisCents
        row = next((r for r in snap.rows
                    if r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None), None)
        return row.basisCents if row else None
    matches = [r for r in snap.rows
               if not r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None
               and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode]
    if matches:
        return min(matches, key=lambda r: _dp.slot_key(r.deliveryMonth)).basisCents
    row = next((r for r in snap.rows
                if r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None
                and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode), None)
    return row.basisCents if row else None


def _trend_curve(snap, grain):
    if snap is None:
        return []
    seen: dict = {}
    for r in snap.rows:
        if r.isSpot or _grain_disp(r.grain) != grain or r.basisCents is None:
            continue
        sym = r.futuresSymbol or ""
        if len(sym) < 5 or not sym[-2:].isdigit():
            continue
        mon = _CME_MONTH_TO_INT.get(sym[-3])
        if not mon:
            continue
        key = (2000 + int(sym[-2:]), mon)
        if key not in seen:
            seen[key] = r.basisCents
    return sorted((y, m, b) for (y, m), b in seen.items())


def _trend_spot_gt_next(snap, grain):
    c = _trend_curve(snap, grain)
    return None if len(c) < 2 else c[0][2] > c[1][2]


def _trend_closest(snaps, target, maxd):
    if not snaps:
        return None
    b = min(snaps, key=lambda s: abs((_trend_ts(s.timestamp) - target).total_seconds()))
    return b if abs((_trend_ts(b.timestamp) - target).total_seconds()) / 86400 <= maxd else None


@st.cache_data(ttl=300, show_spinner=False)
def _trend_load(facility_type: str):
    """Cached snapshot load + anchor for a location type (shared across grain/period)."""
    from collections import Counter as _C
    sl    = get_bids_filter_data()
    pairs = [(l["provider"], l["location"]) for l in sl if l.get("facility_type") == facility_type]
    meta  = {(l["provider"], l["location"]): l for l in sl}
    data  = get_snapshots_bulk(pairs, since_days=400) if pairs else {}
    today_noon = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    loc_latest = []
    for snaps in data.values():
        ds = [d for s in snaps if (d := _trend_ts(s.timestamp)) <= today_noon]
        if ds:
            loc_latest.append(max(ds).date())
    now = (datetime(*_C(loc_latest).most_common(1)[0][0].timetuple()[:3], 12)
           if loc_latest else today_noon)
    return pairs, meta, data, now


def trend_periods(facility_type: str, grain: str) -> set:
    """Available canonical delivery periods (>= current month) for a type + grain."""
    _, _, data, _ = _trend_load(facility_type)
    today = datetime.utcnow().date()
    today_ym = (today.year, today.month)
    periods: set = set()
    for snaps in data.values():
        valid = [s for s in snaps if _trend_ts(s.timestamp).date() <= today]
        if not valid:
            continue
        latest = max(valid, key=lambda s: _trend_ts(s.timestamp))
        for r in latest.rows:
            if _grain_disp(r.grain) == grain and not r.isSpot:
                ym = _dp.canonical(r.deliveryMonth, r.futuresSymbol)
                if ym and ym >= today_ym:
                    periods.add(ym)
    return periods


def build_trend_rows(facility_type: str, grain: str, mode: str = "spot") -> list[dict]:
    """Per-location current/LW/LM/LY basis + spot>next, for a type + grain + delivery."""
    pairs, meta, data, now = _trend_load(facility_type)
    if not pairs:
        return []
    targets = {"current": (now, 1.6),
               "wk_ago":  (now - timedelta(days=7),   4),
               "mo_ago":  (now - timedelta(days=30),  4),
               "yr_ago":  (now - timedelta(days=365), 4)}
    rows = []
    for key in pairs:
        snaps = data.get(key, [])
        m     = meta.get(key, {})
        stt   = m.get("state", "")
        rd = {"region":  region_from_state(stt) or m.get("region", "") or "",
              "segment": river_segment(key[1])}
        for lbl, (tg, md) in targets.items():
            snap = _trend_closest(snaps, tg, md)
            rd[f"b_{lbl}"] = _trend_extract(snap, grain, mode)
            if lbl == "current":
                rd["spot_gt_next"] = _trend_spot_gt_next(snap, grain)
        rows.append(rd)
    return [r for r in rows if r.get("b_current") is not None]


def render_trend_cards(rows, group_field, groups) -> str:
    """
    Three stat tables for a category, mirroring the Summary panel:
      • Avg Basis Change (All / Firmer / Weaker)        — always (global)
      • River → Avg Basis & Change by Segment ; else → Firmer/Weaker by Region
      • Spot > Next by group (segment for river, region otherwise)
    """
    is_river = (group_field == "segment")
    grp_lbl  = "Segment" if is_river else "Region"
    WINS = [("wk_ago", "vs LW"), ("mo_ago", "vs LM"), ("yr_ago", "vs LY")]

    def _avg(xs):    return (sum(xs) / len(xs)) if xs else None
    def _grows(gv):  return [r for r in rows if (r.get(group_field) or "") == gv]
    def _moves(rs, win):
        return [r["b_current"] - r[f"b_{win}"] for r in rs
                if r.get("b_current") is not None and r.get(f"b_{win}") is not None]
    def _fc(v):  return "—" if v is None else f"{'+' if v >= 0 else '−'}{abs(v):.1f}"
    def _fp(v):  return "—" if v is None else f"{round(v)}%"

    TD   = ("font-family:'IBM Plex Mono',monospace;font-size:11px;padding:3px 10px;"
            "border-bottom:1px solid #f1f5f9;text-align:right;white-space:nowrap")
    TDL  = TD.replace("text-align:right", "text-align:left")
    TH   = ("font-family:'IBM Plex Mono',monospace;font-size:9px;font-weight:700;color:#94a3b8;"
            "text-transform:uppercase;letter-spacing:.06em;padding:4px 10px;"
            "border-bottom:2px solid #e2e8f0;text-align:right;white-space:nowrap")
    THL  = TH.replace("text-align:right", "text-align:left")
    CARD = "background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:4px 6px 6px 6px"
    TTL  = ("font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:800;color:#32373c;"
            "text-transform:uppercase;letter-spacing:.08em;padding:4px 10px 6px")

    def _col(txt, v):
        if v is None or v == 0:
            return f'<td style="{TD};color:#64748b">{txt}</td>'
        return f'<td style="{TD};color:{"#16a34a" if v > 0 else "#dc2626"};font-weight:700">{txt}</td>'

    def _hdr(title, extra_col=None):
        h = (f'<div style="{CARD}"><div style="{TTL}">{title}</div>'
             f'<table style="border-collapse:collapse;width:100%"><thead><tr><th style="{THL}"></th>')
        if extra_col:
            h += f'<th style="{TH}">{extra_col}</th>'
        for _, lab in WINS:
            h += f'<th style="{TH}">{lab}</th>'
        return h + '</tr></thead><tbody>'

    # ── Card A: Avg Basis Change — All / Firmer / Weaker (global) ──
    a = _hdr("Avg Basis Change (¢)")
    for grp, fn in (("All Plants",  lambda m: m),
                    ("Firmer only", lambda m: [x for x in m if x > 0]),
                    ("Weaker only", lambda m: [x for x in m if x < 0])):
        a += f'<tr><td style="{TDL};font-weight:700;color:#1e293b">{grp}</td>'
        for win, _ in WINS:
            v = _avg(fn(_moves(rows, win)))
            a += _col(_fc(v), v)
        a += '</tr>'
    a += '</tbody></table></div>'

    # ── Middle card ──
    if is_river:
        # Avg Basis & Change by Segment
        mid = _hdr("Avg Basis &amp; Change by Segment (¢)", extra_col="Avg Basis")
        for gv in groups:
            rs = _grows(gv)
            bl = _avg([r["b_current"] for r in rs if r.get("b_current") is not None])
            bt = "—" if bl is None else f"{'+' if bl >= 0 else '−'}{abs(bl):.1f}"
            mid += (f'<tr><td style="{TDL};font-weight:700;color:#1e293b">{gv}</td>'
                    f'<td style="{TD};color:#0f172a;font-weight:800">{bt}</td>')
            for win, _ in WINS:
                v = _avg(_moves(rs, win))
                mid += _col(_fc(v), v)
            mid += '</tr>'
        mid += '</tbody></table></div>'
    else:
        # Firmer / Weaker by Region
        mid = _hdr(f"Firmer / Weaker by {grp_lbl}")
        for gv in groups:
            rs = _grows(gv)
            for firmer, lab2 in ((True, "Firmer"), (False, "Weaker")):
                mid += (f'<tr><td style="{TDL};font-weight:700;color:#1e293b">'
                        f'{gv} <span style="color:#64748b;font-weight:400">{lab2}</span></td>')
                for win, _ in WINS:
                    ms = _moves(rs, win)
                    pv = None if not ms else 100 * sum(1 for m in ms if (m > 0 if firmer else m < 0)) / len(ms)
                    col = "#16a34a" if firmer else "#dc2626"
                    mid += (f'<td style="{TD};color:#cbd5e1">—</td>' if pv is None
                            else f'<td style="{TD};color:{col};font-weight:700">{_fp(pv)}</td>')
                mid += '</tr>'
        mid += '</tbody></table></div>'

    # ── Card C: Spot > Next by group ──
    c = (f'<div style="{CARD}"><div style="{TTL}">Spot &gt; Next</div>'
         f'<table style="border-collapse:collapse;width:100%"><thead><tr>'
         f'<th style="{THL}">{grp_lbl}</th><th style="{TH}">% Inverted</th></tr></thead><tbody>')
    for gv in groups:
        vs = [r.get("spot_gt_next") for r in _grows(gv) if r.get("spot_gt_next") is not None]
        iv = None if not vs else 100 * sum(1 for v in vs if v) / len(vs)
        c += (f'<tr><td style="{TDL};font-weight:700;color:#1e293b">{gv}</td>'
              f'<td style="{TD};color:#0f172a;font-weight:700">{_fp(iv)}</td></tr>')
    c += '</tbody></table></div>'

    _cols = "0.85fr 2fr 0.7fr" if is_river else "1.0fr 1.35fr 0.7fr"
    return (f'<div style="display:grid;grid-template-columns:{_cols};'
            f'gap:10px;margin:2px 0 18px 0">{a}{mid}{c}</div>')


# (heading, facility_type, grain, grouping) — corn categories first, then soy.
# Shared by the Changes and Trends tabs.
TREND_CATEGORIES = [
    ("Corn Processing — Corn",     "Corn Processing", "Corn",     "region"),
    ("Rail Terminals — Corn",      "Rail Terminal",   "Corn",     "region"),
    ("River Terminals — Corn",     "River Terminal",  "Corn",     "segment"),
    ("Soy Processing — Soybeans",  "Soy Processing",  "Soybeans", "region"),
    ("River Terminals — Soybeans", "River Terminal",  "Soybeans", "segment"),
]


def build_change_rows(facility_type: str, grain: str, mode: str = "spot") -> list[dict]:
    """Locations whose basis changed vs the prior posting. Returns
    [{provider, location, basis, change}], sorted firmest → weakest."""
    pairs, meta, data, now = _trend_load(facility_type)
    out = []
    for key in pairs:
        snaps    = data.get(key, [])
        cur_snap = _trend_closest(snaps, now, 1.6)
        if cur_snap is None:
            continue
        cur = _trend_extract(cur_snap, grain, mode)
        if cur is None:
            continue
        # prior = the snapshot immediately before the current one in the series
        ref_t = _trend_ts(cur_snap.timestamp)
        prior = None
        for s in snaps:
            t = _trend_ts(s.timestamp)
            if t < ref_t and (prior is None or t > _trend_ts(prior.timestamp)):
                prior = s
        prev = _trend_extract(prior, grain, mode)
        if prev is None:
            continue
        chg = cur - prev
        if chg == 0:
            continue
        out.append({"provider": key[0], "location": key[1], "basis": cur, "change": chg})
    out.sort(key=lambda r: (-r["change"], r["provider"], r["location"]))
    return out


def build_segment_change_rows(facility_type: str, grain: str, mode: str = "spot") -> list[dict]:
    """Per river-segment avg basis and avg day-over-day change (firmer = positive)."""
    pairs, meta, data, now = _trend_load(facility_type)
    by_seg: dict = {}
    for key in pairs:
        snaps    = data.get(key, [])
        cur_snap = _trend_closest(snaps, now, 1.6)
        if cur_snap is None:
            continue
        cur = _trend_extract(cur_snap, grain, mode)
        if cur is None:
            continue
        ref_t = _trend_ts(cur_snap.timestamp)
        prior = None
        for s in snaps:
            t = _trend_ts(s.timestamp)
            if t < ref_t and (prior is None or t > _trend_ts(prior.timestamp)):
                prior = s
        prev = _trend_extract(prior, grain, mode)
        chg  = (cur - prev) if prev is not None else None
        by_seg.setdefault(river_segment(key[1]), []).append((cur, chg))
    out = []
    for seg in SEGMENT_ORDER:
        vals = by_seg.get(seg)
        if not vals:
            continue
        curs = [c for c, _ in vals]
        chgs = [ch for _, ch in vals if ch is not None]
        out.append({"segment":    seg,
                    "avg_basis":  sum(curs) / len(curs),
                    "avg_change": (sum(chgs) / len(chgs)) if chgs else None,
                    "n":          len(curs)})
    return out


# ── JPSI brand (jpsi.com) ────────────────────────────────────────────────────
JPSI_DARK  = "#32373c"
JPSI_BLUE  = "#0693e3"
JPSI_LOGO  = "https://www.jpsi.com/wp-content/themes/gate39media/img/logo-white.png"
_GAIN, _LOSS = "#16a34a", "#dc2626"


def build_changes_email_html(mode: str = "spot") -> str:
    """A branded, email-ready HTML report of daily basis changes (JPSI styling)."""
    today = datetime.utcnow()
    _ff   = "font-family:Arial,Helvetica,sans-serif"
    _hdr  = ("font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;"
             "padding:3px 6px")

    body = ""
    for ttl, ft, gr, gmode in TREND_CATEGORIES:
        body += (f'<div style="margin:16px 0 5px;font-size:13px;font-weight:700;color:{JPSI_BLUE};'
                 f'border-bottom:2px solid {JPSI_BLUE};padding-bottom:3px">{ttl}</div>')
        if gmode == "segment":
            rows = build_segment_change_rows(ft, gr, mode)
            body += ('<table width="100%" style="border-collapse:collapse;font-size:12px">'
                     f'<tr><td style="{_hdr}">Segment</td>'
                     f'<td align="right" style="{_hdr}">Avg Basis</td>'
                     f'<td align="right" style="{_hdr}">Avg Δ Day</td></tr>')
            for i, r in enumerate(rows):
                bg = "#f4f9fd" if i % 2 else "#ffffff"
                ab, ch = r["avg_basis"], r["avg_change"]
                if ch is None or round(ch, 1) == 0:
                    chtxt = '<span style="color:#94a3b8">—</span>'
                else:
                    chtxt = f'<span style="color:{_GAIN if ch > 0 else _LOSS};font-weight:700">{ch:+.1f}</span>'
                body += (f'<tr style="background:{bg}">'
                         f'<td style="padding:3px 6px;color:{JPSI_DARK}">{r["segment"]}</td>'
                         f'<td align="right" style="padding:3px 6px;color:{JPSI_DARK};font-weight:600">{ab:+.1f}</td>'
                         f'<td align="right" style="padding:3px 6px">{chtxt}</td></tr>')
            body += '</table>'
        else:
            rows = build_change_rows(ft, gr, mode)
            if not rows:
                body += '<div style="font-size:12px;color:#94a3b8;padding:2px 6px">No changes today.</div>'
                continue
            body += ('<table width="100%" style="border-collapse:collapse;font-size:12px">'
                     f'<tr><td style="{_hdr}">Location</td>'
                     f'<td align="right" style="{_hdr}">Basis</td>'
                     f'<td align="right" style="{_hdr}">Δ Day</td></tr>')
            for i, r in enumerate(rows):
                bg  = "#f4f9fd" if i % 2 else "#ffffff"
                loc = adm_city_from_name(r["location"]) if r["provider"] == "ADM" else r["location"]
                c   = r["change"]
                body += (f'<tr style="background:{bg}">'
                         f'<td style="padding:3px 6px;color:{JPSI_DARK}">'
                         f'<b style="color:{JPSI_DARK}">{r["provider"]}</b> {loc}</td>'
                         f'<td align="right" style="padding:3px 6px;color:{JPSI_DARK};font-weight:600">{r["basis"]:+d}</td>'
                         f'<td align="right" style="padding:3px 6px;color:{_GAIN if c > 0 else _LOSS};'
                         f'font-weight:700">{c:+d}</td></tr>')
            body += '</table>'

    return (
        f'<div style="max-width:680px;margin:0;{_ff};border:1px solid #e2e8f0;'
        f'border-radius:8px;overflow:hidden">'
        # Header bar — dark with white logo + title/date
        f'<table width="100%" style="background:{JPSI_DARK};border-collapse:collapse"><tr>'
        f'<td style="padding:14px 18px"><img src="{JPSI_LOGO}" '
        f'alt="John Stewart &amp; Associates" height="30" style="display:block;height:30px"></td>'
        f'<td align="right" style="padding:14px 18px;color:#ffffff">'
        f'<div style="font-size:16px;font-weight:700">Daily Basis Changes</div>'
        f'<div style="font-size:12px;color:#cbd5e1">{today.day} {today.strftime("%b %Y")} '
        f'· spot basis vs prior posting</div></td></tr></table>'
        # Body
        f'<div style="padding:8px 18px 14px;background:#ffffff">{body}</div>'
        # Footer
        f'<div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:10px 18px;'
        f'font-size:11px;color:#64748b">John Stewart &amp; Associates · '
        f'Commodity &amp; Ag Risk Management Specialists · '
        f'<a href="https://www.jpsi.com" style="color:{JPSI_BLUE};text-decoration:none">jpsi.com</a></div>'
        f'</div>'
    )


tab_changes, tab_bids, tab_map, tab_summary, tab_trends = st.tabs(
    ["🔔 Changes", "📋 Bids", "🗺️ Map", "📊 Summary", "📈 Trends"])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB: CHANGES  (locations whose basis moved vs the prior posting)
# ═══════════════════════════════════════════════════════════════════════════════
with tab_changes:
    st.caption("Branded daily report — select it and copy into your email (paste keeps the formatting).")
    _email_html = build_changes_email_html()
    st.markdown(_email_html, unsafe_allow_html=True)
    with st.expander("HTML source (for email automation / HTML editors)"):
        st.code(_email_html, language="html")

with tab_bids:
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
                'Run <code style="color:#0693e3">python auto_import.py --chs-only</code> '
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
                'Run <code style="color:#0693e3">python auto_import.py --poet-only</code> '
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
                '<code style="color:#0693e3">python auto_import.py --adm-only</code>'
                '</div>',
                unsafe_allow_html=True,
            )
            st.stop()

        sel_adm_loc = st.selectbox(
            "ADM Location", options=adm_db_locs,
            key="adm_loc_select", label_visibility="collapsed",
        )
        loc_key   = sel_adm_loc
        loc_color = "#0693e3"   # blue for ADM
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
                '<code style="color:#0693e3">python auto_import.py --cgb-only</code>'
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
                '<code style="color:#0693e3">python auto_import.py --gpre-only</code>'
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
                '<code style="color:#0693e3">python auto_import.py --cargill-only</code>'
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
                '<code style="color:#0693e3">python auto_import.py --andersons-only</code>'
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
                '<code style="color:#0693e3">python auto_import.py --bunge-only</code>'
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
                '<code style="color:#0693e3">python auto_import.py --scoular-only</code>'
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
                '<code style="color:#0693e3">python auto_import.py --agp-only</code>'
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
                '<code style="color:#0693e3">python auto_import.py --ldc-only</code>'
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
        loc_color = "#0693e3"   # blue for LDC
        _ldc_snaps = _cached_get_snapshots("LDC", loc_key)
        if _ldc_snaps:
            grains = _build_grains(_ldc_snaps[-1].rows)
        else:
            grains = ["Corn"]



# ═══════════════════════════════════════════════════════════════════════════════
# TAB: BIDS
# ═══════════════════════════════════════════════════════════════════════════════
with tab_bids:
    # ── Load snapshots ────────────────────────────────────────────────────────
    snapshots = _cached_get_snapshots(provider, loc_key)

    # ── Commodity filter (populated from latest snapshot) ─────────────────────
    _grain_col, _ = st.columns([2, 8])
    with _grain_col:
        _avail_grains = _build_grains(snapshots[-1].rows) if snapshots else []
        grain = st.selectbox(
            "Commodity",
            _avail_grains if _avail_grains else ["—"],
            key="bids_flt_grain",
        )

    # ── No-data message ───────────────────────────────────────────────────────
    if not snapshots:
        _p = provider
        if _p == "POET":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --poet-only</code> to scrape this location, then refresh.'
        elif _p == "ADM":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --adm-only</code> or click <b>Scrape ADM now</b> in the sidebar, then refresh.'
        elif _p == "CGB":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --cgb-only</code> or click <b>Scrape CGB now</b> in the sidebar, then refresh.'
        elif _p == "CHS":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --chs-only</code> or click <b>Scrape CHS now</b> in the sidebar, then refresh.'
        elif _p == "Cargill":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --cargill-only</code>, then refresh.'
        elif _p == "GPRE":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --gpre-only</code>, then refresh.'
        elif _p == "Andersons":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --andersons-only</code>, then refresh.'
        elif _p == "Bunge":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --bunge-only</code>, then refresh.'
        elif _p == "Scoular":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --scoular-only</code>, then refresh.'
        elif _p == "AGP":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --agp-only</code>, then refresh.'
        elif _p == "LDC":
            hint = 'Run <code style="color:#0693e3">python auto_import.py --ldc-only</code>, then refresh.'
        else:
            hint = "Run the daily scraper to populate data for this location."
        st.markdown(
            f'<div style="color:#64748b;text-align:center;padding:40px;font-size:12px">'
            f'No snapshots yet for <b>{loc_key}</b>.<br><br>{hint}</div>',
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
            key=f"snap_pick_{loc_key}",
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
                f'<span style="color:#0693e3;font-weight:700">{latest_label}</span></span>',
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
            body_rows, spot_row, changes, spot_chg, loc_color, year_ago_label,
            is_meal=(grain == "Soybean Meal"),
        )
        st.markdown(table_html, unsafe_allow_html=True)

        # Roll adjustment legend
        roll_parts = " &nbsp;|&nbsp; ".join(
            f'<span style="color:#0693e3">{r["from"]}->{r["to"]}</span>'
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
            # Fall back to explicit spot row for historical snapshots that have no forward rows
            if _fr is None:
                _fr = next((r for r in _snap.rows
                            if r.isSpot and _grain_disp(r.spotGrain or r.grain) == grain), None)
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

            _df_spot = _pd.DataFrame(_spot_pts).sort_values("Date")
            _spot_color = loc_color

            # ── Time range selector ─────────────────────────────────────────
            _range_col, _ = st.columns([4, 6])
            with _range_col:
                _range_sel = st.radio(
                    "Range",
                    ["Full History", "1 Year", "1 Month"],
                    horizontal=True,
                    key=f"spot_range_{loc_key}_{grain}",
                    label_visibility="collapsed",
                )
            _now = _df_spot["Date"].max()
            if _range_sel == "1 Year":
                _df_view = _df_spot[_df_spot["Date"] >= _now - _pd.Timedelta(days=365)]
            elif _range_sel == "1 Month":
                _df_view = _df_spot[_df_spot["Date"] >= _now - _pd.Timedelta(days=30)]
            else:
                _df_view = _df_spot

            # ── Spot history line chart ─────────────────────────────────────
            _zero_rule = _alt.Chart(_pd.DataFrame({"y": [0]})).mark_rule(
                color="#94a3b8", strokeDash=[4, 4], strokeWidth=1
            ).encode(y="y:Q")
            _spot_line = (
                _alt.Chart(_df_view)
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

            # ── Seasonal chart ─────────────────────────────────────────────
            try:
                _df_seas = _df_spot[["Date", "Basis"]].copy()
                # Strip timezone for vectorized date arithmetic
                _d_naive = _df_seas["Date"].dt.tz_convert(None)
                _yr = _d_naive.dt.year
                _mo = _d_naive.dt.month
                _df_seas["MktYearNum"] = _yr.where(_mo >= 9, _yr - 1)
                _df_seas["MktYear"]    = _df_seas["MktYearNum"].apply(
                    lambda y: f"{y}/{str(y + 1)[-2:]}"
                )
                _sep1 = _pd.to_datetime(
                    _df_seas["MktYearNum"].astype(str) + "-09-01"
                )
                _df_seas["MktWeek"] = ((_d_naive - _sep1).dt.days // 7 + 1).clip(1, 52)
                _df_seas = (
                    _df_seas.groupby(["MktYear", "MktYearNum", "MktWeek"], as_index=False)
                    ["Basis"].mean()
                )
                _df_seas["Basis"] = _df_seas["Basis"].round(1)

                _max_yr  = int(_df_seas["MktYearNum"].max())
                _hist    = _df_seas[_df_seas["MktYearNum"] < _max_yr].copy()
                _curr    = _df_seas[_df_seas["MktYearNum"] == _max_yr].copy()
                _curr_yr = _curr["MktYear"].iloc[0] if not _curr.empty else ""

                _x_s = _alt.X("MktWeek:Q", title="Market Week",
                               scale=_alt.Scale(domain=[1, 52]),
                               axis=_alt.Axis(labelFontSize=10))
                _y_s = _alt.Y("Basis:Q", title="Spot Basis (¢)",
                               scale=_alt.Scale(zero=False),
                               axis=_alt.Axis(labelFontSize=10))
                _tip_s = [
                    _alt.Tooltip("MktYear:N", title="Mkt Year"),
                    _alt.Tooltip("MktWeek:Q", title="Week"),
                    _alt.Tooltip("Basis:Q",   title="Basis (¢)"),
                ]

                _SEAS_H = 560

                # Watermark logo — centered, 50% of chart height, 80% transparent
                import base64 as _b64, pathlib as _pl
                _logo_path = _pl.Path(__file__).parent / "assets" / "50 Year logo JSA.png"
                _s_wm = None
                if _logo_path.exists():
                    _logo_uri = (
                        "data:image/png;base64,"
                        + _b64.b64encode(_logo_path.read_bytes()).decode()
                    )
                    _wm_h = int(_SEAS_H * 0.50)   # 50 % of chart height = 280 px
                    _wm_w = int(_wm_h * 0.93)      # logo aspect ratio ≈ 0.93 : 1
                    _s_wm = (
                        _alt.Chart(_pd.DataFrame({
                            "MktWeek": [26.5],
                            "url":     [_logo_uri],
                        }))
                        .mark_image(
                            width=_wm_w, height=_wm_h,
                            opacity=0.20,
                            align="center",     # centres on x data coordinate
                            baseline="middle",  # centres on y pixel coordinate
                        )
                        .encode(
                            x=_alt.X("MktWeek:Q"),           # data coord → always week 26.5
                            y=_alt.value(_SEAS_H // 2),      # pixel coord → always vertical centre
                            url="url:N",
                        )
                    )

                # Zero reference line — uses same Basis field so y-axis resolves cleanly
                _s_zero = (
                    _alt.Chart(_pd.DataFrame({"MktWeek": [1, 52], "Basis": [0.0, 0.0]}))
                    .mark_line(color="#94a3b8", strokeDash=[4, 4], strokeWidth=1)
                    .encode(x=_alt.X("MktWeek:Q"), y=_alt.Y("Basis:Q"))
                )
                _s_curr = (
                    _alt.Chart(_curr)
                    .mark_line(strokeWidth=3, color="#000000")
                    .encode(x=_x_s, y=_y_s, tooltip=_tip_s)
                )
                _s_curr_end = (
                    _alt.Chart(_curr.nlargest(1, "MktWeek") if not _curr.empty else _curr)
                    .mark_text(align="left", dx=6, fontSize=10, fontWeight="bold",
                               color="#000000")
                    .encode(x=_alt.X("MktWeek:Q"), y=_alt.Y("Basis:Q"), text="MktYear:N")
                )

                _s_layers = ([_s_wm] if _s_wm else []) + [_s_zero, _s_curr, _s_curr_end]

                if not _hist.empty:
                    _s_hist = (
                        _alt.Chart(_hist)
                        .mark_line(strokeWidth=2, opacity=0.9)
                        .encode(
                            x=_x_s, y=_y_s,
                            color=_alt.Color(
                                "MktYear:N",
                                sort=sorted(_hist["MktYear"].unique()),
                                scale=_alt.Scale(scheme="tableau10"),
                                legend=_alt.Legend(
                                    title="Mkt Year", orient="bottom",
                                    columns=6, labelFontSize=10, titleFontSize=10,
                                ),
                            ),
                            tooltip=_tip_s,
                        )
                    )
                    _s_layers.insert(2, _s_hist)

                if grain in ("Corn", "Soybeans"):
                    _fut = _pd.DataFrame([
                        {"MktWeek": 13, "code": "Z"},
                        {"MktWeek": 27, "code": "H"},
                        {"MktWeek": 35, "code": "K"},
                        {"MktWeek": 44, "code": "N"},
                    ])
                    _s_vlines = (
                        _alt.Chart(_fut).mark_rule(color="#cbd5e1", strokeWidth=1.5)
                        .encode(x="MktWeek:Q")
                    )
                    # Labels centered on the line, pinned to the top of the plot area
                    _s_vlbls = (
                        _alt.Chart(_fut)
                        .mark_text(fontSize=12, color="#94a3b8", fontWeight="bold",
                                   align="center", baseline="top")
                        .encode(x=_alt.X("MktWeek:Q"), y=_alt.value(6), text="code:N")
                    )
                    _s_layers = [_s_vlines, _s_vlbls] + _s_layers

                st.markdown(
                    '<div style="margin-top:24px;margin-bottom:4px;font-size:10px;'
                    'color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.1em">'
                    'Seasonal Basis — Marketing Year (Sep–Aug)'
                    + (f'&nbsp;&nbsp;<span style="color:#1e293b;font-weight:900">'
                       f'{_curr_yr} = black</span>' if _curr_yr else '')
                    + '</div>',
                    unsafe_allow_html=True,
                )
                st.altair_chart(
                    _alt.layer(*_s_layers).properties(height=_SEAS_H),
                    use_container_width=True,
                )

            except Exception as _seas_err:
                st.warning(f"Seasonal chart error: {_seas_err}")

        elif len(_spot_pts) == 1:
            st.caption("Only 1 snapshot — scrape more data to see spot history chart.")

    # ── Snapshot history ──────────────────────────────────────────────────────
    if snapshots:
        with st.expander(f"Snapshot history — {loc_key} ({len(snapshots)} records)", expanded=False):
            for snap in reversed(snapshots):
                is_latest  = snap is snapshots[-1]
                is_viewing = snap is viewing
                d_label    = datetime.fromisoformat(
                    snap.timestamp.replace("Z", "+00:00")).strftime("%b %d '%y")
                src_icon    = " [email]" if snap.source == "email" else ""
                badge_color = loc_color if is_viewing else "#e2e8f0"
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

    # Pins are colored by Location Type (facility_type).
    _FTYPE_COLORS = {
        "Country Elevator":   [59,  130, 246],
        "River Terminal":     [6,   182, 212],
        "Soy Processing":     [34,  197, 94],
        "Corn Processing":    [249, 115, 22],
        "Rail Terminal":      [139, 92,  246],
        "Feed Mill":          [234, 179, 8],
        "Wheat Milling":      [239, 68,  68],
        "Export Terminal":    [244, 63,  94],
        "Container Terminal": [100, 116, 139],
        "Ethanol":            [217, 70,  239],
    }
    _DEFAULT_COLOR = [148, 163, 184]

    def _map_base_commodity(g: str) -> str:
        return "Wheat" if g.startswith("Wheat") else g

    map_rows = _cached_get_map_data()

    if not map_rows:
        st.markdown(
            '<div style="color:#64748b;text-align:center;padding:60px;font-size:12px">'
            'No geocoded locations yet.<br><br>'
            'Run <code style="color:#0693e3">python geocode_locations.py</code> '
            'to populate coordinates, then refresh.</div>',
            unsafe_allow_html=True,
        )
    else:
        # ── Filters ───────────────────────────────────────────────────────────
        all_ftypes_map = sorted({r["facility_type"] for r in map_rows if r.get("facility_type")})
        all_states_map = sorted({r["state"] for r in map_rows if r.get("state")})

        fc1, fc2 = st.columns(2)
        with fc1:
            sel_ftypes = st.multiselect(
                "Location Type", options=all_ftypes_map, default=[],
                placeholder="All types", key="map_ftype_filter")
        with fc2:
            sel_states = st.multiselect(
                "State", options=all_states_map, default=[],
                placeholder="All states", key="map_state_filter")

        pre_filtered = [
            r for r in map_rows
            if (not sel_ftypes or r.get("facility_type") in sel_ftypes)
            and (not sel_states or r.get("state") in sel_states)
        ]

        # ── Tooltip basis controls: commodity + delivery month ────────────────
        avail_comm = sorted({_map_base_commodity(b["grain"])
                             for r in pre_filtered for b in r["bids"]})
        gc1, gc2 = st.columns(2)
        with gc1:
            if st.session_state.get("map_commodity") not in avail_comm:
                st.session_state["map_commodity"] = ("Corn" if "Corn" in avail_comm
                                                     else (avail_comm[0] if avail_comm else "Corn"))
            sel_commodity = st.selectbox("Commodity (tooltip)", avail_comm or ["—"],
                                         key="map_commodity")

        # Delivery-month options from the bids for the selected commodity
        # (hide already-past delivery months).
        _map_today_ym = (datetime.utcnow().year, datetime.utcnow().month)
        _map_periods = set()
        for r in pre_filtered:
            for b in r["bids"]:
                if _map_base_commodity(b["grain"]) == sel_commodity:
                    ym = _dp.canonical(b["delivery_month"], b["futures_symbol"])
                    if ym and ym >= _map_today_ym:
                        _map_periods.add(ym)
        _deliv_opts = ["Spot (Front Month)"] + [_dp.label(p) for p in sorted(_map_periods)]
        with gc2:
            if st.session_state.get("map_deliv") not in _deliv_opts:
                st.session_state["map_deliv"] = "Spot (Front Month)"
            sel_deliv = st.selectbox("Delivery Month (tooltip)", _deliv_opts, key="map_deliv")

        # Bid at a location for the selected commodity + delivery month.
        def _map_loc_bid(r):
            bids = [b for b in r["bids"] if _map_base_commodity(b["grain"]) == sel_commodity]
            if not bids:
                return None
            if sel_deliv.startswith("Spot"):
                return min(bids, key=lambda x: _dp.deliv_key(x["delivery_month"], x["futures_symbol"]))
            matches = [b for b in bids
                       if _dp.label(_dp.canonical(b["delivery_month"], b["futures_symbol"])) == sel_deliv]
            if matches:
                return min(matches, key=lambda x: _dp.slot_key(x["delivery_month"]))
            return None

        # Pins: locations offering the selected commodity
        filtered = [r for r in pre_filtered
                    if any(_map_base_commodity(b["grain"]) == sel_commodity for b in r["bids"])]

        def _fmt_basis(cents):
            return "—" if cents is None else f"{'+' if cents >= 0 else ''}{cents}c"

        def _tooltip_text(row):
            bid = _map_loc_bid(row)
            if not bid:
                return f"{row['location']}  |  —"
            fut = short_sym(bid["futures_symbol"]) if bid["futures_symbol"] else ""
            fut = f" ({fut})" if fut else ""
            return f"{row['location']}  |  {_fmt_basis(bid['basis'])}{fut}"

        df = pd.DataFrame([
            {
                "lat":     r["lat"],
                "lon":     r["lon"],
                "tooltip": _tooltip_text(r),
                "color":   _FTYPE_COLORS.get(r.get("facility_type"), _DEFAULT_COLOR),
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
        view = pdk.ViewState(latitude=39.5, longitude=-98.35, zoom=3.8, pitch=0)
        deck = pdk.Deck(
            layers=[layer],
            initial_view_state=view,
            tooltip={"text": "{tooltip}"},
            map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        )
        st.pydeck_chart(deck, use_container_width=True)

        # ── Legend by Location Type (only types present in the view) ──────────
        _ftypes_shown = sorted({r["facility_type"] for r in filtered if r.get("facility_type")})
        legend_parts = []
        for ft in _ftypes_shown:
            c = _FTYPE_COLORS.get(ft, _DEFAULT_COLOR)
            hex_c = "#{:02x}{:02x}{:02x}".format(*c)
            legend_parts.append(
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'margin-right:14px;font-size:11px;color:#374151">'
                f'<span style="width:10px;height:10px;border-radius:50%;'
                f'background:{hex_c};display:inline-block"></span>{ft}</span>'
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
    import holidays as _hol

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
        """
        Return (basis_cents, futures_symbol) from a snapshot.
        mode = 'spot' (nearest delivery) or a canonical delivery period like
        'Jun 2026'. When a month is split (FH/LH), the nearest slot is used.
        """
        if snap is None:
            return None, None

        if mode == "spot":
            # Nearest delivery slot among live forward bids.
            cands = [r for r in snap.rows
                     if not r.isSpot and _grain_disp(r.grain) == grain
                     and r.basisCents is not None and r.futuresSymbol]
            if cands:
                row = min(cands, key=lambda r: _dp.deliv_key(r.deliveryMonth, r.futuresSymbol))
                return row.basisCents, row.futuresSymbol
            # Historical imports store the spot bid as isSpot=True.
            row = next((r for r in snap.rows
                        if r.isSpot and _grain_disp(r.grain) == grain
                        and r.basisCents is not None), None)
            return (row.basisCents, row.futuresSymbol) if row else (None, None)

        # Specific delivery period — match the normalized window, nearest slot.
        matches = [r for r in snap.rows
                   if not r.isSpot and _grain_disp(r.grain) == grain
                   and r.basisCents is not None
                   and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode]
        if matches:
            row = min(matches, key=lambda r: _dp.slot_key(r.deliveryMonth))
            return row.basisCents, row.futuresSymbol
        # Historical isSpot fallback whose normalized period matches.
        row = next((r for r in snap.rows
                    if r.isSpot and _grain_disp(r.grain) == grain
                    and r.basisCents is not None
                    and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode), None)
        return (row.basisCents, row.futuresSymbol) if row else (None, None)

    def _prior_trading_day(ref: datetime, n: int = 1) -> datetime:
        """Return noon UTC on the nth prior US trading day (Mon–Fri, non-US-holiday) before ref."""
        _us_hol = _hol.US(years=[ref.year, ref.year - 1])
        d = ref.date() - timedelta(days=1)
        count = 0
        while True:
            if d.weekday() < 5 and d not in _us_hol:
                count += 1
                if count >= n:
                    return datetime(d.year, d.month, d.day, 12, 0, 0)
            d -= timedelta(days=1)

    def _prior_move(snaps, ref_snap, grain, mode):
        """
        Basis move of ref_snap vs the scrape day immediately before it (same
        location series). Returns delta cents (signed), or None if no prior /
        no comparable bid. Used to flag cells where the bid changed recently.
        """
        if ref_snap is None:
            return None
        ref_b, _ = _sum_extract(ref_snap, grain, mode)
        if ref_b is None:
            return None
        ref_t = _sum_ts(ref_snap.timestamp)
        prior = None
        for s in snaps:
            t = _sum_ts(s.timestamp)
            if t < ref_t and (prior is None or t > _sum_ts(prior.timestamp)):
                prior = s
        prior_b, _ = _sum_extract(prior, grain, mode)
        if prior_b is None:
            return None
        return ref_b - prior_b

    # Columns that get day-over-day cell highlighting (Current area + Last Week)
    _HILITE_COLS = {"wk_ago", "d2_ago", "d1_ago", "current"}

    def _forward_curve(snap, grain: str):
        """Sorted [(year, month, basis), …] of forward bids for grain (one per
        contract month, nearest delivery kept). Used for spot-vs-next stats."""
        if snap is None:
            return []
        seen: dict = {}
        for r in snap.rows:
            if r.isSpot or _grain_disp(r.grain) != grain or r.basisCents is None:
                continue
            sym = r.futuresSymbol or ""
            if len(sym) < 5 or not sym[-2:].isdigit():
                continue
            mon = _CME_MONTH_TO_INT.get(sym[-3])
            if not mon:
                continue
            key = (2000 + int(sym[-2:]), mon)
            if key not in seen:          # rows listed nearest-first → keep first
                seen[key] = r.basisCents
        return sorted((y, m, b) for (y, m), b in seen.items())

    def _spot_gt_next(snap, grain: str):
        """True if the spot (front) month basis is higher than the next month's.
        None when fewer than two forward months are available."""
        curve = _forward_curve(snap, grain)
        if len(curve) < 2:
            return None
        return curve[0][2] > curve[1][2]

    # ── Filters row ───────────────────────────────────────────────────────────
    _sl = _cached_get_bids_filter_data()  # [{provider, location, state, facility_type, region}]
    _sfac_types = sorted({l["facility_type"] for l in _sl if l["facility_type"]})
    _sgrains = ["Soybeans", "Corn", "Wheat", "Soybean Meal", "Soybean Oil"]

    # Default the Grain to whichever commodity has the most bids for the selected
    # Location Type(s). Snaps on first load and whenever Location Type changes;
    # a manual Grain choice still sticks until the type is changed again.
    def _majority_grain(selected_ftypes) -> "str | None":
        from collections import Counter as _C
        cnt = _C()
        for ft, graw, n in _cached_grain_counts_by_facility():
            if selected_ftypes and ft not in selected_ftypes:
                continue
            disp = _grain_disp(graw)
            opt  = next((o for o in _sgrains
                         if disp == o or (disp or "").startswith(o + " ")), None)
            if opt:
                cnt[opt] += n
        return cnt.most_common(1)[0][0] if cnt else None

    if "sum_ftype" not in st.session_state:
        st.session_state["sum_ftype"] = (["Soy Processing"]
                                         if "Soy Processing" in _sfac_types else [])
    _cur_ft = tuple(st.session_state.get("sum_ftype", []))
    if _cur_ft != st.session_state.get("_sum_prev_ftype"):
        _maj = _majority_grain(_cur_ft)
        if _maj:
            st.session_state["sum_grain"] = _maj
        st.session_state["_sum_prev_ftype"] = _cur_ft

    _sf1, _sf2, _sf3 = st.columns([2, 2, 2])
    with _sf1:
        _ssel_types = st.multiselect("Location Type", _sfac_types, key="sum_ftype")
    with _sf2:
        _slocs_by_t = [l for l in _sl if not _ssel_types or l["facility_type"] in _ssel_types]
        _sstates    = sorted({l["state"] for l in _slocs_by_t if l["state"]})
        _ssel_states = st.multiselect("State", _sstates, key="sum_state")
    with _sf3:
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

        # ── Delivery period options (physical delivery month, not futures) ─────
        _today_ym = (datetime.utcnow().year, datetime.utcnow().month)
        _periods: set = set()
        for key, snaps in _sdata.items():
            # latest snapshot on or before today (skip stray future-dated rows)
            _valid = [s for s in snaps if _sum_ts(s.timestamp).date() <= datetime.utcnow().date()]
            if not _valid:
                continue
            latest = max(_valid, key=lambda s: _sum_ts(s.timestamp))
            for r in latest.rows:
                if _grain_disp(r.grain) == _sgrain and not r.isSpot:
                    ym = _dp.canonical(r.deliveryMonth, r.futuresSymbol)
                    if ym and ym >= _today_ym:   # hide already-past delivery months
                        _periods.add(ym)

        _deliv_opts = ["Spot (Front Month)"] + [_dp.label(ym) for ym in sorted(_periods)]
        if st.session_state.get("sum_delivery") not in _deliv_opts:
            st.session_state["sum_delivery"] = "Spot (Front Month)"
        _sel_deliv  = st.selectbox("Delivery Period", _deliv_opts, key="sum_delivery")
        _smode      = "spot" if _sel_deliv.startswith("Spot") else _sel_deliv

        # ── Anchor "Today" to the latest scrape date, not the calendar date ────
        # Until the day's scrape runs, the most recent data is the prior trading
        # day — so "Today" should stay on that date and only shift up once new
        # data lands. We anchor on the MOST COMMON latest snapshot date across
        # the displayed locations (stable through a partial mid-run state).
        _today_noon = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
        _loc_latest = []
        for _snaps in _sdata.values():
            _ds = [d for s in _snaps if (d := _sum_ts(s.timestamp)) <= _today_noon]
            if _ds:
                _loc_latest.append(max(_ds).date())
        if _loc_latest:
            _anchor_date = _Counter(_loc_latest).most_common(1)[0][0]
            _now = datetime(_anchor_date.year, _anchor_date.month, _anchor_date.day, 12)
        else:
            _now = _today_noon

        # d1_ago / d2_ago: skip back to the 1st / 2nd prior trading day so
        # weekends and US holidays never produce blank cells.
        _TARGETS = [
            ("yr_ago",  _now - timedelta(days=365),  4),   # ±4 days of exact year-ago date
            ("mo_ago",  _now - timedelta(days=30),   4),   # ±4 days of exact month-ago date
            ("wk_ago",  _now - timedelta(days=7),    4),   # ±4 days of exact week-ago date
            # 0.6 = within ~14 hours of target noon; matches same-day midnight
            # snapshots (12 h away) but not next/prior day midnight (36 h away).
            ("d2_ago",  _prior_trading_day(_now, 2), 0.6), # 2nd most-recent trading day
            ("d1_ago",  _prior_trading_day(_now, 1), 0.6), # most-recent trading day
            # anchored on the latest scrape date, so this matches that date exactly;
            # 1.6 still lets locations lagging by a day show their latest bid.
            ("current", _now,                        1.6),
        ]

        # ── Build one data row per location ───────────────────────────────────
        _smeta = {(l["provider"], l["location"]): l for l in _sfilt}
        _srows = []
        for key in _spairs:
            snaps  = _sdata.get(key, [])
            meta   = _smeta.get(key, {})
            # Region by the Mississippi River divide (derived from state).
            # Falls back to any stored region only when the state is unknown.
            _st_code = meta.get("state", "")
            rd: dict = {
                "provider": key[0],
                "location": key[1],
                "state":    _st_code,
                "region":   region_from_state(_st_code) or meta.get("region", "") or "",
                "segment":  river_segment(key[1]),
                "lat":      meta.get("lat"),
            }
            for lbl, tgt, max_d in _TARGETS:
                snap = _sum_closest(snaps, tgt, max_d)
                basis, sym = _sum_extract(snap, _sgrain, _smode)
                rd[f"b_{lbl}"] = basis
                rd[f"s_{lbl}"] = sym
                rd[f"d_{lbl}"] = _sum_ts(snap.timestamp).date() if snap else None
                # Day-over-day move (Current area + Last Week) for cell highlighting
                rd[f"m_{lbl}"] = (_prior_move(snaps, snap, _sgrain, _smode)
                                  if lbl in _HILITE_COLS else None)
                # Forward-curve shape from the current snapshot (spot vs next month)
                if lbl == "current":
                    rd["spot_gt_next"] = _spot_gt_next(snap, _sgrain)
            _srows.append(rd)

        # Keep only rows with current data
        _srows = [r for r in _srows if r["b_current"] is not None]

        # River terminals get their own segmentation (by waterway area); every
        # other location type groups East/West by the Mississippi divide.
        _river_view = (set(_ssel_types) == {"River Terminal"})
        _grp_field  = "segment" if _river_view else "region"
        if _river_view:
            _seg_rank = {s: i for i, s in enumerate(SEGMENT_ORDER)}
            # Within a segment, order furthest-north → furthest-south by latitude
            # (best-effort; rows with no/bad coords fall to the end).
            def _ns(r):
                lat = r.get("lat")
                return -lat if isinstance(lat, (int, float)) else 999
            _srows.sort(key=lambda r: (_seg_rank.get(r.get("segment"), 99), _ns(r), r["location"]))
        else:
            # Sort: region (empty last) → state → location, so locations cluster by
            # state within each East/West section for easy cross-company comparison.
            _srows.sort(key=lambda r: (r["region"] or "zzz", r["state"] or "zzz", r["location"]))

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

            # ── Per-column reference option month (majority symbol in column) ──
            # _csyms_raw → the actual majority contract per column (for badge compare)
            # _csyms     → its short display form for the header row
            def _col_ref_raw(lbl: str) -> str:
                syms = [r[f"s_{lbl}"] for r in _srows if r.get(f"s_{lbl}")]
                if not syms:
                    return ""
                return _Counter(syms).most_common(1)[0][0]

            _csyms_raw = {lbl: _col_ref_raw(lbl) for lbl, _, _ in _TARGETS}
            _csyms     = {lbl: (short_sym(s) if s else "—") for lbl, s in _csyms_raw.items()}

            # ── Reference info bar ────────────────────────────────────────────
            st.markdown(
                f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:11px;'
                f'color:#64748b;padding:6px 0 10px 0">'
                f'Reference: <span style="color:#0693e3;font-weight:700">{_ref_disp}</span>'
                f'&nbsp;&nbsp;·&nbsp;&nbsp;'
                f'Grain: <span style="font-weight:700;color:#0f172a">{_sgrain}</span>'
                f'&nbsp;&nbsp;·&nbsp;&nbsp;'
                f'As of: <span style="font-weight:700;color:#0f172a">'
                f'{_now.day} {_now.strftime("%b")}</span>'
                f'&nbsp;&nbsp;·&nbsp;&nbsp;'
                f'{len(_srows)} locations'
                f'</div>',
                unsafe_allow_html=True,
            )

            # ══ Summary maps (state-level choropleth) ═════════════════════════
            # Two US maps, each independently toggled between current value and
            # change vs LW / LM / LY, colored by the state-level basis index for
            # the selected location type + grain.
            import altair as _alt
            import pandas as _pd

            _FIPS = {
                "AL":1,"AK":2,"AZ":4,"AR":5,"CA":6,"CO":8,"CT":9,"DE":10,"FL":12,"GA":13,
                "HI":15,"ID":16,"IL":17,"IN":18,"IA":19,"KS":20,"KY":21,"LA":22,"ME":23,
                "MD":24,"MA":25,"MI":26,"MN":27,"MS":28,"MO":29,"MT":30,"NE":31,"NV":32,
                "NH":33,"NJ":34,"NM":35,"NY":36,"NC":37,"ND":38,"OH":39,"OK":40,"OR":41,
                "PA":42,"RI":44,"SC":45,"SD":46,"TN":47,"TX":48,"UT":49,"VT":50,"VA":51,
                "WA":53,"WV":54,"WI":55,"WY":56,
            }

            def _mavg(xs):
                return (sum(xs) / len(xs)) if xs else None

            _MAP_METRICS = {
                "Current Value": lambda rs: _mavg([r["b_current"] for r in rs
                                                   if r.get("b_current") is not None]),
                "Change vs LW":  lambda rs: _mavg([r["b_current"] - r["b_wk_ago"] for r in rs
                                                   if r.get("b_current") is not None and r.get("b_wk_ago") is not None]),
                "Change vs LM":  lambda rs: _mavg([r["b_current"] - r["b_mo_ago"] for r in rs
                                                   if r.get("b_current") is not None and r.get("b_mo_ago") is not None]),
                "Change vs LY":  lambda rs: _mavg([r["b_current"] - r["b_yr_ago"] for r in rs
                                                   if r.get("b_current") is not None and r.get("b_yr_ago") is not None]),
            }

            # Bucket rows by US state (skip non-US / unknown).
            _map_by_state: dict = {}
            for r in _srows:
                stt = r.get("state")
                if stt in _FIPS:
                    _map_by_state.setdefault(stt, []).append(r)

            _us_states = _alt.topo_feature(
                "https://cdn.jsdelivr.net/npm/vega-datasets@2/data/us-10m.json", "states")

            # Focus the map on the grain belt (the projection auto-fits to these
            # states). Approx label centroids (lon, lat) for each.
            _CENTROID = {
                "ND": (-100.5, 47.4), "SD": (-100.2, 44.4), "NE": (-99.8, 41.5),
                "KS": (-98.4, 38.5),  "OK": (-97.5, 35.6),  "MN": (-94.3, 46.3),
                "IA": (-93.5, 42.0),  "MO": (-92.5, 38.4),  "AR": (-92.4, 34.8),
                "LA": (-92.0, 31.1),  "WI": (-90.0, 44.6),  "IL": (-89.2, 40.0),
                "IN": (-86.3, 39.9),  "OH": (-82.8, 40.2),  "MI": (-84.6, 43.3),
                "KY": (-85.3, 37.5),  "TN": (-86.4, 35.8),  "MS": (-89.7, 32.7),
                "AL": (-86.8, 32.8),  "GA": (-83.5, 32.7),
            }
            _FOCUS_FIPS = [_FIPS[s] for s in _CENTROID]
            _FOCUS_EXPR = f"indexof({_FOCUS_FIPS}, datum.id) != -1"

            def _make_choropleth(metric: str):
                fn = _MAP_METRICS[metric]
                recs = []
                for stt, rs in _map_by_state.items():
                    if stt not in _CENTROID:
                        continue
                    v = fn(rs)
                    if v is not None:
                        lon, lat = _CENTROID[stt]
                        recs.append({"id": _FIPS[stt], "state": stt, "value": round(v, 1),
                                     "n": len(rs), "lon": lon, "lat": lat,
                                     "lbl": f"{'+' if v >= 0 else '−'}{abs(round(v))}"})
                if not recs:
                    return None
                df = _pd.DataFrame(recs)
                _m = max(abs(df["value"].min()), abs(df["value"].max()), 1)
                base = _alt.Chart(_us_states).transform_filter(_FOCUS_EXPR)
                bg = base.mark_geoshape(fill="#f1f5f9", stroke="#ffffff", strokeWidth=0.6)
                fg = (
                    base.mark_geoshape(stroke="#ffffff", strokeWidth=0.6)
                    .transform_lookup(lookup="id",
                                      from_=_alt.LookupData(df, "id", ["state", "value", "n"]))
                    .transform_filter("isValid(datum.value)")
                    .encode(
                        color=_alt.Color("value:Q",
                                         scale=_alt.Scale(scheme="redyellowgreen",
                                                          domain=[-_m, _m]),
                                         legend=_alt.Legend(title=f"{metric} (¢)", orient="bottom")),
                        tooltip=[_alt.Tooltip("state:N", title="State"),
                                 _alt.Tooltip("value:Q", title=metric),
                                 _alt.Tooltip("n:Q", title="Locations")],
                    )
                )
                labels = (
                    _alt.Chart(df).mark_text(fontSize=14, fontWeight="bold", color="#0f172a")
                    .encode(longitude="lon:Q", latitude="lat:Q", text="lbl:N")
                )
                return (bg + fg + labels).project(type="albersUsa").properties(height=460)

            _map_opts = list(_MAP_METRICS)
            _mc1, _mc2 = st.columns(2)
            for _col, _key, _idx in ((_mc1, "sum_map_left", 0), (_mc2, "sum_map_right", 1)):
                with _col:
                    _met = st.selectbox("Map metric", _map_opts, index=_idx, key=_key,
                                        label_visibility="collapsed")
                    _ch = _make_choropleth(_met)
                    if _ch is not None:
                        st.altair_chart(_ch, use_container_width=True)
                    else:
                        st.caption("No state-level data for this metric.")

            # ══ Statistics panel ══════════════════════════════════════════════
            # Summarizes basis moves across all displayed plants. Move vs a
            # window = b_current − b_window (firmer = positive, weaker = negative).
            _WINS = [("wk_ago", "vs LW"), ("mo_ago", "vs LM"), ("yr_ago", "vs LY")]

            def _win_moves(rows, win):
                out = []
                for r in rows:
                    bc, bw = r.get("b_current"), r.get(f"b_{win}")
                    if bc is not None and bw is not None:
                        out.append(bc - bw)
                return out

            def _avg(xs):
                return (sum(xs) / len(xs)) if xs else None

            def _fc(v):  # signed cents, 1 decimal
                if v is None:
                    return "—"
                s = "+" if v >= 0 else "−"
                return f"{s}{abs(v):.1f}"

            def _fp(v):  # percent, 0 decimals
                return "—" if v is None else f"{round(v)}%"

            # Section A — average basis change (All / Firmer / Weaker)
            _avg_rows = []
            for grp, fn in (
                ("All Plants",  lambda ms: ms),
                ("Firmer only", lambda ms: [m for m in ms if m > 0]),
                ("Weaker only", lambda ms: [m for m in ms if m < 0]),
            ):
                vals = [( _avg(fn(_win_moves(_srows, w))) ) for w, _ in _WINS]
                _avg_rows.append((grp, vals))

            # Per-group stats — by river segment in river view, else by region.
            def _grp_moves(gv, win):
                return _win_moves([r for r in _srows if (r.get(_grp_field) or "") == gv], win)

            def _grp_avg(gv, win):
                return _avg(_grp_moves(gv, win))

            def _grp_pct(gv, win, want_firmer):
                ms = _grp_moves(gv, win)
                if not ms:
                    return None
                cnt = sum(1 for m in ms if (m > 0 if want_firmer else m < 0))
                return 100 * cnt / len(ms)

            def _grp_inverse(gv):
                vals = [r.get("spot_gt_next") for r in _srows
                        if (r.get(_grp_field) or "") == gv and r.get("spot_gt_next") is not None]
                if not vals:
                    return None
                return 100 * sum(1 for v in vals if v) / len(vals)

            def _grp_avg_basis(gv):
                vals = [r["b_current"] for r in _srows
                        if (r.get(_grp_field) or "") == gv and r.get("b_current") is not None]
                return (sum(vals) / len(vals)) if vals else None

            if _river_view:
                _grp_title = "Segment"
                _groups = [s for s in SEGMENT_ORDER
                           if any((r.get("segment") or "") == s for r in _srows)]
            else:
                _grp_title = "Region"
                _groups = [g for g in ("East", "West")
                           if any((r.get("region") or "") == g for r in _srows)]

            # ── Render statistics panel ───────────────────────────────────────
            _SC_TD  = ("font-family:'IBM Plex Mono',monospace;font-size:11px;"
                       "padding:3px 10px;border-bottom:1px solid #f1f5f9;text-align:right;white-space:nowrap")
            _SC_TDL = _SC_TD.replace("text-align:right", "text-align:left")
            _SC_TH  = ("font-family:'IBM Plex Mono',monospace;font-size:9px;font-weight:700;"
                       "color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;"
                       "padding:4px 10px;border-bottom:2px solid #e2e8f0;text-align:right;white-space:nowrap")
            _SC_THL = _SC_TH.replace("text-align:right", "text-align:left")
            _SC_CARD = ("background:#fff;border:1px solid #e2e8f0;border-radius:6px;"
                        "padding:4px 6px 6px 6px")
            _SC_TITLE = ("font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:800;"
                         "color:#32373c;text-transform:uppercase;letter-spacing:.08em;padding:4px 10px 6px")

            def _colored(txt, v, good_pos=True):
                if v is None or v == 0:
                    return f'<td style="{_SC_TD};color:#64748b">{txt}</td>'
                pos = v > 0
                green = pos if good_pos else (not pos)
                col = "#16a34a" if green else "#dc2626"
                return f'<td style="{_SC_TD};color:{col};font-weight:700">{txt}</td>'

            # Card A: Avg basis change
            _hA = (f'<div style="{_SC_CARD}"><div style="{_SC_TITLE}">Avg Basis Change (¢)</div>'
                   f'<table style="border-collapse:collapse;width:100%"><thead><tr>'
                   f'<th style="{_SC_THL}"></th>')
            for _, lab in _WINS:
                _hA += f'<th style="{_SC_TH}">{lab}</th>'
            _hA += '</tr></thead><tbody>'
            for grp, vals in _avg_rows:
                _hA += f'<tr><td style="{_SC_TDL};font-weight:700;color:#1e293b">{grp}</td>'
                for v in vals:
                    _hA += _colored(_fc(v), v)
                _hA += '</tr>'
            _hA += '</tbody></table></div>'

            # Card D (river view only): avg basis level + change trends by segment
            # (surfaces the Illinois River zone and Miss/Ohio segment trends).
            _hD = ""
            if _river_view:
                _hD = (f'<div style="{_SC_CARD}"><div style="{_SC_TITLE}">'
                       f'Avg Basis &amp; Change by {_grp_title} (¢)</div>'
                       f'<table style="border-collapse:collapse;width:100%"><thead><tr>'
                       f'<th style="{_SC_THL}"></th>'
                       f'<th style="{_SC_TH}">Avg Basis</th>')
                for _, lab in _WINS:
                    _hD += f'<th style="{_SC_TH}">{lab}</th>'
                _hD += '</tr></thead><tbody>'
                for gv in _groups:
                    _hD += f'<tr><td style="{_SC_TDL};font-weight:700;color:#1e293b">{gv}</td>'
                    # Avg basis level (neutral, bold — distinct from the change cols)
                    ab = _grp_avg_basis(gv)
                    abtxt = "—" if ab is None else f"{'+' if ab >= 0 else '−'}{abs(ab):.1f}"
                    _hD += f'<td style="{_SC_TD};color:#0f172a;font-weight:800">{abtxt}</td>'
                    for w, _ in _WINS:
                        v = _grp_avg(gv, w)
                        _hD += _colored(_fc(v), v)
                    _hD += '</tr>'
                _hD += '</tbody></table></div>'

            # Card B: % firmer / weaker by region (non-river views only)
            _hB = ""
            if not _river_view:
                _hB = (f'<div style="{_SC_CARD}"><div style="{_SC_TITLE}">'
                       f'Firmer / Weaker by {_grp_title}</div>'
                       f'<table style="border-collapse:collapse;width:100%"><thead><tr>'
                       f'<th style="{_SC_THL}"></th>')
                for _, lab in _WINS:
                    _hB += f'<th style="{_SC_TH}">{lab}</th>'
                _hB += '</tr></thead><tbody>'
                for gv in _groups:
                    for want_firmer, lab2 in ((True, "Firmer"), (False, "Weaker")):
                        _hB += (f'<tr><td style="{_SC_TDL};font-weight:700;color:#1e293b">'
                                f'{gv} <span style="color:#64748b;font-weight:400">{lab2}</span></td>')
                        for w, _ in _WINS:
                            pv = _grp_pct(gv, w, want_firmer)
                            col = "#16a34a" if want_firmer else "#dc2626"
                            cell = (f'<td style="{_SC_TD};color:#cbd5e1">—</td>' if pv is None
                                    else f'<td style="{_SC_TD};color:{col};font-weight:700">{_fp(pv)}</td>')
                            _hB += cell
                        _hB += '</tr>'
                _hB += '</tbody></table></div>'

            # Card C: spot above following month, by group
            _hC = (f'<div style="{_SC_CARD}"><div style="{_SC_TITLE}">Spot &gt; Next Month</div>'
                   f'<table style="border-collapse:collapse;width:100%"><thead><tr>'
                   f'<th style="{_SC_THL}">{_grp_title}</th><th style="{_SC_TH}">% Inverted</th>'
                   f'</tr></thead><tbody>')
            for gv in _groups:
                iv = _grp_inverse(gv)
                _hC += (f'<tr><td style="{_SC_TDL};font-weight:700;color:#1e293b">{gv}</td>'
                        f'<td style="{_SC_TD};color:#0f172a;font-weight:700">{_fp(iv)}</td></tr>')
            _hC += '</tbody></table></div>'

            if _river_view:
                _cards, _grid_cols = _hA + _hD + _hC, "0.85fr 2fr 0.7fr"
            else:
                _cards, _grid_cols = _hA + _hB + _hC, "1.15fr 1.15fr .7fr"
            st.markdown(
                f'<div style="display:grid;grid-template-columns:{_grid_cols};'
                f'gap:10px;margin:2px 0 14px 0">{_cards}</div>',
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

            def _bcell(basis, sym, col_ref, move=None, bold=False) -> str:
                # Badge a cell only when it prices against a different option month
                # than its OWN column's majority reference (e.g. Platinum vs Nov).
                # `move` (day-over-day basis delta) drives green/red highlighting:
                # positive → green, negative → red, no change/None → as-is.
                if basis is None:
                    return f'<td style="{_TD_R};color:#cbd5e1">—</td>'
                sign  = "+" if basis >= 0 else ""
                badge = ""
                if sym and col_ref and sym != col_ref:
                    badge = (f'<span style="font-size:9px;color:#f59e0b;'
                             f'margin-left:2px;font-weight:700">{short_sym(sym)}</span>')
                extra = ""
                if move is not None and move != 0:
                    extra += ";background:#dcfce7" if move > 0 else ";background:#fee2e2"
                if bold:
                    extra += ";font-weight:800"
                return f'<td style="{_TD_R}{extra}">{sign}{basis}{badge}</td>'

            def _ccell(chg, bold=False) -> str:
                fw = "800" if bold else "700"
                if chg is None:
                    return f'<td style="{_TD_R};color:#cbd5e1">—</td>'
                if chg == 0:
                    dash_fw = ";font-weight:800" if bold else ""
                    return f'<td style="{_TD_R};color:#64748b{dash_fw}">—</td>'
                sign  = "+" if chg > 0 else ""
                color = "#16a34a" if chg > 0 else "#dc2626"
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
                # Row 2b — reference option month per column (between name and date)
                '<tr style="background:#f8fafc">'
                f'<th colspan="4" style="{_TH_L};font-weight:400;color:#cbd5e1"></th>'
            )
            for lbl, _ in _COL_META:
                bdr = ";border-left:1px solid #e2e8f0" if lbl in ("yr_ago", "current") else ""
                h  += (f'<th style="{_TH_R}{bdr};font-weight:700;color:#0693e3;'
                       f'font-size:9px;letter-spacing:0">{_csyms[lbl]}</th>')
            h += (
                f'<th colspan="4" style="{_TH_R};border-left:1px solid #e2e8f0;'
                f'font-weight:400;color:#cbd5e1"></th>'
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

            # ── Region / state index rows (avg basis per period + avg changes) ──
            _EAST_STATES   = ["IL", "IN", "OH"]
            _WEST_STATES   = ["IA", "NE", "MN", "MO"]
            _REGION_STATES = {"East": _EAST_STATES, "West": _WEST_STATES}

            # Group by river segment in river view, else by East/West region.
            _grp_field = "segment" if _river_view else "region"
            _by_group: dict = {}
            for r in _srows:
                _by_group.setdefault(r.get(_grp_field) or "", []).append(r)

            def _aggregate(subset: list) -> dict:
                agg = {"n": len(subset)}
                for lbl, _, _ in _TARGETS:
                    vals = [r[f"b_{lbl}"] for r in subset if r.get(f"b_{lbl}") is not None]
                    agg[f"b_{lbl}"] = (sum(vals) / len(vals)) if vals else None
                for ck, win in (("c_daily", "d1_ago"), ("c_weekly", "wk_ago"),
                                ("c_monthly", "mo_ago"), ("c_yearly", "yr_ago")):
                    ms = [r["b_current"] - r[f"b_{win}"] for r in subset
                          if r.get("b_current") is not None and r.get(f"b_{win}") is not None]
                    agg[ck] = (sum(ms) / len(ms)) if ms else None
                return agg

            def _index_tr(label: str, agg: dict, region_level: bool) -> str:
                bg     = "#eef2ff" if region_level else "#f8fafc"
                lab_c  = "#32373c" if region_level else "#475569"
                fw     = "800" if region_level else "700"
                pad    = "" if region_level else "padding-left:22px;"
                tr  = f'<tr style="background:{bg}">'
                tr += (f'<td colspan="4" style="{_TD_L};{pad}font-weight:{fw};color:{lab_c};'
                       f'font-size:10px;text-transform:uppercase;letter-spacing:.05em">'
                       f'{label} <span style="color:#94a3b8;font-weight:400">'
                       f'({agg["n"]})</span></td>')
                # Indexed basis per period (avg basis)
                for lbl, _ in _COL_META:
                    bdr = ";border-left:1px solid #e2e8f0" if lbl in ("yr_ago", "current") else ""
                    bw  = ";font-weight:800" if lbl == "current" else ""
                    v   = agg.get(f"b_{lbl}")
                    txt = "—" if v is None else f"{v:.1f}"
                    tr += f'<td style="{_TD_R}{bdr}{bw};color:#0f172a">{txt}</td>'
                # Avg changes (colored like the change columns)
                for j, ck in enumerate(("c_daily", "c_weekly", "c_monthly", "c_yearly")):
                    bdr = ";border-left:1px solid #e2e8f0" if j == 0 else ""
                    bw  = ";font-weight:800" if j == 0 else ";font-weight:700"
                    v   = agg.get(ck)
                    if v is None or round(v, 1) == 0:
                        tr += f'<td style="{_TD_R}{bdr};color:#94a3b8">—</td>'
                    else:
                        col = "#16a34a" if v > 0 else "#dc2626"
                        sgn = "+" if v > 0 else "−"
                        tr += f'<td style="{_TD_R}{bdr}{bw};color:{col}">{sgn}{abs(v):.1f}</td>'
                tr += '</tr>'
                return tr

            _prev_group = object()  # sentinel
            for r in _srows:
                group = r.get(_grp_field) or ""

                # Group divider row + index rows
                if group != _prev_group:
                    h += (
                        f'<tr><td colspan="14" style="font-family:\'IBM Plex Mono\',monospace;'
                        f'font-size:9px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
                        f'letter-spacing:.15em;background:#f8fafc;padding:4px 8px;'
                        f'border-top:2px solid #e2e8f0">'
                        f'{group if group else "—"}</td></tr>'
                    )
                    if _river_view:
                        # One index row per river segment
                        h += _index_tr(f"{group} Index",
                                       _aggregate(_by_group[group]), region_level=True)
                    elif group in _REGION_STATES:
                        # Region index, then state indexes just below it
                        h += _index_tr(f"{group} Index",
                                       _aggregate(_by_group[group]), region_level=True)
                        for _stt in _REGION_STATES[group]:
                            _sub = [r2 for r2 in _by_group[group]
                                    if (r2.get("state") or "") == _stt]
                            if _sub:
                                h += _index_tr(f"{_stt} Index",
                                               _aggregate(_sub), region_level=False)
                    _prev_group = group

                # Highlight row if its CURRENT bid prices against a different
                # option month than the current column's majority reference.
                # Subtle yellow — kept distinct from the green/red trade shades.
                _cur_ref = _csyms_raw.get("current") or _ref_sym
                _row_bg = "background:#fef9c3" if (r["s_current"] and r["s_current"] != _cur_ref) else ""

                h += f'<tr style="{_row_bg}">'
                _loc_disp = (adm_city_from_name(r["location"])
                             if r["provider"] == "ADM" else r["location"])
                h += f'<td style="{_TD_L};color:#64748b;font-size:10px">{r.get("region") or ""}</td>'
                h += f'<td style="{_TD_L};font-weight:700;color:#32373c">{r["provider"]}</td>'
                h += f'<td style="{_TD_L}">{_loc_disp}</td>'
                h += f'<td style="{_TD_R};color:#64748b">{r["state"]}</td>'

                for i, (lbl, _) in enumerate(_COL_META):
                    bdr = ";border-left:1px solid #f1f5f9" if lbl in ("yr_ago", "current") else ""
                    cell = _bcell(r[f"b_{lbl}"], r.get(f"s_{lbl}"), _csyms_raw.get(lbl),
                                  move=r.get(f"m_{lbl}"), bold=(lbl == "current"))
                    # Inject border into the cell's style
                    h += cell.replace(f'style="{_TD_R}', f'style="{_TD_R}{bdr}', 1)

                # Change columns
                _daily  = (r["b_current"] - r["b_d1_ago"])  if (r["b_current"] is not None and r["b_d1_ago"]  is not None) else None
                _weekly = (r["b_current"] - r["b_wk_ago"])  if (r["b_current"] is not None and r["b_wk_ago"]  is not None) else None
                _montly = (r["b_current"] - r["b_mo_ago"])  if (r["b_current"] is not None and r["b_mo_ago"]  is not None) else None
                _yearly = (r["b_current"] - r["b_yr_ago"])  if (r["b_current"] is not None and r["b_yr_ago"]  is not None) else None

                h += _ccell(_daily, bold=True).replace(f'style="{_TD_R}',  f'style="{_TD_R};border-left:1px solid #f1f5f9"', 1)
                h += _ccell(_weekly)
                h += _ccell(_montly)
                h += _ccell(_yearly)
                h += '</tr>'

            h += '</tbody></table></div>'
            st.markdown(h, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# TAB: TRENDS  (basis trend stats by location type)
# ═══════════════════════════════════════════════════════════════════════════════
with tab_trends:
    st.markdown(
        '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:11px;color:#64748b;'
        'padding:4px 0 8px">Basis trend stats by location type · firmer = positive, '
        'weaker = negative · non-river grouped East/West, river grouped by segment.</div>',
        unsafe_allow_html=True,
    )

    _TREND_CATS = TREND_CATEGORIES

    # Delivery-period filter (union of available periods across categories; Spot default)
    _trend_periods: set = set()
    for _, _tft, _tgr, _ in _TREND_CATS:
        _trend_periods |= trend_periods(_tft, _tgr)
    _trend_deliv_opts = ["Spot (Front Month)"] + [_dp.label(p) for p in sorted(_trend_periods)]
    if st.session_state.get("trend_deliv") not in _trend_deliv_opts:
        st.session_state["trend_deliv"] = "Spot (Front Month)"
    _tdcol, _ = st.columns([2, 6])
    with _tdcol:
        _trend_sel = st.selectbox("Delivery Period", _trend_deliv_opts, key="trend_deliv")
    _trend_mode = "spot" if _trend_sel.startswith("Spot") else _trend_sel

    for _ttl, _ft, _gr, _mode in _TREND_CATS:
        _rows = build_trend_rows(_ft, _gr, _trend_mode)
        st.markdown(
            f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:13px;font-weight:800;'
            f'color:#0f172a;margin:10px 0 4px;padding-top:8px;border-top:2px solid #e2e8f0">'
            f'{_ttl} <span style="color:#94a3b8;font-weight:400;font-size:11px">'
            f'· {len(_rows)} locations</span></div>',
            unsafe_allow_html=True,
        )
        if not _rows:
            st.caption("No data for this category.")
            continue
        if _mode == "segment":
            _grps = [s for s in SEGMENT_ORDER if any((r.get("segment") or "") == s for r in _rows)]
            _gf = "segment"
        else:
            _grps = [g for g in ("East", "West") if any((r.get("region") or "") == g for r in _rows)]
            _gf = "region"
        st.markdown(render_trend_cards(_rows, _gf, _grps), unsafe_allow_html=True)
