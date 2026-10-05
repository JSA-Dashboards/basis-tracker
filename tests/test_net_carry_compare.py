"""The Net Carry tab's side-by-side comparison: same reference / curve / interest clock for every column,
tops and row-bests marked, gross vs net, and the two lean database queries that feed it.

    python tests/test_net_carry_compare.py

The database part runs against a throwaway SQLite file (nothing touches Snowflake).
"""
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                 # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="nc_cmp_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
import net_carry as nc                # noqa: E402
import net_carry_compare as cmp_      # noqa: E402
from net_carry_compare import Entry   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-9):
    return a is not None and abs(a - b) <= tol


RATE = 0.0613
CURVE = {"ZCZ26": 502.25, "ZCH27": 516.75, "ZCK27": 523.75, "ZCN27": 528.0, "ZCU26": 480.0}
STL = [{"delivery": "NC 26", "futures": "ZCZ26", "basis": -25}, {"delivery": "Nov 26", "futures": "ZCZ26", "basis": 3},
       {"delivery": "Dec 26", "futures": "ZCZ26", "basis": 19}, {"delivery": "Jan 27", "futures": "ZCH27", "basis": 14},
       {"delivery": "Feb 27", "futures": "ZCH27", "basis": 18}, {"delivery": "Mar 27", "futures": "ZCH27", "basis": 21}]
CSX = [{"delivery": "FH Oct", "futures": "ZCZ26", "basis": -10}, {"delivery": "Nov", "futures": "ZCZ26", "basis": 16},
       {"delivery": "FH Dec", "futures": "ZCH27", "basis": 8}, {"delivery": "Dec", "futures": "ZCH27", "basis": 14},
       {"delivery": "JFM", "futures": "ZCH27", "basis": 17}]
# a plant that starts quoting in NOVEMBER (no October row) and quotes everything off the March contract
LATE = [{"delivery": "Nov 26", "futures": "ZCH27", "basis": -4}, {"delivery": "Dec 26", "futures": "ZCH27", "basis": -2},
        {"delivery": "Jan 27", "futures": "ZCH27", "basis": 0}]
ANCHOR_YM = (2026, 10)
ASOF = date(2026, 10, 2)
ENT = [Entry("b|ADM|STL", "ADM · St. Louis", "basis", STL, ASOF, is_main=True),
       Entry("r|CSX Columbus", "CSX Columbus", "rail", CSX, ASOF),
       Entry("b|X|late", "X · Late", "basis", LATE, date(2026, 9, 28))]

print("net_carry_compare: every column on the same reference, curve and interest clock")
res = cmp_.build_comparison(ENT, "ZCZ26", CURVE, 10, ANCHOR_YM, RATE, "net", ASOF)
cols = res["columns"]
check("three columns, in the order given", [c["entry"].name for c in cols] == ["ADM · St. Louis", "CSX Columbus", "X · Late"])
check("rows = the union of months, in time order", [lab for _, lab in res["months"]] == ["Oct 26", "Nov 26", "Dec 26", "Jan 27", "Feb 27", "Mar 27"], res["months"])
rows_stl, meta_stl = nc.compute_net_carry(STL, "ZCZ26", CURVE, 10, RATE, anchor_ym=ANCHOR_YM)
pts_stl = nc.monthly_carry(rows_stl)
check("a column's numbers are exactly the main table's (same function, same inputs)",
      all(close(cols[0]["points"][p["ym"]]["value"], p["net"]) for p in pts_stl))
check("the NC point sits on the October row and is flagged NC", cols[0]["points"][(2026, 10)]["nc"] and not cols[1]["points"][(2026, 10)]["nc"])
check("a month a location doesn't quote is simply absent (Late has no Oct; CSX has no Feb — its JFM package sits on Mar)", (2026, 10) not in cols[2]["points"] and (2027, 2) not in cols[1]["points"] and (2027, 3) in cols[1]["points"])

print("net_carry_compare: ONE interest clock — a location without an October row still accrues from October")
own, _ = nc.compute_net_carry(LATE, "ZCZ26", CURVE, 10, RATE)                       # its own anchor = its front (Nov)
shared, _ = nc.compute_net_carry(LATE, "ZCZ26", CURVE, 10, RATE, anchor_ym=ANCHOR_YM)
check("on its own it would start the clock in November (Nov interest 0) — not comparable", own[0].interest == 0.0 and own[0].days == 0)
check("with the shared anchor Nov carries 31 days of interest, like every other column",
      shared[0].days == 31 and close(shared[0].interest, 502.25 * RATE * 31 / 360), (shared[0].days, shared[0].interest))
check("the comparison uses the shared one", close(cols[2]["points"][(2026, 11)]["value"], shared[0].net) and
      close(cols[0]["points"][(2026, 11)]["value"], [p for p in pts_stl if p["ym"] == (2026, 11)][0]["net"]))
check("same days, same board price: Nov interest is identical for STL's own run and the late plant's shared-clock run",
      close(rows_stl[1].interest, shared[0].interest) and rows_stl[1].days == shared[0].days == 31)

print("net_carry_compare: tops, gains and row-bests")
check("each column's top is the highest value in that column from the carry start",
      all(close(c["top"]["net"], max(p["value"] for ym, p in c["points"].items() if ym >= ANCHOR_YM)) for c in cols))
check("STL's top is its last month (every month adds net), CSX's is Dec", cols[0]["top"]["label"] == "Mar 27" and cols[1]["top"]["label"] == "Dec 26", (cols[0]["top"]["label"], cols[1]["top"]["label"]))
check("gain = top - front (the first month at or after the start)",
      close(cols[0]["top"]["gain"], cols[0]["top"]["net"] - cols[0]["front"]["net"]) and cols[0]["front"]["label"] == "NC 26")
rb = res["row_best"]
check("the best value in a month is picked across the columns that quote it",
      all(max(c["points"][ym]["value"] for c in cols if ym in c["points"]) == cols[i]["points"][ym]["value"]
          for ym, idx in rb.items() for i in idx))
check("no row-best for a month only one location quotes (Feb 27: only STL)", (2027, 2) not in rb and (2027, 3) in rb)
tied = cmp_.build_comparison([Entry("a", "A", "basis", [{"delivery": "Nov 26", "futures": "ZCZ26", "basis": 5}], ASOF),
                              Entry("b", "B", "basis", [{"delivery": "Nov 26", "futures": "ZCZ26", "basis": 5}], ASOF)],
                             "ZCZ26", CURVE, 10, ANCHOR_YM, RATE, "net", ASOF)
check("an exact tie marks both", tied["row_best"][(2026, 11)] == [0, 1])

print("net_carry_compare: gross = basis vs REF before interest")
rg = cmp_.build_comparison(ENT, "ZCZ26", CURVE, 10, ANCHOR_YM, RATE, "gross", ASOF)
check("gross values are the basis vs REF", all(close(rg["columns"][0]["points"][p["ym"]]["value"], p["basis_ref"]) for p in pts_stl))
check("net - gross = -interest, month by month (STL)",
      all(close(cols[0]["points"][p["ym"]]["value"] - rg["columns"][0]["points"][p["ym"]]["value"], -(p["interest"] or 0.0)) for p in pts_stl))
check("gross never has a lower top than net (no interest to subtract)", all(rg["columns"][i]["top"]["net"] >= cols[i]["top"]["net"] - 1e-9 for i in range(3)))
check("the gross top is the highest basis vs REF", close(rg["columns"][0]["top"]["net"], max(p["basis_ref"] for p in pts_stl)))
try:
    cmp_.build_comparison(ENT, "ZCZ26", CURVE, 10, ANCHOR_YM, RATE, "bogus", ASOF)
    check("an unknown measure is rejected", False)
except ValueError:
    check("an unknown measure is rejected", True)

print("net_carry_compare: staleness and missing data")
check("a quote older than the as-of date is flagged stale; same-day ones are not", cols[2]["stale"] and not cols[0]["stale"] and not cols[1]["stale"])
empty = cmp_.build_comparison([Entry("z", "Z", "basis", [], None), ENT[0]], "ZCZ26", CURVE, 10, ANCHOR_YM, RATE, "net", ASOF)
check("an entry with no quotes is a 'no data' column, and the others still work",
      empty["columns"][0]["no_data"] and empty["columns"][0]["top"] is None and empty["columns"][1]["top"] is not None)
check("nothing at all -> no rows, no crash", cmp_.build_comparison([], "ZCZ26", CURVE, 10, ANCHOR_YM, RATE)["months"] == [])
noref = cmp_.build_comparison(ENT[:1], None, CURVE, 10, ANCHOR_YM, RATE, "net", ASOF)
check("no reference contract: raw basis, no crash", noref["columns"][0]["points"] and noref["columns"][0]["top"] is not None)

print("net_carry_compare: the HTML")
h = cmp_.render_html(res)
check("one header per location, main marked with a star, rail marked with a train", h.count("<th style") == 4 and "★" in h and "🚂" in h)
check("the quote date shows only where it isn't the as-of date", "as of 9/28" in h and h.count("as of ") == 1)
check("each column's top month is highlighted amber", h.count("background:#fff4e5") >= 3 + 3)
check("the footer names the top month and the carry gained",
      "Top of net carry" in h and "Net carry vs the start" in h and "Mar 27" in h and "Dec 26" in h)
check("gross view is labelled as such", "Top of gross carry" in cmp_.render_html(rg) and "before interest" in cmp_.render_html(rg))
check("a column's name is escaped", "&lt;b&gt;" in cmp_.render_html(cmp_.build_comparison(
    [Entry("e", "<b>x</b>", "basis", STL, ASOF)], "ZCZ26", CURVE, 10, ANCHOR_YM, RATE, "net", ASOF)))
check("best-in-month cells are green", "color:#0a7f3f" in h)

print("net_carry_compare: choosing peers")
coords = {"a": (38.6, -90.2), "b": (38.7, -90.1), "c": (41.9, -87.6), "d": (35.1, -90.0), "e": (47.0, -100.0)}
check("nearest_peers: closest first, never itself", cmp_.nearest_peers("a", coords, ["a", "b", "c", "d", "e"], 3) == ["b", "d", "c"])
check("a candidate with no coordinates is skipped; a main with none -> []",
      cmp_.nearest_peers("a", coords, ["b", "zz"], 3) == ["b"] and cmp_.nearest_peers("zz", coords, ["a"], 3) == [])
check("haversine: Chicago -> St. Louis is ~420 km", 380 < cmp_.haversine_km((41.88, -87.63), (38.63, -90.2)) < 460)
check("rail_peers: same railroad first, then the rest, alphabetical, never itself",
      cmp_.rail_peers("UP Group 3", ["UP Group 3", "UP Interior IA", "CSX Columbus", "UP Illinois (Dom)", "BN PNW"], 3)
      == ["UP Illinois (Dom)", "UP Interior IA", "BN PNW"])
check("pick_latest: newest on or before the as-of date, within the window",
      cmp_.pick_latest([date(2026, 9, 20), date(2026, 9, 29), date(2026, 10, 5)], date(2026, 10, 2)) == date(2026, 9, 29)
      and cmp_.pick_latest([date(2026, 9, 1)], date(2026, 10, 2)) is None and cmp_.pick_latest([], date(2026, 10, 2)) is None)
rr = [{"market": "CSX Columbus", "commodity": "Corn", "date": "2026-10-02", "period": "Nov", "futures": "ZCZ26", "bid": 16},
      {"market": "CSX Columbus", "commodity": "Corn", "date": "2026-09-25", "period": "Nov", "futures": "ZCZ26", "bid": 12},
      {"market": "CSX Columbus", "commodity": None, "date": "2026-10-02", "period": "Dec", "futures": "ZCH27", "bid": 14},
      {"market": "UP Group 3", "commodity": "Corn", "date": "2026-10-02", "period": "Nov", "futures": "ZCZ26", "bid": 5},
      {"market": "CSX Columbus", "commodity": "Soybeans", "date": "2026-10-02", "period": "Nov", "futures": "ZSX26", "bid": 9},
      {"market": "CSX Columbus", "commodity": "Corn", "date": "2026-10-02", "period": "Jan", "futures": "ZCH27", "bid": None}]
check("rail_items: one corridor, one commodity, one date; a blank commodity means corn; no bid -> skipped",
      [i["delivery"] for i in cmp_.rail_items(rr, "CSX Columbus", "Corn", "2026-10-02")] == ["Nov", "Dec"])
check("rail_dates", cmp_.rail_dates(rr, "CSX Columbus", "Corn") == [date(2026, 9, 25), date(2026, 10, 2)])

print("database: the two lean queries (throwaway SQLite)")
db.init_db()


def snap(ts, prov, loc, rows, src="web"):
    return {"timestamp": ts, "provider": prov, "location": loc, "source": src,
            "rows": [{"id": i + 1, "grain": g, "deliveryMonth": dm, "futuresSymbol": fs, "basisCents": b, "isSpot": sp, "spotGrain": None}
                     for i, (g, dm, fs, b, sp) in enumerate(rows)]}


db.upsert_snapshots([
    snap("2026-09-28T20:00:00Z", "ADM", "STL", [("Corn", "Oct", "ZCZ26", -40, False), ("Corn", "Nov", "ZCZ26", 3, False)]),
    snap("2026-10-01T20:00:00Z", "ADM", "STL", [("Corn", "Oct", "ZCZ26", -35, False), ("Corn", "Nov", "ZCZ26", 5, False), ("Soybeans", "Nov", "ZSX26", 20, False)]),
    snap("2026-10-02T20:00:00Z", "ADM", "STL", [("Corn", "Oct", "ZCZ26", -30, False), ("Corn", "Nov", "ZCZ26", 6, False)]),
    snap("2026-10-02T21:00:00Z", "ADM", "STL", [("Corn", "Oct", "ZCZ26", -25, False)]),            # a later scrape the same day wins
    snap("2026-09-10T20:00:00Z", "GPC", "Old", [("Corn", "Oct", "ZCZ26", -10, False)]),           # too old for a 10-day window
    snap("2026-09-30T20:00:00Z", "GPC", "Wash", [("Corn", "Oct", "ZCZ26", -17, False)]),
    snap("2026-10-02T20:00:00Z", "SPOT", "Only", [("Corn", "Spot", "", 7, True)]),                 # spot-only: not a forward location
    snap("2026-10-05T20:00:00Z", "ADM", "STL", [("Corn", "Oct", "ZCZ26", -20, False)]),            # after the as-of date: never seen
])
got = db.get_snapshots_asof([("ADM", "STL"), ("GPC", "Wash"), ("GPC", "Old"), ("NOPE", "None")], "2026-10-02", 10)
check("the latest snapshot on or before the as-of date, one per location", set(got) == {("ADM", "STL"), ("GPC", "Wash")}, sorted(got))
check("same-day: the later scrape wins; the day after is never seen",
      got[("ADM", "STL")].timestamp.startswith("2026-10-02T21") and [r.basisCents for r in got[("ADM", "STL")].rows] == [-25])
check("older than the window -> absent", ("GPC", "Old") not in got)
check("rows come back as SnapshotRow with the right types", got[("GPC", "Wash")].rows[0].futuresSymbol == "ZCZ26"
      and got[("GPC", "Wash")].rows[0].isSpot is False and got[("GPC", "Wash")].rows[0].basisCents == -17)
check("an empty pair list -> {}", db.get_snapshots_asof([], "2026-10-02") == {})
wide = db.get_snapshots_asof([("GPC", "Old")], "2026-10-02", 30)
check("a wider window finds it", ("GPC", "Old") in wide)
fl = db.get_forward_locations("2026-10-02", 10)
check("forward locations in the window: distinct (provider, location, grain), spot-only excluded",
      sorted((r["provider"], r["location"], r["grain"]) for r in fl)
      == [("ADM", "STL", "Corn"), ("ADM", "STL", "Soybeans"), ("GPC", "Wash", "Corn")], sorted(map(tuple, (tuple(r.values()) for r in fl))))
check("the window is anchored on the as-of date", db.get_forward_locations("2026-09-12", 5) == [{"provider": "GPC", "location": "Old", "grain": "Corn"}])

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
