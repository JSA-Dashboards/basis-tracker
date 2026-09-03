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
import sys
import time
from pathlib import Path
from models import Snapshot, SnapshotRow

DB_PATH = Path(__file__).parent / "basis_tracker.db"


# ── Backend helpers ───────────────────────────────────────────────────────────

def _pg_url() -> str:
    """Return the PostgreSQL connection URL, or '' if using SQLite."""
    return os.getenv("DATABASE_URL", "")


def _backend() -> str:
    """Active backend: 'snowflake' | 'postgres' | 'sqlite'.

    Snowflake wins when USE_SNOWFLAKE is truthy (even if DATABASE_URL is still
    set), so the cutover is a single env flag. Otherwise Postgres if a
    DATABASE_URL exists, else local SQLite."""
    if os.getenv("USE_SNOWFLAKE", "").strip().lower() in ("1", "true", "yes", "on"):
        return "snowflake"
    if _sf_active_session_conn() is not None:   # running inside Streamlit-in-Snowflake
        return "snowflake"
    return "postgres" if os.getenv("DATABASE_URL") else "sqlite"


def _use_pg() -> bool:
    return _backend() == "postgres"


def _use_sf() -> bool:
    return _backend() == "snowflake"


def _ph() -> str:
    """Parameter placeholder for the active backend (Snowflake & Postgres use %s)."""
    return "?" if _backend() == "sqlite" else "%s"


# ── Snowflake adapter ──────────────────────────────────────────────────────────
# Snowflake's DictCursor returns UPPERCASE column names; the whole app reads rows
# by lowercase keys (r['basis_cents']). These thin wrappers lowercase result keys
# and give the connection the psycopg2-ish surface (cursor/commit/close) the code
# already expects, so the 40 public functions work unchanged on Snowflake.

class _LowerDictCursor:
    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, params=None):
        self._c.execute(sql, params)
        return self

    def executemany(self, sql, seq):
        self._c.executemany(sql, seq)
        return self

    @staticmethod
    def _lc(row):
        return {k.lower(): v for k, v in row.items()} if row else row

    def fetchone(self):
        return self._lc(self._c.fetchone())

    def fetchall(self):
        return [self._lc(r) for r in self._c.fetchall()]

    def fetchmany(self, size=None):
        rows = self._c.fetchmany(size) if size is not None else self._c.fetchmany()
        return [self._lc(r) for r in rows]

    @property
    def rowcount(self):
        return self._c.rowcount

    def close(self):
        return self._c.close()


class _SFConn:
    def __init__(self, conn, owned=True):
        self._conn = conn
        self._owned = owned          # False = shared SiS session conn; never close it

    def cursor(self):
        import snowflake.connector
        return _LowerDictCursor(self._conn.cursor(snowflake.connector.DictCursor))

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        if self._owned:
            return self._conn.close()

    def execute_string(self, sql):
        return self._conn.execute_string(sql)


_SF_SESSION_CONN = "unchecked"   # cached SiS active-session conn ("unchecked" until probed)


def _sf_active_session_conn():
    """Inside Streamlit-in-Snowflake (or any Snowpark context) reuse the active
    session's connection — no credentials needed. Returns None if not in-session.
    The probe (and its result, including None) is cached so it runs at most once."""
    global _SF_SESSION_CONN
    if _SF_SESSION_CONN != "unchecked":
        return _SF_SESSION_CONN
    try:
        from snowflake.snowpark.context import get_active_session
        _SF_SESSION_CONN = get_active_session().connection
    except Exception:
        _SF_SESSION_CONN = None
    return _SF_SESSION_CONN


def _sf_get():
    """Return (raw_conn, owned). In SiS → shared active-session conn (owned=False);
    otherwise a fresh connection from SNOWFLAKE_* env (owned=True, for local/jobs)."""
    sess = _sf_active_session_conn()
    if sess is not None:
        _force_pyformat(sess)     # SiS/Snowpark defaults to qmark; our SQL uses %s
        return sess, False
    import snowflake.connector as sc
    kw = dict(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD") or None,
        role=os.environ.get("SNOWFLAKE_ROLE") or None,
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE") or None,
        database=os.environ.get("SNOWFLAKE_DATABASE") or None,
        schema=os.environ.get("SNOWFLAKE_SCHEMA") or None,
        login_timeout=30,
    )
    conn = sc.connect(**{k: v for k, v in kw.items() if v is not None})
    _force_pyformat(conn)
    return conn, True


def _force_pyformat(conn) -> None:
    """Ensure %s-style parameter binding. Inside Streamlit-in-Snowflake the active
    session's connection uses paramstyle 'qmark', which would pass our %s through
    literally and break every parameterized query; force it to pyformat."""
    try:
        conn._paramstyle = "pyformat"
    except Exception:
        pass


def _merge(c, table, cols, values, key_cols, update_cols=None, coalesce=False):
    """Snowflake single-row upsert emulating INSERT ... ON CONFLICT(key_cols).
    update_cols=None → DO NOTHING; else DO UPDATE SET those cols. coalesce=True
    keeps the existing value when the new one is NULL (COALESCE(new, old)).
    `values` is a tuple aligned with `cols`."""
    using = ", ".join(f"%s AS {col}" for col in cols)
    on = " AND ".join(f"t.{k} = s.{k}" for k in key_cols)
    ins_cols = ", ".join(cols)
    ins_vals = ", ".join(f"s.{col}" for col in cols)
    sql = f"MERGE INTO {table} t USING (SELECT {using}) s ON {on} "
    if update_cols:
        if coalesce:
            setc = ", ".join(f"t.{col} = COALESCE(s.{col}, t.{col})" for col in update_cols)
        else:
            setc = ", ".join(f"t.{col} = s.{col}" for col in update_cols)
        sql += f"WHEN MATCHED THEN UPDATE SET {setc} "
    sql += f"WHEN NOT MATCHED THEN INSERT ({ins_cols}) VALUES ({ins_vals})"
    c.execute(sql, values)


def _merge_bulk(c, table, cols_types, rows, key_cols, update_cols=None,
                coalesce=False, chunk=250, gen_id_col=None):
    """Chunked multi-row Snowflake upsert. cols_types is [(name, snowflaketype), ...]
    (types cast the VALUES literals so NULL-only columns still resolve). `rows` is a
    list of tuples aligned with cols_types. Returns rows processed.

    gen_id_col: name of an auto-id column to generate for newly-inserted rows as
    MAX(existing)+ROW_NUMBER() — assigned in SQL so it always sits above the current
    max (no IDENTITY reliance, no reseed, collision-free across chunks since each
    chunk's MAX sees the prior chunk's inserts)."""
    if not rows:
        return 0
    names = [n for n, _ in cols_types]
    sel = ", ".join(f"column{i + 1}::{t} AS {n}" for i, (n, t) in enumerate(cols_types))
    ins_names = names[:]
    ins_vals = ["s." + n for n in names]
    if gen_id_col:
        sel = (f"(SELECT COALESCE(MAX({gen_id_col}), 0) FROM {table}) "
               f"+ ROW_NUMBER() OVER (ORDER BY column1) AS {gen_id_col}, " + sel)
        ins_names = [gen_id_col] + ins_names
        ins_vals = ["s." + gen_id_col] + ins_vals
    on = " AND ".join(f"t.{k} = s.{k}" for k in key_cols)
    ins = f"WHEN NOT MATCHED THEN INSERT ({', '.join(ins_names)}) " \
          f"VALUES ({', '.join(ins_vals)})"
    upd = ""
    if update_cols:
        setc = (", ".join(f"t.{n} = COALESCE(s.{n}, t.{n})" for n in update_cols)
                if coalesce else ", ".join(f"t.{n} = s.{n}" for n in update_cols))
        upd = f"WHEN MATCHED THEN UPDATE SET {setc} "
    ncol = len(names)
    total = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        vgroups = ", ".join("(" + ", ".join(["%s"] * ncol) + ")" for _ in part)
        sql = (f"MERGE INTO {table} t USING "
               f"(SELECT {sel} FROM VALUES {vgroups}) s ON {on} {upd}{ins}")
        c.execute(sql, [v for row in part for v in row])
        total += len(part)
    return total


def _sf_lookup_ids(c, table, key_cols, keys, id_col="id", chunk=500):
    """Snowflake: map each (key tuple) → id via a VALUES join (no psycopg2 IN %s)."""
    out = {}
    if not keys:
        return out
    sel = ", ".join(f"column{i + 1}::string AS {k}" for i, k in enumerate(key_cols))
    on = " AND ".join(f"t.{k} = k.{k}" for k in key_cols)
    cols = ", ".join(f"t.{k}" for k in key_cols)
    keys = list(keys)
    for i in range(0, len(keys), chunk):
        part = keys[i:i + chunk]
        vgroups = ", ".join("(" + ", ".join(["%s"] * len(key_cols)) + ")" for _ in part)
        c.execute(
            f"SELECT t.{id_col} AS _id, {cols} FROM {table} t "
            f"JOIN (SELECT {sel} FROM VALUES {vgroups}) k ON {on}",
            [v for key in part for v in key])
        for r in c.fetchall():
            out[tuple(r[k] for k in key_cols)] = r["_id"]
    return out


def get_conn():
    """Open and return a database connection for the active backend."""
    b = _backend()
    if b == "snowflake":
        raw, owned = _sf_get()
        return _SFConn(raw, owned=owned)
    if b == "postgres":
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(_pg_url(), cursor_factory=psycopg2.extras.RealDictCursor)
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
    """CREATE TABLE IF NOT EXISTS futures_prices (
        date        TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        price_cents REAL NOT NULL,
        captured_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (date, symbol)
    )""",
    """CREATE TABLE IF NOT EXISTS rail_fob (
        date         TEXT NOT NULL,
        source       TEXT NOT NULL DEFAULT 'manual',
        market       TEXT NOT NULL,
        rail         TEXT,
        commodity    TEXT,
        period       TEXT NOT NULL,
        period_order INTEGER,
        futures      TEXT,
        bid          INTEGER,
        offer        INTEGER,
        bid_raw      TEXT,
        offer_raw    TEXT,
        captured_at  TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (date, source, market, period)
    )""",
    """CREATE TABLE IF NOT EXISTS spot_forward_manual (
        date              TEXT NOT NULL,
        corn_cif_cents    INTEGER,
        bean_cif_cents    INTEGER,
        ilr_freight_cents INTEGER,
        chi_eth_cents     INTEGER,
        ny_eth_cents      INTEGER,
        captured_at       TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (date)
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
    """CREATE TABLE IF NOT EXISTS futures_prices (
        date        TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        price_cents DOUBLE PRECISION NOT NULL,
        captured_at TEXT,
        PRIMARY KEY (date, symbol)
    )""",
    """CREATE TABLE IF NOT EXISTS rail_fob (
        date         TEXT NOT NULL,
        source       TEXT NOT NULL DEFAULT 'manual',
        market       TEXT NOT NULL,
        rail         TEXT,
        commodity    TEXT,
        period       TEXT NOT NULL,
        period_order INTEGER,
        futures      TEXT,
        bid          INTEGER,
        offer        INTEGER,
        bid_raw      TEXT,
        offer_raw    TEXT,
        captured_at  TEXT,
        PRIMARY KEY (date, source, market, period)
    )""",
    """CREATE TABLE IF NOT EXISTS spot_forward_manual (
        date              TEXT NOT NULL,
        corn_cif_cents    INTEGER,
        bean_cif_cents    INTEGER,
        ilr_freight_cents INTEGER,
        chi_eth_cents     INTEGER,
        ny_eth_cents      INTEGER,
        captured_at       TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (date)
    )""",
]

_MIGRATE_DDL = [
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS lat           REAL",
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS lon           REAL",
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS region        TEXT",
    "ALTER TABLE location_meta ADD COLUMN IF NOT EXISTS delivery_zone TEXT",
    "CREATE INDEX IF NOT EXISTS idx_snap_prov_loc_ts ON snapshots(provider, location, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_snap_rows_sid ON snapshot_rows(snapshot_id)",
    "ALTER TABLE rail_fob ADD COLUMN IF NOT EXISTS bid_raw   TEXT",
    "ALTER TABLE rail_fob ADD COLUMN IF NOT EXISTS offer_raw TEXT",
]


def init_db():
    """Create all tables and indexes if they don't exist yet."""
    # Serialize init across app instances. On Streamlit Cloud a reboot can run
    # two instances briefly; both call init_db() and race on the idempotent
    # location_meta upserts, which can deadlock. A session-level Postgres
    # advisory lock held for the whole init lets only one process seed at a time.
    # (Normally auto-released when the holder's connection dies, but through the
    # Supabase pooler that reaping can lag minutes — see the bounded acquire
    # below.) No-op on SQLite.
    if _use_sf():
        # Snowflake schema + seed data are provisioned by snowflake/01_schema.sql
        # and the one-time migration load — nothing for init_db to create/seed.
        return
    _INIT_LOCK_KEY = 727274
    lock_conn = None
    have_lock = False
    if _use_pg():
        lock_conn = get_conn()
        # Bounded, NON-blocking acquire. A plain pg_advisory_lock() blocks
        # indefinitely; through Supabase's pooler a crashed init (which grabbed
        # the lock but died before releasing it) leaves an orphaned backend
        # holding it for minutes, wedging every later start with a statement
        # timeout. Poll pg_try_advisory_lock instead, and if it's still busy
        # after the window, seed anyway — the lock only serializes idempotent
        # seeding, so a rare race is far better than a hard startup failure.
        _lc = lock_conn.cursor()
        _deadline = time.time() + 30
        while True:
            _lc.execute("SELECT pg_try_advisory_lock(%s) AS got", (_INIT_LOCK_KEY,))
            _row = _lc.fetchone()                      # RealDictCursor → dict
            have_lock = bool(_row["got"] if isinstance(_row, dict) else _row[0])
            lock_conn.commit()
            if have_lock or time.time() >= _deadline:
                break
            time.sleep(1.5)
        if not have_lock:
            print("init_db: advisory lock busy after 30s — seeding without it.",
                  file=sys.stderr)
    try:
        conn = get_conn()
        c    = conn.cursor()
        ddl  = _PG_DDL if _use_pg() else _SQLITE_DDL
        try:
            for stmt in ddl:
                c.execute(stmt)
            # Add columns to existing databases that pre-date this schema change.
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
    finally:
        if lock_conn is not None:
            try:
                if have_lock:
                    lock_conn.cursor().execute(
                        "SELECT pg_advisory_unlock(%s)", (_INIT_LOCK_KEY,))
                    lock_conn.commit()
            finally:
                lock_conn.close()


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
    ph   = _ph()
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
    ph   = _ph()
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
    ph   = _ph()
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
    ph   = _ph()
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
        if _use_sf():
            _merge(c, "imported_emails", ["email_id", "subject"],
                   (email_id, subject), ["email_id"])
        elif _use_pg():
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
        if _use_sf():
            # ── Snowflake path (MERGE + explicit ids, no RETURNING/IDENTITY) ──
            c.execute("SELECT COALESCE(MAX(id), 0) + 1 AS nid FROM snapshots")
            _nid = c.fetchone()["nid"]
            _merge(c, "snapshots",
                   ["id", "timestamp", "provider", "location", "source",
                    "email_subject", "email_date"],
                   (_nid, snap["timestamp"], snap["provider"], snap["location"],
                    snap.get("source", "manual"), snap.get("emailSubject"),
                    snap.get("emailDate")),
                   ["timestamp", "provider", "location"])
            c.execute(
                "SELECT id FROM snapshots WHERE timestamp=%s AND provider=%s AND location=%s",
                (snap["timestamp"], snap["provider"], snap["location"]))
            snap_id = c.fetchone()["id"]
            c.execute("SELECT COALESCE(MAX(id), 0) AS b FROM snapshot_rows")
            _rbase = c.fetchone()["b"]
            for _i, r in enumerate(snap.get("rows", [])):
                _merge(c, "snapshot_rows",
                       ["id", "snapshot_id", "row_id", "grain", "delivery_month",
                        "futures_symbol", "basis_cents", "is_spot", "spot_grain"],
                       (_rbase + _i + 1, snap_id, r["id"], r["grain"], r["deliveryMonth"],
                        r["futuresSymbol"], r.get("basisCents"),
                        1 if r.get("isSpot") else 0, r.get("spotGrain")),
                       ["snapshot_id", "row_id"])
        elif _use_pg():
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


def _drop_stale_forward_rows(snaps: list[dict]) -> None:
    """Drop non-spot rows whose delivery month is already before the snapshot's own
    (year, month) — e.g. a lingering 'Sep 2025' milo bid on a 2026 feed. Such stale
    bids create phantom nearest-month slots and bogus contract-rolls downstream.
    Universal guard so every scraper is covered, not just one parser. Best-effort:
    unparseable delivery labels and spot rows are always kept. Mutates `snaps`."""
    try:
        import delivery_period as _dp
    except Exception:
        return
    from datetime import datetime as _dt
    for s in snaps:
        try:
            _d = _dt.fromisoformat((s.get("timestamp") or "").replace("Z", "+00:00"))
            ym = (_d.year, _d.month)
        except Exception:
            continue
        kept = []
        for r in s.get("rows", []):
            if r.get("isSpot"):
                kept.append(r); continue
            try:
                k = _dp.canonical(r.get("deliveryMonth"), r.get("futuresSymbol"))
            except Exception:
                k = None
            if k and k < ym:
                continue                     # stale past-delivery bid — skip
            kept.append(r)
        s["rows"] = kept


def upsert_snapshots(snaps: list[dict]) -> int:
    """Bulk upsert: insert many snapshots + their rows in ONE connection using
    batched multi-row INSERTs. Same INSERT … ON CONFLICT DO NOTHING semantics as
    upsert_snapshot. Replaces the per-snapshot connect/insert/commit storm — e.g.
    CHS produces ~500 snapshots / ~2000 rows; the old path opened a fresh cloud
    connection per snapshot (minutes) and even a single-connection row-at-a-time
    loop was ~100s of round-trips. Here it's 3 round-trips: insert snapshots,
    fetch their ids, insert rows. Note several snaps can share one
    (timestamp, provider, location) — e.g. one per grain — and correctly collapse
    to a single snapshot whose rows are the union. Returns rows written/seen."""
    if not snaps:
        return 0
    _drop_stale_forward_rows(snaps)
    conn = get_conn()
    c    = conn.cursor()
    try:
        # Distinct snapshot keys (a location's per-grain snaps share one key).
        seen: dict[tuple, tuple] = {}
        for s in snaps:
            key = (s["timestamp"], s["provider"], s["location"])
            if key not in seen:
                seen[key] = (s["timestamp"], s["provider"], s["location"],
                             s.get("source", "manual"), s.get("emailSubject"),
                             s.get("emailDate"))

        if _use_sf():
            _merge_bulk(c, "snapshots",
                        [("timestamp", "string"), ("provider", "string"),
                         ("location", "string"), ("source", "string"),
                         ("email_subject", "string"), ("email_date", "string")],
                        list(seen.values()), ["timestamp", "provider", "location"],
                        gen_id_col="id")
            id_map = _sf_lookup_ids(c, "snapshots",
                                    ["timestamp", "provider", "location"], list(seen.keys()))
            row_tuples = []
            for s in snaps:
                sid = id_map.get((s["timestamp"], s["provider"], s["location"]))
                if sid is None:
                    continue
                for r in s.get("rows", []):
                    row_tuples.append((
                        sid, r["id"], r["grain"], r["deliveryMonth"],
                        r["futuresSymbol"], r.get("basisCents"),
                        1 if r.get("isSpot") else 0, r.get("spotGrain")))
            _merge_bulk(c, "snapshot_rows",
                        [("snapshot_id", "number"), ("row_id", "string"),
                         ("grain", "string"), ("delivery_month", "string"),
                         ("futures_symbol", "string"), ("basis_cents", "number"),
                         ("is_spot", "number"), ("spot_grain", "string")],
                        row_tuples, ["snapshot_id", "row_id"], gen_id_col="id")
            conn.commit()
            return len(row_tuples)

        if _use_pg():
            from psycopg2.extras import execute_values
            execute_values(
                c,
                """INSERT INTO snapshots
                   (timestamp, provider, location, source, email_subject, email_date)
                   VALUES %s ON CONFLICT (timestamp, provider, location) DO NOTHING""",
                list(seen.values()),
            )
            id_map: dict[tuple, int] = {}
            keys = list(seen.keys())
            for i in range(0, len(keys), 1000):
                chunk = keys[i:i + 1000]
                c.execute(
                    "SELECT id, timestamp, provider, location FROM snapshots "
                    "WHERE (timestamp, provider, location) IN %s",
                    (tuple(chunk),),
                )
                for r in c.fetchall():
                    id_map[(r["timestamp"], r["provider"], r["location"])] = r["id"]

            row_tuples = []
            for s in snaps:
                sid = id_map.get((s["timestamp"], s["provider"], s["location"]))
                if sid is None:
                    continue
                for r in s.get("rows", []):
                    row_tuples.append((
                        sid, r["id"], r["grain"], r["deliveryMonth"],
                        r["futuresSymbol"], r.get("basisCents"),
                        1 if r.get("isSpot") else 0, r.get("spotGrain")))
            if row_tuples:
                execute_values(
                    c,
                    """INSERT INTO snapshot_rows
                       (snapshot_id, row_id, grain, delivery_month, futures_symbol,
                        basis_cents, is_spot, spot_grain)
                       VALUES %s ON CONFLICT (snapshot_id, row_id) DO NOTHING""",
                    row_tuples,
                )
            conn.commit()
            return len(row_tuples)

        # ── SQLite fallback (local dev) — executemany, still one connection ──
        c.executemany(
            """INSERT OR IGNORE INTO snapshots
               (timestamp, provider, location, source, email_subject, email_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            list(seen.values()),
        )
        id_map = {}
        for key in seen:
            c.execute("SELECT id FROM snapshots WHERE timestamp=? AND provider=? AND location=?", key)
            id_map[key] = c.fetchone()["id"]
        row_tuples = []
        for s in snaps:
            sid = id_map.get((s["timestamp"], s["provider"], s["location"]))
            if sid is None:
                continue
            for r in s.get("rows", []):
                row_tuples.append((
                    sid, r["id"], r["grain"], r["deliveryMonth"],
                    r["futuresSymbol"], r.get("basisCents"),
                    1 if r.get("isSpot") else 0, r.get("spotGrain")))
        if row_tuples:
            c.executemany(
                """INSERT OR IGNORE INTO snapshot_rows
                   (snapshot_id, row_id, grain, delivery_month, futures_symbol,
                    basis_cents, is_spot, spot_grain)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                row_tuples,
            )
        conn.commit()
        return len(row_tuples)
    finally:
        conn.close()


# ── Reads ──────────────────────────────────────────────────────────────────────

def get_snapshots(provider: str, location: str) -> list[Snapshot]:
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
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
    ph   = _ph()
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
        if _use_sf():
            _merge(c, "location_meta",
                   ["provider", "location", "state", "facility_type", "region", "lat", "lon"],
                   (provider, location, state, facility_type, region, lat, lon),
                   ["provider", "location"],
                   update_cols=["state", "facility_type", "region", "lat", "lon"],
                   coalesce=True)
        elif _use_pg():
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


def upsert_location_metas(provider: str, items: list[dict]) -> int:
    """Bulk idempotent upsert of location metadata over ONE connection (same
    COALESCE-only-non-None semantics as upsert_location_meta). Avoids the
    per-location connection storm when a scraper sets meta for many locations.
    items: [{location, state?, facility_type?, region?, lat?, lon?}]."""
    if not items:
        return 0
    conn = get_conn()
    c    = conn.cursor()
    try:
        rows = [(provider, it["location"], it.get("state"), it.get("facility_type"),
                 it.get("region"), it.get("lat"), it.get("lon")) for it in items]
        if _use_sf():
            _merge_bulk(c, "location_meta",
                        [("provider", "string"), ("location", "string"),
                         ("state", "string"), ("facility_type", "string"),
                         ("region", "string"), ("lat", "float"), ("lon", "float")],
                        rows, ["provider", "location"],
                        update_cols=["state", "facility_type", "region", "lat", "lon"],
                        coalesce=True)
            conn.commit()
            return len(rows)
        if _use_pg():
            c.executemany(
                """INSERT INTO location_meta
                   (provider, location, state, facility_type, region, lat, lon)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (provider, location) DO UPDATE SET
                       state         = COALESCE(EXCLUDED.state,         location_meta.state),
                       facility_type = COALESCE(EXCLUDED.facility_type, location_meta.facility_type),
                       region        = COALESCE(EXCLUDED.region,        location_meta.region),
                       lat           = COALESCE(EXCLUDED.lat,           location_meta.lat),
                       lon           = COALESCE(EXCLUDED.lon,           location_meta.lon)""",
                rows)
        else:
            c.executemany(
                """INSERT INTO location_meta
                   (provider, location, state, facility_type, region, lat, lon)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, location) DO UPDATE SET
                       state         = COALESCE(excluded.state,         location_meta.state),
                       facility_type = COALESCE(excluded.facility_type, location_meta.facility_type),
                       region        = COALESCE(excluded.region,        location_meta.region),
                       lat           = COALESCE(excluded.lat,           location_meta.lat),
                       lon           = COALESCE(excluded.lon,           location_meta.lon)""",
                rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def get_location_meta(provider: str) -> dict[str, dict]:
    """Return {location_name: {state, facility_type, region, lat, lon}} for a provider."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
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
    Return one dict per (provider, location) with lat/lon, metadata, and the full
    set of latest forward bids (per grain & delivery month) for the map tab.

    Shape:
        [{"provider", "location", "state", "facility_type", "region", "lat", "lon",
          "bids": [{"grain", "delivery_month", "futures_symbol", "basis"}, ...]}, ...]

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
                r.grain,
                r.delivery_month,
                r.futures_symbol,
                r.basis_cents
            FROM latest l
            JOIN snapshots s       ON s.id  = l.snap_id
            JOIN snapshot_rows r   ON r.snapshot_id = s.id
            LEFT JOIN location_meta lm
                ON lm.provider = s.provider AND lm.location = s.location
            WHERE r.is_spot = 0
              AND lm.lat IS NOT NULL
              AND lm.lon IS NOT NULL
            ORDER BY s.provider, s.location
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

    # Group bid rows into per-location dicts
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
                "lat":           row["lat"],
                "lon":           row["lon"],
                "bids":          [],
            }
        if row["basis_cents"] is not None:
            canon = _canonical(row["grain"])
            if canon:
                locs[key]["bids"].append({
                    "grain":          canon,
                    "delivery_month": row["delivery_month"] or "",
                    "futures_symbol": row["futures_symbol"] or "",
                    "basis":          row["basis_cents"],
                })

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
                   COALESCE(lm.region, '')        AS region,
                   lm.lat AS lat, lm.lon AS lon
            FROM snapshots s
            LEFT JOIN location_meta lm
                ON lm.provider = s.provider AND lm.location = s.location
            ORDER BY s.provider, s.location
        """)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def grain_counts_by_facility(days: int = 21) -> list[tuple]:
    """
    Count recent (web) bid rows by (facility_type, grain).

    Used to default the Summary tab's grain to whichever commodity has the most
    bids for the selected location type. Returns [(facility_type, grain, n), …].
    """
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"""
            SELECT lm.facility_type AS ft, r.grain AS grain, COUNT(*) AS n
            FROM snapshots s
            JOIN snapshot_rows r ON r.snapshot_id = s.id
            JOIN location_meta lm
                ON lm.provider = s.provider AND lm.location = s.location
            WHERE s.source = 'web'
              AND s.timestamp >= {ph}
              AND lm.facility_type IS NOT NULL AND lm.facility_type <> ''
            GROUP BY lm.facility_type, r.grain
        """, (cutoff,))
        return [(r["ft"], r["grain"], r["n"]) for r in c.fetchall()]
    finally:
        conn.close()


def save_futures_curve(curve: dict, date: str) -> int:
    """Upsert a day's futures curve ({symbol -> cents}) under `date` ('YYYY-MM-DD').
    Returns the number of symbols written."""
    if not curve:
        return 0
    from datetime import datetime, timezone
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    now  = datetime.now(timezone.utc).isoformat()
    sql  = (f"INSERT INTO futures_prices (date, symbol, price_cents, captured_at) "
            f"VALUES ({ph},{ph},{ph},{ph}) "
            f"ON CONFLICT (date, symbol) DO UPDATE "
            f"SET price_cents = EXCLUDED.price_cents, captured_at = EXCLUDED.captured_at")
    try:
        if _use_sf():
            _merge_bulk(c, "futures_prices",
                        [("date", "string"), ("symbol", "string"),
                         ("price_cents", "float"), ("captured_at", "string")],
                        [(date, sym, float(px), now) for sym, px in curve.items()],
                        ["date", "symbol"], update_cols=["price_cents", "captured_at"])
            conn.commit()
            return len(curve)
        for sym, px in curve.items():
            c.execute(sql, (date, sym, float(px), now))
        conn.commit()
    finally:
        conn.close()
    return len(curve)


def get_futures_curve(date: str) -> dict:
    """Return the stored futures curve {symbol -> cents} for `date`, or {} if none."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"SELECT symbol, price_cents FROM futures_prices WHERE date={ph}", (date,))
        return {r["symbol"]: r["price_cents"] for r in c.fetchall()}
    finally:
        conn.close()


def get_roll_spread(from_sym: str, to_sym: str):
    """Futures spread in cents = price(from) - price(to) at the most recent date where
    BOTH contracts had a stored price. While both still trade this equals today's
    spread; once `from` passes first notice (no longer quoted) it stays frozen at the
    last close where both were quoted. Returns None if they never co-occur."""
    if not from_sym or not to_sym:
        return None
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"""SELECT a.price_cents - b.price_cents AS spread
                      FROM futures_prices a
                      JOIN futures_prices b ON a.date = b.date
                      WHERE a.symbol={ph} AND b.symbol={ph}
                      ORDER BY a.date DESC
                      LIMIT 1""", (from_sym, to_sym))
        row = c.fetchone()
        return row["spread"] if row else None
    finally:
        conn.close()


def save_rail_fob(date: str, source: str, rows: list) -> int:
    """Upsert a dated rail FOB posting. `rows` items:
    {market, rail, commodity, period, period_order, futures, bid, offer}.
    Returns the number of cells written."""
    if not rows:
        return 0
    from datetime import datetime, timezone
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    now  = datetime.now(timezone.utc).isoformat()
    sql  = (f"INSERT INTO rail_fob (date, source, market, rail, commodity, period, "
            f"period_order, futures, bid, offer, bid_raw, offer_raw, captured_at) "
            f"VALUES ({','.join([ph]*13)}) "
            f"ON CONFLICT (date, source, market, period) DO UPDATE SET "
            f"rail=EXCLUDED.rail, commodity=EXCLUDED.commodity, "
            f"period_order=EXCLUDED.period_order, futures=EXCLUDED.futures, "
            f"bid=EXCLUDED.bid, offer=EXCLUDED.offer, "
            f"bid_raw=EXCLUDED.bid_raw, offer_raw=EXCLUDED.offer_raw, "
            f"captured_at=EXCLUDED.captured_at")
    try:
        if _use_sf():
            _merge_bulk(c, "rail_fob",
                        [("date", "string"), ("source", "string"), ("market", "string"),
                         ("rail", "string"), ("commodity", "string"), ("period", "string"),
                         ("period_order", "number"), ("futures", "string"),
                         ("bid", "number"), ("offer", "number"), ("bid_raw", "string"),
                         ("offer_raw", "string"), ("captured_at", "string")],
                        [(date, source, r["market"], r.get("rail"), r.get("commodity"),
                          r["period"], r.get("period_order"), r.get("futures"),
                          r.get("bid"), r.get("offer"), r.get("bid_raw"),
                          r.get("offer_raw"), now) for r in rows],
                        ["date", "source", "market", "period"],
                        update_cols=["rail", "commodity", "period_order", "futures",
                                     "bid", "offer", "bid_raw", "offer_raw", "captured_at"])
            conn.commit()
            return len(rows)
        for r in rows:
            c.execute(sql, (date, source, r["market"], r.get("rail"), r.get("commodity"),
                            r["period"], r.get("period_order"), r.get("futures"),
                            r.get("bid"), r.get("offer"),
                            r.get("bid_raw"), r.get("offer_raw"), now))
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def get_rail_fob(source: str, date: str) -> list:
    """Return all rail FOB cells for a source + date, ordered by market then period_order."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"""SELECT market, rail, commodity, period, period_order, futures,
                             bid, offer, bid_raw, offer_raw
                      FROM rail_fob WHERE source={ph} AND date={ph}
                      ORDER BY market, period_order, period""", (source, date))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def get_rail_fob_dates(source: str) -> list:
    """Distinct posting dates for a source, most recent first."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"SELECT DISTINCT date FROM rail_fob WHERE source={ph} ORDER BY date DESC",
                  (source,))
        return [r["date"] for r in c.fetchall()]
    finally:
        conn.close()


def get_rail_fob_all(source: str) -> list:
    """All rail FOB cells for a source across every date (for trend/change columns)."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"""SELECT date, market, rail, commodity, period, period_order, futures,
                             bid, offer, bid_raw, offer_raw
                      FROM rail_fob WHERE source={ph}
                      ORDER BY market, period_order, period, date""", (source,))
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
    ph         = _ph()
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

def save_spot_forward_manual(date: str, corn_cif: int | None = None, bean_cif: int | None = None,
                             ilr_freight: int | None = None, chi_eth: int | None = None,
                             ny_eth: int | None = None) -> bool:
    """Upsert manual spot/forward entries for a date. All params in cents."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        if _use_sf():
            _merge(c, "spot_forward_manual",
                   ["date", "corn_cif_cents", "bean_cif_cents", "ilr_freight_cents",
                    "chi_eth_cents", "ny_eth_cents"],
                   (date, corn_cif, bean_cif, ilr_freight, chi_eth, ny_eth), ["date"],
                   update_cols=["corn_cif_cents", "bean_cif_cents", "ilr_freight_cents",
                                "chi_eth_cents", "ny_eth_cents"], coalesce=True)
        elif _use_pg():
            c.execute("""
                INSERT INTO spot_forward_manual
                (date, corn_cif_cents, bean_cif_cents, ilr_freight_cents, chi_eth_cents, ny_eth_cents)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (date) DO UPDATE SET
                    corn_cif_cents = COALESCE(EXCLUDED.corn_cif_cents, spot_forward_manual.corn_cif_cents),
                    bean_cif_cents = COALESCE(EXCLUDED.bean_cif_cents, spot_forward_manual.bean_cif_cents),
                    ilr_freight_cents = COALESCE(EXCLUDED.ilr_freight_cents, spot_forward_manual.ilr_freight_cents),
                    chi_eth_cents = COALESCE(EXCLUDED.chi_eth_cents, spot_forward_manual.chi_eth_cents),
                    ny_eth_cents = COALESCE(EXCLUDED.ny_eth_cents, spot_forward_manual.ny_eth_cents)
            """, (date, corn_cif, bean_cif, ilr_freight, chi_eth, ny_eth))
        else:
            c.execute("""
                INSERT INTO spot_forward_manual
                (date, corn_cif_cents, bean_cif_cents, ilr_freight_cents, chi_eth_cents, ny_eth_cents)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    corn_cif_cents = COALESCE(excluded.corn_cif_cents, spot_forward_manual.corn_cif_cents),
                    bean_cif_cents = COALESCE(excluded.bean_cif_cents, spot_forward_manual.bean_cif_cents),
                    ilr_freight_cents = COALESCE(excluded.ilr_freight_cents, spot_forward_manual.ilr_freight_cents),
                    chi_eth_cents = COALESCE(excluded.chi_eth_cents, spot_forward_manual.chi_eth_cents),
                    ny_eth_cents = COALESCE(excluded.ny_eth_cents, spot_forward_manual.ny_eth_cents)
            """, (date, corn_cif, bean_cif, ilr_freight, chi_eth, ny_eth))
        conn.commit()
        return True
    finally:
        conn.close()


def get_spot_forward_manual(date: str) -> dict:
    """Get manual spot/forward entries for a date. Returns {date, corn_cif_cents, ...}."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"""
            SELECT date, corn_cif_cents, bean_cif_cents, ilr_freight_cents, chi_eth_cents, ny_eth_cents
            FROM spot_forward_manual WHERE date={ph}
        """, (date,))
        row = c.fetchone()
        if row:
            return dict(row)
        return {"date": date, "corn_cif_cents": None, "bean_cif_cents": None,
                "ilr_freight_cents": None, "chi_eth_cents": None, "ny_eth_cents": None}
    finally:
        conn.close()


def get_spot_forward_manual_history(days: int = 30) -> list[dict]:
    """Get manual spot/forward entries for the last N days, ordered by date desc."""
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        c.execute(f"""
            SELECT date, corn_cif_cents, bean_cif_cents, ilr_freight_cents, chi_eth_cents, ny_eth_cents
            FROM spot_forward_manual WHERE date >= {ph}
            ORDER BY date DESC
        """, (cutoff,))
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


# ── Nightly Recap per-row overrides (manual edits to any table row) ──────────────
_NIGHTLY_OVERRIDE_DDL = """
    CREATE TABLE IF NOT EXISTS nightly_override (
        date      TEXT NOT NULL,
        item_name TEXT NOT NULL,
        spot      INTEGER,
        nxt       INTEGER,
        spot_chg  INTEGER,
        nxt_chg   INTEGER,
        fut       TEXT,
        PRIMARY KEY (date, item_name)
    )
"""


def _ensure_nightly_override(conn, c) -> None:
    c.execute(_NIGHTLY_OVERRIDE_DDL)
    conn.commit()


def set_nightly_overrides(date: str, rows: list[dict]) -> bool:
    """Replace ALL Nightly Recap overrides for `date`. Each row:
    {item_name, spot, nxt, spot_chg, nxt_chg, fut}; any field None = 'no
    override for that field, use the computed value'. Rows with every field
    None are skipped (nothing to override)."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        _ensure_nightly_override(conn, c)
        c.execute(f"DELETE FROM nightly_override WHERE date={ph}", (date,))
        for r in rows:
            vals = (r.get("spot"), r.get("nxt"), r.get("spot_chg"),
                    r.get("nxt_chg"), r.get("fut"))
            if all(v is None for v in vals):
                continue
            c.execute(
                f"INSERT INTO nightly_override "
                f"(date, item_name, spot, nxt, spot_chg, nxt_chg, fut) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (date, r["item_name"], *vals))
        conn.commit()
        return True
    finally:
        conn.close()


def get_nightly_overrides(date: str) -> dict:
    """Return {item_name: {spot, nxt, spot_chg, nxt_chg, fut}} for `date`
    (each field None means no override for that field)."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        _ensure_nightly_override(conn, c)
        c.execute(
            f"SELECT item_name, spot, nxt, spot_chg, nxt_chg, fut "
            f"FROM nightly_override WHERE date={ph}", (date,))
        out = {}
        for row in c.fetchall():
            d = dict(row)
            out[d["item_name"]] = {k: d[k] for k in
                                   ("spot", "nxt", "spot_chg", "nxt_chg", "fut")}
        return out
    finally:
        conn.close()


def _ensure_index_excludes(conn, c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS index_excludes (
        provider TEXT NOT NULL,
        location TEXT NOT NULL,
        PRIMARY KEY (provider, location))""")


def get_index_excludes() -> set:
    """Set of (provider, location) locations flagged as outliers — excluded from
    the Trends region/segment index averages (but still scraped & shown elsewhere)."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        _ensure_index_excludes(conn, c)
        c.execute("SELECT provider, location FROM index_excludes")
        return {(r["provider"], r["location"]) for r in c.fetchall()}
    finally:
        conn.close()


def set_index_excludes(pairs) -> bool:
    """Replace the whole outlier set with `pairs` (iterable of (provider, location))."""
    conn = get_conn()
    c    = conn.cursor()
    ph   = _ph()
    try:
        _ensure_index_excludes(conn, c)
        c.execute("DELETE FROM index_excludes")
        for prov, loc in pairs:
            c.execute(f"INSERT INTO index_excludes (provider, location) "
                      f"VALUES ({ph}, {ph})", (prov, loc))
        conn.commit()
        return True
    finally:
        conn.close()


# ── Client basis-report subscriptions (personalized daily/weekly/monthly emails) ──
def _ensure_client_reports(conn, c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS client_reports (
        id           TEXT PRIMARY KEY,
        client_name  TEXT NOT NULL,
        email        TEXT NOT NULL,
        cc           TEXT,
        frequency    TEXT NOT NULL,          -- 'daily' | 'weekly' | 'monthly'
        day_of_week  INTEGER,                -- 0=Mon..6=Sun (weekly); else NULL
        locations    TEXT NOT NULL,          -- JSON [{"provider","location"}]
        depth        TEXT DEFAULT 'curve',   -- 'curve' (full forward) | 'spot'
        commodities  TEXT DEFAULT '[]',      -- JSON grain list; [] = all commodities
        active       INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT)""")
    # Migrate tables created before depth/commodities existed.
    for _col, _decl in (("depth", "TEXT DEFAULT 'curve'"),
                        ("commodities", "TEXT DEFAULT '[]'")):
        try:
            if _use_pg():
                c.execute(f"ALTER TABLE client_reports ADD COLUMN IF NOT EXISTS {_col} {_decl}")
            else:
                c.execute(f"ALTER TABLE client_reports ADD COLUMN {_col} {_decl}")
        except Exception:
            conn.rollback()          # column already present (SQLite raises)
    conn.commit()


def get_client_reports(active_only: bool = False) -> list[dict]:
    """All client report subscriptions; `locations` parsed from JSON to a list."""
    import json as _json
    conn = get_conn(); c = conn.cursor()
    try:
        _ensure_client_reports(conn, c)
        c.execute("SELECT id, client_name, email, cc, frequency, day_of_week, "
                  "locations, depth, commodities, active, created_at FROM client_reports"
                  + (" WHERE active=1" if active_only else "")
                  + " ORDER BY client_name")
        out = []
        for r in c.fetchall():
            d = dict(r)
            try:
                d["locations"] = _json.loads(d["locations"] or "[]")
            except Exception:
                d["locations"] = []
            try:
                d["commodities"] = _json.loads(d.get("commodities") or "[]")
            except Exception:
                d["commodities"] = []
            d["depth"] = d.get("depth") or "curve"
            d["active"] = bool(d["active"])
            out.append(d)
        return out
    finally:
        conn.close()


def upsert_client_report(rec: dict) -> bool:
    """Insert or update one subscription by `id`. rec: {id, client_name, email, cc,
    frequency, day_of_week, locations(list of dicts), active, created_at}."""
    import json as _json
    conn = get_conn(); c = conn.cursor()
    ph = _ph()
    try:
        _ensure_client_reports(conn, c)
        locs = _json.dumps(rec.get("locations") or [])
        coms = _json.dumps(rec.get("commodities") or [])
        depth = (rec.get("depth") or "curve")
        vals = (rec["id"], rec["client_name"], rec["email"], rec.get("cc"),
                rec["frequency"], rec.get("day_of_week"), locs, depth, coms,
                1 if rec.get("active", True) else 0, rec.get("created_at"))
        if _use_sf():
            _merge(c, "client_reports",
                   ["id", "client_name", "email", "cc", "frequency", "day_of_week",
                    "locations", "depth", "commodities", "active", "created_at"],
                   vals, ["id"],
                   update_cols=["client_name", "email", "cc", "frequency", "day_of_week",
                                "locations", "depth", "commodities", "active"])
        elif _use_pg():
            c.execute(f"""INSERT INTO client_reports
                (id, client_name, email, cc, frequency, day_of_week, locations, depth,
                 commodities, active, created_at)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
                ON CONFLICT (id) DO UPDATE SET
                    client_name=EXCLUDED.client_name, email=EXCLUDED.email, cc=EXCLUDED.cc,
                    frequency=EXCLUDED.frequency, day_of_week=EXCLUDED.day_of_week,
                    locations=EXCLUDED.locations, depth=EXCLUDED.depth,
                    commodities=EXCLUDED.commodities, active=EXCLUDED.active""", vals)
        else:
            c.execute(f"""INSERT INTO client_reports
                (id, client_name, email, cc, frequency, day_of_week, locations, depth,
                 commodities, active, created_at)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
                ON CONFLICT(id) DO UPDATE SET
                    client_name=excluded.client_name, email=excluded.email, cc=excluded.cc,
                    frequency=excluded.frequency, day_of_week=excluded.day_of_week,
                    locations=excluded.locations, depth=excluded.depth,
                    commodities=excluded.commodities, active=excluded.active""", vals)
        conn.commit()
        return True
    finally:
        conn.close()


def delete_client_report(report_id: str) -> bool:
    conn = get_conn(); c = conn.cursor()
    ph = _ph()
    try:
        _ensure_client_reports(conn, c)
        c.execute(f"DELETE FROM client_reports WHERE id={ph}", (report_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def get_location_grain_options() -> list[tuple]:
    """Distinct (provider, location, grain) combos that have basis data — the pool
    a client picks their report locations from. Sorted for a stable picker."""
    conn = get_conn(); c = conn.cursor()
    try:
        c.execute("""SELECT DISTINCT s.provider, s.location, sr.grain
                     FROM snapshot_rows sr JOIN snapshots s ON s.id = sr.snapshot_id
                     WHERE sr.basis_cents IS NOT NULL""")
        return sorted((r["provider"], r["location"], r["grain"]) for r in c.fetchall())
    finally:
        conn.close()


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
    # NOTE: tiers key on the snapshot's DATA date (`timestamp`), NOT `created_at`.
    # Using created_at was catastrophic: the DJ archive + jsa_history were bulk-loaded
    # in one week, so every historical date shared a single created_at-week and Tier 2
    # collapsed YEARS of history to one snapshot per location. Curated archival sources
    # are also exempted entirely — they are hand-built, not high-frequency scrapes.
    _ARCHIVE = "('dow_jones', 'jsa_history', 'historical')"
    _TS = "(substr(timestamp, 1, 10))::date"
    _KEEPERS_SQL = f"""
        -- Archival sources: keep EVERYTHING, forever
        SELECT id FROM snapshots WHERE source IN {_ARCHIVE}

        UNION

        -- Tier 1: current data-month — keep everything (daily granularity)
        SELECT id FROM snapshots
        WHERE {_TS} >= DATE_TRUNC('month', NOW())::date
          AND source NOT IN {_ARCHIVE}

        UNION

        -- Tier 2: older scraped data — one (most recent) per provider/location/data-week
        SELECT id FROM (
            SELECT DISTINCT ON (provider, location, DATE_TRUNC('week', {_TS}))
                id
            FROM snapshots
            WHERE {_TS} < DATE_TRUNC('month', NOW())::date
              AND source NOT IN {_ARCHIVE}
            ORDER BY provider, location, DATE_TRUNC('week', {_TS}), timestamp DESC
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
