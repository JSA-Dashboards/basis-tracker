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
import tempfile
from datetime import datetime, date, timedelta

from changes_report import (signature_html, send_via_outlook, JPSI_DARK, JPSI_BLUE,
                            _GAIN, _LOSS, _SIG_LOGO, _SIG_LOGO_CID)
from database import get_rail_fob_all

log = logging.getLogger(__name__)

DEFAULT_TO = "kpostin@jpsi.com"
SUBJECT    = "JSA Rail Basis Update"
_SEAS_CID  = "rail_seasonal"
# Corridors to leave OFF the rail email (kept in the DB, just not reported).
_EXCLUDE = {"UP Illinois (Dom)"}     # Allen Station (Dom) — dropped 2026-08-24

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


def _chg_cell(cur_bid, prior_map, period, tdr):
    if cur_bid is None or not prior_map or prior_map.get(period) is None:
        return f'<td style="{tdr};color:#cbd5e1">—</td>'
    pb = prior_map[period].get("bid")
    if pb is None:
        return f'<td style="{tdr};color:#cbd5e1">—</td>'
    d = cur_bid - pb
    if d == 0:
        return f'<td style="{tdr};color:#94a3b8">0</td>'
    return f'<td style="{tdr};color:{_GAIN if d > 0 else _LOSS};font-weight:700">{d:+d}</td>'


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


def _seasonal_png(market, by_md, mkt_dates):
    """Render the corridor's spot seasonal (recent marketing years + 5-yr band) to PNG
    bytes via vl-convert, or None if not enough history / render unavailable."""
    series = _spot_series(market, by_md, mkt_dates)
    if len(series) < 8:
        return None
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
    mx = int(df["MyNum"].max())
    df = df[df["MyNum"] >= mx - 9]                      # last 10 marketing years (all if fewer)
    if df["MyNum"].nunique() < 2:
        return None

    cur = df[df["MyNum"] == mx]
    hist = df[df["MyNum"] < mx]
    layers = []
    if not hist.empty:
        layers.append(alt.Chart(hist).mark_line(strokeWidth=1.6, opacity=0.55).encode(
            x=alt.X("MktWeek:Q", scale=alt.Scale(domain=[1, 52]),
                    axis=alt.Axis(title=None, values=[1, 5, 10, 14, 18, 23, 27, 31, 36, 40, 45, 49],
                                  labelExpr=("{'1':'Sep','5':'Oct','10':'Nov','14':'Dec','18':'Jan',"
                                             "'23':'Feb','27':'Mar','31':'Apr','36':'May','40':'Jun',"
                                             "'45':'Jul','49':'Aug'}[datum.value]"))),
            y=alt.Y("Bid:Q", title="Spot basis (¢)", scale=alt.Scale(zero=False)),
            color=alt.Color("MktYear:N", scale=alt.Scale(scheme="tableau10"),
                            legend=alt.Legend(title="Mkt Year", orient="bottom", columns=6))))
    if not cur.empty:
        layers.append(alt.Chart(cur).mark_line(strokeWidth=4, color="#000000").encode(
            x="MktWeek:Q", y="Bid:Q"))
    chart = alt.layer(*layers).properties(
        width=640, height=225, padding={"left": 6, "right": 20, "top": 8, "bottom": 6},
        title=f"{_RAIL_DISPLAY.get(market, market)} · Spot basis seasonal")
    try:
        return vlc.vegalite_to_png(json.dumps(chart.to_dict(), default=str), scale=1.5)
    except Exception as exc:
        log.warning("rail seasonal PNG failed for %s: %s", market, exc)
        return None


# ── HTML ─────────────────────────────────────────────────────────────────────
def build_rail_html(seasonal_market: str | None = None) -> tuple[str, dict]:
    """(html, inline_images). inline_images maps cid->filepath for send_via_outlook."""
    by_md, mkt_dates = _load("manual")
    # Only corridors actively being posted — drop long-dead historical markets
    # (KC/KCS 2015, BN MN 2018, BN PNW Beans 2020, …) so the email stays current.
    _all_latest = max((max(ds) for ds in mkt_dates.values()), default=None)
    _cutoff = ((datetime.fromisoformat(_all_latest).date() - timedelta(days=21)).isoformat()
               if _all_latest else "0000")
    _active = [m for m, ds in mkt_dates.items() if max(ds) >= _cutoff and m not in _EXCLUDE]
    markets = sorted(_active, key=lambda m: (CORRIDOR_ORDER.get(m, 99), m))

    th  = ("background:#f1f5f9;color:#475569;font-size:9px;text-transform:uppercase;"
           "letter-spacing:.04em;padding:4px 7px;font-weight:700;border-bottom:2px solid #e2e8f0;"
           "font-family:Arial,sans-serif")
    thl = th + ";text-align:left"
    thr = th + ";text-align:right"
    tdl = "padding:3px 7px;font-family:Arial,sans-serif;font-size:12px;border-bottom:1px solid #eef2f6;text-align:left"
    tdr = tdl.replace("text-align:left", "text-align:right") + ";font-variant-numeric:tabular-nums"

    latest_overall = max((max(ds) for ds in mkt_dates.values()), default=None)
    body = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;color:{JPSI_DARK};max-width:820px">'
        f'<div style="background:{JPSI_DARK};padding:16px 20px;border-radius:8px 8px 0 0">'
        f'<div style="color:#fff;font-size:18px;font-weight:800">Rail Basis Update</div>'
        f'<div style="color:{JPSI_BLUE};font-size:13px;font-weight:600;margin-top:2px">'
        f'Manual rail FOB corridors · {datetime.now():%A, %B %d, %Y}</div></div>'
        f'<div style="padding:4px 2px 0">')

    imgs = {}
    for idx, m in enumerate(markets):
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
        body += ('<table style="border-collapse:collapse;width:100%">'
                 f'<tr><th style="{thl}">Period</th><th style="{thl}">Fut</th>'
                 f'<th style="{thr}">Bid</th><th style="{thr}">Offer</th>'
                 f'<th style="{thr}">Δ Last</th><th style="{thr}">Δ Wk</th>'
                 f'<th style="{thr}">Δ Mo</th><th style="{thr}">Δ Yr</th></tr>')
        for c in cells:
            b = c.get("bid")
            body += (f'<tr><td style="{tdl};color:{JPSI_DARK};font-weight:600">{c["period"]}</td>'
                     f'<td style="{tdl};color:#94a3b8;font-size:10px">{c.get("futures") or ""}</td>'
                     + _bo_cell(c, False, tdr) + _bo_cell(c, True, tdr)
                     + _chg_cell(b, pd_, c["period"], tdr) + _chg_cell(b, pw, c["period"], tdr)
                     + _chg_cell(b, pmo, c["period"], tdr) + _chg_cell(b, pyr, c["period"], tdr)
                     + '</tr>')
        body += '</table>'

        # Spot seasonal chart for this corridor (skipped if not enough history, or if a
        # single seasonal_market was requested and this isn't it).
        if seasonal_market is None or m == seasonal_market:
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


def send_rail_update_email(to_addr: str | None = None, seasonal_market: str | None = None) -> bool:
    html, imgs = build_rail_html(seasonal_market)
    send_via_outlook(SUBJECT, html, to_addr or DEFAULT_TO, inline_images=imgs or None)
    log.info("Rail Basis Update emailed to %s", to_addr or DEFAULT_TO)
    return True


if __name__ == "__main__":
    import argparse, sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--market", default=None, help="corridor for the seasonal chart")
    a = ap.parse_args()
    if a.send:
        send_rail_update_email(seasonal_market=a.market)
        print("sent")
    else:
        html, imgs = build_rail_html(a.market)
        open("_rail_preview.html", "w", encoding="utf-8").write("<html><body>" + html + "</body></html>")
        print(f"wrote _rail_preview.html ({len(html)} chars, {len(imgs)} inline image(s))")
