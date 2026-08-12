"""
client_report.py — personalized per-client basis report (branded HTML + email).

Each client (row in the `client_reports` table) has a hand-picked list of
(provider, location, grain) plus a cadence. This module builds a branded email
for one client — current spot basis, Day/Week/Month change, and a trend arrow per
location — and sends it via the same Outlook path the daily Changes report uses.

The scheduler (`send_due_reports`) runs after the daily scrape and mails every
active client whose cadence matches today.

    python client_report.py --list                 # list subscriptions
    python client_report.py --preview <id>          # print one client's HTML
    python client_report.py --send-due              # mail everyone due today
    python client_report.py --send <id>             # mail one client now
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from changes_report import (_trend_extract, _trend_closest, _trend_ts, _curve_map,
                            _grain_disp, signature_html, send_via_outlook,
                            JPSI_DARK, JPSI_BLUE, _GAIN, _LOSS,
                            _SIG_LOGO, _SIG_LOGO_CID)
import delivery_period as _dp
from database import get_snapshots_bulk, get_client_reports

log = logging.getLogger(__name__)


def _ensure_grain_map() -> None:
    """`_grain_disp` reads changes_report's module-global `_GM`, which is only
    populated as a side effect of the Changes report running. In the client-report
    path (nightly send or the admin Preview) nothing else loads it, so without this
    the grain names come back RAW ("Yellow Corn") and never match a client's
    canonical commodity filter ("Corn"). Load it once, here."""
    import changes_report as _ch
    if not _ch._GM:
        from database import get_grain_map
        _ch._GM = get_grain_map()


# ── per-location-grain forward curve: every delivery period + Day/Week/Month change ──
def _grains_in(snap) -> list:
    """Display grains that this snapshot actually posts a basis for, in first-seen order."""
    out, seen = [], set()
    for r in snap.rows:
        if r.basisCents is None:
            continue
        try:
            g = _grain_disp(r.grain)
        except Exception:               # never let one odd grain crash the whole report
            g = r.grain
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _periods_for(prov: str, loc: str, grain: str, cur, snaps: list, depth: str) -> dict:
    """One (location, grain) block: a Spot row plus — unless depth='spot' — the full
    forward curve, each period carrying its own Day/Wk/Mo change vs the same slot."""
    as_of  = _trend_ts(cur.timestamp).date()
    now_dt = datetime(*as_of.timetuple()[:3], 12)
    older  = [s for s in snaps if _trend_ts(s.timestamp) < now_dt]

    def _past(days, maxd):
        return _trend_closest(older, now_dt - timedelta(days=days), maxd)
    p_d, p_w, p_m = _past(1, 2.0), _past(7, 4.5), _past(30, 14.0)

    periods = []
    spot = _trend_extract(cur, grain, "spot")
    if spot is not None:
        def _sdelta(past):
            pb = _trend_extract(past, grain, "spot") if past else None
            return (spot - pb) if (pb is not None) else None
        periods.append({"label": "Spot", "basis": spot,
                        "d": _sdelta(p_d), "w": _sdelta(p_w), "m": _sdelta(p_m)})

    if depth != "spot":
        cur_curve = _curve_map(cur, grain)
        d_curve = _curve_map(p_d, grain) if p_d else {}
        w_curve = _curve_map(p_w, grain) if p_w else {}
        m_curve = _curve_map(p_m, grain) if p_m else {}

        def _delta(b, key, past_curve):
            pb = past_curve.get(key)
            return (b - pb) if (b is not None and pb is not None) else None

        for key in sorted(cur_curve):
            b = cur_curve[key]
            periods.append({"label": _dp.label(key), "basis": b,
                            "d": _delta(b, key, d_curve),
                            "w": _delta(b, key, w_curve),
                            "m": _delta(b, key, m_curve)})
    return {"provider": prov, "location": loc, "grain": grain,
            "as_of": as_of, "periods": periods}


def _rows_for(client: dict) -> list[dict]:
    """One block per (subscribed location × commodity). Commodities empty → every
    commodity the location posts; depth 'spot' → just the cash bid, else full curve."""
    _ensure_grain_map()
    locations   = client.get("locations", []) or []
    commodities = client.get("commodities") or []          # empty = all commodities
    depth       = (client.get("depth") or "curve").lower()
    want = {str(c).lower() for c in commodities}

    # De-dupe to (provider, location) in the order the client listed them.
    pairs, seen = [], set()
    for l in locations:
        key = (l["provider"], l["location"])
        if key not in seen:
            seen.add(key)
            pairs.append(key)

    data = get_snapshots_bulk(sorted(pairs), since_days=45) if pairs else {}
    today_noon = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)

    blocks = []
    for prov, loc in pairs:
        snaps = data.get((prov, loc), [])
        cur_snaps = [s for s in snaps if _trend_ts(s.timestamp) <= today_noon]
        cur = max(cur_snaps, key=lambda s: _trend_ts(s.timestamp)) if cur_snaps else None
        if cur is None:
            blocks.append({"provider": prov, "location": loc, "grain": None,
                           "as_of": None, "periods": []})
            continue
        grains = _grains_in(cur)
        if want:
            grains = [g for g in grains if g.lower() in want]
        for g in grains:
            blocks.append(_periods_for(prov, loc, g, cur, snaps, depth))
    return blocks


# ── cell formatters ─────────────────────────────────────────────────────────
def _bcell(b) -> str:
    if b is None:
        return '<span style="color:#94a3b8">—</span>'
    return f'{"+" if b >= 0 else ""}{b}¢'


def _ccell(c) -> str:
    if c is None:
        return '<span style="color:#cbd5e1">—</span>'
    if c == 0:
        return '<span style="color:#64748b">0</span>'
    col = _GAIN if c > 0 else _LOSS
    return f'<span style="color:{col};font-weight:700">{"+" if c > 0 else ""}{c}</span>'


def _trend_cell(row) -> str:
    v = row["w"] if row["w"] is not None else row["d"]
    if v is None:
        return '<span style="color:#cbd5e1">—</span>'
    if v > 0:
        return f'<span style="color:{_GAIN};font-weight:700">▲</span>'
    if v < 0:
        return f'<span style="color:{_LOSS};font-weight:700">▼</span>'
    return '<span style="color:#64748b">→</span>'


def build_client_html(client: dict) -> str:
    locs = _rows_for(client)
    th = ("background:#f1f5f9;color:#475569;font-size:11px;text-transform:uppercase;"
          "letter-spacing:.03em;padding:7px 10px;text-align:left;font-weight:700;"
          "border-bottom:2px solid #e2e8f0;font-family:Arial,sans-serif")
    thr = th.replace("text-align:left", "text-align:right")
    td = "padding:6px 10px;font-family:Arial,sans-serif;font-size:13px;border-bottom:1px solid #eef2f6"
    tdr = td + ";text-align:right;font-variant-numeric:tabular-nums"
    grp = (f"background:{JPSI_DARK};color:#fff;font-family:Arial,sans-serif;font-size:12px;"
           "font-weight:700;letter-spacing:.02em;padding:8px 10px")
    body = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;color:{JPSI_DARK};max-width:760px">'
        f'<div style="background:{JPSI_DARK};padding:16px 20px;border-radius:8px 8px 0 0">'
        f'<div style="color:#fff;font-size:18px;font-weight:800;letter-spacing:.01em">'
        f'Daily Basis Update</div>'
        f'<div style="color:{JPSI_BLUE};font-size:13px;font-weight:600;margin-top:2px">'
        f'Prepared for {client["client_name"]} · {datetime.now():%A, %B %d, %Y}</div></div>'
        f'<table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0;'
        f'border-top:none">'
        f'<thead><tr><th style="{th}">Delivery</th>'
        f'<th style="{thr}">Basis</th><th style="{thr}">Day</th>'
        f'<th style="{thr}">Week</th><th style="{thr}">Month</th>'
        f'<th style="{thr}">Trend</th></tr></thead><tbody>')
    for loc in locs:
        _ao = loc.get("as_of")
        aod = f' &middot; as of {_ao.month}/{_ao.day}' if _ao else ""
        gtxt = f' &mdash; {loc["grain"]}' if loc.get("grain") else ""
        head = f'{loc["provider"]} &middot; {loc["location"]}{gtxt}{aod}'
        body += f'<tr><td colspan="6" style="{grp}">{head}</td></tr>'
        periods = loc.get("periods") or []
        if not periods:
            body += (f'<tr><td colspan="6" style="{td};color:#94a3b8;font-style:italic">'
                     'No recent bids posted.</td></tr>')
            continue
        for i, r in enumerate(periods):
            bg = "#f8fafc" if i % 2 else "#ffffff"
            body += (
                f'<tr style="background:{bg}">'
                f'<td style="{td};font-weight:600">{r["label"]}</td>'
                f'<td style="{tdr};font-weight:700">{_bcell(r["basis"])}</td>'
                f'<td style="{tdr}">{_ccell(r["d"])}</td>'
                f'<td style="{tdr}">{_ccell(r["w"])}</td>'
                f'<td style="{tdr}">{_ccell(r["m"])}</td>'
                f'<td style="{tdr}">{_trend_cell(r)}</td></tr>')
    body += (
        '</tbody></table>'
        f'<div style="font-size:11px;color:#94a3b8;margin-top:8px;font-family:Arial,sans-serif">'
        'Cash basis (¢/bu) vs the referenced futures, by delivery period. Δ = change vs '
        '~1 day / ~1 week / ~1 month prior for that same delivery slot. Trend reflects the '
        '~1-week direction. Cash bids are 10-minute delayed and subject to change.</div></div>')
    return body + signature_html()


# ── cadence ──────────────────────────────────────────────────────────────────
def _first_business_day(d: "datetime.date") -> "datetime.date":
    from datetime import date as _date
    x = _date(d.year, d.month, 1)
    while x.weekday() >= 5:
        x += timedelta(days=1)
    return x


def is_due(client: dict, today: "datetime.date") -> bool:
    """Whether a client should receive their report today (grain markets = weekdays)."""
    freq = (client.get("frequency") or "").lower()
    if today.weekday() >= 5:            # Sat/Sun — markets closed, never send
        return False
    if freq == "daily":
        return True
    if freq == "weekly":
        return today.weekday() == int(client.get("day_of_week") or 0)
    if freq == "monthly":
        return today == _first_business_day(today)
    return False


# ── send ─────────────────────────────────────────────────────────────────────
def send_client_report(client: dict) -> bool:
    if not client.get("locations"):
        log.warning("Client %s has no locations — skipped.", client.get("client_name"))
        return False
    html = build_client_html(client)
    subject = f"JSA Daily Basis Update - {datetime.now():%b %d, %Y}"
    imgs = {_SIG_LOGO_CID: _SIG_LOGO} if os.path.exists(_SIG_LOGO) else None
    send_via_outlook(subject, html, client["email"], cc=client.get("cc") or None,
                     inline_images=imgs)
    log.info("Client report sent to %s <%s>", client["client_name"], client["email"])
    return True


def send_due_reports(today: "datetime.date | None" = None) -> int:
    """Mail every active client whose cadence matches today. Returns count sent."""
    from datetime import date as _date
    today = today or _date.today()
    sent = 0
    for client in get_client_reports(active_only=True):
        if not is_due(client, today):
            continue
        try:
            if send_client_report(client):
                sent += 1
        except Exception as exc:
            log.error("Client report failed for %s: %s", client.get("client_name"), exc)
    log.info("Client reports: %d sent for %s", sent, today)
    return sent


if __name__ == "__main__":
    import argparse, sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser(description="Personalized client basis reports")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--preview", metavar="ID")
    ap.add_argument("--send", metavar="ID")
    ap.add_argument("--send-due", action="store_true")
    a = ap.parse_args()
    reports = {r["id"]: r for r in get_client_reports()}
    if a.list:
        for r in reports.values():
            print(f'  {r["id"][:8]}  {r["client_name"]:24} {r["email"]:28} '
                  f'{r["frequency"]:8} {len(r["locations"])} locs '
                  f'{"active" if r["active"] else "off"}')
    elif a.preview:
        print(build_client_html(reports[a.preview]))
    elif a.send:
        send_client_report(reports[a.send])
    elif a.send_due:
        send_due_reports()
