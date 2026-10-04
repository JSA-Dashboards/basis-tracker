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

print("net_carry: futures-spread credit and the FRONT delivery's futures as the reference")
CURVE2 = {"ZCU26": 470.0, "ZCZ26": 440.0, "ZCH27": 455.0, "ZCK27": 460.0}
AUG = [
    {"delivery": "Aug", "futures": "ZCU26", "basis": 20},      # the FRONT, quoted off Sep
    {"delivery": "Sep", "futures": "ZCU26", "basis": 15},
    {"delivery": "Oct", "futures": "ZCZ26", "basis": -10},
    {"delivery": "Dec", "futures": "ZCZ26", "basis": 0},
    {"delivery": "Jan", "futures": "ZCH27", "basis": 3},
]
AS_OF = date(2026, 8, 3)
check("front_symbol = the contract the nearest delivery is quoted off", nc.front_symbol(AUG, CURVE2) == "ZCU26")
check("reference_symbol('front') uses it", nc.reference_symbol("Corn", "front", CURVE2, AS_OF, items=AUG) == "ZCU26")
check("reference_symbol('newcrop') is still Dec", nc.reference_symbol("Corn", "newcrop", CURVE2, AS_OF, items=AUG) == "ZCZ26")
check("front_symbol skips a front contract that has no price",
      nc.front_symbol([{"delivery": "Aug", "futures": "ZCQ26", "basis": 1},
                       {"delivery": "Sep", "futures": "ZCU26", "basis": 2}], CURVE2) == "ZCU26")
check("front_symbol with nothing priced -> None", nc.front_symbol([{"delivery": "Aug", "futures": "ZCQ26", "basis": 1}], CURVE2) is None
      and nc.front_symbol([], CURVE2) is None)
check("no items -> 'front' falls back to the nearest active outright (the old behaviour)",
      nc.reference_symbol("Corn", "front", CURVE2, AS_OF) == "ZCU26")

rf, mf = nc.compute_net_carry(AUG, "ZCU26", CURVE2, 10, 0.0)      # reference = the front's contract
bf = {r.delivery: r for r in rf}
check("front reads exactly as quoted: credit 0, basis vs REF == quoted", bf["Aug"].credit == 0.0 and close(bf["Aug"].basis_ref, 20))
check("Oct (quoted off Dec): credit = 440 - 470 = -30 -> -10 + -30 = -40", close(bf["Oct"].credit, -30) and close(bf["Oct"].basis_ref, -40))
check("Jan (quoted off Mar): credit = 455 - 470 = -15 -> 3 + -15 = -12", close(bf["Jan"].credit, -15) and close(bf["Jan"].basis_ref, -12))
check("quoted basis + futures spread = basis vs REF, on every row", all(close(r.raw_basis + r.credit, r.basis_ref) for r in rf))

rn, mn = nc.compute_net_carry(AUG, "ZCZ26", CURVE2, 10, 0.0)      # reference = new-crop Dec
bn = {r.delivery: r for r in rn}
check("vs new-crop the FRONT is the one that gets credited: 470 - 440 = +30 -> 50", close(bn["Aug"].credit, 30) and close(bn["Aug"].basis_ref, 50))
shift = {d: bf[d].basis_ref - bn[d].basis_ref for d in bf}
check("changing the reference shifts every row by the SAME constant (F(ref_new) - F(ref_front) = -30)",
      all(close(v, -30.0) for v in shift.values()), shift)
check("so the carry BETWEEN deliveries is identical under either reference (rate 0)",
      all(close(a.carry, b.carry) for a, b in zip(rf, rn) if a.carry is not None))

ru, mu = nc.compute_net_carry(AUG + [{"delivery": "Nov", "futures": "ZCX26", "basis": 5}], "ZCU26", CURVE2, 10, 0.0)
bu = {r.delivery: r for r in ru}
check("a contract with no price in the curve: credit None, raw basis, row flagged, no crash",
      bu["Nov"].credit is None and not bu["Nov"].converted and close(bu["Nov"].basis_ref, 5) and not mu["all_converted"])

print("net_carry: NC / New Crop is the front and the carry anchor (ADM St. Louis, 2026-10-02 rows)")
check("is_new_crop: NC, N/C, NC 26, nc26, New Crop, New Crop 2026",
      all(nc.is_new_crop(s) for s in ("NC", "N/C", "NC 26", "nc26", "New Crop", "New Crop 2026", " NC  ")))
check("is_new_crop: NOT a label that names a month, or just starts with 'nc'",
      not any(nc.is_new_crop(s) for s in ("NC Nov", "New Crop Dec", "Nov 26", "Oct", "NCR", "Nov NC", "")))
check("_new_crop_year: from the label, else the futures marketing year",
      nc._new_crop_year("NC 26", None) == 2026 and nc._new_crop_year("New Crop 2026", None) == 2026
      and nc._new_crop_year("NC", "ZCZ26") == 2026 and nc._new_crop_year("NC", "ZCH27") == 2026
      and nc._new_crop_year("NC", None) is None)

STL = [   # in the order the database returned them
    {"delivery": "Dec 26", "futures": "ZCZ26", "basis": 19}, {"delivery": "Feb 27", "futures": "ZCH27", "basis": 18},
    {"delivery": "April '27", "futures": "ZCK27", "basis": 23}, {"delivery": "June 2027", "futures": "ZCN27", "basis": 27},
    {"delivery": "NC 26", "futures": "ZCZ26", "basis": -25}, {"delivery": "Nov 26", "futures": "ZCZ26", "basis": 3},
    {"delivery": "Jan 27", "futures": "ZCH27", "basis": 14}, {"delivery": "Mar 27", "futures": "ZCH27", "basis": 21},
    {"delivery": "May '27", "futures": "ZCK27", "basis": 27}, {"delivery": "July 2027", "futures": "ZCN27", "basis": 30},
]
STL_CURVE = {"ZCZ26": 502.25, "ZCH27": 516.75, "ZCK27": 523.75, "ZCN27": 528.0}
rs, ms = nc.compute_net_carry(STL, "ZCZ26", STL_CURVE, 10, RATE)
order = [r.delivery for r in rs]
check("NC 26 is the FIRST row (it used to land third, between Dec and Jan)", order[0] == "NC 26" and rs[0].new_crop, order)
check("then Nov, Dec, Jan ... in time order", order[1:4] == ["Nov 26", "Dec 26", "Jan 27"], order)
check("NC is the anchor: Oct 2026, interest 0, 0 days", rs[0].ym == (2026, 10) and rs[0].interest == 0.0 and rs[0].days == 0)
check("Nov is 31 days after the anchor", rs[1].days == 31 and close(rs[1].interest, 502.25 * RATE * 31 / 360))
check("the carry anchor reported is Oct 2026", ms["anchor_ym"] == (2026, 10), ms["anchor_ym"])
check("the front's contract is NC's (ZCZ26)", nc.front_symbol(STL, STL_CURVE) == "ZCZ26")
check("NC -> Nov carry = NC net - Nov net", close(rs[1].carry, rs[0].net - rs[1].net))

rn, _ = nc.compute_net_carry(STL, "ZCZ26", STL_CURVE, 11, RATE)      # anchor moved to November
check("anchor = Nov: NC sits at Nov 2026 and still sorts ahead of the explicit 'Nov 26'",
      [r.delivery for r in rn][:2] == ["NC 26", "Nov 26"] and rn[0].ym == (2026, 11))
rb, _ = nc.compute_net_carry(STL + [{"delivery": "Sep 26", "futures": "ZCZ26", "basis": 0}], "ZCZ26", STL_CURVE, 10, RATE)
check("an explicit month BEFORE the anchor (Sep) is still ahead of NC, with no interest",
      [r.delivery for r in rb][:2] == ["Sep 26", "NC 26"] and rb[0].interest == 0.0)
rk, mk = nc.compute_net_carry([{"delivery": "NC", "futures": None, "basis": 1}, {"delivery": "Nov", "futures": "ZCZ26", "basis": 2}],
                              "ZCZ26", STL_CURVE, 10, RATE)
check("an NC with no year and no futures can't be placed: set aside, not mis-sorted", "NC" in mk["skipped"] and [r.delivery for r in rk] == ["Nov"])
rsoy, _ = nc.compute_net_carry(
    [{"delivery": "NC 26", "futures": "ZSX26", "basis": 3}, {"delivery": "Nov 26", "futures": "ZSX26", "basis": 28},
     {"delivery": "Dec 26", "futures": "ZSF27", "basis": 27}], "ZSX26", {"ZSX26": 1284.0, "ZSF27": 1290.0}, 10, RATE)
check("soybeans: NC 26 is first too (it used to land on Nov)", [r.delivery for r in rsoy] == ["NC 26", "Nov 26", "Dec 26"])

print("net_carry: monthly_carry — one point per calendar month (its BEST slot), each vs the previous quoted month")
mc = nc.monthly_carry(rs)
check("STL: 10 months -> 10 points, NC first", [p["label"] for p in mc][:4] == ["NC 26", "Nov 26", "Dec 26", "Jan 27"] and len(mc) == 10, [p["label"] for p in mc])
check("the first point has no carry; the rest compare to the previous point",
      mc[0]["carry"] is None and all(close(mc[i]["carry"], mc[i - 1]["net"] - mc[i]["net"]) for i in range(1, len(mc))))
check("on a monthly-quoted plant the chart equals the table's carry column", all(close(p["carry"], r.carry) for p, r in zip(mc[1:], rs[1:])))
check("each point says what it was compared to", mc[1]["vs"] == "NC 26" and mc[2]["vs"] == "Nov 26")
check("each point carries what the charts and the table need: row, slots, basis vs REF, interest, net",
      all(p["slots"] == 1 and rs[p["row"]].net == p["net"] and rs[p["row"]].basis_ref == p["basis_ref"]
          and rs[p["row"]].interest == p["interest"] for p in mc))

WK = []
for mon, fut, base in (("Nov", "ZCZ26", -19), ("Dec", "ZCZ26", -12), ("Jan", "ZCH27", -22)):
    for w in (1, 2, 3, 4):
        WK.append({"delivery": "%s Wk %d" % (mon, w), "futures": fut, "basis": base + w})
rw, _ = nc.compute_net_carry(WK, "ZCZ26", {"ZCZ26": 528.25, "ZCH27": 542.0}, 10, RATE)
mw = nc.monthly_carry(rw)
check("weekly plant: 12 slot rows collapse to 3 monthly points (not 12)", len(rw) == 12 and [p["label"] for p in mw] == ["Nov 26", "Dec 26", "Jan 27"])
check("each month is represented by its BEST slot (the basis climbs through the month: Wk 4)",
      [p["delivery"] for p in mw] == ["Nov Wk 4", "Dec Wk 4", "Jan Wk 4"] and [p["slots"] for p in mw] == [4, 4, 4], [p["delivery"] for p in mw])
check("`row` points at that slot in the table", [p["row"] for p in mw] == [3, 7, 11] and all(rw[p["row"]].net == p["net"] for p in mw))
check("Dec vs Nov uses those best-slot nets", close(mw[1]["carry"], rw[3].net - rw[7].net))
FH = [{"delivery": "Oct 26 - LH", "futures": "ZCZ26", "basis": -70}, {"delivery": "Oct 26 - FH", "futures": "ZCZ26", "basis": -75},
      {"delivery": "Nov 26 - FH", "futures": "ZCZ26", "basis": -58}, {"delivery": "Nov 26 - LH", "futures": "ZCZ26", "basis": -58}]
rf2, _ = nc.compute_net_carry(FH, "ZCZ26", CURVE, 10, RATE)
mf2 = nc.monthly_carry(rf2)
check("half-month plant: the table lists FH ahead of LH even if LH came first", rf2[0].delivery == "Oct 26 - FH" and rf2[1].delivery == "Oct 26 - LH")
check("Oct picks the better slot (LH -70 beats FH -75); Nov's two slots tie, so the NEARER one (FH) wins",
      [p["delivery"] for p in mf2] == ["Oct 26 - LH", "Nov 26 - FH"], [p["delivery"] for p in mf2])
gap = nc.monthly_carry(nc.compute_net_carry([{"delivery": "Nov", "futures": "ZCZ26", "basis": 0},
                                              {"delivery": "Feb", "futures": "ZCH27", "basis": 9}], "ZCZ26", CURVE, 10, RATE)[0])
check("a month that isn't quoted: the bar compares to the last month that was (Feb vs Nov)", gap[1]["vs"] == "Nov 26", gap)
check("no rows -> no points", nc.monthly_carry([]) == [])

print("net_carry: top_of_net_carry — the highest Net of Interest, from the carry start on")
CSX = [   # CSX Columbus, 2026-10-02 (a whole-month quote sits beside half-month ones: the old 'nearest slot' missed Dec)
    {"delivery": "FH Oct", "futures": "ZCZ26", "basis": -10}, {"delivery": "LH Oct/FH Nov", "futures": "ZCZ26", "basis": -17},
    {"delivery": "LH Oct", "futures": "ZCZ26", "basis": -17}, {"delivery": "FH Nov", "futures": "ZCZ26", "basis": -17},
    {"delivery": "Nov", "futures": "ZCZ26", "basis": 16}, {"delivery": "FH Dec", "futures": "ZCH27", "basis": 8},
    {"delivery": "Dec", "futures": "ZCH27", "basis": 14}, {"delivery": "JFM", "futures": "ZCH27", "basis": 17}]
CSX_CURVE = {"ZCZ26": 502.25, "ZCH27": 516.75}
rc, mcx = nc.compute_net_carry(CSX, "ZCZ26", CSX_CURVE, 10, RATE)
pc = nc.monthly_carry(rc)
tc = nc.top_of_net_carry(pc, mcx["anchor_ym"])
best_in_table = max(r.net for r in rc if r.ym >= mcx["anchor_ym"])
check("CSX Columbus: the top is the highest Net of Interest ANYWHERE in the table (Dec, not JFM)",
      tc is not None and close(tc["net"], best_in_table) and tc["label"] == "Dec 26" and rc[tc["row"]].delivery == "Dec", tc)
check("...and the monthly point for that month is the whole-month 'Dec' quote, one of 2 slots", tc["delivery"] == "Dec" and tc["slots"] == 2)
check("it is not the front and not the last month; the next month (JFM) gives back its net difference",
      not tc["is_front"] and not tc["is_last"] and tc["next_label"] == "Mar 27" and close(tc["give_back"], rc[-1].net - rc[tc["row"]].net) and tc["give_back"] < 0)
check("x is the index into the monthly points", pc[tc["x"]]["label"] == "Dec 26")
check("gain = peak - the first month in range (the Oct anchor)", close(tc["gain"], tc["net"] - pc[0]["net"]) and tc["front_label"] == "Oct 26")

# the promise, over every dataset above: what is highlighted == the highest Net of Interest the table shows
for name, items, ref, cv in (("ITEMS", ITEMS, "ZCZ26", CURVE), ("AUG", AUG, "ZCU26", CURVE2), ("STL", STL, "ZCZ26", STL_CURVE),
                             ("WK", WK, "ZCZ26", {"ZCZ26": 528.25, "ZCH27": 542.0}), ("FH", FH, "ZCZ26", CURVE), ("CSX", CSX, "ZCZ26", CSX_CURVE)):
    rr, mm = nc.compute_net_carry(items, ref, cv, 10, RATE)
    tt = nc.top_of_net_carry(nc.monthly_carry(rr), mm["anchor_ym"])
    want = max(r.net for r in rr if r.ym >= mm["anchor_ym"])
    check("%s: top net == the table's highest Net of Interest from the anchor on (%.2f)" % (name, want),
          tt is not None and close(tt["net"], want) and close(rr[tt["row"]].net, want), tt)

top_sty = nc.top_of_net_carry(mc, (2026, 10))
check("STL: every month's carry is negative, so the top is the LAST month (Jul 27) and it says so",
      top_sty["label"] == "Jul 27" and top_sty["is_last"] and not top_sty["is_front"] and top_sty["give_back"] is None
      and close(top_sty["net"], rs[-1].net) and top_sty["next_label"] is None)


def pt(i, net, label=None, **kw):
    ym = (2026 + (9 + i) // 12, (9 + i) % 12 + 1)
    d = {"ym": ym, "label": label or "%s %02d" % (nc._ABBR[ym[1]], ym[0] % 100), "delivery": label or "x", "new_crop": False,
         "slots": 1, "row": i, "basis_ref": net, "interest": 0.0, "net": net}
    d.update(kw)
    return d


ANC = (2026, 10)
check("no points / nothing at or after the anchor -> None", nc.top_of_net_carry([], ANC) is None
      and nc.top_of_net_carry([pt(0, 5.0)], (2027, 1)) is None)
falling = [pt(0, -10.0), pt(1, -13.2), pt(2, -20.0)]
tf = nc.top_of_net_carry(falling, ANC)
check("a curve that only falls: the top is the front and it says carrying does not pay",
      tf["is_front"] and tf["label"] == "Oct 26" and close(tf["give_back"], -3.2)
      and "does not pay" in nc.top_detail(tf) and "3.2" in nc.top_detail(tf), nc.top_summary(tf))
check("the front's headline", nc.top_headline(tf) == "Top of net carry: Oct 26 at -10.0¢ net of interest", nc.top_headline(tf))
flat = nc.top_of_net_carry([pt(0, 4.0), pt(1, 4.0), pt(2, 1.0)], ANC)
check("a tie goes to the EARLIEST month", flat["label"] == "Oct 26" and flat["is_front"])
check("flat into the next month: 'stays flat', not 'falls 0.0'", "stays flat into Nov 26" in nc.top_detail(flat), nc.top_detail(flat))
mid = nc.top_of_net_carry([pt(0, -11.0), pt(1, 20.0), pt(2, 37.5), pt(3, 35.1)], ANC)
check("a peak in the middle: gain over the start and what carrying past it gives back",
      mid["label"] == "Dec 26" and close(mid["gain"], 48.5) and close(mid["give_back"], -2.4) and mid["next_label"] == "Jan 27"
      and nc.top_summary(mid) == "Top of net carry: Dec 26 at +37.5¢ net of interest. 48.5¢ above Oct 26 (-11.0¢), where the carry starts. "
                                 "Carrying past it gives back 2.4¢ by Jan 27.", nc.top_summary(mid))
rising = nc.top_of_net_carry([pt(0, -11.0), pt(1, 20.0)], ANC)
check("a curve that rises to the last month", rising["is_last"] and "still paying to carry through the last quoted month" in nc.top_detail(rising))
only = nc.top_of_net_carry([pt(0, 3.0)], ANC)
check("a single month", only["is_front"] and only["is_last"] and "only month on the curve" in nc.top_detail(only))
pre = nc.top_of_net_carry([pt(-2, 99.0), pt(-1, 80.0), pt(0, -5.0), pt(1, -7.0)], ANC)
check("months BEFORE the carry start never win (they carry no interest)", pre["label"] == "Oct 26" and close(pre["net"], -5.0) and pre["x"] == 2, pre)
check("...but with no anchor every month counts", nc.top_of_net_carry([pt(-2, 99.0), pt(0, -5.0)], None)["label"] == pt(-2, 0)["label"])
check("no top -> empty text", nc.top_summary(None) == "" and nc.top_headline(None) == "" and nc.top_detail(None) == "")

print("net_carry: the exact quote is named only when it adds something")
w = lambda d, lab, sl: nc._top_where({"label": lab, "delivery": d, "slots": sl})
check("several slots + 'LH Apr 27' -> 'Apr 27 (LH Apr 27)'", w("LH Apr 27", "Apr 27", 2) == "Apr 27 (LH Apr 27)")
check("a plain restatement of the month adds nothing ('Dec', 'December 2026', 'Oct 2026')",
      w("Dec", "Dec 26", 2) == "Dec 26" and w("December 2026", "Dec 26", 2) == "Dec 26" and w("Oct 2026", "Oct 26", 2) == "Oct 26")
check("a single slot never needs it", w("LH Apr 27", "Apr 27", 1) == "Apr 27")
check("a rail/truck qualifier is kept", w("June/July 2027 RAIL", "Jun 27", 2) == "Jun 27 (June/July 2027 RAIL)")
check("the headline uses it", nc.top_headline(nc.top_of_net_carry([pt(0, 1.0, "Apr 27", delivery="LH Apr 27", slots=2)], ANC))
      == "Top of net carry: Apr 27 (LH Apr 27) at +1.0¢ net of interest")

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
