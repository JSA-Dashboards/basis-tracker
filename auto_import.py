"""
Basis Tracker — Automated daily importer.

Run this script daily (via Windows Task Scheduler) to scrape the latest web bids
for ADM Gradable, POET, CHS, CGB Grain, Cargill, GPRE, The Andersons, Bunge,
Scoular, AGP (Ag Processing Inc), LDC (Louis Dreyfus Company), Tyson (LGS),
and GPC / Kent Commodities.

Usage
-----
  python auto_import.py                   # all web scrapers
  python auto_import.py --no-poet         # skip POET scrape
  python auto_import.py --poet-only       # POET scrape only
  python auto_import.py --no-chs          # skip CHS scrape
  python auto_import.py --chs-only        # CHS scrape only
  python auto_import.py --no-cgb          # skip CGB scrape
  python auto_import.py --cgb-only        # CGB scrape only
  python auto_import.py --no-cargill      # skip Cargill scrape
  python auto_import.py --cargill-only    # Cargill scrape only
  python auto_import.py --no-gpre         # skip GPRE scrape
  python auto_import.py --gpre-only       # GPRE scrape only
  python auto_import.py --no-andersons    # skip The Andersons scrape
  python auto_import.py --andersons-only  # The Andersons scrape only
  python auto_import.py --no-bunge        # skip Bunge scrape
  python auto_import.py --bunge-only      # Bunge scrape only
  python auto_import.py --no-scoular      # skip Scoular scrape
  python auto_import.py --scoular-only    # Scoular scrape only
  python auto_import.py --no-agp          # skip AGP scrape
  python auto_import.py --agp-only        # AGP scrape only
  python auto_import.py --no-ldc          # skip LDC scrape
  python auto_import.py --ldc-only        # LDC scrape only
  python auto_import.py --no-tyson        # skip Tyson LGS scrape
  python auto_import.py --tyson-only      # Tyson LGS scrape only
  python auto_import.py --no-gpc          # skip GPC / Kent scrape
  python auto_import.py --gpc-only        # GPC / Kent scrape only
  python auto_import.py --no-prune        # skip automatic Monday pruning
  python auto_import.py --prune-only      # run data retention pruning only

Prerequisites
-------------
  playwright install chrome  (or: playwright install --with-deps chromium)
  must have been run so Playwright can find Chrome for the POET scrape.

Results are logged to auto_import.log in this directory.
"""
import argparse
import sys
import os
import logging
from pathlib import Path
from datetime import datetime, timezone

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()

from database import (
    init_db, upsert_snapshot,
    upsert_location_meta, prune_old_snapshots,
)
from poet_scraper import fetch_poet_bids
from parsers.poet_parser import parse_instruments as parse_poet_instruments
from chs_scraper import fetch_chs_bids, CHS_ILLINOIS_IDS
from parsers.chs_parser import parse_bids_response as parse_chs_bids
from adm_scraper import fetch_adm_bids
from parsers.adm_parser import parse_instruments as parse_adm_instruments
from cgb_scraper import fetch_cgb_bids
from parsers.cgb_parser import parse_cgb_location
from cargill_scraper import fetch_cargill_bids
from parsers.cargill_parser import parse_cargill_location
from gpre_scraper import fetch_gpre_bids
from parsers.gpre_parser import parse_gpre_location
from andersons_scraper import fetch_andersons_bids
from parsers.andersons_parser import parse_andersons_location
from bunge_scraper import fetch_bunge_bids
from parsers.bunge_parser import parse_bunge_location
from scoular_scraper import fetch_scoular_bids
from parsers.scoular_parser import parse_scoular_location
from agp_scraper import fetch_agp_bids
from parsers.agp_parser import parse_agp_location
from ldc_scraper import fetch_ldc_bids
from parsers.ldc_parser import parse_ldc_location
from tyson_scraper import fetch_tyson_bids
from parsers.tyson_parser import parse_tyson_location
from gpc_scraper import fetch_gpc_bids
from parsers.gpc_parser import parse_gpc_location
from zfs_scraper import fetch_zfs_bids
from parsers.zfs_parser import parse_zfs_location
from mnsoy_scraper import fetch_mnsoy_bids
from parsers.mnsoy_parser import parse_mnsoy_location
from platinum_scraper import fetch_platinum_bids
from parsers.platinum_parser import parse_platinum_location
from shellrock_scraper import fetch_shellrock_bids
from parsers.shellrock_parser import parse_shellrock_location
from whiteriver_scraper import fetch_whiteriver_bids
from parsers.whiteriver_parser import parse_whiteriver_location
from hppsd_scraper import fetch_hppsd_bids
from parsers.hppsd_parser import parse_hppsd_location
from bartlett_scraper import fetch_bartlett_bids
from parsers.bartlett_parser import parse_bartlett_location
from primient_scraper import fetch_primient_bids
from parsers.primient_parser import parse_primient_location
from norfolkcrush_scraper import fetch_norfolkcrush_bids
from parsers.norfolkcrush_parser import parse_norfolkcrush_location
from ndsp_scraper import fetch_ndsp_bids
from parsers.ndsp_parser import parse_ndsp_location
from sdsp_scraper import fetch_sdsp_bids
from parsers.sdsp_parser import parse_sdsp_location
from adm_names import adm_state_from_name
import holidays as _holidays

# ── Config ────────────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "auto_import.log"

# Force UTF-8 on the stdout stream so Unicode chars (✓ ⚠ etc.)
# don't raise UnicodeEncodeError on Windows (cp1252 default).
import io as _io
_utf8_stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(_utf8_stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Trading-day guard ──────────────────────────────────────────────────────────

def _is_trading_day(dt: datetime = None) -> bool:
    """
    Return True if dt (today by default) is a US federal trading day:
      - Monday through Friday
      - Not a US federal holiday
    """
    if dt is None:
        dt = datetime.now()
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    us_fed = _holidays.US(years=dt.year)
    return dt.date() not in us_fed


def run_chs() -> int:
    """
    Fetch CHS Illinois bids via the Bushel API and upsert snapshots.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("CHS Illinois scrape starting…")
    log.info("=" * 60)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    try:
        raw = fetch_chs_bids()
    except Exception as exc:
        log.error("CHS fetch failed: %s", exc)
        return 0

    if not raw:
        log.warning("CHS scrape returned no data.")
        return 0

    try:
        snapshots = parse_chs_bids(raw, set(), timestamp)  # empty = all locations
    except Exception as exc:
        log.error("CHS parse failed: %s", exc)
        return 0

    if not snapshots:
        log.warning("CHS parser produced no snapshots.")
        return 0

    total_rows = 0
    errors     = 0
    for snap in snapshots:
        try:
            upsert_snapshot(snap.model_dump())
            total_rows += len(snap.rows)
            log.info(
                "  ✓  %-28s  %-10s  %d row(s)",
                snap.location, snap.rows[0].grain if snap.rows else "", len(snap.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s / %s: %s",
                      snap.location,
                      snap.rows[0].grain if snap.rows else "?",
                      exc)

    log.info("-" * 60)
    log.info(
        "CHS done: %d snapshot(s)  |  %d row(s) total  |  %d error(s)",
        len(snapshots) - errors, total_rows, errors,
    )
    return total_rows


def run_adm() -> int:
    """
    Scrape ADM Gradable (all 151 locations) and upsert bid snapshots.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("ADM Gradable scrape starting…")
    log.info("=" * 60)

    try:
        raw_results = fetch_adm_bids()
    except Exception as exc:
        log.error("ADM scrape failed: %s", exc)
        return 0

    if not raw_results:
        log.warning("ADM scrape returned no results.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    # ADM locations to drop (e.g. Canadian sites priced in CAD → bogus basis).
    _ADM_SKIP = {"Windsor, ON"}

    for item in raw_results:
        market_id        = item["market_id"]
        display_name     = item["display_name"]
        instruments_data = item["instruments_data"]
        timestamp        = item["timestamp"]

        if display_name in _ADM_SKIP:
            skipped += 1
            continue

        if not instruments_data.get("instruments"):
            skipped += 1
            continue

        try:
            snap_req = parse_adm_instruments(
                market_id, display_name, instruments_data, timestamp
            )
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            # Persist state parsed from the ADM name (e.g. "Decatur, IL …" → IL)
            _adm_state = adm_state_from_name(snap_req.location)
            if _adm_state:
                upsert_location_meta("ADM", snap_req.location, state=_adm_state)
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info("  ✓  %-45s  %d row(s)", display_name, len(snap_req.rows))

        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", display_name, exc)

    log.info("-" * 60)
    log.info(
        "ADM done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_cgb() -> int:
    """
    Scrape CGB Grain (agricharts.com) for all 86 locations and upsert bids.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("CGB Grain scrape starting…")
    log.info("=" * 60)

    try:
        raw_locations = fetch_cgb_bids()
    except Exception as exc:
        log.error("CGB scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("CGB scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_cgb_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            # Store state & facility_type in location_meta for UI filters
            upsert_location_meta(
                "CGB",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = loc.get("facility_type") or None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-45s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "CGB done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_cargill() -> int:
    """
    Scrape Cargill (Barchart WebSol API) for all ~81 locations and upsert bids.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("Cargill scrape starting…")
    log.info("=" * 60)

    try:
        raw_locations = fetch_cargill_bids()
    except Exception as exc:
        log.error("Cargill scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("Cargill scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_cargill_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            # Store state in location_meta for UI filters
            upsert_location_meta(
                "Cargill",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-42s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "Cargill done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_gpre() -> int:
    """
    Scrape Green Plains Inc. (GPRE) corn bids via the DTN API (single call).
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("GPRE scrape starting…")
    log.info("=" * 60)

    try:
        raw_locations = fetch_gpre_bids()
    except Exception as exc:
        log.error("GPRE scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("GPRE scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_gpre_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info("  ✓  %-25s  %d row(s)", snap_req.location, len(snap_req.rows))

        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "GPRE done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_andersons() -> int:
    """
    Scrape The Andersons (ASP.NET session-based) for all 18 locations and upsert bids.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("The Andersons scrape starting…")
    log.info("=" * 60)

    try:
        raw_locations = fetch_andersons_bids()
    except Exception as exc:
        log.error("Andersons scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("Andersons scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_andersons_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            upsert_location_meta(
                "Andersons",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-28s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "Andersons done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_bunge() -> int:
    """
    Scrape Bunge AG (static HTML pages) for all ~20 locations and upsert bids.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("Bunge scrape starting…")
    log.info("=" * 60)

    try:
        raw_locations = fetch_bunge_bids()
    except Exception as exc:
        log.error("Bunge scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("Bunge scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_bunge_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            upsert_location_meta(
                "Bunge",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-38s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "Bunge done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_scoular() -> int:
    """
    Scrape Scoular (Bushel-powered cashbidssingle pages) for all ~66 US locations
    and upsert bids.  Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("Scoular scrape starting...")
    log.info("=" * 60)

    try:
        raw_locations = fetch_scoular_bids()
    except Exception as exc:
        log.error("Scoular scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("Scoular scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_scoular_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            upsert_location_meta(
                "Scoular",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-42s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "Scoular done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_poet() -> int:
    """
    Scrape POET Gradable and upsert bid snapshots for all 36 locations.
    Returns the total number of snapshot rows imported (new + existing).
    """
    log.info("=" * 60)
    log.info("POET Gradable scrape starting…")
    log.info("=" * 60)

    try:
        raw_results = fetch_poet_bids(headless=True)
    except Exception as exc:
        log.error("POET scrape failed: %s", exc)
        return 0

    if not raw_results:
        log.warning("POET scrape returned no results.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for item in raw_results:
        market_id      = item["market_id"]
        display_name   = item["display_name"]
        instruments_data = item["instruments_data"]
        timestamp      = item["timestamp"]

        # Skip locations with no instruments
        if not instruments_data.get("instruments"):
            skipped += 1
            log.debug("SKIP  %s (no instruments)", display_name)
            continue

        try:
            snap_req = parse_poet_instruments(
                market_id, display_name, instruments_data, timestamp
            )
            if snap_req is None:
                skipped += 1
                log.debug("SKIP  %s (parser returned None)", display_name)
                continue

            upsert_snapshot(snap_req.model_dump())
            locations_done += 1
            total_rows     += len(snap_req.rows)

            row_summary = "  ".join(
                f"{r.deliveryMonth} {r.futuresSymbol} "
                f"{'+' if (r.basisCents or 0) >= 0 else ''}{r.basisCents}¢"
                for r in snap_req.rows
            )
            log.info("  ✓  %-28s  %d row(s)  %s",
                     display_name, len(snap_req.rows), row_summary)

        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", display_name, exc)

    log.info("-" * 60)
    log.info(
        "POET done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_agp() -> int:
    """
    Scrape AGP (Ag Processing Inc) for all 16 locations and upsert bids.
    Includes Soybeans, Soybean Meal ($/ton basis), and Corn where offered.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("AGP scrape starting...")
    log.info("=" * 60)

    try:
        raw_locations = fetch_agp_bids()
    except Exception as exc:
        log.error("AGP scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("AGP scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_agp_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            upsert_location_meta(
                "AGP",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-42s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "AGP done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_ldc() -> int:
    """
    Scrape LDC (Louis Dreyfus Company) for all 8 US public facilities and upsert bids.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("LDC scrape starting...")
    log.info("=" * 60)

    try:
        raw_locations = fetch_ldc_bids()
    except Exception as exc:
        log.error("LDC scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("LDC scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_ldc_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            upsert_location_meta(
                "LDC",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-42s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "LDC done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_tyson() -> int:
    """
    Scrape Tyson Foods (LGS) for all public Elevator locations and upsert bids.
    FeedMill locations are fetched and their metadata (lat/lon) is persisted so
    they appear on the map, but they post no public bid data.
    Returns the total number of snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("Tyson LGS scrape starting...")
    log.info("=" * 60)

    try:
        raw_locations = fetch_tyson_bids()
    except Exception as exc:
        log.error("Tyson scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("Tyson scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        # Always persist metadata (lat/lon) so map pins appear for FeedMills too
        if loc.get("lat") and loc.get("lon"):
            try:
                upsert_location_meta(
                    "Tyson",
                    loc["location_name"],
                    state         = loc.get("state") or None,
                    facility_type = None,
                    lat           = loc["lat"],
                    lon           = loc["lon"],
                )
            except Exception as exc:
                log.warning("  meta upsert failed for %s: %s", loc["location_name"], exc)

        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_tyson_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-30s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "Tyson done: %d location(s) with bids  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def run_gpc() -> int:
    """
    Scrape GPC / Kent Commodities (3 locations) and upsert bids.
    Returns total snapshot rows upserted.
    """
    log.info("=" * 60)
    log.info("GPC / Kent Commodities scrape starting...")
    log.info("=" * 60)

    try:
        raw_locations = fetch_gpc_bids()
    except Exception as exc:
        log.error("GPC scrape failed: %s", exc)
        return 0

    if not raw_locations:
        log.warning("GPC scrape returned no data.")
        return 0

    locations_done = 0
    total_rows     = 0
    errors         = 0
    skipped        = 0

    for loc in raw_locations:
        if not loc.get("cashbids"):
            skipped += 1
            continue

        try:
            snap_req = parse_gpc_location(loc)
            if snap_req is None:
                skipped += 1
                continue

            upsert_snapshot(snap_req.model_dump())
            upsert_location_meta(
                "GPC",
                snap_req.location,
                state         = loc.get("state") or None,
                facility_type = None,
                lat           = loc.get("lat") or None,
                lon           = loc.get("lon") or None,
            )
            locations_done += 1
            total_rows     += len(snap_req.rows)
            log.info(
                "  ✓  %-24s  %s  %d row(s)",
                snap_req.location, loc.get("state", "--"), len(snap_req.rows),
            )
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s: %s", loc.get("location_name", "?"), exc)

    log.info("-" * 60)
    log.info(
        "GPC done: %d location(s) updated  |  %d row(s) total"
        "  |  %d skipped  |  %d error(s)",
        locations_done, total_rows, skipped, errors,
    )
    return total_rows


def _run_simple(name: str, fetch_fn, parse_fn) -> int:
    """Generic runner for fetch → parse → upsert scrapers."""
    log.info("=" * 60)
    log.info("%s scrape starting…", name)
    log.info("=" * 60)
    try:
        locs = fetch_fn()
    except Exception as exc:
        log.error("%s fetch failed: %s", name, exc)
        return 0
    total_rows = 0
    errors     = 0
    for loc in locs:
        try:
            snap = parse_fn(loc)
            if not snap:
                continue
            upsert_snapshot(snap.model_dump())
            total_rows += len(snap.rows)
            log.info("  ✓  %-34s  %d row(s)", snap.location, len(snap.rows))
        except Exception as exc:
            errors += 1
            log.error("  ✗  %s", exc)
    log.info("-" * 60)
    log.info("%s done: %d row(s)  |  %d error(s)", name, total_rows, errors)
    return total_rows


def run_zfs() -> int:
    return _run_simple("ZFS", fetch_zfs_bids, parse_zfs_location)


def run_mnsp() -> int:
    return _run_simple("MNSP", fetch_mnsoy_bids, parse_mnsoy_location)


def run_platinum() -> int:
    return _run_simple("Platinum", fetch_platinum_bids, parse_platinum_location)


def run_shellrock() -> int:
    return _run_simple("Shell Rock", fetch_shellrock_bids, parse_shellrock_location)


def run_whiteriver() -> int:
    return _run_simple("White River", fetch_whiteriver_bids, parse_whiteriver_location)


def run_hppsd() -> int:
    return _run_simple("HPPSD", fetch_hppsd_bids, parse_hppsd_location)


def run_bartlett() -> int:
    return _run_simple("Bartlett", fetch_bartlett_bids, parse_bartlett_location)


def run_primient() -> int:
    return _run_simple("Primient", fetch_primient_bids, parse_primient_location)


def run_norfolkcrush() -> int:
    return _run_simple("NorfolkCrush", fetch_norfolkcrush_bids, parse_norfolkcrush_location)


def run_ndsp() -> int:
    return _run_simple("NDSP", fetch_ndsp_bids, parse_ndsp_location)


def run_sdsp() -> int:
    return _run_simple("SDSP", fetch_sdsp_bids, parse_sdsp_location)


def run_prune() -> None:
    """
    Apply tiered data retention (runs automatically every Monday).

    Policy:
      • Current calendar month  → keep ALL
      • Anything older          → keep ONE per (provider, location, week) — forever
    """
    log.info("=" * 60)
    log.info("Data retention pruning starting…")
    log.info("=" * 60)
    try:
        result = prune_old_snapshots(dry_run=False)
        if result["deleted"] == 0:
            log.info("Nothing to prune — all data within retention policy.")
        else:
            log.info(
                "Pruned %d snapshot(s) — database now has %d snapshot(s) / %d row(s)",
                result["deleted"], result["snaps_after"], result["rows_after"],
            )
    except Exception as exc:
        log.error("Pruning failed: %s", exc)


def run_futures_capture() -> int:
    """Harvest the current futures curve from ADM's feed and store it under today's
    date, so historical forward-basis curves can later anchor on each day's actual
    futures (instead of always today's). Best-effort — never blocks the scrape."""
    try:
        import adm_futures
        from database import save_futures_curve
        curve = adm_futures.fetch_futures_curve()
        if not curve:
            log.warning("Futures capture: empty curve — skipped")
            return 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        n = save_futures_curve(curve, today)
        log.info("Futures capture: stored %d contracts for %s", n, today)
        return n
    except Exception as exc:
        log.warning("Futures capture failed (scrape unaffected): %s", exc)
        return 0


def run(
    run_poet_scrape: bool = True,
    run_chs_scrape: bool = True,
    run_adm_scrape: bool = True,
    run_cgb_scrape: bool = True,
    run_cargill_scrape: bool = True,
    run_gpre_scrape: bool = True,
    run_andersons_scrape: bool = True,
    run_bunge_scrape: bool = True,
    run_scoular_scrape: bool = True,
    run_agp_scrape: bool = True,
    run_ldc_scrape: bool = True,
    run_tyson_scrape: bool = True,
    run_gpc_scrape: bool = True,
    run_zfs_scrape: bool = True,
    run_mnsp_scrape: bool = True,
    run_platinum_scrape: bool = True,
    run_shellrock_scrape: bool = True,
    run_whiteriver_scrape: bool = True,
    run_hppsd_scrape: bool = True,
    run_bartlett_scrape: bool = True,
    run_primient_scrape: bool = True,
    run_norfolkcrush_scrape: bool = True,
    run_ndsp_scrape: bool = True,
    run_sdsp_scrape: bool = True,
    run_pruning: bool = True,
) -> int:
    """
    Main daily routine — all web scrapes + weekly auto-prune.
    Pruning runs automatically on Mondays (or when run_pruning=True explicitly).
    Returns total snapshot rows imported.
    """
    init_db()
    total = 0
    if run_adm_scrape:
        total += run_adm()
    if run_poet_scrape:
        total += run_poet()
    if run_chs_scrape:
        total += run_chs()
    if run_cgb_scrape:
        total += run_cgb()
    if run_cargill_scrape:
        total += run_cargill()
    if run_gpre_scrape:
        total += run_gpre()
    if run_andersons_scrape:
        total += run_andersons()
    if run_bunge_scrape:
        total += run_bunge()
    if run_scoular_scrape:
        total += run_scoular()
    if run_agp_scrape:
        total += run_agp()
    if run_ldc_scrape:
        total += run_ldc()
    if run_tyson_scrape:
        total += run_tyson()
    if run_gpc_scrape:
        total += run_gpc()
    if run_zfs_scrape:
        total += run_zfs()
    if run_mnsp_scrape:
        total += run_mnsp()
    if run_platinum_scrape:
        total += run_platinum()
    if run_shellrock_scrape:
        total += run_shellrock()
    if run_whiteriver_scrape:
        total += run_whiteriver()
    if run_hppsd_scrape:
        total += run_hppsd()
    if run_bartlett_scrape:
        total += run_bartlett()
    if run_primient_scrape:
        total += run_primient()
    if run_norfolkcrush_scrape:
        total += run_norfolkcrush()
    if run_ndsp_scrape:
        total += run_ndsp()
    if run_sdsp_scrape:
        total += run_sdsp()

    # Capture today's futures curve (for per-day basis anchoring as history builds)
    run_futures_capture()

    # Auto-prune every Monday (weekday 0), or if explicitly requested
    if run_pruning and datetime.now().weekday() == 0:
        run_prune()

    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Basis Tracker — automated daily web scraper"
    )

    poet_group = parser.add_mutually_exclusive_group()
    poet_group.add_argument(
        "--no-poet", dest="no_poet", action="store_true",
        help="Skip POET Gradable scrape",
    )
    poet_group.add_argument(
        "--poet-only", dest="poet_only", action="store_true",
        help="Run POET scrape only — skip everything else",
    )

    chs_group = parser.add_mutually_exclusive_group()
    chs_group.add_argument(
        "--no-chs", dest="no_chs", action="store_true",
        help="Skip CHS scrape",
    )
    chs_group.add_argument(
        "--chs-only", dest="chs_only", action="store_true",
        help="Run CHS scrape only — skip everything else",
    )

    adm_group = parser.add_mutually_exclusive_group()
    adm_group.add_argument(
        "--no-adm", dest="no_adm", action="store_true",
        help="Skip ADM Gradable scrape",
    )
    adm_group.add_argument(
        "--adm-only", dest="adm_only", action="store_true",
        help="Run ADM Gradable scrape only — skip everything else",
    )

    cgb_group = parser.add_mutually_exclusive_group()
    cgb_group.add_argument(
        "--no-cgb", dest="no_cgb", action="store_true",
        help="Skip CGB Grain scrape",
    )
    cgb_group.add_argument(
        "--cgb-only", dest="cgb_only", action="store_true",
        help="Run CGB Grain scrape only — skip everything else",
    )

    cargill_group = parser.add_mutually_exclusive_group()
    cargill_group.add_argument(
        "--no-cargill", dest="no_cargill", action="store_true",
        help="Skip Cargill scrape",
    )
    cargill_group.add_argument(
        "--cargill-only", dest="cargill_only", action="store_true",
        help="Run Cargill scrape only — skip everything else",
    )

    gpre_group = parser.add_mutually_exclusive_group()
    gpre_group.add_argument(
        "--no-gpre", dest="no_gpre", action="store_true",
        help="Skip GPRE scrape",
    )
    gpre_group.add_argument(
        "--gpre-only", dest="gpre_only", action="store_true",
        help="Run GPRE scrape only — skip everything else",
    )

    andersons_group = parser.add_mutually_exclusive_group()
    andersons_group.add_argument(
        "--no-andersons", dest="no_andersons", action="store_true",
        help="Skip The Andersons scrape",
    )
    andersons_group.add_argument(
        "--andersons-only", dest="andersons_only", action="store_true",
        help="Run The Andersons scrape only — skip everything else",
    )

    bunge_group = parser.add_mutually_exclusive_group()
    bunge_group.add_argument(
        "--no-bunge", dest="no_bunge", action="store_true",
        help="Skip Bunge scrape",
    )
    bunge_group.add_argument(
        "--bunge-only", dest="bunge_only", action="store_true",
        help="Run Bunge scrape only — skip everything else",
    )

    scoular_group = parser.add_mutually_exclusive_group()
    scoular_group.add_argument(
        "--no-scoular", dest="no_scoular", action="store_true",
        help="Skip Scoular scrape",
    )
    scoular_group.add_argument(
        "--scoular-only", dest="scoular_only", action="store_true",
        help="Run Scoular scrape only — skip everything else",
    )

    agp_group = parser.add_mutually_exclusive_group()
    agp_group.add_argument(
        "--no-agp", dest="no_agp", action="store_true",
        help="Skip AGP scrape",
    )
    agp_group.add_argument(
        "--agp-only", dest="agp_only", action="store_true",
        help="Run AGP scrape only — skip everything else",
    )

    ldc_group = parser.add_mutually_exclusive_group()
    ldc_group.add_argument(
        "--no-ldc", dest="no_ldc", action="store_true",
        help="Skip LDC scrape",
    )
    ldc_group.add_argument(
        "--ldc-only", dest="ldc_only", action="store_true",
        help="Run LDC scrape only — skip everything else",
    )

    tyson_group = parser.add_mutually_exclusive_group()
    tyson_group.add_argument(
        "--no-tyson", dest="no_tyson", action="store_true",
        help="Skip Tyson LGS scrape",
    )
    tyson_group.add_argument(
        "--tyson-only", dest="tyson_only", action="store_true",
        help="Run Tyson LGS scrape only — skip everything else",
    )

    gpc_group = parser.add_mutually_exclusive_group()
    gpc_group.add_argument(
        "--no-gpc", dest="no_gpc", action="store_true",
        help="Skip GPC / Kent Commodities scrape",
    )
    gpc_group.add_argument(
        "--gpc-only", dest="gpc_only", action="store_true",
        help="Run GPC scrape only — skip everything else",
    )

    zfs_group = parser.add_mutually_exclusive_group()
    zfs_group.add_argument("--no-zfs", dest="no_zfs", action="store_true", help="Skip ZFS scrape")
    zfs_group.add_argument("--zfs-only", dest="zfs_only", action="store_true", help="Run ZFS scrape only")

    mnsp_group = parser.add_mutually_exclusive_group()
    mnsp_group.add_argument("--no-mnsp", dest="no_mnsp", action="store_true", help="Skip MNSP scrape")
    mnsp_group.add_argument("--mnsp-only", dest="mnsp_only", action="store_true", help="Run MNSP scrape only")

    platinum_group = parser.add_mutually_exclusive_group()
    platinum_group.add_argument("--no-platinum", dest="no_platinum", action="store_true", help="Skip Platinum scrape")
    platinum_group.add_argument("--platinum-only", dest="platinum_only", action="store_true", help="Run Platinum scrape only")

    shellrock_group = parser.add_mutually_exclusive_group()
    shellrock_group.add_argument("--no-shellrock", dest="no_shellrock", action="store_true", help="Skip Shell Rock scrape")
    shellrock_group.add_argument("--shellrock-only", dest="shellrock_only", action="store_true", help="Run Shell Rock scrape only")

    whiteriver_group = parser.add_mutually_exclusive_group()
    whiteriver_group.add_argument("--no-whiteriver", dest="no_whiteriver", action="store_true", help="Skip White River scrape")
    whiteriver_group.add_argument("--whiteriver-only", dest="whiteriver_only", action="store_true", help="Run White River scrape only")

    hppsd_group = parser.add_mutually_exclusive_group()
    hppsd_group.add_argument("--no-hppsd", dest="no_hppsd", action="store_true", help="Skip HPPSD scrape")
    hppsd_group.add_argument("--hppsd-only", dest="hppsd_only", action="store_true", help="Run HPPSD scrape only")

    bartlett_group = parser.add_mutually_exclusive_group()
    bartlett_group.add_argument("--no-bartlett", dest="no_bartlett", action="store_true", help="Skip Bartlett scrape")
    bartlett_group.add_argument("--bartlett-only", dest="bartlett_only", action="store_true", help="Run Bartlett scrape only")

    primient_group = parser.add_mutually_exclusive_group()
    primient_group.add_argument("--no-primient", dest="no_primient", action="store_true", help="Skip Primient scrape")
    primient_group.add_argument("--primient-only", dest="primient_only", action="store_true", help="Run Primient scrape only")

    norfolkcrush_group = parser.add_mutually_exclusive_group()
    norfolkcrush_group.add_argument("--no-norfolkcrush", dest="no_norfolkcrush", action="store_true", help="Skip Norfolk Crush scrape")
    norfolkcrush_group.add_argument("--norfolkcrush-only", dest="norfolkcrush_only", action="store_true", help="Run Norfolk Crush scrape only")

    ndsp_group = parser.add_mutually_exclusive_group()
    ndsp_group.add_argument("--no-ndsp", dest="no_ndsp", action="store_true", help="Skip NDSP Casselton scrape")
    ndsp_group.add_argument("--ndsp-only", dest="ndsp_only", action="store_true", help="Run NDSP Casselton scrape only")

    sdsp_group = parser.add_mutually_exclusive_group()
    sdsp_group.add_argument("--no-sdsp", dest="no_sdsp", action="store_true", help="Skip SDSP Volga scrape")
    sdsp_group.add_argument("--sdsp-only", dest="sdsp_only", action="store_true", help="Run SDSP Volga scrape only")

    prune_group = parser.add_mutually_exclusive_group()
    prune_group.add_argument(
        "--no-prune", dest="no_prune", action="store_true",
        help="Skip the automatic Monday data-retention pruning",
    )
    prune_group.add_argument(
        "--prune-only", dest="prune_only", action="store_true",
        help="Run data-retention pruning only — skip all scrapes and email import",
    )

    parser.add_argument(
        "--force", dest="force", action="store_true",
        help="Run even if today is a weekend or federal holiday (bypasses trading-day guard)",
    )

    parser.add_argument(
        "--no-email", dest="no_email", action="store_true",
        help="Skip emailing the Daily Basis Changes report after a full scrape",
    )

    args = parser.parse_args()

    # ── Trading-day guard (bypassed with --force or --prune-only) ─────────────
    if not args.force and not getattr(args, "prune_only", False):
        if not _is_trading_day():
            log.info(
                "Not a trading day (weekend or US federal holiday) — skipping. "
                "Use --force to override."
            )
            sys.exit(0)

    if args.prune_only:
        init_db()
        run_prune()
    elif args.poet_only:
        init_db()
        run_poet()
    elif args.chs_only:
        init_db()
        run_chs()
    elif args.adm_only:
        init_db()
        run_adm()
    elif args.cgb_only:
        init_db()
        run_cgb()
    elif args.cargill_only:
        init_db()
        run_cargill()
    elif args.gpre_only:
        init_db()
        run_gpre()
    elif args.andersons_only:
        init_db()
        run_andersons()
    elif args.bunge_only:
        init_db()
        run_bunge()
    elif args.scoular_only:
        init_db()
        run_scoular()
    elif args.agp_only:
        init_db()
        run_agp()
    elif args.ldc_only:
        init_db()
        run_ldc()
    elif args.tyson_only:
        init_db()
        run_tyson()
    elif args.gpc_only:
        init_db()
        run_gpc()
    elif args.zfs_only:
        init_db()
        run_zfs()
    elif args.mnsp_only:
        init_db()
        run_mnsp()
    elif args.platinum_only:
        init_db()
        run_platinum()
    elif args.shellrock_only:
        init_db()
        run_shellrock()
    elif args.whiteriver_only:
        init_db()
        run_whiteriver()
    elif args.hppsd_only:
        init_db()
        run_hppsd()
    elif args.bartlett_only:
        init_db()
        run_bartlett()
    elif args.primient_only:
        init_db()
        run_primient()
    elif args.norfolkcrush_only:
        init_db()
        run_norfolkcrush()
    elif args.ndsp_only:
        init_db()
        run_ndsp()
    elif args.sdsp_only:
        init_db()
        run_sdsp()
    else:
        run(
            run_poet_scrape=not args.no_poet,
            run_chs_scrape=not args.no_chs,
            run_adm_scrape=not args.no_adm,
            run_cgb_scrape=not args.no_cgb,
            run_cargill_scrape=not args.no_cargill,
            run_gpre_scrape=not args.no_gpre,
            run_andersons_scrape=not args.no_andersons,
            run_bunge_scrape=not args.no_bunge,
            run_scoular_scrape=not args.no_scoular,
            run_agp_scrape=not args.no_agp,
            run_ldc_scrape=not args.no_ldc,
            run_tyson_scrape=not args.no_tyson,
            run_gpc_scrape=not args.no_gpc,
            run_zfs_scrape=not args.no_zfs,
            run_mnsp_scrape=not args.no_mnsp,
            run_platinum_scrape=not args.no_platinum,
            run_shellrock_scrape=not args.no_shellrock,
            run_whiteriver_scrape=not args.no_whiteriver,
            run_hppsd_scrape=not args.no_hppsd,
            run_bartlett_scrape=not args.no_bartlett,
            run_primient_scrape=not args.no_primient,
            run_norfolkcrush_scrape=not args.no_norfolkcrush,
            run_ndsp_scrape=not args.no_ndsp,
            run_sdsp_scrape=not args.no_sdsp,
            run_pruning=not args.no_prune,
        )

        # ── Email the daily Changes report via Outlook (full scheduled run) ──
        if not args.no_email:
            try:
                from changes_report import send_daily_changes_email
                send_daily_changes_email()
            except Exception as exc:
                log.warning("Daily Changes email failed (scrape unaffected): %s", exc)
