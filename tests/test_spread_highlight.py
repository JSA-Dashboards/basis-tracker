"""The Spread tab's highlighted years: the pure helpers (spread_highlight.py).

    python tests/test_spread_highlight.py

Kolten 2026-11-13: "On the spreads tab I want to be able to highlight a year on the chart (i.e. 2025/26 listed on chart)". The helpers decide which years can be
picked, which picks survive a change of locations, what colour each gets and where its end-of-line label goes so it does not sit on the 5-yr avg label. No
Streamlit, no database: tests/test_spread_highlight_tab.py drives the real tab.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spread_highlight as sh   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


print("which years can be highlighted")
YEARS = [(2025, "2025/26"), (2025, "2025/26"), (2024, "2024/25"), (2023, "2023/24"), (2026, "2026/27"), (2021, "2021/22"), (2024, "2024/25")]
check("newest first, one entry per year, the current year (always black) left out",
      sh.year_options(YEARS, 2026) == ["2025/26", "2024/25", "2023/24", "2021/22"], sh.year_options(YEARS, 2026))
check("with no current year every year is on offer, the newest first", sh.year_options(YEARS) == ["2026/27", "2025/26", "2024/25", "2023/24", "2021/22"], sh.year_options(YEARS))
check("a year with no data is not there (the list is built from the years that have weeks), and an empty list gives an empty list", sh.year_options([], 2026) == [] and sh.year_options([(2026, "2026/27")], 2026) == [])
check("the current year may be given as a numpy-style number, an unknown pair is skipped",
      sh.year_options([(2025.0, "2025/26"), (None, "x"), (2024, None), (2024, "2024/25")], 2026.0) == ["2025/26", "2024/25"], sh.year_options([(2025.0, "2025/26"), (None, "x"), (2024, None), (2024, "2024/25")], 2026.0))
check("a gap in the years stays a gap (no filling in)", sh.year_options([(2025, "2025/26"), (2022, "2022/23")], 2026) == ["2025/26", "2022/23"])

print("\nwhat stays picked when the locations change")
OPTS = ["2025/26", "2024/25", "2023/24", "2022/23", "2021/22"]
check("picks still on offer are kept in the order they were made (not the list's order)", sh.clean_selection(["2023/24", "2025/26"], OPTS) == ["2023/24", "2025/26"])
check("a pick the new pair has no data for is dropped, the rest kept", sh.clean_selection(["2019/20", "2025/26"], OPTS) == ["2025/26"])
check("a repeat is kept once", sh.clean_selection(["2025/26", "2025/26", "2024/25"], OPTS) == ["2025/26", "2024/25"])
check("nothing, or nothing valid, is an empty list (and the same list object is never handed back)",
      sh.clean_selection(None, OPTS) == [] and sh.clean_selection([], OPTS) == [] and sh.clean_selection(["x"], OPTS) == [])
_in = ["2025/26", "2024/25"]
check("the input is not changed", sh.clean_selection(_in, OPTS) is not _in and _in == ["2025/26", "2024/25"])
check("at most four are kept (the colours there are), the first four picks", sh.clean_selection(OPTS, OPTS) == OPTS[:4] and sh.MAX_HIGHLIGHTS == 4, sh.clean_selection(OPTS, OPTS))
check("a smaller limit is honoured", sh.clean_selection(OPTS, OPTS, limit=2) == OPTS[:2])
check("a tuple or any other sequence works too", sh.clean_selection(("2024/25",), OPTS) == ["2024/25"])

print("\ncolours")
check("the first pick is the brand blue, then orange, purple, teal", [sh.colour_for(i) for i in range(4)] == [("#0693e3", "blue"), ("#f97316", "orange"), ("#8b5cf6", "purple"), ("#0d9488", "teal")], [sh.colour_for(i) for i in range(4)])
check("a fifth pick wraps round (it cannot happen through clean_selection)", sh.colour_for(4) == sh.colour_for(0))
RESERVED = {"#111827", "#000", "#000000", "#4b6a4b", "#c4d7bd", "#8bab7f", "#c0392b"}      # the chart's black, avg green, range greens and forward red
check("no highlight colour is one the chart already uses, and no two are alike", not ({c for c, _ in sh.HL_COLORS} & RESERVED) and len({c for c, _ in sh.HL_COLORS}) == 4 == len({n for _, n in sh.HL_COLORS}))

print("\nend-of-line labels stay readable (5 px per cent: two labels 12 px apart = 2.4 cents)")
P = 5.0
check("a label far above the average's is left where it is", sh.label_offsets([(52, 10)], [("a", 52, 0)], P) == {"a": 0})
check("a label on top of the 5-yr avg label that is a little ABOVE it moves UP (-12 px)", sh.label_offsets([(52, 10)], [("a", 52, 10.5)], P) == {"a": -12})
check("one a little BELOW it moves DOWN (+12 px): up would still touch it", sh.label_offsets([(52, 10)], [("a", 52, 9.5)], P) == {"a": 12})
check("a label ending weeks earlier does not care (no overlap sideways)", sh.label_offsets([(52, 10)], [("a", 40, 10)], P) == {"a": 0})
check("... the edge: 1.8 weeks apart touches, 1.9 does not", sh.label_offsets([(52, 10)], [("a", 50.2, 10)], P)["a"] != 0 and sh.label_offsets([(52, 10)], [("a", 50.1, 10)], P) == {"a": 0})
check("two highlights at the same spot: the first keeps it, the second goes up", sh.label_offsets([], [("a", 52, 5), ("b", 52, 5)], P) == {"a": 0, "b": -12}, sh.label_offsets([], [("a", 52, 5), ("b", 52, 5)], P))
check("three at the same spot: 0, 12 up, 12 down", sh.label_offsets([], [("a", 52, 5), ("b", 52, 5), ("c", 52, 5)], P) == {"a": 0, "b": -12, "c": 12}, sh.label_offsets([], [("a", 52, 5), ("b", 52, 5), ("c", 52, 5)], P))
check("the higher label is placed first and keeps its place: the lower one gives way", sh.label_offsets([], [("lo", 52, 4.8), ("hi", 52, 5.0)], P)["hi"] == 0 and sh.label_offsets([], [("lo", 52, 4.8), ("hi", 52, 5.0)], P)["lo"] != 0)
check("without a scale (no pixels per cent) nothing moves; no labels, nothing to return",
      sh.label_offsets([(52, 10)], [("a", 52, 10)], 0) == {"a": 0} and sh.label_offsets([(52, 10)], [("a", 52, 10)], None) == {"a": 0} and sh.label_offsets([(52, 10)], [], P) == {})
check("the answer is whole pixels, always a multiple of the step", all(isinstance(v, int) and v % 12 == 0 for v in sh.label_offsets([(52, 5)], [("a", 52, 5), ("b", 52, 5.2), ("c", 52, 4.9)], P).values()))
check("the same input always gives the same answer", sh.label_offsets([(52, 5)], [("a", 52, 5), ("b", 52, 5.2)], P) == sh.label_offsets([(52, 5)], [("a", 52, 5), ("b", 52, 5.2)], P))

rng = random.Random(7)
bad = None
for trial in range(400):
    fixed = [(rng.choice([51, 52]), rng.uniform(2, 8)) for _ in range(rng.randint(0, 2))]
    labels = [("k%d" % i, rng.choice([50.5, 51, 52]), rng.uniform(3, 7)) for i in range(rng.randint(1, 4))]
    dy = sh.label_offsets(fixed, labels, P)
    spots = [(w, -v * P + dy[k], k) for k, w, v in labels]
    pts = [(w, -v * P, None) for w, v in fixed] + spots
    for i, (w1, p1, k1) in enumerate(pts):
        for w2, p2, k2 in pts[i + 1:]:
            if (k1 is None and k2 is None) or abs(w1 - w2) > 1.8:
                continue                                     # two fixed labels are not ours to move; different weeks do not touch
            if abs(p1 - p2) < 12 - 1e-9:
                bad = (fixed, labels, dy)
print()
check("400 random crowds (up to 4 labels and 2 fixed ones within two weeks of each other): no two end up closer than 12 px", bad is None, bad)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
