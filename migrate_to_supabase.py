"""
migrate_to_supabase.py — One-time migration: SQLite basis_tracker.db -> Supabase PostgreSQL.

Usage:
    1. Add DATABASE_URL to your .env  (the Supabase transaction-mode pooler URL).
    2. Run:  python migrate_to_supabase.py
    3. The script creates tables in Supabase and copies all existing data.

Safe to re-run — uses ON CONFLICT DO NOTHING so duplicate rows are skipped.
"""
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

DB_PATH = Path(__file__).parent / "basis_tracker.db"
PG_URL  = os.getenv("DATABASE_URL", "")

if not PG_URL:
    print("ERROR: DATABASE_URL not set. Add it to your .env file and retry.")
    sys.exit(1)

if not DB_PATH.exists():
    print(f"ERROR: SQLite database not found at {DB_PATH}")
    sys.exit(1)

import psycopg2
import psycopg2.extras

print(f"Source : {DB_PATH}")
print(f"Target : {PG_URL[:40]}...")
print()

# ── Open connections ──────────────────────────────────────────────────────────
sqlite_conn = sqlite3.connect(DB_PATH)
sqlite_conn.row_factory = sqlite3.Row
sc = sqlite_conn.cursor()

pg_conn = psycopg2.connect(PG_URL, cursor_factory=psycopg2.extras.RealDictCursor)
pc = pg_conn.cursor()

# ── Create tables in PostgreSQL ───────────────────────────────────────────────
print("Creating tables in Supabase...")

pg_ddl = [
    """CREATE TABLE IF NOT EXISTS snapshots (
        id            BIGSERIAL PRIMARY KEY,
        timestamp     TEXT NOT NULL,
        provider      TEXT NOT NULL,
        location      TEXT NOT NULL,
        source        TEXT NOT NULL DEFAULT 'manual',
        email_subject TEXT,
        email_date    TEXT,
        created_at    TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_unique
       ON snapshots(timestamp, provider, location)""",
    """CREATE TABLE IF NOT EXISTS snapshot_rows (
        id             BIGSERIAL PRIMARY KEY,
        snapshot_id    BIGINT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        row_id         TEXT NOT NULL,
        grain          TEXT NOT NULL,
        delivery_month TEXT NOT NULL,
        futures_symbol TEXT NOT NULL,
        basis_cents    INTEGER,
        is_spot        SMALLINT NOT NULL DEFAULT 0,
        spot_grain     TEXT,
        UNIQUE(snapshot_id, row_id)
    )""",
    """CREATE TABLE IF NOT EXISTS imported_emails (
        email_id    TEXT PRIMARY KEY,
        subject     TEXT,
        imported_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS location_meta (
        provider      TEXT NOT NULL,
        location      TEXT NOT NULL,
        state         TEXT,
        facility_type TEXT,
        PRIMARY KEY (provider, location)
    )""",
]
for stmt in pg_ddl:
    pc.execute(stmt)
pg_conn.commit()
print("  Tables ready.")
print()

# ── Migrate: snapshots + snapshot_rows ────────────────────────────────────────
sc.execute("SELECT * FROM snapshots ORDER BY id")
sqlite_snaps = sc.fetchall()
print(f"Migrating {len(sqlite_snaps)} snapshot(s)...")

snap_id_map: dict[int, int] = {}   # sqlite_id -> postgres_id
snap_ok = 0
snap_skip = 0

for s in sqlite_snaps:
    pc.execute(
        """INSERT INTO snapshots
           (timestamp, provider, location, source, email_subject, email_date)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (timestamp, provider, location) DO NOTHING
           RETURNING id""",
        (s["timestamp"], s["provider"], s["location"], s["source"],
         s["email_subject"], s["email_date"]),
    )
    row = pc.fetchone()
    if row:
        snap_id_map[s["id"]] = row["id"]
        snap_ok += 1
    else:
        # Already existed — fetch its id
        pc.execute(
            "SELECT id FROM snapshots WHERE timestamp=%s AND provider=%s AND location=%s",
            (s["timestamp"], s["provider"], s["location"]),
        )
        snap_id_map[s["id"]] = pc.fetchone()["id"]
        snap_skip += 1

pg_conn.commit()
print(f"  Inserted: {snap_ok}   Already existed: {snap_skip}")
print()

# ── Migrate: snapshot_rows ────────────────────────────────────────────────────
sc.execute("SELECT * FROM snapshot_rows ORDER BY snapshot_id, id")
sqlite_rows = sc.fetchall()
print(f"Migrating {len(sqlite_rows)} snapshot row(s)...")

rows_ok   = 0
rows_skip = 0

for r in sqlite_rows:
    old_snap_id = r["snapshot_id"]
    new_snap_id = snap_id_map.get(old_snap_id)
    if new_snap_id is None:
        print(f"  WARNING: no mapping for snapshot_id={old_snap_id}, skipping row {r['row_id']}")
        continue

    pc.execute(
        """INSERT INTO snapshot_rows
           (snapshot_id, row_id, grain, delivery_month, futures_symbol,
            basis_cents, is_spot, spot_grain)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (snapshot_id, row_id) DO NOTHING""",
        (new_snap_id, r["row_id"], r["grain"], r["delivery_month"],
         r["futures_symbol"], r["basis_cents"], r["is_spot"], r["spot_grain"]),
    )
    if pc.rowcount > 0:
        rows_ok += 1
    else:
        rows_skip += 1

pg_conn.commit()
print(f"  Inserted: {rows_ok}   Already existed: {rows_skip}")
print()

# ── Migrate: imported_emails ──────────────────────────────────────────────────
sc.execute("SELECT * FROM imported_emails")
emails = sc.fetchall()
print(f"Migrating {len(emails)} imported email record(s)...")
em_ok = em_skip = 0
for e in emails:
    pc.execute(
        "INSERT INTO imported_emails (email_id, subject) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING",
        (e["email_id"], e["subject"]),
    )
    if pc.rowcount > 0:
        em_ok += 1
    else:
        em_skip += 1
pg_conn.commit()
print(f"  Inserted: {em_ok}   Already existed: {em_skip}")
print()

# ── Migrate: location_meta ────────────────────────────────────────────────────
sc.execute("SELECT * FROM location_meta")
metas = sc.fetchall()
print(f"Migrating {len(metas)} location_meta record(s)...")
meta_ok = meta_skip = 0
for m in metas:
    pc.execute(
        """INSERT INTO location_meta (provider, location, state, facility_type)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (provider, location) DO NOTHING""",
        (m["provider"], m["location"], m["state"], m["facility_type"]),
    )
    if pc.rowcount > 0:
        meta_ok += 1
    else:
        meta_skip += 1
pg_conn.commit()
print(f"  Inserted: {meta_ok}   Already existed: {meta_skip}")
print()

# ── Verify ────────────────────────────────────────────────────────────────────
pc.execute("SELECT COUNT(*) AS n FROM snapshots")
pg_snaps = pc.fetchone()["n"]
pc.execute("SELECT COUNT(*) AS n FROM snapshot_rows")
pg_rows = pc.fetchone()["n"]
pc.execute("SELECT COUNT(*) AS n FROM location_meta")
pg_meta = pc.fetchone()["n"]

print("=" * 50)
print(f"Supabase now has:")
print(f"  {pg_snaps} snapshot(s)")
print(f"  {pg_rows} snapshot row(s)")
print(f"  {pg_meta} location_meta record(s)")
print()
print("Migration complete!")
print()
print("Next steps:")
print("  1. Push this repo to GitHub.")
print("  2. Connect to Streamlit Community Cloud.")
print("  3. Set DATABASE_URL (and optionally AZURE_CLIENT_ID/TENANT_ID)")
print("     in Streamlit Cloud -> App settings -> Secrets.")
print("  4. For local auto_import.py, DATABASE_URL in .env already points to Supabase.")

sqlite_conn.close()
pg_conn.close()
