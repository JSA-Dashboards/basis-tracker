"""database.get_snapshots_bulk(compact=True): the daily Changes email's snapshot load, light enough for the 2 GB droplet.

    python tests/test_bulk_snapshots_compact.py

2026-11-12: the 15:45 CT run on the droplet (1 vCPU / 2 GB, 2 GB swap) was killed by the kernel's out-of-memory killer while building the Changes email
(`Out of memory: Killed process ... (python) anon-rss:1387924kB`, free swap 248 kB, rc=137, no email, no client reports). Measured on live data, building
that email needed 1,293 MB before the trend tables went in (a whole facility type's 400 days, ~1M bid rows, as pydantic models, with the driver holding its
own list of dicts as well), and 1,791 MB after (the per-report snapshot memo then keeps every type's load). `compact=True` streams the cursor in chunks into
tuples and shared strings: 380 MB, and the email is the same to the byte. These tests pin that the compact form carries exactly the same values as the
pydantic one, however the rows are chunked, and that it really is light. Hermetic: a throwaway SQLite, and fake cursors for the shapes SQLite cannot make.
"""
import logging
import os
import re
import sys
import tempfile
import tracemalloc
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import changes_report as cr            # noqa: E402
import database as db                  # noqa: E402
from pydantic import BaseModel         # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="bulk_compact_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


SNAP_FIELDS = ("id", "timestamp", "provider", "location", "source")
ROW_FIELDS = ("id", "grain", "deliveryMonth", "futuresSymbol", "basisCents", "isSpot", "spotGrain")


def diff(a, b):
    """'' when two {(provider, location): [snapshot]} loads carry exactly the same values (and types), else what differs first."""
    if list(a) != list(b):
        return "keys %s != %s" % (list(a)[:4], list(b)[:4])
    for key in a:
        if len(a[key]) != len(b[key]):
            return "%s: %d vs %d snapshots" % (key, len(a[key]), len(b[key]))
        for sa, sb in zip(a[key], b[key]):
            for f in SNAP_FIELDS:
                if getattr(sa, f) != getattr(sb, f) or type(getattr(sa, f)) is not type(getattr(sb, f)):
                    return "%s snapshot %s.%s: %r vs %r" % (key, sa.timestamp, f, getattr(sa, f), getattr(sb, f))
            if (sa.emailSubject, sa.emailDate) != (sb.emailSubject, sb.emailDate):
                return "%s snapshot %s: email fields" % (key, sa.timestamp)
            if len(sa.rows) != len(sb.rows):
                return "%s snapshot %s: %d vs %d rows" % (key, sa.timestamp, len(sa.rows), len(sb.rows))
            for ra, rb in zip(sa.rows, sb.rows):
                for f in ROW_FIELDS:
                    if getattr(ra, f) != getattr(rb, f) or type(getattr(ra, f)) is not type(getattr(rb, f)):
                        return "%s snapshot %s row %s.%s: %r (%s) vs %r (%s)" % (key, sa.timestamp, ra.id, f, getattr(ra, f), type(getattr(ra, f)).__name__,
                                                                                getattr(rb, f), type(getattr(rb, f)).__name__)
    return ""


# ── a throwaway SQLite with the shapes the readers meet ────────────────────────
TODAY = datetime.now(timezone.utc).date()


def stamp(days_ago):
    return (TODAY - timedelta(days=days_ago)).strftime("%Y-%m-%dT00:00:00Z")


def code(month, year):
    return "ZC%s%02d" % ("FGHJKMNQUVXZ"[month - 1], year % 100)


def mlabel(month, year):
    return datetime(year, month, 1).strftime("%b %Y")


M1 = (TODAY.month, TODAY.year)                                          # (month, year): this month, the next, the one after
M2 = (TODAY.month % 12 + 1, TODAY.year + (TODAY.month == 12))
M3 = (M2[0] % 12 + 1, M2[1] + (M2[0] == 12))


def sd(prov, loc, days_ago, rows, src="web"):
    return {"timestamp": stamp(days_ago), "provider": prov, "location": loc, "source": src,
            "rows": [{"id": "R%d" % (i + 1), "grain": g, "deliveryMonth": dm, "futuresSymbol": fs, "basisCents": b, "isSpot": sp, "spotGrain": (g if sp else None)}
                     for i, (g, dm, fs, b, sp) in enumerate(rows)]}


def corn(b1, b2, b3=None, sym1=None):
    rows = [("Corn", mlabel(*M1), sym1 or code(*M1), b1, False), ("Corn", mlabel(*M2), code(*M2), b2, False)]
    if b3 is not None:
        rows.append(("Corn", mlabel(*M3), code(*M3), b3, False))
    return rows


print("the seeded database: compact vs pydantic, value for value")
db.init_db()
SEED = []
for prov, loc, st, ft, series in (
        ("ADM", "A, IL", "IL", "Corn Processing", {0: corn(10, 8, 5), 1: corn(7, 8), 7: corn(4, 3), 30: corn(0, 1)}),
        ("ADM", "B, IA", "IA", "Corn Processing", {0: corn(-5, -8) + [("Corn", "Spot", "", -6, True)], 1: corn(-5, -9), 7: corn(-2, -3, None, code(M2[0], M2[1]))}),
        ("CHS", "C, NE", "NE", "Corn Processing", {0: corn(0, 0), 2: corn(0, 4)}),
        ("ADM", "Velva, ND", "ND", "Rail Terminal", {0: [("Canola (Us)", mlabel(*M1), "RSF27", -4250, False), ("Canola (Us)", mlabel(*M2), "RSF27", -4500, False), ("Canola (Us)", mlabel(*M3), "RSH27", None, False)],
                                                     1: [("Canola (Us)", mlabel(*M1), "RSF27", -3500, False)]}),
        ("Cargill", "D, OH", "OH", "Rail Terminal", {0: corn(3, None)}),
):
    for d, rows in series.items():
        SEED.append(sd(prov, loc, d, rows))
    db.upsert_location_meta(prov, loc, state=st, facility_type=ft, lat=40.0, lon=-90.0)
db.upsert_snapshots(SEED)
PAIRS = [("ADM", "A, IL"), ("ADM", "B, IA"), ("CHS", "C, NE"), ("ADM", "Velva, ND"), ("Cargill", "D, OH")]

pyd = db.get_snapshots_bulk(PAIRS, since_days=400)
com = db.get_snapshots_bulk(PAIRS, since_days=400, compact=True)
check("the default is still pydantic models", all(isinstance(s.rows[0], BaseModel) for v in pyd.values() for s in v))
check("compact=True is not (a tuple per row, a slotted object per snapshot)", all(isinstance(r, tuple) and not isinstance(r, BaseModel) for v in com.values() for s in v for r in s.rows)
      and all(not hasattr(s, "__dict__") for v in com.values() for s in v))
check("same keys, same snapshots in the same (ascending) order, same rows, same values and TYPES", diff(pyd, com) == "", diff(pyd, com))
check("... and there is something to compare (5 locations, 12 snapshots, the spot row, the canola scaling)",
      len(com) == 5 and sum(len(v) for v in com.values()) == 12 and any(r.isSpot for s in com[("ADM", "B, IA")] for r in s.rows)
      and [r.basisCents for r in com[("ADM", "Velva, ND")][-1].rows] == [-43, -45, None], (len(com), [r.basisCents for r in com[("ADM", "Velva, ND")][-1].rows]))
check("each location's snapshots ascend by timestamp", all([s.timestamp for s in v] == sorted(s.timestamp for s in v) for v in com.values()))
check("a snapshot's two never-read email fields are None, as on the pydantic one", all(s.emailSubject is None and s.emailDate is None for v in com.values() for s in v))
check("a narrower window gives the same cut both ways", diff(db.get_snapshots_bulk(PAIRS, since_days=3), db.get_snapshots_bulk(PAIRS, since_days=3, compact=True)) == ""
      and len(db.get_snapshots_bulk(PAIRS, since_days=3, compact=True)[("ADM", "A, IL")]) == 2)
check("no pairs: {} (and no query)", db.get_snapshots_bulk([], compact=True) == {} and db.get_snapshots_bulk([("Nobody", "Nowhere")], compact=True) == {})

print("\nchunking: the cursor is read a few rows at a time and nothing is lost between chunks")
for n in (1, 2, 3, 7, 11):
    with mock.patch.object(db, "_BULK_CHUNK", n):
        check("chunks of %d rows: identical to one big read" % n, diff(pyd, db.get_snapshots_bulk(PAIRS, since_days=400, compact=True)) == "")


class FakeCursor:
    """Rows as the cursor wrapper hands them back (lowercase keys), `fetchmany` in the asked size; fetchall for the pydantic path."""
    def __init__(self, rows):
        self.rows, self.i, self.sizes = list(rows), 0, []

    def execute(self, sql, params=None):
        return self

    def fetchmany(self, size=None):
        self.sizes.append(size)
        out = self.rows[self.i:self.i + size]
        self.i += len(out)
        return out

    def fetchall(self):
        out, self.i = self.rows[self.i:], len(self.rows)
        return out


class FakeConn:
    def __init__(self, rows):
        self.cur, self.closed = FakeCursor(rows), 0

    def cursor(self):
        return self.cur

    def close(self):
        self.closed += 1


def raw(sid, ts, prov, loc, rid, grain="Corn", dm="Dec 2026", fs="ZCZ26", basis=-10, spot=0, sg=None, src="web"):
    return {"snap_id": sid, "timestamp": ts, "provider": prov, "location": loc, "source": src, "row_id": rid, "grain": grain, "delivery_month": dm,
            "futures_symbol": fs, "basis_cents": basis, "is_spot": spot, "spot_grain": sg}


def fake_load(rows, compact, chunk=None):
    conn = FakeConn(rows)
    with mock.patch.object(db, "get_conn", lambda: conn), mock.patch.object(db, "_BULK_CHUNK", chunk or db._BULK_CHUNK):
        res = db.get_snapshots_bulk([("P", "L")], since_days=400, compact=compact)
    return res, conn


# Snowflake has no unique key: two snapshots may share (provider, location, timestamp), and their rows then interleave in `ORDER BY ..., r.id`.
INTER = [raw(1, "2026-11-12T00:00:00Z", "P", "L", "a1"), raw(2, "2026-11-12T00:00:00Z", "P", "L", "b1"), raw(1, "2026-11-12T00:00:00Z", "P", "L", "a2", dm="Jan 2027", fs="ZCH27"),
         raw(2, "2026-11-12T00:00:00Z", "P", "L", "b2", dm="Jan 2027", fs="ZCH27"), raw(3, "2026-11-13T00:00:00Z", "P", "L", "c1", spot=1, sg="Corn", dm="Spot", fs=""),
         raw(1, "2026-11-12T00:00:00Z", "P", "L", "a3", dm="Mar 2027", fs="ZCH27")]
want, _ = fake_load(INTER, compact=False)
check("(the fake cursor feeds the pydantic path the same rows: 3 snapshots, ids 1 and 2 sharing a timestamp)",
      [(s.id, [r.id for r in s.rows]) for s in want[("P", "L")]] == [(1, ["a1", "a2", "a3"]), (2, ["b1", "b2"]), (3, ["c1"])], [(s.id, [r.id for r in s.rows]) for s in want[("P", "L")]])
for n in (1, 2, 4, 100):
    got, conn = fake_load(INTER, compact=True, chunk=n)
    check("interleaved snapshot ids, chunks of %d: rows are grouped by snapshot id exactly as the pydantic path does" % n, diff(want, got) == "", diff(want, got))
check("the cursor is asked for the configured chunk size, to the end (a short final batch, then an empty one)", fake_load(INTER, True, 4)[1].cur.sizes == [4, 4, 4], fake_load(INTER, True, 4)[1].cur.sizes)

print("\nthe connection and the failure paths")
_, c1 = fake_load(INTER, compact=True)
_, c0 = fake_load(INTER, compact=False)
check("the connection is closed exactly once, compact or not", c1.closed == 1 and c0.closed == 1, (c1.closed, c0.closed))
conn = FakeConn(INTER)
with mock.patch.object(db, "get_conn", lambda: conn), mock.patch.object(db, "_compact_snapshots", mock.Mock(side_effect=RuntimeError("boom"))):
    try:
        db.get_snapshots_bulk([("P", "L")], compact=True)
        raised = False
    except RuntimeError:
        raised = True
check("a failure while reading still closes the connection, and is raised", raised and conn.closed == 1, (raised, conn.closed))

print("\nvalues are carried, not 'fixed'")
ODD = [raw(1, "2026-11-12T00:00:00Z", "P", "L", "r1", basis=0), raw(1, "2026-11-12T00:00:00Z", "P", "L", "r2", basis=None), raw(1, "2026-11-12T00:00:00Z", "P", "L", "r3", basis=-4250, grain="Canola (Us)"),
       raw(1, "2026-11-12T00:00:00Z", "P", "L", "r4", basis=7, spot=True, sg="Corn"), raw(1, "2026-11-12T00:00:00Z", "P", "L", "r5", basis=300, fs="")]
g, _ = fake_load(ODD, compact=True)
rows = g[("P", "L")][0].rows
check("0 stays 0 (not None), None stays None, a canola -4250 reads -43, a truthy is_spot becomes True, an empty symbol stays ''",
      [r.basisCents for r in rows] == [0, None, -43, 7, 300] and rows[0].basisCents is not None and [r.isSpot for r in rows] == [False, False, False, True, False]
      and all(type(r.isSpot) is bool for r in rows) and rows[4].futuresSymbol == "", [(r.basisCents, r.isSpot) for r in rows])
check("_int_like: an int is the same object, None stays, a whole float / Decimal becomes an int, anything else is kept",
      db._int_like(5) == 5 and type(db._int_like(5)) is int and db._int_like(None) is None and db._int_like(5.0) == 5 and type(db._int_like(5.0)) is int
      and type(db._int_like(Decimal("-12"))) is int and db._int_like(5.5) == 5.5 and db._int_like("x") == "x" and db._int_like(Decimal("2.5")) == Decimal("2.5"))

print("\nthe memory shape (what the droplet needed)")
N = 20000
BIG = [raw(1 + i // 10, "2026-11-%02dT00:00:00Z" % (1 + (i // 10) % 28), "P", "L%d" % ((i // 10) % 40), "".join(["R", str(i % 10)]), grain="".join(["Co", "rn"]), dm="".join(["Dec ", "2026"]),
           fs="".join(["ZC", "Z26"]), basis=(i % 90) - 45) for i in range(N)]


def retained(compact):
    conn = FakeConn(BIG)
    with mock.patch.object(db, "get_conn", lambda: conn):
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        res = db.get_snapshots_bulk([("P", "L")], since_days=400, compact=compact)
        after = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
    return after - before, res


mem_p, rp = retained(False)
mem_c, rc = retained(True)
check("%d rows come out the same both ways" % N, diff(rp, rc) == "")
check("... and what compact keeps is under a quarter of what the pydantic models keep (%.1f vs %.1f MB for %d rows)" % (mem_c / 1e6, mem_p / 1e6, N), mem_c * 4 < mem_p, (mem_c, mem_p))
rows_c = [r for v in rc.values() for s in v for r in s.rows]
check("a string repeated across rows is ONE object (grain, delivery month, symbol, row id)", rows_c[0].grain is rows_c[-1].grain and rows_c[0].deliveryMonth is rows_c[-1].deliveryMonth
      and rows_c[0].futuresSymbol is rows_c[-1].futuresSymbol and rows_c[0].id is rows_c[10].id)

print("\nthe Changes email asks for it, and builds the same email either way")
asked = []
real_bulk = db.get_snapshots_bulk


def spy(pairs, since_days=400, compact=False):
    asked.append((len(pairs), since_days, compact))
    return real_bulk(pairs, since_days=since_days, compact=compact)


def force_pydantic(pairs, since_days=400, compact=False):
    return real_bulk(pairs, since_days=since_days, compact=False)


CURVE = {code(*M1): 450.0, code(*M2): 464.0}
cr._GM = {}


def email():
    with mock.patch.object(cr, "_futures_curve", lambda: CURVE), mock.patch.object(db, "get_roll_spread", lambda a, b: None), \
            mock.patch.object(cr, "_table_watermark", lambda: None), mock.patch.object(cr, "_index_excludes", lambda: set()):
        return cr.build_changes_email_html()


with mock.patch.object(cr, "get_snapshots_bulk", spy):
    html_c = email()
check("every load the report makes is compact=True, 400 days", asked and all(a[1] == 400 and a[2] is True for a in asked), asked)
with mock.patch.object(cr, "get_snapshots_bulk", force_pydantic):
    html_p = email()
check("the email built from compact objects is IDENTICAL, byte for byte, to the one built from pydantic models", html_c == html_p, (len(html_c), len(html_p)))
TXT = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_c))
check("... and it is a real email: the Corn Processing trend tables, and ADM A's row in its change table (basis +10, up 3 on the day, the roll-free 'vs' contract)",
      html_c.count("AVG BASIS CHANGE") >= 1 and re.search(r"ADM A vs C[FGHJKMNQUVXZ] \+10 \+3 ", TXT) is not None and "C, NE" in html_c and "No data." in html_c and len(html_c) > 8000,
      (len(html_c), html_c.count("AVG BASIS CHANGE"), TXT[TXT.find("Location"):TXT.find("Location") + 200]))
with mock.patch.object(cr, "get_snapshots_bulk", spy):
    del asked[:]
    one = cr._load_fresh("Corn Processing")
check("_load_fresh itself: compact=True, and it still returns (pairs, meta, data, now) for the type's locations",
      asked == [(3, 400, True)] and len(one) == 4 and sorted(one[0]) == [("ADM", "A, IL"), ("ADM", "B, IA"), ("CHS", "C, NE")] and one[3].hour == 12, (asked, one[0]))

print("\nthe daily send says what the build cost (so the next run shows its footprint)")


class Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


grab = Grab()
cr.log.addHandler(grab)
_old_level = cr.log.level
cr.log.setLevel(logging.INFO)
logging.disable(logging.NOTSET)
try:
    with mock.patch.object(cr, "build_changes_email_html", lambda mode="spot": "<p>x</p>"), mock.patch.object(cr, "signature_html", lambda: ""), \
            mock.patch.object(cr, "_table_watermark", lambda: None), mock.patch.object(cr, "send_email", lambda *a, **k: "graph"):
        sent = cr.send_daily_changes_email("someone@example.com", cc=None)
finally:
    logging.disable(logging.CRITICAL)
    cr.log.removeHandler(grab)
    cr.log.setLevel(_old_level)
built = [i for i, m in enumerate(grab.msgs) if m.startswith("Daily Changes email built in")]
sent_at = [i for i, m in enumerate(grab.msgs) if m.startswith("Daily Changes email sent via")]
check("the send still returns True and logs ONE 'built in N s (K KB); peak memory ... before / after' line, ahead of the 'sent via' line",
      sent is True and len(built) == 1 and len(sent_at) == 1 and built[0] < sent_at[0] and "peak memory" in grab.msgs[built[0]] and "KB" in grab.msgs[built[0]], grab.msgs)
pk = cr._peak_rss_mb()
check("_peak_rss_mb: a positive whole number of MB where the platform can tell (Linux, macOS), else None (Windows)", (pk is None) if os.name == "nt" else (isinstance(pk, int) and pk > 0), pk)
with mock.patch.dict(sys.modules, {"resource": None}):
    check("... and None, never an exception, where it cannot be read", cr._peak_rss_mb() is None)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
