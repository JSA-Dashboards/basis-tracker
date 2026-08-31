"""Massive (api.massive.com) futures curve for the basis tracker.

Returns {basis-tracker symbol -> price in cents}, e.g. {"ZCU26": 512.25, ...} — the
same shape as adm_futures.fetch_futures_curve(). Used as the PRIMARY source for the
roll-spread / forward-basis math (official CBOT settlements); callers fall back to the
free ADM Gradable curve when the key is missing or the API fails.

Ticker mapping: Massive outrights are 1-digit-year (ZCU6 = Sep 2026); we rebuild the
2-digit-year symbol the tracker uses (ZCU26) from the ticker's month letter + the
contract's settlement year. See [[reference_massive_futures_api]].
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com/futures/v1"
# CBOT grains the tracker prices basis against (corn/soy/wheat/meal/oil/KC wheat).
_PRODUCTS = ["ZC", "ZS", "ZW", "ZM", "ZL", "KE"]
_TICKER_RE = re.compile(r"^([A-Z]{1,3})([FGHJKMNQUVXZ])(\d)$")


def _key() -> str | None:
    k = os.getenv("MASSIVE_API_KEY")
    if k:
        return k
    try:                                   # Streamlit Cloud secret
        import streamlit as st
        return st.secrets.get("MASSIVE_API_KEY")
    except Exception:
        return None


def _get(path: str, key: str, params: dict | None = None, _attempt: int = 0) -> dict:
    try:
        r = requests.get(f"{BASE_URL}{path}", headers={"Authorization": f"Bearer {key}"},
                         params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        if _attempt < 2:                    # transient timeouts happen — retry twice
            return _get(path, key, params, _attempt + 1)
        raise


def _outright(ticker: str, pc: str) -> bool:
    """Plain single-month contract (exclude spreads/butterflies/combos)."""
    return ticker.startswith(pc) and bool(_TICKER_RE.match(ticker))


def _price(snap: dict) -> float | None:
    """Settlement first (stable), then close, last trade, bid."""
    for path in (("session", "settlement_price"), ("session", "close"),
                 ("last_trade", "price"), ("last_quote", "bid")):
        node = snap
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, (int, float)) and node:
            return float(node)
    return None


def fetch_futures_curve() -> dict[str, float]:
    """{'ZCU26': price_cents, ...} from Massive settlements, or {} on any failure."""
    key = _key()
    if not key:
        return {}
    as_of = datetime.now(timezone.utc).date().isoformat()
    curve: dict[str, float] = {}
    for pc in _PRODUCTS:
        try:
            data = _get("/contracts", key, {"product_code": pc, "active": "true",
                                            "date": as_of, "limit": 400})
        except Exception as exc:
            log.warning("Massive /contracts %s failed: %s", pc, exc)
            continue
        exp_of = {}
        for r in data.get("results", []):
            t = r.get("ticker", "")
            if not _outright(t, pc):
                continue
            exp = r.get("settlement_date") or r.get("last_trade_date")
            if exp:
                exp_of[t] = exp
        tickers = list(exp_of)
        for i in range(0, len(tickers), 25):
            chunk = tickers[i:i + 25]
            try:
                snap = _get("/snapshot", key,
                            {"ticker.any_of": ",".join(chunk), "limit": len(chunk)})
            except Exception as exc:
                log.warning("Massive /snapshot %s failed: %s", pc, exc)
                continue
            for r in snap.get("results", []):
                t = (r.get("details") or {}).get("ticker")
                mon = _TICKER_RE.match(t or "")
                p = _price(r)
                exp = exp_of.get(t) or (r.get("details") or {}).get("settlement_date")
                if not (t and mon and p is not None and exp):
                    continue
                try:
                    yr = datetime.fromisoformat(str(exp)[:10]).year
                except Exception:
                    continue
                curve[f"{pc}{mon.group(2)}{yr % 100:02d}"] = p   # ZC + U + 26 -> ZCU26
    return curve


if __name__ == "__main__":
    import pathlib
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).with_name(".env"))
    c = fetch_futures_curve()
    print(f"{len(c)} contracts")
    for s in sorted(c)[:20]:
        print(f"  {s}: {c[s]}")
