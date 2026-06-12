"""
database.py — Basis Tracker persistence layer.

Supports two backends automatically:

  PostgreSQL (production / Streamlit Cloud):
      Set DATABASE_URL env var to a Supabase connection string.
      Uses psycopg2-binary.

  SQLite (local development):
      DATABASE_URL not set — uses ./basis_tracker.db.
      No extra dependencies required.

All public functions are backend-agnostic; callers don't need to know
which database is active.
"""
import os
from pathlib import Path
from models import Snapshot, SnapshotRow

DB_PATH = Path(__file__).parent / "basis_tracker.db"


# ── Backend helpers ───────────────────────────────────────────────────────────

def _pg_url() -> str:
    """Return the PostgreSQL connection URL, or '' if using SQLite."""
    return os.getenv("DATABASE_URL", "")


def _use_pg() -> bool:
    return bool(_pg_url())


def get_conn():
    """Open and return a database connection for the active backend."""
    url = _pg_url()
    if url:
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Schema creation ────────────────────────────────────────────────────────────

_SQLITE_DDL = [
    """CREATE TABLE IF NOT EXISTS snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp     TEXT NOT NULL,
        provider      TEXT NOT NULL,
        location      TEXT NOT NULL,
        source        TEXT NOT NULL DEFAULT 'manual',
        email_subject TEXT,
        email_date    TEXT,
        created_at    TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_unique
       ON snapshots(timestamp, provider, location)""",
    """CREATE TABLE IF NOT EXISTS snapshot_rows (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        row_id         TEXT NOT NULL,
        grain          TEXT NOT NULL,
        delivery_month TEXT NOT NULL,
        futures_symbol TEXT NOT NULL,
        basis_cents    INTEGER,
        is_spot        INTEGER NOT NULL DEFAULT 0,
        spot_grain     TEXT,
        UNIQUE(snapshot_id, row_id)
    )""",
    """CREATE TABLE IF NOT EXISTS imported_emails (
        email_id    TEXT PRIMARY KEY,
        subject     TEXT,
        imported_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS location_meta (
        provider      TEXT NOT NULL,
        location      TEXT NOT NULL,
        state         TEXT,
        facility_type TEXT,
        PRIMARY KEY (provider, location)
    )""",
]

_PG_DDL = [
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


def init_db():
    """Create all tables and indexes if they don't exist yet."""
    conn = get_conn()
    c    = conn.cursor()
    ddl  = _PG_DDL if _use_pg() else _SQLITE_DDL
    try:
        for stmt in ddl:
            c.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ── Email dedup ────────────────────────────────────────────────────────────────

def is_email_imported(email_id: str) -> bool:
    """Return True if this email_id has already been imported."""
    if not email_id:
        return False
    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    try:
        c.execute(f"SELECT 1 FROM imported_emails WHERE email_id={ph} LIMIT 1", (email_id,))
        return c.fetchone() is not None
    finally:
        conn.close()


def mark_email_imported(email_id: str, subject: str = ""):
    """Record that this email has been imported (idempotent)."""
    if not email_id:
        return
    conn = get_conn()
    c    = conn.cursor()
    try:
        if _use_pg():
            c.execute(
                "INSERT INTO imported_emails (email_id, subject) VALUES (%s, %s)"
                " ON CONFLICT DO NOTHING",
                (email_id, subject),
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO imported_emails (email_id, subject) VALUES (?, ?)",
                (email_id, subject),
            )
        conn.commit()
    finally:
        conn.close()


# ── Snapshot upsert ────────────────────────────────────────────────────────────

def upsert_snapshot(snap: dict) -> int:
    """
    Insert snapshot + rows, ignoring if (timestamp, provider, location) already exists.
    Returns the snapshot's database id.
    """
    conn = get_conn()
    c    = conn.cursor()
    try:
        if _use_pg():
            # ── PostgreSQL path ──────────────────────────────────────────────
            c.execute(
                """INSERT INTO snapshots
                   (timestamp, provider, location, source, email_subject, email_date)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (timestamp, provider, location) DO NOTHING
                   RETURNING id""",
                (snap["timestamp"], snap["provider"], snap["location"],
                 snap.get("source", "manual"), snap.get("emailSubject"),
                 snap.get("emailDate")),
            )
            row = c.fetchone()
            if row:
                snap_id = row["id"]
            else:
                c.execute(
                    "SELECT id FROM snapshots WHERE timestamp=%s AND provider=%s AND location=%s",
                    (snap["timestamp"], snap["provider"], snap["location"]),
                )
                snap_id = c.fetchone()["id"]

            for r in snap.get("rows", []):
                c.execute(
                    """INSERT INTO snapshot_rows
                       (snapshot_id, row_id, grain, delivery_month, futures_symbol,
                        basis_cents, is_spot, spot_grain)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (snapshot_id, row_id) DO NOTHING""",
                    (snap_id, r["id"], r["grain"], r["deliveryMonth"],
                     r["futuresSymbol"], r.get("basisCents"),
                     1 if r.get("isSpot") else 0, r.get("spotGrain")),
                )

        else:
            # ── SQLite path ──────────────────────────────────────────────────
            c.execute(
                """INSERT OR IGNORE INTO snapshots
                   (timestamp, provider, location, source, email_subject, email_date)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (snap["timestamp"], snap["provider"], snap["location"],
                 snap.get("source", "manual"), snap.get("emailSubject"),
                 snap.get("emailDate")),
            )
            if c.lastrowid == 0:
                c.execute(
                    "SELECT id FROM snapshots WHERE timestamp=? AND provider=? AND location=?",
                    (snap["timestamp"], snap["provider"], snap["location"]),
                )
                snap_id = c.fetchone()["id"]
            else:
                snap_id = c.lastrowid

            for r in snap.get("rows", []):
                c.execute(
                    """INSERT OR IGNORE INTO snapshot_rows
                       (snapshot_id, row_id, grain, delivery_month, futures_symbol,
                        basis_cents, is_spot, spot_grain)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (snap_id, r["id"], r["grain"], r["deliveryMonth"],
                     r["futuresSymbol"], r.get("basisCents"),
                     1 if r.get("isSpot") else 0, r.get("spotGrain")),
                )

        conn.commit()
        return snap_id
    finally:
        conn.close()


# ── Reads ──────────────────────────────────────────────────────────────────────

def get_snapshots(provider: str, location: str) -> list[Snapshot]:
    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    try:
        c.execute(
            f"SELECT * FROM snapshots WHERE provider={ph} AND location={ph} ORDER BY timestamp",
            (provider, location),
        )
        snap_rows = c.fetchall()
        result    = []
        for sr in snap_rows:
            c.execute(
                f"SELECT * FROM snapshot_rows WHERE snapshot_id={ph} ORDER BY id",
                (sr["id"],),
            )
            rows = [
                SnapshotRow(
                    id            = r["row_id"],
                    grain         = r["grain"],
                    deliveryMonth = r["delivery_month"],
                    futuresSymbol = r["futures_symbol"],
                    basisCents    = r["basis_cents"],
                    isSpot        = bool(r["is_spot"]),
                    spotGrain     = r["spot_grain"],
                )
                for r in c.fetchall()
            ]
            result.append(
                Snapshot(
                    id           = sr["id"],
                    timestamp    = sr["timestamp"],
                    provider     = sr["provider"],
                    location     = sr["location"],
                    source       = sr["source"],
                    emailSubject = sr["email_subject"],
                    emailDate    = sr["email_date"],
                    rows         = rows,
                )
            )
        return result
    finally:
        conn.close()


def delete_snapshot(snapshot_id: int) -> bool:
    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    try:
        c.execute(f"DELETE FROM snapshot_rows WHERE snapshot_id={ph}", (snapshot_id,))
        c.execute(f"DELETE FROM snapshots WHERE id={ph}", (snapshot_id,))
        deleted = c.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


# ── Location metadata ──────────────────────────────────────────────────────────

def upsert_location_meta(provider: str, location: str,
                         state: str | None = None,
                         facility_type: str | None = None):
    """Insert or update state/facility_type metadata for a location (idempotent)."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        if _use_pg():
            c.execute("""
                INSERT INTO location_meta (provider, location, state, facility_type)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (provider, location) DO UPDATE SET
                    state         = COALESCE(EXCLUDED.state,         location_meta.state),
                    facility_type = COALESCE(EXCLUDED.facility_type, location_meta.facility_type)
            """, (provider, location, state, facility_type))
        else:
            c.execute("""
                INSERT INTO location_meta (provider, location, state, facility_type)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, location) DO UPDATE SET
                    state         = COALESCE(excluded.state,         location_meta.state),
                    facility_type = COALESCE(excluded.facility_type, location_meta.facility_type)
            """, (provider, location, state, facility_type))
        conn.commit()
    finally:
        conn.close()


def get_location_meta(provider: str) -> dict[str, dict]:
    """Return {location_name: {state, facility_type}} for a provider."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    try:
        c.execute(
            f"SELECT location, state, facility_type FROM location_meta WHERE provider={ph}",
            (provider,),
        )
        return {
            row["location"]: {
                "state":         row["state"]         or "",
                "facility_type": row["facility_type"] or "",
            }
            for row in c.fetchall()
        }
    finally:
        conn.close()


def list_locations() -> list[dict]:
    """Return distinct (provider, location) pairs that have snapshot data."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        c.execute(
            "SELECT DISTINCT provider, location FROM snapshots ORDER BY provider, location"
        )
        return [{"provider": r["provider"], "location": r["location"]} for r in c.fetchall()]
    finally:
        conn.close()


# ── Data retention / pruning ───────────────────────────────────────────────────

def prune_old_snapshots(dry_run: bool = False) -> dict:
    """
    Apply tiered data retention to snapshots (PostgreSQL only).

    Policy:
      • Current calendar month  → keep ALL  (daily granularity)
      • 1 month – 1 year old    → keep ONE per (provider, location, ISO week)
      • Older than 1 year       → keep ONE per (provider, location, calendar month)

    The ON DELETE CASCADE on snapshot_rows handles row cleanup automatically.

    Args:
        dry_run: If True, count candidates but do not delete anything.

    Returns:
        dict with keys: candidates, deleted, snaps_after, rows_after
    """
    if not _use_pg():
        # SQLite is only used for local dev — data volume is small, skip pruning.
        return {"candidates": 0, "deleted": 0, "snaps_after": 0, "rows_after": 0}

    # Sub-selects for each retention tier.  Each DISTINCT ON must be wrapped
    # in a subquery before being combined with UNION.
    _KEEPERS_SQL = """
        -- Tier 1: current calendar month — keep everything
        SELECT id FROM snapshots
        WHERE created_at >= DATE_TRUNC('month', NOW())

        UNION

        -- Tier 2: 1 month to 1 year — keep one (most recent) per provider/location/week
        SELECT id FROM (
            SELECT DISTINCT ON (provider, location, DATE_TRUNC('week', created_at))
                id
            FROM snapshots
            WHERE created_at >= NOW() - INTERVAL '1 year'
              AND created_at <  DATE_TRUNC('month', NOW())
            ORDER BY provider, location, DATE_TRUNC('week', created_at), created_at DESC
        ) weekly

        UNION

        -- Tier 3: older than 1 year — keep one (most recent) per provider/location/month
        SELECT id FROM (
            SELECT DISTINCT ON (provider, location, DATE_TRUNC('month', created_at))
                id
            FROM snapshots
            WHERE created_at < NOW() - INTERVAL '1 year'
            ORDER BY provider, location, DATE_TRUNC('month', created_at), created_at DESC
        ) monthly
    """

    conn = get_conn()
    c    = conn.cursor()
    try:
        # Count how many snapshots fall outside the retention windows
        c.execute(f"SELECT COUNT(*) AS n FROM snapshots WHERE id NOT IN ({_KEEPERS_SQL})")
        candidates = c.fetchone()["n"]

        if dry_run or candidates == 0:
            c.execute("SELECT COUNT(*) AS n FROM snapshots")
            snaps_after = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) AS n FROM snapshot_rows")
            rows_after = c.fetchone()["n"]
            return {
                "candidates": candidates,
                "deleted":    0,
                "snaps_after": snaps_after,
                "rows_after":  rows_after,
            }

        c.execute(f"DELETE FROM snapshots WHERE id NOT IN ({_KEEPERS_SQL})")
        deleted = c.rowcount
        conn.commit()

        c.execute("SELECT COUNT(*) AS n FROM snapshots")
        snaps_after = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM snapshot_rows")
        rows_after = c.fetchone()["n"]

        return {
            "candidates": candidates,
            "deleted":    deleted,
            "snaps_after": snaps_after,
            "rows_after":  rows_after,
        }
    finally:
        conn.close()
