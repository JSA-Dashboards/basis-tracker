"""Hollander & Feuerhaken's Incobrasa block folded into the existing INCO / Gilman, IL series (inco_bids.fold_hf).

    python tests/test_inco_hf_merge.py

hfgrain.com's daily sheet quotes Incobrasa for Oct / Nov / Dec only — a SUBSET of the six-month sheet Inco sends and Kolten pastes (Kolten 2026-10-23: "Add inco into the existing one we have").
The block must replace those months on the sheet's own day, leave Jan-Mar as the last Inco sheet had them, leave the days before alone, re-carry the days after, be idempotent, and not hide
a forgotten Inco sheet from the carry guard. A throwaway SQLite database stands in for Snowflake; nothing touches the network.
"""
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                                       # noqa: E402
import inco_bids as ib                                      # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="inco_hf_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
db.init_db()
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:600]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


# The sheet Kolten pasted on Mon 2026-10-19: six months (what archive INCO held on 10-22).
SHEET = [("Oct 2026", "ZSX26", 20), ("Nov 2026", "ZSX26", 25), ("Dec 2026", "ZSF27", 15), ("Jan 2027", "ZSF27", 20), ("Feb 2027", "ZSH27", 20), ("Mar 2027", "ZSH27", 30)]


def rows(triples):
    return [{"grain": "Soybeans", "deliveryMonth": l, "futuresSymbol": f, "basisCents": b, "isSpot": False, "spotGrain": None} for l, f, b in triples]


def loc(d, triples):
    return {"sheet_date": d, "bids": [{"grain": "Soybeans", "delivery": l, "cme_symbol": f, "basis_cents": b, "bid_id": "HF_IB_S%d" % i} for i, (l, f, b) in enumerate(triples, 1)]}


def day(d):
    """{label: (symbol, basis)} of the INCO snapshot dated d, and its subject; None when there is none."""
    sn = [s for s in ib.load_snaps() if s["d"] == date.fromisoformat(d)]
    if not sn:
        return None, None
    return {r["deliveryMonth"]: (r["futuresSymbol"], r["basisCents"]) for r in ib.load_rows(sn[0]["id"])}, sn[0]["email_subject"]


def post_sheet(d, triples, today):
    plan = ib.plan_post(ib.load_snaps(), date.fromisoformat(d), rows(triples), today)
    return ib.apply_plan(plan)


T22 = date(2026, 10, 22)
print("inco: the starting point (the 10-19 sheet, carried to 10-22)")
check("the hand sheet is posted and carried through 10-22", post_sheet("2026-10-19", SHEET, T22))
check("10-22 is a carried day holding all six months", day("2026-10-22")[0] == {l: (f, b) for l, f, b in SHEET} and ib.is_carried([s for s in ib.load_snaps() if s["d"] == T22][0]))

print("\ninco: H&F's sheet of 10-22 (Oct unchanged, Nov +27, Dec unchanged)")
H1 = [("Oct 2026", "ZSX26", 20), ("Nov 2026", "ZSX26", 27), ("Dec 2026", "ZSF27", 15)]
res = ib.fold_hf(loc("2026-10-22", H1), today=T22)
d22, subj22 = day("2026-10-22")
check("written and read back", res is True, res)
check("Nov is H&F's +27; Oct and Dec as quoted; Jan / Feb / Mar still the 10-19 sheet's (+20 / +20 / +30): six rows, no duplicates", d22 == {"Oct 2026": ("ZSX26", 20), "Nov 2026": ("ZSX26", 27), "Dec 2026": ("ZSF27", 15),
      "Jan 2027": ("ZSF27", 20), "Feb 2027": ("ZSH27", 20), "Mar 2027": ("ZSH27", 30)}, d22)
check("the day says so, and is still 'carried' from the last REAL Inco sheet (10-19)", subj22 == "carried forward from 2026-10-19 sheet; Oct/Nov/Dec from H&F bids PDF of 2026-10-22", subj22)
snaps = ib.load_snaps()
check("it is still the same single INCO snapshot per day, source 'manual'", [s["d"].isoformat() for s in snaps] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"] and {s["source"] for s in snaps} == {"manual"})
check("the days before are untouched (10-20 and 10-21 still hold the hand sheet's Nov +25)", day("2026-10-21")[0]["Nov 2026"] == ("ZSX26", 25) and day("2026-10-20")[0]["Nov 2026"] == ("ZSX26", 25))

print("\ninco: the same sheet again (the 17:10 pass, or a second run)")
before = [(s["id"], s["d"]) for s in ib.load_snaps()]
check("nothing to write: the series already holds it", ib.fold_hf(loc("2026-10-22", H1), today=T22) is None)
check("... and not a row moved (same snapshot ids)", [(s["id"], s["d"]) for s in ib.load_snaps()] == before)

print("\ninco: H&F re-issues the sheet (REVISED) with Nov +30")
H2 = [("Oct 2026", "ZSX26", 20), ("Nov 2026", "ZSX26", 30), ("Dec 2026", "ZSF27", 15)]
check("the day is replaced", ib.fold_hf(loc("2026-10-22", H2), today=T22) is True)
d22, _ = day("2026-10-22")
check("Nov +30 and still exactly six rows (the old +27 is gone, not stacked beside it)", d22["Nov 2026"] == ("ZSX26", 30) and len(d22) == 6 and len(ib.load_rows([s for s in ib.load_snaps() if s["d"] == T22][0]["id"])) == 6)

print("\ninco: the next day, before H&F has posted (the 15:45 carry)")
T23 = date(2026, 10, 23)
plan = ib.plan_carry(ib.load_snaps(), T23)
check("the carry copies the H&F-updated day, not the old hand sheet (Nov +30)", len(plan["write"]) == 1 and {r["deliveryMonth"]: r["basisCents"] for r in plan["write"][0]["rows"]}["Nov 2026"] == 30, plan["notes"])
check("... and says it is carried from the last REAL sheet (10-19): the stale-sheet guard still measures from Kolten's paste", plan["write"][0]["emailSubject"] == "carried forward from 2026-10-19 sheet", plan["write"][0].get("emailSubject"))
check("with the last real sheet more than 21 days old the carry refuses (a visible gap), H&F's refreshes notwithstanding", ib.plan_carry(ib.load_snaps(), date(2026, 11, 12))["write"] == [])
ib.apply_plan(plan)

print("\ninco: H&F's sheet of 10-23 arrives later that day")
H3 = [("Oct 2026", "ZSX26", 22), ("Nov 2026", "ZSX26", 30), ("Dec 2026", "ZSF27", 17)]
check("the carried 10-23 is replaced by the folded day", ib.fold_hf(loc("2026-10-23", H3), today=T23) is True)
d23, subj23 = day("2026-10-23")
check("Oct +22, Dec +17 from H&F; Jan-Mar from the hand sheet", d23["Oct 2026"] == ("ZSX26", 22) and d23["Dec 2026"] == ("ZSF27", 17) and d23["Jan 2027"] == ("ZSF27", 20) and len(d23) == 6, d23)
check("10-22 keeps its own H&F values (Oct +20)", day("2026-10-22")[0]["Oct 2026"] == ("ZSX26", 20))

print("\ninco: a sheet captured late for an earlier day re-carries the days after it")
T26 = date(2026, 10, 26)
ib.apply_plan(ib.plan_carry(ib.load_snaps(), T26))                      # 10-26 carried from the 10-23 folded day
H4 = [("Oct 2026", "ZSX26", 40), ("Nov 2026", "ZSX26", 30), ("Dec 2026", "ZSF27", 17)]
check("folding a sheet dated 10-23 again with new Oct (+40) writes", ib.fold_hf(loc("2026-10-23", H4), today=T26) is True)
check("10-26 (carried) now follows the new 10-23: Oct +40", day("2026-10-26")[0]["Oct 2026"] == ("ZSX26", 40), day("2026-10-26")[0])

print("\ninco: an 'LH Oct' from H&F replaces the whole of October, not just the plain 'Oct' row")
T27 = date(2026, 10, 27)
H5 = [("LH Oct 2026", "ZSX26", 35), ("Nov 2026", "ZSX26", 30), ("Dec 2026", "ZSF27", 17)]
check("folded", ib.fold_hf(loc("2026-10-27", H5), today=T27) is True)
d27, _ = day("2026-10-27")
check("one October row (LH Oct +35), no stray plain Oct", [k for k in d27 if "Oct" in k] == ["LH Oct 2026"] and d27["LH Oct 2026"] == ("ZSX26", 35), d27)

print("\ninco: months that have expired are dropped, like the carry does")
T2 = date(2026, 11, 2)
H6 = [("Nov 2026", "ZSX26", 31), ("Dec 2026", "ZSF27", 18), ("Jan 2027", "ZSF27", 21)]
check("a November sheet folds in (the hand sheet is 14 days old: inside the 21-day limit)", ib.fold_hf(loc("2026-11-02", H6), today=T2) is True)
d2, _ = day("2026-11-02")
check("no October row (expired); Nov / Dec / Jan from H&F; Feb / Mar still the hand sheet's", sorted(d2) == ["Dec 2026", "Feb 2027", "Jan 2027", "Mar 2027", "Nov 2026"] and d2["Jan 2027"] == ("ZSF27", 21) and d2["Feb 2027"] == ("ZSH27", 20), d2)

print("\ninco: the hand sheet is forgotten (more than 21 days old)")
T20 = date(2026, 11, 20)                                                # 10-19 + 32 days
H7 = [("Nov 2026", "ZSX26", 33), ("Dec 2026", "ZSF27", 19), ("Jan 2027", "ZSF27", 23)]
plan = ib.plan_hf(ib.load_snaps(), T20, rows(H7), T20)
wrote = [s for s in plan["write"] if s["timestamp"][:10] == "2026-11-20"][0]
check("the day holds ONLY what H&F quotes — the stale Feb / Mar are not carried into it", sorted(r["deliveryMonth"] for r in wrote["rows"]) == ["Dec 2026", "Jan 2027", "Nov 2026"], [r["deliveryMonth"] for r in wrote["rows"]])
check("... and the plan says why", any("NOT carried" in n for n in plan["notes"]), plan["notes"])

print("\ninco: a day that holds a sheet Kolten POSTED is not replaced by H&F's relay of it (2026-10-26: a real sheet and H&F's daily PDF landed on the same day)")
T23n = date(2026, 11, 23)
POSTED = [("Nov 2026", "ZSX26", 36), ("Dec 2026", "ZSF27", 24), ("Jan 2027", "ZSF27", 28), ("Feb 2027", "ZSH27", 22)]
check("a sheet is posted for 11-23", post_sheet("2026-11-23", POSTED, T23n))
HP = [("Nov 2026", "ZSX26", 33), ("Dec 2026", "ZSF27", 19)]                 # H&F's PDF of the same day, with different bids
res = ib.fold_hf(loc("2026-11-23", HP), today=T23n)
d_p, subj_p = day("2026-11-23")
check("the fold writes nothing", res is None, res)
check("the posted bids stand: Nov +36 and Dec +24, not H&F's +33 / +19", d_p["Nov 2026"] == ("ZSX26", 36) and d_p["Dec 2026"] == ("ZSF27", 24) and len(d_p) == 4, d_p)
check("the day is still a REAL sheet (no 'carried' mark), so the stale-sheet guard keeps counting from it", not subj_p and not ib.is_carried([s for s in ib.load_snaps() if s["d"] == T23n][0]), subj_p)
check("the plan says why", any("posted" in n for n in ib.plan_hf(ib.load_snaps(), T23n, rows(HP), T23n)["notes"]))
T24n = date(2026, 11, 24)
ib.apply_plan(ib.plan_carry(ib.load_snaps(), T24n))
check("the next day (a carried copy of that sheet) still takes H&F's block", ib.fold_hf(loc("2026-11-24", HP), today=T24n) is True and day("2026-11-24")[0]["Nov 2026"] == ("ZSX26", 33))

print("\ninco: no Inco history at all")
db.DB_PATH = Path(tempfile.mkdtemp(prefix="inco_hf_empty_")) / "t.db"
db.init_db()
check("H&F alone starts the series", ib.fold_hf(loc("2026-10-22", H1), today=T22) is True)
dd, sj = day("2026-10-22")
check("three rows, marked as an H&F day (not 'carried', so there is no phantom hand sheet to age)", len(dd) == 3 and sj == "H&F bids PDF of 2026-10-22 (Oct/Nov/Dec)" and not ib.is_carried(ib.load_snaps()[0]), (dd, sj))

print("\ninco: refusals")
check("a weekend-dated sheet is refused, not written", ib.fold_hf(loc("2026-10-24", H1), today=date(2026, 10, 26)) is False and day("2026-10-24")[0] is None)
check("a block with no soybean rows folds nothing", ib.fold_hf({"sheet_date": "2026-10-22", "bids": [{"grain": "Corn", "delivery": "Oct 2026", "cme_symbol": "ZCZ26", "basis_cents": 5}]}, today=T22) is None)
check("dry_run reports there is something to write and writes nothing", ib.fold_hf(loc("2026-10-23", H1), today=T23, dry_run=True) is True and day("2026-10-23")[0] is None)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
