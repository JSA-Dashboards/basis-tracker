"""audit_futures_dating.py — READ-ONLY: is each futures_prices row the settlement of its own date?

Compares every stored row with Massive's 1session bar for the same contract (matched on session_end_date), and
with the one and two sessions before, and says which session the row actually holds:

    right    = the settlement of its own date
    late1    = the settlement of the session before its date (the old capture's bug, 2026-06-22 .. 2026-10-09)
    late2    = two sessions before
    ambig    = the settlement didn't change, so its own and an earlier session both match
    close    = within a tick of its own session's close, not its settlement (a provisional row)
    nobar    = Massive has no settlement for that contract on or before the date (it did not trade)
    mismatch = matches none of them

Massive's bars start 2021-09-02, so rows before that are spot-checked against the analyst's weekly sheets
(data/rtc_futures_1996_2006.csv, data/rtc_futures_soy_2005_2007.csv) where they overlap.

    python audit_futures_dating.py --out <dir>        # writes rows.csv, by_date.csv, summary.md there

It writes nothing to the database. Bars are cached in <dir>/bars.json (delete it to refetch).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

import database as db                  # noqa: E402
import massive_futures as mf           # noqa: E402

MASSIVE_FROM = "2021-09-02"            # first session Massive's bars cover
MONTHS = "FGHJKMNQUVXZ"
TOL = 0.005                            # cents; the smallest tick stored is 0.01 (soybean oil)


def contract_bounds(symbol: str) -> tuple[date, date]:
    """A contract's plausible life: from 4 years before its delivery month to that month's end."""
    y = 2000 + int(symbol[3:5])
    m = MONTHS.index(symbol[2]) + 1
    end = date(y + (m == 12), m % 12 + 1, 1)
    return date(y - 4, 1, 1), end


def load_bars(symbols: list[str], cache: Path) -> dict[str, dict[str, dict]]:
    """{symbol: {session date iso: {'settle': x|None, 'close': y}}}, fetched once and cached."""
    have: dict = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [s for s in symbols if s not in have and mf.massive_ticker(s)]
    key = mf._api_key()

    def one(sym):
        lo, hi = contract_bounds(sym)
        out = {}
        for b in mf.fetch_bars(mf.massive_ticker(sym), key):
            d = str(b.get("session_end_date"))[:10]
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            if lo <= dd < hi:
                out[d] = {"settle": b.get("settlement_price") or None, "close": b.get("close")}
        return sym, out

    if todo:
        print(f"fetching bars for {len(todo)} contracts ...", flush=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, fut in enumerate(as_completed([pool.submit(one, s) for s in todo]), 1):
                try:
                    sym, bars = fut.result()
                    have[sym] = bars
                except Exception as exc:
                    print("  bars failed:", exc, flush=True)
                if i % 25 == 0:
                    print(f"  {i}/{len(todo)}", flush=True)
                    cache.write_text(json.dumps(have))
        cache.write_text(json.dumps(have))
    return have


def classify(d: str, price: float, bars: dict, sessions: list[str]) -> tuple[str, dict]:
    """Verdict for one row: which session's settlement it holds. `sessions` = the market's session dates."""
    import bisect
    i = bisect.bisect_right(sessions, d)       # sessions[:i] are <= d
    own = sessions[i - 1] if i and sessions[i - 1] == d else None
    prev = [s for s in sessions[max(0, i - 3):i] if s < d][-2:]
    p1 = prev[-1] if prev else None
    p2 = prev[-2] if len(prev) > 1 else None
    st = lambda s: (bars.get(s) or {}).get("settle") if s else None   # noqa: E731
    s0, s1, s2 = st(own), st(p1), st(p2)
    info = {"own_session": own or "", "settle_own": s0, "settle_prev": s1, "settle_prev2": s2,
            "close_own": (bars.get(own) or {}).get("close") if own else None}
    eq = lambda a: a is not None and abs(float(a) - price) <= TOL       # noqa: E731
    hits = [k for k, v in (("right", s0), ("late1", s1), ("late2", s2)) if eq(v)]
    if len(hits) == 1:
        return hits[0], info
    if len(hits) > 1:
        return "ambig", info
    if s0 is None and s1 is None and s2 is None:
        return "nobar", info
    c0 = info["close_own"]
    if c0 is not None and abs(float(c0) - price) <= TOL:
        return "close", info
    return "mismatch", info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="directory for the report files")
    ap.add_argument("--from", dest="date_from", default=MASSIVE_FROM)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not db._use_sf():
        print("ERROR: the backend is not Snowflake — this would audit an empty local SQLite file")
        return 2

    conn = db.get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT date, symbol, price_cents, captured_at FROM futures_prices WHERE date >= %s",
                  (args.date_from,))
        rows = [dict(r) for r in c.fetchall()]
        c.execute("SELECT date, symbol, price_cents, captured_at FROM futures_prices WHERE date < %s",
                  (args.date_from,))
        old_rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()
    print(f"{len(rows):,} rows from {args.date_from}; {len(old_rows):,} before", flush=True)

    syms = sorted({r["symbol"] for r in rows if mf.massive_ticker(r["symbol"])})
    bars = load_bars(syms, out / "bars.json")
    # The market's sessions: every date a liquid front contract has a bar for.
    sessions = sorted({d for s, b in bars.items() for d in b if s[:2] in ("ZC", "ZS")})

    def kind(cap):
        cap = str(cap or "")
        if cap.startswith("backfill:"):
            return cap
        if cap.startswith(("massive:", "adm:")):
            return "capture (fixed)"
        if cap.startswith("repair:"):
            return "repair (from Massive bars)"
        return "daily capture (old)"

    detail = []
    for r in rows:
        d = str(r["date"])[:10]
        sym = r["symbol"]
        k = kind(r["captured_at"])
        if not mf.massive_ticker(sym):
            v, info = "not-on-massive", {}
        else:
            v, info = classify(d, float(r["price_cents"]), bars.get(sym, {}), sessions)
        detail.append({"date": d, "symbol": sym, "price_cents": r["price_cents"], "kind": k,
                       "captured_at": r["captured_at"], "verdict": v, **info})
    detail.sort(key=lambda x: (x["date"], x["symbol"]))

    with open(out / "rows.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "symbol", "price_cents", "kind", "captured_at", "verdict",
                                          "own_session", "settle_own", "settle_prev", "settle_prev2", "close_own"])
        w.writeheader()
        w.writerows(detail)

    by_date = defaultdict(Counter)
    kinds_by_date = defaultdict(Counter)
    for x in detail:
        by_date[x["date"]][x["verdict"]] += 1
        kinds_by_date[x["date"]][x["kind"]] += 1
    verdicts = ["right", "late1", "late2", "ambig", "close", "nobar", "mismatch", "not-on-massive"]
    with open(out / "by_date.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "kind", "rows", "is_session", *verdicts, "holds"])
        sset = set(sessions)
        for d in sorted(by_date):
            cnt = by_date[d]
            decisive = {k: cnt[k] for k in ("right", "late1", "late2")}
            holds = max(decisive, key=decisive.get) if any(decisive.values()) else "-"
            w.writerow([d, "/".join(sorted(kinds_by_date[d])), sum(cnt.values()), d in sset,
                        *[cnt[k] for k in verdicts], holds])

    # ── Summary ──────────────────────────────────────────────────────────────
    lines = [f"# futures_prices dating audit — {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Rows checked: {len(detail):,} from {args.date_from} (Massive bars cover {MASSIVE_FROM} on).", ""]
    for k in sorted({x['kind'] for x in detail}):
        sub = [x for x in detail if x["kind"] == k]
        cnt = Counter(x["verdict"] for x in sub)
        dates = sorted({x["date"] for x in sub})
        lines.append(f"## {k}: {len(sub):,} rows, {len(dates):,} dates ({dates[0]} .. {dates[-1]})")
        lines.append("")
        lines.append("| verdict | rows |")
        lines.append("|---|---:|")
        for v in verdicts:
            if cnt[v]:
                lines.append(f"| {v} | {cnt[v]:,} |")
        holds = Counter()
        for d in dates:
            cntd = Counter(x["verdict"] for x in sub if x["date"] == d)
            dec = {v: cntd[v] for v in ("right", "late1", "late2")}
            holds[max(dec, key=dec.get) if any(dec.values()) else "-"] += 1
        lines.append("")
        lines.append("Dates by the session they mostly hold: " +
                     ", ".join(f"{v} {n}" for v, n in holds.most_common()))
        lines.append("")
        if k.startswith("backfill"):
            by_year = defaultdict(Counter)
            for x in sub:
                by_year[x["date"][:4]][x["verdict"]] += 1
            lines.append("| year | right | late1 | late2 | ambig | nobar | mismatch |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for y in sorted(by_year):
                cy = by_year[y]
                lines.append(f"| {y} | {cy['right']:,} | {cy['late1']:,} | {cy['late2']:,} | {cy['ambig']:,} "
                             f"| {cy['nobar']:,} | {cy['mismatch']:,} |")
            lines.append("")
            mm = [x for x in sub if x["verdict"] in ("late1", "late2", "mismatch")]
            if mm:
                lines.append(f"First {min(15, len(mm))} of {len(mm)} rows that are not their own date's settlement:")
                lines.append("")
                for x in mm[:15]:
                    lines.append(f"- {x['date']} {x['symbol']} {x['price_cents']} -> {x['verdict']} "
                                 f"(own {x['settle_own']}, prev {x['settle_prev']}, prev2 {x['settle_prev2']})")
                lines.append("")

    # ── Before Massive: the analyst's weekly sheets ──────────────────────────
    ref = {}
    for name in ("rtc_futures_1996_2006.csv", "rtc_futures_soy_2005_2007.csv"):
        p = HERE / "data" / name
        if p.exists():
            for r in csv.DictReader(open(p, encoding="utf-8")):
                ref[(r["date"], r["symbol"])] = float(r["price_cents"])
    old = {(str(r["date"])[:10], r["symbol"]): float(r["price_cents"]) for r in old_rows}
    old_dates = defaultdict(dict)
    for (d, s), p in old.items():
        old_dates[s][d] = p
    same = moved = differ = 0
    examples = []
    for (d, s), p in sorted(ref.items()):
        if (d, s) not in old:
            continue
        if abs(old[(d, s)] - p) <= TOL:
            same += 1
            continue
        # does the sheet's Wednesday price sit on the stored Tuesday or Thursday?
        near = {dd: v for dd, v in old_dates[s].items() if abs((date.fromisoformat(dd) - date.fromisoformat(d)).days) <= 3}
        hit = [dd for dd, v in near.items() if abs(v - p) <= TOL]
        if hit:
            moved += 1
        else:
            differ += 1
        if len(examples) < 10:
            examples.append(f"- {d} {s}: sheet {p}, stored {old[(d, s)]}" + (f" (sheet value stored on {hit})" if hit else ""))
    lines.append("## Archive rows before Massive (spot check vs the analyst's weekly sheets)")
    lines.append("")
    lines.append(f"{same + moved + differ} overlapping (date, contract) pairs: {same} equal, {moved} equal to a stored "
                 f"neighbouring day's, {differ} different.")
    lines.append("")
    lines.extend(examples)
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
