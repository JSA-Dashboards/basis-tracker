"""
rail_email_log.py — the ledger of rail update emails, so a rundown that was SAVED but never EMAILED is caught up.

Why (2026-10-06): the Cloud admin app saved a rundown, its email silently failed (its Secrets had lost the Graph keys) and
nothing else noticed. The update email is sent by whatever saved the rundown — the Cloud app's Save all, post_rail.py on the
droplet — so a failure there left no trace. Now every send path writes one row per corridor to `rail_email_log` (rail_report
does it inside send_rail_update_email / send_rail_recap_email), saves made with the email switched off write a 'skipped' row,
and rail_email_watch.py (a droplet cron job) emails any posting that is older than a grace period and has no row.

A row says "an email that was built from the board as of `snapshot_at` showed this corridor's posting `date`":

    kind      update | recap | catchup   an email went out (the app, post_rail.py, the in-app buttons / the weekly recap / the watcher)
              skipped                    saved with the email off on purpose (the Rail Entry checkbox, post_rail.py --no-email)
              heartbeat                  the Cloud app's Rail Entry tab was opened: proof that it runs the ledger code
              baseline                   when the watcher was first started — postings captured before it are never emailed by it
    via       cloud | droplet | local    where the code ran (where_am_i)

The coverage rule (uncovered): a posting (corridor, date, last_capture) is covered when some update / recap / catchup / skipped
row for the corridor has `date` >= the posting's date and `snapshot_at` >= the posting's last_capture — i.e. an email built after
the posting was saved that showed that day or a later one. A re-save (new last_capture) is uncovered again until it is emailed.

Pure decisions (uncovered, cloud_proof, signature) are separate from the DB calls so they can be tested without a database.
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import date as _date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

TABLE = "rail_email_log"
DDL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    sent_at            TEXT NOT NULL,
    via                TEXT,
    host               TEXT,
    kind               TEXT NOT NULL,
    date               TEXT,
    market             TEXT,
    snapshot_at        TEXT,
    covers_captured_at TEXT,
    subject            TEXT
)"""

HANDLED_KINDS = ("update", "recap", "catchup", "skipped")           # a posting these cover needs no email
CLOUD_PROOF_KINDS = ("heartbeat", "update", "recap", "skipped")     # rows that show the Cloud app writes the ledger
_FALLBACK_EXCLUDE = {"UP Illinois (Dom)"}                           # rail_report._EXCLUDE: never in the email
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ── small helpers ─────────────────────────────────────────────────────────────
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(s) -> datetime | None:
    """A stored timestamp as an aware UTC datetime (a naive one is taken as UTC); None when blank or unreadable."""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_freight(market: str) -> bool:
    return ("Freight" in market) or ("Shuttle" in market)


def _excluded() -> set:
    try:
        from rail_report import _EXCLUDE
        return set(_EXCLUDE)
    except Exception:                                                # the report module is optional here
        return set(_FALLBACK_EXCLUDE)


def is_emailable(market: str) -> bool:
    """Basis corridors the update email actually shows (freight / shuttle and the dropped corridors are never emailed)."""
    return bool(market) and not is_freight(market) and market not in _excluded()


def where_am_i() -> str:
    """'cloud' (Streamlit Community Cloud mounts the repo under /mount/src), 'droplet' (/opt/...) or 'local'."""
    p = os.path.abspath(__file__).replace("\\", "/")
    if p.startswith("/mount/src") or os.environ.get("RAIL_EMAIL_VIA") == "cloud":
        return "cloud"
    if p.startswith("/opt/") or os.environ.get("RAIL_EMAIL_VIA") == "droplet":
        return "droplet"
    return "local"


def signature(market: str, date: str, last_capture: datetime) -> str:
    """Identity of one version of one posting (a re-save changes last_capture): what the watcher remembers it already sent."""
    return f"{market}|{date}|{iso(last_capture)}"


# ── pure decisions ────────────────────────────────────────────────────────────
def uncovered(postings: list, ledger: list, *, now: datetime, grace: timedelta, since: datetime, min_date: str, max_date: str,
              sent=frozenset()) -> list:
    """The postings that were saved but never emailed.

    postings  [{'market', 'date', 'last_capture': datetime}] — every (corridor, date) written since `since`
    ledger    [{'kind', 'market', 'date', 'snapshot_at': datetime}]
    now/grace a posting younger than `grace` is left alone (the app's own send is still in flight)
    since     the watcher's baseline: anything captured before it is history, never emailed
    min_date / max_date  ISO dates; postings outside are ignored (a bulk rebuild of old history must not be emailed)
    sent      signatures the watcher has already sent itself (a second line of defence if a ledger write fails)
    Freight / shuttle and the corridors the email drops are ignored."""
    cover: dict = {}
    for r in ledger:
        if r.get("kind") in HANDLED_KINDS and r.get("market") and r.get("date") and r.get("snapshot_at"):
            cover.setdefault(r["market"], []).append((r["date"], r["snapshot_at"]))
    out = []
    for p in postings:
        m, d, cap = p["market"], p["date"], p.get("last_capture")
        if cap is None or not is_emailable(m) or d < min_date or d > max_date:
            continue
        if cap <= since or now - cap < grace:
            continue
        if signature(m, d, cap) in sent:
            continue
        if any(cd >= d and snap >= cap for cd, snap in cover.get(m, ())):
            continue
        out.append(p)
    return sorted(out, key=lambda p: (p["last_capture"], p["market"], p["date"]))


def cloud_proof(ledger: list, since: datetime) -> datetime | None:
    """When the Cloud app last wrote the ledger after `since` (None = it hasn't: it may still run code that sends without logging,
    so the watcher stays silent rather than send a duplicate)."""
    ts = [r["sent_at"] for r in ledger if r.get("via") == "cloud" and r.get("kind") in CLOUD_PROOF_KINDS
          and r.get("sent_at") and r["sent_at"] >= since]
    return max(ts) if ts else None


# ── database ──────────────────────────────────────────────────────────────────
def _conn():
    import database as db
    return db.get_conn(), db._ph()


def ensure_table() -> None:
    conn, _ = _conn()
    try:
        c = conn.cursor()
        c.execute(DDL)
        conn.commit()
    finally:
        conn.close()


def latest_postings(markets=None, since_days: int = 30, min_date: str | None = None) -> list:
    """Every (corridor, date) of the manual rundowns from the last `since_days` days: [{'market', 'date', 'last_capture'}]."""
    from rail_corridors import today_ct
    lo = min_date or (today_ct() - timedelta(days=since_days)).isoformat()
    conn, ph = _conn()
    try:
        c = conn.cursor()
        c.execute(f"SELECT market, date, MAX(captured_at) AS last_capture FROM rail_fob WHERE source={ph} AND date>={ph} "
                  f"GROUP BY market, date", ("manual", lo))
        rows = c.fetchall()
    finally:
        conn.close()
    want = None if markets is None else set(markets)
    out = []
    for r in rows:
        if want is not None and r["market"] not in want:
            continue
        out.append({"market": r["market"], "date": str(r["date"])[:10], "last_capture": parse_ts(r["last_capture"])})
    return out


def snapshot(markets=None) -> dict:
    """What an email built NOW would show: {'at': now, 'postings': [{'market', 'date', 'last_capture'}]} — for each emailable corridor
    (those in `markets`, or all) its latest posting. Taken BEFORE the email is built so a save that lands meanwhile stays uncovered."""
    at = now_utc()
    best: dict = {}
    for p in latest_postings(markets):
        if not is_emailable(p["market"]):
            continue
        cur = best.get(p["market"])
        if cur is None or p["date"] > cur["date"]:
            best[p["market"]] = p
    return {"at": at, "postings": sorted(best.values(), key=lambda p: p["market"])}


def _insert(rows: list) -> int:
    if not rows:
        return 0
    ensure_table()
    conn, ph = _conn()
    try:
        c = conn.cursor()
        for r in rows:
            c.execute(f"INSERT INTO {TABLE} (sent_at, via, host, kind, date, market, snapshot_at, covers_captured_at, subject) "
                      f"VALUES ({','.join([ph] * 9)})",
                      (r["sent_at"], r.get("via"), r.get("host"), r["kind"], r.get("date"), r.get("market"),
                       r.get("snapshot_at"), r.get("covers_captured_at"), r.get("subject")))
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _row(kind: str, **kw) -> dict:
    return {"sent_at": iso(now_utc()), "via": where_am_i(), "host": socket.gethostname(), "kind": kind, **kw}


def record(snap: dict, kind: str, subject: str = "") -> int:
    """Write one row per corridor of a snapshot: an email (kind update / recap / catchup) or a deliberate no-email (skipped) covers them."""
    rows = [_row(kind, date=p["date"], market=p["market"], snapshot_at=iso(snap["at"]),
                 covers_captured_at=iso(p["last_capture"]) if p.get("last_capture") else None, subject=(subject or "")[:300])
            for p in snap["postings"]]
    return _insert(rows)


def mark_handled(markets, kind: str = "skipped", subject: str = "") -> int:
    """The rundown for these corridors was saved with the email OFF on purpose: tell the watcher not to send it."""
    return record(snapshot(markets), kind, subject)


def heartbeat() -> None:
    """The Cloud app's Rail Entry tab was opened (once per session): proof that it runs this ledger code."""
    _insert([_row("heartbeat")])


def read_ledger(since: datetime | None = None, kinds: tuple | None = None) -> list:
    """The ledger rows written at or after `since` (default: all) of these `kinds` (default: all), with timestamps parsed."""
    ensure_table()
    conn, ph = _conn()
    try:
        c = conn.cursor()
        where, params = [], []
        if since is not None:
            where.append(f"sent_at>={ph}")
            params.append(iso(since - timedelta(minutes=1)))
        if kinds:
            where.append("kind IN (" + ",".join([ph] * len(kinds)) + ")")
            params.extend(kinds)
        c.execute(f"SELECT sent_at, via, host, kind, date, market, snapshot_at, covers_captured_at, subject FROM {TABLE}"
                  + (" WHERE " + " AND ".join(where) if where else ""), tuple(params))
        rows = c.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({"sent_at": parse_ts(r["sent_at"]), "via": r["via"], "host": r["host"], "kind": r["kind"], "date": r["date"],
                    "market": r["market"], "snapshot_at": parse_ts(r["snapshot_at"]), "covers": parse_ts(r["covers_captured_at"]),
                    "subject": r["subject"]})
    return out


def baseline() -> datetime | None:
    """When the watcher was first started (the oldest 'baseline' row), or None."""
    ts = [r["sent_at"] for r in read_ledger(kinds=("baseline",)) if r["sent_at"]]
    return min(ts) if ts else None


def set_baseline() -> datetime:
    """Start watching from now: postings captured before this are history and never emailed by the watcher."""
    _insert([_row("baseline")])
    return baseline() or now_utc()
