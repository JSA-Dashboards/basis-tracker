"""Read layer for the River FOB archive (cif_history / freight_history /
calendar_history).

River data lives in the portal's standalone Snowflake database RIVER_FOB.PUBLIC,
read cross-database on the basis tracker's main Snowflake connection. (The old
dedicated-Supabase path via RIVER_DATABASE_URL was retired 2026-09 once the portal
moved to Snowflake — that secret is now ignored.) On a non-Snowflake backend the
reads hit the main connection's own river tables, which is a stale fallback. The
portal remains the place data is entered/saved.
"""
from database import get_conn


def _river_conn():
    """River reads/writes go through the basis tracker's main connection —
    Snowflake, where the portal's RIVER_FOB.PUBLIC archive lives."""
    return get_conn()


def _ph() -> str:
    # Follow the main backend's placeholder (Snowflake & Postgres = %s, SQLite = ?).
    from database import _ph as _db_ph
    return _db_ph()


def _tbl(name: str) -> str:
    """Qualify a River FOB table. On Snowflake the portal's archive is a standalone
    RIVER_FOB.PUBLIC database, read cross-database; other backends read the main
    connection's own (unqualified) river tables."""
    try:
        from database import _use_sf
        if _use_sf():
            return f"RIVER_FOB.PUBLIC.{name}"
    except Exception:
        pass
    return name


def using_fallback() -> bool:
    """True only when reads fall back to the main (non-Snowflake) DB's own river
    tables — stale. On Snowflake we read the current RIVER_FOB.PUBLIC archive."""
    try:
        from database import _use_sf
        return not _use_sf()
    except Exception:
        return True


def list_dates() -> list:
    """All archived as-of dates, newest first."""
    conn = _river_conn()
    c = conn.cursor()
    try:
        c.execute(f"""SELECT as_of FROM {_tbl('cif_history')}
                     UNION SELECT as_of FROM {_tbl('freight_history')}
                     ORDER BY as_of DESC""")
        return [r["as_of"] for r in c.fetchall()]
    finally:
        conn.close()


def load_snapshot(as_of: str):
    """Return (cif_by_commodity, freight_by_region, calendar) for a date, or
    (None, None, None) if absent.  calendar: {commodity: [(month, contract)…]}."""
    ph = _ph()
    conn = _river_conn()
    c = conn.cursor()
    try:
        c.execute(f"SELECT commodity, month, value FROM {_tbl('cif_history')} WHERE as_of={ph}", (as_of,))
        cif = {}
        for r in c.fetchall():
            cif.setdefault(r["commodity"], {})[r["month"]] = r["value"]
        c.execute(f"SELECT region, month, value FROM {_tbl('freight_history')} WHERE as_of={ph}", (as_of,))
        frt = {}
        for r in c.fetchall():
            frt.setdefault(r["region"], {})[r["month"]] = r["value"]
        c.execute(f"SELECT commodity, seq, month, contract FROM {_tbl('calendar_history')} "
                  f"WHERE as_of={ph} ORDER BY commodity, seq", (as_of,))
        cal = {}
        for r in c.fetchall():
            cal.setdefault(r["commodity"], []).append((r["month"], r["contract"]))
        if not cif and not frt:
            return None, None, None
        return cif, frt, cal
    finally:
        conn.close()


def latest_date() -> str | None:
    ds = list_dates()
    return ds[0] if ds else None


def _f(v):
    try:
        if v is None:
            return None
        import math
        v = float(v)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def save_snapshot(as_of, cif_by_commodity, freight_by_region, calendar=None):
    """Upsert one day's River FOB inputs into the shared archive (replaces the
    date's rows). Used by the on-demand 'Update from the FOB sheet' control.
    Returns (n_cif, n_freight)."""
    from datetime import datetime, timezone
    ph = _ph()
    conn = _river_conn()
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    try:
        for t in ("cif_history", "freight_history", "calendar_history"):
            c.execute(f"DELETE FROM {_tbl(t)} WHERE as_of={ph}", (as_of,))
        cif_rows = [(as_of, com, m, _f(v))
                    for com, mv in cif_by_commodity.items()
                    for m, v in mv.items() if _f(v) is not None]
        frt_rows = [(as_of, r, m, _f(v))
                    for r, mv in freight_by_region.items()
                    for m, v in mv.items() if _f(v) is not None]
        cal_rows = [(as_of, com, i, m, ct)
                    for com, cols in (calendar or {}).items()
                    for i, (m, ct) in enumerate(cols)]
        if cif_rows:
            c.executemany(f"INSERT INTO {_tbl('cif_history')} VALUES ({ph},{ph},{ph},{ph})", cif_rows)
        if frt_rows:
            c.executemany(f"INSERT INTO {_tbl('freight_history')} VALUES ({ph},{ph},{ph},{ph})", frt_rows)
        if cal_rows:
            c.executemany(
                f"INSERT INTO {_tbl('calendar_history')} VALUES ({ph},{ph},{ph},{ph},{ph})", cal_rows)
        conn.commit()
        return len(cif_rows), len(frt_rows)
    finally:
        conn.close()
