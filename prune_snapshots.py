"""
prune_snapshots.py — Tiered data retention for basis_tracker.

Retention policy:
  • Current calendar month  → keep ALL snapshots (daily granularity)
  • 1 month – 1 year old    → keep ONE per (provider, location, ISO week)
  • Older than 1 year       → keep ONE per (provider, location, calendar month)

The ON DELETE CASCADE on snapshot_rows means pruning snapshots automatically
removes their rows — no separate cleanup needed.

Usage:
    python prune_snapshots.py               # live run
    python prune_snapshots.py --dry-run     # preview what would be deleted
    python prune_snapshots.py --stats-only  # just show row counts, no changes
"""
import os
import sys
import argparse
from dotenv import load_dotenv

load_dotenv()

PG_URL = os.getenv("DATABASE_URL", "")
if not PG_URL:
    print("ERROR: DATABASE_URL not set. Add it to .env and retry.")
    sys.exit(1)

import psycopg2
import psycopg2.extras

# ── Parse args ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Prune old basis tracker snapshots.")
parser.add_argument("--dry-run",    action="store_true", help="Show what would be deleted without deleting.")
parser.add_argument("--stats-only", action="store_true", help="Show row counts only, no changes.")
args = parser.parse_args()

conn = psycopg2.connect(PG_URL, cursor_factory=psycopg2.extras.RealDictCursor)
c = conn.cursor()

# ── Current counts ────────────────────────────────────────────────────────────
c.execute("SELECT COUNT(*) AS n FROM snapshots")
before_snaps = c.fetchone()["n"]
c.execute("SELECT COUNT(*) AS n FROM snapshot_rows")
before_rows = c.fetchone()["n"]

print(f"Current: {before_snaps:,} snapshots  |  {before_rows:,} rows")

if args.stats_only:
    conn.close()
    sys.exit(0)

print()

# ── Build the "keepers" subquery ──────────────────────────────────────────────
# For each tier, pick the MOST RECENT snapshot in that window.

KEEPERS_SQL = """
    -- Tier 1: current month — keep all
    SELECT id FROM snapshots
    WHERE created_at >= DATE_TRUNC('month', NOW())

    UNION

    -- Tier 2: 1 month to 1 year — keep one per (provider, location, ISO week)
    SELECT DISTINCT ON (provider, location, DATE_TRUNC('week', created_at))
        id
    FROM snapshots
    WHERE created_at >= NOW() - INTERVAL '1 year'
      AND created_at <  DATE_TRUNC('month', NOW())
    ORDER BY provider, location, DATE_TRUNC('week', created_at), created_at DESC

    UNION

    -- Tier 3: older than 1 year — keep one per (provider, location, calendar month)
    SELECT DISTINCT ON (provider, location, DATE_TRUNC('month', created_at))
        id
    FROM snapshots
    WHERE created_at < NOW() - INTERVAL '1 year'
    ORDER BY provider, location, DATE_TRUNC('month', created_at), created_at DESC
"""

# ── Count what would be deleted ───────────────────────────────────────────────
c.execute(f"""
    SELECT provider, COUNT(*) AS cnt
    FROM snapshots
    WHERE id NOT IN ({KEEPERS_SQL})
    GROUP BY provider
    ORDER BY cnt DESC
""")
breakdown = c.fetchall()

c.execute(f"SELECT COUNT(*) AS n FROM snapshots WHERE id NOT IN ({KEEPERS_SQL})")
to_delete_count = c.fetchone()["n"]

if to_delete_count == 0:
    print("Nothing to prune — all data is within retention policy.")
    conn.close()
    sys.exit(0)

print(f"Snapshots eligible for pruning: {to_delete_count:,}")
print()
print("Breakdown by provider:")
for row in breakdown:
    print(f"  {row['provider']:<30} {row['cnt']:>6} snapshots")
print()

if args.dry_run:
    print("DRY RUN — no changes made.")
    conn.close()
    sys.exit(0)

# ── Execute delete ────────────────────────────────────────────────────────────
print("Pruning...")
c.execute(f"""
    DELETE FROM snapshots
    WHERE id NOT IN ({KEEPERS_SQL})
""")
deleted = c.rowcount
conn.commit()

# ── After counts ─────────────────────────────────────────────────────────────
c.execute("SELECT COUNT(*) AS n FROM snapshots")
after_snaps = c.fetchone()["n"]
c.execute("SELECT COUNT(*) AS n FROM snapshot_rows")
after_rows = c.fetchone()["n"]

print(f"Deleted:  {deleted:,} snapshot(s) (+ their rows via cascade)")
print(f"After:    {after_snaps:,} snapshots  |  {after_rows:,} rows")
print(f"Freed:    {before_rows - after_rows:,} rows removed")
print()
print("Done.")

conn.close()
