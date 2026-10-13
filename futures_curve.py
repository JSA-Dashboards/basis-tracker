"""Combined futures curve for the basis tracker.

Massive (official CBOT settlements) is the PRIMARY source; the free ADM Gradable
curve fills any gaps (e.g. a grain whose Massive call times out) and covers the case
where no Massive key is configured. Returns {symbol -> cents}, same shape both feeds
already use. This is the single entry point every curve consumer should call.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _adm() -> dict[str, float]:
    try:
        import adm_futures
        return adm_futures.fetch_futures_curve() or {}
    except Exception as exc:
        log.warning("ADM futures curve failed: %s", exc)
        return {}


def _massive() -> dict[str, float]:
    try:
        import massive_futures
        return massive_futures.fetch_futures_curve() or {}
    except Exception as exc:
        log.warning("Massive futures curve failed: %s", exc)
        return {}


def fetch_futures_curve() -> dict[str, float]:
    # The two feeds are independent network calls, so run them side by side (a first
    # load used to pay for ADM and then Massive back to back); the merge order is unchanged.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        adm_f, massive_f = pool.submit(_adm), pool.submit(_massive)
        adm, m = adm_f.result(), massive_f.result()
    curve: dict[str, float] = dict(adm)
    if m:
        curve.update(m)          # official Massive settlements override ADM where present
        log.info("Massive futures curve: %d contracts (merged over %d ADM)", len(m), len(adm))
    return curve
