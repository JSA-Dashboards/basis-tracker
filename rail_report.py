"""
rail_report.py — "JSA Rail Basis Update" email.

Mirrors the Rail FOB board's change table (per corridor: current bid/offer + Δ vs
last posting / ~1 week / ~1 month / ~1 year) and embeds a spot seasonal basis chart,
sent via the same Outlook path as the daily Changes email (branded + signature).

Fires whenever the manual rail values are updated — call send_rail_update_email()
after a rundown is loaded.

    python rail_report.py --preview            # write the HTML to _rail_preview.html
    python rail_report.py --send               # email it to the default recipient
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, date, timedelta

from changes_report import (signature_html, send_email, JPSI_DARK, JPSI_BLUE,
                            _GAIN, _LOSS, _SIG_LOGO, _SIG_LOGO_CID,
                            _table_watermark, _TBL_WM_CID,
                            _roll_adjust, _futures_curve)
from database import get_rail_fob_all

# Load DATABASE_URL when rail_report is invoked standalone (a script or the scheduled
# task) so we connect to Postgres — otherwise database.py silently falls back to the
# empty local SQLite, which blanks the tables AND the per-corridor seasonal charts.
# No-op under the Streamlit app / Cloud, where the env / st.secrets are already set
# (load_dotenv never overrides an existing DATABASE_URL).
import pathlib as _pl_env
from dotenv import load_dotenv as _load_env
_load_env(_pl_env.Path(__file__).with_name(".env"))

log = logging.getLogger(__name__)

DEFAULT_TO = "kpostin@jpsi.com"
SUBJECT    = "JSA Rail Basis Update"
_SEAS_CID  = "rail_seasonal"
# Corridors to leave OFF the rail email (kept in the DB, just not reported).
_EXCLUDE = {"UP Illinois (Dom)"}     # Allen Station (Dom) — dropped 2026-08-24
# Corridors to ALWAYS include (user-requested freight series that update less often
# than the twice-weekly basis rundowns, so the freshness filter shouldn't drop them).
_ALWAYS  = {"BN Freight", "UP Freight"}

try:
    from rail_corridors import CORRIDORS, CORRIDOR_ORDER
    _RAIL_BY = {n: r for n, r in CORRIDORS}
except Exception:                                   # registry optional
    CORRIDOR_ORDER, _RAIL_BY = {}, {}

# Board display overrides (match app.py's _RAIL_DISPLAY).
_RAIL_DISPLAY = {"BN PNW CP": "CP PNW", "UP Illinois (Dom)": "Allen Station (Dom)",
                 "UP Illinois (Mex)": "Allen Station (Mex)"}
_RAIL_COLORS = {"CSX": "#0693e3", "NS": "#7c3aed", "UP": "#d97706",
                "BNSF": "#16a34a", "CN": "#b91c1c"}


# ── load + index the manual rail board ───────────────────────────────────────
def _load(source: str = "manual"):
    by_md: dict = {}          # (market, date) -> {period: row}
    mkt_dates: dict = {}      # market -> set(dates)
    for r in get_rail_fob_all(source):
        by_md.setdefault((r["market"], r["date"]), {})[r["period"]] = r
        mkt_dates.setdefault(r["market"], set()).add(r["date"])
    return by_md, mkt_dates


def _prior_maps(market, cur, by_md, mkt_dates):
    """(last posting, ~1wk, ~1mo, ~1yr) prior period->row maps for a corridor —
    same logic the board uses: 'last' is the previous POSTING, not the previous day."""
    earlier = sorted(d for d in mkt_dates.get(market, ()) if d < cur)
    if not earlier:
        return (None, None, None, None)
    cd = datetime.fromisoformat(cur).date()

    def closest(days, maxd):
        tgt = cd - timedelta(days=days)
        best = min(earlier, key=lambda d: abs((datetime.fromisoformat(d).date() - tgt).days))
        return best if abs((datetime.fromisoformat(best).date() - tgt).days) <= maxd else None

    return (by_md.get((market, earlier[-1])),
            by_md.get((market, closest(7, 4))),
            by_md.get((market, closest(30, 4))),
            by_md.get((market, closest(365, 60))))


# ── cell formatters ──────────────────────────────────────────────────────────
def _disp(num, raw):
    return raw if raw else (f"{num:+d}" if num is not None else None)


def _bo_cell(cell, blue, tdr):
    s = _disp(cell.get("offer" if blue else "bid"), cell.get("offer_raw" if blue else "bid_raw"))
    if s is None:
        return f'<td style="{tdr};color:#cbd5e1">—</td>'
    if s == "?":
        return f'<td style="{tdr};color:#94a3b8">?</td>'
    col = f"color:{JPSI_BLUE};font-weight:600" if blue else "color:#32373c;font-weight:700"
    return f'<td style="{tdr};{col}">{s}</td>'


def _chg_cell(cur_bid, prior_map, period, tdr, cur_fut=None, curve=None):
    if cur_bid is None or not prior_map or prior_map.get(period) is None:
        return f'<td style="{tdr};color:#cbd5e1">—</td>'
    prow = prior_map[period]
    pb = prow.get("bid")
    if pb is None:
        return f'<td style="{tdr};color:#cbd5e1">—</td>'
    d = cur_bid - pb
    rolled = False
    pf = prow.get("futures")
    # If this period's reference contract rolled (e.g. spot CU->CZ), spread-adjust the
    # change so it's the true basis move, not the contract gap. "R" packages can't be
    # anchored to a single contract, so leave those raw.
    if cur_fut and pf and cur_fut != pf and "R" not in (cur_fut, pf):
        adj = _roll_adjust(d, pf, cur_fut, curve or {})
        if adj is not None:
            d, rolled = adj, True
    mark = ' <span style="color:#d97706">&#8635;</span>' if rolled else ''
    if d == 0:
        return f'<td style="{tdr};color:#94a3b8">0{mark}</td>'
    return f'<td style="{tdr};color:{_GAIN if d > 0 else _LOSS};font-weight:700">{d:+d}{mark}</td>'


# ── spot seasonal chart (front-period bid by marketing week) ──────────────────
def _spot_series(market, by_md, mkt_dates):
    """{date -> nearest-period bid} for a corridor (smallest period_order with a bid)."""
    out = {}
    for d in sorted(mkt_dates.get(market, ())):
        cells = sorted(by_md.get((market, d), {}).values(),
                       key=lambda r: (r.get("period_order") if r.get("period_order") is not None else 99))
        bid = next((c["bid"] for c in cells if c.get("bid") is not None), None)
        if bid is not None:
            out[d] = bid
    return out


# Delivery-month → marketing week (Sep-start year), matching the app's rail seasonal.
_MON_WK = {"Sep": 1, "Oct": 5, "Nov": 10, "Dec": 14, "Jan": 18, "Feb": 23, "Mar": 27,
           "Apr": 31, "May": 36, "Jun": 40, "Jul": 45, "Aug": 49}
# Package periods → the months they span (spread into a per-month carry on the curve).
_PKG_MON = {"OND": ["Oct", "Nov", "Dec"], "JFM": ["Jan", "Feb", "Mar"], "AM": ["Apr", "May"],
            "JJ": ["Jun", "Jul"], "AMJJ": ["Apr", "May", "Jun", "Jul"],
            "JFMAMJJ": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"],
            "JAN-JUL": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"], "AS": ["Aug", "Sep"]}
_FWD_CARRY = 2.0
_MON_RE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)


def _fwd_bucket(period: str):
    """A rail period label → a single month name, a package key, or None."""
    p = " ".join(str(period).split())
    key = p.replace(" ", "").upper()
    if key in _PKG_MON:
        return key
    m = _MON_RE.search(p)                      # first month named (FH Oct → Oct)
    return m.group(1).title() if m else None


def _fwd_curve(market, by_md, mkt_dates):
    """Forward curve of the corridor's LATEST rundown as a (MktWeek, Bid) DataFrame:
    each delivery period placed at its month's week, packages spread into carry, kept
    to the current marketing year from today forward. None if it can't be built."""
    ds = sorted(mkt_dates.get(market, ()))
    if not ds:
        return None
    pts = []
    for period, r in by_md.get((market, ds[-1]), {}).items():
        bid = r.get("bid")
        if bid is None:
            continue
        bk = _fwd_bucket(period)
        if bk in _MON_WK:
            pts.append((_MON_WK[bk], float(bid)))
        elif bk in _PKG_MON:
            mons = _PKG_MON[bk]; ctr = (len(mons) - 1) / 2.0
            for i, mo in enumerate(mons):
                pts.append((_MON_WK[mo], round(float(bid) + _FWD_CARRY * (i - ctr), 1)))
    if len(pts) < 2:
        return None
    import pandas as pd
    df = pd.DataFrame(pts, columns=["MktWeek", "Bid"]).groupby("MktWeek", as_index=False)["Bid"].mean()
    t = date.today(); tmy = t.year if t.month >= 9 else t.year - 1
    twk = min(52, max(1, ((t - date(tmy, 9, 1)).days // 7) + 1))
    cur = df[df["MktWeek"] >= twk].sort_values("MktWeek")
    return cur if len(cur) >= 2 else df.sort_values("MktWeek")


def _seasonal_png(market, by_md, mkt_dates):
    """Render the corridor's spot seasonal (recent marketing years + 5-yr band) to PNG
    bytes via vl-convert, or None if not enough history / render unavailable."""
    series = _spot_series(market, by_md, mkt_dates)
    if len(series) < 8:
        return None
    _ds = sorted(mkt_dates.get(market, ()))
    _cells = list(by_md.get((market, _ds[-1]), {}).values()) if _ds else []
    _comm = (_cells[0].get("commodity") if _cells else None) or "Corn"
    try:
        import pandas as pd
        import altair as alt
        import vl_convert as vlc
    except Exception as exc:
        log.warning("rail seasonal render unavailable: %s", exc)
        return None

    rows = []
    for d, bid in series.items():
        dt = datetime.fromisoformat(d).date()
        my = dt.year if dt.month >= 9 else dt.year - 1
        wk = min(52, max(1, ((dt - date(my, 9, 1)).days // 7) + 1))
        rows.append({"MktYear": f"{my}/{str(my + 1)[-2:]}", "MyNum": my, "MktWeek": wk, "Bid": bid})
    df = pd.DataFrame(rows).groupby(["MktYear", "MyNum", "MktWeek"], as_index=False)["Bid"].mean()
    mx = int(df["MyNum"].max())                          # current (bold) marketing year
    if df["MyNum"].nunique() < 2:
        return None

    _yr = lambda y: f"{y}/{str(y + 1)[-2:]}"
    cur = df[df["MyNum"] == mx]
    cur_lbl = _yr(mx)
    prev = df[df["MyNum"] == mx - 1]        # most recent COMPLETE marketing year
    prev_lbl = _yr(mx - 1)
    _hy = sorted(y for y in df["MyNum"].unique() if y < mx)[-5:]   # 5 prior complete years (or fewer)
    hist = df[df["MyNum"].isin(_hy)]
    band = (hist.groupby("MktWeek")["Bid"].agg(lo="min", hi="max", avg="mean").reset_index()
            if len(_hy) >= 2 else None)
    _n = len(_hy)
    rng_lbl = f"{_yr(_hy[0])}–{_yr(_hy[-1])}" if _hy else ""
    _fwd = _fwd_curve(market, by_md, mkt_dates)

    # y-axis auto-fit to the band + current + forward (2.5–97.5 pct) so an outlier week
    # doesn't squash the chart; outliers clamp to the edge.
    _yv = list(cur["Bid"])
    if band is not None:
        _yv += list(band["lo"]) + list(band["hi"])
    if _fwd is not None:
        _yv += list(_fwd["Bid"])
    _ysc = alt.Scale(zero=False)
    if len(_yv) >= 8:
        _q = pd.Series(_yv).quantile([0.025, 0.975]); _lo, _hi = float(_q.iloc[0]), float(_q.iloc[1])
        if _hi > _lo:
            _pad = (_hi - _lo) * 0.10
            _ysc = alt.Scale(zero=False, domain=[round(_lo - _pad), round(_hi + _pad)], clamp=True)

    def _X(ax):                                          # fresh X channel per layer
        if ax:                                           # only the base layer sets the axis
            return alt.X("MktWeek:Q", scale=alt.Scale(domain=[1, 52]),
                         axis=alt.Axis(title=None, values=[1, 5, 10, 14, 18, 23, 27, 31, 36, 40, 45, 49],
                                       labelExpr=("{'1':'Sep','5':'Oct','10':'Nov','14':'Dec','18':'Jan',"
                                                  "'23':'Feb','27':'Mar','31':'Apr','36':'May','40':'Jun',"
                                                  "'45':'Jul','49':'Aug'}[datum.value]")))
        return alt.X("MktWeek:Q", scale=alt.Scale(domain=[1, 52]))   # inherit the shared axis

    def _Y(field, ax):                                   # fresh Y channel per layer
        if ax:
            return alt.Y(f"{field}:Q", scale=_ysc, title="Basis (¢)", axis=alt.Axis(labelFontSize=10))
        return alt.Y(f"{field}:Q", scale=_ysc)

    # Muted green/gray theme: bold near-black current year, dashed sage avg, light-sage
    # range band, brick-red forward curve. Legend up top with a swatch per series.
    _CUR, _PREV, _AVG, _BAND, _FWD = "#111827", "#2563eb", "#4b6a4b", "#c4d7bd", "#c0392b"
    _rng_name, _avg_name = f"{_n}-yr range ({rng_lbl})", f"{_n}-yr average"
    _dom, _rng = [cur_lbl], [_CUR]
    if not prev.empty:
        _dom.append(prev_lbl); _rng.append(_PREV)      # most recent complete year, hero blue
    _dom += [_avg_name, _rng_name, "Forward curve"]
    _rng += [_AVG, _BAND, _FWD]
    _cscale = alt.Scale(domain=_dom, range=_rng)
    _leg = alt.Legend(orient="top", title=None, direction="horizontal",
                      labelFontSize=9, columns=5, offset=2)
    _col = lambda: alt.Color("Series:N", scale=_cscale, legend=_leg)

    layers = [alt.Chart(pd.DataFrame({"MktWeek": [1, 52], "Bid": [0.0, 0.0]}))
              .mark_line(color="#cbd5e1", strokeDash=[3, 3], strokeWidth=1)
              .encode(x=_X(True), y=_Y("Bid", True))]     # base layer carries the axes
    if band is not None:
        layers.append(alt.Chart(band.assign(Series=_rng_name)).mark_area(opacity=0.6).encode(
            x=_X(False), y=_Y("lo", False), y2="hi:Q", color=_col()))
        layers.append(alt.Chart(band.assign(Series=_avg_name))
                      .mark_line(strokeDash=[7, 4], strokeWidth=2)
                      .encode(x=_X(False), y=_Y("avg", False), color=_col()))
    if not prev.empty:
        layers.append(alt.Chart(prev.assign(Series=prev_lbl))
                      .mark_line(strokeWidth=2.5).encode(x=_X(False), y=_Y("Bid", False), color=_col()))
    if not cur.empty:
        layers.append(alt.Chart(cur.assign(Series=cur_lbl))
                      .mark_line(strokeWidth=3.5).encode(x=_X(False), y=_Y("Bid", False), color=_col()))
    if _fwd is not None and not _fwd.empty:
        _fw = _fwd.assign(Series="Forward curve")
        layers.append(alt.Chart(_fw).mark_line(strokeDash=[6, 3], strokeWidth=2)
                      .encode(x=_X(False), y=_Y("Bid", False), color=_col()))
        layers.append(alt.Chart(_fw).mark_point(filled=True, size=34)
                      .encode(x=_X(False), y=_Y("Bid", False), color=_col()))
        layers.append(alt.Chart(_fw).mark_text(align="center", dy=-9, fontSize=8, fontWeight="bold",
                                               color=_FWD)
                      .encode(x=_X(False), y=_Y("Bid", False),
                              text=alt.Text("Bid:Q", format="+.0f")))

    # JSA 50-Year logo watermark — faint, centered, behind the data (over the zero rule).
    import base64 as _b64, pathlib as _pl
    _logo = _pl.Path(__file__).parent / "assets" / "50 Year logo JSA.png"
    if _logo.exists():
        _wm_h = int(250 * 0.55)
        _uri = "data:image/png;base64," + _b64.b64encode(_logo.read_bytes()).decode()
        _wm = (alt.Chart(pd.DataFrame({"MktWeek": [26.5], "url": [_uri]}))
               .mark_image(width=int(_wm_h * 0.93), height=_wm_h, opacity=0.15,
                           align="center", baseline="middle")
               .encode(x=alt.X("MktWeek:Q", scale=alt.Scale(domain=[1, 52])),
                       y=alt.value(int(250 / 2)), url="url:N"))
        layers = [layers[0], _wm] + layers[1:]

    chart = alt.layer(*layers).properties(
        width=680, height=250, padding={"left": 6, "right": 26, "top": 6, "bottom": 6},
        title=f"{_RAIL_DISPLAY.get(market, market)} · Spot {_comm} Basis Seasonal")
    try:
        return vlc.vegalite_to_png(json.dumps(chart.to_dict(), default=str), scale=1.5)
    except Exception as exc:
        log.warning("rail seasonal PNG failed for %s: %s", market, exc)
        return None


# ── HTML ─────────────────────────────────────────────────────────────────────
# _table_watermark / _TBL_WM_CID are imported from changes_report (shared with the
# daily-changes email) so both email families use one faint watermark tile.
def build_rail_html(markets: list | None = None, charts: bool = True,
                    title: str = "Rail Basis Update") -> tuple[str, dict]:
    """(html, inline_images) for the rail email; inline_images maps cid->filepath.

    `markets`: restrict to just these corridors (an UPDATE email of what was posted);
    None = the full active board (the weekly RECAP). `charts`: include the per-corridor
    seasonal images (kept for the recap, dropped for the lean update)."""
    by_md, mkt_dates = _load("manual")
    # Only corridors actively being posted — drop long-dead historical markets
    # (KC/KCS 2015, BN MN 2018, BN PNW Beans 2020, …) so the email stays current.
    _all_latest = max((max(ds) for ds in mkt_dates.values()), default=None)
    _cutoff = ((datetime.fromisoformat(_all_latest).date() - timedelta(days=21)).isoformat()
               if _all_latest else "0000")
    _active = [m for m, ds in mkt_dates.items()
               if (max(ds) >= _cutoff or m in _ALWAYS) and m not in _EXCLUDE]
    if markets is not None:                       # update email → only these corridors
        _sel = {m for m in markets}
        _active = [m for m in _active if m in _sel]
    _ordered = sorted(_active, key=lambda m: (CORRIDOR_ORDER.get(m, 99), m))

    th  = ("background:#f1f5f9;color:#475569;font-size:9px;text-transform:uppercase;"
           "letter-spacing:.04em;padding:4px 7px;font-weight:700;border-bottom:2px solid #e2e8f0;"
           "font-family:Arial,sans-serif")
    thl = th + ";text-align:left"
    thr = th + ";text-align:right"
    tdl = "padding:3px 7px;font-family:Arial,sans-serif;font-size:12px;border-bottom:1px solid #eef2f6;text-align:left"
    tdr = tdl.replace("text-align:left", "text-align:right") + ";font-variant-numeric:tabular-nums"

    latest_overall = max((max(ds) for ds in mkt_dates.values()), default=None)
    try:
        _curve = _futures_curve()       # for spread-adjusting a period's contract roll
    except Exception:
        _curve = {}
    body = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;color:{JPSI_DARK};max-width:820px">'
        f'<div style="background:{JPSI_DARK};padding:16px 20px;border-radius:8px 8px 0 0">'
        f'<div style="color:#fff;font-size:18px;font-weight:800">{title}</div>'
        f'<div style="color:{JPSI_BLUE};font-size:13px;font-weight:600;margin-top:2px">'
        f'Manual rail FOB corridors · {datetime.now():%A, %B %d, %Y}</div></div>'
        f'<div style="padding:4px 2px 0">')

    imgs = {}
    _wm_path = _table_watermark()
    if _wm_path:
        imgs[_TBL_WM_CID] = _wm_path
    _tbl_attr = f' background="cid:{_TBL_WM_CID}"' if _wm_path else ""
    _tbl_css = (f"background-image:url('cid:{_TBL_WM_CID}');background-position:center;"
                f"background-repeat:no-repeat;background-size:contain;") if _wm_path else ""
    for idx, m in enumerate(_ordered):
        elig = sorted(mkt_dates[m])
        eff = elig[-1]
        cells = sorted(by_md.get((m, eff), {}).values(),
                       key=lambda r: (r.get("period_order") if r.get("period_order") is not None else 99))
        if not cells:
            continue
        rail = cells[0].get("rail") or ""
        rcol = _RAIL_COLORS.get(rail, "#64748b")
        pd_, pw, pmo, pyr = _prior_maps(m, eff, by_md, mkt_dates)
        aod = "" if eff == latest_overall else (
            f' <span style="font-size:9px;color:#fff;background:#d97706;padding:1px 5px;'
            f'border-radius:3px">as of {int(eff[5:7])}/{int(eff[8:10])}</span>')
        body += (f'<div style="margin-top:14px;margin-bottom:3px;font-family:Arial,sans-serif;'
                 f'font-size:12px;font-weight:700;color:{JPSI_DARK}">'
                 f'{_RAIL_DISPLAY.get(m, m)} <span style="font-size:9px;color:#fff;background:{rcol};'
                 f'padding:1px 5px;border-radius:3px">{rail}</span>{aod}</div>')
        body += (f'<table{_tbl_attr} style="border-collapse:collapse;width:100%;{_tbl_css}">'
                 f'<tr><th style="{thl}">Period</th><th style="{thl}">Fut</th>'
                 f'<th style="{thr}">Bid</th><th style="{thr}">Offer</th>'
                 f'<th style="{thr}">Δ Last</th><th style="{thr}">Δ Wk</th>'
                 f'<th style="{thr}">Δ Mo</th><th style="{thr}">Δ Yr</th></tr>')
        for c in cells:
            b = c.get("bid")
            cf = c.get("futures")
            body += (f'<tr><td style="{tdl};color:{JPSI_DARK};font-weight:600">{c["period"]}</td>'
                     f'<td style="{tdl};color:#94a3b8;font-size:10px">{cf or ""}</td>'
                     + _bo_cell(c, False, tdr) + _bo_cell(c, True, tdr)
                     + _chg_cell(b, pd_, c["period"], tdr, cf, _curve)
                     + _chg_cell(b, pw, c["period"], tdr, cf, _curve)
                     + _chg_cell(b, pmo, c["period"], tdr, cf, _curve)
                     + _chg_cell(b, pyr, c["period"], tdr, cf, _curve)
                     + '</tr>')
        body += '</table>'

        # Spot seasonal chart for this corridor (skipped if not enough history, or in
        # the lean update email where charts=False).
        if charts:
            png = _seasonal_png(m, by_md, mkt_dates)
            if png:
                cid = f"seas_{idx}"
                fh = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
                fh.write(png); fh.close()
                imgs[cid] = fh.name
                body += (f'<div style="margin:6px 0 2px"><img src="cid:{cid}" '
                         f'style="max-width:100%;border:1px solid #e2e8f0;border-radius:6px"></div>')

    body += (f'<div style="font-size:11px;color:#94a3b8;margin-top:10px;font-family:Arial,sans-serif">'
             'Rail FOB corn basis (¢/bu) vs the referenced futures. Δ = bid change vs the corridor\'s '
             'previous posting / ~1 week / ~1 month / ~1 year prior. Freight rows ($/car) show no futures.'
             '</div></div>')
    if os.path.exists(_SIG_LOGO):
        imgs[_SIG_LOGO_CID] = _SIG_LOGO
    return body + signature_html(), imgs


def send_rail_update_email(markets: list | None = None, to_addr: str | None = None) -> bool:
    """UPDATE email: just the corridors in `markets` (what was posted), each with its
    spot seasonal chart. `markets=None` falls back to the full board."""
    if markets is not None and not markets:
        log.info("Rail update email: no corridors to report — skipped.")
        return False
    n = len(markets) if markets else 0
    title = (f"Rail Basis Update · {n} corridor{'s' if n != 1 else ''}") if markets else "Rail Basis Update"
    subj  = (f"JSA Rail Update — {', '.join(markets)}"[:150]) if markets else SUBJECT
    html, imgs = build_rail_html(markets=markets, charts=True, title=title)
    _via = send_email(subj, html, to_addr or DEFAULT_TO, inline_images=imgs or None)
    log.info("Rail update email (%s) sent via %s to %s",
             ", ".join(markets) if markets else "full", _via, to_addr or DEFAULT_TO)
    return True


def send_rail_recap_email(to_addr: str | None = None) -> bool:
    """Full weekly RECAP: every active corridor + a spot seasonal chart each."""
    html, imgs = build_rail_html(markets=None, charts=True, title="Rail Basis Weekly Recap")
    _via = send_email("JSA Rail Basis — Weekly Recap", html, to_addr or DEFAULT_TO,
                      inline_images=imgs or None)
    log.info("Rail weekly recap emailed via %s to %s", _via, to_addr or DEFAULT_TO)
    return True


if __name__ == "__main__":
    import argparse, sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--send", action="store_true", help="send an update email")
    ap.add_argument("--recap", action="store_true", help="send the full weekly recap")
    ap.add_argument("--markets", default=None, help="comma-separated corridors for the update email")
    a = ap.parse_args()
    _mkts = [m.strip() for m in a.markets.split(",")] if a.markets else None
    if a.recap:
        send_rail_recap_email()
        print("recap sent")
    elif a.send:
        send_rail_update_email(markets=_mkts)
        print("sent")
    else:
        html, imgs = build_rail_html(markets=_mkts, charts=a.recap)
        open("_rail_preview.html", "w", encoding="utf-8").write("<html><body>" + html + "</body></html>")
        print(f"wrote _rail_preview.html ({len(html)} chars, {len(imgs)} inline image(s))")
