"""The Inco (Gilman, IL) bid update email: the table, the changes, the chart, the send and its ledger, and the hook in `inco_bids.py post`.

    python tests/test_inco_report.py

Kolten 2026-10-26: "Can we make an auto email like we do on rail when Inco update occurs. Put the normal table of bids and trends and then a seasonal chart" / "setup to send to
soyprocessing@jpsi.com". A throwaway SQLite database is filled like the live INCO series (weekly 'Spot' history from 2023, real sheets, carried days); the sender is a fake, so nothing
is emailed and nothing touches the network.
"""
import contextlib
import io
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on
for _k in ("INCO_EMAIL_TO", "INCO_EMAIL_BCC"):
    os.environ.pop(_k, None)

import database as db                  # noqa: E402
import inco_bids as ib                 # noqa: E402
import inco_report as ir               # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="inco_report_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
db.init_db()
logging.disable(logging.CRITICAL)
ir.today_ct = lambda: date(2026, 10, 26)           # the 'sheet of m/d' tag depends on today: pin it
FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:600]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


# -- the series: weekly 'Spot' history (2023-01 .. 2026-07, as the live jsa_history), then the real sheets of Sep-Oct 2026 --------------------------------------------------------------
def hist_value(d):
    base = {2023: -10, 2024: 0, 2025: 8, 2026: 18}[d.year]
    return base + ((d.isocalendar()[1] * 7) % 11) - 5


OVERRIDE = {date(2025, 10, 22): 15, date(2025, 10, 29): 15}           # what the live archive holds a year before the 10-26 sheet
hist, d = [], date(2023, 1, 4)                                          # a Wednesday
while d <= date(2026, 7, 29):
    v = OVERRIDE.get(d, hist_value(d))
    hist.append({"timestamp": d.isoformat() + "T00:00:00Z", "provider": ib.PROV, "location": ib.LOC, "source": "jsa_history",
                 "rows": [{"id": "h0", "grain": "Soybeans", "deliveryMonth": "Spot", "futuresSymbol": "", "basisCents": v, "isSpot": True, "spotGrain": None}]})
    d += timedelta(days=7)
db.upsert_snapshots(hist)


def rows(triples):
    return [{"grain": "Soybeans", "deliveryMonth": l, "futuresSymbol": f, "basisCents": b, "isSpot": False, "spotGrain": None} for l, f, b in triples]


def post(day, triples, today=None):
    D = date.fromisoformat(day)
    return ib.apply_plan(ib.plan_post(ib.load_snaps(), D, rows(triples), today or D))


S15 = [("Sep 21-26", "ZSX26", 20), ("9/27-Oct", "ZSX26", 5), ("Nov 2026", "ZSX26", 25), ("Dec 2026", "ZSF27", 15), ("Jan 2027", "ZSF27", 20), ("Feb 2027", "ZSH27", 20), ("Mar 2027", "ZSH27", 30)]
S24 = [("Sep 21-27", "ZSX26", 40)] + S15[1:]
S02 = [("FH Oct 2026", "ZSX26", 20), ("LH Oct 2026", "ZSX26", 0), ("Nov 2026", "ZSX26", 25), ("Dec 2026", "ZSF27", 15), ("Jan 2027", "ZSF27", 20), ("Feb 2027", "ZSH27", 20), ("Mar 2027", "ZSH27", 30)]
S19 = [("Oct 2026", "ZSX26", 20), ("Nov 2026", "ZSX26", 25), ("Dec 2026", "ZSF27", 15), ("Jan 2027", "ZSF27", 20), ("Feb 2027", "ZSH27", 20), ("Mar 2027", "ZSH27", 30)]
S26 = [("Oct 2026", "ZSX26", 25), ("Nov 2026", "ZSX26", 25), ("Dec 2026", "ZSF27", 20), ("Jan 2027", "ZSF27", 25), ("Feb 2027", "ZSH27", 20), ("Mar 2027", "ZSH27", 30)]
for day, sheet in (("2026-09-15", S15), ("2026-09-24", S24), ("2026-10-02", S02), ("2026-10-19", S19), ("2026-10-26", S26)):
    assert post(day, sheet), day
D26 = date(2026, 10, 26)
snaps = ir.load_snaps()

print("reading the series")
check("every snapshot loads, oldest first", len(snaps) > 180 and [ir.day_of(s) for s in snaps] == sorted(ir.day_of(s) for s in snaps), len(snaps))
s26 = ir.snapshot_on(snaps, D26)
check("the sheet of 10-26 is a posted sheet, not a carried copy; 10-23 is a carried copy", s26 is not None and not ir.is_carried(s26) and ir.is_carried(ir.snapshot_on(snaps, date(2026, 10, 23))))
check("forward_rows: the six months in delivery order, no Spot row", [r.deliveryMonth for r in ir.forward_rows(s26)] == ["Oct 2026", "Nov 2026", "Dec 2026", "Jan 2027", "Feb 2027", "Mar 2027"])
check("the weekly history has no forward rows, and its front bid is its Spot row", ir.forward_rows(ir.snapshot_on(snaps, date(2025, 10, 29))) == [] and ir.nearby(ir.snapshot_on(snaps, date(2025, 10, 29))) == (15, None))
check("nearby of a sheet = its nearest month (Oct +25 on ZSX26)", ir.nearby(s26) == (25, "ZSX26"))
check("latest_sheet_day = the last POSTED sheet", ir.latest_sheet_day(snaps) == D26)
check("... carried days after it do not move it", ir.latest_sheet_day(snaps + [SimpleNamespace(timestamp="2026-10-27T00:00:00Z", rows=s26.rows, emailSubject="carried forward from 2026-10-26 sheet", emailDate="2026-10-26", source="manual")]) == D26)

print("\nthe month map (the same delivery month across sheets whatever the label)")
s02 = ir.snapshot_on(snaps, date(2026, 10, 2))
check("'FH Oct' and 'LH Oct' are one month: the nearest slot (FH, +20) stands for October", ir.month_map(s02)[(2026, 10)] == (20, "ZSX26"), ir.month_map(s02))
s25 = ir.snapshot_on(snaps, date(2026, 9, 25))
check("'9/27-Oct' is October's window; 'Sep 21-27' is September", ir.month_map(s25)[(2026, 10)] == (5, "ZSX26") and ir.month_map(s25)[(2026, 9)] == (40, "ZSX26"), ir.month_map(s25))
stale = SimpleNamespace(timestamp="2026-10-26T00:00:00Z", emailSubject=None, rows=[SimpleNamespace(deliveryMonth="Sep 2026", futuresSymbol="ZSX26", basisCents=40, isSpot=False),
                                                                                   SimpleNamespace(deliveryMonth="Nov 2026", futuresSymbol="ZSX26", basisCents=25, isSpot=False)])
check("a month already past on the snapshot's own date is dropped (a lingering 'Sep 2026')", list(ir.month_map(stale)) == [(2026, 11)])

print("\nthe earlier snapshots a sheet is compared with")
last, wk, mo, yr = ir.priors(snaps, D26)
check("last = the day before (10-23, a carried copy of the 10-19 sheet)", ir.day_of(last) == date(2026, 10, 23))
check("~1 week = the nearest to 10-19 (the real sheet of that day)", ir.day_of(wk) == date(2026, 10, 19))
check("~1 month = the nearest to 9-26 within 4 days (9-25)", ir.day_of(mo) == date(2026, 9, 25), ir.day_of(mo))
check("~1 year = the nearest weekly spot to 2025-10-26 (10-29, three days off; 10-22 is four)", ir.day_of(yr) == date(2025, 10, 29), ir.day_of(yr))
check("nothing earlier than a sheet is ever its 'prior'", all(ir.day_of(p) < D26 for p in (last, wk, mo, yr)))
check("an early sheet has nothing a year back", ir.priors(snaps, date(2023, 1, 18))[3] is None and ir.priors(snaps, date(2023, 1, 4))[0] is None)
check("a gap wider than the window leaves the column empty (nothing within 4 days of 9-26)", ir.priors([s for s in snaps if not (date(2026, 9, 21) <= ir.day_of(s) <= date(2026, 10, 1))], D26)[2] is None)
check("... a week back (nothing within 3 days of 10-19)", ir.priors([s for s in snaps if not (date(2026, 10, 14) <= ir.day_of(s) <= date(2026, 10, 23))], D26)[1] is None)
check("... and a year back (nothing within 4 days of 2025-10-26)", ir.priors([s for s in snaps if not (date(2025, 10, 10) <= ir.day_of(s) <= date(2025, 11, 10))], D26)[3] is None)

print("\nthe changes")
ch = lambda prior, key, front, cur=(25, "ZSX26"), curve=None: ir.change(cur[0], cur[1], key, front, prior, curve if curve is not None else {})   # noqa: E731
check("same month vs the previous sheet: Oct 20 -> 25 = +5; Nov 25 -> 25 = 0", ch(last, (2026, 10), True) == (5, False) and ch(last, (2026, 11), False, (25, "ZSX26")) == (0, False))
check("vs a month ago October is +20: it was the '9/27-Oct' window at +5", ch(mo, (2026, 10), True) == (20, False))
check("a month the earlier sheet did not quote is blank, unless it is the front row: the front row is compared with that sheet's front bid (25 - 40 = -15)",
      ch(ir.snapshot_on(snaps, date(2026, 9, 25)), (2026, 12), False, (20, "ZSF27")) != (None, False) and ir.change(25, "ZSX26", (2026, 8), True, s25, {}) == (-15, False)
      and ir.change(25, "ZSX26", (2026, 8), False, s25, {}) == (None, False))
check("a year ago the front row is read against the weekly spot: 25 - 15 = +10; the other months have no year-ago", ch(yr, (2026, 10), True) == (10, False) and ch(yr, (2026, 12), False, (20, "ZSF27")) == (None, False))
check("no earlier snapshot, no bid: nothing", ir.change(25, "ZSX26", (2026, 10), True, None, {}) == (None, False) and ir.change(None, "ZSX26", (2026, 10), True, last, {}) == (None, False))
mk = lambda ym, fut, b: SimpleNamespace(timestamp="2026-10-19T00:00:00Z", emailSubject=None, rows=[SimpleNamespace(deliveryMonth=ym, futuresSymbol=fut, basisCents=b, isSpot=False)])   # noqa: E731
check("a contract roll is spread-adjusted: +20 on ZSF27 vs +10 on ZSH27 with H=1000 F=990 is a move of 0, marked as rolled",
      ir.change(20, "ZSF27", (2026, 12), False, mk("Dec 2026", "ZSH27", 10), {"ZSH27": 1000, "ZSF27": 990}) == (0, True))
check("... with no spread to adjust by the raw change stands, unmarked", ir.change(20, "ZSF27", (2026, 12), False, mk("Dec 2026", "ZSH27", 10), {}) == (10, False))
check("the same contract is never adjusted", ir.change(20, "ZSF27", (2026, 12), False, mk("Dec 2026", "ZSF27", 10), {"ZSF27": 990}) == (10, False))
check("signature: stable under row order, moved by any bid or by the date",
      ir.signature(D26, ir.forward_rows(s26)) == ir.signature(D26, list(reversed(ir.forward_rows(s26)))) and ir.signature(D26, ir.forward_rows(s26)) != ir.signature(D26, ir.forward_rows(ir.snapshot_on(snaps, date(2026, 10, 19))))
      and ir.signature(D26, ir.forward_rows(s26)) != ir.signature(date(2026, 10, 27), ir.forward_rows(s26)))

print("\nthe email's table")
html, imgs = ir.build_inco_html(snaps, D26, charts=False)


def table_rows(h):
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).replace("&mdash;", "-").replace("&#8635;", "R").strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if cells:
            out.append(cells)
    return out


T = table_rows(html)
check("header: Period / Fut / Bid and the four change columns", T[0] == ["Period", "Fut", "Bid", "Δ Last", "Δ Wk", "Δ Mo", "Δ Yr"] or T[0][:3] == ["Period", "Fut", "Bid"] and len(T[0]) == 7, T[0])
by = {r[0]: r for r in T[1:]}
check("six rows, in delivery order", [r[0] for r in T[1:]] == ["Oct 2026", "Nov 2026", "Dec 2026", "Jan 2027", "Feb 2027", "Mar 2027"], [r[0] for r in T[1:]])
check("Oct: ZSX26 +25, +5 last, +5 wk, +20 mo, +10 yr", by["Oct 2026"][1:] == ["ZSX26", "+25", "+5", "+5", "+20", "+10"], by["Oct 2026"])
check("Nov: unchanged against the last sheet, the week and the month, no year-ago (blank)", by["Nov 2026"][1:] == ["ZSX26", "+25", "0", "0", "0", "-"], by["Nov 2026"])
check("Dec +20 (+5 / +5 / +5), Jan +25 (+5 / +5 / +5), Feb +20 and Mar +30 unchanged",
      by["Dec 2026"][2:6] == ["+20", "+5", "+5", "+5"] and by["Jan 2027"][2:6] == ["+25", "+5", "+5", "+5"] and by["Feb 2027"][2:6] == ["+20", "0", "0", "0"] and by["Mar 2027"][2:6] == ["+30", "0", "0", "0"])
check("the page: the title, the sheet's day, the view button, the footnote", "Inco Bid Update" in html and "Gilman, IL" in html and "Monday, October 26, 2026" in html and "Open the Basis Tracker" in html and "flat = 0" in html)
check("no 'sheet of' / 'carried' tag on today's own sheet", "sheet of " not in html and "carried from" not in html)
check("a flat bid reads 'flat'", "<td" in ir._bid_td(0, "x") and ">flat<" in ir._bid_td(0, "x") and ">+5<" in ir._bid_td(5, "x") and ">-10<" in ir._bid_td(-10, "x"))
h22, _ = ir.build_inco_html(snaps, date(2026, 10, 22), charts=False)
check("a carried day is tagged with the sheet it repeats", "carried from the 10/19 sheet" in h22, re.findall(r"carried[^<]*", h22))
h19, _ = ir.build_inco_html(snaps, date(2026, 10, 19), charts=False)
check("a resend of an older sheet says whose day it is ('sheet of 10/19')", "sheet of 10/19" in h19)
try:
    ir.build_inco_html(snaps, date(2020, 1, 1), charts=False)
    nosnap = False
except ValueError:
    nosnap = True
check("no snapshot for the date -> ValueError, not an empty email", nosnap)
evil = SimpleNamespace(timestamp="2026-10-26T00:00:00Z", emailSubject=None, emailDate=None, source="manual",
                       rows=[SimpleNamespace(deliveryMonth="Oct 2026 <script>x</script>", futuresSymbol="ZSX26", basisCents=25, isSpot=False)])
he, _ = ir.build_inco_html([evil], D26, charts=False)
check("a label is escaped before it goes into the page", "<script>" not in he and "&lt;script&gt;" in he)
check("the watermark tile and the signature logo ride along as inline images", "jsatblwm" in imgs and "jsa50yr" in imgs and all(os.path.exists(p) for p in imgs.values()), list(imgs))

print("\nthe seasonal chart (rail_report's own chart, handed INCO's history)")
by_md, mkt_dates = ir.rail_shaped(snaps, D26)
ds = sorted(mkt_dates[ir.MARKET])
check("one 'market', named for the chart's title", list(mkt_dates) == [ir.MARKET] and all(m == ir.MARKET for (m, _d) in by_md))
check("the part-year 2022/23 (Jan-Aug 2023, 35 weekly values) is left out; the first date is Sep 2023", ds[0] >= "2023-09-01" and ds[0] <= "2023-09-07", ds[0])
check("... the current year is in, however short (it starts 9-15)", "2026-09-15" in ds and ds[-1] == "2026-10-26")
c19 = by_md[(ir.MARKET, "2026-10-19")]
check("a sheet's cells carry the label as the period, in delivery order, with the contract", [c["period"] for c in sorted(c19.values(), key=lambda c: c["period_order"])][:2] == ["Oct 2026", "Nov 2026"] and c19["Oct 2026"]["futures"] == "ZSX26" and c19["Oct 2026"]["bid"] == 20)
ch_h = by_md[(ir.MARKET, "2025-10-29")]
check("a weekly history day is one 'Spot' cell with no contract", list(ch_h) == ["Spot"] and ch_h["Spot"]["bid"] == 15 and ch_h["Spot"]["futures"] is None)
check("mkt_year_week: Sep 1 opens the year; Aug 31 is its last week", ir.mkt_year_week(date(2026, 9, 1)) == (2026, 1) and ir.mkt_year_week(date(2026, 8, 31)) == (2025, 52) and ir.mkt_year_week(date(2026, 10, 26)) == (2026, 8))
import vl_convert  # noqa: E402

with mock.patch.object(vl_convert, "vegalite_to_png", side_effect=lambda spec, scale=1: spec.encode("utf-8")):
    spec = json.loads(ir.seasonal_png(snaps, D26))
legend = json.dumps(spec, ensure_ascii=True)
check("the title names Inco, the legend counts the three complete years behind the band and says 'range' (never 'ex hi/lo')",
      "Inco (Gilman, IL)" in legend and "3-yr range (2023/24" in legend and "3-yr average" in legend and "ex hi/lo" not in legend and "4-yr" not in legend, legend[:400])
check("... and carries this year, last year and the forward curve", "2026/27" in legend and "2025/26" in legend and "Forward curve" in legend)
band = [v for v in spec["datasets"].values() if v and {"avg", "lo", "hi"} <= set(v[0])]
check("the band is a real range around its average in every week (lo <= avg <= hi)", bool(band) and all(r["lo"] <= r["avg"] + 1e-9 <= r["hi"] + 2e-9 for r in band[0]), band[:1] and band[0][:2])
png = ir.seasonal_png(snaps, D26)
check("rendered for real it is a PNG", isinstance(png, (bytes, bytearray)) and bytes(png[:4]) == b"\x89PNG", type(png))
check("too little history -> no chart, not an error", ir.seasonal_png([s for s in snaps if ir.day_of(s) >= date(2026, 9, 15)], D26) is None)
html_c, imgs_c = ir.build_inco_html(snaps, D26, charts=True)
check("the chart rides in the page as an inline image", "cid:inco_seasonal" in html_c and "inco_seasonal" in imgs_c and os.path.exists(imgs_c["inco_seasonal"]))

print("\nsending, and the ledger")
SENT = []


def fake_send(subject, html, to_addr, cc=None, inline_images=None, bcc=None):
    SENT.append({"subject": subject, "html": html, "to": to_addr, "bcc": bcc, "imgs": inline_images})
    return "fake"


def ledger():
    ir.ensure_table()
    conn = db.get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT kind, sheet_date, signature, to_addr, subject FROM inco_email_log ORDER BY sent_at")
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


with mock.patch.object(ir, "send_email", fake_send):
    check("a test copy (--to) goes to that address only, with no BCC", ir.send_inco_update_email(D26, to_addr="kpostin@jpsi.com") is True and SENT[-1]["to"] == "kpostin@jpsi.com" and SENT[-1]["bcc"] is None)
    check("... and is logged as a test", [r["kind"] for r in ledger()] == ["test"])
    check("a test does not count as the list's copy: the list still gets the sheet", ir.send_inco_update_email(D26) is True and SENT[-1]["to"] == "soyprocessing@jpsi.com" and SENT[-1]["bcc"] is None)
    check("the subject names the sheet's day", SENT[-1]["subject"] == "JSA Inco Bid Update — Gilman, IL · Oct 26", SENT[-1]["subject"])
    check("the page and its images went with it", "Inco Bid Update" in SENT[-1]["html"] and "inco_seasonal" in (SENT[-1]["imgs"] or {}), list(SENT[-1]["imgs"] or {}))
    n = len(SENT)
    check("the same sheet again is NOT sent to the list twice", ir.send_inco_update_email(D26) is False and len(SENT) == n)
    check("... unless forced", ir.send_inco_update_email(D26, force=True) is True and len(SENT) == n + 1)
    check("a test copy is never blocked by an earlier send", ir.send_inco_update_email(D26, to_addr="kpostin@jpsi.com") is True)
    check("the ledger: test, update, update (forced), test - one signature throughout", [r["kind"] for r in ledger()] == ["test", "update", "update", "test"] and len({r["signature"] for r in ledger()}) == 1, ledger())
    check("the default date is the latest posted sheet", ir.send_inco_update_email(None, force=True) is True and SENT[-1]["subject"].endswith("Oct 26"))
    # a corrected sheet for the same day is a new sheet: it goes out again
    check("the corrected sheet (Dec +25, not +20) is posted, silently", post("2026-10-26", [r if r[0] != "Dec 2026" else ("Dec 2026", "ZSF27", 25) for r in S26], D26))
    snaps_b = ir.load_snaps()
    n = len(SENT)
    check("a corrected sheet is a different signature and goes to the list again", ir.send_inco_update_email(D26) is True and len(SENT) == n + 1 and "+25" in SENT[-1]["html"])
    os.environ["INCO_EMAIL_TO"], os.environ["INCO_EMAIL_BCC"] = "list@example.com", "copy@example.com"
    check("INCO_EMAIL_TO / INCO_EMAIL_BCC override the recipients", ir.recipients() == ("list@example.com", "copy@example.com") and ir.recipients("me@example.com") == ("me@example.com", None))
    del os.environ["INCO_EMAIL_TO"], os.environ["INCO_EMAIL_BCC"]
    check("the default recipients: soyprocessing@jpsi.com, no BCC", ir.recipients() == ("soyprocessing@jpsi.com", None))
    with mock.patch.object(ir, "load_snaps", return_value=[]):
        try:
            ir.send_inco_update_email(None)
            empty = False
        except ValueError:
            empty = True
    check("no INCO sheet at all -> ValueError", empty)
    try:
        ir.send_inco_update_email(date(2026, 10, 25))
        none_day = False
    except ValueError:
        none_day = True
    check("a date with no snapshot (a Sunday) -> ValueError", none_day)

# a send that fails leaves no trace in the ledger, so the retry is not mistaken for a duplicate
def boom(*a, **k):
    raise RuntimeError("graph down")


post("2026-10-27", [("Oct 2026", "ZSX26", 30), ("Nov 2026", "ZSX26", 25)])
before = len(ledger())
with mock.patch.object(ir, "send_email", boom):
    try:
        ir.send_inco_update_email(date(2026, 10, 27))
        raised = False
    except RuntimeError:
        raised = True
check("a failed send raises", raised)
check("... and writes nothing to the ledger", len(ledger()) == before)
with mock.patch.object(ir, "send_email", fake_send):
    check("the retry goes out (it was never marked sent)", ir.send_inco_update_email(date(2026, 10, 27)) is True)

print("\n`inco_bids.py post --commit` sends the email")
SENT.clear()


def run_post(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.object(sys, "argv", ["inco_bids.py", "post"] + list(args)):
        rc = ib.main()
    return rc, out.getvalue()


sheet_f = Path(tempfile.mkdtemp(prefix="inco_sheet_")) / "sheet.txt"
sheet_f.write_text("Oct 2026, ZSX26, 30\nNov 2026, ZSX26, 25\nDec 2026, ZSF27, 20\n", encoding="utf-8")
with mock.patch.object(ir, "send_email", fake_send):
    rc, out = run_post("--date", "2026-10-28", "--file", str(sheet_f), "--today", "2026-10-28")
    check("a preview writes nothing and sends nothing, and says who it would go to", rc == 0 and not SENT and "To soyprocessing@jpsi.com" in out and "PREVIEW ONLY" in out
          and not any(ir.day_of(s) == date(2026, 10, 28) for s in ir.load_snaps()), out)
    rc, out = run_post("--date", "2026-10-28", "--file", str(sheet_f), "--today", "2026-10-28", "--commit")
    check("--commit writes the sheet, reads it back, then emails the list once", rc == 0 and "VERIFIED" in out and "update email sent" in out and len(SENT) == 1 and SENT[0]["to"] == "soyprocessing@jpsi.com", out)
    rc, out = run_post("--date", "2026-10-28", "--file", str(sheet_f), "--today", "2026-10-28", "--commit")
    check("the same sheet committed again is rewritten but NOT emailed twice", rc == 0 and len(SENT) == 1 and "NOT sent" in out and "already emailed" in out, out)
    rc, out = run_post("--date", "2026-10-28", "--file", str(sheet_f), "--today", "2026-10-28", "--commit", "--force-email")
    check("--force-email sends it again", rc == 0 and len(SENT) == 2)
    sheet_f.write_text("Oct 2026, ZSX26, 35\nNov 2026, ZSX26, 25\n", encoding="utf-8")
    rc, out = run_post("--date", "2026-10-29", "--file", str(sheet_f), "--today", "2026-10-29", "--commit", "--no-email")
    check("--no-email saves the sheet and sends nothing", rc == 0 and len(SENT) == 2 and "email skipped" in out and any(ir.day_of(s) == date(2026, 10, 29) for s in ir.load_snaps()), out)
    sheet_f.write_text("Oct 2026, ZSX26, 40\nNov 2026, ZSX26, 25\n", encoding="utf-8")
    rc, out = run_post("--date", "2026-10-30", "--file", str(sheet_f), "--today", "2026-10-30", "--commit", "--to", "kpostin@jpsi.com")
    check("--to sends a test copy to that address only, and the list is not marked as having it", rc == 0 and SENT[-1]["to"] == "kpostin@jpsi.com" and ledger()[-1]["kind"] == "test", out)

    class Down:
        def __call__(self, *a, **k):
            raise RuntimeError("graph down")

    sheet_f.write_text("Nov 2026, ZSX26, 45\nDec 2026, ZSF27, 25\n", encoding="utf-8")        # a November sheet: October is already past
    with mock.patch.object(ir, "send_email", Down()):
        rc, out = run_post("--date", "2026-11-02", "--file", str(sheet_f), "--today", "2026-11-02", "--commit")
    check("a send that fails leaves the sheet SAVED, says so, prints the resend command, and exits 3", rc == 3 and "SAVED but the update email FAILED" in out and "python inco_report.py --send --date 2026-11-02" in out
          and any(ir.day_of(s) == date(2026, 11, 2) for s in ir.load_snaps()), out)

print("\nthe other writers never email")
with mock.patch.object(ir, "send_inco_update_email", side_effect=AssertionError("an email was sent")):
    loc = {"sheet_date": "2026-11-03", "bids": [{"grain": "Soybeans", "delivery": "Nov 2026", "cme_symbol": "ZSX26", "basis_cents": 30, "bid_id": "HF_IB_S1"}]}
    try:
        res = ib.fold_hf(loc, today=date(2026, 11, 3))
        emailed = False
    except AssertionError:
        emailed = True
    check("the H&F fold (a carried day refreshed from hfgrain.com) sends nothing", not emailed and res is True, res)
    try:
        rc = ib.cmd_carry(SimpleNamespace(date="2026-11-04", max_age=21, dry_run=False))
        emailed = False
    except AssertionError:
        emailed = True
    check("the daily carry-forward sends nothing", not emailed)

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("all inco_report checks passed")
