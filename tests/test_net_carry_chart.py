"""The Net Carry tab's 'Cash Fwd Curve' chart: what is plotted, where the top of net carry is marked,
and that the spec is valid Vega-Lite (it compiles, expressions and all).

    python tests/test_net_carry_chart.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import net_carry as nc          # noqa: E402
import net_carry_chart as ncc   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-9):
    return a is not None and abs(a - b) <= tol


RATE = 0.0613
STL = [
    {"delivery": "NC 26", "futures": "ZCZ26", "basis": -25}, {"delivery": "Nov 26", "futures": "ZCZ26", "basis": 3},
    {"delivery": "Dec 26", "futures": "ZCZ26", "basis": 19}, {"delivery": "Jan 27", "futures": "ZCH27", "basis": 14},
    {"delivery": "Feb 27", "futures": "ZCH27", "basis": 18}, {"delivery": "Mar 27", "futures": "ZCH27", "basis": 21},
    {"delivery": "April '27", "futures": "ZCK27", "basis": 23}, {"delivery": "May '27", "futures": "ZCK27", "basis": 27},
    {"delivery": "June 2027", "futures": "ZCN27", "basis": 27}, {"delivery": "July 2027", "futures": "ZCN27", "basis": 30},
]
CURVE = {"ZCZ26": 502.25, "ZCH27": 516.75, "ZCK27": 523.75, "ZCN27": 528.0}
rows, meta = nc.compute_net_carry(STL, "ZCZ26", CURVE, 10, RATE)
pts = nc.monthly_carry(rows)
top = nc.top_of_net_carry(pts, meta["anchor_ym"])
LOGO = "data:image/png;base64,iVBORw0KGgo="


def build(points=pts, tp=top, **kw):
    args = dict(title="Cash Fwd Curve Corn (Basis vs ZCZ26)", subtitle=["ADM · St. Louis, MO (Elevator)"],
                curve_label="10/02/26", net_label="Net of int (from Oct)", anchor_ym=meta["anchor_ym"], logo_uri=LOGO)
    args.update(kw)
    return ncc.build_curve_chart(points, tp, **args)


def datasets(spec):
    return {k: v for k, v in spec.get("datasets", {}).items()}


def layers(spec):
    return spec["layer"]


def mark_type(layer):
    m = layer.get("mark")
    return m if isinstance(m, str) else (m or {}).get("type")


print("net_carry_chart: x labels read like the River FOB sheet")
check("month names, NC for the new-crop point", ncc.x_labels(pts) == ["NC", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"], ncc.x_labels(pts))
longer = [{"label": "Oct 26", "new_crop": False}, {"label": "Nov 26", "new_crop": False}, {"label": "Oct 27", "new_crop": False}]
check("a month that would repeat -> every label carries its year", ncc.x_labels(longer) == ["Oct 26", "Nov 26", "Oct 27"])

print("net_carry_chart: titles fit a phone")
check("a short grain stays in the title, the location is the subtitle",
      ncc.chart_titles("Corn", "ZCZ26", "ADM · St. Louis, MO (Elevator)") == ("Cash Fwd Curve Corn (Basis vs ZCZ26)", ["ADM · St. Louis, MO (Elevator)"]))
check("soybeans still fit", ncc.chart_titles("Soybeans", "ZSX26", "Cargill · Iowa Falls")[0] == "Cash Fwd Curve Soybeans (Basis vs ZSX26)")
check("a long grain name moves to the subtitle",
      ncc.chart_titles("Hard Red Winter (HRW)", "KEZ26", "ADM · Abilene, KS") == ("Cash Fwd Curve (Basis vs KEZ26)", ["Hard Red Winter (HRW)", "ADM · Abilene, KS"]))
check("no reference contract -> 'quoted futures'", "quoted futures" in ncc.chart_titles("Corn", None, "CSX Columbus")[0] + " ".join(ncc.chart_titles("Corn", None, "CSX Columbus")[1]))
check("no title is wider than ~42 characters", all(len(ncc.chart_titles(g, r, "x")[0]) <= 42 for g in ("Corn", "Soybeans", "Wheat (Soft Red Winter)", "Hard Red Winter (HRW)", "Sorghum")
                                                      for r in ("ZCZ26", None)))

print("net_carry_chart: what is plotted")
ch = build()
spec = ch.to_dict()
ds = list(datasets(spec).values())
curve_rows = [r for d in ds for r in d if r.get("Kind") == "Curve"]
net_rows = [r for d in ds for r in d if r.get("Kind") == "Net"]
check("one blue point per month, in order, at the basis vs REF",
      [r["Month"] for r in curve_rows] == ncc.x_labels(pts) and all(close(r["Value"], p["basis_ref"]) for r, p in zip(curve_rows, pts)))
check("legend labels: the as-of date and 'Net of int (from Oct)'",
      {r["Series"] for r in curve_rows} == {"10/02/26"} and {r["Series"] for r in net_rows} == {"Net of int (from Oct)"})
check("the orange net line = net of interest per month", len(net_rows) == len(pts) and all(close(r["Value"], p["net"]) for r, p in zip(net_rows, pts)))
check("x is the month's index (so the peak marker lands exactly on it)", [r["x"] for r in curve_rows] == list(range(len(pts))))

pre = [dict(p) for p in pts]
pre.insert(0, dict(pts[0], ym=(2026, 9), label="Sep 26", net=-30.0, basis_ref=-30.0, new_crop=False))
net_pre = [r for d in build(points=pre, tp=None).to_dict()["datasets"].values() for r in d if r.get("Kind") == "Net"]
check("the net line starts at the carry start: a month BEFORE the anchor has a blue point but no orange one",
      len(net_pre) == len(pts) and all(r["Month"] != "Sep" for r in net_pre))

print("net_carry_chart: the top of net carry is marked")
tops = [d for d in datasets(spec).values() if d and "label" in d[0]]
check("one marker dataset, at the peak's x and net", len(tops) == 1 and tops[0][0]["x"] == top["x"] and close(tops[0][0]["Value"], top["net"]), tops)
check("its label says what and where: 'Top of net carry' / month / signed cents",
      tops[0][0]["label"] == "▲ Top of net carry\nJul 27 · +32.4¢", tops[0][0]["label"])
kinds = [mark_type(l) for l in layers(spec)]
check("layers: watermark, lines, rule, dot, halo, label", kinds == ["image", "line", "rule", "point", "text", "text"], kinds)
check("the dot is solid (Vega-Lite draws points at 70% opacity by default)", [l["mark"] for l in layers(spec) if mark_type(l) == "point"][0]["opacity"] == 1)

nonet = build(show_net=False).to_dict()
check("no board price (show_net=False): only the blue curve, no peak marker",
      not [r for d in nonet["datasets"].values() for r in d if r.get("Kind") == "Net"] and [mark_type(l) for l in nonet["layer"]] == ["image", "line"])
notop = build(tp=None).to_dict()
check("no top -> no marker layers", [mark_type(l) for l in notop["layer"]] == ["image", "line"])
nologo = build(logo_uri=None).to_dict()
check("no logo -> no watermark layer", [mark_type(l) for l in nologo["layer"]][0] == "line")

print("net_carry_chart: label alignment keeps the text on the plot")
al = lambda t: [l for l in build(tp=t).to_dict()["layer"] if mark_type(l) == "text"][-1]["mark"]["align"]
check("a peak at the left edge is left-aligned, at the right edge right-aligned, else centred",
      al(dict(top, x=0)) == "left" and al(dict(top, x=len(pts) - 1)) == "right" and al(dict(top, x=4)) == "center")

print("net_carry_chart: phone-friendly layout")
check("the legend sits under the plot (a right-hand legend squeezed the plot to a sliver at 375 px)",
      spec["layer"][1]["encoding"]["color"]["legend"]["orient"] == "bottom")
check("title + subtitle (the location) rather than one long line",
      spec["title"]["text"] == "Cash Fwd Curve Corn (Basis vs ZCZ26)" and spec["title"]["subtitle"] == ["ADM · St. Louis, MO (Elevator)"])
check("fonts shrink on a narrow chart (Vega expressions on `width`)", "width < 480" in json.dumps(spec))
check("the watermark is centred on the plot with the Vega `height` signal", "height / 2" in json.dumps(layers(spec)[0]))
check("title colour matches the River FOB sheet's dark red", spec["title"]["color"] == "#c00000")

print("net_carry_chart: the spec compiles to Vega (valid expressions, valid encodings)")
try:
    import vl_convert as vlc
    vega = vlc.vegalite_to_vega(ch.to_json())
    check("Vega-Lite -> Vega compiles", isinstance(vega, dict) and vega.get("marks"), list(vega)[:6])
    for name, c in (("no net", build(show_net=False)), ("no top", build(tp=None)), ("no logo", build(logo_uri=None)),
                    ("2 points", build(points=pts[:2], tp=nc.top_of_net_carry(pts[:2], meta["anchor_ym"])))):
        ok = isinstance(vlc.vegalite_to_vega(c.to_json()), dict)
        check("also compiles: %s" % name, ok)
except ImportError:
    print("  [SKIP] vl_convert is not installed")

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
