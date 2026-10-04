"""net_carry.py — "Net Carry" analysis for a location's forward basis curve.

For one location (a scraped basis location OR a rail corridor) it takes the
forward set of (delivery, own-futures, basis) quotes and produces the columns on
Kolten's Net Carry sheet:

    Delivery | Basis vs REF | Interest | Net of Interest Basis | Inverse(+)/Carry(-)

Definitions (confirmed against Kolten's screenshot):
  • **Basis vs REF** — every delivery's basis re-expressed against ONE common
    futures contract (the reference), so the levels are comparable across the curve:
        basis_ref = raw_basis + futures_spread
        futures_spread = futures(own contract) - futures(reference)       (cents)
    The futures spread is the credit a delivery gets for being quoted off a later (or
    earlier) month than the reference — the market's own carry along the futures
    curve. By default the reference is the contract the FRONT delivery is quoted off
    (the front reads exactly as quoted; every later month is credited its spread vs
    that contract); the alternative is the nearest new-crop contract. (Same algebra as
    futures_spread.anchor_basis.) Because the reference only shifts every row by the
    same constant, it never changes the carry BETWEEN deliveries (except through the
    interest base, the reference's board price).
  • **Interest** — the cost of carrying grain from an anchor month forward, priced
    exactly like the Cost of Carry sheet (cost-of-carry-calculator, interest_full):
        interest = ref_price × annual_rate × days / 360
    where `days` is the actual calendar days from the first of the anchor month to the
    first of the delivery month, and annual_rate is fed funds + 2.25% (see carry_rate).
    Deliveries at or before the anchor month carry no interest (blank). (Until
    2026-10-04 this was months × (ref_price × a hand-set 9% / 12) — too high.)
  • **Net of Interest Basis** = Basis vs REF − Interest.
  • **Inverse(+)/Carry(-)** = prior delivery's Net − this delivery's Net.
    Positive ⇒ inverse (market pays to move now); negative ⇒ carry (market pays
    to store). Blank on the first (nearest) delivery.
  • **NC / New Crop** quotes (no month named) are the harvest-time bid — the earliest
    new-crop delivery — so they are placed AT the carry anchor and sorted first (see
    is_new_crop), not at their futures contract's month.
  • `monthly_carry` collapses weekly / half-month / duplicate slots to ONE point per calendar
    month — the slot with the HIGHEST net of interest, so the monthly curve never hides a better
    quote that the table shows — and measures each against the PREVIOUS quoted month. That is
    the charts; the table keeps every slot. `top_of_net_carry` finds the month where that
    curve peaks (the highest Net of Interest, from the carry start on).

Everything is in cents/bu, matching the rest of the app.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import delivery_period as _dp

_MCODE = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
          "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
_CODE_FOR = {v: k for k, v in _MCODE.items()}


def _root_for(commodity: str) -> tuple[str, str]:
    """(CME root, new-crop month code) for a commodity display name.

    Detected by substring so any label variant maps correctly — corn Dec (ZCZ),
    soy Nov (ZSX), SRW/Chicago wheat Jul (ZWN), HRW/KC wheat Jul (KEN); sorghum &
    milo trade off the corn board. Spring wheat (MGEX/MW) isn't in the futures
    curve, so its spreads fall back to raw basis. Unknown → corn."""
    t = (commodity or "").lower()
    if "soy" in t:
        return ("ZS", "X")
    if "sorghum" in t or "milo" in t:
        return ("ZC", "Z")
    if "wheat" in t or "hrw" in t or "srw" in t or "hrs" in t:
        if "hrw" in t or "hard red winter" in t or "kc" in t:
            return ("KE", "N")               # Kansas City hard red winter
        if "hrs" in t or "spring" in t or "dns" in t or "mgex" in t:
            return ("MW", "N")               # Minneapolis spring (not in curve)
        return ("ZW", "N")                   # Chicago soft red winter
    return ("ZC", "Z")                       # corn / default


def parse_symbol(sym: str):
    """'ZCZ26' → ('ZC', 12, 2026). None for anything that isn't a CME outright."""
    s = sym or ""
    if len(s) < 5 or s[2] not in _MCODE or not s[3:5].isdigit():
        return None
    return (s[:2], _MCODE[s[2]], 2000 + int(s[3:5]))


def _mi(year: int, month: int) -> int:
    return year * 12 + month


# A generic NEW-CROP quote: "NC", "N/C", "NC 26", "New Crop", "New Crop 2026" — it names no
# month. delivery_period reads such a label as its futures contract's month (Dec for corn),
# which drops it in the middle of the ladder; it is really the harvest-time bid, the EARLIEST
# new-crop delivery (corn NC 26 −25 vs Nov +3; soy NC 26 +3 vs Nov +28), so here it is the
# carry anchor and the front. A label that names a month ("NC Nov") is an ordinary month.
_NC_RE = re.compile(r"^\s*(?:n\s*/?\s*c|new[\s-]*crop)(?![a-z])", re.I)
_MONTH_WORD = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)


def is_new_crop(label: str) -> bool:
    return bool(_NC_RE.match(label or "")) and not _MONTH_WORD.search(label or "")


def _new_crop_year(label: str, fut: str | None) -> int | None:
    """The crop year an NC quote belongs to: the year in its label ('NC 26', 'New Crop
    2026'), else its futures contract's marketing year (Jul–Dec contract → that year,
    Jan–Jun contract → the year before). None if neither is available."""
    m = re.search(r"(?<!\d)(20\d\d)(?!\d)", label or "") or re.search(r"(?<!\d)(\d{2})(?!\d)", label or "")
    if m:
        y = int(m.group(1))
        return y if y >= 2000 else 2000 + y
    p = parse_symbol(fut or "")
    if p:
        return p[2] if p[1] >= 7 else p[2] - 1
    return None


def _normalize(items: list[dict], anchor_month: int = 10) -> tuple[list[dict], list[str]]:
    """Normalize + sort the quotes by nearness (delivery window, not just the futures
    month). A carry ladder needs a concrete (year, month) per delivery, so quotes that
    don't map to one — packages like "Jan-July"/"R", note rows like "NoBN" — are set
    aside (returned as `skipped`) rather than polluting the timeline. A generic NC / New
    Crop quote is placed AT the carry anchor (its crop year, `anchor_month`) and sorted
    ahead of any explicit quote in that month: it is the front the carry runs from."""
    norm, skipped = [], []
    for it in items:
        b = it.get("basis")
        if b is None:
            continue
        deliv, fut = it.get("delivery") or "", it.get("futures")
        nc, ym = False, None
        if is_new_crop(deliv):
            cy = _new_crop_year(deliv, fut)
            if cy is not None:
                nc, ym = True, (cy, anchor_month)
        if ym is None:
            ym = _dp.canonical(deliv, fut or "")
        if ym is None:
            skipped.append(deliv or "?")
            continue
        norm.append({"delivery": deliv, "futures": fut, "basis": float(b), "ym": ym, "nc": nc})

    def _key(r):
        if r["nc"]:
            return (r["ym"][0], r["ym"][1], -1, 0)      # ahead of any explicit quote that month
        return _dp.deliv_key(r["delivery"], r["futures"] or "")

    norm.sort(key=_key)
    return norm, skipped


def front_symbol(items: list[dict], curve: dict, anchor_month: int = 10) -> str | None:
    """The futures contract the FRONT delivery is quoted off: the nearest delivery on
    the ladder whose own futures is a CME outright with a price in `curve`. (Normally
    the first row; the next one if the front's contract can't be priced.)"""
    norm, _ = _normalize(items, anchor_month)
    for r in norm:
        s = r["futures"]
        if s and parse_symbol(s) and (curve or {}).get(s) is not None:
            return s
    return None


def reference_symbol(commodity: str, mode: str, curve: dict,
                     as_of: date | None = None, items: list[dict] | None = None,
                     anchor_month: int = 10) -> str | None:
    """Pick the ONE contract every basis is expressed against.

    mode='front'   → the futures month the FRONT delivery is quoted off (needs `items`;
                     every later delivery is credited its futures spread vs that contract).
                     Without items, or if none of them can be priced, it falls back to the
                     nearest active outright in the curve.
    mode='newcrop' → the nearest new-crop contract (corn Dec/ZCZ, soy Nov/ZSX,
                     wheat Jul/ZWN).
    Returns a CME symbol or None.
    """
    as_of = as_of or date.today()
    root, nc_code = _root_for(commodity)
    if mode == "newcrop":
        nc_month = _MCODE[nc_code]
        year = as_of.year if as_of.month <= nc_month else as_of.year + 1
        return f"{root}{nc_code}{year % 100:02d}"
    if items:
        fs = front_symbol(items, curve, anchor_month)
        if fs:
            return fs
    # nearest active: the smallest-dated outright of this root in the curve
    # whose delivery is not already behind us; else the earliest one we have.
    now = _mi(as_of.year, as_of.month)
    cands = []
    for s in (curve or {}):
        p = parse_symbol(s)
        if p and p[0] == root:
            cands.append((_mi(p[2], p[1]), s))
    if not cands:
        return None
    fwd = [c for c in cands if c[0] >= now]
    return (min(fwd) if fwd else min(cands))[1]


@dataclass
class CarryRow:
    delivery: str
    futures: str | None
    ym: tuple            # (year, month)
    raw_basis: float     # cents, as quoted vs its OWN futures contract
    credit: float | None  # futures spread credited: futures(own) − futures(ref), cents
                          # (0 when own == ref; None when it can't be priced)
    basis_ref: float | None   # raw_basis + credit
    converted: bool      # True if spread-adjusted to the reference (else raw)
    months: int          # months from anchor (0 or negative ⇒ no interest)
    days: int            # actual calendar days from the anchor month (0 ⇒ no interest)
    interest: float | None
    net: float | None
    carry: float | None
    new_crop: bool = False   # a generic NC / New Crop quote, placed at the carry anchor


def _anchor_ym(rows: list, anchor_month: int) -> tuple | None:
    """The (year, month) the interest clock starts from: the earliest delivery in
    the curve whose month equals anchor_month; else the front delivery's ym."""
    hits = [r["ym"] for r in rows if r["ym"] and r["ym"][1] == anchor_month]
    if hits:
        return min(hits, key=lambda ym: _mi(*ym))
    return rows[0]["ym"] if rows and rows[0]["ym"] else None


def compute_net_carry(items: list[dict], ref_symbol: str | None, curve: dict,
                      anchor_month: int, annual_rate: float) -> tuple[list[CarryRow], dict]:
    """Build the Net Carry rows.

    items: [{'delivery': str, 'futures': str|None, 'basis': float(cents)}] — the
           bid basis of each delivery, quoted vs its OWN futures symbol.
    ref_symbol: the common contract to express everything against (CZ26, …).
    curve: {symbol -> cents} futures prices for spread conversion + board price.
    anchor_month: 1–12, the month interest starts accruing from (0 there).
    annual_rate: decimal (0.0613 = 6.13%) applied to the reference board price on an
                 actual/360 basis — the Cost of Carry sheet's convention.

    Returns (rows_sorted, meta) where meta has ref_price, per_month (the sheet's
    30-day "Monthly interest"), all_converted.
    """
    norm, skipped = _normalize(items, anchor_month)

    ref_price = (curve or {}).get(ref_symbol) if ref_symbol else None
    # The Cost of Carry sheet's "Monthly interest" (a 30-day month): price × rate × 30/360.
    per_month = (ref_price * annual_rate * 30.0 / 360.0) if ref_price is not None else None
    anchor_ym = _anchor_ym(norm, anchor_month)

    rows: list[CarryRow] = []
    all_converted = True
    prev_net = None
    for r in norm:
        raw = r["basis"]
        own = r["futures"]
        # Re-express vs the reference contract: credit the futures spread between the
        # contract this delivery is quoted off and the reference (own − ref, in cents).
        if not ref_symbol or not own or own == ref_symbol:
            basis_ref, converted = raw, (own == ref_symbol or not ref_symbol)
            credit = 0.0 if own == ref_symbol else None
        else:
            po, pr = (curve or {}).get(own), (curve or {}).get(ref_symbol)
            if po is None or pr is None:
                basis_ref, converted, credit = raw, False, None   # fall back to raw
            else:
                credit = po - pr
                basis_ref, converted = raw + credit, True
        if not converted and own and own != ref_symbol:
            all_converted = False

        # Interest accrues from the anchor month forward.
        if r["ym"] and anchor_ym:
            months = _mi(*r["ym"]) - _mi(*anchor_ym)
            # actual calendar days, first of the anchor month → first of the delivery month
            days = (date(r["ym"][0], r["ym"][1], 1)
                    - date(anchor_ym[0], anchor_ym[1], 1)).days
        else:
            months = days = 0
        if ref_price is None:
            interest = None
            net = basis_ref
        else:
            # Cost of Carry sheet: interest_full = price × annual_rate × days / 360
            interest = ref_price * annual_rate * days / 360.0 if days > 0 else 0.0
            net = basis_ref - interest
        carry = None if prev_net is None else (prev_net - net)
        prev_net = net

        rows.append(CarryRow(
            delivery=r["delivery"], futures=own, ym=r["ym"], raw_basis=raw,
            credit=credit, basis_ref=basis_ref, converted=converted,
            months=months if months > 0 else 0,
            days=days if days > 0 else 0,
            interest=interest, net=net, carry=carry, new_crop=r["nc"],
        ))

    meta = {"ref_price": ref_price, "per_month": per_month,
            "all_converted": all_converted, "anchor_ym": anchor_ym,
            "skipped": skipped}
    return rows, meta


_ABBR = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
         7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}


def monthly_carry(rows: list[CarryRow]) -> list[dict]:
    """One point per calendar month for the charts, each vs the PREVIOUS quoted month.

    The table has a row per delivery SLOT (weekly at some plants, FH/LH at others, a whole-month
    and a half-month quote side by side on a rail corridor), so its carry column compares a
    slot with the slot before it — fine as a sheet, noisy as a chart (a run of near-zero bars
    within a month). Here each month is represented by its BEST slot — the one with the highest
    net of interest (every slot of a month carries the same interest, so that is also the
    highest basis; a tie goes to the nearer slot) — which keeps the monthly curve, its peak and
    the table in agreement: the highest Net of Interest the table shows is never hidden.
        carry = previous quoted month's net of interest − this month's net of interest
    (+ inverse: the nearer month is worth more; − carry: the market pays to store). If a
    month is not quoted at all, the bar compares to the last month that was.

    Returns [{ym, label, delivery, new_crop, slots, row, basis_ref, interest, net, carry, vs}] in
    time order (`row` = the chosen slot's index in `rows`, so the table can mark it; `slots` =
    how many quotes that month had); the first point has carry None. An NC / New Crop quote that
    is the month's best keeps its own label (it stands for the anchor month). The same points
    feed the "Cash Fwd Curve" line chart.
    """
    best, slots, order = {}, {}, []
    for i, r in enumerate(rows):
        if r.net is None:
            continue
        slots[r.ym] = slots.get(r.ym, 0) + 1
        j = best.get(r.ym)
        if j is None:
            best[r.ym] = i
            order.append(r.ym)
        elif r.net > rows[j].net + 1e-9:          # strictly better; a tie keeps the nearer slot
            best[r.ym] = i
    order.sort(key=lambda ym: _mi(*ym))
    pts = []
    for ym in order:
        i = best[ym]
        r = rows[i]
        label = r.delivery if r.new_crop else f"{_ABBR[ym[1]]} {ym[0] % 100:02d}"
        pts.append({"ym": ym, "label": label, "delivery": r.delivery, "new_crop": r.new_crop,
                    "slots": slots[ym], "row": i, "basis_ref": r.basis_ref,
                    "interest": r.interest, "net": r.net})
    for i, p in enumerate(pts):
        prev = pts[i - 1] if i else None
        p["carry"] = None if prev is None else prev["net"] - p["net"]
        p["vs"] = prev["label"] if prev else None
    return pts


def top_of_net_carry(points: list[dict], anchor_ym: tuple | None = None) -> dict | None:
    """The month where Net of Interest PEAKS — past it, carrying the grain further stops paying.

    Looks at the monthly points (monthly_carry) from the carry anchor on — all of them if there is
    no anchor; on a tie the EARLIEST month wins. Because a month's point is its best slot, the
    peak is the highest Net of Interest anywhere in the table from the anchor on. Returns None
    for no points, else
      {x, row, ym, label, delivery, slots, net, basis_ref,      the peak month (x = its index in `points`,
                                                                 row = its slot's index in the table rows)
       front_label, front_net, gain,                            the first month in range, and peak − front
       is_front, is_last, next_label, give_back}                where it sits, and what the next month gives back
    """
    cands = [i for i, p in enumerate(points)
             if p.get("net") is not None and (anchor_ym is None or p["ym"] >= anchor_ym)]
    if not cands:
        return None
    best_k = 0
    for k in range(1, len(cands)):
        if points[cands[k]]["net"] > points[cands[best_k]]["net"] + 1e-9:
            best_k = k
    bi, fi = cands[best_k], cands[0]
    best, first = points[bi], points[fi]
    nxt = points[cands[best_k + 1]] if best_k + 1 < len(cands) else None
    return {"x": bi, "row": best.get("row"), "ym": best["ym"], "label": best["label"],
            "delivery": best.get("delivery"), "slots": best.get("slots", 1),
            "net": best["net"], "basis_ref": best.get("basis_ref"),
            "front_label": first["label"], "front_net": first["net"],
            "gain": best["net"] - first["net"], "is_front": best_k == 0, "is_last": nxt is None,
            "next_label": nxt["label"] if nxt else None,
            "give_back": (nxt["net"] - best["net"]) if nxt else None}


_MONTH_WORDS = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
                          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", re.I)


def _top_where(top: dict) -> str:
    """The peak month, plus the exact quote when that month had several AND the quote says more than
    the month ('Apr 27 (LH Apr 27)'; a plain 'Dec' next to 'Dec 26' adds nothing, so it is left off)."""
    d = (top.get("delivery") or "").strip()
    if top.get("slots", 1) > 1 and d and d != top["label"]:
        extra = re.sub(r"[\d\W_]+", "", _MONTH_WORDS.sub(" ", d))
        if extra:
            return f"{top['label']} ({d})"
    return top["label"]


def top_headline(top: dict | None) -> str:
    """'Top of net carry: Apr 27 at +37.5¢ net of interest' — the part to show in bold."""
    if not top:
        return ""
    return f"Top of net carry: {_top_where(top)} at {top['net']:+.1f}¢ net of interest"


def top_detail(top: dict | None) -> str:
    """The sentence after the headline: how far above the start, and what carrying past it does."""
    if not top:
        return ""
    if top["is_front"] and top["is_last"]:
        return "It is the only month on the curve."
    flat = top["give_back"] is not None and abs(top["give_back"]) < 0.05
    if top["is_front"]:
        if flat:
            return f"That is where the carry starts — it stays flat into {top['next_label']}."
        return (f"That is where the carry starts — it falls {abs(top['give_back']):.1f}¢ by "
                f"{top['next_label']}, so carrying the grain does not pay.")
    above = (f"{top['gain']:.1f}¢ above {top['front_label']} ({top['front_net']:+.1f}¢), "
             f"where the carry starts.")
    if top["is_last"]:
        return f"{above} It is still paying to carry through the last quoted month."
    if flat:
        return f"{above} It stays flat into {top['next_label']}."
    return f"{above} Carrying past it gives back {abs(top['give_back']):.1f}¢ by {top['next_label']}."


def top_summary(top: dict | None) -> str:
    """The headline and the detail as one plain string ('' when there is no top)."""
    if not top:
        return ""
    return f"{top_headline(top)}. {top_detail(top)}"
