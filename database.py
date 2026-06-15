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
    """CREATE INDEX IF NOT EXISTS idx_snap_prov_loc_ts
       ON snapshots(provider, location, timestamp DESC)""",
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
    """CREATE INDEX IF NOT EXISTS idx_snap_rows_sid
       ON snapshot_rows(snapshot_id)""",
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
        region        TEXT,
        lat           REAL,
        lon           REAL,
        delivery_zone TEXT,
        PRIMARY KEY (provider, location)
    )""",
    """CREATE TABLE IF NOT EXISTS grain_map (
        raw_grain        TEXT PRIMARY KEY,
        canonical_grain  TEXT NOT NULL,
        wheat_class      TEXT,
        protein          TEXT,
        is_active        INTEGER NOT NULL DEFAULT 1
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
    """CREATE INDEX IF NOT EXISTS idx_snap_prov_loc_ts
       ON snapshots(provider, location, timestamp DESC)""",
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
    """CREATE INDEX IF NOT EXISTS idx_snap_rows_sid
       ON snapshot_rows(snapshot_id)""",
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
        region        TEXT,
        lat           DOUBLE PRECISION,
        lon           DOUBLE PRECISION,
        delivery_zone TEXT,
        PRIMARY KEY (provider, location)
    )""",
    """CREATE TABLE IF NOT EXISTS grain_map (
        raw_grain        TEXT PRIMARY KEY,
        canonical_grain  TEXT NOT NULL,
        wheat_class      TEXT,
        protein          TEXT,
        is_active        SMALLINT NOT NULL DEFAULT 1
    )""",
]

_MIGRATE_DDL = [
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS lat           REAL",
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS lon           REAL",
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS region        TEXT",
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS delivery_zone TEXT",
    "CREATE INDEX IF NOT EXISTS idx_snap_prov_loc_ts ON snapshots(provider, location, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_snap_rows_sid ON snapshot_rows(snapshot_id)",
]


def init_db():
    """Create all tables and indexes if they don't exist yet."""
    conn = get_conn()
    c    = conn.cursor()
    ddl  = _PG_DDL if _use_pg() else _SQLITE_DDL
    try:
        for stmt in ddl:
            c.execute(stmt)
        # Add lat/lon columns to existing databases that pre-date this schema change.
        for stmt in _MIGRATE_DDL:
            try:
                if _use_pg():
                    c.execute(stmt)
                else:
                    # SQLite doesn't support IF NOT EXISTS on ALTER TABLE
                    sqlite_stmt = stmt.replace(" IF NOT EXISTS", "")
                    c.execute(sqlite_stmt)
            except Exception:
                pass  # column already exists
        conn.commit()
    finally:
        conn.close()
    # Populate lat/lon, facility tags, and grain map from committed seed files.
    seed_geocoding()
    seed_facility_types()
    seed_grain_map()


def seed_geocoding(seed_path: str | None = None) -> int:
    """
    Load coords_seed.json and upsert lat/lon into location_meta for any rows
    that are still missing coordinates.  Safe to call repeatedly; only writes
    when a row is missing coords.  Returns number of rows written.
    """
    import json, os
    if seed_path is None:
        seed_path = os.path.join(os.path.dirname(__file__), "coords_seed.json")
    if not os.path.exists(seed_path):
        return 0

    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)

    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    written = 0
    try:
        for row in seed:
            if row.get("lat") is None or row.get("lon") is None:
                continue
            if _use_pg():
                c.execute(f"""
                    INSERT INTO location_meta (provider, location, state, lat, lon)
                    VALUES ({ph},{ph},{ph},{ph},{ph})
                    ON CONFLICT (provider, location) DO UPDATE SET
                        state = COALESCE(EXCLUDED.state, location_meta.state),
                        lat   = COALESCE(location_meta.lat,  EXCLUDED.lat),
                        lon   = COALESCE(location_meta.lon,  EXCLUDED.lon)
                """, (row["provider"], row["location"], row["state"], row["lat"], row["lon"]))
            else:
                c.execute(f"""
                    INSERT INTO location_meta (provider, location, state, lat, lon)
                    VALUES ({ph},{ph},{ph},{ph},{ph})
                    ON CONFLICT(provider, location) DO UPDATE SET
                        state = COALESCE(excluded.state, location_meta.state),
                        lat   = COALESCE(location_meta.lat,  excluded.lat),
                        lon   = COALESCE(location_meta.lon,  excluded.lon)
                """, (row["provider"], row["location"], row["state"], row["lat"], row["lon"]))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def seed_facility_types(seed_path: str | None = None) -> int:
    """
    Load facility_tags_seed.json and upsert facility_type/region into location_meta.
    Only overwrites when the existing value is NULL.  Safe to call repeatedly.
    Returns number of rows written.
    """
    import json, os
    if seed_path is None:
        seed_path = os.path.join(os.path.dirname(__file__), "facility_tags_seed.json")
    if not os.path.exists(seed_path):
        return 0

    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)

    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    written = 0
    try:
        for row in seed:
            prov  = row.get("provider")
            loc   = row.get("location")
            ft    = row.get("facility_type")
            reg   = row.get("region")
            dz    = row.get("delivery_zone")
            state = row.get("state")
            if not prov or not loc:
                continue
            if _use_pg():
                c.execute(f"""
                    INSERT INTO location_meta (provider, location, state, facility_type, region, delivery_zone)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
                    ON CONFLICT (provider, location) DO UPDATE SET
                        state         = COALESCE(EXCLUDED.state,         location_meta.state),
                        facility_type = COALESCE(EXCLUDED.facility_type, location_meta.facility_type),
                        region        = COALESCE(EXCLUDED.region,        location_meta.region),
                        delivery_zone = COALESCE(EXCLUDED.delivery_zone, location_meta.delivery_zone)
                """, (prov, loc, state, ft, reg, dz))
            else:
                c.execute(f"""
                    INSERT INTO location_meta (provider, location, state, facility_type, region, delivery_zone)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph})
                    ON CONFLICT(provider, location) DO UPDATE SET
                        state         = COALESCE(excluded.state,         location_meta.state),
                        facility_type = COALESCE(excluded.facility_type, location_meta.facility_type),
                        region        = COALESCE(excluded.region,        location_meta.region),
                        delivery_zone = COALESCE(excluded.delivery_zone, location_meta.delivery_zone)
                """, (prov, loc, state, ft, reg, dz))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def seed_grain_map(seed_path: str | None = None) -> int:
    """
    Load grain_seed.json and upsert all rows into grain_map.
    Mappings are authoritative — always overwrites existing values.
    Returns number of rows written.
    """
    import json, os
    if seed_path is None:
        seed_path = os.path.join(os.path.dirname(__file__), "grain_seed.json")
    if not os.path.exists(seed_path):
        return 0

    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)

    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    written = 0
    try:
        for row in seed:
            raw = row.get("raw_grain")
            if not raw:
                continue
            if _use_pg():
                c.execute(f"""
                    INSERT INTO grain_map (raw_grain, canonical_grain, wheat_class, protein, is_active)
                    VALUES ({ph},{ph},{ph},{ph},{ph})
                    ON CONFLICT (raw_grain) DO UPDATE SET
                        canonical_grain = EXCLUDED.canonical_grain,
                        wheat_class     = EXCLUDED.wheat_class,
                        protein         = EXCLUDED.protein,
                        is_active       = EXCLUDED.is_active
                """, (raw, row["canonical_grain"], row.get("wheat_class"),
                      row.get("protein"), row.get("is_active", 1)))
            else:
                c.execute(f"""
                    INSERT INTO grain_map (raw_grain, canonical_grain, wheat_class, protein, is_active)
                    VALUES ({ph},{ph},{ph},{ph},{ph})
                    ON CONFLICT(raw_grain) DO UPDATE SET
                        canonical_grain = excluded.canonical_grain,
                        wheat_class     = excluded.wheat_class,
                        protein         = excluded.protein,
                        is_active       = excluded.is_active
                """, (raw, row["canonical_grain"], row.get("wheat_class"),
                      row.get("protein"), row.get("is_active", 1)))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def get_grain_map() -> dict[str, dict]:
    """Return {raw_grain: {canonical_grain, wheat_class, protein, is_active}} from grain_map table."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        c.execute(
            "SELECT raw_grain, canonical_grain, wheat_class, protein, is_active FROM grain_map"
        )
        return {
            r["raw_grain"]: {
                "canonical_grain": r["canonical_grain"],
                "wheat_class":     r["wheat_class"],
                "protein":         r["protein"],
                "is_active":       bool(r["is_active"]),
            }
            for r in c.fetchall()
        }
    except Exception:
        return {}
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
        c.execute(f"""
            SELECT s.id AS snap_id, s.timestamp, s.provider, s.location,
                   s.source, s.email_subject, s.email_date,
                   r.row_id, r.grain, r.delivery_month, r.futures_symbol,
                   r.basis_cents, r.is_spot, r.spot_grain
            FROM snapshots s
            JOIN snapshot_rows r ON r.snapshot_id = s.id
            WHERE s.provider={ph} AND s.location={ph}
            ORDER BY s.timestamp, r.id
        """, (provider, location))
        db_rows = c.fetchall()
    finally:
        conn.close()

    snaps_by_id: dict = {}
    result: list      = []
    for row in db_rows:
        sid = row["snap_id"]
        if sid not in snaps_by_id:
            snap = Snapshot(
                id           = sid,
                timestamp    = row["timestamp"],
                provider     = row["provider"],
                location     = row["location"],
                source       = row["source"],
                emailSubject = row["email_subject"],
                emailDate    = row["email_date"],
                rows         = [],
            )
            snaps_by_id[sid] = snap
            result.append(snap)
        snaps_by_id[sid].rows.append(SnapshotRow(
            id            = row["row_id"],
            grain         = row["grain"],
            deliveryMonth = row["delivery_month"],
            futuresSymbol = row["futures_symbol"],
            basisCents    = row["basis_cents"],
            isSpot        = bool(row["is_spot"]),
            spotGrain     = row["spot_grain"],
        ))
    return result


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
                         facility_type: str | None = None,
                         region: str | None = None,
                         lat: float | None = None,
                         lon: float | None = None):
    """Insert or update metadata for a location (idempotent). Only non-None fields overwrite."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        if _use_pg():
            c.execute("""
                INSERT INTO location_meta (provider, location, state, facility_type, region, lat, lon)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider, location) DO UPDATE SET
                    state         = COALESCE(EXCLUDED.state,         location_meta.state),
                    facility_type = COALESCE(EXCLUDED.facility_type, location_meta.facility_type),
                    region        = COALESCE(EXCLUDED.region,        location_meta.region),
                    lat           = COALESCE(EXCLUDED.lat,           location_meta.lat),
                    lon           = COALESCE(EXCLUDED.lon,           location_meta.lon)
            """, (provider, location, state, facility_type, region, lat, lon))
        else:
            c.execute("""
                INSERT INTO location_meta (provider, location, state, facility_type, region, lat, lon)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, location) DO UPDATE SET
                    state         = COALESCE(excluded.state,         location_meta.state),
                    facility_type = COALESCE(excluded.facility_type, location_meta.facility_type),
                    region        = COALESCE(excluded.region,        location_meta.region),
                    lat           = COALESCE(excluded.lat,           location_meta.lat),
                    lon           = COALESCE(excluded.lon,           location_meta.lon)
            """, (provider, location, state, facility_type, region, lat, lon))
        conn.commit()
    finally:
        conn.close()


def get_location_meta(provider: str) -> dict[str, dict]:
    """Return {location_name: {state, facility_type, region, lat, lon}} for a provider."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    try:
        c.execute(
            f"SELECT location, state, facility_type, region, lat, lon FROM location_meta WHERE provider={ph}",
            (provider,),
        )
        return {
            row["location"]: {
                "state":         row["state"]         or "",
                "facility_type": row["facility_type"] or "",
                "region":        row["region"]        or "",
                "lat":           row["lat"],
                "lon":           row["lon"],
            }
            for row in c.fetchall()
        }
    finally:
        conn.close()


def get_all_location_meta() -> list[dict]:
    """Return all location_meta rows across providers as a list of dicts."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        c.execute("SELECT provider, location, state, facility_type, region, lat, lon FROM location_meta")
        return [
            {
                "provider":      row["provider"],
                "location":      row["location"],
                "state":         row["state"]         or "",
                "facility_type": row["facility_type"] or "",
                "region":        row["region"]        or "",
                "lat":           row["lat"],
                "lon":           row["lon"],
            }
            for row in c.fetchall()
        ]
    finally:
        conn.close()


def get_map_data() -> list[dict]:
    """
    Return one dict per (provider, location) with lat/lon and latest basis by canonical grain.

    Shape:
        [{"provider", "location", "state", "lat", "lon",
          "grains": {"Corn": -12, "Soybeans": 45, "Wheat (HRS)": 30, ...}}, ...]

    Only locations with known lat/lon are included. Inactive grains are excluded.
    Raw grains are normalized to canonical display names via the grain_map table.
    """
    conn = get_conn()
    c    = conn.cursor()
    try:
        c.execute("""
            WITH latest AS (
                SELECT provider, location, MAX(id) AS snap_id
                FROM snapshots
                GROUP BY provider, location
            )
            SELECT
                s.provider,
                s.location,
                lm.state,
                lm.facility_type,
                lm.region,
                lm.lat,
                lm.lon,
                lm.delivery_zone,
                r.grain,
                r.basis_cents
            FROM latest l
            JOIN snapshots s       ON s.id  = l.snap_id
            JOIN snapshot_rows r   ON r.snapshot_id = s.id
            LEFT JOIN location_meta lm
                ON lm.provider = s.provider AND lm.location = s.location
            WHERE r.is_spot = 0
              AND lm.lat IS NOT NULL
              AND lm.lon IS NOT NULL
            ORDER BY s.provider, s.location, r.grain
        """)
        rows = c.fetchall()
        # Load grain map for normalization
        try:
            c.execute(
                "SELECT raw_grain, canonical_grain, wheat_class, protein, is_active FROM grain_map"
            )
            gm = {r["raw_grain"]: dict(r) for r in c.fetchall()}
        except Exception:
            gm = {}
    finally:
        conn.close()

    def _canonical(raw: str) -> str | None:
        entry = gm.get(raw)
        if entry is None:
            return raw  # unknown grain: pass through
        if not entry["is_active"]:
            return None  # explicitly inactive — drop
        cls  = entry.get("wheat_class")
        prot = entry.get("protein")
        base = entry["canonical_grain"]
        if cls:
            return f"{base} ({cls} {prot})" if prot else f"{base} ({cls})"
        return base

    # Group grain rows into per-location dicts
    locs: dict[tuple, dict] = {}
    for row in rows:
        key = (row["provider"], row["location"])
        if key not in locs:
            locs[key] = {
                "provider":      row["provider"],
                "location":      row["location"],
                "state":         row["state"]         or "",
                "facility_type": row["facility_type"] or "",
                "region":        row["region"]        or "",
                "delivery_zone": row["delivery_zone"] or "",
                "lat":           row["lat"],
                "lon":           row["lon"],
                "grains":        {},
            }
        if row["basis_cents"] is not None:
            canon = _canonical(row["grain"])
            if canon and canon not in locs[key]["grains"]:
                locs[key]["grains"][canon] = row["basis_cents"]

    return list(locs.values())


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


def get_bids_filter_data() -> list[dict]:
    """
    Return all (provider, location) pairs with snapshot data plus their metadata.

    Shape: [{provider, location, state, facility_type, region}]
    Used to populate the Bids tab cascade filters and the Summary tab.
    """
    conn = get_conn()
    c    = conn.cursor()
    try:
        c.execute("""
            SELECT DISTINCT s.provider, s.location,
                   COALESCE(lm.state, '')         AS state,
                   COALESCE(lm.facility_type, '') AS facility_type,
                   COALESCE(lm.region, '')        AS region
            FROM snapshots s
            LEFT JOIN location_meta lm
                ON lm.provider = s.provider AND lm.location = s.location
            ORDER BY s.provider, s.location
        """)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_snapshots_bulk(pairs: list[tuple[str, str]], since_days: int = 400) -> dict:
    """
    Fetch all snapshots (with rows) for multiple (provider, location) pairs
    within the last `since_days` days.
    Returns: {(provider, location): [Snapshot, ...] sorted ascending by timestamp}
    """
    if not pairs:
        return {}
    from datetime import datetime, timedelta
    from collections import defaultdict

    cutoff     = (datetime.utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%dT00:00:00")
    conn       = get_conn()
    c          = conn.cursor()
    ph         = "%s" if _use_pg() else "?"
    try:
        pair_conds = " OR ".join(f"(s.provider={ph} AND s.location={ph})" for _ in pairs)
        params     = [v for p in pairs for v in p] + [cutoff]
        c.execute(f"""
            SELECT s.id     AS snap_id,
                   s.timestamp, s.provider, s.location, s.source,
                   r.row_id, r.grain, r.delivery_month, r.futures_symbol,
                   r.basis_cents, r.is_spot, r.spot_grain
            FROM snapshots s
            JOIN snapshot_rows r ON r.snapshot_id = s.id
            WHERE ({pair_conds}) AND s.timestamp >= {ph}
            ORDER BY s.provider, s.location, s.timestamp, r.id
        """, params)
        db_rows = c.fetchall()
    finally:
        conn.close()

    snaps_by_id: dict = {}
    result: dict      = defaultdict(list)

    for row in db_rows:
        sid = row["snap_id"]
        key = (row["provider"], row["location"])
        if sid not in snaps_by_id:
            snap = Snapshot(
                id        = sid,
                timestamp = row["timestamp"],
                provider  = row["provider"],
                location  = row["location"],
                source    = row["source"],
                rows      = [],
            )
            snaps_by_id[sid] = snap
            result[key].append(snap)
        snaps_by_id[sid].rows.append(SnapshotRow(
            id            = row["row_id"],
            grain         = row["grain"],
            deliveryMonth = row["delivery_month"],
            futuresSymbol = row["futures_symbol"],
            basisCents    = row["basis_cents"],
            isSpot        = bool(row["is_spot"]),
            spotGrain     = row["spot_grain"],
        ))

    return dict(result)


# ── Data retention / pruning ───────────────────────────────────────────────────

def prune_old_snapshots(dry_run: bool = False) -> dict:
    """
    Apply tiered data retention to snapshots (PostgreSQL only).

    Policy:
      • Current calendar month  → keep ALL  (daily granularity)
      • Anything older          → keep ONE per (provider, location, ISO week) — forever

    The ON DELETE CASCADE on snapshot_rows handles row cleanup automatically.

    Args:
        dry_run: If True, count candidates but do not delete anything.

    Returns:
        dict with keys: candidates, deleted, snaps_after, rows_after
    """
    if not _use_pg():
        # SQLite is only used for local dev — data volume is small, skip pruning.
        return {"candidates": 0, "deleted": 0, "snaps_after": 0, "rows_after": 0}

    # Two-tier retention:
    #   Tier 1 — current month: keep everything
    #   Tier 2 — anything older: keep one (most recent) per provider/location/ISO week
    #            Weekly resolution is preserved forever — no monthly rollup.
    _KEEPERS_SQL = """
        -- Tier 1: current calendar month — keep everything
        SELECT id FROM snapshots
        WHERE created_at >= DATE_TRUNC('month', NOW())

        UNION

        -- Tier 2: anything older than current month — one per provider/location/week (forever)
        SELECT id FROM (
            SELECT DISTINCT ON (provider, location, DATE_TRUNC('week', created_at))
                id
            FROM snapshots
            WHERE created_at < DATE_TRUNC('month', NOW())
            ORDER BY provider, location, DATE_TRUNC('week', created_at), created_at DESC
        ) weekly
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
