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

──────────────────────────────────────────────────────────────────────────────
TODO (futures API): implement `get_futures_price(symbol)` to return the latest
settle/last price in CENTS per bushel for a CME symbol like "ZSN26" / "ZCZ26".
Until then it returns None and callers fall back to plotting raw basis.
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from typing import Optional

# Optional manual overrides (cents/bu), handy for testing before the API lands:
#   _PRICE_OVERRIDES = {"ZSN26": 1123.50, "ZSX26": 1143.50, ...}
_PRICE_OVERRIDES: dict[str, float] = {}


def get_futures_price(symbol: str) -> Optional[float]:
    """Latest futures price (cents/bu) for a CME symbol, or None if unavailable.

    Replace the body with a call to the futures-quote API. Returning None for any
    symbol makes the forward-curve chart fall back to raw (un-anchored) basis.
    """
    if symbol in _PRICE_OVERRIDES:
        return _PRICE_OVERRIDES[symbol]
    return None  # ← wire to the futures quote API


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
