"""Highlight years on the Spread tab's seasonal chart: pure helpers (tests/test_spread_highlight.py); app.py only draws.

Kolten, 2026-11-13, with a screenshot of the Spread tab (CSX Columbus - CSX Evansville, corn): "On the spreads tab I want to be able to highlight a year on
the chart (i.e. 2025/26 listed on chart)". The chart draws the current marketing year in black against the 5-year range and average; a picked past year is
drawn as a coloured line over the range, labelled with its year at its last point like the current year, and named in the legend line above the chart.
"""
from __future__ import annotations

# (hex, name): the colour a highlighted marketing year is drawn in, in the order the years were picked, and what the legend line calls it. None is the black of
# the current year, the dark green of the 5-yr average, the pale green of the range or the brick red of the forward points.
HL_COLORS = (("#0693e3", "blue"), ("#f97316", "orange"), ("#8b5cf6", "purple"), ("#0d9488", "teal"))
MAX_HIGHLIGHTS = len(HL_COLORS)


def year_options(years, current_num=None) -> list:
    """The marketing years that can be highlighted, newest first, as the labels the chart uses ('2025/26'). `years` = (start-year number, label) pairs, repeats
    allowed (one per week of data); the current year, which is always drawn in black, is left out."""
    seen: dict = {}
    for num, label in years:
        if num is None or label is None:
            continue
        seen[int(num)] = str(label)
    return [seen[n] for n in sorted(seen, reverse=True) if current_num is None or n != int(current_num)]


def clean_selection(selected, options, limit: int = MAX_HIGHLIGHTS) -> list:
    """What is still picked: the picks that are on offer, in the order they were made, each once, at most `limit`. A pick the new pair of locations has no data for
    (the picker keeps its value from the last pair) is dropped here, before the widget is built."""
    out: list = []
    for s in (selected or ()):
        if s in options and s not in out:
            out.append(s)
    return out[:limit]


def colour_for(index: int):
    """(hex, name) of the index-th highlighted year."""
    return HL_COLORS[index % len(HL_COLORS)]


def label_offsets(fixed, labels, px_per_unit, *, gap_px: float = 12.0, week_gap: float = 1.8, step_px: float = 12.0, tries: int = 9) -> dict:
    """Vertical nudges, in pixels (negative = up), that keep the year labels of the highlighted lines off each other and off the labels that cannot move.

    A label sits at its line's last point and reads to the right, so two collide when their weeks are within `week_gap` and their heights on screen (value x
    px_per_unit, plus the nudge) are less than `gap_px` apart: a prior year ends in August like the 5-yr average, whose "5-yr avg" label is already there.
    fixed: [(week, value)] labels that stay put (the average's, the current year's). labels: [(key, week, value)] the ones to place, the highest first; each tries 0,
    then 12 px up, 12 down, 24 up, ... and takes the first free spot. Returns {key: dy}."""
    out = {key: 0 for key, _, _ in labels}
    if not px_per_unit or px_per_unit <= 0:
        return out
    placed = [(float(w), -float(v) * px_per_unit) for w, v in fixed]            # (week, height on screen): SVG y grows downward
    cand = [0.0]
    for i in range(1, tries):
        cand.append((-1 if i % 2 else 1) * step_px * ((i + 1) // 2))

    def clear(week, pos):
        return all(abs(pos - py) >= gap_px or abs(week - pw) > week_gap for pw, py in placed)

    for key, week, value in sorted(labels, key=lambda t: -float(t[2])):         # stable: equal values keep the order given
        base = -float(value) * px_per_unit
        week = float(week)
        spot = next((dy for dy in cand if clear(week, base + dy)), None)
        if spot is None:                                                        # a crowd bigger than the tries cover: the roomiest one
            spot = max(cand, key=lambda dy: min([abs(base + dy - py) for pw, py in placed if abs(week - pw) <= week_gap] or [gap_px]))
        placed.append((week, base + spot))
        out[key] = int(round(spot))
    return out
