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

import json
import statistics

import altair as alt
import pandas as pd

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


def headline_html(rows: list[dict], measure: str = "net") -> str:
    """Four numbers for the latest crop year with data: the best return so far, where it is now, the harvest basis
    it is measured from, and the futures carry. '' when there are no rows."""
    if not rows:
        return ""
    r = rows[-1]
    state = "complete" if r["complete"] else f"{r['weeks']} weeks in"
    rk = rank_of_latest(rows)
    avg_best = _avg([x["best"] for x in completed(rows[:-1])])
    best_sub = (f"{_md(r['best_date'])}" if r["best_date"] else "no weeks yet")
    if rk:
        best_sub += f" · #{rk[0]} of {rk[1]} years"
    last_sub = f"{_md(r['last_date'])}" if r["last_date"] else "—"
    if avg_best is not None and r["last"] is not None:
        last_sub += f" · a typical year's best is {avg_best:+.0f}¢"
    b0_sub = (f"average of the first {r['b0_weeks']} weekly bids, vs Dec" if r["b0"] is not None else "needs the first weeks of October")
    carry_sub = "Dec → Jul, rolled the last Wednesday before each month" if r["carry"] is not None else "known once the May roll is in"
    gross_note = "" if measure == "net" else " (before interest)"
    head = (f'<div style="font-size:12px;font-weight:700;color:#32373c;margin:6px 0 8px">{r["label"]} crop year '
            f'<span style="font-weight:400;color:#64748b">({state}) — {measure_name(measure)}</span></div>')
    cards = (_card(f"Best return so far{gross_note}", _f(r["best"]) + "¢" if r["best"] is not None else "—", best_sub, DARK_ORANGE)
             + _card("Latest", _f(r["last"]) + "¢" if r["last"] is not None else "—", last_sub)
             + _card("Harvest basis", _f(r["b0"]) + "¢" if r["b0"] is not None else "—", b0_sub)
             + _card("Futures carry", _f(r["carry"]) + "¢" if r["carry"] is not None else "—", carry_sub))
    return head + f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:6px">{cards}</div>'


# ── the by-year table ─────────────────────────────────────────────────────────────────────────────
def table_html(rows: list[dict], measure: str = "net") -> str:
    """A line per crop year, newest first. Harvest basis = the average of the first weekly bids; futures carry = the
    Dec->Jul roll spreads; summer basis = the best bid quoted off July; best return = the highest weekly return
    on the chosen measure (and its week); 'end' = the return at the last week to July."""
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
        body += (f'<tr><td style="{td};text-align:left{bg}">{r["label"]}{tag}</td>'
                 f'<td style="{td}{bg}">{_f(r["b0"])}</td><td style="{td}{bg}">{_f(r["carry"])}</td>'
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
    return (f'<div style="overflow-x:auto"><table style="border-collapse:collapse;min-width:640px"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}{foot}</tbody></table></div>')


# ── chart 1: the best return of each crop year ────────────────────────────────────────────────────
def best_bar_chart(rows: list[dict], measure: str = "net", height: int = 280) -> alt.Chart | None:
    """One bar per crop year at its best weekly return; the latest year orange, a dashed line at the average of the
    completed years. None when no year has a return."""
    have = [r for r in rows if r["best"] is not None]
    if not have:
        return None
    df = pd.DataFrame([{
        "Crop year": r["label"], "Best": float(r["best"]), "latest": r is rows[-1],
        "When": _md(r["best_date"]), "Harvest basis": r["b0"], "Futures carry": r["carry"]} for r in have])
    order = list(df["Crop year"])
    base = alt.Chart(df).encode(x=alt.X("Crop year:N", sort=order, title=None,
                                       axis=alt.Axis(labelAngle=-90, labelFontSize=10, labelColor="#1f4e79", labelFontWeight="bold")))
    bars = base.mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        y=alt.Y("Best:Q", title="¢/bu", axis=alt.Axis(format=".0f", titleColor="#64748b", labelColor="#64748b")),
        color=alt.condition("datum.latest", alt.value(ORANGE), alt.value(BLUE)),
        tooltip=[alt.Tooltip("Crop year:N"), alt.Tooltip("Best:Q", format="+.1f", title=f"Best return ({measure_name(measure)}) ¢"),
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
def _month_ticks(results: list) -> tuple[list[int], list[str]]:
    """Week indexes (and month names) where each month first appears on the crop-year grid, Oct through Jul."""
    from datetime import timedelta
    from return_to_carry import first_wednesday
    ref = next((cy for cy in reversed(results) if cy.weeks), None)
    if ref is None:
        return [], []
    start = first_wednesday(ref.crop_year)
    idx, names, seen = [], [], set()
    for k in range(0, 44):
        d = start + timedelta(days=7 * k)
        if (d.year, d.month) not in seen:
            seen.add((d.year, d.month))
            idx.append(k)
            names.append(MONTHS[d.month - 1])
    return idx, names


def seasonal_chart(results: list, measure: str = "net", height: int = 340, logo_uri: str | None = None) -> alt.LayerChart | None:
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
    ticks, names = _month_ticks(results)
    xmax = int(df["week"].max())
    x_enc = alt.X("week:Q", title=None, scale=alt.Scale(domain=[0, max(xmax, 40) + 0.5]),
                  axis=alt.Axis(values=ticks, labelExpr=f"{json.dumps(names)}[indexof({json.dumps(ticks)}, datum.value)]",
                                labelColor="#1f4e79", labelFontWeight="bold", labelAngle=0, grid=False, ticks=False))
    y_title = "¢/bu"
    if logo_uri:
        layers.append(alt.Chart(pd.DataFrame({"x": [max(xmax, 40) / 2], "url": [logo_uri]}))
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
