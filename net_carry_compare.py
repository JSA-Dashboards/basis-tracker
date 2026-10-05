"""net_carry_compare.py — the Net Carry tab's side-by-side comparison of several locations / corridors.

One column per location, one row per delivery month, the same measure in every cell:
  • net   — Net of Interest (Basis vs REF less the interest to carry there),
  • gross — Basis vs REF, before interest (the carry the market pays, before the cost of money).

For the columns to be comparable every location is expressed against the SAME reference contract, on
the SAME futures curve, with the SAME interest clock (one anchor month, one rate); a location that
quotes off a different contract is re-based to it with the futures spread, exactly as on the main table
(net_carry.compute_net_carry). Each column's top (the month where its net / gross peaks) is marked and the
best value in every month is picked out across locations.

Pure functions plus an HTML renderer — no Streamlit here, so it can be unit-tested.
"""
from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import date

import net_carry as nc

MEASURES = ("net", "gross")
AMBER_BG, AMBER = "#fff4e5", "#f28e2b"
GREEN = "#0a7f3f"


@dataclass
class Entry:
    """One location or corridor in the comparison."""
    key: str                         # unique id, e.g. 'b|ADM|St. Louis, MO (Elevator)' / 'r|CSX Columbus'
    name: str                        # what the column header says
    kind: str                        # 'basis' | 'rail'
    items: list = field(default_factory=list)   # [{delivery, futures, basis}] — its quotes on its quote date
    quote_date: date | None = None   # the posting the items come from
    is_main: bool = False            # the location the tab is showing (always the first column)


def value_of(point: dict, measure: str):
    """The number a point contributes: net of interest, or basis vs REF (gross, before interest)."""
    return point["net"] if measure == "net" else point["basis_ref"]


def top_for(points: list[dict], anchor_ym, measure: str):
    """net_carry.top_of_net_carry on the chosen measure (for 'gross' the value is the basis vs REF)."""
    pts = points if measure == "net" else [dict(p, net=p["basis_ref"]) for p in points]
    return nc.top_of_net_carry(pts, anchor_ym)


def build_comparison(entries: list[Entry], ref_symbol, curve: dict, anchor_month: int, anchor_ym,
                     annual_rate: float, measure: str = "net", asof: date | None = None) -> dict:
    """Compute every entry's monthly curve against ONE reference and lay them side by side.

    Returns {measure, ref, anchor_ym, months:[(ym, label)], columns:[...], row_best:{ym: [col idx]}}.
    Each column: {entry, points:{ym: {value, label, delivery, nc, converted, slots}}, top, front,
    stale, no_data}. `top` is net_carry.top_of_net_carry's dict for the measure; `front` the first
    month at or after the carry start (the value the gain is measured from)."""
    if measure not in MEASURES:
        raise ValueError(f"measure must be one of {MEASURES}")
    cols = []
    months: dict = {}
    for e in entries:
        col = {"entry": e, "points": {}, "top": None, "front": None, "no_data": not e.items,
               "stale": bool(asof and e.quote_date and e.quote_date != asof)}
        if e.items:
            rows, _meta = nc.compute_net_carry(e.items, ref_symbol, curve, anchor_month, annual_rate,
                                               anchor_ym=anchor_ym)
            pts = nc.monthly_carry(rows)
            for p in pts:
                col["points"][p["ym"]] = {
                    "value": value_of(p, measure), "label": p["label"], "delivery": p["delivery"],
                    "nc": bool(p["new_crop"]), "converted": rows[p["row"]].converted, "slots": p["slots"]}
                months.setdefault(p["ym"], f"{nc._ABBR[p['ym'][1]]} {p['ym'][0] % 100:02d}")
            col["top"] = top_for(pts, anchor_ym, measure)
            if col["top"]:
                first = next((p for p in pts if anchor_ym is None or p["ym"] >= anchor_ym), None)
                col["front"] = first
            col["no_data"] = not pts
        cols.append(col)
    ordered = sorted(months.items(), key=lambda kv: kv[0][0] * 12 + kv[0][1])
    row_best = {}
    for ym, _lab in ordered:
        vals = [(i, c["points"][ym]["value"]) for i, c in enumerate(cols) if ym in c["points"]]
        if len(vals) >= 2:
            best = max(v for _, v in vals)
            row_best[ym] = [i for i, v in vals if abs(v - best) < 1e-9]
    return {"measure": measure, "ref": ref_symbol, "anchor_ym": anchor_ym, "months": ordered,
            "columns": cols, "row_best": row_best}


def front_value(col: dict, measure: str):
    f = col.get("front")
    return None if f is None else value_of(f, measure)


def gain(col: dict, measure: str):
    """Carry gained from the front to the top month on the measure (net: after interest; gross: before)."""
    t = col.get("top")
    return None if not t else t["gain"]


def _fv(v) -> str:
    return "—" if v is None else f"{v:+.1f}"


def render_html(res: dict) -> str:
    """The comparison as one HTML table, styled like the main Net Carry table."""
    measure = res["measure"]
    cols = res["columns"]
    th_base = ("padding:6px 10px;border-bottom:2px solid #cbd5e1;font-size:11px;color:#475569;"
               "vertical-align:bottom;line-height:1.3")
    head = f'<th style="{th_base};text-align:left;white-space:nowrap">DELIVERY</th>'
    for c in cols:
        e = c["entry"]
        icon = "🚂 " if e.kind == "rail" else ""
        star = '<span style="color:#f28e2b" title="the location above">★ </span>' if e.is_main else ""
        sub = ""
        if c["no_data"]:
            sub = '<div style="font-weight:400;color:#b45309;font-size:10px">no recent quote</div>'
        elif c["stale"] and e.quote_date:
            sub = (f'<div style="font-weight:400;color:#b45309;font-size:10px">'
                   f'as of {e.quote_date.month}/{e.quote_date.day}</div>')
        head += (f'<th style="{th_base};text-align:right;min-width:104px;text-transform:uppercase;'
                 f'letter-spacing:.02em">{star}{icon}{html.escape(e.name)}{sub}</th>')

    body = ""
    for ym, label in res["months"]:
        tds = f'<td style="padding:5px 10px;border-bottom:1px solid #eef2f6;font-size:13px;white-space:nowrap">{label}</td>'
        for i, c in enumerate(cols):
            p = c["points"].get(ym)
            if p is None:
                tds += '<td style="padding:5px 10px;border-bottom:1px solid #eef2f6;text-align:right;color:#cbd5e1">—</td>'
                continue
            is_top = bool(c["top"]) and c["top"]["ym"] == ym
            best = i in res["row_best"].get(ym, [])
            style = ("padding:5px 10px;border-bottom:1px solid #eef2f6;text-align:right;font-size:13px;"
                     "font-variant-numeric:tabular-nums;white-space:nowrap")
            if is_top:
                style += f";background:{AMBER_BG}"
            if best or is_top:
                style += ";font-weight:700"
            if best:
                style += f";color:{GREEN}"
            tag = ""
            if p["nc"]:
                tag += ('<span style="color:#94a3b8;font-size:9px;font-weight:700;margin-right:5px">NC</span>')
            num = _fv(p["value"])
            if is_top:
                num += f'<span style="color:{AMBER};font-size:10px;margin-left:3px">▲</span>'
            if not p["converted"]:
                num += '<span style="color:#c0392b" title="no futures spread to the reference — raw basis"> ·</span>'
            tds += f'<td style="{style}">{tag}{num}</td>'
        body += f"<tr>{tds}</tr>"

    what = "net of interest" if measure == "net" else "gross carry (before interest)"
    top_lbl = f"Top of {'net' if measure == 'net' else 'gross'} carry"
    gain_lbl = f"{'Net' if measure == 'net' else 'Gross'} carry vs the start"
    foot_td = "padding:6px 10px;border-top:2px solid #cbd5e1;font-size:12px;text-align:right;font-variant-numeric:tabular-nums"
    foot2_td = "padding:4px 10px 6px;font-size:12px;text-align:right;font-variant-numeric:tabular-nums"
    row_top = f'<td style="{foot_td};text-align:left;font-weight:700;color:#475569">{top_lbl}</td>'
    row_gain = f'<td style="{foot2_td};text-align:left;font-weight:700;color:#475569">{gain_lbl}</td>'
    row_date = f'<td style="{foot2_td};text-align:left;color:#94a3b8">Quoted</td>'
    for c in cols:
        t, e = c["top"], c["entry"]
        if t:
            row_top += (f'<td style="{foot_td};background:{AMBER_BG};font-weight:700">{html.escape(t["label"])}'
                        f'<div style="font-weight:600;color:#7c2d12">{_fv(t["net"])}</div></td>')
            fr = c["front"]
            row_gain += (f'<td style="{foot2_td};font-weight:700">{_fv(t["gain"])}'
                         f'<div style="font-weight:400;color:#94a3b8;font-size:10px">vs {html.escape(fr["label"]) if fr else "—"}</div></td>')
        else:
            row_top += f'<td style="{foot_td};color:#cbd5e1">—</td>'
            row_gain += f'<td style="{foot2_td};color:#cbd5e1">—</td>'
        qd = f"{e.quote_date.month}/{e.quote_date.day}" if e.quote_date else "—"
        row_date += f'<td style="{foot2_td};color:#94a3b8">{qd}</td>'

    return (f'<div style="font-size:12px;font-weight:700;color:#32373c;margin:4px 0 6px">'
            f'{what[0].upper() + what[1:]} by delivery month · vs {res["ref"] or "each location\'s own futures"}</div>'
            f'<div style="overflow-x:auto"><table style="border-collapse:collapse;min-width:{140 + 104 * len(cols)}px">'
            f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody>'
            f'<tfoot><tr>{row_top}</tr><tr>{row_gain}</tr><tr>{row_date}</tr></tfoot></table></div>')


# ── choosing what to compare ──────────────────────────────────────────────────────────────────
def haversine_km(a, b) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def nearest_peers(main_key, coords: dict, candidates, n: int = 3) -> list:
    """The `n` candidate keys closest to the main one (needs coordinates for both); fewer if too few
    candidates have a position. The main key itself is never returned."""
    if main_key not in coords:
        return []
    here = coords[main_key]
    scored = sorted((haversine_km(here, coords[k]), k) for k in candidates if k != main_key and k in coords)
    return [k for _, k in scored[:n]]


def rail_peers(main_market: str, markets, n: int = 3) -> list:
    """Corridors to compare a rail corridor with: the same railroad first ('UP Group 3' -> the other 'UP …'),
    then the rest, alphabetical; never the corridor itself."""
    mine = main_market.split()[0] if main_market else ""
    others = sorted(m for m in set(markets) if m != main_market)
    same = [m for m in others if m.split()[0] == mine]
    rest = [m for m in others if m.split()[0] != mine]
    return (same + rest)[:n]


def pick_latest(dates, asof: date, max_age_days: int = 10):
    """The newest posting date on or before `asof` and not older than `max_age_days` (else None)."""
    ok = [d for d in dates if d <= asof and (asof - d).days <= max_age_days]
    return max(ok) if ok else None


def snapshot_items(snap, grain: str, grain_disp) -> list[dict]:
    """A basis snapshot's forward quotes for one display grain, as net_carry items."""
    return [{"delivery": r.deliveryMonth, "futures": r.futuresSymbol, "basis": r.basisCents}
            for r in snap.rows
            if not r.isSpot and r.basisCents is not None and grain_disp(r.grain) == grain]


def rail_items(rows: list[dict], market: str, commodity: str, date_str: str) -> list[dict]:
    """A rail corridor's quotes on one posting date as net_carry items (period = the delivery label)."""
    return [{"delivery": r["period"], "futures": r.get("futures"), "basis": r["bid"]}
            for r in rows
            if r["market"] == market and (r.get("commodity") or "Corn") == commodity
            and str(r["date"])[:10] == date_str[:10] and r.get("bid") is not None]


def rail_dates(rows: list[dict], market: str, commodity: str) -> list[date]:
    """Every posting date a corridor has for a commodity."""
    out = set()
    for r in rows:
        if r["market"] == market and (r.get("commodity") or "Corn") == commodity and r.get("bid") is not None:
            out.add(date.fromisoformat(str(r["date"])[:10]))
    return sorted(out)
