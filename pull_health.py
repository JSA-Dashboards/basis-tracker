"""
pull_health.py — did the daily basis pull STORE what it should have, and what can be put right while the run is still going.

Why (Kolten 2026-10-21, "For the days that have a basis pull fail are we checking and correcting where needed?"): auto_import
counted rows PARSED, not rows STORED. The Snowflake JWT outage of 9/29 and 9/30 ran rc=0 with ~0 snapshots written and an alert
that named five empty scrapers; nothing retried a failed scraper; and the same four dead ones (ZFS, VistaComm, DTN, Georges) were
emailed every day until the email stopped being read. A day that was never pulled cannot be pulled later (a site shows today's
bids), so the only fix is to notice while the run is going, when a second try can still help.

After the scrapers finish (auto_import.__main__, before the Changes email) post_run():

 1. VERIFY  reads `snapshots` for today's date: distinct (provider, location) per provider, against a normal day (the median of
            the last 10 pull days). The whole run: stored < 50% of normal = FAILED, < 95% = SHORT. Each provider that is
            normally there with 5 or more locations: none at all = missing, under 80% of its usual locations = short (the
            one-plant ethanol sites skip a day now and then; they are left to the stale list). Locations that missed the
            last two pull days = stale.
 2. RETRY   once, after a pause: every scraper that crashed / was abandoned / returned nothing and had not already failed its
            last two runs, the scraper behind each missing or short provider, and, if the whole run failed, every scraper.
            A scraper that keeps failing is not retried (it would cost minutes a day for nothing), nor is one whose abandoned
            thread is still running.
 3. VERIFY again and keep the books in state/basis_pull.json: the runs in a row each scraper has failed, the issues open at the
            last alert, retries that worked.
 4. TELL    the operator (CHANGES_EMAIL_TO) when the run failed, something NEW broke, something RECOVERED, on Mondays (a digest
            of everything still open) and the first time. A day with nothing but the known issues is quiet.
            `PULL_HEALTH_ALERTS=daily` mails whenever anything is open, as the old alert did.

The exit code is 2 when the run stored under half a normal day, or the database could not be read to check, so cron-alert and the
healthchecks heartbeat see a run that stored nothing (9/29 and 9/30 were rc=0).

Switches: `PULL_HEALTH=off` (or a file state/basis_pull.off) turns the whole check off and the old scraper alert is used again;
`PULL_HEALTH_RETRY=off` keeps the check and the alert but not the retry. A run that enabled under 85% of the scrapers (several
--no-* flags) is not judged: the skipped providers would only look missing.

    python pull_health.py report [--date YYYY-MM-DD]    today's (or a past day's) verdict and the stale locations, read-only
    python pull_health.py backtest [--days 40]          the verdict for each past trading day, and how many emails the policy would have sent
    python pull_health.py state                         the books in state/basis_pull.json

The decisions (evaluate, stale_locations, plan_retries, decide, build_alert) are pure and tested without a database
(tests/test_pull_health.py); load_coverage is the one query.
"""
from __future__ import annotations

import argparse
import html as _html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state" / "basis_pull.json"      # state/ is gitignored: it is the droplet's (or the PC's) own books

COVERAGE_DAYS = 45        # calendar days of per-day counts read (the baseline needs ~10 trading days)
STALE_DAYS = 60           # a location last stored longer ago than this is gone, not stale
BASELINE_DAYS = 10        # pull days that define a normal day
MIN_BASELINE_DAYS = 5     # with fewer than this nothing is judged
PULL_DAY_SHARE = 0.5      # a date is a pull day when it stored at least this share of the window's median day
RUN_FAILED = 0.5          # stored under this share of a normal day = the run failed
RUN_SHORT = 0.95          # ... under this = short (a normal day is within ~2% of the median; 10/2-10/8 lost 57 locations = 95%)
REGULAR_SHARE = 0.8       # a provider is 'regular' when it stored on at least this share of the baseline days
PROVIDER_SHORT = 0.8      # a regular provider storing under this share of its usual locations is short
MIN_PROVIDER = 5          # a provider with fewer usual locations than this is left to the stale list (one-plant sites skip a day)
PARTIAL_RUN = 0.85        # a run that enabled less than this share of the scrapers is not judged
PERSISTENT_AFTER = 2      # a scraper that failed this many runs in a row is not retried
RETRY_DELAY = 90          # seconds to wait before the retry (a site that hiccuped has had time to come back)
RETRY_BUDGET = 1200       # seconds the retry may take in all; no new scraper is started after that
DB_TRIES = 3              # reads of the coverage before the pull counts as unverifiable
DB_BACKOFF = 20           # seconds between those reads

FAIL_STATUSES = ("crashed", "abandoned", "empty")


# ── switches ──────────────────────────────────────────────────────────────────
def _off(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("off", "0", "false", "no")


def enabled(state_path=None) -> bool:
    if _off("PULL_HEALTH"):
        return False
    return not (Path(state_path or STATE_PATH).parent / "basis_pull.off").exists()


def alert_mode() -> str:
    return "daily" if os.getenv("PULL_HEALTH_ALERTS", "").strip().lower() == "daily" else "changes"


def utc_today() -> str:
    """The date the scrapers stamp their snapshots with (auto_import files them under the UTC date of the run)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── data ──────────────────────────────────────────────────────────────────────
@dataclass
class Coverage:
    by_day: dict = field(default_factory=dict)       # 'YYYY-MM-DD' -> {provider: distinct locations stored that day}
    last_seen: dict = field(default_factory=dict)    # (provider, location) -> newest 'YYYY-MM-DD' stored


@dataclass
class Verdict:
    today: str
    stored: int = 0
    typical: float | None = None
    share: float | None = None
    status: str = "ok"          # ok | short | failed | no_baseline | unverifiable
    baseline: list = field(default_factory=list)     # the pull days that defined a normal day
    providers: list = field(default_factory=list)    # [{provider, today, typical, kind: missing|short}]
    stale: list = field(default_factory=list)        # [{provider, n, oldest, newest, days, examples}]
    error: str = ""


@dataclass
class Result:
    skipped: str = ""           # why nothing was checked ('' = checked)
    first: Verdict | None = None
    final: Verdict | None = None
    retried: list = field(default_factory=list)
    keys: list = field(default_factory=list)
    new: list = field(default_factory=list)
    recovered: list = field(default_factory=list)
    emailed: bool = False
    subject: str = ""
    exit_code: int = 0


def load_coverage(today: str, days: int = COVERAGE_DAYS, stale_days: int = STALE_DAYS) -> Coverage:
    """The one query: per day and provider the distinct locations stored (the last `days`), and each location's newest day
    (the last `stale_days`), as of `today`. Snapshots are stamped 'YYYY-MM-DDT00:00:00Z' by the scrapers, so the first ten
    characters are the date and plain string comparison orders them."""
    import database as db
    ph = db._ph()
    d0 = _date.fromisoformat(today)
    upper = (d0 + timedelta(days=1)).isoformat()
    conn = db.get_conn()
    try:
        c = conn.cursor()
        c.execute(f"SELECT SUBSTR(timestamp, 1, 10) AS d, provider, COUNT(DISTINCT location) AS n FROM snapshots "
                  f"WHERE timestamp >= {ph} AND timestamp < {ph} GROUP BY 1, 2",
                  ((d0 - timedelta(days=days)).isoformat(), upper))
        by_day: dict = {}
        for r in c.fetchall():
            by_day.setdefault(str(r["d"]), {})[str(r["provider"])] = int(r["n"])
        c.execute(f"SELECT provider, location, MAX(SUBSTR(timestamp, 1, 10)) AS last_d FROM snapshots "
                  f"WHERE timestamp >= {ph} AND timestamp < {ph} GROUP BY 1, 2",
                  ((d0 - timedelta(days=stale_days)).isoformat(), upper))
        last_seen = {(str(r["provider"]), str(r["location"])): str(r["last_d"]) for r in c.fetchall()}
    finally:
        conn.close()
    return Coverage(by_day, last_seen)


def read_coverage(load, today, sleep=time.sleep, tries=DB_TRIES, backoff=DB_BACKOFF):
    """(coverage, '') or (None, the last error): a few reads, because the 9/29 outage was a connection that failed at random."""
    err = ""
    for i in range(tries):
        try:
            return load(today), ""
        except Exception as exc:                       # noqa: BLE001 — any failure to read means 'cannot verify'
            err = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("Pull health: reading the coverage failed (try %d of %d): %s", i + 1, tries, err)
            if i + 1 < tries:
                sleep(backoff)
    return None, err


# ── verdicts (pure) ───────────────────────────────────────────────────────────
def pull_days(by_day: dict, share: float = PULL_DAY_SHARE) -> list:
    """The dates that were real pulls: stored at least `share` of the window's median day. A day holding only Inco's carried
    sheet (one location), or a hand-filed day, is not a pull day, so it neither defines 'normal' nor counts as a miss."""
    totals = {d: sum(p.values()) for d, p in by_day.items() if p}
    if not totals:
        return []
    ref = median(totals.values())
    return sorted(d for d, t in totals.items() if t >= share * ref)


def baseline_days(by_day: dict, today: str, n: int = BASELINE_DAYS) -> list:
    return [d for d in pull_days(by_day) if d < today][-n:]


def stale_locations(cov: Coverage, today: str, window: int = STALE_DAYS) -> list:
    """Locations stored within the last `window` days that missed the latest pull day AND the one before it, grouped by provider
    (largest first). `days` = pull days since the freshest of the group last stored."""
    pulls = [d for d in pull_days(cov.by_day) if d <= today]
    if len(pulls) < 2:
        return []
    ref = pulls[-2]
    floor = (_date.fromisoformat(today) - timedelta(days=window)).isoformat()
    groups: dict = {}
    for (prov, loc), last in cov.last_seen.items():
        if last >= ref or last < floor:
            continue
        groups.setdefault(prov, []).append((last, loc))
    out = []
    for prov, items in groups.items():
        items.sort()
        newest = items[-1][0]
        out.append({"provider": prov, "n": len(items), "oldest": items[0][0], "newest": newest,
                    "days": sum(1 for d in pulls if d > newest), "examples": [loc for _, loc in items[:3]]})
    out.sort(key=lambda g: (-g["n"], g["provider"]))
    return out


def evaluate(cov: Coverage, today: str, with_stale: bool = True) -> Verdict:
    """What the pull stored on `today`, against a normal day. Only dates before `today` define normal."""
    v = Verdict(today=today)
    day = cov.by_day.get(today, {})
    v.stored = sum(day.values())
    base = baseline_days(cov.by_day, today)
    v.baseline = base
    if with_stale:
        v.stale = stale_locations(cov, today)
    if len(base) < MIN_BASELINE_DAYS:
        v.status = "no_baseline"
        return v
    v.typical = float(median(sum(cov.by_day[d].values()) for d in base))
    v.share = v.stored / v.typical if v.typical else None
    v.status = "ok" if v.share >= RUN_SHORT else "short" if v.share >= RUN_FAILED else "failed"
    for p in sorted(set().union(*(cov.by_day[d].keys() for d in base))):
        present = [cov.by_day[d].get(p, 0) for d in base if cov.by_day[d].get(p, 0) > 0]
        if len(present) < REGULAR_SHARE * len(base):
            continue                                   # not a regular: absent most days anyway, the stale list covers it
        usual = float(median(present))
        if usual < MIN_PROVIDER:
            continue
        n = day.get(p, 0)
        if n == 0:
            kind = "missing"
        elif n < PROVIDER_SHORT * usual:
            kind = "short"
        else:
            continue
        v.providers.append({"provider": p, "today": n, "typical": usual, "kind": kind})
    return v


# ── retry plan ────────────────────────────────────────────────────────────────
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def scrapers_for(provider: str, names) -> list:
    """The scrapers (by name) that file under this provider: the same name ignoring case and punctuation, or one a prefix of the
    other ('DTN' -> 'DTN-HTTP', 'DTN', 'DTN-Content'). Best effort: a wrong match only costs one extra run of a harmless scraper."""
    p = _norm(provider)
    if len(p) < 3:
        return []
    out = []
    for n in names:
        s = _norm(n)
        if len(s) >= 3 and (s == p or s.startswith(p) or p.startswith(s)):
            out.append(n)
    return out


def streak_before(state: dict, name: str, today: str) -> int:
    """Runs in a row the scraper had failed BEFORE today (an earlier run today has already been counted in the books)."""
    cur = state.get("scrapers", {}).get(name)
    if not cur:
        return 0
    return max(0, int(cur.get("streak", 0)) - (1 if cur.get("last") == today else 0))


def plan_retries(health, state, verdict, registry, alive, today, first_run) -> list:
    """[(scraper, why)] in the order they should run. Failed scrapers first, then the ones behind a short or missing provider,
    then (only if the whole run failed) the rest. Never a scraper that is not registered, whose abandoned thread is still
    running, or that has failed its last PERSISTENT_AFTER runs."""
    plan: list = []
    seen: set = set()

    def add(name, why):
        if name in seen or name not in registry:
            return
        if alive is not None and alive(name):
            return
        if streak_before(state, name, today) >= PERSISTENT_AFTER:
            return
        seen.add(name)
        plan.append((name, why))

    if not first_run:                                  # with no books there is no telling a new failure from an old one
        for h in health:
            if h["status"] in FAIL_STATUSES:
                add(h["name"], h["status"])
    if verdict.status not in ("failed", "unverifiable"):
        for p in verdict.providers:
            for s in scrapers_for(p["provider"], registry):
                add(s, f"{p['provider']} {p['kind']}")
    if verdict.status == "failed" and verdict.share is not None:
        for name in registry:
            add(name, f"the run stored {verdict.share:.0%} of a normal day")
    return plan


def retry_scrapers(plan, health, registry, run_guarded, deadline, clock=time.monotonic) -> list:
    """Re-run the planned scrapers one after another (the database does not like two writers on location_meta). Each one's
    _HEALTH entry is replaced by the retry's outcome, tagged `retried`. -> one record per scraper for the email."""
    out = []
    for name, why in plan:
        if clock() >= deadline:
            out.append({"name": name, "why": why, "before": None, "after": None, "rows": 0, "skipped": "retry time used up"})
            continue
        fn, budget = registry[name]
        old = next((h for h in health if h["name"] == name), None)
        health[:] = [h for h in health if h["name"] != name]
        try:
            run_guarded(fn, name, budget)
        except Exception as exc:                       # noqa: BLE001 — run_guarded does not raise, but nothing here may stop the run
            log.warning("Pull health: retry of %s raised: %s", name, exc)
        new = next((h for h in reversed(health) if h["name"] == name), None)
        if new is None:                                # keep the book whole whatever happened
            new = old or {"name": name, "rows": 0, "status": "crashed"}
            health.append(new)
        else:
            new["retried"] = True
        out.append({"name": name, "why": why, "before": old["status"] if old else None, "after": new["status"],
                    "rows": new.get("rows", 0), "skipped": ""})
    return out


# ── books ─────────────────────────────────────────────────────────────────────
def load_state(path=None) -> dict:
    p = Path(path or STATE_PATH)
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:                           # noqa: BLE001
        log.warning("Pull health: %s is unreadable (%s) — starting the books afresh", p, exc)
        return {}


def save_state(state: dict, path=None) -> None:
    p = Path(path or STATE_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:                           # noqa: BLE001 — losing the books means a repeated alert, not a failed run
        log.warning("Pull health: could not write %s: %s", p, exc)


def update_scrapers(state: dict, health, today: str, seed: bool = False) -> None:
    """Runs in a row each scraper has failed. A second run the same day does not count twice; an ok scraper is forgotten.
    `seed` (the first run, no books yet): whatever is failing is entered as already persistent, because nothing says how long it
    has been — otherwise the four scrapers that have been dead for weeks would be retried (up to 20 minutes) for two more days."""
    sc = state.setdefault("scrapers", {})
    for h in health:
        name, cur = h["name"], sc.get(h["name"])
        if h["status"] == "ok":
            sc.pop(name, None)
        elif cur and cur.get("last") == today:
            cur["status"] = h["status"]
        elif seed and not cur:
            sc[name] = {"streak": PERSISTENT_AFTER + 1, "since": today, "last": today, "status": h["status"], "seeded": True}
        else:
            sc[name] = {"streak": (int(cur["streak"]) + 1) if cur else 1, "since": cur["since"] if cur else today,
                        "last": today, "status": h["status"]}
            if cur and cur.get("seeded"):
                sc[name]["seeded"] = True


def issue_keys(health, verdict: Verdict) -> set:
    """What is open right now, as stable keys: scraper:NAME, provider:NAME, stale:PROVIDER, run:short|failed|unverifiable."""
    keys = {f"scraper:{h['name']}" for h in health if h["status"] in FAIL_STATUSES}
    if verdict.status in ("short", "failed", "unverifiable"):
        keys.add(f"run:{verdict.status}")
    if verdict.status != "failed":                     # a failed run has every provider missing: one key says it
        keys |= {f"provider:{p['provider']}" for p in verdict.providers}
    keys |= {f"stale:{g['provider']}" for g in verdict.stale}
    return keys


def run_fixed(first: Verdict | None, final: Verdict) -> bool:
    """The run failed (or could not be verified) at first and the retry put it right."""
    return bool(first and first.status in ("failed", "unverifiable") and final.status not in ("failed", "unverifiable"))


def decide(keys: set, state: dict, today: str, verdict: Verdict, retried, *, email: bool, first_run: bool, mode: str, first=None):
    """-> (send, new, recovered, digest, why). `new` / `recovered` are against the issues open at the last alert."""
    prev = set(state.get("issues", []))
    new, rec = sorted(keys - prev), sorted(prev - keys)
    digest = (_date.fromisoformat(today).weekday() == 0 and state.get("last_digest") != today and bool(keys))
    why = []
    if verdict.status in ("failed", "unverifiable"):
        why.append("the run failed")
    if run_fixed(first, verdict):
        why.append("the run failed at first and the retry fixed it")
    if new:
        why.append("new issues")
    if rec:
        why.append("recovered")
    if first_run:
        why.append("first run")
    if digest:
        why.append("Monday digest")
    if mode == "daily" and (keys or retried):
        why.append("daily mode")
    return (bool(email and why)), new, rec, digest, ", ".join(why)


# ── the email ─────────────────────────────────────────────────────────────────
def _esc(s) -> str:
    return _html.escape(str(s))


def _describe(key: str, ctx: dict):
    """(what, detail) for a key, from the run's own data."""
    kind, _, name = key.partition(":")
    if kind == "scraper":
        h, st = ctx["scr"].get(name, {}), ctx["scrapers"].get(name, {})
        d = h.get("status", "failed") + (f" — {str(h['detail'])[:120]}" if h.get("detail") else "")
        if st.get("seeded"):
            d += f" · failing since {st['since']} or earlier"
        elif int(st.get("streak", 0)) > 1:
            d += f" · {st['streak']} runs in a row, since {st['since']}"
        return f"{name} scraper", d
    if kind == "provider":
        p = ctx["prov"].get(name)
        if not p:
            return name, "missing / short"
        return name, ("none stored today" if p["kind"] == "missing" else f"{p['today']} locations stored")+ f" (normally {p['typical']:.0f})"
    if kind == "stale":
        g = ctx["stale"].get(name)
        if not g:
            return f"{name} locations", "not updated"
        last = g["newest"] if g["oldest"] == g["newest"] else f"{g['oldest']} .. {g['newest']}"
        return f"{name} — {g['n']} location(s)", f"last stored {last} · {g['days']} pull days ago · e.g. {', '.join(g['examples'])}"
    if kind == "run":
        v = ctx["v"]
        if key == "run:unverifiable":
            return "Run", f"could not read the database: {v.error}"
        return "Run", f"stored {v.stored:,} — {v.share:.0%} of a normal day" if v.share is not None else f"stored {v.stored:,}"
    return key, ""


def build_alert(rep: dict):
    """(subject, html) from the facts post_run collected. Sections without anything in them are left out."""
    v, today = rep["v"], rep["today"]
    ctx = {"scr": {h["name"]: h for h in rep["health"]}, "scrapers": rep["scrapers"], "v": v,
           "prov": {p["provider"]: p for p in v.providers}, "stale": {g["provider"]: g for g in v.stale}}
    new, rec = rep["new"], rep["recovered"]
    still = sorted(set(rep["keys"]) - set(new))
    n_stale = sum(g["n"] for g in v.stale)
    fixed = [r for r in rep["retried"] if r.get("before") in FAIL_STATUSES and r.get("after") == "ok"]

    v0 = rep.get("first")
    fixed_run = run_fixed(v0, v)
    if v.status == "failed":
        subject = f"🚨 Basis pull FAILED {today} — stored {v.share:.0%} of a normal day"
    elif v.status == "unverifiable":
        subject = f"🚨 Basis pull {today} — could not verify what was stored"
    elif fixed_run:
        subject = f"✅ Basis pull {today} — the run failed at first and the retry fixed it"
    elif new and not rep["first_run"]:
        subject = f"⚠️ Basis pull {today} — {len(new)} new issue(s)" + (f", {len(rec)} recovered" if rec else "")
    elif rec and not rep["first_run"]:
        subject = f"✅ Basis pull {today} — {len(rec)} issue(s) recovered"
    elif rep["first_run"]:
        subject = f"Basis pull check is on — {len(rep['keys'])} open issue(s) today"
    else:
        subject = f"Basis pull weekly digest {today} — {len(rep['keys'])} open issue(s), {n_stale} stale location(s)"

    td = "padding:3px 14px 3px 0;vertical-align:top"
    def rows(keys, color):
        out = []
        for k in keys:
            what, det = _describe(k, ctx)
            out.append(f"<tr><td style='{td};font-weight:600'>{_esc(what)}</td><td style='{td};color:{color}'>{_esc(det)}</td></tr>")
        return "<table style='border-collapse:collapse'>" + "".join(out) + "</table>"
    def h3(t):
        return f"<p style='font-size:13px;font-weight:700;margin:14px 0 4px'>{_esc(t)}</p>"

    if v.status == "unverifiable":
        head = f"<p style='color:#b91c1c'>The database could not be read to check what the pull stored: {_esc(v.error)}.</p>"
    elif v.status == "no_baseline":
        head = f"<p>Stored <b>{v.stored:,}</b> provider-locations. No baseline yet ({len(v.baseline)} pull days on file).</p>"
    else:
        col = "#b91c1c" if v.status == "failed" else "#1e293b"
        head = (f"<p style='color:{col}'>Stored <b>{v.stored:,}</b> provider-locations — <b>{v.share:.0%}</b> of a normal day "
                f"(~{v.typical:,.0f}, the median of the last {len(v.baseline)} pull days).</p>")
    body = [head]
    if fixed_run:
        was = (f"stored {v0.stored:,}" + (f" ({v0.share:.0%} of a normal day)" if v0.share is not None else "")
               if v0.status == "failed" else f"could not be verified ({v0.error})")
        body.append(f"<p style='color:#15803d;margin:2px 0'>At first the pull {_esc(was)}; after the retry it holds {v.stored:,}.</p>")
    if rep["retried"]:
        rr = "".join(
            f"<tr><td style='{td};font-weight:600'>{_esc(r['name'])}</td><td style='{td}'>{_esc(r['why'])}</td>"
            f"<td style='{td};color:{'#15803d' if r.get('after') == 'ok' else '#b91c1c'}'>"
            + (_esc(r["skipped"]) if r.get("skipped") else f"{_esc(r.get('before') or 'ok')} → {_esc(r.get('after'))} ({r.get('rows', 0)} rows)") + "</td></tr>"
            for r in rep["retried"])
        body.append(h3("Retried once") + f"<table style='border-collapse:collapse'>{rr}</table>")
    if new:
        body.append(h3("Open today" if rep["first_run"] else "New") + rows(new, "#b91c1c"))
    if rec:
        body.append(h3("Recovered since the last alert") + rows(rec, "#15803d"))
    if still:
        if rep["digest"] or v.status in ("failed", "unverifiable"):
            body.append(h3("Still open") + rows(still, "#64748b"))
        else:
            body.append(h3("Still open (unchanged)") + "<p style='color:#64748b;margin:2px 0'>"
                        + _esc(", ".join(_describe(k, ctx)[0] for k in still)) + "</p>")
    if rep["digest"] and rep["retried_ok"]:
        body.append(h3("Fixed by the retry since the last digest") + "<p style='margin:2px 0'>"
                    + _esc(", ".join(f"{n} ×{c}" for n, c in sorted(rep["retried_ok"].items()))) + "</p>")
    elif fixed and not rep["digest"]:
        body.append("<p style='color:#15803d;margin:8px 0 0'>Fixed by the retry: " + _esc(", ".join(r["name"] for r in fixed)) + ".</p>")
    body.append(
        "<p style='color:#64748b;font-size:12px;margin-top:14px'>Counted from what is in the database, not what the scrapers "
        "reported. <b>Stale</b> = a location that missed the last two pull days (seen within 60 days). A scraper that has "
        "failed twice in a row is not retried. Quiet on days with nothing new — set PULL_HEALTH_ALERTS=daily to hear every day. "
        "Auto-generated by pull_health.py.</p>")
    html = ("<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1e293b'>"
            f"<p style='font-size:15px;font-weight:700'>Basis Tracker — daily pull check, {today}</p>" + "".join(body) + "</div>")
    return subject, html


def send_default(subject: str, html: str) -> None:
    from changes_report import send_email
    send_email(subject, html, os.getenv("CHANGES_EMAIL_TO", "kpostin@jpsi.com"), cc="")


# ── the whole check ───────────────────────────────────────────────────────────
def summary_line(v: Verdict) -> str:
    if v.status == "unverifiable":
        return f"could not read the database: {v.error}"
    if v.status == "no_baseline":
        return f"stored {v.stored:,}; no baseline yet ({len(v.baseline)} pull days)"
    return (f"stored {v.stored:,} of ~{v.typical:,.0f} normal ({v.share:.0%}, {v.status}); providers short or missing: "
            f"{len(v.providers)}; stale locations: {sum(g['n'] for g in v.stale)} in {len(v.stale)} providers")


def post_run(health, *, registry, run_guarded, alive=None, run_info=None, email=True, today=None, state_path=None, send=None,
             sleep=time.sleep, clock=time.monotonic, load=None, delay=RETRY_DELAY, budget=RETRY_BUDGET) -> Result:
    """Verify, retry once, verify again, keep the books, tell the operator. `health` is auto_import's _HEALTH (changed in
    place by the retry); `registry` = {scraper name: (fn, budget)}; `run_guarded(fn, name, budget)` runs one; `alive(name)` says
    whether an abandoned scraper's thread is still going. Everything outside is injectable, so the tests need no network."""
    res = Result()
    if not enabled(state_path):
        res.skipped = "switched off"
        return res
    if run_info and run_info.get("total") and run_info.get("enabled", 0) < PARTIAL_RUN * run_info["total"]:
        res.skipped = f"partial run ({run_info['enabled']} of {run_info['total']} scrapers)"
        log.info("Pull health: %s — not judged.", res.skipped)
        return res
    today = today or utc_today()
    state = load_state(state_path)
    first_run = not state
    load = load or load_coverage

    def verify():
        cov, err = read_coverage(load, today, sleep)
        return evaluate(cov, today) if cov is not None else Verdict(today=today, status="unverifiable", error=err)

    v = res.first = verify()
    log.info("Pull health: %s", summary_line(v))

    if not _off("PULL_HEALTH_RETRY") and v.status != "unverifiable":
        plan = plan_retries(health, state, v, registry, alive, today, first_run)
        if plan:
            log.info("Pull health: retrying %d scraper(s) in %ds: %s", len(plan), delay,
                     ", ".join(f"{n} ({w})" for n, w in plan))
            sleep(delay)
            res.retried = retry_scrapers(plan, health, registry, run_guarded, clock() + budget, clock)
            v = verify()
            log.info("Pull health after the retry: %s", summary_line(v))
    res.final = v

    # the books: streaks always; the open-issue list only when the operator is actually told (an email run)
    update_scrapers(state, health, today, seed=first_run)
    for r in res.retried:
        if r.get("before") in FAIL_STATUSES and r.get("after") == "ok":
            state.setdefault("retried_ok", {})
            state["retried_ok"][r["name"]] = state["retried_ok"].get(r["name"], 0) + 1
    keys = issue_keys(health, v)
    send_it, new, rec, digest, why = decide(keys, state, today, v, res.retried, email=email, first_run=first_run, mode=alert_mode(),
                                            first=res.first)
    res.keys, res.new, res.recovered = sorted(keys), new, rec
    if send_it:
        subject, html = build_alert({"today": today, "v": v, "health": health, "scrapers": state.get("scrapers", {}),
                                     "keys": sorted(keys), "new": new, "recovered": rec, "retried": res.retried, "digest": digest,
                                     "first_run": first_run, "retried_ok": state.get("retried_ok", {}), "first": res.first})
        res.subject = subject
        try:
            (send or send_default)(subject, html)
            res.emailed = True
            log.info("Pull-health alert emailed (%s): %s", why, subject)
        except Exception as exc:                       # noqa: BLE001
            log.warning("Pull-health alert email failed (%s) — will try again next run: %s", why, exc)
    if email and (res.emailed or not send_it):
        state["issues"] = sorted(keys)
        if res.emailed:
            state["last_alert"] = today
            if digest:
                state["last_digest"] = today
                state.pop("retried_ok", None)
    state["last_run"] = today
    state["version"] = 1
    save_state(state, state_path)
    res.exit_code = 2 if v.status in ("failed", "unverifiable") else 0
    return res


# ── command line ──────────────────────────────────────────────────────────────
def format_report(v: Verdict) -> str:
    out = [f"Basis pull — {v.today}", "  " + summary_line(v)]
    if v.baseline:
        out.append(f"  a normal day = the median of {len(v.baseline)} pull days, {v.baseline[0]} .. {v.baseline[-1]}")
    if v.providers:
        out.append(f"\n  providers missing or short ({len(v.providers)}):")
        for p in v.providers:
            out.append(f"    {p['provider']:<34} {p['kind']:<8} today {p['today']:>4} · normally {p['typical']:.0f}")
    if v.stale:
        out.append(f"\n  stale locations — missed the last two pull days, seen in the last {STALE_DAYS} ({sum(g['n'] for g in v.stale)} in {len(v.stale)} providers):")
        for g in v.stale:
            last = g["newest"] if g["oldest"] == g["newest"] else f"{g['oldest']} .. {g['newest']}"
            out.append(f"    {g['provider']:<34} {g['n']:>3}  last {last}  ({g['days']} pull days)  e.g. {'; '.join(x[:30] for x in g['examples'])}")
    return "\n".join(out)


def backtest(cov: Coverage, start: str, end: str) -> list:
    """The verdict for each trading day in [start, end] using only the days before it as 'normal', plus whether the alert policy
    (provider and run issues only: scraper statuses and stale locations are not recorded per day) would have sent an email."""
    import grain_calendar
    rows, prev = [], set()
    d = _date.fromisoformat(start)
    end = min(end, max(cov.by_day)) if cov.by_day else end       # today, before its pull has run, is not a failure
    while d <= _date.fromisoformat(end):
        ds = d.isoformat()
        d += timedelta(days=1)
        if not grain_calendar.is_trading_day(_date.fromisoformat(ds)):
            continue
        v = evaluate(cov, ds, with_stale=False)
        keys = {k for k in issue_keys([], v) if not k.startswith("stale:")}
        new, rec = sorted(keys - prev), sorted(prev - keys)
        rows.append({"date": ds, "v": v, "new": new, "recovered": rec,
                     "email": v.status in ("failed", "unverifiable") or bool(new) or bool(rec)})
        prev = keys
    return rows


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Did the daily basis pull store what it should have?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="the verdict for a day (default today) and the stale locations; read-only")
    r.add_argument("--date")
    b = sub.add_parser("backtest", help="the verdict for each past trading day and how many emails the policy would have sent")
    b.add_argument("--days", type=int, default=40)
    sub.add_parser("state", help="print the books")
    a = ap.parse_args(argv)
    if a.cmd == "state":
        print(json.dumps(load_state(), indent=1, sort_keys=True))
        return 0
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env", override=True)
    except ImportError:
        pass
    logging.disable(logging.CRITICAL)
    import database as db
    if not db._use_sf():
        print("Not on Snowflake (the .env did not load?) — refusing to report on a local SQLite.", file=sys.stderr)
        return 1
    if a.cmd == "report":
        today = a.date or utc_today()
        print(format_report(evaluate(load_coverage(today), today)))
        return 0
    end = utc_today()
    start = (_date.fromisoformat(end) - timedelta(days=a.days)).isoformat()
    cov = load_coverage(end, days=a.days + COVERAGE_DAYS)
    rows = backtest(cov, start, end)
    print(f"{'date':<11}{'stored':>7}{'normal':>8}{'share':>7}  {'status':<10}providers flagged")
    for x in rows:
        v = x["v"]
        names = ", ".join(f"{p['provider']}({p['kind'][0]})" for p in v.providers[:6]) + (" …" if len(v.providers) > 6 else "")
        print(f"{x['date']:<11}{v.stored:>7}{(f'{v.typical:.0f}' if v.typical else '-'):>8}"
              f"{(f'{v.share:.0%}' if v.share is not None else '-'):>7}  {v.status:<10}{names}{'   <<< email' if x['email'] else ''}")
    judged = [x for x in rows if x["v"].status not in ("no_baseline",)]
    print(f"\n{len(judged)} judged days: " + ", ".join(f"{s} {sum(1 for x in judged if x['v'].status == s)}" for s in ("ok", "short", "failed"))
          + f"; the policy would have emailed on {sum(1 for x in judged if x['email'])} of them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
