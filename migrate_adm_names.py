"""
migrate_adm_names.py — One-time migration: reconcile email-sourced ADM location names
with the ADM Gradable web-scraper location names so all data forms one time series.

MAPPING
  Email name              -> Web scraper name(s)
  "ADM Cedar Rapids"      -> "Cedar Rapids, IA"
  "ADM St. Louis"         -> "St. Louis, MO (Elevator)"
  "ADM Decatur" (corn)    -> "Decatur, IL (Corn Processing)"
  "ADM Decatur" (soy)     -> "Decatur, IL (Soy Processing)"

Cedar Rapids and St. Louis are simple renames.
Decatur requires a *split*: each "ADM Decatur" snapshot had both corn and soy rows;
they must fan out into two separate new location buckets.

Usage:
    python migrate_adm_names.py             # dry run — shows what would happen
    python migrate_adm_names.py --apply     # apply locally (SQLite)
    python migrate_adm_names.py --apply --pg  # apply to Supabase (requires DATABASE_URL)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import database as db


# ── Rename mapping for simple one-to-one locations ────────────────────────────

_SIMPLE_RENAMES: list[tuple[str, str, str]] = [
    # (provider, old_location, new_location)
    ("ADM", "ADM Cedar Rapids", "Cedar Rapids, IA"),
    ("ADM", "ADM St. Louis",    "St. Louis, MO (Elevator)"),
]

# ── Decatur split mapping (by grain keyword) ──────────────────────────────────

_DECATUR_CORN_LOC = "Decatur, IL (Corn Processing)"
_DECATUR_SOY_LOC  = "Decatur, IL (Soy Processing)"
_DECATUR_GRAINS   = {
    "Corn":     _DECATUR_CORN_LOC,
    "Soybeans": _DECATUR_SOY_LOC,
}


# ─────────────────────────────────────────────────────────────────────────────

def _count(conn, provider: str, location: str) -> int:
    c = conn.cursor()
    ph = "%s" if db._use_pg() else "?"
    c.execute(f"SELECT COUNT(*) AS n FROM snapshots WHERE provider={ph} AND location={ph}",
              (provider, location))
    return c.fetchone()["n"]


def _do_simple_rename(conn, provider: str, old_loc: str, new_loc: str,
                      apply: bool, verbose: bool) -> int:
    """Rename all snapshots from old_loc to new_loc. Returns number renamed."""
    ph = "%s" if db._use_pg() else "?"
    c  = conn.cursor()

    c.execute(f"SELECT COUNT(*) AS n FROM snapshots WHERE provider={ph} AND location={ph}",
              (provider, old_loc))
    n = c.fetchone()["n"]

    if n == 0:
        if verbose:
            print(f"  [skip] {provider}/{old_loc!r} — no rows found")
        return 0

    if verbose:
        print(f"  {'[DRY]' if not apply else '[APPLY]'} Rename {n} snapshot(s): "
              f"{provider}/{old_loc!r} -> {new_loc!r}")

    if not apply:
        return n

    # Update snapshots table
    c.execute(f"UPDATE snapshots SET location={ph} WHERE provider={ph} AND location={ph}",
              (new_loc, provider, old_loc))
    renamed = c.rowcount

    # Update location_meta if present
    c.execute(f"SELECT 1 FROM location_meta WHERE provider={ph} AND location={ph}",
              (provider, old_loc))
    if c.fetchone():
        # If the new name already has a meta row, merge into it; else just rename
        c.execute(f"SELECT 1 FROM location_meta WHERE provider={ph} AND location={ph}",
                  (provider, new_loc))
        if c.fetchone():
            # new name already exists — drop the old row
            c.execute(f"DELETE FROM location_meta WHERE provider={ph} AND location={ph}",
                      (provider, old_loc))
        else:
            c.execute(f"UPDATE location_meta SET location={ph} WHERE provider={ph} AND location={ph}",
                      (new_loc, provider, old_loc))

    conn.commit()
    return renamed


def _do_decatur_split(conn, apply: bool, verbose: bool) -> int:
    """
    Split "ADM Decatur" snapshots into corn / soy buckets.
    Each old snapshot fans out into up to two new snapshots (one per grain).
    Returns number of old snapshots processed.
    """
    ph = "%s" if db._use_pg() else "?"
    c  = conn.cursor()

    c.execute(f"""
        SELECT s.id, s.timestamp, s.source, s.email_subject, s.email_date
        FROM snapshots s
        WHERE s.provider={ph} AND s.location={ph}
        ORDER BY s.timestamp
    """, ("ADM", "ADM Decatur"))
    old_snaps = c.fetchall()

    if not old_snaps:
        if verbose:
            print("  [skip] ADM/ADM Decatur — no rows found")
        return 0

    if verbose:
        print(f"  {'[DRY]' if not apply else '[APPLY]'} Split {len(old_snaps)} ADM Decatur "
              f"snapshot(s) into Corn Processing + Soy Processing")

    if not apply:
        # Show preview
        for snap in old_snaps[:5]:
            c.execute(f"""
                SELECT grain, COUNT(*) AS n FROM snapshot_rows
                WHERE snapshot_id={ph} GROUP BY grain ORDER BY grain
            """, (snap["id"],))
            grain_counts = {r["grain"]: r["n"] for r in c.fetchall()}
            if verbose:
                print(f"    {snap['timestamp'][:10]}  grains={grain_counts}")
        if len(old_snaps) > 5:
            print(f"    ... ({len(old_snaps) - 5} more)")
        return len(old_snaps)

    processed = 0
    for snap in old_snaps:
        # Fetch all rows for this snapshot
        c.execute(f"""
            SELECT row_id, grain, delivery_month, futures_symbol,
                   basis_cents, is_spot, spot_grain
            FROM snapshot_rows WHERE snapshot_id={ph}
        """, (snap["id"],))
        all_rows = c.fetchall()

        # Group by grain
        by_grain: dict[str, list] = {}
        for row in all_rows:
            g = row["grain"]
            by_grain.setdefault(g, []).append(row)

        for grain, grain_rows in by_grain.items():
            new_loc = _DECATUR_GRAINS.get(grain)
            if not new_loc:
                if verbose:
                    print(f"    WARNING: unknown grain {grain!r} in ADM Decatur — skipping")
                continue

            # Insert snapshot into new location (idempotent via UNIQUE constraint)
            if db._use_pg():
                c.execute("""
                    INSERT INTO snapshots (timestamp, provider, location, source, email_subject, email_date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (timestamp, provider, location) DO NOTHING
                    RETURNING id
                """, (snap["timestamp"], "ADM", new_loc,
                      snap["source"] or "email", snap["email_subject"], snap["email_date"]))
                row = c.fetchone()
                if row:
                    new_snap_id = row["id"]
                else:
                    c.execute(
                        "SELECT id FROM snapshots WHERE timestamp=%s AND provider=%s AND location=%s",
                        (snap["timestamp"], "ADM", new_loc))
                    new_snap_id = c.fetchone()["id"]
            else:
                c.execute("""
                    INSERT OR IGNORE INTO snapshots
                    (timestamp, provider, location, source, email_subject, email_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (snap["timestamp"], "ADM", new_loc,
                      snap["source"] or "email", snap["email_subject"], snap["email_date"]))
                if c.lastrowid == 0:
                    c.execute(
                        "SELECT id FROM snapshots WHERE timestamp=? AND provider=? AND location=?",
                        (snap["timestamp"], "ADM", new_loc))
                    new_snap_id = c.fetchone()["id"]
                else:
                    new_snap_id = c.lastrowid

            # Copy rows
            for row in grain_rows:
                if db._use_pg():
                    c.execute("""
                        INSERT INTO snapshot_rows
                        (snapshot_id, row_id, grain, delivery_month, futures_symbol,
                         basis_cents, is_spot, spot_grain)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (snapshot_id, row_id) DO NOTHING
                    """, (new_snap_id, row["row_id"], row["grain"],
                          row["delivery_month"], row["futures_symbol"],
                          row["basis_cents"], row["is_spot"], row["spot_grain"]))
                else:
                    c.execute("""
                        INSERT OR IGNORE INTO snapshot_rows
                        (snapshot_id, row_id, grain, delivery_month, futures_symbol,
                         basis_cents, is_spot, spot_grain)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (new_snap_id, row["row_id"], row["grain"],
                          row["delivery_month"], row["futures_symbol"],
                          row["basis_cents"], row["is_spot"], row["spot_grain"]))

        # Delete the original "ADM Decatur" snapshot (rows cascade)
        c.execute(f"DELETE FROM snapshot_rows WHERE snapshot_id={ph}", (snap["id"],))
        c.execute(f"DELETE FROM snapshots WHERE id={ph}", (snap["id"],))
        processed += 1

    # Migrate location_meta for Decatur
    c.execute(f"SELECT * FROM location_meta WHERE provider={ph} AND location={ph}",
              ("ADM", "ADM Decatur"))
    old_meta = c.fetchone()
    if old_meta:
        for new_loc in (_DECATUR_CORN_LOC, _DECATUR_SOY_LOC):
            if db._use_pg():
                c.execute("""
                    INSERT INTO location_meta
                    (provider, location, state, facility_type, region, lat, lon, delivery_zone)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (provider, location) DO NOTHING
                """, ("ADM", new_loc, old_meta["state"], old_meta["facility_type"],
                      old_meta["region"], old_meta["lat"], old_meta["lon"],
                      old_meta["delivery_zone"] if "delivery_zone" in old_meta.keys() else None))
            else:
                dz = old_meta["delivery_zone"] if "delivery_zone" in old_meta.keys() else None
                c.execute("""
                    INSERT OR IGNORE INTO location_meta
                    (provider, location, state, facility_type, region, lat, lon, delivery_zone)
                    VALUES (?,?,?,?,?,?,?,?)
                """, ("ADM", new_loc, old_meta["state"], old_meta["facility_type"],
                      old_meta["region"], old_meta["lat"], old_meta["lon"], dz))
        c.execute(f"DELETE FROM location_meta WHERE provider={ph} AND location={ph}",
                  ("ADM", "ADM Decatur"))

    conn.commit()
    return processed


def run(apply: bool = False, verbose: bool = True):
    db.init_db()
    conn = db.get_conn()
    backend = "PostgreSQL" if db._use_pg() else "SQLite"
    print(f"\nADM location-name migration  [{backend}]  apply={apply}")
    print("=" * 60)

    total = 0
    for provider, old_loc, new_loc in _SIMPLE_RENAMES:
        n = _do_simple_rename(conn, provider, old_loc, new_loc, apply=apply, verbose=verbose)
        total += n

    n = _do_decatur_split(conn, apply=apply, verbose=verbose)
    total += n

    print()
    if apply:
        print(f"Done. {total} snapshot(s) migrated.")
    else:
        print(f"Dry run complete. {total} snapshot(s) would be migrated.")
        print("Re-run with --apply to commit the changes.")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate ADM email location names")
    parser.add_argument("--apply", action="store_true",
                        help="Apply changes (default is dry run)")
    parser.add_argument("--pg", action="store_true",
                        help="Use DATABASE_URL (Supabase). Must be set in environment.")
    args = parser.parse_args()

    if args.pg:
        url = os.getenv("DATABASE_URL", "")
        if not url:
            print("ERROR: --pg requires DATABASE_URL to be set in the environment.")
            sys.exit(1)
        print(f"Target: PostgreSQL ({url[:40]}...)")
    else:
        print(f"Target: SQLite ({db.DB_PATH})")

    run(apply=args.apply)
