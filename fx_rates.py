"""fx_rates.py — the daily USD/CAD behind the Canadian bids.

ADM's Ontario markets (Blenheim, Maidstone, Windsor, Bacres Grain, ...) post corn, soybeans and wheat as a Canadian-dollar CASH price against US futures in US dollars, and the "basis" they
publish is simply the one minus the other (Blenheim corn: C$5.785 - US$4.4875 = +1.2975, shown as +130 cents). That is not a basis, so the ADM import converts it to US cents per bushel
(`parsers.adm_parser`): cash / USDCAD - futures. The rate is the day's close of Massive's `C:USDCAD` currency aggregate (Canadian dollars per US dollar, 1.405 on 2026-10-27), on the same
key as the futures; each close is kept in the `fx_rates` table so a later day without a fresh rate (a failed call, a weekend) uses the latest earlier one, and a row can be re-converted.

    python fx_rates.py            # capture the last 10 days and print the stored rates
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

PAIR = "USDCAD"                                      # Canadian dollars per US dollar
_URL = "https://api.massive.com/v2/aggs/ticker/C:{pair}/range/1/day/{start}/{end}"


def _key(key: str | None) -> str:
    key = key or os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        raise RuntimeError("MASSIVE_API_KEY is not set")
    return key


def fetch_closes(pair: str = PAIR, start: date | None = None, end: date | None = None, key: str | None = None, timeout: int = 30) -> dict[str, float]:
    """Daily closes {'YYYY-MM-DD': rate} of a currency pair over start..end (default: the last 10 days) from Massive. A bar's date is its UTC day; the Sunday-evening open arrives as a
    small Sunday bar, which is dropped so each date is a weekday's own close (as in the Brazil tab's USDBRL). Raises on a failed request."""
    end = end or datetime.now(timezone.utc).date()
    start = start or end - timedelta(days=10)
    r = requests.get(_URL.format(pair=pair, start=start.isoformat(), end=end.isoformat()), headers={"Authorization": f"Bearer {_key(key)}"},
                     params={"adjusted": "true", "sort": "asc", "limit": 50000}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"Massive {pair} returned {r.status_code}: {r.text[:200]}")
    out: dict[str, float] = {}
    for bar in r.json().get("results") or []:
        day = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).date()
        if day.weekday() < 5 and bar.get("c"):
            out[day.isoformat()] = float(bar["c"])
    return out


def capture(pair: str = PAIR, days: int = 10, key: str | None = None) -> int:
    """Fetch the last `days` days of `pair` and store them (a day already stored is replaced). Never raises: a failed call logs a warning and returns 0, the stored rates stay as they were."""
    import database
    try:
        closes = fetch_closes(pair, end=datetime.now(timezone.utc).date(), start=datetime.now(timezone.utc).date() - timedelta(days=days), key=key)
        now = datetime.now(timezone.utc).isoformat()
        return database.upsert_fx_rates([(d, pair, rate, now) for d, rate in closes.items()])
    except Exception as exc:
        log.warning("FX %s capture failed: %s", pair, exc)
        return 0


def rate_on(day: date | str, pair: str = PAIR, max_age_days: int = 7) -> float | None:
    """The stored rate on `day`, else the latest before it within `max_age_days`; None when there is none that recent."""
    import database
    return database.get_fx_rate_on(pair, day.isoformat() if isinstance(day, date) else str(day), max_age_days)


def usdcad_now() -> float | None:
    """The rate the ADM import converts with: capture the last few days (best effort), then today's rate or the latest within a week. None when there is no rate at all, in which
    case the import files no Canadian-dollar-against-US-futures bid rather than an unconverted one."""
    capture(days=5)
    try:
        return rate_on(datetime.now(timezone.utc).date())
    except Exception as exc:
        log.warning("FX rate lookup failed: %s", exc)
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass
    import database
    print("captured:", capture(days=10))
    today = datetime.now(timezone.utc).date()
    for d, r in database.get_fx_rates(PAIR, (today - timedelta(days=10)).isoformat(), today.isoformat()).items():
        print(" ", d, r)
