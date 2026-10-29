"""
inco_report.py - "JSA Inco Bid Update" email: Incobrasa (Gilman, IL) soybean-processor bids.

Kolten 2026-10-26, after pasting an Inco sheet: "Can we make an auto email like we do on rail when Inco update occurs. Put the normal table of bids and trends and then a seasonal
chart", then "setup to send to soyprocessing@jpsi.com". Built on the rail update email (rail_report): the posted sheet as a table (period, futures, bid, change vs the previous
sheet / ~1 week / ~1 month / ~1 year), then the spot seasonal chart - rail_report's own chart code, handed INCO's history in the shape it reads.

It goes out from `inco_bids.py post --commit`, the one way a new sheet gets in (there is no entry tab and no second writer, so no catch-up job like the rail one): --no-email switches
it off, and a send that fails leaves the sheet saved and prints the resend command.

    python inco_report.py --preview [--date YYYY-MM-DD]          # writes _inco_preview.html and _inco_seasonal.png; sends nothing
    python inco_report.py --send [--date D] [--to ADDR] [--force]

Recipients: To kpostin@jpsi.com (INCO_EMAIL_TO overrides) with soyprocessing@jpsi.com on BCC (INCO_EMAIL_BCC overrides) - the rail email's pattern; Kolten 2026-10-29, after the first list send had gone To the list: "We need the Soy Processing email address to be in BCC please". A send to anyone else (--to) is a TEST: it is logged as kind 'test' and does not count as
the sheet having been emailed. `inco_email_log` remembers each emailed sheet by a signature of its date and rows, so re-running a post does not mail the list twice; a corrected sheet
(different bids) is a new signature and goes out again.

Only a real sheet is a reason to email. The daily carry-forward and the H&F fold change what a day shows without a new sheet, and neither calls this.
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import tempfile
from datetime import date, datetime, timedelta, timezone
from html import escape as _esc

import database as db
import delivery_period as dp
from changes_report import (JPSI_BLUE, JPSI_DARK, _GAIN, _LOSS, _SIG_LOGO, _SIG_LOGO_CID, _TBL_WM_CID, _futures_curve, _roll_adjust,
                            _table_watermark, dashboard_cta_html, send_email, signature_html)
from inco_bids import CARRY_PREFIX, GRAIN, LOC, PROV, today_ct

log = logging.getLogger(__name__)

DEFAULT_TO = "kpostin@jpsi.com"
DEFAULT_BCC = "soyprocessing@jpsi.com"          # the Soy Processing list rides on BCC, never on the To line
SUBJECT = "JSA Inco Bid Update"
MARKET = "Inco (Gilman, IL)"          # the name rail_report's seasonal chart is handed; it is the chart's title
SEAS_CID = "inco_seasonal"
MIN_YEAR_WEEKS = 40                   # a prior marketing year sits behind the band only with this many weekly values (of 52): INCO's history opens 2023-01, so its 2022/23 is a part-year


# -- reading the series ---------------------------------------------------------
def load_snaps() -> list:
    """Every INCO / Gilman snapshot, oldest first."""
    return sorted(db.get_snapshots(PROV, LOC), key=lambda s: str(s.timestamp))


def day_of(s) -> date:
    return date.fromisoformat(str(s.timestamp)[:10])


def is_carried(s) -> bool:
    """A day that repeats an earlier sheet (inco_bids marks it in the subject), not a sheet Kolten posted."""
    return str(getattr(s, "emailSubject", None) or "").startswith(CARRY_PREFIX)


def forward_rows(s) -> list:
    """The snapshot's posted delivery months with a bid, nearest first. The older weekly history holds one 'Spot' row instead (isSpot), which is not one of these."""
    rows = [r for r in s.rows if not r.isSpot and r.basisCents is not None]
    return sorted(rows, key=lambda r: dp.deliv_key(r.deliveryMonth, r.futuresSymbol))


def month_map(s) -> dict:
    """{(year, month): (basis, futures)} of the snapshot's forward rows: the nearest slot of each delivery month (a plain 'Oct' beats nothing, 'FH Oct' beats both), months already
    past on the snapshot's own date dropped (a lingering stale row)."""
    d = day_of(s)
    out = {}
    for r in forward_rows(s):
        k = dp.canonical(r.deliveryMonth, r.futuresSymbol)
        if k is None or k < (d.year, d.month) or k in out:
            continue
        out[k] = (r.basisCents, r.futuresSymbol)
    return out


def nearby(s):
    """(basis, futures) of the snapshot's front bid: the nearest posted delivery month, else the weekly 'Spot' row of the older history (no futures). None when it has neither."""
    fwd = [r for r in forward_rows(s) if r.futuresSymbol]
    if fwd:
        return fwd[0].basisCents, fwd[0].futuresSymbol
    sp = next((r for r in s.rows if r.isSpot and r.basisCents is not None), None)
    return (sp.basisCents, None) if sp is not None else None


def snapshot_on(snaps, D: date):
    return next((s for s in snaps if day_of(s) == D), None)


def latest_sheet_day(snaps):
    """The date of the latest sheet that was POSTED: a snapshot with forward rows that is not a carried copy."""
    real = [s for s in snaps if forward_rows(s) and not is_carried(s)]
    return day_of(real[-1]) if real else None


def _closest(snaps, before: date, target: date, maxd: int):
    pool = [s for s in snaps if day_of(s) < before]
    if not pool:
        return None
    best = min(pool, key=lambda s: abs((day_of(s) - target).days))
    return best if abs((day_of(best) - target).days) <= maxd else None


def priors(snaps, D: date) -> tuple:
    """(last, ~1 week, ~1 month, ~1 year) earlier snapshots for the sheet of D. 'Last' is the day before's snapshot: on a posting day that is the previous sheet (carried, or as H&F
    refreshed its near months). The others are the nearest to 7 / 30 / 365 days earlier, within 3 / 4 / 4 days (the old history is weekly)."""
    earlier = [s for s in snaps if day_of(s) < D]
    last = earlier[-1] if earlier else None
    return (last, _closest(snaps, D, D - timedelta(days=7), 3), _closest(snaps, D, D - timedelta(days=30), 4), _closest(snaps, D, D - timedelta(days=365), 4))


def change(cur_basis, cur_fut, month_key, is_front: bool, prior, curve):
    """(change in cents, rolled) of a bid against an earlier snapshot. The same delivery month first - spread-adjusted when its contract changed, so the figure is the basis move and
    not the gap between contracts; a front row whose month the earlier snapshot did not quote is compared with that snapshot's front bid (raw), which is also how the year-ago weekly
    spot is read. (None, False) when there is nothing to compare."""
    if prior is None or cur_basis is None:
        return None, False
    pm = month_map(prior).get(month_key)
    if pm is not None:
        pb, pf = pm
        d = cur_basis - pb
        if cur_fut and pf and cur_fut != pf:
            adj = _roll_adjust(d, pf, cur_fut, curve or {})
            if adj is not None:
                return adj, True
        return d, False
    if is_front:
        nb = nearby(prior)
        if nb is not None:
            return cur_basis - nb[0], False
    return None, False


def signature(D: date, rows) -> str:
    """Identity of one version of one sheet: its date and every (label, contract, bid)."""
    body = repr((D.isoformat(), sorted((str(r.deliveryMonth), str(r.futuresSymbol), r.basisCents) for r in rows)))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


# -- the seasonal chart ---------------------------------------------------------
def mkt_year_week(d: date) -> tuple:
    """(marketing year, marketing week): the year starts Sep 1, the same weeks as the rail charts."""
    my = d.year if d.month >= 9 else d.year - 1
    return my, min(52, max(1, ((d - date(my, 9, 1)).days // 7) + 1))


def rail_shaped(snaps, upto: date) -> tuple:
    """INCO's snapshots up to `upto` as the (by_md, mkt_dates) pair rail_report's seasonal chart reads: one 'market', and per date a {period: cell} map in delivery order - the posted
    months, or the single 'Spot' cell of the weekly history. Part-years (fewer than MIN_YEAR_WEEKS weekly values) are left out unless it is the current one: the chart would otherwise
    call a band made of three whole years and a few months of a fourth '4-yr'."""
    cells_by_day = {}
    for s in snaps:
        d = day_of(s)
        if d > upto:
            continue
        fwd = [r for r in forward_rows(s) if r.futuresSymbol]
        if fwd:
            cells_by_day[d] = {r.deliveryMonth: {"period": r.deliveryMonth, "period_order": i, "bid": r.basisCents, "futures": r.futuresSymbol, "commodity": GRAIN, "bid_raw": None}
                               for i, r in enumerate(fwd)}
            continue
        sp = next((r for r in s.rows if r.isSpot and r.basisCents is not None), None)
        if sp is not None:
            cells_by_day[d] = {"Spot": {"period": "Spot", "period_order": 0, "bid": sp.basisCents, "futures": None, "commodity": GRAIN, "bid_raw": None}}
    weeks: dict = {}
    for d in cells_by_day:
        my, wk = mkt_year_week(d)
        weeks.setdefault(my, set()).add(wk)
    cur_my = max(weeks) if weeks else None
    keep = {my for my, w in weeks.items() if my == cur_my or len(w) >= MIN_YEAR_WEEKS}
    by_md = {(MARKET, d.isoformat()): cells for d, cells in cells_by_day.items() if mkt_year_week(d)[0] in keep}
    return by_md, {MARKET: {d for (_m, d) in by_md}}


def seasonal_png(snaps, D: date):
    """The spot seasonal chart (PNG bytes) as the rail emails draw it - this year bold, last year blue, the average and range of the complete years before them, the posted curve
    in red - or None when there is not enough history or the renderer is missing."""
    try:
        import rail_report as rr
    except Exception as exc:                                                # noqa: BLE001
        log.warning("inco seasonal chart unavailable: %s", exc)
        return None
    by_md, mkt_dates = rail_shaped(snaps, D)
    if not mkt_dates.get(MARKET):
        return None
    return rr._seasonal_png(MARKET, by_md, mkt_dates)


# -- the email --------------------------------------------------------------------
def _bid_td(b, tdr):
    if b is None:
        return f'<td style="{tdr};color:#cbd5e1">&mdash;</td>'
    return f'<td style="{tdr};color:#32373c;font-weight:700">{"flat" if b == 0 else f"{b:+d}"}</td>'


def _chg_td(d, rolled, tdr):
    if d is None:
        return f'<td style="{tdr};color:#cbd5e1">&mdash;</td>'
    mark = ' <span style="color:#d97706">&#8635;</span>' if rolled else ""
    if d == 0:
        return f'<td style="{tdr};color:#94a3b8">0{mark}</td>'
    return f'<td style="{tdr};color:{_GAIN if d > 0 else _LOSS};font-weight:700">{d:+d}{mark}</td>'


def build_inco_html(snaps, D: date, charts: bool = True, title: str = "Inco Bid Update") -> tuple:
    """(html, inline_images) of the update email for the sheet of D; inline_images maps cid -> file path. Raises ValueError when there is no snapshot for D."""
    cur = snapshot_on(snaps, D)
    rows = forward_rows(cur) if cur is not None else []
    if not rows:
        raise ValueError("no INCO bids on %s" % D)
    last, wk, mo, yr = priors(snaps, D)
    try:
        curve = _futures_curve()          # for spread-adjusting a contract roll
    except Exception:                      # noqa: BLE001
        curve = {}

    th = ("background:#f1f5f9;color:#475569;font-size:9px;text-transform:uppercase;letter-spacing:.04em;padding:4px 7px;font-weight:700;border-bottom:2px solid #e2e8f0;"
          "font-family:Arial,sans-serif")
    thl, thr = th + ";text-align:left", th + ";text-align:right"
    tdl = "padding:3px 7px;font-family:Arial,sans-serif;font-size:12px;border-bottom:1px solid #eef2f6;text-align:left"
    tdr = tdl.replace("text-align:left", "text-align:right") + ";font-variant-numeric:tabular-nums"

    imgs: dict = {}
    wm = _table_watermark()
    if wm:
        imgs[_TBL_WM_CID] = wm
    tbl_attr = f' background="cid:{_TBL_WM_CID}"' if wm else ""
    tbl_css = (f"background-image:url('cid:{_TBL_WM_CID}');background-position:center;background-repeat:no-repeat;background-size:contain;") if wm else ""

    tags = ""
    if is_carried(cur):
        tags += (f' <span style="font-size:9px;color:#fff;background:#d97706;padding:1px 5px;border-radius:3px">carried from the '
                 f'{str(cur.emailDate or "")[5:7].lstrip("0")}/{str(cur.emailDate or "")[8:10].lstrip("0")} sheet</span>')
    elif D != today_ct():
        tags += f' <span style="font-size:9px;color:#fff;background:#d97706;padding:1px 5px;border-radius:3px">sheet of {D.month}/{D.day}</span>'

    body = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;color:{JPSI_DARK};max-width:820px">'
        f'<div style="background:{JPSI_DARK};padding:16px 20px;border-radius:8px 8px 0 0">'
        f'<div style="color:#fff;font-size:18px;font-weight:800">{title}</div>'
        f'<div style="color:{JPSI_BLUE};font-size:13px;font-weight:600;margin-top:2px">Incobrasa &middot; Gilman, IL &middot; {D:%A, %B} {D.day}, {D.year}</div></div>'
        f'{dashboard_cta_html(hint="Bids tab has the full INCO history")}'
        f'<div style="padding:4px 2px 0">'
        f'<div style="margin-top:14px;margin-bottom:3px;font-family:Arial,sans-serif;font-size:12px;font-weight:700;color:{JPSI_DARK}">'
        f'Incobrasa (Gilman, IL) <span style="font-size:9px;color:#fff;background:{JPSI_BLUE};padding:1px 5px;border-radius:3px">soy processor</span>{tags}</div>'
        f'<table{tbl_attr} style="border-collapse:collapse;width:100%;{tbl_css}">'
        f'<tr><th style="{thl}">Period</th><th style="{thl}">Fut</th><th style="{thr}">Bid</th><th style="{thr}">&Delta; Last</th>'
        f'<th style="{thr}">&Delta; Wk</th><th style="{thr}">&Delta; Mo</th><th style="{thr}">&Delta; Yr</th></tr>')
    for i, r in enumerate(rows):
        key = dp.canonical(r.deliveryMonth, r.futuresSymbol)
        cells = "".join(_chg_td(*change(r.basisCents, r.futuresSymbol, key, i == 0, p, curve), tdr) for p in (last, wk, mo, yr))
        body += (f'<tr><td style="{tdl};color:{JPSI_DARK};font-weight:600">{_esc(str(r.deliveryMonth))}</td>'
                 f'<td style="{tdl};color:#94a3b8;font-size:10px">{_esc(str(r.futuresSymbol or ""))}</td>{_bid_td(r.basisCents, tdr)}{cells}</tr>')
    body += "</table>"

    if charts:
        png = seasonal_png(snaps, D)
        if png:
            fh = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            fh.write(png)
            fh.close()
            imgs[SEAS_CID] = fh.name
            body += f'<div style="margin:6px 0 2px"><img src="cid:{SEAS_CID}" style="max-width:100%;border:1px solid #e2e8f0;border-radius:6px"></div>'

    body += ('<div style="font-size:11px;color:#94a3b8;margin-top:10px;font-family:Arial,sans-serif">'
             "Incobrasa (Gilman, IL) soybean basis (&cent;/bu) vs the referenced futures; flat = 0. &Delta; = bid change vs the previous sheet / ~1 week / ~1 month / ~1 year earlier, "
             "by delivery month. Where an earlier sheet did not quote that month, the front row is compared with its front bid, and a year ago with the weekly spot bid. "
             "&#8635; = the contract rolled, so the change is spread-adjusted.</div></div>")
    if os.path.exists(_SIG_LOGO):
        imgs[_SIG_LOGO_CID] = _SIG_LOGO
    return body + signature_html(), imgs


# -- the send ledger ----------------------------------------------------------------
TABLE = "inco_email_log"
DDL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    sent_at    TEXT NOT NULL,
    via        TEXT,
    host       TEXT,
    kind       TEXT NOT NULL,
    sheet_date TEXT,
    signature  TEXT,
    to_addr    TEXT,
    subject    TEXT
)"""


def _where_am_i() -> str:
    p = os.path.abspath(__file__).replace("\\", "/")
    return "droplet" if p.startswith("/opt/") else ("cloud" if p.startswith("/mount/src") else "local")


def ensure_table() -> None:
    conn = db.get_conn()
    try:
        conn.cursor().execute(DDL)
        conn.commit()
    finally:
        conn.close()


def already_sent(sig: str):
    """When this exact sheet was last emailed to the list (kind 'update'), or None. A test send does not count."""
    ensure_table()
    conn = db.get_conn()
    try:
        c = conn.cursor()
        c.execute(f"SELECT sent_at FROM {TABLE} WHERE signature={db._ph()} AND kind={db._ph()} ORDER BY sent_at DESC", (sig, "update"))
        r = c.fetchone()
        return r["sent_at"] if r else None
    finally:
        conn.close()


def record(kind: str, D: date, sig: str, to_addr: str, subject: str) -> None:
    """Log a send. Never fails the send: the email has already gone out."""
    try:
        ensure_table()
        conn = db.get_conn()
        try:
            ph = db._ph()
            conn.cursor().execute(f"INSERT INTO {TABLE} (sent_at, via, host, kind, sheet_date, signature, to_addr, subject) VALUES ({','.join([ph] * 8)})",
                                  (datetime.now(timezone.utc).isoformat(), _where_am_i(), socket.gethostname(), kind, D.isoformat(), sig, to_addr, (subject or "")[:300]))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                                                # noqa: BLE001
        log.warning("inco email ledger: write failed (%s) - the email went out but is not logged", exc)


def recipients(to_addr: str | None = None) -> tuple:
    """(To, BCC) for a send: Kolten on To and the Soy Processing list on BCC, or just `to_addr` (no BCC) for a test."""
    if to_addr:
        return to_addr, None
    return os.getenv("INCO_EMAIL_TO") or DEFAULT_TO, (os.getenv("INCO_EMAIL_BCC") or DEFAULT_BCC)


def send_inco_update_email(D: date | None = None, to_addr: str | None = None, *, force: bool = False) -> bool:
    """Email the sheet of D (default: the latest posted sheet). True when sent, False when this exact sheet had already gone to the list (force=True sends it again). A send to
    `to_addr` is a test: it is logged as such, never blocked by, and never counted as, the list's copy. Raises when there is nothing to send or the send fails."""
    snaps = load_snaps()
    D = D or latest_sheet_day(snaps)
    if D is None:
        raise ValueError("no INCO sheet to email")
    cur = snapshot_on(snaps, D)
    if cur is None:
        raise ValueError("no INCO snapshot on %s" % D)
    sig = signature(D, forward_rows(cur))
    is_test = bool(to_addr)
    if not is_test and not force:
        when = already_sent(sig)
        if when:
            log.info("Inco update email for %s: this exact sheet was already emailed at %s - skipped", D, when)
            return False
    html, imgs = build_inco_html(snaps, D, charts=True)
    subj = f"{SUBJECT} — Gilman, IL · {D.strftime('%b')} {D.day}"
    to, bcc = recipients(to_addr)
    via = send_email(subj, html, to, bcc=bcc, inline_images=imgs or None)
    log.info("Inco update email for %s sent via %s to %s%s", D, via, to, " (bcc %s)" % bcc if bcc else "")
    record("test" if is_test else "update", D, sig, to + ("; bcc " + bcc if bcc else ""), subj)
    return True


if __name__ == "__main__":
    import argparse
    import sys

    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser(description="The Inco (Gilman, IL) bid update email.")
    ap.add_argument("--date", help="the sheet's date, YYYY-MM-DD (default: the latest posted sheet)")
    ap.add_argument("--preview", action="store_true", help="write _inco_preview.html and _inco_seasonal.png; send nothing")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--to", help="send only to this address (a test: not logged as the list's copy)")
    ap.add_argument("--force", action="store_true", help="send even if this exact sheet was already emailed to the list")
    a = ap.parse_args()
    day = date.fromisoformat(a.date) if a.date else None
    if a.send:
        print("sent" if send_inco_update_email(day, to_addr=a.to, force=a.force) else "not sent: this exact sheet was already emailed (--force to send it again)")
    else:
        snaps = load_snaps()
        day = day or latest_sheet_day(snaps)
        html, imgs = build_inco_html(snaps, day, charts=True)
        for cid, path in imgs.items():
            html = html.replace(f"cid:{cid}", "file:///" + path.replace("\\", "/"))
        open("_inco_preview.html", "w", encoding="utf-8").write("<html><body>" + html + "</body></html>")
        png = seasonal_png(snaps, day)
        if png:
            open("_inco_seasonal.png", "wb").write(png)
        print(f"wrote _inco_preview.html ({len(html)} chars, {len(imgs)} inline image(s)) for the sheet of {day}")
