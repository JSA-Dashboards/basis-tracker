#!/usr/bin/env python3
"""
post_rail.py — post a rail-corridor rundown server-side (the droplet) AND fire the
rail update email, in one step.

Mirrors the admin app's Rail Entry tab (parse -> save -> email) so a server-side
post can't silently skip the email the way a raw DB write does. Rundown text comes
from --file or stdin. Previews by default; --commit writes.

    # eyeball the parse (no DB write, no email):
    cat rundown.txt | python post_rail.py --date 2026-10-02 --commodity Corn

    # write it and email the basis corridors (To: kpostin, BCC the JSA group):
    cat rundown.txt | python post_rail.py --date 2026-10-02 --commit

Flags:
    --date YYYY-MM-DD   posting date (default: today, US Central — the droplet clock is UTC, which is already tomorrow after ~7 PM CT)
    --commodity NAME    Corn | Soybeans | Wheat | Sorghum  (default: Corn)
    --file PATH         read the rundown from PATH instead of stdin
    --commit            actually save (default is preview only)
    --no-email          save but do NOT send the rail update email
    --email-freight     include freight/shuttle corridors in the email
                        (normally excluded, exactly like the app)
    --to ADDR           override the To: (default kpostin@jpsi.com; the email
                        always BCCs the JSA group via rail_report)

On --commit each posted (date, 'manual', market) is replaced wholesale (delete
then insert) so re-running a corrected rundown never leaves stale period rows
behind — save_rail_fob only upserts, it does not clear the day.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as _date

import rail_paste as rp
import database as db
from rail_corridors import RAIL_BY_CORRIDOR, today_ct

# Fallback rail from the corridor's first word, same map the app uses when a
# corridor isn't in RAIL_BY_CORRIDOR.
_RAIL_FALLBACK = {"CSX": "CSX", "NS": "NS", "CN": "CN", "UP": "UP", "BN": "BNSF"}


def _is_freight(corr: str) -> bool:
    return ("Freight" in corr) or ("Shuttle" in corr)


def _rail_of(corr: str):
    return RAIL_BY_CORRIDOR.get(corr) or _RAIL_FALLBACK.get(corr.split()[0].upper())


def _pn(v):
    """None/blank -> None; anything numeric -> rounded int (cents, or $/car for freight)."""
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def build_corridor_rows(parsed, commodity):
    """Group parse_multi output by corridor into save_rail_fob row dicts, exactly
    like the app's Rail Entry save (including the freight Spot=Return Trip row)."""
    by_corr: dict[str, list] = {}
    for r in parsed:
        corr = str(r.get("market") or "").strip()
        per = str(r.get("period") or "").strip()
        if not corr or not per:
            continue
        by_corr.setdefault(corr, []).append(r)

    out_by_corr: dict[str, list] = {}
    for corr, rrows in by_corr.items():
        rail = _rail_of(corr)
        out = []
        for i, r in enumerate(rrows):
            out.append({
                "market": corr, "rail": rail,
                "commodity": (str(r.get("commodity") or commodity).strip() or commodity),
                "period": str(r["period"]).strip(), "period_order": i,
                "futures": (str(r.get("futures") or "").strip() or None),
                "bid": _pn(r.get("bid")), "offer": _pn(r.get("offer")),
                "bid_raw": "?" if r.get("bid_raw") == "?" else None,
                "offer_raw": "?" if r.get("offer_raw") == "?" else None,
            })
        if _is_freight(corr):
            # Freight is $/car with no futures (all existing freight history is null,
            # and the board/email render freight without a futures ref). parse_multi
            # guesses a contract from the month name, so strip it on freight rows.
            for x in out:
                x["futures"] = None
            rt = next((x for x in out if x["period"].strip().lower() == "return trip"), None)
            if rt and not any(x["period"].strip().lower() == "spot" for x in out):
                out.append({**rt, "period": "Spot", "period_order": 0, "futures": None})
        out_by_corr[corr] = out
    return out_by_corr


def _delete_markets_day(date_s: str, markets: list[str]) -> None:
    """Clear every (date, 'manual', market) in one connection before reinsert."""
    if not markets:
        return
    conn = db.get_conn(); c = conn.cursor(); ph = db._ph()
    try:
        for m in markets:
            c.execute(f"DELETE FROM rail_fob WHERE date={ph} AND source={ph} AND market={ph}",
                      (date_s, "manual", m))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Post a rail rundown + fire the rail update email.")
    ap.add_argument("--date", default=today_ct().isoformat())
    ap.add_argument("--commodity", default="Corn")
    ap.add_argument("--file")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--email-freight", action="store_true")
    ap.add_argument("--to", default=None)
    args = ap.parse_args()

    try:
        y, m, d = (int(x) for x in args.date.split("-"))
        as_of = _date(y, m, d)
    except Exception:
        ap.error(f"--date must be YYYY-MM-DD (got {args.date!r})")
    date_s = as_of.isoformat()

    text = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    if not text.strip():
        ap.error("no rundown text (pass --file or pipe it on stdin)")

    parsed, warnings = rp.parse_multi(text, as_of=as_of, commodity=args.commodity)
    out_by_corr = build_corridor_rows(parsed, args.commodity)

    corrs = list(out_by_corr)
    basis = [c for c in corrs if not _is_freight(c)]
    freight = [c for c in corrs if _is_freight(c)]

    print(f"=== parsed {date_s} · {args.commodity} ===")
    if not corrs:
        print("  (nothing parsed — start each block with a corridor name)")
    for corr in corrs:
        tag = "FREIGHT" if _is_freight(corr) else "basis"
        print(f"\n[{tag}] {corr}  (rail={_rail_of(corr)})")
        for r in out_by_corr[corr]:
            fut = r["futures"] or "-"
            bd = "?" if r["bid_raw"] == "?" else r["bid"]
            of = "?" if r["offer_raw"] == "?" else r["offer"]
            print(f"    {r['period']:<24} {fut:<8} bid={bd} offer={of}")
    if warnings:
        print("\n--- warnings ---")
        for w in warnings:
            print("  ! " + w)

    email_markets = (basis + freight) if args.email_freight else basis
    print("\n=== plan ===")
    print(f"  corridors: {len(corrs)}  (basis={len(basis)}, freight={len(freight)})")
    if not args.commit:
        print("  PREVIEW ONLY — nothing saved, nothing emailed. Re-run with --commit to write.")
        return 0
    if args.no_email or not email_markets:
        print("  email: (none)")
    else:
        print(f"  email: {', '.join(email_markets)}  ->  {args.to or 'kpostin@jpsi.com'} + BCC JSA group")

    # --- commit ---
    _delete_markets_day(date_s, corrs)
    total = 0
    for corr, rows in out_by_corr.items():
        total += db.save_rail_fob(date_s, "manual", rows)
    print(f"\nsaved {total} rows across {len(corrs)} corridor(s) for {date_s}.")

    if args.no_email:
        try:                                         # on purpose: tell the droplet catch-up job (rail_email_watch.py) to leave it alone
            import rail_email_log as _rel
            _rel.mark_handled(basis, "skipped")
            print("email skipped (--no-email); logged as intentional so the catch-up job leaves it alone.")
        except Exception as exc:                     # noqa: BLE001
            print(f"email skipped (--no-email); could NOT log it as intentional ({exc}) - the catch-up job may email it.")
    elif not email_markets:
        print("email skipped (no basis corridors; pass --email-freight to email freight).")
    else:
        import rail_report as rr
        ok = rr.send_rail_update_email(markets=email_markets, to_addr=args.to)
        print(f"rail update email sent={ok} for: {', '.join(email_markets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
