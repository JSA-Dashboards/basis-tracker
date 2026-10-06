"""return_to_carry_view.py — how the Return to Carry tracker looks on the Net Carry tab.

Pure builders (HTML strings and Altair charts) over the rows return_to_carry_data produces, so they can be tested
without Streamlit:

  headline_html(rows, measure)      the strip of four numbers for the latest crop year
  table_html(rows, measure)         one line per crop year, the latest highlighted, with an average line
  best_bar_chart(rows, measure)     the best return of each crop year (the latest in orange)
  seasonal_chart(results, measure)  every week's return across the crop year: the historical range as a band, the
                                    median, and the latest two years on top — "where are we vs. a normal year"

Colours match the rest of the tab: blue = history, orange = the year being tracked (also the net-of-interest line
and the top-of-net-carry marker above), dark red titles like the River FOB sheet's charts.
"""
from __future__ import annotations

import html
import json
import statistics

import altair as alt
import pandas as pd

import return_to_carry as rtc

BLUE, ORANGE, DARK_ORANGE, TITLE_RED = "#4e79a7", "#f28e2b", "#9a3412", "#c00000"
AMBER_BG = "#fff4e5"
GREEN, RED = "#0a7f3f", "#c0392b"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _f(v, dec: int = 1, sign: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.{dec}f}" if sign else f"{v:.{dec}f}"


def _md(d) -> str:
    return "—" if d is None else f"{d.strftime('%b')} {d.day}"


def measure_name(measure: str) -> str:
    return "net of interest" if measure == "net" else "gross (before interest)"


def measure_short(measure: str) -> str:
    """For chart titles, which have to fit a phone."""
    return "net of interest" if measure == "net" else "gross"


_NARROW = "width < 480"


def completed(rows: list[dict]) -> list[dict]:
    """The crop years that ran their course — the history a new year is compared with."""
    return [r for r in rows if r["complete"] and r["best"] is not None]


def _avg(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def rank_of_latest(rows: list[dict]) -> tuple | None:
    """(rank, of) of the latest year's best return among all years that have one; None if there isn't one."""
    have = [r for r in rows if r["best"] is not None]
    if not rows or rows[-1]["best"] is None or len(have) < 2:
        return None
    return 1 + sum(1 for r in have if r["best"] > rows[-1]["best"]), len(have)


# ── the headline strip ───────────────────────────────────────────────────────────────────────────
def _card(label: str, value: str, sub: str, accent: str = "#32373c") -> str:
    return (f'<div style="flex:1 1 150px;min-width:140px;background:#fff;border:1px solid #e2e8f0;border-radius:10px;'
            f'padding:10px 14px"><div style="font-size:10px;font-weight:700;letter-spacing:.08em;color:#64748b;'
            f'text-transform:uppercase">{label}</div><div style="font-size:24px;font-weight:700;color:{accent};'
            f'line-height:1.25;font-variant-numeric:tabular-nums">{value}</div>'
            f'<div style="font-size:11px;color:#64748b;line-height:1.35">{sub}</div></div>')


def headline_html(rows: list[dict], measure: str = "net", spec: rtc.Spec = rtc.CORN) -> str:
    """Four numbers for the latest crop year with data: the best return so far, where it is now, the harvest basis
    it is measured from, and the futures carry. '' when there are no rows."""
    if not rows:
        return ""
    r = rows[-1]
    state = "complete" if r["complete"] else f"{r['weeks']} week{'s' if r['weeks'] != 1 else ''} in"
    rk = rank_of_latest(rows)
    avg_best = _avg([x["best"] for x in completed(rows[:-1])])
    best_sub = (f"{_md(r['best_date'])}" if r["best_date"] else "no weeks yet")
    if rk:
        best_sub += f" · #{rk[0]} of {rk[1]} years"
    last_sub = f"{_md(r['last_date'])}" if r["last_date"] else "—"
    if avg_best is not None and r["last"] is not None:
        last_sub += f" · a typical year's best is {avg_best:+.0f}¢"
    ln = rtc.LETTER_NAME
    n0 = r["b0_weeks"]
    if r.get("b0_own"):
        b0_sub = f"your own, vs {ln[spec.base]}" + (f" · calculated {_f(r['b0_calc'])}" if r.get("b0_calc") is not None else "")
    elif r["b0"] is None:
        b0_sub = "needs the first weeks of October"
    elif n0 >= 7:
        b0_sub = f"average of the first {n0} weekly bids, vs {ln[spec.base]}"
    elif n0 == 1:
        b0_sub = f"the first weekly bid so far, vs {ln[spec.base]}"
    else:
        b0_sub = f"average of the first {n0} weekly bids so far, vs {ln[spec.base]}"
    carry_sub = (f"{ln[spec.carry_from]} → {ln[spec.carry_to]}, rolled the last Wednesday before each month" if r["carry"] is not None
                 else "known once the May roll is in")
    gross_note = "" if measure == "net" else " (before interest)"
    head = (f'<div style="font-size:12px;font-weight:700;color:#32373c;margin:6px 0 8px">{r["label"]} crop year '
            f'<span style="font-weight:400;color:#64748b">({state}) — {measure_name(measure)}</span></div>')
    cards = (_card(f"Best return so far{gross_note}", _f(r["best"]) + "¢" if r["best"] is not None else "—", best_sub, DARK_ORANGE)
             + _card("Latest", _f(r["last"]) + "¢" if r["last"] is not None else "—", last_sub)
             + _card("Harvest basis", _f(r["b0"]) + "¢" if r["b0"] is not None else "—", b0_sub)
             + _card("Futures carry", _f(r["carry"]) + "¢" if r["carry"] is not None else "—", carry_sub))
    return head + f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:6px">{cards}</div>'


# ── the by-year table ─────────────────────────────────────────────────────────────────────────────
def _derived_pill(status: str) -> str:
    label = "DERIVED" if status == "derived" else "PART DERIVED"
    tip = ("built from the River FOB sheet's FOB values, not this location's own bids" if status == "derived"
           else "built partly from the River FOB sheet's FOB values: this location's own bids take over part-way through the year")
    return (f'<span title="{tip}" style="background:#fff4e5;color:#9a3412;border:1px solid #f28e2b;font-size:9px;font-weight:700;'
            f'letter-spacing:.05em;padding:1px 6px;border-radius:8px;margin-left:6px;white-space:nowrap">{label}</span>')


def _own_pill() -> str:
    return ('<span title="the harvest basis you entered, in place of the calculated average of the first weekly bids" style="background:#eef5fc;'
            'color:#1f4e79;border:1px solid #4e79a7;font-size:9px;font-weight:700;letter-spacing:.05em;padding:1px 6px;border-radius:8px;'
            'margin-left:6px;white-space:nowrap">OWN</span>')


def table_html(rows: list[dict], measure: str = "net", derived: dict | None = None) -> str:
    """A line per crop year, newest first. Harvest basis = the average of the first weekly bids; futures carry = the
    Dec->Jul roll spreads; summer basis = the best bid quoted off July; best return = the highest weekly return
    on the chosen measure (and its week); 'end' = the return at the last week to July. `derived` = {crop year: 'derived' | 'part'}
    (return_to_carry_data.derived_years): those years carry a DERIVED / PART DERIVED pill and a line under the table says what it means.
    A year measured from the user's own harvest basis (row['b0_own']) carries an OWN pill beside it."""
    if not rows:
        return ""
    th = ("padding:6px 10px;border-bottom:2px solid #cbd5e1;font-size:11px;color:#475569;text-transform:uppercase;"
          "letter-spacing:.03em;white-space:nowrap;text-align:right")
    heads = ["Crop year", "Harvest basis", "Futures carry", "Best summer basis", "Best return", "Best week", "Return at end"]
    head = "".join(f'<th style="{th}{";text-align:left" if i == 0 else ""}">{h}</th>' for i, h in enumerate(heads))
    bests = [r["best"] for r in rows if r["best"] is not None]
    top = max(bests) if bests else None
    td = "padding:5px 10px;border-bottom:1px solid #eef2f6;font-size:13px;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap"
    body = ""
    for r in reversed(rows):
        latest = r is rows[-1]
        bg = f";background:{AMBER_BG}" if latest else ""
        tag = ('<span style="background:#f28e2b;color:#fff;font-size:9px;font-weight:700;letter-spacing:.05em;padding:2px 6px;'
               'border-radius:8px;margin-left:6px">' + ("LATEST" if r["complete"] else "IN PROGRESS") + "</span>") if latest else ""
        isbest = r["best"] is not None and top is not None and abs(r["best"] - top) < 1e-9
        bcol = f";color:{GREEN};font-weight:700" if isbest else ";font-weight:600"
        end_col = ("" if r["last"] is None else (f";color:{GREEN}" if r["last"] > 0 else f";color:{RED}"))
        dstat = (derived or {}).get(r["crop_year"])
        body += (f'<tr><td style="{td};text-align:left{bg}">{r["label"]}{tag}{_derived_pill(dstat) if dstat else ""}</td>'
                 f'<td style="{td}{bg}">{_f(r["b0"])}{_own_pill() if r.get("b0_own") else ""}</td><td style="{td}{bg}">{_f(r["carry"])}</td>'
                 f'<td style="{td}{bg}">{_f(r["summer"], 0)}</td><td style="{td}{bcol}{bg}">{_f(r["best"])}</td>'
                 f'<td style="{td};color:#64748b{bg}">{_md(r["best_date"])}</td><td style="{td}{end_col}{bg}">{_f(r["last"])}</td></tr>')
    done = completed(rows)
    foot = ""
    if len(done) >= 3:
        def stat_row(name, fn):
            ks = ("b0", "carry", "summer", "best")
            cells = [fn([r[k] for r in done if r[k] is not None]) for k in ks] + [""] + [fn([r["last"] for r in done if r["last"] is not None])]
            edge = "border-top:2px solid #cbd5e1"
            return (f'<tr><td style="{td};text-align:left;font-weight:700;color:#475569;{edge}">{name}</td>'
                    + "".join(f'<td style="{td};font-weight:600;color:#475569;{edge}">{c}</td>' for c in cells) + "</tr>")
        mean = lambda xs: _f(sum(xs) / len(xs)) if xs else "—"
        med = lambda xs: _f(statistics.median(xs)) if xs else "—"
        foot = stat_row(f"Average of {len(done)} completed years", mean) + stat_row("Median", med)
    legend = ""
    if derived:
        legend = ('<div style="font-size:11px;color:#9a3412;margin-top:4px">DERIVED / PART DERIVED = built from the River FOB sheet\'s FOB '
                  'values (less the gap in the note above), not this location\'s own basis history.</div>')
    if any(r.get("b0_own") for r in rows):
        legend += ('<div style="font-size:11px;color:#1f4e79;margin-top:4px">OWN = measured from the harvest basis you entered, in place of the '
                   'calculated average of the first weekly bids.</div>')
    return (f'<div style="overflow-x:auto"><table style="border-collapse:collapse;min-width:640px"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}{foot}</tbody></table></div>{legend}')


# ── derived history: the banner that says what it is ────────────────────────────────────────────
def derived_banner_html(location: str, dv: dict) -> str:
    """The amber note above a history that is partly ESTIMATED from the River FOB sheet (river_derived.Derivation.as_dict()): which reach, the
    gap, how it was measured, and that it is not the location's own basis history."""
    name = html.escape(location or "This location")
    first_year = rtc.crop_label(dv["own_from"].year if dv["own_from"].month >= 10 else dv["own_from"].year - 1)
    return ('<div style="background:#fff7ed;border:1px solid #fed7aa;border-left:5px solid #f28e2b;border-radius:8px;padding:10px 14px;'
            'margin:8px 0 10px;font-size:13px;line-height:1.5;color:#7c2d12">'
            f'<b>Derived history — not {name}\'s own basis.</b> {name} began posting bids on {dv["own_from"]:%b %d, %Y}. Before that, the weekly '
            f'history below is the River FOB sheet\'s <b>{html.escape(dv["fob_location"])}</b> FOB barge basis less <b>{dv["gap"]:.1f}¢</b> — the median '
            f'gap between the two on the {dv["pairs"]:,} same-day bid pairs both posted since then (the middle half ran {dv["q1"]:.1f} to {dv["q3"]:.1f}¢). '
            'It is derived through the historical FOB river values, not actual basis history, and the gap is only measured over the last few months, '
            f'so read the earlier returns as an estimate of what storing here would have paid. Years tagged DERIVED are built entirely from it; '
            f'{first_year} is a mixture ({dv.get("mixed_weeks", dv["derived_weeks"])} derived weeks, then {name}\'s own bids).</div>')


# ── the user's own harvest basis: the note that says where it is used ────────────────────────────
def own_basis_note_html(value: float, calc: float | None, label: str, spec: rtc.Spec = rtc.CORN, every_year: bool = False,
                        in_history: bool = True) -> str:
    """The blue note under the Harvest basis switch when the user's OWN number is in use: which crop year(s) it measures, what it replaces.
    value / calc = their number and what the calculated method gives (None = not known yet); label = the tracked crop year ('2026-27');
    every_year = the what-if that applies it to every crop year; in_history = the tracked year has weekly bids to draw (else the shipment table only)."""
    base = rtc.LETTER_NAME[spec.base]
    if every_year:
        body = (f"<b>What-if:</b> every crop year below is measured from your harvest basis of <b>{value:+.1f}¢</b> vs {base} instead of its own "
                "calculated one (the average of its first weekly bids). Each year's returns move by the difference, the same for all its weeks, so the best "
                "week stays where it was — this is what storing would have paid had the grain always been bought at that basis, not what the analyst's "
                "workbooks computed.")
    else:
        where = ("the shipment table, the headline numbers, the orange line and bar, and the latest row of the table below" if in_history
                 else "the shipment table (that year has no weekly bids to draw yet)")
        calc_txt = "" if calc is None else f", in place of the calculated {calc:+.1f}¢"
        body = (f"<b>Your own harvest basis, {value:+.1f}¢ vs {base}</b>{calc_txt}, measures {label}: {where}. Earlier years keep their calculated "
                "harvest basis, so the comparison with history is still the analyst's method.")
    return ('<div style="font-size:12px;color:#1f4e79;background:#eef5fc;border:1px solid #c5daf1;border-left:4px solid #4e79a7;border-radius:8px;'
            f'padding:8px 12px;margin:6px 0 10px;line-height:1.5">{body}</div>')


# ── the words that explain the method (per commodity) ────────────────────────────────────────────
def method_caption(spec: rtc.Spec = rtc.CORN) -> str:
    """The one-paragraph caption under the tracker."""
    chain = {"corn": "Dec→Mar→May→Jul→Sep", "soy": "Nov→Jan→Mar→May→Jul→Aug→Nov"}[spec.key]
    end = {"corn": "July 31", "soy": "September 30"}[spec.key]
    return (f"Same method as the Research Analyst's {'Return to Carry' if spec.key == 'corn' else 'bean carry'} workbooks: buy at the "
            "harvest basis, hedge, carry. Weekly return = that week's bid − the harvest basis + the futures carry banked rolling the hedge "
            f"{chain} − interest. Gross = the same without the interest. Weeks run to {end}.")


def how_it_works(spec: rtc.Spec = rtc.CORN) -> str:
    """The markdown bullets of the 'How the return is calculated' expander."""
    soy = spec.key == "soy"
    base = rtc.LETTER_NAME[spec.base]
    ship_to = "Aug" if soy else "Jul"
    shipment = ("- **Shipment by month** (the report's front page" + (", applied to soybeans" if soy else "") + f") — bought at the harvest basis on Oct 20; for each "
                f"shipment date (the 20th of Nov-{ship_to}) the **basis cost** is the break-even: harvest basis − the futures carry "
                f"banked rolling to that month's contract + interest on ({base} futures + basis) at the rate × days ÷ 360. "
                "**Current basis** is the latest posted bid for that month (a package such as JFM or AMJJ counts for each "
                "month it covers; a bid quoted off another futures month is moved to the column's by that day's spread); "
                "**return** = bid − basis cost; the best bid and best return are tracked from Oct 20, each return against "
                "that day's break-even. Before the weekly harvest bids start, the harvest basis is estimated from the "
                + ("posted October and November bids (weighted 4 : 3, like the weeks the sheets average)." if soy else "posted FH Oct / LH Oct / FH Nov bids."))
    if soy:
        harvest = ("- **Harvest basis** — the average of the first 7 weekly bids from the first Wednesday of October (\"Oct / F-H Nov\"), "
                   "all expressed against Jan: October's bids are quoted off Nov and are moved to Jan by the Nov−Jan spread measured the last "
                   "Wednesday of October; the first November bids are quoted off Jan. The sheets' own exceptions are kept (2015-16 to 2017-18 "
                   "left the fifth week out).")
        weekly = ("- **Weekly bid** — the location's spot bid; when there is none, the nearest forward period. It is quoted off Nov in Oct, "
                  "Jan Nov-Dec, Mar Jan-Feb, May Mar-Apr, Jul May-Jun, Aug in Jul and the next crop's Nov Aug-Sep; a bid quoted off another "
                  "contract is moved to that week's by the same day's spread, as the sheets' columns do.")
        carry = ("- **Futures carry** — each roll spread (Jan−Nov, Mar−Jan, May−Mar, Jul−May, Aug−Jul, next Nov−Aug) is measured the last "
                 "Wednesday before the month the next contract takes over and counted from the week the bid moves to it (the sheets' own "
                 "exceptions: the Nov/Jan spread on the first Wednesday of November in 2010-11, the Jan/Mar one on Dec 30 in 2019-20, Mar/May on "
                 "Mar 1 in 2016-17).")
        start = "from the fifth weekly bid (about Nov 1; the third in 2005-06 to 2008-09)"
        data = ("- **Data** — weekly bids from the archive (Wednesdays from Oct 2004; the soybean tracker starts at 2005-06, the first year "
                "of the analyst's bean sheets); settlements from the futures archive, and for 2005-07 from the workbooks. Reproduces her "
                "Decatur, Des Moines, Hennepin and St. Louis workbooks to the cent in most weeks (tests/test_return_to_carry_soy.py).")
    else:
        harvest = ("- **Harvest basis** — the average of the first 7 weekly bids from the first Wednesday of October "
                   "(\"Oct / F-H Nov\"), all quoted off Dec; a few years used a different window in the sheets, and those "
                   "are kept (2009-10 and 2019-20 started later, 2012-13 in September, 1998-2001, 2010 and 2015 used 6 weeks).")
        weekly = ("- **Weekly bid** — the corridor's Spot bid; since the 2026 rundowns stopped posting Spot, the nearest "
                  "forward period. It is quoted off Dec in Oct-Nov, Mar Dec-Feb, May Mar-Apr, Jul May-Jun, Sep Jul-Aug.")
        carry = ("- **Futures carry** — each roll spread (Mar−Dec, May−Mar, Jul−May, Sep−Jul) is measured the last "
                 "Wednesday before the expiring month and counted from the week the bid moves to the new contract "
                 "(the sheets' own exceptions: Apr 21 2021, Jul 1-2 in 2025-26).")
        start = "from the third weekly bid (about Oct 20)"
        data = ("- **Data** — weekly corridor bids from the archive (true Wednesdays from Oct 2004, so history starts "
                "2004-05); settlements from the futures archive, and for 2004-07 from the workbooks. Reproduces the "
                "yearly workbooks to the cent in most weeks (tests/test_return_to_carry.py).")
    harvest += (" The Harvest basis switch above can use your own number instead (the calculated average is the default): it measures the "
                "crop year being tracked, or, as a what-if, every crop year. In the weekly history the interest is charged on each week's cash price, "
                "not on the harvest basis, so a different harvest basis moves every week's return by the same amount; the shipment table's "
                "break-even does charge interest on it (futures + harvest basis).")
    interest = ("- **Interest** — the same rate as the rest of this tab: the effective fed funds rate on each date + 2.25% "
                "(the Cost of Carry sheet's), moved by whatever the rate box above was edited by. In the weekly history it is "
                f"charged as the sheets do — that week's rate ÷ 52 on the cash price (futures + basis), {start}; the shipment table "
                "charges rate × days ÷ 360 on the same price, as the carry calculations above do. The second choice is the bank prime "
                "rate the analyst's own sheets use, to tie out to their numbers.")
    return "\n".join([shipment, harvest, weekly, carry, interest, data])


# ── the report's page 1: shipment by month ───────────────────────────────────────────────────────
def _n(v, dec: int = 2, sign: bool = True) -> str:
    return "—" if v is None else (f"{v:+.{dec}f}" if sign else f"{v:.{dec}f}")


def _bid(v) -> str:
    """A bid as a trader writes it: 17, 10.25 (no trailing zeros)."""
    if v is None:
        return "—"
    s = f"{v:+.2f}".rstrip("0").rstrip(".")
    return s


def _short(d) -> str:
    return "" if d is None else f"{d.strftime('%b')} {d.day}"


def _ship_notes(tbl, est) -> str:
    """The one line under the cards that says how firm the harvest basis is."""
    if tbl.b0 is not None and getattr(tbl, "b0_own", False):
        return ("<b>Your own harvest basis</b> is in use: the break-even, the returns and the interest (charged on the futures plus the harvest basis) "
                "are all measured from it.")
    if tbl.b0 is None:
        return ("The harvest basis is not known yet — the first weekly bid posts the first Wednesday of October, and "
                "until a harvest-period bid is posted there is nothing to measure the break-even from.")
    if tbl.b0_est:
        src = ""
        if est:
            src = f" ({html.escape(', '.join(est[2]))} posted {_short(est[1])})"
        if tbl.spec.key == "soy":                        # October's bids are quoted off Nov: they need that spread, measured Oct 28 or so
            return ("<b>Estimate.</b> The harvest basis is the average of the posted harvest-period bids"
                    f"{src} until the weekly average can be built — it moves October's bids from Nov to Jan with the spread measured "
                    "the last Wednesday of October. It becomes the average of the first 7 weekly bids as they post.")
        return ("<b>Estimate.</b> The weekly harvest bids have not started, so the harvest basis is the average of the "
                f"posted harvest-period bids{src}. It becomes the average of the first 7 weekly bids as they post.")
    if tbl.b0_weeks and tbl.b0_weeks < 7:
        first = "the first weekly bid" if tbl.b0_weeks == 1 else f"the average of the first {tbl.b0_weeks} weekly bids"
        return (f"The harvest basis is {first} so far; the report uses the average of the first 7, so it can still move.")
    return ""


def _cost_tip(tbl, col) -> str:
    """Hover text: how the break-even is built."""
    if tbl.b0 is None or col.cost is None or tbl.levels is None:
        return ""
    lb = tbl.levels.levels.get(tbl.spec.base)                 # the base contract (corn Dec, soybeans Jan)
    carry = 0.0 if lb is None or col.level is None else col.level - lb
    parts = [f"harvest basis {tbl.b0:+.2f}"]
    if abs(carry) > 1e-9:
        parts.append(f"minus {carry:.2f} futures carry banked rolling to {rtc.LETTER_NAME[col.letter]}")
    if tbl.measure != "gross" and tbl.rate is not None and lb is not None:
        days = (col.ship - tbl.purchase).days
        parts.append(f"plus interest {(lb + tbl.b0) * tbl.rate / 100 * days / 360:.2f} ({tbl.rate:.2f}% x {days} days on {lb + tbl.b0:.2f})")
    return "; ".join(parts) + f" = {col.cost:+.2f}"


def shipment_html(tbl, est=None, rate_note: str = "") -> str:
    """The report's front page: for each shipment month Nov..Jul (soybeans Nov..Aug), the break-even basis, today's bid and what
    it returns, and the best bid and return since the purchase. `tbl` is return_to_carry.ShipTable (net or gross); `rate_note`
    says where the interest rate comes from ('fed funds + 2.25%, as in the rest of this tab', 'bank prime')."""
    gross = tbl.measure == "gross"
    best_now = rtc.best_column(tbl, "ret")
    best_ytd = rtc.best_column(tbl, "best_ret")
    head = (f'<div style="font-size:12px;font-weight:700;color:#32373c;margin:6px 0 8px">{tbl.label} shipment by month '
            f'<span style="font-weight:400;color:#64748b">— bought {_short(tbl.purchase)} at the harvest basis, {measure_name(tbl.measure)}</span></div>')
    lv = tbl.levels
    spec = tbl.spec
    ln = rtc.LETTER_NAME
    sp = []
    if lv is not None:
        for key in spec.carry_pairs:
            name = f"{ln[key[0]]}→{ln[key[1]]}"
            v = lv.spreads.get(key)
            sp.append(f"{name} {v[0]:+.2f}{'*' if v[2] else ''}" if v else f"{name} —")
    carry_total = None
    if lv is not None and all(lv.spreads.get(k) for k in spec.carry_pairs):
        carry_total = sum(lv.spreads[k][0] for k in spec.carry_pairs)
    if tbl.b0 is not None and getattr(tbl, "b0_own", False):
        b0_sub = f"your own, vs {ln[spec.base]}" + (f" · calculated {_n(tbl.b0_calc, 1)}" if tbl.b0_calc is not None else "")
    elif tbl.b0 is None:
        b0_sub = "waiting for the first harvest bids"
    elif tbl.b0_est:
        b0_sub = "estimate — average of the posted harvest-period bids"
    else:
        n0 = tbl.b0_weeks
        b0_sub = (f"average of the first {n0} weekly bids, vs {ln[spec.base]}" if n0 >= 7 else
                  f"the first weekly bid so far, vs {ln[spec.base]}" if n0 == 1 else
                  f"average of the first {n0} weekly bids so far, vs {ln[spec.base]}")
    rate_sub = rate_note if tbl.rate is not None else "no rate"
    if gross:
        rate_val, rate_sub = "—", "not charged in the gross view"
    else:
        rate_val = f"{tbl.rate:.2f}%" if tbl.rate is not None else "—"
    cards = (_card("Harvest basis", _n(tbl.b0, 1) + "¢" if tbl.b0 is not None else "—", b0_sub)
             + _card(f"{ln[spec.base]} futures", _n(tbl.f_base, 2, False) if tbl.f_base is not None else "—", "the price the interest is charged on, with the basis")
             + _card("Interest", rate_val, rate_sub)
             + _card("Futures carry", _n(carry_total, 1) + "¢" if carry_total is not None else "—", " · ".join(sp) if sp else "—"))
    cards = f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px">{cards}</div>'
    note = _ship_notes(tbl, est)
    note = f'<div style="font-size:11px;color:#9a3412;background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:6px 10px;margin-bottom:8px">{note}</div>' if note else ""

    callout = ""
    if best_now is not None:
        c = best_now
        callout = (f'<div style="background:{AMBER_BG};border:1px solid #fcd9a8;border-left:4px solid {ORANGE};border-radius:8px;padding:8px 12px;'
                   f'margin-bottom:8px;font-size:13px;color:#1f2937"><b>Best on today\'s bids: ship {MONTHS[c.month - 1]} {c.ship.day}</b> — '
                   f'<b style="color:{DARK_ORANGE}">{c.ret:+.1f}¢</b> {"gross" if gross else "net"} '
                   f'<span style="color:#64748b">(bid {_bid(c.bid)} against a break-even of {c.cost:+.1f}'
                   + (f'; best so far this year {_short(best_ytd.best_ret_date)}: {best_ytd.best_ret:+.1f}¢ for {MONTHS[best_ytd.month - 1]}' if best_ytd is not None else "")
                   + ")</span></div>")
    elif tbl.b0 is not None and all(c.bid is None for c in tbl.cols):
        callout = ('<div style="font-size:12px;color:#64748b;margin-bottom:8px">No forward bids were posted for this crop year in the '
                   f'10 days to {_short(tbl.asof)} — the break-even row below is what a bid has to reach.</div>')

    th = ("padding:6px 8px;border-bottom:2px solid #cbd5e1;font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:.03em;"
          "white-space:nowrap;text-align:right;font-weight:700")
    lab = ("padding:5px 10px 5px 0;font-size:12px;color:#475569;font-weight:600;text-align:left;white-space:nowrap;position:sticky;left:0;"
           "background:#fff;z-index:1")
    td = "padding:5px 8px;font-size:13px;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap"
    sep = "border-top:1px solid #e2e8f0"

    def css(*parts) -> str:
        return ";".join(p.strip(";") for p in parts if p and p.strip(";"))

    def hl(col, now=False, ytd=False) -> str:
        if now and best_now is col or ytd and best_ytd is col:
            return f"background:{AMBER_BG}"
        return ""

    def row(label, cells, extra_label="", border=False, now=False, ytd=False):
        b = sep if border else ""
        out = f'<tr><td style="{css(lab, b)}">{label}{extra_label}</td>'
        for col, (txt, style, tip) in zip(tbl.cols, cells):
            t = f' title="{html.escape(tip)}"' if tip else ""
            out += f'<td style="{css(td, b, hl(col, now, ytd), style)}"{t}>{txt}</td>'
        return out + "</tr>"

    heads = "".join(
        f'<th style="{css(th, hl(c, now=True))}">{MONTHS[c.month - 1]} {c.ship.day}'
        + ('<div style="margin-top:2px"><span style="background:#f28e2b;color:#fff;font-size:8px;font-weight:700;letter-spacing:.05em;'
           'padding:1px 5px;border-radius:7px">BEST NOW</span></div>' if best_now is c else "") + "</th>" for c in tbl.cols)
    corner = f'<th style="{th};text-align:left;position:sticky;left:0;background:#fff;z-index:1">Shipment date</th>'

    r_month = row("Futures month", [(rtc.LETTER_NAME[c.letter], "color:#64748b", "") for c in tbl.cols])
    r_level = row("Futures (current)", [(_n(c.level, 2, False), "color:#64748b", "") for c in tbl.cols])
    r_cost = row("Basis cost" + (" (carry only)" if gross else " (break-even)"),
                 [(_n(c.cost, 2), "font-weight:700", _cost_tip(tbl, c)) for c in tbl.cols], border=True, now=True)

    def bid_cell(c):
        if c.bid is None:
            return ("—", "color:#94a3b8", "")
        tip = f"Posted {_short(c.bid_date)} as '{c.bid_label}'"
        mark = ""
        if c.bid_posted is not None:
            mark += "†"
            tip += f" at {_bid(c.bid_posted)}; moved to {rtc.LETTER_NAME[c.letter]} terms by that day's spread ({c.bid - c.bid_posted:+.2f})"
        if c.bid_label and parse_bundle(c.bid_label):
            mark += "‡"
            tip += " — a package that covers this month"
        return (f"{_bid(c.bid)}{mark}", "font-weight:600", tip)

    r_bid = row("Current basis", [bid_cell(c) for c in tbl.cols], border=True, now=True)

    def ret_cell(c):
        if c.ret is None:
            return ("—", "color:#94a3b8", "")
        return (_n(c.ret, 2), f"font-weight:700;color:{GREEN if c.ret > 0 else RED}", "")

    r_ret = row("Return today", [ret_cell(c) for c in tbl.cols], now=True)

    def best_bid_cell(c):
        if c.best_bid is None:
            return ("—", "color:#94a3b8", "")
        return (f'{_bid(c.best_bid)}<div style="font-size:10px;font-weight:400;color:#64748b">{_short(c.best_bid_date)}</div>', "font-weight:600", "")

    def best_ret_cell(c):
        if c.best_ret is None:
            return ("—", "color:#94a3b8", "")
        win = "font-weight:800;" if best_ytd is c else "font-weight:600;"
        return (f'{_n(c.best_ret, 2)}<div style="font-size:10px;font-weight:400;color:#64748b">{_short(c.best_ret_date)}</div>',
                f"{win}color:{GREEN if c.best_ret > 0 else RED}", "")

    since = f'<div style="font-size:10px;font-weight:400;color:#94a3b8">since {_short(tbl.purchase)}</div>'
    r_bbid = row("Best basis YTD", [best_bid_cell(c) for c in tbl.cols], extra_label=since, border=True, ytd=True)
    r_bret = row("Best return YTD", [best_ret_cell(c) for c in tbl.cols], extra_label=since, ytd=True)
    table = (f'<div style="overflow-x:auto"><table style="border-collapse:collapse;min-width:{70 + 76 * len(tbl.cols)}px;width:100%"><thead><tr>{corner}{heads}</tr></thead>'
             f'<tbody>{r_month}{r_level}{r_cost}{r_bid}{r_ret}{r_bbid}{r_bret}</tbody></table></div>')
    foot = (f'<div style="font-size:11px;color:#64748b;line-height:1.5;margin:6px 0 20px">'
            f'* Futures spreads measured the last Wednesday before the expiring month are held for the rest of the crop year; the others are today\'s. '
            f'† A bid quoted off a different futures month than the column is moved to it by that day\'s spread. '
            f'‡ A package (JFM, AMJJ …) counts for each month it covers. '
            f'Best return = the bid less that day\'s basis cost, from the purchase on {_short(tbl.purchase)}.</div>')
    return head + cards + note + callout + table + foot


def parse_bundle(label) -> bool:
    """True when a posted period is a package that covers several months."""
    from return_to_carry_data import parse_label
    info = parse_label(label)
    return bool(info) and info["kind"] == "bundle"


# ── chart 1: the best return of each crop year ────────────────────────────────────────────────────
SOURCE_RANGE = {"own bids": BLUE, "partly derived": "#7fa1cc", "derived": "#bcd0e6"}


def best_bar_chart(rows: list[dict], measure: str = "net", height: int = 280, derived: dict | None = None) -> alt.Chart | None:
    """One bar per crop year at its best weekly return; the latest year orange, a dashed line at the average of the
    completed years. `derived` = {crop year: 'derived' | 'part'}: those bars are lighter and a legend says why. None when no year has a return."""
    have = [r for r in rows if r["best"] is not None]
    if not have:
        return None
    src = {"derived": "derived", "part": "partly derived"}
    df = pd.DataFrame([{
        "Crop year": r["label"], "Best": float(r["best"]), "latest": r is rows[-1],
        "When": _md(r["best_date"]), "Harvest basis": r["b0"], "Futures carry": r["carry"],
        "Source": src.get((derived or {}).get(r["crop_year"]), "own bids")} for r in have])
    order = list(df["Crop year"])
    base = alt.Chart(df).encode(x=alt.X("Crop year:N", sort=order, title=None,
                                       axis=alt.Axis(labelAngle=-90, labelFontSize=10, labelColor="#1f4e79", labelFontWeight="bold")))
    bars = base.mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        y=alt.Y("Best:Q", title="¢/bu", axis=alt.Axis(format=".0f", titleColor="#64748b", labelColor="#64748b")),
        color=(alt.condition("datum.latest", alt.value(ORANGE),
                             alt.Color("Source:N", scale=alt.Scale(domain=list(SOURCE_RANGE), range=list(SOURCE_RANGE.values())),
                                       legend=alt.Legend(title=None, orient="bottom", labelFontSize=11, symbolType="square")))
               if derived else alt.condition("datum.latest", alt.value(ORANGE), alt.value(BLUE))),
        tooltip=[alt.Tooltip("Crop year:N"), alt.Tooltip("Source:N"), alt.Tooltip("Best:Q", format="+.1f", title=f"Best return ({measure_name(measure)}) ¢"),
                 alt.Tooltip("When:N", title="Best week"), alt.Tooltip("Harvest basis:Q", format="+.1f"),
                 alt.Tooltip("Futures carry:Q", format="+.1f")])
    layers = [bars]
    done = [r["best"] for r in completed(rows)]
    if len(done) >= 3:
        avg = sum(done) / len(done)
        rule = alt.Chart(pd.DataFrame({"y": [avg]})).mark_rule(strokeDash=[5, 4], color="#64748b", opacity=0.9).encode(y="y:Q")
        lab = alt.Chart(pd.DataFrame({"y": [avg], "t": [f"average {avg:+.0f}¢"], "x": [order[0]]})).mark_text(
            align="left", baseline="bottom", dy=-3, dx=2, fontSize=10, color="#64748b").encode(x=alt.X("x:N", sort=order), y="y:Q", text="t:N")
        layers += [rule, lab]
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#94a3b8", strokeWidth=1).encode(y="y:Q")
    layers.append(zero)
    return (alt.layer(*layers).properties(height=height, background="transparent", padding={"left": 6, "right": 14, "top": 4, "bottom": 4},
                                          title=alt.TitleParams(f"Best return by crop year — {measure_short(measure)}", color=TITLE_RED,
                                                                fontSize=alt.expr(f"{_NARROW} ? 12 : 15"), fontWeight="bold", anchor="middle"))
            .configure_view(strokeWidth=0, fill=None).configure_axis(grid=True, gridColor="#e6e6e6", domainColor="#cccccc"))


# ── chart 2: the path through the crop year ───────────────────────────────────────────────────────
def _month_ticks(results: list, weeks: int = 44, horizon: tuple | None = None) -> tuple[list[int], list[str]]:
    """Week indexes (and month names) where each month first appears on the crop-year grid, Oct through Jul (soybeans: Sep).
    `horizon` = (month, day) of the following year where the season ends: a week past it gets no tick (a year that starts
    Oct 7 would otherwise label its week 43 'Aug', soybeans' week 52 'Oct')."""
    from datetime import date, timedelta
    from return_to_carry import first_wednesday
    ref = next((cy for cy in reversed(results) if cy.weeks), None)
    if ref is None:
        return [], []
    start = first_wednesday(ref.crop_year)
    end = date(ref.crop_year + 1, *horizon) if horizon else None
    idx, names, seen = [], [], set()
    for k in range(0, weeks):
        d = start + timedelta(days=7 * k)
        if end is not None and d > end:
            break
        if (d.year, d.month) not in seen:
            seen.add((d.year, d.month))
            idx.append(k)
            names.append(MONTHS[d.month - 1])
    return idx, names


def seasonal_chart(results: list, measure: str = "net", height: int = 340, logo_uri: str | None = None,
                   spec: rtc.Spec = rtc.CORN) -> alt.LayerChart | None:
    """Return by week of the crop year. Band = the middle 80% (p10-p90) and 50% (p25-p75) of the completed years,
    dashed line = their median; the latest year is orange, the one before blue. A dot marks the latest year's best."""
    from return_to_carry_data import seasonal_points
    pts = seasonal_points(results, measure)
    if not pts:
        return None
    df = pd.DataFrame(pts)
    latest = results[-1].label
    prev = results[-2].label if len(results) >= 2 else None
    hist_labels = [cy.label for cy in results if cy.complete]
    hist = df[df["crop"].isin(hist_labels)]
    layers = []
    full = 40 if spec.key == "corn" else 51                  # the season's last week (Jul 31 for corn, Sep 30 for soybeans)
    ticks, names = _month_ticks(results, full + 4, spec.horizon)
    xmax = int(df["week"].max())
    x_enc = alt.X("week:Q", title=None, scale=alt.Scale(domain=[0, max(xmax, full) + 0.5]),
                  axis=alt.Axis(values=ticks, labelExpr=f"{json.dumps(names)}[indexof({json.dumps(ticks)}, datum.value)]",
                                labelColor="#1f4e79", labelFontWeight="bold", labelAngle=0, grid=False, ticks=False))
    y_title = "¢/bu"
    if logo_uri:
        layers.append(alt.Chart(pd.DataFrame({"x": [max(xmax, full) / 2], "url": [logo_uri]}))
                      .mark_image(width=int(height * 0.5), height=int(height * 0.5), opacity=0.10, align="center", baseline="middle")
                      .encode(x=alt.X("x:Q"), y=alt.value(alt.expr("height / 2")), url="url:N"))
    if hist["crop"].nunique() >= 5:
        g = hist.groupby("week")["value"]
        cnt = g.count()
        bands = pd.DataFrame({"week": cnt.index, "n": cnt.values,
                              "p10": g.quantile(0.10).values, "p25": g.quantile(0.25).values, "p50": g.quantile(0.5).values,
                              "p75": g.quantile(0.75).values, "p90": g.quantile(0.90).values})
        bands = bands[bands["n"] >= 5]
        if len(bands):
            layers.append(alt.Chart(bands).mark_area(opacity=0.14, color=BLUE).encode(x=x_enc, y=alt.Y("p10:Q", title=y_title), y2="p90:Q"))
            layers.append(alt.Chart(bands).mark_area(opacity=0.24, color=BLUE).encode(x=x_enc, y="p25:Q", y2="p75:Q"))
            layers.append(alt.Chart(bands).mark_line(color="#64748b", strokeDash=[5, 4], strokeWidth=2).encode(
                x=x_enc, y="p50:Q", tooltip=[alt.Tooltip("p50:Q", format="+.1f", title="Median ¢"), alt.Tooltip("n:Q", title="years")]))
    series = [(latest, ORANGE, 3.5)] + ([(prev, BLUE, 2)] if prev else [])
    ldf = df[df["crop"].isin([s[0] for s in series])].copy()
    ldf["Date"] = ldf["date"].map(lambda d: d.isoformat())
    ldf = ldf.drop(columns=["date"])                       # Altair cannot serialise date objects
    color = alt.Color("crop:N", title=None, sort=[s[0] for s in series], scale=alt.Scale(domain=[s[0] for s in series], range=[s[1] for s in series]),
                      legend=alt.Legend(orient="bottom", direction="horizontal"))
    layers.append(alt.Chart(ldf).mark_line(point=alt.OverlayMarkDef(size=30)).encode(
        x=x_enc, y=alt.Y("value:Q", title=y_title, axis=alt.Axis(format=".0f", titleColor="#64748b", labelColor="#64748b")),
        color=color, size=alt.condition(f"datum.crop === '{latest}'", alt.value(3.5), alt.value(2)),
        tooltip=[alt.Tooltip("crop:N", title="Crop year"), alt.Tooltip("Date:N"), alt.Tooltip("value:Q", format="+.1f", title="Return ¢/bu")]))
    best = results[-1].best.get(measure)
    if best is not None:
        v = best.net if measure == "net" else best.gross
        pk = pd.DataFrame([{"week": best.idx, "value": float(v), "label": f"Best {v:+.1f}¢ · {_md(best.date)}"}])
        layers.append(alt.Chart(pk).mark_point(shape="circle", filled=True, size=220, color=ORANGE, opacity=1, stroke=DARK_ORANGE, strokeWidth=2.5)
                      .encode(x="week:Q", y="value:Q"))
        layers.append(alt.Chart(pk).mark_text(baseline="bottom", dy=-14, fontSize=12, fontWeight="bold", color="white", stroke="white",
                                              strokeWidth=5, strokeJoin="round", align="center").encode(x="week:Q", y="value:Q", text="label:N"))
        layers.append(alt.Chart(pk).mark_text(baseline="bottom", dy=-14, fontSize=12, fontWeight="bold", color=DARK_ORANGE, align="center")
                      .encode(x="week:Q", y="value:Q", text="label:N"))
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#94a3b8", strokeWidth=1).encode(y="y:Q")
    layers.append(zero)
    n_hist = hist["crop"].nunique()
    tkw = dict(color=TITLE_RED, fontSize=alt.expr(f"{_NARROW} ? 12 : 15"), fontWeight="bold", anchor="middle")
    if n_hist >= 5:
        tkw.update(subtitle=[f"shaded = the middle 50% and 80% of {n_hist} completed years", "dashed = their median"],
                   subtitleColor="#64748b", subtitleFontSize=alt.expr(f"{_NARROW} ? 10 : 11"))
    return (alt.layer(*layers).resolve_scale(y="shared")
            .properties(height=height, background="transparent", padding={"left": 6, "right": 24, "top": 6, "bottom": 6},
                        title=alt.TitleParams(f"Return through the crop year — {measure_short(measure)}", **tkw))
            .configure_view(strokeWidth=0, fill=None).configure_axis(grid=True, gridColor="#e6e6e6", domainColor="#cccccc")
            .configure_legend(labelColor="#333", labelFontWeight="bold", padding=2))
