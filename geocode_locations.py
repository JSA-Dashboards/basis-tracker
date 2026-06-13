"""
geocode_locations.py — Populate lat/lon for all locations in location_meta.

Uses Nominatim (OpenStreetMap, free, no API key).  Rate-limited to 1 req/sec
per OSM ToS.  Skips locations that already have coordinates.  Safe to re-run.

Usage:
    python geocode_locations.py            # geocode everything missing coords
    python geocode_locations.py --reset    # clear all coords and re-geocode
    python geocode_locations.py --dry-run  # show what would be queried, no writes
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

sys.path.insert(0, str(Path(__file__).parent))
from database import init_db, get_conn, _use_pg

log = logging.getLogger(__name__)

# One user-agent string required by Nominatim ToS
_UA     = "basis-tracker-geocoder/1.0 (kpostin@jpsi.com)"
_DELAY       = 1.2   # seconds between requests (Nominatim ToS: max 1/sec)
_BACKOFF_429 = 15.0  # extra sleep after a 429 response

# ── Name-cleaning patterns ─────────────────────────────────────────────────────

# Provider prefixes to strip before geocoding
_PROV_PREFIXES = re.compile(
    r"^(ADM|CGB\s*[-–]?|CGB\s+|CHS\s+|GPRE\s+|AGP\s*[-–]?\s*|POET\s+|"
    r"Cargill\s+|Andersons?\s+|Bunge\s+|Scoular\s+|LDC\s+)",
    re.IGNORECASE,
)

# Parenthetical suffixes to strip: "(Corn Processing)", "(Elevator)", etc.
_PAREN_SUFFIX  = re.compile(r"\s*\(.*?\)\s*$")

# Trailing qualifiers after commas or dashes: "East", "West", "#1", etc.
_QUALIFIER_RE  = re.compile(
    r"\s*[-–,]\s*(East|West|North|South|East\s+\d+|#\d+|GOS|TRKY|NE|OH|IN)\s*$",
    re.IGNORECASE,
)

# Facility-type words to strip from end of name
_FACILITY_WORDS = re.compile(
    r"\s+(?:Shuttle|Terminal|Mill|Elevator|Bunker|Container\s+Mkt|"
    r"Cash\s+Bids?|Mkt|Marketstreet|LLC|Inc\.?|L\.L\.C\.?|"
    r"Processing|Plant|Elev|Elev\.|Crush|NG)\s*$",
    re.IGNORECASE,
)

# Scoular "Scoular XYZ Cash Bids" pattern
_SCOULAR_CASH_BIDS = re.compile(r"^Scoular\s+(.+?)\s+Cash\s+Bids?\s*$", re.IGNORECASE)

# "City/City2" — take first city
_SLASH_CITY = re.compile(r"^(.+?)\s*/\s*.+$")

# "City, ST" pattern — extract both
_CITY_STATE_RE = re.compile(r"^(.+?),\s*([A-Z]{2})\s*$")

# Canadian province codes — skip geocoding these
_CA_PROVINCES  = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}

# State overrides for providers whose metadata has no state column populated
_PROVIDER_STATE: dict[tuple[str, str], str] = {
    # GPRE (Green Plains Renewable Energy)
    ("GPRE", "Wood River"):   "NE",
    ("GPRE", "York"):         "NE",
    ("GPRE", "Superior"):     "NE",
    ("GPRE", "Central City"): "NE",
    ("GPRE", "Madison"):      "NE",
    ("GPRE", "Shenandoah"):   "IA",
    ("GPRE", "Otter Tail"):   "MN",
    ("GPRE", "Mount Vernon"): "IN",
    # Mendota (IL-area ADM/JPSI locations)
    ("Mendota", "Burlington"):      "IA",
    ("Mendota", "Havana"):          "IL",
    ("Mendota", "Ottawa"):          "IL",
    ("Mendota", "Clinton Proc"):    "IA",
    ("Mendota", "Mendota BN Rail"): "IL",
    ("Mendota", "Mendota Mill"):    "IL",
}


def _parse_city_state(location: str, meta_state: str) -> tuple[str, str] | None:
    """
    Return (city, state_code) to geocode, or None to skip.

    Strategy:
      1. Strip provider prefix and paren suffix from location name.
      2. If cleaned name has ", ST" pattern, extract city and state.
      3. Otherwise use city = cleaned name and state = meta_state.
      4. Skip Canadian provinces.
    """
    name = location.strip()

    # Scoular "Scoular Adrian Cash Bids" → "Adrian"
    m = _SCOULAR_CASH_BIDS.match(name)
    if m:
        name = m.group(1).strip()

    # Strip provider prefix
    name = _PROV_PREFIXES.sub("", name).strip()

    # Strip parenthetical suffixes
    name = _PAREN_SUFFIX.sub("", name).strip()

    # Try "City, ST" or "City, ST - suffix" anywhere in name (captures state early)
    m = re.search(r'^(.+?),\s*([A-Z]{2})\b', name)
    if m:
        city  = m.group(1).strip()
        state = m.group(2).upper()
    else:
        # "City/City2" → take first
        slash_m = _SLASH_CITY.match(name)
        if slash_m:
            name = slash_m.group(1).strip()

        # Strip trailing facility-type words repeatedly
        prev = None
        while prev != name:
            prev = name
            name = _FACILITY_WORDS.sub("", name).strip()

        # Strip trailing qualifiers (East, West, #2, etc.)
        city  = _QUALIFIER_RE.sub("", name).strip()
        state = (meta_state or "").strip().upper()

        # "City - Street/qualifier" → strip everything after dash
        if " - " in city and not re.search(r',\s*[A-Z]{2}$', city):
            city = city.split(" - ")[0].strip()

    if not city:
        return None

    # Skip Canada
    if state in _CA_PROVINCES:
        return None

    return city, state


def _geocode_one(geocoder: Nominatim, city: str, state: str, retries: int = 2) -> tuple[float, float] | None:
    """Return (lat, lon) or None if not found."""
    query = f"{city}, {state}, USA" if state else f"{city}, USA"
    for attempt in range(retries + 1):
        try:
            result = geocoder.geocode(query, timeout=10)
            if result:
                return round(result.latitude, 6), round(result.longitude, 6)
            # Try without state if first attempt fails
            if state and attempt == 0:
                result = geocoder.geocode(f"{city}, USA", timeout=10)
                if result:
                    return round(result.latitude, 6), round(result.longitude, 6)
            return None
        except GeocoderTimedOut:
            if attempt < retries:
                time.sleep(2)
            continue
        except GeocoderServiceError as exc:
            msg = str(exc)
            if "429" in msg:
                log.warning("Rate-limited (429) for %r — sleeping %.0fs", query, _BACKOFF_429)
                time.sleep(_BACKOFF_429)
                if attempt < retries:
                    continue
            else:
                log.warning("Geocoder error for %r: %s", query, exc)
            return None
    return None


def _clear_coords():
    conn = get_conn()
    c    = conn.cursor()
    try:
        c.execute("UPDATE location_meta SET lat = NULL, lon = NULL")
        conn.commit()
        log.info("Cleared all coordinates.")
    finally:
        conn.close()


def _load_pending(skip_existing: bool) -> list[dict]:
    conn = get_conn()
    c    = conn.cursor()
    try:
        if skip_existing:
            c.execute(
                "SELECT provider, location, state FROM location_meta "
                "WHERE lat IS NULL OR lon IS NULL "
                "ORDER BY provider, location"
            )
        else:
            c.execute(
                "SELECT provider, location, state FROM location_meta "
                "ORDER BY provider, location"
            )
        return [
            {"provider": r["provider"], "location": r["location"], "state": r["state"] or ""}
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def _write_coords(provider: str, location: str, lat: float, lon: float):
    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    try:
        c.execute(
            f"UPDATE location_meta SET lat={ph}, lon={ph} WHERE provider={ph} AND location={ph}",
            (lat, lon, provider, location),
        )
        conn.commit()
    finally:
        conn.close()


def _bootstrap_missing():
    """
    Create bare location_meta rows (no state, no coords) for any (provider, location)
    that has snapshots but is not yet in location_meta.  Safe to re-run.
    """
    conn = get_conn()
    c    = conn.cursor()
    ph   = "%s" if _use_pg() else "?"
    try:
        c.execute("""
            SELECT DISTINCT s.provider, s.location
            FROM snapshots s
            WHERE NOT EXISTS (
                SELECT 1 FROM location_meta lm
                WHERE lm.provider = s.provider AND lm.location = s.location
            )
            ORDER BY s.provider, s.location
        """)
        missing = c.fetchall()
        if not missing:
            log.info("Bootstrap: nothing to add")
            return
        log.info("Bootstrap: inserting %d missing location_meta rows", len(missing))
        if _use_pg():
            for row in missing:
                c.execute(
                    "INSERT INTO location_meta (provider, location) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (row["provider"], row["location"]),
                )
        else:
            for row in missing:
                c.execute(
                    "INSERT OR IGNORE INTO location_meta (provider, location) VALUES (?, ?)",
                    (row["provider"], row["location"]),
                )
        conn.commit()
    finally:
        conn.close()


def run(reset: bool = False, dry_run: bool = False, bootstrap: bool = False):
    init_db()

    if bootstrap and not dry_run:
        _bootstrap_missing()

    if reset and not dry_run:
        _clear_coords()

    pending = _load_pending(skip_existing=not reset)
    log.info("%d location(s) to geocode", len(pending))

    if not pending:
        log.info("Nothing to do.")
        return

    geocoder = Nominatim(user_agent=_UA)
    ok = skip = fail = 0

    for row in pending:
        provider = row["provider"]
        location = row["location"]
        state    = row["state"]

        parsed = _parse_city_state(location, state)
        if parsed is None:
            log.debug("  SKIP  %-10s  %s  (Canadian or unparseable)", provider, location)
            skip += 1
            continue

        city, st = parsed

        # Apply provider-level state override when metadata has no state
        if not st:
            override = _PROVIDER_STATE.get((provider, city)) or _PROVIDER_STATE.get((provider, location))
            if override:
                st = override
        if dry_run:
            query = f"{city}, {st}, USA" if st else f"{city}, USA"
            print(f"  WOULD  {provider:10s}  {location:45s}  ->  {query}")
            continue

        coords = _geocode_one(geocoder, city, st)
        if coords:
            lat, lon = coords
            _write_coords(provider, location, lat, lon)
            log.info("  OK    %-10s  %-45s  %.4f, %.4f", provider, location, lat, lon)
            ok += 1
        else:
            log.warning("  FAIL  %-10s  %s  (city=%r, state=%r)", provider, location, city, st)
            fail += 1

        time.sleep(_DELAY)

    log.info("Done. ok=%d  skip=%d  fail=%d", ok, skip, fail)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    ap = argparse.ArgumentParser(description="Geocode location_meta rows using Nominatim")
    ap.add_argument("--reset",     action="store_true", help="Clear all coords and re-geocode everything")
    ap.add_argument("--dry-run",   action="store_true", help="Print queries without writing")
    ap.add_argument("--bootstrap", action="store_true", help="Create location_meta rows for providers not yet registered")
    args = ap.parse_args()

    run(reset=args.reset, dry_run=args.dry_run, bootstrap=args.bootstrap)
