"""
massive_futures.py — Futures curve harvested from Massive's flat-file S3
archive (official CME/CBOT session settlement data).

Massive publishes one gzipped CSV per CBOT trading session under

    us_futures_cbot/session_aggs_v1/YYYY/MM/YYYY-MM-DD.csv.gz

with one row per instrument traded that day (outrights, calendar spreads,
butterflies) and columns:

    ticker,session_end_date,window_start,open,high,low,close,volume,
    dollar_volume,transactions,settlement_price

We keep only outright contracts (tickers containing '-', ':' or a space are
calendar spreads / butterflies) for the commodities Basis Tracker tracks —
corn, soybeans, wheat, soybean meal, soybean oil, oats and KC HRW wheat, all
of which clear on CBOT — and use `settlement_price` (the exchange's official
close) rather than the last raw trade `close`. Verified against a live ADM
quote for the same contract months: Massive's settlement_price is already in
the same units Basis Tracker stores (no dollars->cents scaling needed, unlike
ADM's feed).

Ticker year codes are single-digit (e.g. "ZCZ6" = Dec 2026 corn); converted
here to Basis Tracker's own 2-digit-year convention ("ZCZ26") to match
adm_futures.py and the rest of the app.

Files land once per session, around 9 PM Central (02:00 UTC), not intraday —
this walks back up to a week to find the most recently published file, so
weekends and holidays are transparent to the caller. During the trading day
and the afternoon that means YESTERDAY's session: fine for a "latest settle"
curve, wrong for filing a price under a date. The daily capture therefore uses
`fetch_session_prices` (the REST API's 1session bars, each dated by its own
session) — see futures_capture.py.

Minneapolis spring wheat (MW) trades on MGEX, which Massive doesn't carry —
that symbol has to keep coming from adm_futures.py or another source.
"""
from __future__ import annotations

import gzip
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

_ENDPOINT = "https://files.massive.com"
_BUCKET   = "flatfiles"
_PREFIX   = "us_futures_cbot/session_aggs_v1"

# Root symbols we want (matches app.py's _FUT_SHORT, minus MW — see docstring).
_ROOTS  = {"ZC", "ZS", "ZW", "ZM", "ZL", "ZO", "KE"}
_MONTHS = "FGHJKMNQUVXZ"


def _client():
    """boto3 S3 client for Massive's endpoint. Credentials from st.secrets,
    falling back to env vars for non-Streamlit callers (e.g. cron scripts)."""
    key = secret = ""
    try:
        import streamlit as st
        key    = st.secrets.get("MASSIVE_S3_ACCESS_KEY", "")
        secret = st.secrets.get("MASSIVE_S3_SECRET_KEY", "")
    except Exception:
        pass
    if not key or not secret:
        import os
        key    = key    or os.environ.get("MASSIVE_S3_ACCESS_KEY", "")
        secret = secret or os.environ.get("MASSIVE_S3_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("MASSIVE_S3_ACCESS_KEY / MASSIVE_S3_SECRET_KEY not configured")
    return boto3.client(
        "s3",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        endpoint_url=_ENDPOINT,
        config=Config(signature_version="s3v4"),
    )


def _to_2digit_year(root: str, month: str, digit: str) -> str:
    """'ZC' + 'Z' + '6' -> 'ZCZ26', anchored on today via the standard
    nearby-decade rule (never resolves more than ~1 year into the past)."""
    d      = int(digit)
    today  = datetime.now(timezone.utc)
    decade = (today.year // 10) * 10
    year   = decade + d
    if year < today.year - 1:
        year += 10
    return f"{root}{month}{year % 100:02d}"


def _fetch_session_file(s3, date: datetime) -> bytes | None:
    key = f"{_PREFIX}/{date:%Y}/{date:%m}/{date:%Y-%m-%d}.csv.gz"
    try:
        return s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read()
    except ClientError as exc:
        # 404/NoSuchKey  = no session that day (weekend/holiday).
        # 403/Forbidden  = a valid session date whose file Massive has NOT published
        #   to us yet — the current day before its post-close upload returns 403,
        #   not 404. Both mean "not available", so the caller's walk-back should
        #   skip to the previous session rather than log an error. A genuine auth
        #   failure 403s on EVERY key (including old ones and list_objects), so the
        #   walk-back exhausts to an empty curve and surfaces as "no session file
        #   found in the last N days" instead.
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "403", "Forbidden", "AccessDenied"):
            return None
        raise


def fetch_futures_curve(max_lookback_days: int = 7) -> dict[str, float]:
    """Return {2-digit-year symbol -> settlement price} for the most recently
    published CBOT session. Walks back up to `max_lookback_days` to skip
    weekends/holidays. Empty dict on any failure (never raises) — mirrors
    adm_futures.fetch_futures_curve()'s contract so callers can treat the two
    sources interchangeably."""
    try:
        s3 = _client()
    except Exception as exc:
        log.error("Massive S3 client init failed: %s", exc)
        return {}

    today = datetime.now(timezone.utc)
    raw, found_date = None, None
    for back in range(max_lookback_days):
        day = today - timedelta(days=back)
        try:
            raw = _fetch_session_file(s3, day)
        except Exception as exc:
            log.warning("Massive fetch failed for %s: %s", day.date(), exc)
            continue
        if raw is not None:
            found_date = day.date()
            break

    if raw is None:
        log.warning("Massive: no session file found in the last %d days", max_lookback_days)
        return {}

    text  = gzip.decompress(raw).decode("utf-8")
    lines = text.splitlines()
    if not lines:
        return {}

    header = lines[0].split(",")
    try:
        i_ticker = header.index("ticker")
        i_settle = header.index("settlement_price")
    except ValueError:
        log.error("Massive: unexpected CSV header %r", header)
        return {}

    curve: dict[str, float] = {}
    for line in lines[1:]:
        fields = line.split(",")
        if len(fields) <= max(i_ticker, i_settle):
            continue
        tkr = fields[i_ticker]
        if "-" in tkr or ":" in tkr or " " in tkr:
            continue  # calendar spread / butterfly — outrights only
        root, rest = tkr[:2], tkr[2:]
        if root not in _ROOTS or len(rest) != 2:
            continue
        month, digit = rest[0], rest[1]
        if month not in _MONTHS or not digit.isdigit():
            continue
        sym = _to_2digit_year(root, month, digit)
        try:
            curve[sym] = float(fields[i_settle])
        except ValueError:
            pass

    log.info("Massive futures curve: %d contracts (session %s)", len(curve), found_date)
    return curve


# ── Per-session bars (REST API) ───────────────────────────────────────────────
# /aggs/<ticker>?resolution=1session returns one bar per session, newest first, each
# keyed by its own session_end_date (the session that closes at 1:20 PM Central that
# day). A bar gets its settlement_price some time after the close; before that, and
# for the session still trading, only the close (last trade) is known. A contract
# that did not trade has no bar that day (the flat files skip it the same way).

_API_BASE = "https://api.massive.com/futures/v1"
_OUTRIGHT = re.compile("(%s)([%s])(\\d)" % ("|".join(sorted(_ROOTS)), _MONTHS))


def _api_key() -> str:
    key = ""
    try:
        import streamlit as st
        key = st.secrets.get("MASSIVE_API_KEY", "")
    except Exception:
        pass
    key = key or os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        raise RuntimeError("MASSIVE_API_KEY not configured")
    return key


def _api_get(key: str, path: str | None = None, url: str | None = None, **params) -> dict:
    last: Exception = RuntimeError("Massive %s: no attempt made" % (path or url))
    for attempt in range(3):
        try:
            r = requests.get(url or _API_BASE + path, headers={"Authorization": "Bearer " + key},
                             params=None if url else params, timeout=60)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError("Massive %s -> HTTP %s: %s" % (path or url, r.status_code, r.text[:200]))
            if r.status_code < 500 and r.status_code != 429:
                break                       # a 4xx is not going to heal by retrying
        except requests.RequestException as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise last


def massive_ticker(symbol: str) -> str | None:
    """'ZCZ26' -> 'ZCZ6' (Massive's outrights carry a one-digit year); None for other symbols."""
    if (len(symbol) != 5 or symbol[:2] not in _ROOTS or symbol[2] not in _MONTHS
            or not symbol[3:].isdigit()):
        return None
    return symbol[:3] + symbol[4]


def symbol_from_contract(rec: dict) -> str | None:
    """Our 2-digit-year symbol for a /contracts record ('ZCZ6' last trading 2026-12-14 -> 'ZCZ26');
    None for spreads, other roots, or a record whose dates disagree with its year digit. A CBOT grain
    contract's last trading day falls in its own delivery month."""
    m = _OUTRIGHT.fullmatch(str(rec.get("ticker") or ""))
    if not m:
        return None
    root, month, digit = m.groups()
    try:
        last = date.fromisoformat(str(rec.get("last_trade_date") or rec.get("settlement_date"))[:10])
    except ValueError:
        return None
    cm = _MONTHS.index(month) + 1
    year = last.year if last.month >= cm else last.year - 1
    if year % 10 != int(digit):
        return None
    return f"{root}{month}{year % 100:02d}"


def list_outrights(on: date, key: str | None = None) -> dict[str, str]:
    """{Massive ticker -> our symbol} for every outright of the tracked roots listed on `on`."""
    key = key or _api_key()
    out: dict[str, str] = {}
    for root in sorted(_ROOTS):
        js = _api_get(key, "/contracts", product_code=root, date=on.isoformat(), limit=1000)
        recs, pages = list(js.get("results", [])), 1
        while js.get("next_url") and pages < 10:
            js = _api_get(key, url=js["next_url"])
            recs += js.get("results", [])
            pages += 1
        for rec in recs:
            sym = symbol_from_contract(rec)
            if sym:
                out[rec["ticker"]] = sym
    return out


def fetch_bars(ticker: str, key: str | None = None, limit: int = 50000) -> list[dict]:
    """The newest `limit` 1session bars of one contract (newest first)."""
    js = _api_get(key or _api_key(), f"/aggs/{ticker}", resolution="1session", limit=limit)
    return list(js.get("results", []))


def session_prices(bars: list[dict]) -> dict[date, tuple[float, bool]]:
    """{session_end_date -> (cents, official)} from one contract's bars: the exchange settlement
    when the bar carries one (official=True), else the session's close (False)."""
    out: dict[date, tuple[float, bool]] = {}
    for b in bars:
        try:
            d = date.fromisoformat(str(b.get("session_end_date"))[:10])
        except ValueError:
            continue
        settle, close = b.get("settlement_price"), b.get("close")
        if settle:                          # absent (or 0) until the session settles
            out[d] = (float(settle), True)
        elif close is not None:
            out[d] = (float(close), False)
    return out


def fetch_session_prices(on: date, sessions: int = 8, max_workers: int = 8
                         ) -> dict[date, dict[str, tuple[float, bool]]]:
    """The last `sessions` sessions of every tracked outright listed on `on`, each filed under its
    bar's own session date: {date: {symbol: (cents, official)}}. Raises when the contract list
    can't be read; a contract whose bars fail is logged and left out."""
    key = _api_key()
    tickers = list_outrights(on, key)
    out: dict[date, dict[str, tuple[float, bool]]] = {}
    failed = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch_bars, t, key, sessions): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                bars = fut.result()
            except Exception as exc:
                failed.append(t)
                log.warning("Massive bars failed for %s: %s", t, exc)
                continue
            for d, v in session_prices(bars).items():
                out.setdefault(d, {})[tickers[t]] = v
    log.info("Massive session bars: %d contracts listed, %d failed, sessions %s", len(tickers),
             len(failed), ", ".join(str(d) for d in sorted(out)[-3:]))
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    cur = fetch_futures_curve()
    for s in sorted(cur):
        print(f"  {s:7} {cur[s]:9.2f}")
