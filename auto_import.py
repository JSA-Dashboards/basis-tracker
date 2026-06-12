"""
Basis Tracker — Automated daily importer.

Run this script daily (via Windows Task Scheduler) to pull in new ADM bid sheet
emails AND scrape the latest web bids for POET, CHS, ADM Gradable, CGB Grain,
and Cargill.
Run once with --all to retroactively import every ADM email in your mailbox.

Usage
-----
  python auto_import.py                   # emails (last 2 days) + all web scrapers
  python auto_import.py --all             # retroactive: ALL historical ADM emails + scrapers
  python auto_import.py --days 7          # emails from last 7 days + scrapers
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
  python auto_import.py --no-scoular     # skip Scoular scrape
  python auto_import.py --scoular-only   # Scoular scrape only
  python auto_import.py --no-prune       # skip automatic Monday pruning
  python auto_import.py --prune-only     # run data retention pruning only

Prerequisites
-------------
  1. Sign in to the Streamlit app at least once (http://localhost:8501)
     via the Email panel so the MSAL token is cached locally (for email import).
  2. .env must contain AZURE_CLIENT_ID and AZURE_TENANT_ID.
  3. playwright install chrome  (or: playwright install --with-deps chromium)
     must have been run so Playwright can find Chrome for the POET scrape.

The script reuses the cached MSAL token — no browser or sign-in prompt needed.
Results are logged to auto_import.log in this directory.
"""
import argparse
import sys
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()

from email_client import fetch_all_bid_emails, fetch_all_bid_emails_retroactive, parse_email, get_token
from database import (
    init_db, upsert_snapshot,
    is_email_imported, mark_email_imported,
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

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
TENANT_ID = os.getenv("AZURE_TENANT_ID", "common")
LOG_FILE  = Path(__file__).parent / "auto_import.log"

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

    for item in raw_results:
        market_id        = item["market_id"]
        display_name     = item["display_name"]
        instruments_data = item["instruments_data"]
        timestamp        = item["timestamp"]

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


def run_email_import(all_emails: bool = False, days: int = 2) -> int:
    """
    Import ADM bid emails from Outlook.
    Returns number of snapshot rows imported.
    """
    # ── Auth check ─────────────────────────────────────────────────────────────
    if not CLIENT_ID:
        log.error("AZURE_CLIENT_ID not set in .env — cannot authenticate.")
        return 0
    if not get_token(CLIENT_ID, TENANT_ID):
        log.error(
            "Not authenticated. Open the Streamlit app (http://localhost:8501) "
            "and sign in via the ✉ Email panel first, then re-run this script."
        )
        return 0

    mode_label = "ALL historical emails" if all_emails else f"last {days} days"
    log.info("=" * 60)
    log.info("Basis Tracker email import — %s", mode_label)
    log.info("=" * 60)

    # ── Fetch email list ───────────────────────────────────────────────────────
    if all_emails:
        log.info("Retroactive mode: scanning mailbox year-by-year via receivedDateTime $filter…")
        log.info("Scanning back to %d — this may take 1-3 minutes for large mailboxes.", 2020)
        emails, year_counts = fetch_all_bid_emails_retroactive(
            CLIENT_ID, TENANT_ID, adm_only=True, start_year=2020
        )
        log.info("Found %d ADM bid emails across full mailbox history", len(emails))
        for yr, cnt in sorted(year_counts.items()):
            log.info("  %s: %d email(s)", yr, cnt)
    else:
        log.info("Daily mode: fetching ADM bid emails (last %d days)…", days)
        emails = fetch_all_bid_emails(CLIENT_ID, TENANT_ID, max_results=500, adm_only=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        emails = [
            e for e in emails
            if datetime.fromisoformat(e["receivedAt"].replace("Z", "+00:00")) >= cutoff
        ]
        log.info("Found %d email(s) within last %d days", len(emails), days)

    if not emails:
        log.info("No new emails to import.")
        return 0

    # ── Import loop ────────────────────────────────────────────────────────────
    imported_snaps  = 0
    imported_emails = 0
    skipped         = 0
    errors          = 0

    for em in emails:
        email_id = em["id"]
        subject  = em["subject"]
        recv_str = em["receivedAt"][:10]

        if is_email_imported(email_id):
            skipped += 1
            log.debug("SKIP  [%s] %s", recv_str, subject)
            continue

        log.info("PARSE [%s] %s", recv_str, subject)
        try:
            result = parse_email(CLIENT_ID, TENANT_ID, email_id, em["provider"])

            if result.snapshots:
                for snap in result.snapshots:
                    upsert_snapshot(snap.model_dump())
                    imported_snaps += 1
                mark_email_imported(email_id, subject)
                imported_emails += 1
                locs = ", ".join(s.location for s in result.snapshots)
                log.info("  ✓  %d snapshot(s)  →  %s", len(result.snapshots), locs)
                if result.needsReview:
                    log.warning("  ⚠  Parser flagged for review — verify values in app")
            else:
                errors += 1
                log.warning("  ✗  Parse failed: %s", result.parseError or "unknown error")

        except Exception as exc:
            errors += 1
            log.error("  ✗  Exception: %s", exc)

    log.info("-" * 60)
    log.info(
        "Emails done: %d imported (%d snapshots)  |  %d skipped  |  %d error(s)",
        imported_emails, imported_snaps, skipped, errors,
    )
    return imported_snaps


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


def run(
    all_emails: bool = False,
    days: int = 2,
    run_poet_scrape: bool = True,
    run_chs_scrape: bool = True,
    run_adm_scrape: bool = True,
    run_cgb_scrape: bool = True,
    run_cargill_scrape: bool = True,
    run_gpre_scrape: bool = True,
    run_andersons_scrape: bool = True,
    run_bunge_scrape: bool = True,
    run_scoular_scrape: bool = True,
    run_pruning: bool = True,
) -> int:
    """
    Main daily routine — email import + all web scrapes + weekly auto-prune.
    Email auth failure does NOT prevent the web scrapes from running.
    Pruning runs automatically on Mondays (or when run_pruning=True explicitly).
    Returns total snapshot rows imported.
    """
    init_db()
    total = 0
    total += run_email_import(all_emails=all_emails, days=days)
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

    # Auto-prune every Monday (weekday 0), or if explicitly requested
    if run_pruning and datetime.now().weekday() == 0:
        run_prune()

    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Basis Tracker — automated ADM email + POET Gradable importer"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Retroactive mode: import ALL found ADM emails (ignore date filter)",
    )
    parser.add_argument(
        "--days", type=int, default=2,
        help="Import emails from the last N days (default: 2; ignored with --all)",
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

    prune_group = parser.add_mutually_exclusive_group()
    prune_group.add_argument(
        "--no-prune", dest="no_prune", action="store_true",
        help="Skip the automatic Monday data-retention pruning",
    )
    prune_group.add_argument(
        "--prune-only", dest="prune_only", action="store_true",
        help="Run data-retention pruning only — skip all scrapes and email import",
    )

    args = parser.parse_args()

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
    else:
        run(
            all_emails=args.all,
            days=args.days,
            run_poet_scrape=not args.no_poet,
            run_chs_scrape=not args.no_chs,
            run_adm_scrape=not args.no_adm,
            run_cgb_scrape=not args.no_cgb,
            run_cargill_scrape=not args.no_cargill,
            run_gpre_scrape=not args.no_gpre,
            run_andersons_scrape=not args.no_andersons,
            run_bunge_scrape=not args.no_bunge,
            run_scoular_scrape=not args.no_scoular,
            run_pruning=not args.no_prune,
        )
