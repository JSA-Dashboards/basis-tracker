"""Combined futures curve for the basis tracker.

Massive (official CBOT settlements) is the PRIMARY source; the free ADM Gradable
curve fills any gaps (e.g. a grain whose Massive call times out) and covers the case
where no Massive key is configured. Returns {symbol -> cents}, same shape both feeds
already use. This is the single entry point every curve consumer should call.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def fetch_futures_curve() -> dict[str, float]:
    curve: dict[str, float] = {}
    try:
        import adm_futures
        curve.update(adm_futures.fetch_futures_curve() or {})
    except Exception as exc:
        log.warning("ADM futures curve failed: %s", exc)
    try:
        import massive_futures
        m = massive_futures.fetch_futures_curve()
        if m:
            curve.update(m)          # official Massive settlements override ADM where present
            log.info("Massive futures curve: %d contracts (merged over %d ADM)",
                     len(m), len(curve))
    except Exception as exc:
        log.warning("Massive futures curve failed: %s", exc)
    return curve
