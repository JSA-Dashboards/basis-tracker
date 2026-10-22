"""The daily pull-health check (pull_health.py): did the pull STORE what it should have, and what is put right while the run is going.

    python tests/test_pull_health.py

The cases come from what really happened (2026-09-29 / 09-30: the Snowflake JWT outage ran rc=0 with ~1 location stored and an alert that
named five empty scrapers; 09-24: Cargill's 76 locations missing; 10-02 to 10-08: 57 locations short; the same four dead scrapers mailed
every day). Nothing touches Snowflake, the network or the real books: coverage is built in memory (a throwaway SQLite for the one query),
every retry is a fake scraper, every email a recorder, and the books live in a temp directory.
"""
import copy
import itertools
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ["USE_SNOWFLAKE"] = ""          # BEFORE importing database: the repo .env turns Snowflake on
for _k in ("PULL_HEALTH", "PULL_HEALTH_RETRY", "PULL_HEALTH_ALERTS"):
    os.environ.pop(_k, None)

import database                            # noqa: E402

database.DB_PATH = Path(tempfile.mkdtemp(prefix="pull_health_")) / "t.db"
assert database._backend() == "sqlite", "refusing to run on a non-sqlite backend"
import pull_health as ph                   # noqa: E402

FAILS = []
TMP = Path(tempfile.mkdtemp(prefix="pull_health_state_"))
TODAY = "2026-10-21"                       # a Wednesday
MONDAY = "2026-10-19"


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def weekdays_before(end, n):
    """The n weekdays before `end` (not including it), oldest first."""
    out, d = [], date.fromisoformat(end) - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


BASE = {"Alpha": 50, "Bravo": 10, "Cinco": 3, "Delta": 20}      # 83 locations on a normal day; C is a one-plant-style provider (under 5)


def cov_with(today_counts, today=TODAY, prior=12, base=BASE, by_day_extra=None, last_seen_extra=None):
    """Coverage of `prior` normal weekdays before `today`, then `today` with `today_counts` (None = no data at all that day)."""
    by_day = {d: dict(base) for d in weekdays_before(today, prior)}
    if by_day_extra:
        by_day.update(by_day_extra)
    if today_counts is not None:
        by_day[today] = dict(today_counts)
    last_seen = {}
    for p, n in base.items():
        for i in range(n):
            days = [d for d in sorted(by_day) if by_day[d].get(p, 0) > i]
            if days:
                last_seen[(p, f"{p} {i}")] = days[-1]
    last_seen.update(last_seen_extra or {})
    return ph.Coverage(by_day, last_seen)


def kinds(v):
    return {p["provider"]: p["kind"] for p in v.providers}


# ── pull days, the verdict ─────────────────────────────────────────────────────────────────────────────────────────────────────────
print("pull days and the baseline")
bd = {d: dict(BASE) for d in weekdays_before(TODAY, 12)}
bd["2026-10-17"] = {"Inco": 1}            # a Saturday holding only Inco's carried sheet
bd[TODAY] = dict(BASE)
pd = ph.pull_days(bd)
check("a day holding only Inco's carried sheet is not a pull day", "2026-10-17" not in pd)
check("every real pull day is one", len(pd) == 13, pd)
check("the baseline is the 10 pull days before today", ph.baseline_days(bd, TODAY) == pd[-11:-1])
check("no data at all -> no pull days", ph.pull_days({}) == [])

print("\nthe verdict on a day")
v = ph.evaluate(cov_with(BASE), TODAY)
check("a normal day is ok", v.status == "ok" and v.share == 1.0 and v.typical == 83 and v.stored == 83, v)
check("a normal day flags no provider", v.providers == [])
check("the baseline is 10 days", len(v.baseline) == 10)

v = ph.evaluate(cov_with({"Alpha": 40, "Bravo": 10, "Cinco": 3, "Delta": 20}), TODAY)
check("a provider at exactly 80% of its usual locations is not short", kinds(v) == {}, v.providers)
v = ph.evaluate(cov_with({"Alpha": 39, "Bravo": 10, "Cinco": 3, "Delta": 20}), TODAY)
check("a provider just under 80% is short", kinds(v) == {"Alpha": "short"}, v.providers)
check("... with what it stored and what is usual", v.providers[0]["today"] == 39 and v.providers[0]["typical"] == 50)
v = ph.evaluate(cov_with({"Alpha": 50, "Cinco": 3, "Delta": 20}), TODAY)
check("a regular provider with nothing stored is missing", kinds(v) == {"Bravo": "missing"}, v.providers)
check("... and the run is short (73 of 83 = 88%)", v.status == "short" and abs(v.share - 73 / 83) < 1e-9, (v.status, v.share))
v = ph.evaluate(cov_with({"Alpha": 50, "Bravo": 10, "Delta": 20}), TODAY)
check("a provider with under 5 usual locations is never flagged (the stale list covers it)", kinds(v) == {} and v.status == "ok", (v.providers, v.status))

v = ph.evaluate(cov_with({"Alpha": 45, "Bravo": 10, "Cinco": 3, "Delta": 20}), TODAY)
check("94% of a normal day is short", v.status == "short" and 0.93 < v.share < 0.95, (v.status, v.share))
v = ph.evaluate(cov_with({"Alpha": 47, "Bravo": 10, "Cinco": 3, "Delta": 20}), TODAY)
check("96% of a normal day is ok", v.status == "ok", (v.status, v.share))

v = ph.evaluate(cov_with({"Inco": 1}), TODAY)
check("the JWT-outage day (one location stored of 83) failed", v.status == "failed" and v.stored == 1 and v.share < 0.02, (v.status, v.share))
check("... every regular provider is missing", kinds(v) == {"Alpha": "missing", "Bravo": "missing", "Delta": "missing"}, v.providers)
v = ph.evaluate(cov_with(None), TODAY)
check("a day with no data at all failed", v.status == "failed" and v.stored == 0)
v = ph.evaluate(cov_with({"Alpha": 41, "Bravo": 10, "Cinco": 3, "Delta": 20}), TODAY)       # 74 of 83 = 89%
v2 = ph.evaluate(cov_with({"Alpha": 25, "Bravo": 5, "Cinco": 1, "Delta": 10}), TODAY)        # 41 of 83 = 49%
check("49% is failed, 89% is short", v2.status == "failed" and v.status == "short", (v2.status, v.status))

v = ph.evaluate(cov_with(BASE, prior=3), TODAY)
check("with fewer than 5 pull days of history nothing is judged", v.status == "no_baseline" and v.providers == [] and v.stored == 83, v.status)

# a provider that is only there some days is not a regular: it is never flagged missing
extra = {}
dates = weekdays_before(TODAY, 12)
for i, d in enumerate(dates):
    row = dict(BASE)
    if i >= 2 and i % 2 == 0:                     # E: 5 of the last 10 baseline days
        row["Echo"] = 9
    if i >= 2 and i != 5:                         # F: 9 of the last 10
        row["Foxtrot"] = 9
    extra[d] = row
v = ph.evaluate(cov_with(BASE, by_day_extra=extra), TODAY)
check("a provider present only half the baseline days is not flagged", "Echo" not in kinds(v), kinds(v))
check("a provider present 9 of 10 baseline days is regular, so missing today is flagged", kinds(v).get("Foxtrot") == "missing", kinds(v))

# only days BEFORE today define normal: a later day does not
cov = cov_with(BASE)
cov.by_day["2026-10-23"] = {"Alpha": 1}
check("days after the one judged do not move the baseline", ph.evaluate(cov, TODAY).typical == 83)

# ── stale locations ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nstale locations")
ls = {("Landus", f"L{i}"): "2026-09-19" for i in range(49)}
ls[("Gone", "g1")] = "2026-08-01"             # more than 60 days before today: dropped, not stale
ls[("Yday", "y1")] = "2026-10-20"             # stored yesterday: only missed today
ls[("Late", "l1")] = "2026-10-19"             # missed yesterday and today
cov = cov_with(BASE, last_seen_extra=ls)
st = ph.stale_locations(cov, TODAY)
names = [g["provider"] for g in st]
check("a location dark for two pull days is stale", "Late" in names and "Landus" in names, names)
check("a location that only missed today is not", "Yday" not in names)
check("a location last seen over 60 days ago is dropped", "Gone" not in names)
check("the biggest group is first", st[0]["provider"] == "Landus" and st[0]["n"] == 49, st[0])
check("... with up to three example locations", len(st[0]["examples"]) == 3)
check("... and how many pull days it has been dark", st[0]["days"] == 13 and [g for g in st if g["provider"] == "Late"][0]["days"] == 2, st)
cov = cov_with(None, last_seen_extra=ls)
names = [g["provider"] for g in ph.stale_locations(cov, TODAY)]
check("when today stored nothing the bar moves back a day (yesterday's miss is not yet stale)", "Late" not in names and "Landus" in names, names)
check("too little history -> no stale list", ph.stale_locations(ph.Coverage({TODAY: dict(BASE)}, ls), TODAY) == [])

# ── retry plan ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nwhich scrapers are retried")
check("a scraper is matched to its provider by name", ph.scrapers_for("Cargill", ["Cargill", "CHS"]) == ["Cargill"])
check("... ignoring case and punctuation", ph.scrapers_for("Star of the West", ["Star of the West"]) == ["Star of the West"] and ph.scrapers_for("shell-rock", ["Shell Rock"]) == ["Shell Rock"])
check("... DTN matches its three scrapers", ph.scrapers_for("DTN", ["DTN-HTTP", "DTN", "DTN-Content", "ADM"]) == ["DTN-HTTP", "DTN", "DTN-Content"])
check("... a name under three characters matches nothing", ph.scrapers_for("GP", ["GPC", "GPRE"]) == [])

REG = {n: (None, 240) for n in ("Alpha", "Bravo", "Cinco", "Delta", "Zulu")}
H = [{"name": "Alpha", "rows": 50, "status": "ok"}, {"name": "Bravo", "rows": 0, "status": "empty"},
     {"name": "Cinco", "rows": 0, "status": "crashed"}, {"name": "Delta", "rows": 0, "status": "abandoned"},
     {"name": "Quebec", "rows": 0, "status": "empty"}, {"name": "Zulu", "rows": 3, "status": "ok"}]
okv = ph.evaluate(cov_with(BASE), TODAY)


def plan(health=H, state=None, verdict=okv, alive=None, first_run=False, registry=REG):
    return ph.plan_retries(health, state if state is not None else {}, verdict, registry, alive, TODAY, first_run)


names = [n for n, _ in plan()]
check("new failures are retried, in the order they ran", names == ["Bravo", "Cinco", "Delta"], names)
check("a scraper not in the registry is left out", "Quebec" not in names)
check("an ok scraper is not retried", "Alpha" not in names and "Zulu" not in names)
names = [n for n, _ in plan(state={"scrapers": {"Cinco": {"streak": 2, "since": "2026-10-19", "last": "2026-10-20"}}})]
check("a scraper that failed its last two runs is not retried", names == ["Bravo", "Delta"], names)
names = [n for n, _ in plan(state={"scrapers": {"Cinco": {"streak": 1, "since": "2026-10-20", "last": "2026-10-20"}}})]
check("one earlier failure is still retried", "Cinco" in names)
names = [n for n, _ in plan(state={"scrapers": {"Cinco": {"streak": 2, "since": "2026-10-20", "last": TODAY}}})]
check("a failure already counted for today does not count as an earlier run", "Cinco" in names, names)
names = [n for n, _ in plan(alive=lambda n: n == "Delta")]
check("an abandoned scraper whose thread is still running is not started twice", names == ["Bravo", "Cinco"], names)
check("on the first run (no books) status failures are not retried", [n for n, _ in plan(first_run=True)] == [])

v = ph.evaluate(cov_with({"Alpha": 50, "Cinco": 3, "Delta": 20}), TODAY)         # B missing
p = plan(health=[{"name": n, "rows": 5, "status": "ok"} for n in BASE], verdict=v)
check("the scraper behind a missing provider is retried even though it said ok", p == [("Bravo", "Bravo missing")], p)
p = plan(health=[{"name": "Bravo", "rows": 0, "status": "empty"}], verdict=v)
check("... once, not twice", [n for n, _ in p] == ["Bravo"], p)
p = plan(health=[{"name": n, "rows": 5, "status": "ok"} for n in BASE], verdict=v, first_run=True)
check("... also on the first run", [n for n, _ in p] == ["Bravo"], p)
vf = ph.evaluate(cov_with({"Inco": 1}), TODAY)
p = plan(health=[{"name": n, "rows": 5, "status": "ok"} for n in BASE], verdict=vf)
check("when the whole run failed every scraper is retried", [n for n, _ in p] == ["Alpha", "Bravo", "Cinco", "Delta", "Zulu"] and "1%" in p[0][1], p)
p = plan(health=[{"name": n, "rows": 5, "status": "ok"} for n in BASE], verdict=vf, state={"scrapers": {"Bravo": {"streak": 5, "last": "2026-10-20"}}})
check("... except one that keeps failing", "Bravo" not in [n for n, _ in p])
vu = ph.Verdict(today=TODAY, status="unverifiable", error="x")
check("nothing to retry by provider when the check itself could not read the database", plan(health=[], verdict=vu) == [])

# ── retrying ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nthe retry itself")


def guard_factory(health, outcomes=None):
    calls = []

    def guard(fn, name, budget):
        calls.append((name, budget))
        if fn:
            fn()
        status, rows = (outcomes or {}).get(name, ("ok", 5))
        health.append({"name": name, "rows": rows, "status": status})
        return rows
    return guard, calls


health = [dict(h) for h in H]
guard, calls = guard_factory(health, {"Bravo": ("ok", 12), "Cinco": ("crashed", 0)})
reg = {n: (None, 100 + i) for i, n in enumerate(["Alpha", "Bravo", "Cinco", "Delta", "Zulu"])}
out = ph.retry_scrapers([("Bravo", "empty"), ("Cinco", "crashed")], health, reg, guard, deadline=100, clock=lambda: 0)
check("each planned scraper is re-run with its own budget", calls == [("Bravo", 101), ("Cinco", 102)], calls)
b = [h for h in health if h["name"] == "Bravo"]
check("its health entry is replaced by the retry's outcome, tagged", len(b) == 1 and b[0]["status"] == "ok" and b[0]["rows"] == 12 and b[0]["retried"], b)
check("the records say what happened", out[0]["before"] == "empty" and out[0]["after"] == "ok" and out[0]["rows"] == 12 and out[1]["after"] == "crashed", out)
check("untouched scrapers keep their entry", [h["status"] for h in health if h["name"] == "Alpha"] == ["ok"])
health = [dict(h) for h in H]
guard, calls = guard_factory(health)
out = ph.retry_scrapers([("Bravo", "empty"), ("Cinco", "crashed")], health, reg, guard, deadline=100, clock=itertools.chain([0], itertools.repeat(500)).__next__)
check("no new scraper starts once the retry budget is used up", calls == [("Bravo", 101)] and out[1]["skipped"], (calls, out))
check("... and its first result stays in the books", [h["status"] for h in health if h["name"] == "Cinco"] == ["crashed"])


class Boom:
    pass


def guard_raises(fn, name, budget):
    raise RuntimeError("x")


health = [dict(h) for h in H]
out = ph.retry_scrapers([("Bravo", "empty")], health, reg, guard_raises, deadline=100, clock=lambda: 0)
check("a guard that raises cannot stop the run or lose the entry", [h["status"] for h in health if h["name"] == "Bravo"] == ["empty"] and out[0]["after"] == "empty", (health, out))

# ── the books ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nthe books and the issue list")
state = {}
ph.update_scrapers(state, [{"name": "Bravo", "status": "empty", "rows": 0}, {"name": "Alpha", "status": "ok", "rows": 9}], "2026-10-20")
check("a failure starts a streak", state["scrapers"] == {"Bravo": {"streak": 1, "since": "2026-10-20", "last": "2026-10-20", "status": "empty"}}, state)
ph.update_scrapers(state, [{"name": "Bravo", "status": "crashed", "rows": 0}], "2026-10-20")
check("a second run the same day does not count twice", state["scrapers"]["Bravo"]["streak"] == 1 and state["scrapers"]["Bravo"]["status"] == "crashed")
ph.update_scrapers(state, [{"name": "Bravo", "status": "empty", "rows": 0}], TODAY)
check("the next day does, and keeps the day it began", state["scrapers"]["Bravo"]["streak"] == 2 and state["scrapers"]["Bravo"]["since"] == "2026-10-20")
ph.update_scrapers(state, [{"name": "Bravo", "status": "ok", "rows": 4}], "2026-10-22")
check("an ok scraper is forgotten", "Bravo" not in state["scrapers"])
seeded = {}
ph.update_scrapers(seeded, [{"name": "Zulu", "status": "empty", "rows": 0}, {"name": "Alpha", "status": "ok", "rows": 9}], TODAY, seed=True)
check("the first run enters what is failing as already persistent", seeded["scrapers"]["Zulu"]["streak"] > ph.PERSISTENT_AFTER and seeded["scrapers"]["Zulu"]["seeded"] and "Alpha" not in seeded["scrapers"], seeded)
check("... so it is not retried even the same day", ph.streak_before(seeded, "Zulu", TODAY) >= ph.PERSISTENT_AFTER)
ph.update_scrapers(seeded, [{"name": "Zulu", "status": "empty", "rows": 0}], "2026-10-22")
check("... the next failure keeps counting and stays marked", seeded["scrapers"]["Zulu"]["streak"] == ph.PERSISTENT_AFTER + 2 and seeded["scrapers"]["Zulu"]["seeded"] and seeded["scrapers"]["Zulu"]["since"] == TODAY)

v = ph.evaluate(cov_with({"Alpha": 39, "Bravo": 10, "Cinco": 3, "Delta": 20}, last_seen_extra=ls), TODAY)
keys = ph.issue_keys([{"name": "Bravo", "status": "empty", "rows": 0}, {"name": "Alpha", "status": "ok", "rows": 5}], v)
check("open issues are scraper, provider, stale and run keys", keys == {"scraper:Bravo", "provider:Alpha", "run:short", "stale:Landus", "stale:Late"}, keys)
v = ph.evaluate(cov_with({"Inco": 1}), TODAY)
keys = ph.issue_keys([], v)
check("a failed run is one key, not one per provider", keys == {"run:failed"}, keys)

# ── who is told ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nwho is told, and when")


def dec(keys, issues, today=TODAY, verdict=okv, retried=(), email=True, first_run=False, mode="changes", first=None, last_digest=""):
    state = {"issues": sorted(issues)}
    if last_digest:
        state["last_digest"] = last_digest
    return ph.decide(set(keys), state, today, verdict, list(retried), email=email, first_run=first_run, mode=mode, first=first)


check("known issues only -> quiet", dec({"scraper:Bravo", "stale:Landus"}, {"scraper:Bravo", "stale:Landus"})[0] is False)
send, new, rec, digest, why = dec({"scraper:Bravo", "scraper:Cinco"}, {"scraper:Bravo"})
check("a new issue -> mail, and it is the new one", send and new == ["scraper:Cinco"] and rec == [], (send, new, rec))
send, new, rec, digest, why = dec({"scraper:Bravo"}, {"scraper:Bravo", "provider:Cargill"})
check("a recovery -> mail", send and rec == ["provider:Cargill"] and new == [], (send, new, rec))
check("a failed run is always mailed", dec({"run:failed"}, {"run:failed"}, verdict=ph.evaluate(cov_with({"Inco": 1}), TODAY))[0])
check("an unverifiable run is always mailed", dec(set(), set(), verdict=vu)[0])
check("a Monday digest of what is still open", dec({"scraper:Bravo"}, {"scraper:Bravo"}, today=MONDAY)[0] and dec({"scraper:Bravo"}, {"scraper:Bravo"}, today=MONDAY)[3])
check("... only once that day", dec({"scraper:Bravo"}, {"scraper:Bravo"}, today=MONDAY, last_digest=MONDAY)[0] is False)
check("... and not when nothing is open", dec(set(), set(), today=MONDAY)[0] is False)
check("the first run is mailed", dec({"scraper:Bravo"}, set(), first_run=True)[0])
check("daily mode mails whatever is open", dec({"scraper:Bravo"}, {"scraper:Bravo"}, mode="daily")[0] and not dec(set(), set(), mode="daily")[0])
check("a run without email never mails", dec({"scraper:Cinco"}, set(), email=False)[0] is False)
fv = ph.evaluate(cov_with({"Inco": 1}), TODAY)
check("a run that failed at first and was fixed by the retry is mailed", dec(set(), set(), first=fv)[0] and "retry fixed it" in dec(set(), set(), first=fv)[4])

# ── the email ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nthe email")


def rep(v, keys, new=(), rec=(), retried=(), health=(), digest=False, first_run=False, first=None, scrapers=None, retried_ok=None):
    return {"today": TODAY, "v": v, "health": list(health), "scrapers": scrapers or {}, "keys": sorted(keys), "new": list(new), "recovered": list(rec),
            "retried": list(retried), "digest": digest, "first_run": first_run, "retried_ok": retried_ok or {}, "first": first}


subj, html = ph.build_alert(rep(fv, {"run:failed"}, new=["run:failed"]))
check("a failed run says so in the subject, with the share", subj.startswith("🚨 Basis pull FAILED") and "1%" in subj, subj)
check("... and the body has the numbers", "Stored <b>1</b>" in html and "1%" in html)
subj, html = ph.build_alert(rep(vu, {"run:unverifiable"}, new=["run:unverifiable"]))
check("an unverifiable run names the error", "could not verify" in subj and "x" in html)
v = ph.evaluate(cov_with({"Alpha": 39, "Bravo": 10, "Cinco": 3, "Delta": 20}, last_seen_extra=ls), TODAY)
hl = [{"name": "Bravo", "status": "empty", "rows": 0, "detail": "HTTP 403"}]
subj, html = ph.build_alert(rep(v, {"scraper:Bravo", "provider:Alpha", "stale:Landus"}, new=["provider:Alpha"], health=hl,
                                scrapers={"Bravo": {"streak": 5, "since": "2026-10-15"}}))
check("new issues are in the subject", "1 new issue" in subj, subj)
check("the new issue is described", "Alpha</td>" in html and "39 locations stored" in html and "normally 50" in html, html)
check("what is still open is listed compactly", "Still open (unchanged)" in html and "Bravo scraper" in html and "Landus" in html)
subj, html = ph.build_alert(rep(v, {"scraper:Bravo", "stale:Landus"}, rec=["provider:Cargill"]))
check("a recovery is in the subject", "recovered" in subj and "Cargill" in html, subj)
subj, html = ph.build_alert(rep(v, {"scraper:Bravo", "stale:Landus"}, digest=True, health=hl, scrapers={"Bravo": {"streak": 5, "since": "2026-10-15"}},
                                retried_ok={"Cargill": 2}))
check("the Monday digest lists everything with how long", "weekly digest" in subj and "5 runs in a row, since 2026-10-15" in html and "HTTP 403" in html, subj)
check("... the stale locations with their last day and examples", "Landus — 49 location(s)" in html and "2026-09-19" in html and "L0" in html)
check("... and the retries that worked this week", "Cargill ×2" in html)
subj, html = ph.build_alert(rep(v, {"scraper:Bravo"}, new=["scraper:Bravo"], first_run=True))
check("the first mail says the check is on", subj.startswith("Basis pull check is on"), subj)
rt = [{"name": "Bravo", "why": "empty", "before": "empty", "after": "ok", "rows": 12, "skipped": ""},
      {"name": "Cinco", "why": "crashed", "before": "crashed", "after": "crashed", "rows": 0, "skipped": ""},
      {"name": "Delta", "why": "x", "before": None, "after": None, "rows": 0, "skipped": "retry time used up"}]
subj, html = ph.build_alert(rep(v, {"scraper:Cinco"}, new=["scraper:Cinco"], retried=rt))
check("retries are listed with before → after and rows", "empty → ok (12 rows)" in html and "crashed → crashed" in html and "retry time used up" in html)
check("... and a fix is called out", "Fixed by the retry: B" in html)
subj, html = ph.build_alert(rep(okv, set(), first=fv))
check("a run fixed by the retry says what it first stored", "retry fixed it" in subj and "stored 1 (1% of a normal day)" in html and "83" in html, (subj, html))
evil = ph.Verdict(today=TODAY, status="ok", stored=1, typical=1.0, share=1.0, baseline=list(range(10)),
                  providers=[{"provider": "A&B <script>", "today": 0, "typical": 9.0, "kind": "missing"}])
subj, html = ph.build_alert(rep(evil, {"provider:A&B <script>"}, new=["provider:A&B <script>"]))
check("names are escaped in the HTML", "<script>" not in html and "A&amp;B &lt;script&gt;" in html, html)

# ── the whole check, end to end ────────────────────────────────────────────────────────────────────────────────────────────────────
print("\npost_run end to end")


class World:
    """A fake database the fake scrapers write to, plus recorders for what the check does."""

    def __init__(self, today_counts=None, today=TODAY, **kw):
        self.today = today
        self.cov = cov_with(BASE if today_counts is None else today_counts, today=today, **kw)
        self.fail_reads = False
        self.sent, self.sleeps, self.calls = [], [], []
        self.fix = {}                                # scraper -> counts it stores when re-run
        self.send_raises = False
        self.parsed = 0                              # rows a re-run scraper reports PARSED when it stores nothing (the JWT outage)
        self.health = []
        self.path = TMP / f"state_{len(list(TMP.iterdir()))}.json"
        self.registry = {n: (self._fn(n), 240) for n in BASE}

    def _fn(self, name):
        def fn():
            self.calls.append(name)
            if name in self.fix:
                self.cov.by_day.setdefault(self.today, {})[name] = self.fix[name]
            return self.fix.get(name, self.parsed)
        return fn

    def load(self, today):
        if self.fail_reads:
            raise RuntimeError("JWT token is invalid")
        return copy.deepcopy(self.cov)

    def guard(self, fn, name, budget):
        rows = fn()
        self.health.append({"name": name, "rows": rows, "status": "ok" if rows else "empty"})
        return rows

    def send(self, subject, html):
        if self.send_raises:
            raise RuntimeError("graph down")
        self.sent.append((subject, html))

    def books(self, **st):
        self.path.write_text(json.dumps({"version": 1, "issues": [], "scrapers": {}, "last_run": "2026-10-20", **st}), encoding="utf-8")

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def ok_health(self, rows=5):
        self.health = [{"name": n, "rows": rows, "status": "ok"} for n in BASE]

    def run(self, **kw):
        kw.setdefault("run_info", {"enabled": 45, "total": 45})
        return ph.post_run(self.health, registry=self.registry, run_guarded=self.guard, today=self.today, state_path=self.path, send=self.send,
                           sleep=self.sleeps.append, load=self.load, **kw)


w = World()
w.books()
w.ok_health()
r = w.run()
check("a healthy day: no retry, no mail, rc 0", w.sleeps == [] and w.calls == [] and w.sent == [] and r.exit_code == 0 and r.final.status == "ok", (w.sleeps, w.sent))
check("... and the books say when it last ran", w.state()["last_run"] == TODAY and w.state()["issues"] == [])

w = World(last_seen_extra=ls)
w.ok_health()
r = w.run()
check("the first run (no books) mails once, what is open", len(w.sent) == 1 and w.sent[0][0].startswith("Basis pull check is on"), w.sent)
check("... and records it, so the next run is quiet", "stale:Landus" in w.state()["issues"])
w.sent.clear()
w.run()
check("the next run, nothing changed, is quiet", w.sent == [])

w = World()
w.books()
w.health = [{"name": "Alpha", "rows": 5, "status": "ok"}, {"name": "Bravo", "rows": 5, "status": "ok"}, {"name": "Cinco", "rows": 0, "status": "empty"},
            {"name": "Delta", "rows": 5, "status": "ok"}]
w.fix = {"Cinco": 3}
r = w.run()
check("a new empty scraper is retried once after a pause", w.calls == ["Cinco"] and w.sleeps == [ph.RETRY_DELAY], (w.calls, w.sleeps))
check("... and when the retry stores it, nothing is mailed and rc is 0", w.sent == [] and r.exit_code == 0 and r.retried[0]["after"] == "ok", (w.sent, r.retried))
check("... the books count the retry that worked, not a failure", w.state().get("retried_ok") == {"Cinco": 1} and "Cinco" not in w.state()["scrapers"], w.state())

w = World({"Inco": 1})
w.books()
w.ok_health()
w.fix = dict(BASE)
r = w.run()
check("a JWT-outage run (all 'ok', nothing stored) retries every scraper", sorted(w.calls) == sorted(BASE) and w.sleeps == [ph.RETRY_DELAY], (w.calls, w.sleeps))
check("... and when that fixes it: rc 0, one mail saying so", r.exit_code == 0 and len(w.sent) == 1 and "retry fixed it" in w.sent[0][0], [s for s, _ in w.sent])
check("... the first verdict is kept for the record", r.first.status == "failed" and r.final.status == "ok")

w = World({"Inco": 1})
w.books()
w.ok_health()
w.parsed = 5
r = w.run()
check("when the retry does not help the run fails: rc 2", r.exit_code == 2 and r.final.status == "failed", r.final)
check("... one mail, FAILED", len(w.sent) == 1 and w.sent[0][0].startswith("🚨 Basis pull FAILED"), [s for s, _ in w.sent])
check("... run:failed is on the books, so tomorrow's repeat is not a new alert", w.state()["issues"] == ["run:failed"])

w = World()
w.books()
w.ok_health()
w.fail_reads = True
r = w.run()
check("a database that cannot be read is unverifiable: three tries, rc 2", r.final.status == "unverifiable" and r.exit_code == 2 and w.sleeps == [ph.DB_BACKOFF, ph.DB_BACKOFF], (r.final.status, w.sleeps))
check("... nothing is retried against a database that is down", w.calls == [])
check("... and the operator is told", len(w.sent) == 1 and "could not verify" in w.sent[0][0] and "JWT token is invalid" in w.sent[0][1])

os.environ["PULL_HEALTH"] = "off"
w = World({"Inco": 1})
w.ok_health()
r = w.run()
check("PULL_HEALTH=off: nothing is checked, nothing written, rc 0", r.skipped == "switched off" and r.exit_code == 0 and not w.path.exists() and w.sleeps == [] and w.sent == [])
os.environ.pop("PULL_HEALTH")
w = World({"Inco": 1})
w.ok_health()
(w.path.parent / "basis_pull.off").write_text("", encoding="utf-8")
r = w.run()
check("... the off file does the same", r.skipped == "switched off")
(w.path.parent / "basis_pull.off").unlink()
w = World({"Inco": 1})
w.ok_health()
r = w.run(run_info={"enabled": 20, "total": 45})
check("a run with most scrapers switched off is not judged", r.skipped.startswith("partial run") and r.exit_code == 0 and w.sent == [], r.skipped)
r = w.run(run_info={"enabled": 40, "total": 45})
check("... but 40 of 45 is", not r.skipped and r.exit_code == 2)

os.environ["PULL_HEALTH_RETRY"] = "off"
w = World()
w.books()
w.health = [{"name": "Alpha", "rows": 5, "status": "ok"}, {"name": "Bravo", "rows": 0, "status": "crashed", "detail": "boom"}]
r = w.run()
check("PULL_HEALTH_RETRY=off keeps the check and the alert but not the retry", w.calls == [] and w.sleeps == [] and len(w.sent) == 1 and "1 new issue" in w.sent[0][0], (w.calls, [s for s, _ in w.sent]))
check("... and the streak is recorded", w.state()["scrapers"]["Bravo"]["streak"] == 1)
os.environ.pop("PULL_HEALTH_RETRY")

w = World()
w.books()
w.health = [{"name": "Bravo", "rows": 0, "status": "crashed"}]
w.fix = {}
w.send_raises = True
r = w.run()
check("a mail that fails to send is not lost: the issue stays new", not r.emailed and w.state()["issues"] == [] and r.exit_code == 0, w.state())
w.send_raises = False
w.health = [{"name": "Bravo", "rows": 0, "status": "crashed"}]
r = w.run()
check("... the next run sends it", r.emailed and len(w.sent) == 1 and w.state()["issues"] == ["scraper:Bravo"], (r.emailed, w.state()))

w = World()
w.books()
w.health = [{"name": "Bravo", "rows": 0, "status": "crashed"}]
r = w.run(email=False)
check("a run without email checks and keeps the streaks, but mails nothing and leaves the issue list alone",
      not r.emailed and w.sent == [] and w.state()["issues"] == [] and w.state()["scrapers"]["Bravo"]["streak"] == 1, w.state())

w = World()
w.books(scrapers={"Bravo": {"streak": 4, "since": "2026-10-15", "last": "2026-10-20", "status": "empty"}}, issues=["scraper:Bravo"])
w.health = [{"name": "Alpha", "rows": 5, "status": "ok"}, {"name": "Bravo", "rows": 0, "status": "empty"}]
r = w.run()
check("a scraper that has failed 4 runs in a row is not retried and not mailed again", w.calls == [] and w.sleeps == [] and w.sent == [], (w.calls, w.sent))
check("... its streak grows", w.state()["scrapers"]["Bravo"]["streak"] == 5)
w.health = [{"name": "Bravo", "rows": 7, "status": "ok"}]
w.today = "2026-10-22"
w.cov = cov_with(BASE, today="2026-10-22")
r = w.run()
check("... and when it comes back that is mailed as a recovery", len(w.sent) == 1 and "recovered" in w.sent[0][0] and w.state()["issues"] == [], [s for s, _ in w.sent])

w = World(today=MONDAY)
w.books(issues=["scraper:Bravo"], scrapers={"Bravo": {"streak": 3, "since": "2026-10-14", "last": "2026-10-16", "status": "empty"}})
w.health = [{"name": "Bravo", "rows": 0, "status": "empty"}]
r = w.run()
check("Monday: the digest of what is still open goes out", len(w.sent) == 1 and "weekly digest" in w.sent[0][0], [s for s, _ in w.sent])
r = w.run()
check("... and only once that day", len(w.sent) == 1)

w = World()
w.health = [{"name": "Alpha", "rows": 5, "status": "ok"}, {"name": "Bravo", "rows": 0, "status": "empty"}]
r = w.run()
check("the first run does not retry a failure it cannot date", w.calls == [] and w.sleeps == [] and len(w.sent) == 1, (w.calls, w.sleeps))
check("... it books it as persistent, and says 'since ... or earlier'", w.state()["scrapers"]["Bravo"]["seeded"] and "failing since 2026-10-21 or earlier" in w.sent[0][1])
w.today = "2026-10-22"
w.cov = cov_with(BASE, today="2026-10-22")
w.health = [{"name": "Alpha", "rows": 5, "status": "ok"}, {"name": "Bravo", "rows": 0, "status": "empty"}]
w.sent.clear()
r = w.run()
check("the next day it is still not retried, and still quiet", w.calls == [] and w.sent == [], (w.calls, [s for s, _ in w.sent]))

w = World()
w.books()
w.health = [{"name": "Bravo", "rows": 0, "status": "crashed"}]
w.run(email=False)
w.run(email=False)
check("a second run on the same day does not count the failure twice", w.state()["scrapers"]["Bravo"]["streak"] == 1)

w = World({"Inco": 1})
w.books()
w.ok_health()
w.fix = dict(BASE)
n = iter(itertools.chain([0, 10], itertools.repeat(5000)))
r = w.run(clock=lambda: next(n), budget=1200)
check("the retry stops starting scrapers once its time budget is spent", len(w.calls) == 1 and sum(1 for x in r.retried if x["skipped"]) == 3, (w.calls, r.retried))

os.environ["PULL_HEALTH_ALERTS"] = "daily"
w = World()
w.books(issues=["scraper:Bravo"])
w.health = [{"name": "Bravo", "rows": 0, "status": "empty"}]
w.registry.pop("Bravo")
r = w.run()
check("PULL_HEALTH_ALERTS=daily mails every day something is open", len(w.sent) == 1, [s for s, _ in w.sent])
os.environ.pop("PULL_HEALTH_ALERTS")

# ── the query ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nthe coverage query (throwaway SQLite)")
database.init_db()
conn = database.get_conn()
c = conn.cursor()
for ts, prov, loc in [("2026-10-21T00:00:00Z", "P1", "a"), ("2026-10-21T00:00:00Z", "P1", "b"), ("2026-10-21T00:00:00Z", "P2", "c"),
                      ("2026-10-20T00:00:00Z", "P1", "a"), ("2026-10-22T00:00:00Z", "P1", "a"),
                      ("2026-08-30T00:00:00Z", "P3", "old"), ("2026-08-01T00:00:00Z", "P4", "ancient")]:
    c.execute("INSERT INTO snapshots (timestamp, provider, location, source) VALUES (?, ?, ?, 'test')", (ts, prov, loc))
conn.commit()
conn.close()
cov = ph.load_coverage(TODAY)
check("per day and provider, distinct locations", cov.by_day.get("2026-10-21") == {"P1": 2, "P2": 1} and cov.by_day.get("2026-10-20") == {"P1": 1}, cov.by_day)
check("a later day is not read (as of the date asked)", "2026-10-22" not in cov.by_day)
check("the per-day window is 45 days", "2026-08-30" not in cov.by_day)
check("each location's newest day, over 60 days", cov.last_seen.get(("P1", "a")) == "2026-10-21" and cov.last_seen.get(("P3", "old")) == "2026-08-30", cov.last_seen)
check("... older than that is not read", ("P4", "ancient") not in cov.last_seen)
check("a past date is read as of that date", ph.load_coverage("2026-10-20").by_day.get("2026-10-21") is None)

# ── report and backtest ────────────────────────────────────────────────────────────────────────────────────────────────────────────
print("\nreport and backtest")
txt = ph.format_report(ph.evaluate(cov_with({"Alpha": 39, "Bravo": 10, "Cinco": 3, "Delta": 20}, last_seen_extra=ls), TODAY))
check("the report names the short provider and the stale group", "Alpha  " in txt and "short" in txt and "Landus" in txt and "49" in txt, txt)
days = weekdays_before("2026-10-22", 14)
bd = {d: dict(BASE) for d in days}
bd[days[-4]] = {"Inco": 1}                          # one failed day
rows = ph.backtest(ph.Coverage(bd, {}), days[3], "2026-10-23")
st = {x["date"]: x["v"].status for x in rows}
check("the backtest judges trading days only and stops at the newest day with data", "2026-10-23" not in st and all(date.fromisoformat(d).weekday() < 5 for d in st), sorted(st))
check("it finds the failed day", st[days[-4]] == "failed", st)
em = {x["date"]: x["email"] for x in rows}
check("it would mail on the failed day and the recovery after, and not on the quiet days", em[days[-4]] and em[days[-3]] and not em[days[-2]] and not em[days[5]], em)

print()
if FAILS:
    print("FAILED: %d\n  " % len(FAILS) + "\n  ".join(FAILS))
    sys.exit(1)
print("all pull_health checks passed")
