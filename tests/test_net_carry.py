"""Net Carry interest must be the Cost of Carry sheet's formula, at its rate.

    python tests/test_net_carry.py

The oracle is cost-of-carry-calculator/app.py, compute_carry_table():
    interest_full = near["price"] * annual_rate * days / 360     (days = actual calendar days)
    annual_rate   = fed funds + 2.25%                            (FED_FUNDS_SPREAD_PCT)
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carry_rate as cr   # noqa: E402
import net_carry as nc    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-9):
    return a is not None and abs(a - b) <= tol


print("carry_rate: parsing and lookups")
ds, vs = cr.parse_dff("observation_date,DFF\n2026-09-29,3.88\n2026-09-30,.\n2026-10-01,3.88\nbad,row\n2026-09-28,3.63\n")
check("parse drops the header, '.' and junk rows and sorts", ds == [date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1)]
      and vs == [3.63, 3.88, 3.88], (ds, vs))
ff = cr.FedFunds(ds, vs, "fred")
check("on(): exact day", ff.on(date(2026, 9, 29)) == (date(2026, 9, 29), 3.88))
check("on(): a missing day carries the previous value forward", ff.on(date(2026, 9, 30)) == (date(2026, 9, 29), 3.88))
check("on(): a date past the end carries the last value forward", ff.on(date(2026, 12, 25)) == (date(2026, 10, 1), 3.88))
check("on(): a date before the first observation is None", ff.on(date(2026, 1, 1)) is None)

r = cr.rate_for(date(2026, 10, 2), ff)
check("rate_for = fed funds + 2.25", close(r.rate_pct, 3.88 + 2.25) and r.fed_funds_pct == 3.88 and r.source == "fred", r)
r = cr.rate_for(date(2026, 9, 28), ff)
check("rate_for on an older date uses THAT day's fed funds", close(r.rate_pct, 3.63 + 2.25), r)
r = cr.rate_for(date(2026, 10, 2), cr.FedFunds([], [], "none"))
check("no data -> the Cost of Carry fallback (5.89%)", r.rate_pct == 5.89 and r.source == "fallback" and r.fed_funds_pct is None, r)
r = cr.rate_for(date(2026, 1, 1), ff)
check("date before the data -> fallback", r.source == "fallback")

print("carry_rate: offline fallback to the committed snapshot")
_real_get = cr.requests.get


def _boom(*a, **k):
    raise ConnectionError("simulated: FRED unreachable")


cr.requests.get = _boom
try:
    snap = cr.load_fed_funds()
finally:
    cr.requests.get = _real_get
check("FRED down -> snapshot is used", snap.source == "snapshot" and len(snap) > 7000, (snap.source, len(snap)))
check("snapshot is recent enough to be useful", snap.dates[-1] >= date(2026, 10, 1), snap.dates[-1])
hit = snap.on(date(2026, 10, 2))
check("snapshot gives 3.88% fed funds -> 6.13% carry rate for 2026-10-02",
      hit is not None and close(cr.rate_for(date(2026, 10, 2), snap).rate_pct, 6.13), hit)

print("net_carry: interest = price x rate x days / 360, actual days from the anchor month")
CURVE = {"ZCZ26": 440.0, "ZCH27": 455.0, "ZCK27": 460.0, "ZCN27": 463.0}
ITEMS = [
    {"delivery": "Sep", "futures": "ZCZ26", "basis": -20},    # before the Oct anchor
    {"delivery": "Oct", "futures": "ZCZ26", "basis": -10},    # the anchor month itself
    {"delivery": "Nov", "futures": "ZCZ26", "basis": -5},
    {"delivery": "Dec", "futures": "ZCZ26", "basis": 0},
    {"delivery": "Jan", "futures": "ZCH27", "basis": 3},
    {"delivery": "Feb", "futures": "ZCH27", "basis": 6},
    {"delivery": "Mar", "futures": "ZCH27", "basis": 9},
]
RATE = 0.0613
rows, meta = nc.compute_net_carry(ITEMS, "ZCZ26", CURVE, 10, RATE)
by = {r.delivery: r for r in rows}
ANCHOR = date(2026, 10, 1)
for deliv, (y, m) in {"Nov": (2026, 11), "Dec": (2026, 12), "Jan": (2027, 1), "Feb": (2027, 2), "Mar": (2027, 3)}.items():
    days = (date(y, m, 1) - ANCHOR).days
    oracle = CURVE["ZCZ26"] * RATE * days / 360          # the Cost of Carry line, verbatim
    check("%s: %d days -> %.4f c" % (deliv, days, oracle), by[deliv].days == days and close(by[deliv].interest, oracle),
          (by[deliv].days, by[deliv].interest, oracle))
check("Jan is 92 days from Oct 1", by["Jan"].days == 92)
check("Oct (the anchor) and Sep (before it) carry no interest", by["Oct"].interest == 0.0 and by["Sep"].interest == 0.0
      and by["Oct"].days == 0 and by["Sep"].days == 0)
check("30-day 'Monthly interest' = price x rate x 30/360 (= price x rate/12)",
      close(meta["per_month"], 440.0 * RATE * 30 / 360) and close(meta["per_month"], 440.0 * RATE / 12))

print("net_carry: net / carry columns still follow from it")
dec, jan = by["Dec"], by["Jan"]
check("net = basis vs REF - interest", close(dec.net, dec.basis_ref - dec.interest) and close(jan.net, jan.basis_ref - jan.interest))
check("Jan is spread-converted to ZCZ26 (basis 3 + (455 - 440))", close(jan.basis_ref, 3 + (455.0 - 440.0)), jan.basis_ref)
check("carry = prior delivery's net - this net", close(jan.carry, dec.net - jan.net))

print("net_carry: the rate matters, and it is lower than the old 9%")
hi, _ = nc.compute_net_carry(ITEMS, "ZCZ26", CURVE, 10, 0.09)
h = {r.delivery: r for r in hi}
check("same days, interest scales with the rate (6.13% vs 9%)", close(by["Mar"].interest / h["Mar"].interest, RATE / 0.09))
check("at 6.13% the Oct->Mar interest is well under the old 9% figure", by["Mar"].interest < 0.7 * h["Mar"].interest)
r0, m0 = nc.compute_net_carry(ITEMS, None, CURVE, 10, RATE)
check("no reference price -> interest is None, net falls back to the raw basis, no crash",
      all(x.interest is None for x in r0) and m0["per_month"] is None)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
