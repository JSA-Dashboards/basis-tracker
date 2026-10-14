"""Canola basis is stored x100 (hundredths of C$ per tonne, like soybean meal's $/ton) and every reader in database.py hands it back as the quoted number
(throwaway SQLite): the Bids / Trends / Summary / Spread tabs and the emails then show -45, not -4500. The stored values are untouched.

    python tests/test_canola_units.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import database as db                 # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="canola_units_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:300]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def snap(ts, prov, loc, rows, src="web"):
    return {"timestamp": ts, "provider": prov, "location": loc, "source": src,
            "rows": [{"id": "R%d" % (i + 1), "grain": g, "deliveryMonth": dm, "futuresSymbol": fs, "basisCents": b, "isSpot": False, "spotGrain": None}
                     for i, (g, dm, fs, b) in enumerate(rows)]}


print("canola units: the readers of database.py (throwaway SQLite)")
db.init_db()
db.upsert_snapshots([
    snap("2026-10-12T00:00:00Z", "ADM", "Velva, ND", [("Canola (Us)", "Nov", "RSF27", -4000), ("Canola (Us)", "FH Oct", "RSF27", -4500), ("Canola (Us)", "Feb", "RSH27", None)]),
    snap("2026-10-13T00:00:00Z", "ADM", "Velva, ND", [("Canola (Us)", "Nov", "RSF27", -3500), ("Canola (Us)", "FH Oct", "RSF27", -4250), ("Canola (Us)", "Feb", "RSH27", 0)]),
    snap("2026-10-13T00:00:00Z", "CHS", "Hallock NG", [("Canola Usd/Cwt", "Oct 2026", "RSX26", -6000), ("Canola Usd/Cwt", "Dec 2026", "RSF27", -4500), ("Corn", "Oct 2026", "ZCZ26", -25)]),
    snap("2026-10-13T00:00:00Z", "Bunge", "Bellevue, OH", [("Canola Meal Picked Up Marion", "Oct", "ZMZ26", 3300), ("Soybean Meal", "Oct", "ZMZ26", 300)]),
])
db.upsert_location_meta("ADM", "Velva, ND", state="ND", facility_type="Rail Terminal", lat=48.0561, lon=-100.929)
db.upsert_location_meta("CHS", "Hallock NG", state="MN", facility_type="Country Elevator", lat=48.77, lon=-96.94)
db.upsert_location_meta("Bunge", "Bellevue, OH", state="OH", facility_type="Soy Processing", lat=41.27, lon=-83.19)


def by_id(snapshot):
    return {r.id: r.basisCents for r in snapshot.rows}


one = db.get_snapshots("ADM", "Velva, ND")
check("get_snapshots: ADM Velva canola reads -40, -45 and (the later snapshot) -35, -43 (a -4250 half-dollar rounds away from zero), 0; a missing basis stays None",
      [by_id(s) for s in one] == [{"R1": -40, "R2": -45, "R3": None}, {"R1": -35, "R2": -43, "R3": 0}], [by_id(s) for s in one])
chs = db.get_snapshots("CHS", "Hallock NG")[0]
check("get_snapshots: CHS 'Canola Usd/Cwt' reads -60 and -45, the corn row in the same snapshot is still -25", by_id(chs) == {"R1": -60, "R2": -45, "R3": -25}, by_id(chs))
bun = db.get_snapshots("Bunge", "Bellevue, OH")[0]
check("get_snapshots: canola MEAL (3300) and soybean meal (300) come back as stored — the app divides those itself", by_id(bun) == {"R1": 3300, "R2": 300}, by_id(bun))

bulk = db.get_snapshots_bulk([("ADM", "Velva, ND"), ("CHS", "Hallock NG"), ("Bunge", "Bellevue, OH")], since_days=36500)
check("get_snapshots_bulk: the same values",
      [by_id(s) for s in bulk[("ADM", "Velva, ND")]] == [{"R1": -40, "R2": -45, "R3": None}, {"R1": -35, "R2": -43, "R3": 0}]
      and by_id(bulk[("CHS", "Hallock NG")][0]) == {"R1": -60, "R2": -45, "R3": -25} and by_id(bulk[("Bunge", "Bellevue, OH")][0]) == {"R1": 3300, "R2": 300})
asof = db.get_snapshots_asof([("ADM", "Velva, ND"), ("CHS", "Hallock NG")], "2026-10-13", 10)
check("get_snapshots_asof: the same values", by_id(asof[("ADM", "Velva, ND")]) == {"R1": -35, "R2": -43, "R3": 0} and by_id(asof[("CHS", "Hallock NG")]) == {"R1": -60, "R2": -45, "R3": -25})

mp = {(m["provider"], m["location"]): m for m in db.get_map_data()}
bids = {(b["grain"], b["delivery_month"]): b["basis"] for b in mp[("ADM", "Velva, ND")]["bids"]}
check("get_map_data: the map's canola bids are the quoted numbers too (-35, -43; the 0 and the corn row as they were)",
      bids.get(("Canola", "Nov")) == -35 and bids.get(("Canola", "FH Oct")) == -43, bids)
chs_map = {(b["grain"], b["delivery_month"]): b["basis"] for b in mp[("CHS", "Hallock NG")]["bids"]}
check("...and CHS Hallock's corn bid on the map is untouched", chs_map.get(("Corn", "Oct 2026")) == -25 and chs_map.get(("Canola", "Oct 2026")) == -60, chs_map)

conn = db.get_conn()
cur = conn.cursor()
cur.execute("SELECT r.basis_cents FROM snapshot_rows r JOIN snapshots s ON s.id = r.snapshot_id WHERE s.provider = 'ADM' AND s.location = 'Velva, ND' AND r.row_id = 'R1' ORDER BY s.timestamp")
stored = [r["basis_cents"] for r in cur.fetchall()]
conn.close()
check("the database itself still holds the x100 values (nothing was migrated)", stored == [-4000, -3500], stored)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
