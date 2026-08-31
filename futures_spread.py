"""
futures_spread.py — Futures prices / spreads for anchoring forward basis curves.

Each delivery's basis is quoted against its OWN futures month (June vs ZSN, the
new-crop vs ZSX, etc.).  To draw a meaningful forward BASIS curve, every point is
re-expressed against ONE anchor month (the spot/front month) by adding the
futures spread between that delivery's month and the anchor:

    cash            = futures(row_symbol)  + raw_basis
    anchored_basis  = cash - futures(anchor_symbol)
                    = raw_basis + futures(row_symbol) - futures(anchor_symbol)

So the only external input needed is a futures price (cents/bu) per CME symbol.

Source: `adm_futures.fetch_futures_curve()` harvests these prices for free from
ADM's own Gradable feed (each instrument carries the underlying futures price +
symbol). The app loads that curve behind a cache and calls `set_curve()`; any
symbol not in the curve returns None and that point falls back to raw basis.
"""
from __future__ import annotations

from typing import Optional

# Optional manual overrides (cents/bu), handy for testing:
#   _PRICE_OVERRIDES = {"ZSN26": 1123.50, "ZSX26": 1143.50, ...}
_PRICE_OVERRIDES: dict[str, float] = {}

# Futures curve {symbol -> cents}. Populated from ADM's Gradable feed
# (adm_futures.fetch_futures_curve), set via set_curve() — the app loads it
# behind a cache and pushes it in so anchor calls do no network I/O.
_CURVE: dict[str, float] = {}


def set_curve(curve: Optional[dict[str, float]]) -> None:
    """Install the futures curve ({symbol -> cents}) used for anchoring."""
    global _CURVE
    _CURVE = dict(curve or {})


def ensure_curve() -> dict[str, float]:
    """Lazily harvest the futures curve if one hasn't been set (standalone use) —
    Massive settlements primary, ADM Gradable fallback (see futures_curve.py)."""
    global _CURVE
    if not _CURVE:
        try:
            import futures_curve
            _CURVE = futures_curve.fetch_futures_curve()
        except Exception:
            pass
    return _CURVE


def get_futures_price(symbol: str) -> Optional[float]:
    """Latest futures price (cents/bu) for a CME symbol, or None if unavailable.

    Source is the ADM-harvested curve (see adm_futures.py). None for a symbol
    makes the forward-curve chart fall back to raw (un-anchored) basis.
    """
    if symbol in _PRICE_OVERRIDES:
        return _PRICE_OVERRIDES[symbol]
    return ensure_curve().get(symbol)


def spread(near_symbol: str, far_symbol: str) -> Optional[float]:
    """far − near futures price (cents). None if either price is unavailable."""
    if near_symbol == far_symbol:
        return 0.0
    pn = get_futures_price(near_symbol)
    pf = get_futures_price(far_symbol)
    if pn is None or pf is None:
        return None
    return pf - pn


def anchor_basis(raw_basis: float, row_symbol: str,
                 anchor_symbol: str) -> Optional[float]:
    """Re-express `raw_basis` (quoted vs row_symbol) as a basis to anchor_symbol.

    Returns None when the required futures spread is unavailable, so the caller
    can fall back to the raw basis.
    """
    if not row_symbol or not anchor_symbol or row_symbol == anchor_symbol:
        return raw_basis
    s = spread(anchor_symbol, row_symbol)  # = price(row) − price(anchor)
    return None if s is None else raw_basis + s
