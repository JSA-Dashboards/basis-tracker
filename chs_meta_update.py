"""
chs_meta_update.py — apply CHS location metadata corrections.

Run once:  python chs_meta_update.py
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "basis_tracker.db"


def run():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # ── 1. Drops ──────────────────────────────────────────────────────────────
    # Remove snapshots + metadata for locations user said to drop.
    drops = [
        ("CHS", "Akron"),
        ("CHS", "Cheyenne Wells"),
        ("CHS", "Idalia"),
        ("CHS", "Idalia West"),
        ("CHS", "Holyoke"),       # keep "Holyoke Shuttle" (Rail Terminal)
        ("CHS", "Fleming"),
        ("CHS", "French"),
    ]
    for provider, location in drops:
        cur.execute(
            "SELECT id FROM snapshots WHERE provider=? AND location=?",
            (provider, location),
        )
        snap_ids = [r[0] for r in cur.fetchall()]
        if snap_ids:
            cur.execute(
                f"DELETE FROM snapshot_rows WHERE snapshot_id IN ({','.join('?'*len(snap_ids))})",
                snap_ids,
            )
            cur.execute(
                f"DELETE FROM snapshots WHERE id IN ({','.join('?'*len(snap_ids))})",
                snap_ids,
            )
        cur.execute(
            "DELETE FROM location_meta WHERE provider=? AND location=?",
            (provider, location),
        )
        print(f"  Dropped  CHS / {location}  ({len(snap_ids)} snapshots)")

    # ── 2. State corrections ───────────────────────────────────────────────────
    state_fixes = [
        ("CHS", "Callaway",  "MN"),   # was NE
        ("CHS", "Darien",    "WI"),   # was IL
        ("CHS", "Amherst",   "CO"),   # was NE
        ("CHS", "Omega",     "OK"),   # was IN
        ("CHS", "Kershaw",   "MT"),   # was MN
    ]
    for provider, location, new_state in state_fixes:
        cur.execute(
            "UPDATE location_meta SET state=? WHERE provider=? AND location=?",
            (new_state, provider, location),
        )
        print(f"  State    CHS / {location} -> {new_state}  ({cur.rowcount} row)")

    # ── 3. Facility-type corrections (non-shuttle) ────────────────────────────
    type_fixes = [
        ("CHS", "Amherst",  "Rail Terminal"),   # CO rail terminal (not elevator)
        ("CHS", "Winona",   "River Terminal"),   # MN river terminal
    ]
    for provider, location, new_type in type_fixes:
        cur.execute(
            "UPDATE location_meta SET facility_type=? WHERE provider=? AND location=?",
            (new_type, provider, location),
        )
        print(f"  Type     CHS / {location} -> {new_type}  ({cur.rowcount} row)")

    # ── 4. Rail Shuttle upgrades ───────────────────────────────────────────────
    # All these should be "Rail Shuttle" (not "Country Elevator").
    # Use exact location names matching the DB; handle the Fergus Falls one via LIKE.
    rail_shuttles = [
        # CO
        "Brush", "Byers", "Haxtun", "Julesburg", "Otis", "Yuma",
        # IA
        "Hinton",
        # IL
        "Lowder", "Steward",
        # KS
        "Sharon Springs", "Weskan",
        # MN
        "Ruthton", "Beltrami", "Erskine", "Glenwood", "Greenbush",
        "Hallock Drayton", "Hallock NG", "Herman", "Kennedy",
        "Magnolia", "Mahnomen", "Oklee", "Tracy", "Ulen", "Warren",
        # MT
        "Glasgow", "Glendive", "Havre", "Kalispell", "Kershaw", "Wolf Point",
        # ND
        "Bowbells", "Calvin", "Cavalier", "Davidson", "Devils Lake", "Drayton",
        "Edgeley", "Galchutt", "Garrison", "Gwinner", "Hannaford", "Harwood",
        "Lakota", "Langdon", "Lankin", "Manvel", "Minot", "Mohall",
        "Mooreton", "New Salem",
        # NE
        "Elm Creek", "Holdrege West", "Loomis",
        # OK
        "Okarche",
        # SD
        "Worthing Ag Grain",
    ]

    for location in rail_shuttles:
        cur.execute(
            "UPDATE location_meta SET facility_type='Rail Shuttle' "
            "WHERE provider='CHS' AND location=?",
            (location,),
        )
        if cur.rowcount:
            print(f"  Shuttle  CHS / {location}")
        else:
            print(f"  NOMATCH  CHS / {location}  ← check spelling")

    # Handle Fergus Falls South (has dump-fee note in name)
    cur.execute(
        "UPDATE location_meta SET facility_type='Rail Shuttle' "
        "WHERE provider='CHS' AND location LIKE 'Fergus Falls South%'",
    )
    print(f"  Shuttle  CHS / Fergus Falls South  ({cur.rowcount} row)")

    con.commit()
    con.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
