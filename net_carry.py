"""net_carry.py — "Net Carry" analysis for a location's forward basis curve.

For one location (a scraped basis location OR a rail corridor) it takes the
forward set of (delivery, own-futures, basis) quotes and produces the columns on
Kolten's Net Carry sheet:

    Delivery | Basis vs REF | Interest | Net of Interest Basis | Inverse(+)/Carry(-)

Definitions (confirmed against Kolten's screenshot):
  • **Basis vs REF** — every delivery's basis re-expressed against ONE common
    futures contract (the reference, e.g. CZ26), so the levels are comparable
    across the curve:  basis_ref = raw_basis + futures(own) - futures(ref)
    (the same algebra as futures_spread.anchor_basis).
  • **Interest** — the cost of carrying grain from an anchor month forward, at an
    annual interest rate applied to the reference board price:
        interest = months_from_anchor × (ref_price × annual_rate / 12)
    Months at or before the anchor carry no interest (blank).
  • **Net of Interest Basis** = Basis vs REF − Interest.
  • **Inverse(+)/Carry(-)** = prior delivery's Net − this delivery's Net.
    Positive ⇒ inverse (market pays to move now); negative ⇒ carry (market pays
    to store). Blank on the first (nearest) delivery.

Everything is in cents/bu, matching the rest of the app.
"""
from __future__ import annotations

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


def reference_symbol(commodity: str, mode: str, curve: dict,
                     as_of: date | None = None) -> str | None:
    """Pick the ONE contract every basis is expressed against.

    mode='newcrop' → the nearest new-crop contract (corn Dec/ZCZ, soy Nov/ZSX,
    wheat Jul/ZWN); mode='front' → the nearest active contract present in the
    futures curve (rolls as contracts expire). Returns a CME symbol or None.
    """
    as_of = as_of or date.today()
    root, nc_code = _root_for(commodity)
    if mode == "newcrop":
        nc_month = _MCODE[nc_code]
        year = as_of.year if as_of.month <= nc_month else as_of.year + 1
        return f"{root}{nc_code}{year % 100:02d}"
    # front / nearest active: the smallest-dated outright of this root in the curve
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
    raw_basis: float     # cents, vs own futures
    basis_ref: float | None
    converted: bool      # True if spread-adjusted to the reference (else raw)
    months: int          # months from anchor (0 or negative ⇒ no interest)
    interest: float | None
    net: float | None
    carry: float | None


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
    annual_rate: decimal (0.09 = 9%) applied to the reference board price.

    Returns (rows_sorted, meta) where meta has ref_price, per_month, all_converted.
    """
    # Normalize + sort by nearness (delivery window, not just the futures month).
    # A carry ladder needs a concrete (year, month) per delivery, so quotes that
    # don't map to one — packages like "Jan-July"/"R", note rows like "NoBN" — are
    # set aside (reported in meta) rather than polluting the timeline.
    norm, skipped = [], []
    for it in items:
        b = it.get("basis")
        if b is None:
            continue
        deliv, fut = it.get("delivery") or "", it.get("futures")
        ym = _dp.canonical(deliv, fut or "")
        if ym is None:
            skipped.append(deliv or "?")
            continue
        norm.append({"delivery": deliv, "futures": fut, "basis": float(b), "ym": ym})
    norm.sort(key=lambda r: _dp.deliv_key(r["delivery"], r["futures"] or ""))

    ref_price = (curve or {}).get(ref_symbol) if ref_symbol else None
    per_month = (ref_price * annual_rate / 12.0) if ref_price is not None else None
    anchor_ym = _anchor_ym(norm, anchor_month)

    rows: list[CarryRow] = []
    all_converted = True
    prev_net = None
    for r in norm:
        raw = r["basis"]
        own = r["futures"]
        # Re-express vs the reference contract via the futures spread.
        if not ref_symbol or not own or own == ref_symbol:
            basis_ref, converted = raw, (own == ref_symbol or not ref_symbol)
        else:
            po, pr = (curve or {}).get(own), (curve or {}).get(ref_symbol)
            if po is None or pr is None:
                basis_ref, converted = raw, False   # fall back to raw
            else:
                basis_ref, converted = raw + (po - pr), True
        if not converted and own and own != ref_symbol:
            all_converted = False

        # Interest accrues from the anchor month forward.
        if r["ym"] and anchor_ym:
            months = _mi(*r["ym"]) - _mi(*anchor_ym)
        else:
            months = 0
        if per_month is None:
            interest = None
            net = basis_ref
        else:
            interest = per_month * months if months > 0 else 0.0
            net = basis_ref - interest
        carry = None if prev_net is None else (prev_net - net)
        prev_net = net

        rows.append(CarryRow(
            delivery=r["delivery"], futures=own, ym=r["ym"], raw_basis=raw,
            basis_ref=basis_ref, converted=converted,
            months=months if months > 0 else 0,
            interest=interest, net=net, carry=carry,
        ))

    meta = {"ref_price": ref_price, "per_month": per_month,
            "all_converted": all_converted, "anchor_ym": anchor_ym,
            "skipped": skipped}
    return rows, meta
