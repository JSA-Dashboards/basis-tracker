"""net_carry_chart.py — the Net Carry tab's "Cash Fwd Curve" chart.

Styled like the River FOB sheet's chart (river-fob-portal, app.py `render_carry_chart`): the
cash forward curve as a solid blue line ("Basis vs <reference>"), the same curve net of interest
from the carry-start month as an orange dashed line ("Net of int (from Oct)"), a "Curve" legend,
a dark-red bold title, month labels along the bottom, and the JSA 50-year watermark behind it.
What the sheet's chart doesn't have: the TOP OF NET CARRY (the month where the net-of-interest
line peaks) is marked with a dot, a faint vertical rule and a label; the values are in cents
(the Net Carry tab's unit; the River FOB sheet is in dollars); and it is built to survive a phone —
the legend sits under the plot (a legend on the right squeezed the plot to a sliver at 375 px), the
location is a subtitle rather than a long one-line title, and fonts shrink on a narrow chart.

Pure function — it takes the monthly points from net_carry.monthly_carry and returns an Altair
chart, so it can be tested without Streamlit.
"""
from __future__ import annotations

import json

import altair as alt
import pandas as pd

# tableau10's first two colours — what the River FOB sheet's chart gets from scheme="tableau10".
BLUE, ORANGE, DARK_ORANGE = "#4e79a7", "#f28e2b", "#9a3412"
TITLE_RED = "#c00000"
# "narrow" in Vega expressions: `width` is the plot's pixel width (a phone is ~280, a desktop 700+).
_NARROW = "width < 480"


def x_labels(points: list[dict]) -> list[str]:
    """Month names along the bottom, as on the River FOB sheet. A new-crop point reads 'NC'.
    If a month name would repeat (a curve longer than 12 months) every month carries its
    two-digit year so the labels stay unambiguous."""
    months = [p["label"].split()[0] for p in points if not p.get("new_crop")]
    repeat = len(months) != len(set(months))
    out = []
    for p in points:
        if p.get("new_crop"):
            out.append("NC")
        elif repeat:
            out.append(p["label"])                 # "Nov 26"
        else:
            out.append(p["label"].split()[0])      # "Nov"
    return out


def chart_titles(grain: str, ref: str | None, location: str) -> tuple[str, list[str]]:
    """(title, subtitle lines). 'Cash Fwd Curve Corn (Basis vs ZCZ26)' over the location, like the River
    FOB sheet's 'Cash Fwd Curve Corn (Basis Spot Futures): STL'. A long grain name ('Hard Red Winter
    (HRW)') would be clipped on a phone, so it moves down to the subtitle."""
    basis = f"(Basis vs {ref or 'quoted futures'})"
    title = f"Cash Fwd Curve {grain} {basis}"
    if len(title) <= 42:                      # ~310 px at the narrow title size
        return title, [location]
    return f"Cash Fwd Curve {basis}", [grain, location]


def peak_label(top: dict) -> str:
    """The two-line label at the peak: what it is, then where and how much."""
    return f"▲ Top of net carry\n{top['label']} · {top['net']:+.1f}¢"


def build_curve_chart(points: list[dict], top: dict | None, *, title: str, subtitle: str | list = "",
                      curve_label: str, net_label: str, anchor_ym: tuple | None = None,
                      show_net: bool = True, logo_uri: str | None = None,
                      height: int = 380) -> alt.LayerChart:
    """points: net_carry.monthly_carry(rows). top: net_carry.top_of_net_carry(...) or None.
    title / subtitle: see chart_titles — 'Cash Fwd Curve Corn (Basis vs ZCZ26)' / ['ADM · St. Louis, MO (Elevator)'].
    curve_label: the legend entry for the basis curve (the as-of date, e.g. '10/02/26').
    net_label: the legend entry for the net line (e.g. 'Net of int (from Oct)').
    The net line starts at `anchor_ym` (interest is zero before it)."""
    n = len(points)
    labels = x_labels(points)

    rows = [{"x": i, "Month": labels[i], "Value": float(p["basis_ref"]), "Series": curve_label, "Kind": "Curve"}
            for i, p in enumerate(points)]
    if show_net:
        rows += [{"x": i, "Month": labels[i], "Value": float(p["net"]), "Series": net_label, "Kind": "Net"}
                 for i, p in enumerate(points)
                 if p.get("net") is not None and (anchor_ym is None or p["ym"] >= anchor_ym)]
    df = pd.DataFrame(rows)

    # A quantitative x with a label expression (instead of a nominal one) so the watermark and the
    # peak marker can be placed on exact positions; the ticks still read as plain month names.
    x_enc = alt.X("x:Q", title=None, scale=alt.Scale(domain=[-0.4, n - 0.6]),
                  axis=alt.Axis(values=list(range(n)), labelExpr=f"{json.dumps(labels)}[datum.value]",
                                labelColor="#1f4e79", labelFontWeight="bold",
                                labelFontSize=alt.expr(f"{_NARROW} ? 10 : 12"),
                                labelAngle=0, labelOverlap=True, grid=False, ticks=False))
    # Auto-fit the Y domain to the data (don't force zero) so the curve fills the chart; padded so
    # the first and last points never sit on the axis edge.
    y_enc = alt.Y("Value:Q", title="¢/bu", scale=alt.Scale(zero=False, nice=True, padding=14),
                  axis=alt.Axis(format=".0f", titleColor="#64748b", labelColor="#64748b"))

    order = [curve_label] + ([net_label] if show_net else [])
    color = alt.Color("Series:N", sort=order, title="Curve",
                      scale=alt.Scale(domain=order, range=[BLUE, ORANGE][:len(order)]),
                      legend=alt.Legend(orient="bottom", direction="horizontal", titleOrient="left",
                                        titleAnchor="middle", offset=12))
    size = alt.condition(f"datum.Series === '{curve_label}'", alt.value(3.5), alt.value(2))
    dash = alt.StrokeDash("Kind:N", scale=alt.Scale(domain=["Curve", "Net"], range=[[1, 0], [6, 4]]),
                          legend=None)
    lines = alt.Chart(df).mark_line(point=alt.OverlayMarkDef(size=35)).encode(
        x=x_enc, y=y_enc, color=color, size=size, strokeDash=dash,
        tooltip=[alt.Tooltip("Series:N"), alt.Tooltip("Month:N"),
                 alt.Tooltip("Value:Q", format=".1f", title="¢/bu")])

    layers = []
    if logo_uri:
        # `height` here is the chart's total; the plot is shorter (title, axis, legend), so centre on
        # the plot itself with the Vega `height` signal.
        wm_h = int(height * 0.62)
        layers.append(alt.Chart(pd.DataFrame({"x": [(n - 1) / 2], "url": [logo_uri]}))
                      .mark_image(width=int(wm_h * 0.93), height=wm_h, opacity=0.16,
                                  align="center", baseline="middle")
                      .encode(x=alt.X("x:Q"), y=alt.value(alt.expr("height / 2")), url="url:N"))
    layers.append(lines)

    if top and show_net:
        end = top["x"]
        align = "left" if end <= 0.12 * (n - 1) else ("right" if end >= 0.88 * (n - 1) else "center")
        pk = pd.DataFrame([{"x": end, "Value": float(top["net"]), "Month": labels[end],
                            "label": peak_label(top)}])
        layers.append(alt.Chart(pk).mark_rule(strokeDash=[3, 3], color=ORANGE, opacity=0.6)
                      .encode(x="x:Q"))
        layers.append(alt.Chart(pk).mark_point(shape="circle", filled=True, size=300, color=ORANGE,
                                               opacity=1, stroke=DARK_ORANGE, strokeWidth=2.5)
                      .encode(x="x:Q", y="Value:Q",
                              tooltip=[alt.Tooltip("label:N", title=" "),
                                       alt.Tooltip("Value:Q", format="+.1f", title="Net of interest ¢/bu")]))
        # a white halo first, so the label stays legible where the blue curve runs behind it
        text = dict(baseline="bottom", dy=-22, fontSize=alt.expr(f"{_NARROW} ? 11 : 12"),
                    fontWeight="bold", lineBreak="\n", lineHeight=14, align=align)
        layers.append(alt.Chart(pk).mark_text(color="white", stroke="white", strokeWidth=5,
                                              strokeJoin="round", **text)
                      .encode(x="x:Q", y="Value:Q", text="label:N"))
        layers.append(alt.Chart(pk).mark_text(color=DARK_ORANGE, **text)
                      .encode(x="x:Q", y="Value:Q", text="label:N"))

    return (alt.layer(*layers)
            .properties(height=height, background="transparent",
                        padding={"left": 6, "right": 24, "top": 6, "bottom": 6},
                        title=alt.TitleParams(
                            title, subtitle=subtitle or None, anchor="middle", color=TITLE_RED,
                            fontWeight="bold", fontSize=alt.expr(f"{_NARROW} ? 14 : 17"),
                            subtitleColor=TITLE_RED, subtitleFontWeight="normal",
                            subtitleFontSize=alt.expr(f"{_NARROW} ? 11 : 13"), subtitlePadding=4))
            .configure_view(strokeWidth=0, fill=None)
            .configure_axis(grid=True, gridColor="#e6e6e6", domainColor="#cccccc")
            .configure_legend(titleColor="#1f4e79", labelColor="#333", labelFontWeight="bold",
                              padding=2))
