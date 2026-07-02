"""Read layer for the River FOB archive (cif_history / freight_history /
calendar_history), sharing the basis tracker's DB connection so the River FOB
tab reads the same Supabase the rest of the dashboard uses.

Mirrors river-fob-portal/db.py's read functions; the River FOB portal remains the
place data is entered/saved.  Read-only here.
"""
from database import get_conn, _use_pg


def _ph() -> str:
    return "%s" if _use_pg() else "?"


def list_dates() -> list:
    """All archived as-of dates, newest first."""
    conn = get_conn()
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
    conn = get_conn()
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
