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

Files land once per session (after the close), not intraday — this walks
back up to a week to find the most recently published file, so weekends and
holidays are transparent to the caller.

Minneapolis spring wheat (MW) trades on MGEX, which Massive doesn't carry —
that symbol has to keep coming from adm_futures.py or another source.
"""
from __future__ import annotations

import gzip
import logging
from datetime import datetime, timedelta, timezone

import boto3
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
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
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


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    cur = fetch_futures_curve()
    for s in sorted(cur):
        print(f"  {s:7} {cur[s]:9.2f}")
