"""
Basis Tracker · JPSI
Streamlit app — run with: streamlit run app.py
"""
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
import streamlit as st

from database import (
    init_db, upsert_snapshot, get_snapshots, delete_snapshot,
    list_locations, get_location_meta, get_all_location_meta, get_map_data,
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

# ── On-startup init ───────────────────────────────────────────────────────────
init_db()

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

def compute_changes(snapshots):
    if not snapshots:
        return {"rows": {}, "spots": {}}

    latest  = snapshots[-1]
    now_ms  = datetime.fromisoformat(
        latest.timestamp.replace("Z", "+00:00")).timestamp() * 1000
    WEEK    = 7  * 864e5
    MONTH   = 30 * 864e5
    YEAR    = 365 * 864e5

    row_lookup  = {}
    spot_lookup = {"Soybeans": [], "Corn": [], "Wheat": []}

    for snap in snapshots:
        ts_ms = datetime.fromisoformat(
            snap.timestamp.replace("Z", "+00:00")).timestamp() * 1000
        for r in snap.rows:
            entry = {"ts_ms": ts_ms, "b": r.basisCents, "sym": r.futuresSymbol}
            if r.isSpot:
                g = r.spotGrain or r.grain
                if g in spot_lookup:
                    spot_lookup[g].append(entry)
            else:
                if r.id not in row_lookup:
                    row_lookup[r.id] = []
                row_lookup[r.id].append(entry)

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
    for g in ["Soybeans", "Corn", "Wheat"]:
        sp = next((r for r in latest.rows
                   if r.isSpot and (r.spotGrain or r.grain) == g), None)
        if sp and spot_lookup[g]:
            spot_changes[g] = calc(spot_lookup[g], sp.basisCents, sp.futuresSymbol)

    return {"rows": row_changes, "spots": spot_changes}

def delta_html(d, is_meal=False):
    if not d:
        return '<span style="color:#1e3a5f">—</span>'
    if d.get("unknown"):
        return '<span style="color:#f59e0b;font-weight:700">⚠ roll</span>'
    val = d.get("val")
    if val is None:
        return '<span style="color:#1e3a5f">—</span>'
    if val == 0:
        zero_str = "±$0.00/t" if is_meal else "±0¢"
        return f'<span style="color:#334155;font-weight:600">{zero_str}</span>'
    color  = "#4ade80" if val > 0 else "#f87171"
    arrow  = "▲" if val > 0 else "▼"
    sign   = "+" if val > 0 else "−"
    adj    = ' <span style="font-size:9px;color:#64748b">adj</span>' if d.get("rolled") else ""
    amount = f"${abs(val)/100:.2f}/t" if is_meal else f"{abs(val)}¢"
    return (f'<span style="color:{color};font-weight:700">'
            f'<span style="font-size:9px">{arrow}</span>'
            f'{sign}{amount}{adj}</span>')

def render_table(body_rows, spot_row, changes, spot_chg, loc_color, year_ago_label, is_meal=False):
    th = ("background:#070b14;color:#1e3a5f;font-size:9px;text-transform:uppercase;"
          "letter-spacing:.12em;padding:5px 12px;text-align:left;border-bottom:1px solid #0c1e36;"
          "font-weight:700;white-space:pre;line-height:1.3;font-family:inherit")
    td_base = "padding:9px 12px;font-family:'IBM Plex Mono',monospace"

    headers = ["Delivery","Futures","Contract","Basis",
               "vs Prev","vs ~1 Wk","vs ~1 Mo",f"vs ~1 Yr\n{year_ago_label}"]

    html = (
        '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono'
        ':wght@400;600;700;800&display=swap" rel="stylesheet">'
        '<table style="width:100%;border-collapse:collapse;font-size:12px;'
        'font-family:\'IBM Plex Mono\',monospace">'
        "<thead><tr>" +
        "".join(f'<th style="{th}">{h}</th>' for h in headers) +
        "</tr></thead><tbody>"
    )

    # Spot row
    if spot_row and spot_row.basisCents is not None:
        bc    = spot_row.basisCents
        color = "#86efac" if bc >= 0 else "#fca5a5"
        chgs  = spot_chg or {}
        html += (
            f'<tr style="background:#0c1f38">'
            f'<td style="{td_base};border-left:3px solid {loc_color}">'
            f'<div style="font-size:9px;color:{loc_color};text-transform:uppercase;'
            f'letter-spacing:.15em;font-weight:700;margin-bottom:2px">SPOT</div>'
            f'<div style="color:#f1f5f9;font-weight:800">{spot_row.deliveryMonth}</div></td>'
            f'<td style="{td_base}"><span style="background:#1d3461;border:1px solid {loc_color};'
            f'color:#93c5fd;padding:2px 7px;border-radius:3px;font-size:11px;font-weight:800">'
            f'{spot_row.futuresSymbol}</span></td>'
            f'<td style="{td_base};color:#60a5fa;font-size:11px">{short_sym(spot_row.futuresSymbol)}</td>'
            f'<td style="{td_base}"><span style="color:{color};font-weight:800;font-size:16px;'
            f'font-variant-numeric:tabular-nums">{fmt_basis(bc, is_meal)}</span></td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromPrev"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromWeek"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromMonth"), is_meal)}</td>'
            f'<td style="{td_base}">{delta_html(chgs.get("fromYear"), is_meal)}</td>'
            f'</tr>'
        )
        html += (f'<tr><td colspan="8" style="padding:2px 0">'
                 f'<div style="height:1px;background:#0c1e36;margin:0 12px"></div></td></tr>')

    # Body rows
    for i, row in enumerate(body_rows):
        bc  = row.basisCents
        chg = changes["rows"].get(row.id, {})
        changed = chg.get("fromPrev", {}).get("val") not in (None, 0)
        dot = (' <span style="display:inline-block;width:5px;height:5px;border-radius:50%;'
               'background:#fbbf24;vertical-align:middle"></span>') if changed else ""
        bg  = "#0a1828" if changed else ("#080f1c" if i % 2 == 1 else "transparent")
        bc_color = "#4ade80" if (bc or 0) >= 0 else "#f87171"
        html += (
            f'<tr style="background:{bg}">'
            f'<td style="{td_base};color:#cbd5e1;font-weight:700">{row.deliveryMonth}{dot}</td>'
            f'<td style="{td_base}"><span style="background:#0b1e38;border:1px solid #1e3a5f;'
            f'color:#60a5fa;padding:2px 7px;border-radius:3px;font-size:11px;font-weight:700">'
            f'{row.futuresSymbol}</span></td>'
            f'<td style="{td_base};color:#334155;font-size:11px">{short_sym(row.futuresSymbol)}</td>'
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
  .block-container { padding-top: 1rem !important; }
  div[data-testid="stHorizontalBlock"] { gap: 0 !important; }
  button[kind="secondary"] { font-family: 'IBM Plex Mono', monospace !important; }
  .stTabs [data-baseweb="tab-list"] { gap: 0; background: #070b14; border-bottom: 1px solid #0c1e36; }
  .stTabs [data-baseweb="tab"] { color: #334155; font-size: 12px; padding: 8px 18px;
    font-family: 'IBM Plex Mono', monospace; border-radius: 0; }
  .stTabs [aria-selected="true"] { color: #60a5fa !important; font-weight: 700 !important;
    border-bottom: 2px solid #3b82f6 !important; }
  .stTabs [data-baseweb="tab-panel"] { padding-top: 0 !important; }
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --adm-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --poet-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'Both run automatically at 3:45 PM daily.<br>'
        'CLI: <code style="color:#60a5fa">python auto_import.py --chs-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --cgb-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --cargill-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --bunge-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --andersons-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --scoular-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --ldc-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --agp-only</code>'
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
        '<div style="font-size:9px;color:#475569;padding-top:4px">'
        'CLI: <code style="color:#60a5fa">python auto_import.py --gpre-only</code>'
        '</div>',
        unsafe_allow_html=True,
    )

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="margin-bottom:4px">
  <div style="font-size:9px;color:#1d4ed8;letter-spacing:.2em;text-transform:uppercase;
    font-weight:700">JPSI · Cash Grain Basis Monitor</div>
  <div style="font-size:22px;font-weight:800;color:#f1f5f9;letter-spacing:-.03em;
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
    chs_db_locs = [r for r in list_locations() if r["provider"] == "CHS"]
    if not chs_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No CHS data yet.<br><br>'
            'Run <code style="color:#60a5fa">python auto_import.py --chs-only</code> '
            'to scrape all CHS locations, then refresh this page.'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # ── Load state / type metadata ────────────────────────────────────────────
    chs_meta = get_location_meta("CHS")   # {location: {"state": ..., "facility_type": ...}}
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
            '<div style="color:#334155;text-align:center;padding:20px;font-size:12px">'
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
    _chs_snaps = get_snapshots("CHS", loc_key)
    if _chs_snaps:
        grains = sorted({r.grain for r in _chs_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]

elif provider == "POET":
    # Dynamically load POET locations from whatever's in the database.
    # Each row is {"provider": "POET", "location": "Alexandria, IN"}.
    poet_db_locs = [r for r in list_locations() if r["provider"] == "POET"]
    if not poet_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No POET data yet.<br><br>'
            'Run <code style="color:#60a5fa">python auto_import.py --poet-only</code> '
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
    _poet_snaps = get_snapshots("POET", loc_key)
    if _poet_snaps:
        grains = sorted({r.grain for r in _poet_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]

elif provider == "ADM":
    adm_db_locs = sorted({r["location"] for r in list_locations() if r["provider"] == "ADM"})
    if not adm_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No ADM data yet.<br><br>'
            'Click <b>Scrape ADM now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --adm-only</code>'
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
    _adm_snaps = get_snapshots("ADM", loc_key)
    if _adm_snaps:
        grains = sorted({r.grain for r in _adm_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]

elif provider == "CGB":
    cgb_db_locs = sorted(
        {r["location"] for r in list_locations() if r["provider"] == "CGB"}
    )
    if not cgb_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No CGB data yet.<br><br>'
            'Click <b>Scrape CGB now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --cgb-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    cgb_meta        = get_location_meta("CGB")  # {name: {"state": ..., "facility_type": ...}}
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
    _cgb_snaps = get_snapshots("CGB", loc_key)
    if _cgb_snaps:
        grains = sorted({r.grain for r in _cgb_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]

elif provider == "GPRE":
    gpre_db_locs = sorted(
        {r["location"] for r in list_locations() if r["provider"] == "GPRE"}
    )
    if not gpre_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No GPRE data yet.<br><br>'
            'Click <b>Scrape GPRE now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --gpre-only</code>'
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
        {r["location"] for r in list_locations() if r["provider"] == "Cargill"}
    )
    if not cargill_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No Cargill data yet.<br><br>'
            'Click <b>Scrape Cargill now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --cargill-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    cargill_meta         = get_location_meta("Cargill")
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
    _cargill_snaps = get_snapshots("Cargill", loc_key)  # noqa: F841
    if _cargill_snaps:
        grains = sorted({r.grain for r in _cargill_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]

elif provider == "Andersons":
    andersons_db_locs = sorted(
        {r["location"] for r in list_locations() if r["provider"] == "Andersons"}
    )
    if not andersons_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No Andersons data yet.<br><br>'
            'Click <b>Scrape Andersons now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --andersons-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    andersons_meta         = get_location_meta("Andersons")
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
    _andersons_snaps = get_snapshots("Andersons", loc_key)
    if _andersons_snaps:
        grains = sorted({r.grain for r in _andersons_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]

elif provider == "Bunge":
    bunge_db_locs = sorted(
        {r["location"] for r in list_locations() if r["provider"] == "Bunge"}
    )
    if not bunge_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No Bunge data yet.<br><br>'
            'Click <b>Scrape Bunge now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --bunge-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    bunge_meta         = get_location_meta("Bunge")
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
    _bunge_snaps = get_snapshots("Bunge", loc_key)
    if _bunge_snaps:
        grains = sorted({r.grain for r in _bunge_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Soybeans"]

elif provider == "Scoular":
    scoular_db_locs = sorted(
        {r["location"] for r in list_locations() if r["provider"] == "Scoular"}
    )
    if not scoular_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No Scoular data yet.<br><br>'
            'Click <b>Scrape Scoular now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --scoular-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    # State filter via location_meta (populated during scrape)
    scoular_meta         = get_location_meta("Scoular")
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
    _scoular_snaps = get_snapshots("Scoular", loc_key)
    if _scoular_snaps:
        grains = sorted({r.grain for r in _scoular_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]

elif provider == "AGP":
    agp_db_locs = sorted(
        {r["location"] for r in list_locations() if r["provider"] == "AGP"}
    )
    if not agp_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No AGP data yet.<br><br>'
            'Click <b>Scrape AGP now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --agp-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    agp_meta         = get_location_meta("AGP")
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
    _agp_snaps = get_snapshots("AGP", loc_key)
    if _agp_snaps:
        grains = sorted({r.grain for r in _agp_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Soybeans"]

elif provider == "LDC":
    ldc_db_locs = sorted(
        {r["location"] for r in list_locations() if r["provider"] == "LDC"}
    )
    if not ldc_db_locs:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            'No LDC data yet.<br><br>'
            'Click <b>Scrape LDC now</b> in the sidebar or run:<br>'
            '<code style="color:#60a5fa">python auto_import.py --ldc-only</code>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.stop()

    ldc_meta         = get_location_meta("LDC")
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
    _ldc_snaps = get_snapshots("LDC", loc_key)
    if _ldc_snaps:
        grains = sorted({r.grain for r in _ldc_snaps[-1].rows if not r.isSpot})
    else:
        grains = ["Corn"]


tab_bids, tab_map = st.tabs(["📋 Bids", "🗺️ Map"])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB: BIDS
# ═══════════════════════════════════════════════════════════════════════════════
with tab_bids:
    # ── Load snapshots ────────────────────────────────────────────────────────
    snapshots = get_snapshots(provider, loc_key)

    if not snapshots:
        if provider == "POET":
            hint = ('Run <code style="color:#60a5fa">python auto_import.py --poet-only</code> '
                    'to scrape this location, then refresh.')
        elif provider == "ADM":
            hint = ('Run <code style="color:#60a5fa">python auto_import.py --adm-only</code> '
                    'or click <b>Scrape ADM now</b> in the sidebar, then refresh.')
        elif provider == "CGB":
            hint = ('Click <b>Scrape CGB now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --cgb-only</code>, '
                    'then refresh.')
        elif provider == "CHS":
            hint = ('Click <b>Scrape CHS now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --chs-only</code>, '
                    'then refresh.')
        elif provider == "Cargill":
            hint = ('Click <b>Scrape Cargill now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --cargill-only</code>, '
                    'then refresh.')
        elif provider == "GPRE":
            hint = ('Click <b>Scrape GPRE now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --gpre-only</code>, '
                    'then refresh.')
        elif provider == "Andersons":
            hint = ('Click <b>Scrape Andersons now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --andersons-only</code>, '
                    'then refresh.')
        elif provider == "Bunge":
            hint = ('Click <b>Scrape Bunge now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --bunge-only</code>, '
                    'then refresh.')
        elif provider == "Scoular":
            hint = ('Click <b>Scrape Scoular now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --scoular-only</code>, '
                    'then refresh.')
        elif provider == "AGP":
            hint = ('Click <b>Scrape AGP now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --agp-only</code>, '
                    'then refresh.')
        elif provider == "LDC":
            hint = ('Click <b>Scrape LDC now</b> in the sidebar or run:<br>'
                    '<code style="color:#60a5fa">python auto_import.py --ldc-only</code>, '
                    'then refresh.')
        else:
            hint = "Run the daily scraper to populate data for this provider."
        st.markdown(
            f'<div style="color:#334155;text-align:center;padding:40px;font-size:12px">'
            f'No snapshots yet for <b>{loc_key}</b>.<br><br>{hint}</div>',
            unsafe_allow_html=True,
        )
    else:
        # ── Date picker + grain selector ──────────────────────────────────────
        snap_labels = []
        for s in snapshots:
            d = datetime.fromisoformat(s.timestamp.replace("Z","+00:00"))
            lbl = d.strftime("%b %d, %Y") + (" ★ latest" if s is snapshots[-1] else "")
            snap_labels.append(lbl)

        pick_col, grain_col = st.columns([3, 4])
        with pick_col:
            sel_label_snap = st.selectbox(
                "Viewing snapshot",
                options=snap_labels[::-1],   # newest first
                index=0,
                key=f"snap_pick_{loc_key}",
                label_visibility="visible",
            )
            sel_idx = snap_labels[::-1].index(sel_label_snap)
            viewing = snapshots[::-1][sel_idx]   # the selected snapshot
            snaps_up_to = snapshots[: snapshots.index(viewing) + 1]
            changes = compute_changes(snaps_up_to)

        with grain_col:
            if len(grains) > 1:
                grain = st.radio("Grain", grains, horizontal=True,
                                 label_visibility="collapsed", key=f"grain_{loc_key}")
            else:
                grain = grains[0]

        body_rows = [r for r in viewing.rows if not r.isSpot and r.grain == grain]
        spot_row  = next((r for r in viewing.rows
                          if r.isSpot and (r.spotGrain or r.grain) == grain), None)
        spot_chg  = changes["spots"].get(grain)

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
                    f'<span style="color:#fbbf24;font-size:11px;font-weight:600">'
                    f'● {moved} changed vs prior</span>', unsafe_allow_html=True)
            else:
                st.markdown(
                    '<span style="color:#1e3a5f;font-size:11px">No changes vs prior</span>',
                    unsafe_allow_html=True)
        with s_col2:
            st.markdown(
                f'<span style="color:#334155;font-size:10px">as of '
                f'<span style="color:#60a5fa;font-weight:700">{latest_label}</span></span>',
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
            f'<span style="color:#60a5fa">{r["from"]}->{r["to"]}</span>'
            f' {r["adj"]}c' for r in ROLL_ADJ)
        st.markdown(
            f'<div style="margin-top:8px;padding:8px 14px;background:#08111e;'
            f'border:1px solid #0c1e36;border-radius:6px;font-size:10px;color:#334155">'
            f'<span style="color:#1e3a5f;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.1em">Roll adj:</span> {roll_parts}'
            f' &nbsp;|&nbsp; <span style="font-size:9px">Same letter diff year = no adj'
            f' | ? = unknown roll</span></div>',
            unsafe_allow_html=True,
        )

    # ── Snapshot history ──────────────────────────────────────────────────────
    if snapshots:
        with st.expander(f"Snapshot history — {loc_key} ({len(snapshots)} records)", expanded=False):
            for snap in reversed(snapshots):
                is_latest  = snap is snapshots[-1]
                is_viewing = snap is viewing
                d_label    = datetime.fromisoformat(
                    snap.timestamp.replace("Z", "+00:00")).strftime("%b %d '%y")
                src_icon    = " [email]" if snap.source == "email" else ""
                badge_color = loc_color if is_viewing else "#1e3a5f"
                c1, c2 = st.columns([9, 1])
                with c1:
                    st.markdown(
                        f'<span style="background:#08111e;border:1px solid {badge_color};'
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

    map_rows = get_map_data()

    if not map_rows:
        st.markdown(
            '<div style="color:#334155;text-align:center;padding:60px;font-size:12px">'
            'No geocoded locations yet.<br><br>'
            'Run <code style="color:#60a5fa">python geocode_locations.py</code> '
            'to populate coordinates, then refresh.</div>',
            unsafe_allow_html=True,
        )
    else:
        # ── Provider filter ───────────────────────────────────────────────────
        all_providers_map = sorted({r["provider"] for r in map_rows})
        sel_provs = st.multiselect(
            "Filter providers",
            options=all_providers_map,
            default=all_providers_map,
            key="map_prov_filter",
            label_visibility="collapsed",
        )
        filtered = [r for r in map_rows if r["provider"] in sel_provs]

        # ── Build DataFrame ───────────────────────────────────────────────────
        def _fmt_basis(cents):
            if cents is None:
                return "—"
            sign = "+" if cents >= 0 else ""
            return f"{sign}{cents}c"

        def _tooltip_text(row):
            grains_str = "  ".join(
                f"{g}: {_fmt_basis(row['grains'].get(g))}"
                for g in sorted(row["grains"])
            )
            state_str = f", {row['state']}" if row["state"] else ""
            return f"{row['location']}{state_str} [{row['provider']}]  |  {grains_str}"

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
            map_style="mapbox://styles/mapbox/dark-v11",
        )

        st.pydeck_chart(deck, use_container_width=True)

        # ── Legend ────────────────────────────────────────────────────────────
        legend_parts = []
        for p in all_providers_map:
            c = _MAP_COLORS.get(p, _DEFAULT_COLOR)
            hex_c = "#{:02x}{:02x}{:02x}".format(*c)
            legend_parts.append(
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'margin-right:14px;font-size:11px;color:#94a3b8">'
                f'<span style="width:10px;height:10px;border-radius:50%;'
                f'background:{hex_c};display:inline-block"></span>{p}</span>'
            )
        st.markdown(
            '<div style="padding:8px 0;margin-top:4px">' + "".join(legend_parts) + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(f"{len(filtered)} locations shown  •  geocoding may be incomplete — run `python geocode_locations.py` to fill gaps")
