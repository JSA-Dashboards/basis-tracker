"""river_derived.py — Return to Carry history for river elevators that only began posting in 2026, DERIVED from the River FOB sheet.

Kolten 2026-10-06: ~35 river elevators (ADM Havana, Cargill Beardstown, CHS Seneca, ...) have bids only since June 2026, so the tracker
had no history for them. The River FOB sheet archive (river_carry; weekly since 2006-09) has the FOB barge basis of the reach they sit on,
and an elevator's bid tracks it at a fairly steady distance, so its history can be ESTIMATED as

    derived bid = the reach's FOB basis (weekly nearby, river_carry.nearby_obs) - gap

where `gap` is the median of (FOB - this location's bid) over every full-month quote both posted on the same day since June (the FOB moved
to the bid's futures contract by that day's spread). It is not the location's own basis history, and the screen says so: every derived bid
is flagged (`'derived': True`), the crop years built from them are marked, and a banner states the FOB reach, the gap and its spread.
A constant gap cancels out of the weekly gross return (bid - harvest basis + carry), and moves the net return only by the interest on
the gap (a fraction of a cent), so the returns are those of the FOB series; only the harvest-basis and summer-basis LEVELS carry the gap.
The location's own bids take over from their first week, so the 2025-26 year is a mixture.

Only locations with an explicit reach below are derived, and only where the gap is steady enough to mean something (MIN_PAIRS, MAX_IQR,
MAX_GAP); the rest are left without a history rather than given a misleading one.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

import return_to_carry as rtc
import return_to_carry_data as rd
import river_carry as rc

# (provider, location as the tracker names it) -> the River FOB sheet location whose reach it sits on
FOB_REACH = {
    ("ADM", "Havana, IL"): "Havana", ("Cargill", "Havana"): "Havana", ("Cargill", "Beardstown"): "Havana",
    ("Cargill", "Meredosia"): "Havana", ("CHS", "Havana/Beardstown"): "Havana",
    ("ADM", "Morris, IL"): "Seneca", ("CHS", "Morris, IL"): "Seneca", ("CHS", "Seneca"): "Seneca",
    ("ADM", "Quincy, IL (Barge Dock)"): "Quincy", ("CHS", "Quincy Elevator"): "Quincy",
    ("ADM", "Memphis, TN"): "Memphis", ("CGB", "CGB WEST MEMPHIS"): "Memphis", ("LDC", "West Memphis"): "Memphis",
    ("Bunge", "Cairo, IL"): "Cairo",
    ("ADM", "Evansville, IN (1st Ave.)"): "MTV", ("ADM", "Evansville, IN (Broadway)"): "MTV",
    ("ADM", "Evansville, IN (River South)"): "MTV", ("ADM", "Mt. Vernon, IN (Elevator)"): "MTV",
    ("Cargill", "Cincinnati Kellogg"): "Cincy", ("Cargill", "Cincinnati River Road"): "Cincy",
    ("ADM", "Clinton, IA (Elevator)"): "Davenport", ("CGB", "CGB JOLIET"): "Chicago",
}

MIN_PAIRS = 40          # same-day, same-month quote pairs behind the gap
MAX_IQR = 12.0          # cents: how wide the middle half of the pairs' gaps may be
MAX_GAP = 30.0          # cents: a median gap beyond this is a different animal, not a basis
OWN_HISTORY_SINCE = date(2025, 1, 1)    # a location whose own bids reach back before this already has a history of its own
GRAIN_ROOT = {"Corn": "ZC", "Soybeans": "ZS"}


@dataclass(frozen=True)
class Derivation:
    fob_location: str
    gap: float                 # cents: median of (FOB - this location's bid)
    q1: float                  # the middle half of the pairs' gaps runs q1 .. q3
    q3: float
    pairs: int                 # same-day, same-month quote pairs it was measured on
    own_from: date             # the location's first own weekly bid: the derived series stops the week before
    derived_weeks: int         # weekly bids derived
    first: date                # the first derived week

    def as_dict(self) -> dict:
        return {"fob_location": self.fob_location, "gap": self.gap, "q1": self.q1, "q3": self.q3, "pairs": self.pairs,
                "own_from": self.own_from, "derived_weeks": self.derived_weeks, "first": self.first}


def _ym(d: date, info: dict) -> tuple:
    m = info["months"][0]
    return (info["year"] if info["year"] is not None else (d.year if m >= d.month else d.year + 1), m)


def _full_month(label):
    info = rd.parse_label(label)
    return info if info and info["kind"] == "full" and len(info["months"]) == 1 else None


def gap_pairs(live_quotes: list, fob_quotes: list, futs: dict) -> list:
    """[(date, gap cents)]: for every day and delivery month BOTH the location and the FOB sheet posted (full-month quotes only),
    FOB - the location's bid, the FOB moved to the bid's futures contract by that day's spread when the two are quoted off different ones."""
    fob = {}
    for q in fob_quotes:
        info = _full_month(q.get("label"))
        if info and q.get("tag") and q.get("basis") is not None:
            fob[(q["date"], _ym(q["date"], info))] = q
    out = []
    for q in live_quotes:
        info = _full_month(q.get("label"))
        if not info or not q.get("tag") or q.get("basis") is None:
            continue
        f = fob.get((q["date"], _ym(q["date"], info)))
        if f is None:
            continue
        adj = 0.0
        sym = (q["tag"] or "").strip().upper()
        if f["tag"] != sym:
            pf, _ = rtc.price_on(futs, q["date"], f["tag"])
            pq, _ = rtc.price_on(futs, q["date"], sym)
            if pf is None or pq is None:
                continue
            adj = pf - pq
        out.append((q["date"], f["basis"] + adj - q["basis"]))
    return out


def calibrate(pairs: list):
    """(median, q1, q3, n) of the pairs' gaps, or None when there are too few, or they are too scattered / too large to be a steady
    gap between a bid and the FOB (the quote is of something else: a container terminal, a different grade, a different reach)."""
    gaps = [g for _, g in pairs]
    if len(gaps) < MIN_PAIRS:
        return None
    q1, _, q3 = statistics.quantiles(gaps, n=4)
    med = statistics.median(gaps)
    if (q3 - q1) > MAX_IQR or abs(med) > MAX_GAP:
        return None
    return med, q1, q3, len(gaps)


def needs_history(live_obs: list) -> bool:
    """True when the location's own weekly bids start too recently to have a history (no bid before OWN_HISTORY_SINCE)."""
    return bool(live_obs) and min(o["date"] for o in live_obs) >= OWN_HISTORY_SINCE


def with_derived(provider: str, location: str, grain: str, live_obs: list, live_quotes: list, archive: dict, futs: dict):
    """(obs, Derivation | None): the location's weekly bids, with the derived history in front of them when the location is a mapped river
    elevator with no history of its own, the grain is corn or soybeans, and its gap to the FOB reach is steady. Every derived bid carries
    'derived': True; the location's own bids are returned untouched."""
    reach = FOB_REACH.get((provider, location))
    if reach is None or grain not in GRAIN_ROOT or not needs_history(live_obs):
        return live_obs, None
    cal = calibrate(gap_pairs(live_quotes, rc.forward_quotes(archive, reach, grain), futs))
    if cal is None:
        return live_obs, None
    gap, q1, q3, n = cal
    own_from = min(o["date"] for o in live_obs)
    derived = [{"date": o["date"], "basis": o["basis"] - gap, "tag": o["tag"], "derived": True}
               for o in rc.nearby_obs(archive, reach, grain) if o["date"] < own_from]
    if not derived:
        return live_obs, None
    return derived + list(live_obs), Derivation(reach, round(gap, 2), round(q1, 2), round(q3, 2), n, own_from, len(derived), derived[0]["date"])
