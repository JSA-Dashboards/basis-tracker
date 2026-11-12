"""
changes_report.py — Standalone builder + emailer for the Daily Basis Changes report.

Reproduces the dashboard's "Changes" tab (build_changes_email_html in app.py) as
a self-contained module with NO Streamlit dependency, so the scheduled scraper
(auto_import.py) can build the same branded HTML and email it after each run.

Send path: Outlook desktop via win32com (no passwords / API keys / IT approval —
runs in the user's logged-in Windows session where Outlook is configured).

CLI:
    python changes_report.py --html       # print the HTML to stdout
    python changes_report.py --send        # build + email via Outlook
    python changes_report.py --send --to someone@jpsi.com
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta

import delivery_period as _dp
from regions import region_from_state
from river_segments import river_segment, SEGMENT_ORDER
from adm_names import adm_city_from_name
from database import get_grain_map, get_bids_filter_data, get_snapshots_bulk

log = logging.getLogger(__name__)

DEFAULT_TO = os.getenv("CHANGES_EMAIL_TO", "kpostin@jpsi.com")
# Standing BCC on the daily bids/changes email (overridable via DAILY_BIDS_BCC env).
# Comma-separated; the Outlook path re-joins with ';' so both transports work.
DAILY_BIDS_BCC = '"Daily Bids" <dailybids@jpsi.com>, jsagroup@jpsi.com'
# Standing BCC added to the rail updates (overridable via JSA_GROUP_BCC env).
JSA_GROUP_BCC = "jsagroup@jpsi.com"

_CME_MONTH_TO_INT = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# ── JPSI brand (jpsi.com) ────────────────────────────────────────────────────
JPSI_DARK  = "#32373c"
JPSI_BLUE  = "#0693e3"
VIEW_DASHBOARD_URL = os.getenv("VIEW_DASHBOARD_URL",
                               "https://basis-tracker-view.streamlit.app")
JPSI_LOGO  = "https://www.jpsi.com/wp-content/themes/gate39media/img/logo-white.png"
_GAIN, _LOSS = "#16a34a", "#dc2626"

TREND_CATEGORIES = [
    ("Corn Processing — Corn",     "Corn Processing", "Corn",     "region"),
    ("Rail Terminals — Corn",      "Rail Terminal",   "Corn",     "region"),
    ("River Terminals — Corn",     "River Terminal",  "Corn",     "segment"),
    ("Soy Processing — Soybeans",  "Soy Processing",  "Soybeans", "region"),
    ("River Terminals — Soybeans", "River Terminal",  "Soybeans", "segment"),
    ("Wheat Mills — Soft Red Winter",     "Wheat Milling",  "Soft Red Winter (SRW)", "region"),
    ("Rail Terminals — Soft Red Winter",  "Rail Terminal",  "Soft Red Winter (SRW)", "region"),
    ("River Terminals — Soft Red Winter", "River Terminal", "Soft Red Winter (SRW)", "segment"),
    ("Ethanol Plants — Sorghum",   "Corn Processing",  "Sorghum", "region"),
    ("Country Elevators — Sorghum", "Country Elevator", "Sorghum", "region"),
    ("Rail Terminals — Sorghum",   "Rail Terminal",    "Sorghum", "region"),
]

_GM: dict = {}


def _grain_disp(raw: str) -> str | None:
    """Canonical display name for a raw grain, or None if inactive."""
    entry = _GM.get(raw)
    if entry is None:
        return raw
    if not entry["is_active"]:
        return None
    cls  = entry.get("wheat_class")
    prot = entry.get("protein")
    base = entry["canonical_grain"]
    if cls:
        return f"{base} ({cls} {prot})" if prot else f"{base} ({cls})"
    return base


def _trend_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.min


def _trend_extract(snap, grain, mode="spot"):
    """Basis for grain at delivery `mode` ('spot' = nearest), with isSpot fallback."""
    if snap is None:
        return None
    if mode == "spot":
        cands = [r for r in snap.rows
                 if not r.isSpot and _grain_disp(r.grain) == grain
                 and r.basisCents is not None and r.futuresSymbol]
        if cands:
            return min(cands, key=lambda r: _dp.deliv_key(r.deliveryMonth, r.futuresSymbol)).basisCents
        row = next((r for r in snap.rows
                    if r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None), None)
        return row.basisCents if row else None
    matches = [r for r in snap.rows
               if not r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None
               and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode]
    if matches:
        return min(matches, key=lambda r: _dp.slot_key(r.deliveryMonth)).basisCents
    row = next((r for r in snap.rows
                if r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None
                and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode), None)
    return row.basisCents if row else None


def _trend_closest(snaps, target, maxd):
    if not snaps:
        return None
    b = min(snaps, key=lambda s: abs((_trend_ts(s.timestamp) - target).total_seconds()))
    return b if abs((_trend_ts(b.timestamp) - target).total_seconds()) / 86400 <= maxd else None


_LOAD_MEMO = None        # {facility type: load result} while ONE report is being built (its sections and trend cards of a type share a load); None otherwise


def _load(facility_type: str):
    """_load_fresh, remembered for the length of one report build (build_changes_email_html sets and clears _LOAD_MEMO); a plain call is never cached."""
    if _LOAD_MEMO is not None and facility_type in _LOAD_MEMO:
        return _LOAD_MEMO[facility_type]
    res = _load_fresh(facility_type)
    if _LOAD_MEMO is not None:
        _LOAD_MEMO[facility_type] = res
    return res


def _load_fresh(facility_type: str):
    """Snapshot load + 'now' anchor for a facility type (uncached version of app._trend_load)."""
    global _GM
    if not _GM:
        _GM = get_grain_map()
    from collections import Counter as _C
    from facility_overrides import override_pairs_for
    sl    = get_bids_filter_data()
    _meta = {(l["provider"], l["location"]): l for l in sl}
    # Base-type locations PLUS any overridden into this type for some grain (e.g.
    # ADM Beech Grove, a rail terminal that mills its wheat); the per-grain guard
    # in each builder filters within.
    pairs = [(l["provider"], l["location"]) for l in sl if l.get("facility_type") == facility_type]
    pairs = sorted(set(pairs) | (override_pairs_for(facility_type) & set(_meta)))
    # compact: this load is a whole facility type's 400 days (~1M bid rows) that is only READ by attribute. As pydantic models the whole email build peaked at
    # ~1.3 GB, ~1.8 GB once the memo below kept every type's load (measured on live data), which the 2 GB droplet could not give: the 11-12 15:45 run was
    # OOM-killed building this email. Compact: ~0.38 GB, the same email to the byte.
    data  = get_snapshots_bulk(pairs, since_days=400, compact=True) if pairs else {}
    today_noon = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    loc_latest = []
    for snaps in data.values():
        ds = [d for s in snaps if (d := _trend_ts(s.timestamp)) <= today_noon]
        if ds:
            loc_latest.append(max(ds).date())
    # Anchor on the LATEST date reached (≤ today), not the most common, so a provider
    # that's a day ahead of the others still surfaces its fresh change.
    now = (datetime(*max(loc_latest).timetuple()[:3], 12) if loc_latest else today_noon)
    return pairs, _meta, data, now


_CME_MON = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
            "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


def _fut_ord(sym) -> int:
    """Sortable ordinal for a futures contract ('ZSN26' → 2026*12+7) so a roll's
    newer contract sorts after the older one."""
    if not sym or len(sym) < 4:
        return 0
    mon = _CME_MON.get(sym[2], 0)
    try:
        yr = int(sym[3:])
    except ValueError:
        yr = 0
    return yr * 12 + mon


def _snap_ym(snap):
    """(year, month) of a snapshot's timestamp, for dropping stale past-delivery bids."""
    try:
        d = datetime.fromisoformat(snap.timestamp.replace("Z", "+00:00"))
        return (d.year, d.month)
    except Exception:
        return None


def _curve_map(snap, grain):
    """{canonical (year, month) -> basis_cents} for a grain's forward rows (nearest
    slot; on a slot tie the later/rolled-to contract wins)."""
    m, best = {}, {}
    ym = _snap_ym(snap)
    for r in snap.rows:
        if (r.isSpot or _grain_disp(r.grain) != grain
                or r.basisCents is None or not r.futuresSymbol):
            continue
        k   = _dp.canonical(r.deliveryMonth, r.futuresSymbol)
        if ym and k and k < ym:      # stale past-delivery bid (e.g. a lingering 'Sep 2025')
            continue
        sk  = _dp.slot_key(r.deliveryMonth)
        fo  = _fut_ord(r.futuresSymbol)
        cur = best.get(k)
        if cur is None or sk < cur[0] or (sk == cur[0] and fo > cur[1]):
            best[k], m[k] = (sk, fo), r.basisCents
    return m


def _curve_syms(snap, grain):
    """{canonical (year, month) -> nearest-slot futures symbol}; on a slot tie the
    later/rolled-to contract wins, so a roll (ZSN26 → ZSQ26) is detected even while a
    location still lists the delivery month against both the old and new contract."""
    m, best = {}, {}
    ym = _snap_ym(snap)
    for r in snap.rows:
        if (r.isSpot or _grain_disp(r.grain) != grain
                or r.basisCents is None or not r.futuresSymbol):
            continue
        k   = _dp.canonical(r.deliveryMonth, r.futuresSymbol)
        if ym and k and k < ym:      # stale past-delivery bid (e.g. a lingering 'Sep 2025')
            continue
        sk  = _dp.slot_key(r.deliveryMonth)
        fo  = _fut_ord(r.futuresSymbol)
        cur = best.get(k)
        if cur is None or sk < cur[0] or (sk == cur[0] and fo > cur[1]):
            best[k], m[k] = (sk, fo), r.futuresSymbol
    return m


_FUT_SHORT = {"ZS": "S", "ZC": "C", "ZW": "W", "ZM": "SM",
              "ZL": "BO", "ZO": "O", "KE": "KW", "MW": "MW"}


def _short_fut(sym) -> str:
    """'ZSN26' → 'SN', 'ZCN26' → 'CN'."""
    if not sym:
        return ""
    comm = _FUT_SHORT.get(sym[:2])
    return (comm + sym[2]) if (comm and len(sym) >= 3) else sym


def _futures_curve() -> dict:
    """Today's futures curve {symbol -> cents} (for spread-adjusting rolls)."""
    import futures_spread as _fs
    return _fs.ensure_curve()


def _roll_adjust(nominal, from_sym, to_sym, curve):
    """True basis move across a contract roll = nominal change minus the from→to
    futures spread. Uses the live curve while both legs trade, else the spread frozen
    at the last joint close (past first notice). None if no spread is available."""
    if nominal is None:
        return None
    pf, pt = curve.get(from_sym), curve.get(to_sym)
    if pf is not None and pt is not None:
        spread = pf - pt
    else:
        try:
            from database import get_roll_spread
            spread = get_roll_spread(from_sym, to_sym)
        except Exception:
            spread = None
        if spread is None:
            return None
    return round(nominal - spread)


_FULL_MON = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]


def build_change_rows(facility_type: str, grain: str, mode: str = "spot") -> dict:
    """Day-over-day basis change at the category's two nearest delivery months.

    Returns {m1_label, m2_label, rows}. m1 = the category's nearest delivery month
    (e.g. 'June'), m2 = the next ('July'); both roll forward over time. Each row:
    provider, location, b1/c1 (basis & daily change at m1), b2/c2 (at m2). A
    location is included if it moved at m1 OR m2.
    """
    from collections import Counter
    from facility_overrides import effective_ftype
    pairs, meta, data, now = _load(facility_type)
    locs = []
    for key in pairs:
        if effective_ftype(key[0], key[1], grain,
                           meta.get(key, {}).get("facility_type")) != facility_type:
            continue      # this grain belongs to a different (overridden) category
        snaps    = data.get(key, [])
        cur_snap = _trend_closest(snaps, now, 1.6)
        if cur_snap is None:
            continue
        ref_t = _trend_ts(cur_snap.timestamp)
        prior = None
        for s in snaps:
            t = _trend_ts(s.timestamp)
            if t < ref_t and (prior is None or t > _trend_ts(prior.timestamp)):
                prior = s
        cm = _curve_map(cur_snap, grain)
        if not cm:
            continue
        pm   = _curve_map(prior, grain)  if prior is not None else {}
        csym = _curve_syms(cur_snap, grain)
        psym = _curve_syms(prior, grain) if prior is not None else {}
        locs.append((key[0], key[1], cm, pm, csym, psym))
    if not locs:
        return {"m1_label": None, "m2_label": None, "rows": []}

    m1 = Counter(min(cm) for _, _, cm, _, _, _ in locs).most_common(1)[0][0]
    nxt = Counter()
    for _, _, cm, _, _, _ in locs:
        after = [k for k in sorted(cm) if k > m1]
        if after:
            nxt[after[0]] += 1
    m2 = nxt.most_common(1)[0][0] if nxt else None
    curve = _futures_curve()

    rows = []
    for prov, loc, cm, pm, csym, psym in locs:
        b1 = cm.get(m1)
        c1 = (cm[m1] - pm[m1]) if (m1 in cm and m1 in pm) else None
        b2 = cm.get(m2) if m2 else None
        c2 = (cm[m2] - pm[m2]) if (m2 is not None and m2 in cm and m2 in pm) else None
        roll1 = (m1 in csym and m1 in psym and csym[m1] != psym[m1])
        roll2 = (m2 is not None and m2 in csym and m2 in psym and csym[m2] != psym[m2])
        if roll1:        # add back the contract spread to recover the true basis move
            c1 = _roll_adjust(c1, psym.get(m1), csym.get(m1), curve)
        if roll2:
            c2 = _roll_adjust(c2, psym.get(m2), csym.get(m2), curve)
        if (c1 or 0) == 0 and (c2 or 0) == 0 and not roll1 and not roll2:
            continue
        rows.append({"provider": prov, "location": loc, "b1": b1, "c1": c1, "b2": b2, "c2": c2,
                     "m1_sym": csym.get(m1), "m2_sym": csym.get(m2),
                     "roll1": roll1, "roll1_from": (psym.get(m1) if roll1 else None),
                     "roll1_to": (csym.get(m1) if roll1 else None),
                     "roll2": roll2, "roll2_from": (psym.get(m2) if roll2 else None),
                     "roll2_to": (csym.get(m2) if roll2 else None)})
    rows.sort(key=lambda r: (-(r["c1"] or 0), -(r["c2"] or 0), r["provider"], r["location"]))
    return {"m1_label": (_FULL_MON[m1[1]] if m1 else None),
            "m2_label": (_FULL_MON[m2[1]] if m2 else None),
            "rows": rows}


def build_segment_change_rows(facility_type: str, grain: str, mode: str = "spot") -> dict:
    """Per river-segment avg basis & avg daily change at the category's two nearest
    delivery months. Returns {m1_label, m2_label, rows:[{segment,b1,c1,b2,c2,n}]}."""
    from collections import Counter
    from facility_overrides import effective_ftype
    pairs, meta, data, now = _load(facility_type)
    locs = []
    for key in pairs:
        if effective_ftype(key[0], key[1], grain,
                           meta.get(key, {}).get("facility_type")) != facility_type:
            continue      # this grain belongs to a different (overridden) category
        snaps    = data.get(key, [])
        cur_snap = _trend_closest(snaps, now, 1.6)
        if cur_snap is None:
            continue
        ref_t = _trend_ts(cur_snap.timestamp)
        prior = None
        for s in snaps:
            t = _trend_ts(s.timestamp)
            if t < ref_t and (prior is None or t > _trend_ts(prior.timestamp)):
                prior = s
        cm = _curve_map(cur_snap, grain)
        if not cm:
            continue
        pm = _curve_map(prior, grain) if prior is not None else {}
        locs.append((river_segment(key[1]), cm, pm))
    if not locs:
        return {"m1_label": None, "m2_label": None, "rows": []}

    m1 = Counter(min(cm) for _, cm, _ in locs).most_common(1)[0][0]
    nxt = Counter()
    for _, cm, _ in locs:
        after = [k for k in sorted(cm) if k > m1]
        if after:
            nxt[after[0]] += 1
    m2 = nxt.most_common(1)[0][0] if nxt else None

    by_seg: dict = {}
    for seg, cm, pm in locs:
        by_seg.setdefault(seg, []).append((cm, pm))

    def avg(vals):
        return (sum(vals) / len(vals)) if vals else None

    rows = []
    for seg in SEGMENT_ORDER:
        items = by_seg.get(seg)
        if not items:
            continue
        b1 = avg([cm[m1] for cm, _ in items if m1 in cm])
        c1 = avg([cm[m1] - pm[m1] for cm, pm in items if m1 in cm and m1 in pm])
        b2 = avg([cm[m2] for cm, _ in items if m2 and m2 in cm]) if m2 else None
        c2 = avg([cm[m2] - pm[m2] for cm, pm in items if m2 and m2 in cm and m2 in pm]) if m2 else None
        rows.append({"segment": seg, "b1": b1, "c1": c1, "b2": b2, "c2": c2, "n": len(items)})
    return {"m1_label": (_FULL_MON[m1[1]] if m1 else None),
            "m2_label": (_FULL_MON[m2[1]] if m2 else None),
            "rows": rows}


# ── Trend cards ───────────────────────────────────────────────────────────────────────────────────────────────
# Kolten 2026-11-12: "can we add in the trends table that is on the trends tab ... above each section show the table of trends avg basis changes
# weekly, monthly and yearly changes overall and % east vs west". These are the Trends tab's three small tables (app.render_trend_cards) with the
# SAME numbers: per location the nearest-delivery basis now / ~1 week / ~1 month / ~1 year ago (app.build_trend_rows), each move spread-adjusted
# across a contract roll (app._trend_move / get_adj), and the locations marked as index outliers on the Trends tab left out. This module has no
# Streamlit, so that logic is mirrored here (as _load mirrors app._trend_load); tests/test_changes_trend_cards.py pins it.

_ROLL_ADJ = [{"from": "ZSK26", "to": "ZSN26", "adj": -16}, {"from": "ZCK26", "to": "ZCN26", "adj": -10}]     # manual fallback, as app.ROLL_ADJ
_FROZEN_SPREAD_MEMO: dict = {}
_CURVE_MEMO = None       # the futures curve, fetched once for ONE report build (None outside one); see _trend_curve_once


def _trend_curve_once() -> dict:
    """The futures curve for the trend tables' roll adjustments. Inside one report build it is fetched once, an empty answer included: futures_spread.ensure_curve
    keeps nothing while a feed is down and tries the network again on every call, and every rolled location in every section would pay for that (the Trends tab's
    st.cache_data holds an empty answer for its TTL the same way). Outside a build it is just _futures_curve()."""
    global _CURVE_MEMO
    if _LOAD_MEMO is None:
        return _futures_curve()
    if _CURVE_MEMO is None:
        _CURVE_MEMO = _futures_curve() or {}
    return _CURVE_MEMO


def _roll_spread(from_sym, to_sym):
    """Futures spread (cents, price(from) - price(to)) for a contract roll: the live curve while both legs trade, else the spread frozen at the
    last joint close in stored history (mirror of app._roll_spread)."""
    curve = _trend_curve_once()
    pf, pt = curve.get(from_sym), curve.get(to_sym)
    if pf is not None and pt is not None:
        return pf - pt
    key = (from_sym, to_sym)
    if key not in _FROZEN_SPREAD_MEMO:
        try:
            from database import get_roll_spread
            _FROZEN_SPREAD_MEMO[key] = get_roll_spread(from_sym, to_sym)
        except Exception:                                    # noqa: BLE001
            _FROZEN_SPREAD_MEMO[key] = None
    return _FROZEN_SPREAD_MEMO[key]


def _trend_adj(from_sym, to_sym):
    """{'adj': cents or None, 'rolled': bool[, 'unknown': True]} - mirror of app.get_adj: no adjustment inside one contract month (same letter, any
    year) or between two meal contracts; otherwise the from-to futures spread, else the manual fallback, else unknown."""
    if not from_sym or not to_sym or from_sym == to_sym:
        return {"adj": 0, "rolled": False}
    if from_sym[:2] == "ZM" and to_sym[:2] == "ZM":
        return {"adj": 0, "rolled": False}
    if len(from_sym) >= 3 and len(to_sym) >= 3 and from_sym[2] == to_sym[2] and from_sym[:2] == to_sym[:2]:
        return {"adj": 0, "rolled": False}
    sp = _roll_spread(from_sym, to_sym)
    if sp is not None:
        return {"adj": round(sp), "rolled": True}
    for r in _ROLL_ADJ:
        if r["from"] == from_sym and r["to"] == to_sym:
            return {"adj": r["adj"], "rolled": True}
    return {"adj": None, "rolled": True, "unknown": True}


def _trend_extract_bf(snap, grain, mode="spot"):
    """(basis, futures symbol) for a grain at delivery `mode` ('spot' = nearest), with the isSpot fallback - mirror of app._trend_extract. The symbol lets
    a move be spread-adjusted across a contract roll; None for a bare spot row."""
    if snap is None:
        return (None, None)
    if mode == "spot":
        cands = [r for r in snap.rows
                 if not r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None and r.futuresSymbol]
        if cands:
            r = min(cands, key=lambda r: _dp.deliv_key(r.deliveryMonth, r.futuresSymbol))
            return (r.basisCents, r.futuresSymbol)
        row = next((r for r in snap.rows if r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None), None)
        return (row.basisCents, row.futuresSymbol or None) if row else (None, None)
    matches = [r for r in snap.rows
               if not r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None
               and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode]
    if matches:
        r = min(matches, key=lambda r: _dp.slot_key(r.deliveryMonth))
        return (r.basisCents, r.futuresSymbol)
    row = next((r for r in snap.rows
                if r.isSpot and _grain_disp(r.grain) == grain and r.basisCents is not None
                and _dp.label(_dp.canonical(r.deliveryMonth, r.futuresSymbol)) == mode), None)
    return (row.basisCents, row.futuresSymbol or None) if row else (None, None)


def _trend_curve(snap, grain):
    """[(year, month, basis)] of a snapshot's forward rows for a grain, by delivery month (mirror of app._trend_curve)."""
    if snap is None:
        return []
    seen: dict = {}
    for r in snap.rows:
        if r.isSpot or _grain_disp(r.grain) != grain or r.basisCents is None:
            continue
        sym = r.futuresSymbol or ""
        if len(sym) < 5 or not sym[-2:].isdigit():
            continue
        mon = _CME_MONTH_TO_INT.get(sym[-3])
        if not mon:
            continue
        key = (2000 + int(sym[-2:]), mon)
        if key not in seen:
            seen[key] = r.basisCents
    return sorted((y, m, b) for (y, m), b in seen.items())


def _trend_spot_gt_next(snap, grain):
    """True when the nearest delivery month's basis is above the next one's (an inverted curve); None with fewer than two months."""
    c = _trend_curve(snap, grain)
    return None if len(c) < 2 else c[0][2] > c[1][2]


def build_trend_rows(facility_type: str, grain: str, mode: str = "spot") -> list[dict]:
    """Per-location current / ~1 week / ~1 month / ~1 year ago basis (and futures symbol) and spot > next, for a type + grain + delivery - mirror of
    app.build_trend_rows (same targets and tolerances, same grain-override guard, same region and river segment)."""
    from facility_overrides import effective_ftype
    pairs, meta, data, now = _load(facility_type)
    if not pairs:
        return []
    targets = {"current": (now, 1.6),
               "wk_ago":  (now - timedelta(days=7),   4),
               "mo_ago":  (now - timedelta(days=30),  4),
               "yr_ago":  (now - timedelta(days=365), 4)}
    rows = []
    for key in pairs:
        snaps = data.get(key, [])
        m     = meta.get(key, {})
        if effective_ftype(key[0], key[1], grain, m.get("facility_type")) != facility_type:
            continue      # this grain belongs to a different (overridden) category
        stt = m.get("state", "")
        rd = {"provider": key[0], "location": key[1],
              "region":  region_from_state(stt) or m.get("region", "") or "",
              "segment": river_segment(key[1])}
        for lbl, (tg, md) in targets.items():
            snap = _trend_closest(snaps, tg, md)
            rd[f"b_{lbl}"], rd[f"f_{lbl}"] = _trend_extract_bf(snap, grain, mode)
            if lbl == "current":
                rd["spot_gt_next"] = _trend_spot_gt_next(snap, grain)
        rows.append(rd)
    return [r for r in rows if r.get("b_current") is not None]


def _trend_move(r, win):
    """Current-vs-`win` basis move of a trend row, spread-adjusted if the reference contract rolled between the two (mirror of app._trend_move). None
    to leave the row out: a missing endpoint, or a roll with no spread on file."""
    bc, bw = r.get("b_current"), r.get(f"b_{win}")
    if bc is None or bw is None:
        return None
    d = bc - bw
    fc, fw = r.get("f_current"), r.get(f"f_{win}")
    if fc and fw and fc != fw:
        a = _trend_adj(fw, fc)
        if a.get("adj") is not None:
            return d - a["adj"]
        if a.get("unknown"):
            return None
    return d


def render_trend_cards_email(rows, group_field, groups) -> str:
    """The Trends tab's three tables for one section (app.render_trend_cards), as email-safe HTML, side by side in one row.
    Avg Basis Change (All / Firmer only / Weaker only) vs LW / LM / LY; Firmer / Weaker % by region (a river section: Avg Basis & Change by segment);
    Spot > Next, the % of locations inverted, by region (segment). `rows` are build_trend_rows rows, `groups` the East / West (or segment) names present.
    Built from table attributes (cellpadding, align, bgcolor) and a colour on the cell, not per-cell CSS: 11 sections of the old markup were 120 KB,
    and the attributes are what Outlook honours most reliably. The font comes from the table, as in the section tables."""
    is_river = (group_field == "segment")
    grp_lbl = "Segment" if is_river else "Region"
    WINS = [("wk_ago", "VS LW"), ("mo_ago", "VS LM"), ("yr_ago", "VS LY")]

    def _avg(xs):
        return (sum(xs) / len(xs)) if xs else None

    def _grows(gv):
        return [r for r in rows if (r.get(group_field) or "") == gv]

    def _moves(rs, win):
        return [m for r in rs if (m := _trend_move(r, win)) is not None]

    def _fc(v):
        return "&mdash;" if v is None else f"{'+' if v >= 0 else '&minus;'}{abs(v):.1f}"

    def _fp(v):
        return "&mdash;" if v is None else f"{round(v)}%"

    def _col(txt, v):               # a change: green up, red down, grey for none / zero
        if v is None or v == 0:
            return f'<td align="right" style="color:#64748b">{txt}</td>'
        return f'<td align="right" style="color:{_GAIN if v > 0 else _LOSS}"><b>{txt}</b></td>'

    def _hd(t, left=False):
        al = "" if left else ' align="right"'
        return f'<td{al} style="color:#64748b;font-size:9px"><b>{t}</b></td>'

    def _open(title, first="", extra_col=None):
        h = (f'<div style="font-size:10px;font-weight:700;color:{JPSI_DARK};padding:0 0 3px 1px">{title}</div>'
             f'<table cellpadding="4" cellspacing="0" style="font-size:11px"><tr bgcolor="#f1f5f9">{_hd(first, left=True)}')
        if extra_col:
            h += _hd(extra_col)
        for _, lab in WINS:
            h += _hd(lab)
        return h + '</tr>'

    # Avg Basis Change - All / Firmer / Weaker
    a = _open("AVG BASIS CHANGE (&cent;)")
    for grp, fn in (("All Plants",  lambda m: m),
                    ("Firmer only", lambda m: [x for x in m if x > 0]),
                    ("Weaker only", lambda m: [x for x in m if x < 0])):
        a += f'<tr><td nowrap><b>{grp}</b></td>'
        for win, _ in WINS:
            v = _avg(fn(_moves(rows, win)))
            a += _col(_fc(v), v)
        a += '</tr>'
    a += '</table>'

    if is_river:             # Avg Basis & Change by Segment
        mid = _open("AVG BASIS &amp; CHANGE BY SEGMENT (&cent;)", extra_col="AVG BASIS")
        for gv in groups:
            rs = _grows(gv)
            bl = _avg([r["b_current"] for r in rs if r.get("b_current") is not None])
            bt = "&mdash;" if bl is None else f"{'+' if bl >= 0 else '&minus;'}{abs(bl):.1f}"
            mid += f'<tr><td nowrap><b>{gv}</b></td><td align="right"><b>{bt}</b></td>'
            for win, _ in WINS:
                v = _avg(_moves(rs, win))
                mid += _col(_fc(v), v)
            mid += '</tr>'
    else:                    # Firmer / Weaker by Region: the share of locations that moved up / down
        mid = _open(f"FIRMER / WEAKER BY {grp_lbl.upper()}")
        for gv in groups:
            rs = _grows(gv)
            for firmer, lab2 in ((True, "Firmer"), (False, "Weaker")):
                mid += f'<tr><td nowrap><b>{gv}</b> <span style="color:#64748b">{lab2}</span></td>'
                for win, _ in WINS:
                    ms = _moves(rs, win)
                    pv = None if not ms else 100 * sum(1 for m in ms if (m > 0 if firmer else m < 0)) / len(ms)
                    mid += ('<td align="right" style="color:#cbd5e1">&mdash;</td>' if pv is None
                            else f'<td align="right" style="color:{_GAIN if firmer else _LOSS}"><b>{_fp(pv)}</b></td>')
                mid += '</tr>'
    mid += '</table>'

    # Spot > Next - the share of locations whose nearest month is above the next (an inverted curve)
    c = (f'<div style="font-size:10px;font-weight:700;color:{JPSI_DARK};padding:0 0 3px 1px">SPOT &gt; NEXT</div>'
         f'<table cellpadding="4" cellspacing="0" style="font-size:11px"><tr bgcolor="#f1f5f9">{_hd(grp_lbl.upper(), left=True)}{_hd("% INVERTED")}</tr>')
    for gv in groups:
        vs = [r.get("spot_gt_next") for r in _grows(gv) if r.get("spot_gt_next") is not None]
        iv = None if not vs else 100 * sum(1 for v in vs if v) / len(vs)
        c += f'<tr><td nowrap><b>{gv}</b></td><td align="right"><b>{_fp(iv)}</b></td></tr>'
    c += '</table>'

    return ('<table cellpadding="0" cellspacing="0" style="margin:4px 0 10px"><tr>'
            f'<td valign="top" style="padding-right:18px">{a}</td>'
            f'<td valign="top" style="padding-right:18px">{mid}</td>'
            f'<td valign="top">{c}</td></tr></table>')


def _index_excludes() -> set:
    """The locations marked as index outliers on the Trends tab (left out of its averages, so out of these too); an empty set if the list cannot be read."""
    try:
        from database import get_index_excludes
        return set(get_index_excludes() or ())
    except Exception as exc:                                  # noqa: BLE001
        log.warning("trend cards: no index-outlier list (%s); excluding nothing", str(exc)[:120])
        return set()


def _trend_cards_html(facility_type: str, grain: str, group_mode: str, mode: str, excluded: set) -> str:
    """The three trend tables for one section of the email, or '' when it has no locations - or when anything at all goes wrong: a problem in these
    tables must never stop the daily email."""
    try:
        rows = [r for r in build_trend_rows(facility_type, grain, mode) if (r["provider"], r["location"]) not in excluded]
        if not rows:
            return ""
        if group_mode == "segment":
            groups = [s for s in SEGMENT_ORDER if any((r.get("segment") or "") == s for r in rows)]
            gf = "segment"
        else:
            groups = [g for g in ("East", "West") if any((r.get("region") or "") == g for r in rows)]
            gf = "region"
        return render_trend_cards_email(rows, gf, groups)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("trend cards skipped for %s / %s: %s", facility_type, grain, exc)
        return ""


def _bcell(b) -> str:
    """Basis cell."""
    td = "padding:3px 6px;text-align:right;white-space:nowrap;border-bottom:1px solid #eef2f6"
    if b is None:
        return f'<td style="{td};color:#cbd5e1">—</td>'
    return f'<td style="{td};color:{JPSI_DARK};font-weight:700">{b:+d}</td>'


def _ccell(c) -> str:
    """Daily-change cell (colored; — when unchanged or no prior)."""
    td = "padding:3px 6px;text-align:right;white-space:nowrap;border-bottom:1px solid #eef2f6"
    if c is None or c == 0:
        return f'<td style="{td};color:#cbd5e1">—</td>'
    return f'<td style="{td};color:{_GAIN if c > 0 else _LOSS};font-weight:700">{c:+d}</td>'


def _ccell_roll(c, rolled) -> str:
    """Rolled month: show the spread-adjusted change with a ↻ marker; if it couldn't
    be adjusted (missing futures price), show ↻ alone."""
    td = "padding:3px 6px;text-align:right;white-space:nowrap;border-bottom:1px solid #eef2f6"
    if not rolled:
        return _ccell(c)
    if c is None:
        return f'<td style="{td};color:#d97706;font-weight:700">&#8635;</td>'
    col = _GAIN if c > 0 else (_LOSS if c < 0 else "#94a3b8")
    return (f'<td style="{td}"><span style="color:{col};font-weight:700">{c:+d}</span>'
            f'<span style="color:#d97706"> &#8635;</span></td>')


def _bcellf(b) -> str:
    """Segment avg-basis cell (1 decimal)."""
    td = "padding:3px 6px;text-align:right;white-space:nowrap;border-bottom:1px solid #eef2f6"
    if b is None:
        return f'<td style="{td};color:#cbd5e1">—</td>'
    return f'<td style="{td};color:{JPSI_DARK};font-weight:600">{b:+.1f}</td>'


def _ccellf(c) -> str:
    """Segment avg-change cell (1 decimal, colored)."""
    td = "padding:3px 6px;text-align:right;white-space:nowrap;border-bottom:1px solid #eef2f6"
    if c is None or round(c, 1) == 0:
        return f'<td style="{td};color:#cbd5e1">—</td>'
    return f'<td style="{td};color:{_GAIN if c > 0 else _LOSS};font-weight:700">{c:+.1f}</td>'


# Faint watermark tile for email tables. Outlook honors <table background=…>; used
# with transparent data cells so the mark shows through behind the numbers. Built once.
_TBL_WM_PATH: str | None = None
_TBL_WM_CID = "jsatblwm"


def _table_watermark() -> str | None:
    """Path to a faint JSA 50-Year watermark tile, or None if it can't be built."""
    global _TBL_WM_PATH
    if _TBL_WM_PATH and os.path.exists(_TBL_WM_PATH):
        return _TBL_WM_PATH
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "50 Year logo JSA.png")
    if not os.path.exists(src):
        return None
    try:
        from PIL import Image
        W, H = 460, 300
        logo = Image.open(src).convert("RGBA")
        lw, lh = logo.size
        s = min(W * 0.66 / lw, H * 0.66 / lh)
        logo = logo.resize((max(1, int(lw * s)), max(1, int(lh * s))), Image.LANCZOS)
        logo.putalpha(logo.split()[3].point(lambda p: int(p * 0.18)))  # faint watermark
        canvas = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        canvas.paste(logo, ((W - logo.width) // 2, (H - logo.height) // 2), logo)
        fh = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        canvas.save(fh, "PNG"); fh.close()
        _TBL_WM_PATH = fh.name
        return _TBL_WM_PATH
    except Exception as exc:
        log.warning("table watermark build failed: %s", exc)
        return None


def dashboard_cta_html(label: str = "Open the Basis Tracker",
                       url: str | None = None, hint: str | None = None) -> str:
    """A pronounced, centered JPSI-blue button linking to the VIEW-ONLY Basis Tracker,
    for the very top of the branded emails (daily changes + rail update). The line under
    it says "view-only" and shows the address, so a recipient (or a client that strips
    buttons) can see exactly where it goes. `hint` adds a short pointer after that."""
    url = url or VIEW_DASHBOARD_URL
    host = url.split("://", 1)[-1].rstrip("/")
    sub = "View-only dashboard" + (f" &middot; {hint}" if hint else "")
    return (
        f'<div style="background:#ffffff;padding:16px 18px 6px;text-align:center;'
        f'font-family:Arial,Helvetica,sans-serif">'
        f'<a href="{url}" style="display:inline-block;background:{JPSI_BLUE};color:#ffffff;'
        f'font-size:15px;font-weight:700;text-decoration:none;padding:12px 30px;'
        f'border-radius:6px;box-shadow:0 1px 3px rgba(6,147,227,.4)">'
        f'{label} &rarr;</a>'
        f'<div style="font-size:11px;color:#64748b;margin-top:7px">{sub} &middot; '
        f'<a href="{url}" style="color:#64748b;text-decoration:underline">{host}</a></div></div>'
    )


def _build_changes_email_html(mode: str = "spot") -> str:
    """The report itself (build_changes_email_html holds the shared snapshot load open around this)."""
    today = datetime.now()
    _ff   = "font-family:Arial,Helvetica,sans-serif"
    _hdr  = ("font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;"
             "padding:3px 6px")
    _wm = _table_watermark()
    _tbl_attr = f' background="cid:{_TBL_WM_CID}"' if _wm else ""
    _tbl_css = (f"background-image:url('cid:{_TBL_WM_CID}');background-position:center;"
                f"background-repeat:no-repeat;background-size:contain;") if _wm else ""

    excluded = _index_excludes()         # the Trends tab's index outliers, left out of the trend cards as there
    _cards_shown = False
    body = ""
    for ttl, ft, gr, gmode in TREND_CATEGORIES:
        body += (f'<div style="margin:16px 0 5px;font-size:13px;font-weight:700;color:{JPSI_BLUE};'
                 f'border-bottom:2px solid {JPSI_BLUE};padding-bottom:3px">{ttl}</div>')
        _cards = _trend_cards_html(ft, gr, gmode, mode, excluded)       # the Trends tab's tables, above the section's own table
        _cards_shown = _cards_shown or bool(_cards)
        body += _cards
        if gmode == "segment":
            result = build_segment_change_rows(ft, gr, mode)
            rows   = result["rows"]
            if not rows:
                body += '<div style="font-size:12px;color:#94a3b8;padding:2px 6px">No data.</div>'
                continue
            _h2 = ("font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;"
                   "padding:2px 6px;text-align:right")
            body += (f'<table width="100%"{_tbl_attr} style="border-collapse:collapse;font-size:12px;{_tbl_css}">'
                     f'<tr><td style="{_hdr}" rowspan="2">Segment</td>'
                     f'<td style="{_hdr};text-align:center" colspan="2">{result["m1_label"] or ""}</td>'
                     f'<td style="{_hdr};text-align:center" colspan="2">{result["m2_label"] or ""}</td></tr>'
                     f'<tr><td style="{_h2}">Avg Basis</td><td style="{_h2}">Δ</td>'
                     f'<td style="{_h2}">Avg Basis</td><td style="{_h2}">Δ</td></tr>')
            for i, r in enumerate(rows):
                body += (f'<tr>'
                         f'<td style="padding:3px 6px;color:{JPSI_DARK};border-bottom:1px solid #eef2f6">{r["segment"]}</td>'
                         + _bcellf(r["b1"]) + _ccellf(r["c1"]) + _bcellf(r["b2"]) + _ccellf(r["c2"]) + '</tr>')
            body += '</table>'
        else:
            result = build_change_rows(ft, gr, mode)
            rows = result["rows"]
            if not rows:
                body += '<div style="font-size:12px;color:#94a3b8;padding:2px 6px">No changes today.</div>'
                continue
            _h2 = ("font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;"
                   "padding:2px 6px;text-align:right")
            body += (f'<table width="100%"{_tbl_attr} style="border-collapse:collapse;font-size:12px;{_tbl_css}">'
                     f'<tr><td style="{_hdr}" rowspan="2">Location</td>'
                     f'<td style="{_hdr};text-align:center" colspan="2">{result["m1_label"] or ""}</td>'
                     f'<td style="{_hdr};text-align:center" colspan="2">{result["m2_label"] or ""}</td></tr>'
                     f'<tr><td style="{_h2}">Basis</td><td style="{_h2}">Δ</td>'
                     f'<td style="{_h2}">Basis</td><td style="{_h2}">Δ</td></tr>')
            for i, r in enumerate(rows):
                loc = adm_city_from_name(r["location"]) if r["provider"] == "ADM" else r["location"]
                if r["roll1"] or r["roll2"]:
                    rf  = _short_fut(r["roll1_from"] or r["roll2_from"])
                    rt  = _short_fut(r["roll1_to"] or r["roll2_to"])
                    tag = (f' <span style="font-size:9px;color:#fff;background:#d97706;'
                           f'padding:1px 5px;border-radius:3px">&#8635; {rf}&rarr;{rt}</span>')
                elif r.get("m1_sym"):    # persistent note of the contract the spot is vs
                    tag = (f' <span style="font-size:9px;color:#94a3b8">'
                           f'vs {_short_fut(r["m1_sym"])}</span>')
                else:
                    tag = ""
                body += (f'<tr>'
                         f'<td style="padding:3px 6px;color:{JPSI_DARK};border-bottom:1px solid #eef2f6">'
                         f'<b style="color:{JPSI_DARK}">{r["provider"]}</b> {loc}{tag}</td>'
                         + _bcell(r["b1"]) + _ccell_roll(r["c1"], r["roll1"])
                         + _bcell(r["b2"]) + _ccell_roll(r["c2"], r["roll2"]) + '</tr>')
            body += '</table>'

    if _cards_shown:
        body += ('<div style="margin-top:10px;font-size:10px;color:#94a3b8">'
                 'Trend tables (the Trends tab): each location at its nearest delivery month, now vs about a week (LW), a month (LM) and a year (LY) ago, '
                 'spread-adjusted across contract rolls; firmer = a higher basis. East / West is split at the Mississippi River and river terminals are '
                 'grouped by river segment. Locations excluded from the index on the Trends tab are left out.</div>')
    if "&#8635;" in body:
        body += ('<div style="margin-top:10px;font-size:10px;color:#d97706">'
                 '&#8635; = rolled to a new futures contract — the change is spread-adjusted '
                 '(the contract spread is added back so it reflects the true basis move).</div>')

    return (
        f'<div style="max-width:680px;margin:0;{_ff};border:1px solid #e2e8f0;'
        f'border-radius:8px;overflow:hidden">'
        f'<table width="100%" style="background:{JPSI_DARK};border-collapse:collapse"><tr>'
        f'<td style="padding:14px 18px"><img src="{JPSI_LOGO}" '
        f'alt="John Stewart &amp; Associates" height="30" style="display:block;height:30px"></td>'
        f'<td align="right" style="padding:14px 18px;color:#ffffff">'
        f'<div style="font-size:16px;font-weight:700">Daily Basis Changes</div>'
        f'<div style="font-size:12px;color:#cbd5e1">{today.day} {today.strftime("%b %Y")} '
        f'· nearest & next delivery month vs prior posting</div></td></tr></table>'
        f'{dashboard_cta_html()}'
        f'<div style="padding:8px 18px 14px;background:#ffffff">{body}</div>'
        f'<div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:10px 18px;'
        f'font-size:11px;color:#64748b">John Stewart &amp; Associates · '
        f'Commodity &amp; Ag Risk Management Specialists · '
        f'<a href="https://www.jpsi.com" style="color:{JPSI_BLUE};text-decoration:none">jpsi.com</a></div>'
        f'</div>'
    )


def build_changes_email_html(mode: str = "spot") -> str:
    """A branded, email-ready HTML report of daily basis changes (JPSI styling). The sections and the trend tables of one facility type share ONE
    snapshot load while it is built (_load); the memo is dropped again whatever happens, so a later call never reads this one's data."""
    global _LOAD_MEMO, _CURVE_MEMO
    _LOAD_MEMO = {}
    _CURVE_MEMO = None
    try:
        return _build_changes_email_html(mode)
    finally:
        _LOAD_MEMO = None
        _CURVE_MEMO = None


_SIG_LOGO     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "50 Year logo JSA.png")
_SIG_LOGO_CID = "jsa50yr"
_TEAMS_URL    = os.getenv("SIG_TEAMS_URL",
                          "https://teams.microsoft.com/l/chat/0/0?users=kpostin@jpsi.com")
_WHATSAPP_URL = os.getenv("SIG_WHATSAPP_URL", "https://wa.me/18776711670")
_DISCLAIMER = (
    "Trading commodity futures, options on futures, cash commodities, and over-the-counter "
    "derivative products involves substantial risk of loss and may not be suitable for all "
    "investors. This communication is provided for informational purposes only and does not "
    "constitute investment advice, a recommendation, or an offer or solicitation to buy or sell "
    "any futures, options, cash commodities, or derivative products. John Stewart &amp; "
    "Associates, Inc. does not accept orders to buy or sell any financial instruments via email. "
    "The information contained herein has been obtained from sources believed to be reliable; "
    "however, its accuracy and completeness are not guaranteed. Any opinions expressed are solely "
    "those of the author, are subject to change without notice, and should not be relied upon as a "
    "basis for investment decisions. Past performance is not indicative of future results. This "
    "message may contain confidential or proprietary information intended solely for the use of the "
    "designated recipient. &copy; John Stewart &amp; Associates, Inc. 2026"
)


def signature_html() -> str:
    """Kolten Postin's JSA email signature (logo via cid:, disclaimer included)."""
    logo = (f'<img src="cid:{_SIG_LOGO_CID}" alt="John Stewart &amp; Associates - 50 Years" '
            f'height="64" style="height:64px;display:block;margin:8px 0 6px">'
            if os.path.exists(_SIG_LOGO) else "")
    b = "font-weight:bold"
    return (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#32373c;'
        f'margin-top:20px;border-top:1px solid #e2e8f0;padding-top:12px">'
        f'<div style="{b};font-size:13px">Kolten A. Postin</div>'
        f'<div style="color:#5b6770">John Stewart &amp; Associates</div>'
        f'<div style="{b}">Commodity Broker</div>'
        f'<div style="{b}">Phone 877-671-1670</div>'
        f'<div style="{b}">Email: <a href="mailto:kpostin@jpsi.com" '
        f'style="color:{JPSI_BLUE};text-decoration:none">kpostin@jpsi.com</a></div>'
        f'<div style="{b};margin-top:8px"><a href="{_TEAMS_URL}" '
        f'style="color:{JPSI_BLUE};text-decoration:none">MS TEAMS: Chat with me on Microsoft Teams</a></div>'
        f'<div style="{b}"><a href="{_WHATSAPP_URL}" '
        f'style="color:{JPSI_BLUE};text-decoration:none">WhatsApp: Chat with me on WhatsApp Here</a></div>'
        f'{logo}'
        f'<div style="font-size:9px;color:#9aa0a6;line-height:1.45;margin-top:10px;'
        f'max-width:680px">{_DISCLAIMER}</div>'
        f'</div>'
    )


def send_via_outlook(subject: str, html: str, to_addr: str, cc: str | None = None,
                     inline_images: dict | None = None, bcc: str | None = None) -> None:
    """Send an HTML email via the local, logged-in Outlook desktop app.

    `inline_images` is an optional {content_id: filepath} map for images referenced
    in the HTML as <img src="cid:content_id">.
    """
    import win32com.client as win32  # pywin32 (local/dev only)
    from email.utils import getaddresses
    # Outlook wants ';'-separated recipients; our cc/bcc may be comma-separated
    # (multiple addresses). Re-join to bare addresses on ';' so both work.
    def _semi(v):
        return "; ".join(a for _, a in getaddresses([v]) if a) if v else v
    outlook = win32.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = _semi(to_addr)
    if cc:
        mail.CC = _semi(cc)
    if bcc:
        mail.BCC = _semi(bcc)
    mail.Subject = subject
    mail.HTMLBody = html
    for cid, path in (inline_images or {}).items():
        if os.path.exists(path):
            att = mail.Attachments.Add(path)
            att.PropertyAccessor.SetProperty(
                "http://schemas.microsoft.com/mapi/proptag/0x3712001F", cid)
    mail.Send()


def _email_cfg(key: str, default=None):
    """Read an SMTP setting from the environment, falling back to st.secrets (Cloud)."""
    v = os.getenv(key)
    if v:
        return v
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return default


def send_via_smtp(subject: str, html: str, to_addr: str, cc: str | None = None,
                  inline_images: dict | None = None, bcc: str | None = None) -> None:
    """Send the HTML email over SMTP (for environments without Outlook, e.g. Streamlit
    Cloud). Config from env or st.secrets: SMTP_HOST, SMTP_PORT (default 587),
    SMTP_USER, SMTP_PASS, SMTP_FROM (defaults to SMTP_USER). Inline images embed by
    Content-ID exactly like the Outlook path."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage

    host = _email_cfg("SMTP_HOST")
    user = _email_cfg("SMTP_USER")
    pw   = _email_cfg("SMTP_PASS")
    port = int(_email_cfg("SMTP_PORT", "587"))
    frm  = _email_cfg("SMTP_FROM") or user
    if not host or not frm:
        raise RuntimeError("SMTP not configured (need SMTP_HOST and SMTP_FROM/SMTP_USER).")

    root = MIMEMultipart("related")
    root["Subject"], root["From"], root["To"] = subject, frm, to_addr
    if cc:
        root["Cc"] = cc
    alt = MIMEMultipart("alternative"); root.attach(alt)
    alt.attach(MIMEText("This message is best viewed in an HTML-capable email client.", "plain"))
    alt.attach(MIMEText(html, "html"))
    for cid, path in (inline_images or {}).items():
        if os.path.exists(path):
            with open(path, "rb") as fh:
                img = MIMEImage(fh.read())
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=os.path.basename(path))
            root.attach(img)
    # BCC is an envelope-only recipient — added to the send list but NOT to any header.
    rcpts = [to_addr] + ([cc] if cc else [])
    if bcc:
        from email.utils import getaddresses
        rcpts += [a for _, a in getaddresses([bcc]) if a]
    # Port 465 = implicit TLS (SMTPS); 587/other = STARTTLS. Login only when creds
    # are given, so a no-auth relay still works. Broadens support to any relay the
    # desk stands up (Brevo/SendGrid/etc.) while IT's Graph path is pending.
    conn = smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 \
        else smtplib.SMTP(host, port, timeout=30)
    with conn as s:
        if port != 465:
            try:
                s.starttls()
            except smtplib.SMTPNotSupportedError:
                pass
        if user and pw:
            s.login(user, pw)
        s.sendmail(frm, rcpts, root.as_string())


def _graph_configured() -> bool:
    """True when the Microsoft Graph app-only credentials are present."""
    return bool(_email_cfg("GRAPH_TENANT_ID") and _email_cfg("GRAPH_CLIENT_ID")
                and _email_cfg("GRAPH_CLIENT_SECRET"))


def send_via_graph(subject: str, html: str, to_addr: str, cc: str | None = None,
                   inline_images: dict | None = None, bcc: str | None = None) -> None:
    """Send the HTML email through Microsoft Graph using the OAuth 2.0
    client-credentials (app-only) flow — the Cloud-friendly path IT approved.

    No user or service-account password: the app authenticates as itself with a
    client secret and calls Graph's sendMail as the shared mailbox. Config from
    env or st.secrets:
        GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET  (the registration)
        GRAPH_SENDER (the mailbox to send as, e.g. basis-tracker@jpsi.com)
    Inline images embed by Content-ID exactly like the Outlook/SMTP paths."""
    import base64
    import msal
    import requests

    tenant = _email_cfg("GRAPH_TENANT_ID")
    client = _email_cfg("GRAPH_CLIENT_ID")
    secret = _email_cfg("GRAPH_CLIENT_SECRET")
    sender = _email_cfg("GRAPH_SENDER") or _email_cfg("SMTP_FROM")
    if not (tenant and client and secret and sender):
        raise RuntimeError("Graph not configured (need GRAPH_TENANT_ID, "
                           "GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_SENDER).")

    app = msal.ConfidentialClientApplication(
        client_id=client,
        authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=secret,
    )
    tok = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    access = tok.get("access_token")
    if not access:
        raise RuntimeError(f"Graph auth failed: {tok.get('error_description') or tok}")

    def _recips(addrs: str | None):
        if not addrs:
            return []
        from email.utils import getaddresses
        return [{"emailAddress": {"address": a}} for _, a in getaddresses([addrs]) if a]

    # Display name on the From line — the shared mailbox's directory name would
    # otherwise show. Override with GRAPH_FROM_NAME; address stays the sender.
    from_name = _email_cfg("GRAPH_FROM_NAME") or "JSA Basis Tracker"
    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html},
        "toRecipients": _recips(to_addr),
        "from": {"emailAddress": {"address": sender, "name": from_name}},
    }
    if cc:
        message["ccRecipients"] = _recips(cc)
    if bcc:
        message["bccRecipients"] = _recips(bcc)

    # Inline images ride as attachments with a Content-ID and isInline=true, so the
    # HTML's cid: references resolve — the Graph analogue of the MIME-related part.
    atts = []
    for cid, path in (inline_images or {}).items():
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode("ascii")
            atts.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": os.path.basename(path),
                "contentId": cid,
                "isInline": True,
                "contentBytes": data,
            })
    if atts:
        message["attachments"] = atts

    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {access}",
                 "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": True},
        timeout=30,
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"Graph sendMail failed [{resp.status_code}]: {resp.text[:300]}")


def send_email(subject: str, html: str, to_addr: str, cc: str | None = None,
               inline_images: dict | None = None, bcc: str | None = None) -> str:
    """Dispatch: local Outlook if available, else Microsoft Graph (Cloud), else SMTP.
    Returns which path was used ('outlook' | 'graph' | 'smtp'); raises if all fail."""
    try:
        import win32com.client  # noqa: F401  (present only on the local machine)
        send_via_outlook(subject, html, to_addr, cc=cc, inline_images=inline_images, bcc=bcc)
        return "outlook"
    except Exception as e_out:
        errors = [f"Outlook: {e_out}"]
        if _graph_configured():
            try:
                send_via_graph(subject, html, to_addr, cc=cc,
                               inline_images=inline_images, bcc=bcc)
                return "graph"
            except Exception as e_graph:
                errors.append(f"Graph: {e_graph}")
        else:
            # Say so: this used to be skipped silently, and the only error left was the SMTP one (the interim relay,
            # long dead), which sent a whole evening chasing a bad SMTP login instead of the missing Graph secrets.
            errors.append("Graph: NOT CONFIGURED — GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET are missing "
                          "from this app's environment / secrets (Cloud: Settings → Secrets, top level, above any [section])")
        try:
            send_via_smtp(subject, html, to_addr, cc=cc, inline_images=inline_images, bcc=bcc)
            return "smtp"
        except Exception as e_smtp:
            errors.append(f"SMTP: {e_smtp}")
            raise RuntimeError("Email send failed — " + "; ".join(errors))


# When the mail service itself is down or busy (Exchange Online / Graph: HTTP 429 or 5xx, a timeout, a dropped connection) one try is not enough for the
# unattended 15:45 run: the email would be lost and, with every alert going through the same service, nobody told. Kolten 2026-11-12: "Outlook is down and
# could cause an issue" (Graph answered 502 / 503 for the tracker's mailbox that afternoon). The run passes these waits (seconds before each further try:
# eight more tries over about 35 minutes, well inside cron-alert's 90-minute limit); a person at a button gets one try and the error, as before.
DAILY_SEND_RETRY_WAITS = (120, 180, 300, 300, 300, 300, 300, 300)

_TRANSIENT_MAIL = re.compile(r"\[(?:429|5\d\d)\]|timed out|timeout|connection (?:aborted|reset|refused|error)|max retries exceeded|remote ?disconnected"
                             r"|service unavailable|temporarily unavailable", re.I)


def transient_send_error(exc) -> bool:
    """Whether a failed send is worth waiting out: the mail service is busy or unreachable (Graph's HTTP 429 / 5xx, a timeout, a dropped connection).
    A bad credential, a missing setting or a refused message (4xx) fails the same way later, so it is not."""
    return bool(_TRANSIENT_MAIL.search(str(exc)))


def send_with_retry(send, waits=(), what: str = "email", sleep=None):
    """send() once; if it fails with a transient mail-service error, wait waits[0] seconds and try again, then waits[1], and so on, and re-raise the last
    error when the waits run out. Any other error is raised at once. No waits (the default) is exactly one try."""
    sleep = sleep or time.sleep
    for i in range(len(waits) + 1):
        try:
            return send()
        except Exception as exc:                              # noqa: BLE001
            if i >= len(waits) or not transient_send_error(exc):
                raise
            log.warning("%s: the mail service is not taking mail (%s); try %d of %d in %d s", what, str(exc)[:200], i + 2, len(waits) + 1, waits[i])
            sleep(waits[i])


def _peak_rss_mb():
    """This process's peak resident memory so far, in MB (Linux / macOS); None where it cannot be told (Windows). Never raises."""
    try:
        import resource
        import sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss          # kilobytes on Linux, bytes on macOS
        return int(peak / (1024 * 1024 if sys.platform == "darwin" else 1024))
    except Exception:                                                      # noqa: BLE001
        return None


def send_daily_changes_email(to_addr: str | None = None, cc: str | None = None,
                             mode: str = "spot", retry_waits=()) -> bool:
    """Build the daily Changes report and email it via Outlook. Returns True on success.

    `cc` defaults to the CHANGES_EMAIL_CC env var (so a standing copy recipient can
    be configured for the scheduled daily send). `retry_waits`: seconds to wait before each
    further try when the mail service is down or busy (DAILY_SEND_RETRY_WAITS); the report is
    built once and the same message is sent again. Empty = one try."""
    to_addr = to_addr or DEFAULT_TO
    if cc is None:
        cc = os.getenv("CHANGES_EMAIL_CC") or None
    _pk0, _t0 = _peak_rss_mb(), time.time()
    html = build_changes_email_html(mode) + signature_html()
    # The 2 GB droplet's out-of-memory killer ended the 11-12 15:45 run inside this build (rc=137, no email): the line makes the next run's footprint visible.
    _pk1 = _peak_rss_mb()
    log.info("Daily Changes email built in %.0f s (%.0f KB); this process's peak memory: %s MB before the build, %s MB after",
             time.time() - _t0, len(html.encode("utf-8")) / 1024, "?" if _pk0 is None else _pk0, "?" if _pk1 is None else _pk1)
    subject = f"JSA Daily Basis Changes - {datetime.now():%b %d, %Y}"
    imgs = {_SIG_LOGO_CID: _SIG_LOGO} if os.path.exists(_SIG_LOGO) else {}
    _wm = _table_watermark()
    if _wm:
        imgs[_TBL_WM_CID] = _wm
    bcc = os.getenv("DAILY_BIDS_BCC") or DAILY_BIDS_BCC
    _via = send_with_retry(lambda: send_email(subject, html, to_addr, cc=cc, inline_images=imgs or None, bcc=bcc),
                           retry_waits, what="Daily Changes email")
    log.info("Daily Changes email sent via %s to %s (cc %s | bcc %s)",
             _via, to_addr, cc or "—", bcc or "—")
    return True


if __name__ == "__main__":
    import argparse
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser(description="Daily Basis Changes report")
    ap.add_argument("--html", action="store_true", help="Print the report HTML to stdout")
    ap.add_argument("--send", action="store_true", help="Email the report via Outlook")
    ap.add_argument("--to", default=None, help=f"Recipient (default: {DEFAULT_TO})")
    a = ap.parse_args()
    if a.send:
        send_daily_changes_email(a.to)
        print("Sent.")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(build_changes_email_html())
