"""sf_scrape_task.py — Snowpark stored-procedure entrypoint for the scraper fleet.

Runs the pure-HTTP scrapers INSIDE Snowflake (as a scheduled task), reusing
auto_import's run_* functions. Each fetches + parses + upserts via database.py,
which auto-detects the active Snowpark session (get_active_session) → writes to
JSA.BASIS_TRACKER with no credentials. No browser, no desktop.

Requires (paid Snowflake): the CASH_BID_ACCESS External Access Integration
(outbound HTTP) attached to the procedure, and the code staged at @CODE_STAGE.
Handler = sf_scrape_task.run.
"""
import json

# The 38 pure-HTTP scrape functions from auto_import (run_dtn_playwright excluded —
# every DTN plant is browser-free now). Kept as names so a missing one is skipped,
# not fatal.
_HTTP_SCRAPERS = [
    "run_adm", "run_agp", "run_agrex", "run_agricharts_md", "run_agricharts_tenants",
    "run_agtegra", "run_alto", "run_andersons", "run_bartlett", "run_bunge",
    "run_bushelsites", "run_cargill", "run_cgb", "run_chs", "run_cihedging",
    "run_dtn_http", "run_gpc", "run_gpre", "run_heartland", "run_hppsd",
    "run_ksethanol", "run_ldc", "run_mennel", "run_mnsp", "run_ndsp",
    "run_norfolkcrush", "run_platinum", "run_poet", "run_primient", "run_scoular",
    "run_sdsp", "run_shellrock", "run_sotw", "run_tyson", "run_vistacomm",
    "run_whiteriver", "run_wpe", "run_zfs",
]


def run(session) -> str:
    """Run every HTTP scraper; upserts flow to Snowflake via the active session.
    Returns a JSON summary {total_rows, ok, failed, detail}. One scraper failing
    never aborts the batch."""
    import auto_import as ai

    total, ok, failed, detail = 0, 0, 0, {}
    for name in _HTTP_SCRAPERS:
        fn = getattr(ai, name, None)
        if fn is None:
            detail[name] = "MISSING"
            failed += 1
            continue
        try:
            n = int(fn() or 0)
            detail[name] = n
            total += n
            ok += 1
        except Exception as exc:                       # keep going on any single failure
            detail[name] = "ERR: " + str(exc)[:160]
            failed += 1
    return json.dumps({"total_rows": total, "ok": ok, "failed": failed,
                       "scrapers": len(_HTTP_SCRAPERS), "detail": detail})
