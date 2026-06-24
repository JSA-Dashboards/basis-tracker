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


_FULL_MON = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]


def build_change_rows(facility_type: str, grain: str, mode: str = "spot") -> dict:
    """Day-over-day basis change at the category's two nearest delivery months.

    Returns {m1_label, m2_label, rows}. m1 = the category's nearest delivery month
    (e.g. 'June'), m2 = the next ('July'); both roll forward over time. Each row:
    provider, location, b1/c1 (basis & daily change at m1), b2/c2 (at m2). A
    location is included if it moved at m1 OR m2.
    """
    from collections import Counter
    pairs, data, now = _load(facility_type)
    locs = []
    for key in pairs:
        snaps    = data.get(key, [])
        cur_snap = _trend_closest(snaps, now, 1.6)
        if cur_snap is None:
            continue
        ref_t = _trend_ts(cur_snap.timestamp)
        prior = None
        for s in snaps:
            t = _trend_ts(s.timestamp)
            if t < ref_t and (prior is None or t > _trend_ts(prior.timestamp)):
                prior = s
        cm = _curve_map(cur_snap, grain)
        if not cm:
            continue
        pm = _curve_map(prior, grain) if prior is not None else {}
        locs.append((key[0], key[1], cm, pm))
    if not locs:
        return {"m1_label": None, "m2_label": None, "rows": []}

    m1 = Counter(min(cm) for _, _, cm, _ in locs).most_common(1)[0][0]
    nxt = Counter()
    for _, _, cm, _ in locs:
        after = [k for k in sorted(cm) if k > m1]
        if after:
            nxt[after[0]] += 1
    m2 = nxt.most_common(1)[0][0] if nxt else None

    rows = []
    for prov, loc, cm, pm in locs:
        b1 = cm.get(m1)
        c1 = (cm[m1] - pm[m1]) if (m1 in cm and m1 in pm) else None
        b2 = cm.get(m2) if m2 else None
        c2 = (cm[m2] - pm[m2]) if (m2 is not None and m2 in cm and m2 in pm) else None
        if (c1 or 0) == 0 and (c2 or 0) == 0:
            continue
        rows.append({"provider": prov, "location": loc,
                     "b1": b1, "c1": c1, "b2": b2, "c2": c2})
    rows.sort(key=lambda r: (-(r["c1"] or 0), -(r["c2"] or 0), r["provider"], r["location"]))
    return {"m1_label": (_FULL_MON[m1[1]] if m1 else None),
            "m2_label": (_FULL_MON[m2[1]] if m2 else None),
            "rows": rows}


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


def _bcell(b) -> str:
    """Basis cell."""
    td = "padding:3px 6px;text-align:right;white-space:nowrap"
    if b is None:
        return f'<td style="{td};color:#cbd5e1">—</td>'
    return f'<td style="{td};color:{JPSI_DARK};font-weight:700">{b:+d}</td>'


def _ccell(c) -> str:
    """Daily-change cell (colored; — when unchanged or no prior)."""
    td = "padding:3px 6px;text-align:right;white-space:nowrap"
    if c is None or c == 0:
        return f'<td style="{td};color:#cbd5e1">—</td>'
    return f'<td style="{td};color:{_GAIN if c > 0 else _LOSS};font-weight:700">{c:+d}</td>'


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
            result = build_change_rows(ft, gr, mode)
            rows = result["rows"]
            if not rows:
                body += '<div style="font-size:12px;color:#94a3b8;padding:2px 6px">No changes today.</div>'
                continue
            _h2 = ("font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;"
                   "padding:2px 6px;text-align:right")
            body += ('<table width="100%" style="border-collapse:collapse;font-size:12px">'
                     f'<tr><td style="{_hdr}" rowspan="2">Location</td>'
                     f'<td style="{_hdr};text-align:center" colspan="2">{result["m1_label"] or ""}</td>'
                     f'<td style="{_hdr};text-align:center" colspan="2">{result["m2_label"] or ""}</td></tr>'
                     f'<tr><td style="{_h2}">Basis</td><td style="{_h2}">Δ</td>'
                     f'<td style="{_h2}">Basis</td><td style="{_h2}">Δ</td></tr>')
            for i, r in enumerate(rows):
                bg  = "#f4f9fd" if i % 2 else "#ffffff"
                loc = adm_city_from_name(r["location"]) if r["provider"] == "ADM" else r["location"]
                body += (f'<tr style="background:{bg}">'
                         f'<td style="padding:3px 6px;color:{JPSI_DARK}">'
                         f'<b style="color:{JPSI_DARK}">{r["provider"]}</b> {loc}</td>'
                         + _bcell(r["b1"]) + _ccell(r["c1"]) + _bcell(r["b2"]) + _ccell(r["c2"]) + '</tr>')
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
        f'· nearest & next delivery month vs prior posting</div></td></tr></table>'
        f'<div style="padding:8px 18px 14px;background:#ffffff">{body}</div>'
        f'<div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:10px 18px;'
        f'font-size:11px;color:#64748b">John Stewart &amp; Associates · '
        f'Commodity &amp; Ag Risk Management Specialists · '
        f'<a href="https://www.jpsi.com" style="color:{JPSI_BLUE};text-decoration:none">jpsi.com</a></div>'
        f'</div>'
    )


_SIG_LOGO     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "50 Year logo JSA.png")
_SIG_LOGO_CID = "jsa50yr"
_TEAMS_URL    = os.getenv("SIG_TEAMS_URL",
                          "https://teams.microsoft.com/l/chat/0/0?users=kpostin@jpsi.com")
_WHATSAPP_URL = os.getenv("SIG_WHATSAPP_URL", "https://wa.me/18776711670")
_DISCLAIMER = (
    "Trading commodity futures, options on futures, cash commodities, and over-the-counter "
    "derivative products involves substantial risk of loss and may not be suitable for all "
    "investors. This communication is provided for informational purposes only and does not "
    "constitute investment advice, a recommendation, or an offer or solicitation to buy or sell "
    "any futures, options, cash commodities, or derivative products. John Stewart &amp; "
    "Associates, Inc. does not accept orders to buy or sell any financial instruments via email. "
    "The information contained herein has been obtained from sources believed to be reliable; "
    "however, its accuracy and completeness are not guaranteed. Any opinions expressed are solely "
    "those of the author, are subject to change without notice, and should not be relied upon as a "
    "basis for investment decisions. Past performance is not indicative of future results. This "
    "message may contain confidential or proprietary information intended solely for the use of the "
    "designated recipient. &copy; John Stewart &amp; Associates, Inc. 2026"
)


def signature_html() -> str:
    """Kolten Postin's JSA email signature (logo via cid:, disclaimer included)."""
    logo = (f'<img src="cid:{_SIG_LOGO_CID}" alt="John Stewart &amp; Associates - 50 Years" '
            f'height="64" style="height:64px;display:block;margin:8px 0 6px">'
            if os.path.exists(_SIG_LOGO) else "")
    b = "font-weight:bold"
    return (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#32373c;'
        f'margin-top:20px;border-top:1px solid #e2e8f0;padding-top:12px">'
        f'<div style="{b};font-size:13px">Kolten A. Postin</div>'
        f'<div style="color:#5b6770">John Stewart &amp; Associates</div>'
        f'<div style="{b}">Commodity Broker</div>'
        f'<div style="{b}">Phone 877-671-1670</div>'
        f'<div style="{b}">Email: <a href="mailto:kpostin@jpsi.com" '
        f'style="color:{JPSI_BLUE};text-decoration:none">kpostin@jpsi.com</a></div>'
        f'<div style="{b};margin-top:8px"><a href="{_TEAMS_URL}" '
        f'style="color:{JPSI_BLUE};text-decoration:none">MS TEAMS: Chat with me on Microsoft Teams</a></div>'
        f'<div style="{b}"><a href="{_WHATSAPP_URL}" '
        f'style="color:{JPSI_BLUE};text-decoration:none">WhatsApp: Chat with me on WhatsApp Here</a></div>'
        f'{logo}'
        f'<div style="font-size:9px;color:#9aa0a6;line-height:1.45;margin-top:10px;'
        f'max-width:680px">{_DISCLAIMER}</div>'
        f'</div>'
    )


def send_via_outlook(subject: str, html: str, to_addr: str, cc: str | None = None,
                     inline_images: dict | None = None) -> None:
    """Send an HTML email via the local, logged-in Outlook desktop app.

    `inline_images` is an optional {content_id: filepath} map for images referenced
    in the HTML as <img src="cid:content_id">.
    """
    import win32com.client as win32  # pywin32 (local/dev only)
    outlook = win32.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = to_addr
    if cc:
        mail.CC = cc
    mail.Subject = subject
    mail.HTMLBody = html
    for cid, path in (inline_images or {}).items():
        if os.path.exists(path):
            att = mail.Attachments.Add(path)
            att.PropertyAccessor.SetProperty(
                "http://schemas.microsoft.com/mapi/proptag/0x3712001F", cid)
    mail.Send()


def send_daily_changes_email(to_addr: str | None = None, cc: str | None = None,
                             mode: str = "spot") -> bool:
    """Build the daily Changes report and email it via Outlook. Returns True on success.

    `cc` defaults to the CHANGES_EMAIL_CC env var (so a standing copy recipient can
    be configured for the scheduled daily send)."""
    to_addr = to_addr or DEFAULT_TO
    if cc is None:
        cc = os.getenv("CHANGES_EMAIL_CC") or None
    html = build_changes_email_html(mode) + signature_html()
    subject = f"JSA Daily Basis Changes - {datetime.now():%b %d, %Y}"
    imgs = {_SIG_LOGO_CID: _SIG_LOGO} if os.path.exists(_SIG_LOGO) else None
    send_via_outlook(subject, html, to_addr, cc=cc, inline_images=imgs)
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
