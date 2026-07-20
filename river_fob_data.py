"""Read layer for the River FOB archive (cif_history / freight_history /
calendar_history).

River data lives in its OWN Supabase now (the portal writes there). If
RIVER_DATABASE_URL is set we read/write that dedicated DB; otherwise we fall
back to the basis tracker's main connection (the old shared DB) for backward
compatibility. The River FOB portal remains the place data is entered/saved.
"""
import os
from database import get_conn, _use_pg


def _river_url() -> str:
    return os.environ.get("RIVER_DATABASE_URL", "").strip()


def _river_conn():
    """Connection to the dedicated river DB (RIVER_DATABASE_URL) if configured,
    else the basis tracker's main connection."""
    url = _river_url()
    if url:
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    return get_conn()


def _ph() -> str:
    return "%s" if (_river_url() or _use_pg()) else "?"


def using_fallback() -> bool:
    """True when RIVER_DATABASE_URL is NOT configured, so reads fall back to the
    basis tracker's main DB. That fallback froze once the portal switched to the
    dedicated river DB, so a True here means the River FOB data is likely stale.
    The app surfaces this as a visible banner rather than serving silent staleness."""
    return not _river_url()


def list_dates() -> list:
    """All archived as-of dates, newest first."""
    conn = _river_conn()
    c = conn.cursor()
    try:
        c.execute("""SELECT as_of FROM cif_history
                     UNION SELECT as_of FROM freight_history
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
        c.execute(f"SELECT commodity, month, value FROM cif_history WHERE as_of={ph}", (as_of,))
        cif = {}
        for r in c.fetchall():
            cif.setdefault(r["commodity"], {})[r["month"]] = r["value"]
        c.execute(f"SELECT region, month, value FROM freight_history WHERE as_of={ph}", (as_of,))
        frt = {}
        for r in c.fetchall():
            frt.setdefault(r["region"], {})[r["month"]] = r["value"]
        c.execute(f"SELECT commodity, seq, month, contract FROM calendar_history "
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
            c.execute(f"DELETE FROM {t} WHERE as_of={ph}", (as_of,))
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
            c.executemany(f"INSERT INTO cif_history VALUES ({ph},{ph},{ph},{ph})", cif_rows)
        if frt_rows:
            c.executemany(f"INSERT INTO freight_history VALUES ({ph},{ph},{ph},{ph})", frt_rows)
        if cal_rows:
            c.executemany(
                f"INSERT INTO calendar_history VALUES ({ph},{ph},{ph},{ph},{ph})", cal_rows)
        conn.commit()
        return len(cif_rows), len(frt_rows)
    finally:
        conn.close()
