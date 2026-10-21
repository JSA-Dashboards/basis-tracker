"""forward_month.py - one number per delivery month, for every forward curve drawn from posted period labels.

A posting often quotes a month several ways at once: 'Dec' (the whole month), 'FH Dec' / 'LH Dec' (its halves), and packages that span it
('JFM', 'Nov-Jul'). Averaging all of them made a chart disagree with the bid on the board: CSX Columbus posted Dec +16 and FH Dec +13 on
2026-10-20, and the emailed forward curve drew +14.5 ('+15') for December - and +10 for November, whose own bid was +19, because FH Nov and the
Nov-Jul package were averaged in (Kolten, 2026-10-20: "for months that show a full month but also a FH or LH just default to the full month
indication, only take the midpoint if its a FH and LH separate bids"). The rule, in one place:

  * a month's FULL bid is the month - its half-month bids and day windows are left out of it;
  * no full bid: the MIDPOINT of the first part (FH, or a window that opens the month) and the last part (LH), when both are posted;
  * no full bid and only one part posted: that part;
  * a package ('JFM', 'AMJJ', 'Nov-Jul', 'Oct/Nov') never overrides a month's own quote: it fills a month nothing of its own names, the narrowest
    package first, spread by a carry so its months average back to its bid (rail_forward).

Pure - no database, no Streamlit. A label is classified by return_to_carry_data.parse_label (the reading the shipment table and the Spread tab
use), imported on first use. Callers: rail_report (the emailed seasonal chart), app.py (the Rail FOB tab's seasonal chart; the Bids and Spread tabs'
forward points) and rail_spread (the Spread tab's forward spread).
"""
from __future__ import annotations

import re
from functools import lru_cache

FIRST_PART = ("fh", "window")      # a half, or a window to about the 20th, that opens the month
LAST_PART = ("lh",)                # the last half
CARRY = 2.0                        # cents per month: how a package is spread across the months it names (the chart's long-standing 2c)
ABBR = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}

_PLACEMENT = re.compile(r"\b(lp|fp)\b", re.I)      # LP (last placement) = LH, FP = FH: how the Rail FOB seasonal chart reads them
_SLASH_HALF = re.compile(r"\b([fl])\s*[/.]\s*h\b\.?", re.I)    # F/H, L/H (the freight sheets' spelling), F.H. = FH, LH
_MID_PLACEMENT = re.compile(r"\bmp\b", re.I)       # MP (mid-month placement): a partial of its month, not the whole month


@lru_cache(maxsize=4096)
def label_info(label):
    """(kind, months) of a posted period label: kind 'full' | 'window' | 'fh' | 'lh' | 'bundle', months a tuple of month numbers in the
    order the label names them. (None, ()) when the label names no month ('Spot', 'Return Trip', 'NC 26', note rows). The reading is
    return_to_carry_data.parse_label's, except that LP / FP / F/H / L/H are the last / first half and MP is a partial window (parse_label
    would call all of them whole-month bids). A spread note ('Spread-Sell FH Nov CP / Buy FH Nov ; offered') is no delivery period at all."""
    import return_to_carry_data as rd
    if rd.is_spread_note(label):
        return None, ()
    text = _SLASH_HALF.sub(lambda m: m.group(1).upper() + "H", str(label or ""))
    text = _PLACEMENT.sub(lambda m: "LH" if m.group(1).lower() == "lp" else "FH", text)
    info = rd.parse_label(text)
    if not info or not info.get("months"):
        return None, ()
    kind = info["kind"]
    if kind == "full" and _MID_PLACEMENT.search(text):
        kind = "window"
    return kind, tuple(info["months"])


def is_spread_note(label) -> bool:
    """A posted line that quotes a spread between two periods, not a period's basis (return_to_carry_data.is_spread_note): never a spot, a month or a package."""
    import return_to_carry_data as rd
    return rd.is_spread_note(label)


def kind_of(label):
    """The kind of one delivery label as a month quote - 'full' when it names no month at all (a 'New Crop' row stands for its futures month)."""
    return label_info(label)[0] or "full"


def month_value(quotes):
    """The one value a month stands for, from its quotes [(kind, value)]; None when there are none (a value of None is skipped).
    The mean of the full bids (normally one); else the midpoint of the first part's and the last part's means when both are posted; else the
    lone part; else - only package folds left - their mean."""
    full, first, last, folded = [], [], [], []
    for kind, v in quotes:
        if v is None:
            continue
        v = float(v)
        if kind in FIRST_PART:
            first.append(v)
        elif kind in LAST_PART:
            last.append(v)
        elif kind == "bundle":
            folded.append(v)
        else:
            full.append(v)

    def mean(xs):
        return sum(xs) / len(xs)

    if full:
        return mean(full)
    if first and last:
        return (mean(first) + mean(last)) / 2.0
    if first or last:
        return mean(first or last)
    return mean(folded) if folded else None


def pick_months(items):
    """{key: value} from [(key, kind, value)] - month_value over each key's quotes, keys in the order first seen. The key is whatever
    identifies a month to the caller: a month number, (year, month), (marketing year, week), or a whole (market, month, date) tuple."""
    by: dict = {}
    for key, kind, v in items:
        by.setdefault(key, []).append((kind, v))
    out: dict = {}
    for key, qs in by.items():
        v = month_value(qs)
        if v is not None:
            out[key] = v
    return out


def rail_forward(cells, carry=CARRY):
    """{month number: value} for ONE rail posting; cells = [(period label, bid)], a bid of None skipped, a label naming no month ignored.
    A month's own quotes go through month_value. A package (one price for several months) fills each month nothing of its own names, the
    narrowest package first (the higher bid between equals), spread by `carry` cents a month around its centre so its months average back to
    the package bid: JFM +19 -> Jan 17 / Feb 19 / Mar 21."""
    own: dict = {}
    packages: list = []
    for label, bid in cells:
        if bid is None:
            continue
        kind, months = label_info(label)
        if not months:
            continue
        if kind == "bundle" or len(months) > 1:
            packages.append((len(months), -float(bid), months, float(bid)))
        else:
            own.setdefault(months[0], []).append((kind, bid))
    out = {m: month_value(qs) for m, qs in own.items()}
    for _span, _nb, months, bid in sorted(packages, key=lambda p: (p[0], p[1])):
        centre = (len(months) - 1) / 2.0
        for i, m in enumerate(months):
            if m not in out:
                out[m] = round(bid + carry * (i - centre), 1)
    return out
