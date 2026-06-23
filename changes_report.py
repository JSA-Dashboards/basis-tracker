"""
changes_report.py — Standalone builder + emailer for the Daily Basis Changes report.

Reproduces the dashboard's "Changes" tab (build_changes_email_html in app.py) as
a self-contained module with NO Streamlit dependency, so the scheduled scraper
(auto_import.py) can build the same branded HTML and email it after each run.

Send path: Outlook desktop via win32com (no passwords / API keys / IT approval —
runs in the user's logged-in Windows session where Outlook is configured).

CLI:
    python changes_report.py --html       # print the HTML to stdout
    python changes_report.py --send        # build + email via Outlook
    python changes_report.py --send --to someone@jpsi.com
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

import delivery_period as _dp
from river_segments import river_segment, SEGMENT_ORDER
from adm_names import adm_city_from_name
from database import get_grain_map, get_bids_filter_data, get_snapshots_bulk

log = logging.getLogger(__name__)

DEFAULT_TO = os.getenv("CHANGES_EMAIL_TO", "kpostin@jpsi.com")

_CME_MONTH_TO_INT = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# ── JPSI brand (jpsi.com) ────────────────────────────────────────────────────
JPSI_DARK  = "#32373c"
JPSI_BLUE  = "#0693e3"
JPSI_LOGO  = "https://www.jpsi.com/wp-content/themes/gate39media/img/logo-white.png"
_GAIN, _LOSS = "#16a34a", "#dc2626"

TREND_CATEGORIES = [
    ("Corn Processing — Corn",     "Corn Processing", "Corn",     "region"),
    ("Rail Terminals — Corn",      "Rail Terminal",   "Corn",     "region"),
    ("River Terminals — Corn",     "River Terminal",  "Corn",     "segment"),
    ("Soy Processing — Soybeans",  "Soy Processing",  "Soybeans", "region"),
    ("River Terminals — Soybeans", "River Terminal",  "Soybeans", "segment"),
]

_GM: dict = {}


def _grain_disp(raw: str) -> str | None:
    """Canonical display name for a raw grain, or None if inactive."""
    entry = _GM.get(raw)
    if entry is None:
        return raw
    if not entry["is_active"]:
        return None
    cls  = entry.get("wheat_class")
    prot = entry.get("protein")
    base = entry["canonical_grain"]
    if cls:
        return f"{base} ({cls} {prot})" if prot else f"{base} ({cls})"
    return base


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


def _trend_closest(snaps, target, maxd):
    if not snaps:
        return None
    b = min(snaps, key=lambda s: abs((_trend_ts(s.timestamp) - target).total_seconds()))
    return b if abs((_trend_ts(b.timestamp) - target).total_seconds()) / 86400 <= maxd else None


def _load(facility_type: str):
    """Snapshot load + 'now' anchor for a facility type (uncached version of app._trend_load)."""
    global _GM
    if not _GM:
        _GM = get_grain_map()
    from collections import Counter as _C
    sl    = get_bids_filter_data()
    pairs = [(l["provider"], l["location"]) for l in sl if l.get("facility_type") == facility_type]
    data  = get_snapshots_bulk(pairs, since_days=400) if pairs else {}
    today_noon = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    loc_latest = []
    for snaps in data.values():
        ds = [d for s in snaps if (d := _trend_ts(s.timestamp)) <= today_noon]
        if ds:
            loc_latest.append(max(ds).date())
    # Anchor on the LATEST date reached (≤ today), not the most common, so a provider
    # that's a day ahead of the others still surfaces its fresh change.
    now = (datetime(*max(loc_latest).timetuple()[:3], 12) if loc_latest else today_noon)
    return pairs, data, now


def _curve_map(snap, grain):
    """{canonical (year, month) -> basis_cents} for a grain's forward rows (nearest slot)."""
    m, best = {}, {}
    for r in snap.rows:
        if (r.isSpot or _grain_disp(r.grain) != grain
                or r.basisCents is None or not r.futuresSymbol):
            continue
        k  = _dp.canonical(r.deliveryMonth, r.futuresSymbol)
        sk = _dp.slot_key(r.deliveryMonth)
        if k not in best or sk < best[k]:
            best[k], m[k] = sk, r.basisCents
    return m


def _curve_metrics(cur_snap, prior, grain):
    """Avg basis change + firmer/weaker month counts across matched delivery months."""
    if cur_snap is None or prior is None:
        return None
    cm, pm = _curve_map(cur_snap, grain), _curve_map(prior, grain)
    deltas = [cm[k] - pm[k] for k in cm if k in pm]
    if not deltas:
        return None
    return {"avg":    sum(deltas) / len(deltas),
            "n_up":   sum(1 for d in deltas if d > 0),
            "n_down": sum(1 for d in deltas if d < 0)}


def build_change_rows(facility_type: str, grain: str, mode: str = "spot") -> list[dict]:
    """Locations whose spot OR forward curve moved vs the prior posting. Each row:
    provider, location, basis (spot), change (spot Δ), spot_changed, curve_avg,
    n_up, n_down. Sorted firmest spot → weakest."""
    pairs, data, now = _load(facility_type)
    out = []
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
        prev     = _trend_extract(prior, grain, mode)
        spot_chg = (cur - prev) if prev is not None else 0
        cm       = _curve_metrics(cur_snap, prior, grain)
        n_up     = cm["n_up"]   if cm else 0
        n_down   = cm["n_down"] if cm else 0
        if spot_chg == 0 and (n_up + n_down) == 0:
            continue
        out.append({"provider": key[0], "location": key[1], "basis": cur,
                    "change": spot_chg, "spot_changed": spot_chg != 0,
                    "curve_avg": (cm["avg"] if cm else None),
                    "n_up": n_up, "n_down": n_down})
    out.sort(key=lambda r: (-r["change"], r["provider"], r["location"]))
    return out


def build_segment_change_rows(facility_type: str, grain: str, mode: str = "spot") -> list[dict]:
    """Per river-segment avg basis and avg day-over-day change (firmer = positive)."""
    pairs, data, now = _load(facility_type)
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


def _tally_html(n_up: int, n_down: int) -> str:
    """Compact firmer/weaker month tally, e.g. '6▲ 1▼'."""
    parts = []
    if n_up:
        parts.append(f'<span style="color:{_GAIN}">{n_up}&#9650;</span>')
    if n_down:
        parts.append(f'<span style="color:{_LOSS}">{n_down}&#9660;</span>')
    return '&nbsp;'.join(parts) if parts else '<span style="color:#94a3b8">—</span>'


def _curve_cell_html(avg) -> str:
    """Average curve change, colored, muted weight."""
    if avg is None or round(avg, 1) == 0:
        return '<span style="color:#94a3b8">—</span>'
    return f'<span style="color:{_GAIN if avg > 0 else _LOSS}">{avg:+.1f}</span>'


def build_changes_email_html(mode: str = "spot") -> str:
    """A branded, email-ready HTML report of daily basis changes (JPSI styling)."""
    today = datetime.now()
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
            rows       = build_change_rows(ft, gr, mode)
            spot_rows  = [r for r in rows if r["spot_changed"]]
            curve_only = sorted([r for r in rows if not r["spot_changed"]],
                                key=lambda r: -(r["curve_avg"] or 0))
            if not spot_rows and not curve_only:
                body += '<div style="font-size:12px;color:#94a3b8;padding:2px 6px">No changes today.</div>'
                continue
            if spot_rows:
                body += ('<table width="100%" style="border-collapse:collapse;font-size:12px">'
                         f'<tr><td style="{_hdr}">Location</td>'
                         f'<td align="right" style="{_hdr}">Basis</td>'
                         f'<td align="right" style="{_hdr}">Δ Spot</td>'
                         f'<td align="right" style="{_hdr}">Δ Curve</td>'
                         f'<td align="right" style="{_hdr}">Months</td></tr>')
                for i, r in enumerate(spot_rows):
                    bg  = "#f4f9fd" if i % 2 else "#ffffff"
                    loc = adm_city_from_name(r["location"]) if r["provider"] == "ADM" else r["location"]
                    c   = r["change"]
                    body += (f'<tr style="background:{bg}">'
                             f'<td style="padding:3px 6px;color:{JPSI_DARK}">'
                             f'<b style="color:{JPSI_DARK}">{r["provider"]}</b> {loc}</td>'
                             f'<td align="right" style="padding:3px 6px;color:{JPSI_DARK};font-weight:600">{r["basis"]:+d}</td>'
                             f'<td align="right" style="padding:3px 6px;color:{_GAIN if c > 0 else _LOSS};font-weight:700">{c:+d}</td>'
                             f'<td align="right" style="padding:3px 6px">{_curve_cell_html(r["curve_avg"])}</td>'
                             f'<td align="right" style="padding:3px 6px;font-size:11px">{_tally_html(r["n_up"], r["n_down"])}</td>'
                             f'</tr>')
                body += '</table>'
            if curve_only:
                body += ('<div style="margin:7px 0 1px;font-size:10px;color:#94a3b8;'
                         'text-transform:uppercase;letter-spacing:.05em">Curve shifts · spot unchanged</div>')
                body += ('<table width="100%" style="border-collapse:collapse;font-size:12px">'
                         f'<tr><td style="{_hdr}">Location</td>'
                         f'<td align="right" style="{_hdr}">Basis</td>'
                         f'<td align="right" style="{_hdr}">Δ Curve</td>'
                         f'<td align="right" style="{_hdr}">Months</td></tr>')
                for i, r in enumerate(curve_only):
                    bg  = "#f4f9fd" if i % 2 else "#ffffff"
                    loc = adm_city_from_name(r["location"]) if r["provider"] == "ADM" else r["location"]
                    body += (f'<tr style="background:{bg}">'
                             f'<td style="padding:3px 6px;color:#64748b">'
                             f'<b style="color:#64748b">{r["provider"]}</b> {loc}</td>'
                             f'<td align="right" style="padding:3px 6px;color:#64748b">{r["basis"]:+d}</td>'
                             f'<td align="right" style="padding:3px 6px">{_curve_cell_html(r["curve_avg"])}</td>'
                             f'<td align="right" style="padding:3px 6px;font-size:11px">{_tally_html(r["n_up"], r["n_down"])}</td>'
                             f'</tr>')
                body += '</table>'

    return (
        f'<div style="max-width:680px;margin:0;{_ff};border:1px solid #e2e8f0;'
        f'border-radius:8px;overflow:hidden">'
        f'<table width="100%" style="background:{JPSI_DARK};border-collapse:collapse"><tr>'
        f'<td style="padding:14px 18px"><img src="{JPSI_LOGO}" '
        f'alt="John Stewart &amp; Associates" height="30" style="display:block;height:30px"></td>'
        f'<td align="right" style="padding:14px 18px;color:#ffffff">'
        f'<div style="font-size:16px;font-weight:700">Daily Basis Changes</div>'
        f'<div style="font-size:12px;color:#cbd5e1">{today.day} {today.strftime("%b %Y")} '
        f'· spot + curve vs prior posting</div></td></tr></table>'
        f'<div style="padding:8px 18px 14px;background:#ffffff">{body}</div>'
        f'<div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:10px 18px;'
        f'font-size:11px;color:#64748b">John Stewart &amp; Associates · '
        f'Commodity &amp; Ag Risk Management Specialists · '
        f'<a href="https://www.jpsi.com" style="color:{JPSI_BLUE};text-decoration:none">jpsi.com</a></div>'
        f'</div>'
    )


def send_via_outlook(subject: str, html: str, to_addr: str, cc: str | None = None) -> None:
    """Send an HTML email through the local, logged-in Outlook desktop app."""
    import win32com.client as win32  # pywin32 (local/dev only)
    outlook = win32.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = to_addr
    if cc:
        mail.CC = cc
    mail.Subject = subject
    mail.HTMLBody = html
    mail.Send()


def send_daily_changes_email(to_addr: str | None = None, cc: str | None = None,
                             mode: str = "spot") -> bool:
    """Build the daily Changes report and email it via Outlook. Returns True on success.

    `cc` defaults to the CHANGES_EMAIL_CC env var (so a standing copy recipient can
    be configured for the scheduled daily send)."""
    to_addr = to_addr or DEFAULT_TO
    if cc is None:
        cc = os.getenv("CHANGES_EMAIL_CC") or None
    html = build_changes_email_html(mode)
    subject = f"Daily Basis Changes — {datetime.now():%b %d, %Y}"
    send_via_outlook(subject, html, to_addr, cc=cc)
    log.info("Daily Changes email sent to %s (cc %s)", to_addr, cc or "—")
    return True


if __name__ == "__main__":
    import argparse
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser(description="Daily Basis Changes report")
    ap.add_argument("--html", action="store_true", help="Print the report HTML to stdout")
    ap.add_argument("--send", action="store_true", help="Email the report via Outlook")
    ap.add_argument("--to", default=None, help=f"Recipient (default: {DEFAULT_TO})")
    a = ap.parse_args()
    if a.send:
        send_daily_changes_email(a.to)
        print("Sent.")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(build_changes_email_html())
