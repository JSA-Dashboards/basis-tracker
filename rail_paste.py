"""rail_paste.py — parse a pasted manual rail-corridor rundown into rail_fob rows.

This is the rail analogue of the River FOB portal's `paste_parse`: the user pastes
one corridor's rundown, this turns it into rows for `save_rail_fob`, and the app
shows them in an editable grid for review before saving.

It encodes the desk conventions (see the feedback-rail-entry memory):
  • the value is recorded EXACTLY as posted — never rolled or spread-adjusted;
    the tag only picks the contract, it never changes the number.
  • contract from the cell's tag when present (u=CU z=CZ h=CH k=CK n=CN); else the
    standard corn cycle via rail_corridors (post-FND roll), with the special rule
    that a **December delivery defaults to CH (March) unless tagged z**, and
    spanning packages (AMJJ, Jan-Jul, …) price vs 'R'.
  • futures stored as a full CME symbol (ZCZ26, ZCH27) so the board's roll-adjust
    math works — or 'R' for a spanning package, or None when unknown/pending.
  • desk notations dropped (pk / wtx / mex / dom / KS / MO / center); a FH/LH/MP
    qualifier stays in the period label.
  • '*' or blank → None ; 'Flat' → 0 ; a bare '?' → pending (kept as the raw '?'
    so the board shows "?"); a value plus '?' (e.g. 27?) → keep 27, drop the '?'.
  • period_order follows the listed order of the cells.

Cell grammar handled: "PERIOD bid[/offer][tag]" repeated, e.g.
    Sep -5/+3  Oct 0/6  Nov 4/10  Dec 8/14z  JFM 12/18  AMJJ 17/25
Everything between one value expression and the next is read as that cell's
period, so multi-word periods ("FH Sep", "LH Oct/FH Nov", "Jan-Jul") work.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from rail_corridors import (RAIL_BY_CORRIDOR, canonical_corridor, corn_futures,
                            corn_fnd, period_start_month)

_TAG2SHORT = {"u": "CU", "z": "CZ", "h": "CH", "k": "CK", "n": "CN"}
_CM = {"CH": 3, "CK": 5, "CN": 7, "CU": 9, "CZ": 12}     # contract → month

# Desk notations that ride along with a value but are NOT part of the period and
# NOT futures tags — dropped per the memory. (dom/mex also split the UP Illinois
# market, but the corridor is picked separately here, so in a cell they're noise.)
_DROP_NOTES = re.compile(r"(?i)\b(pk|wtx|dctx|mex|dom|ks/mo|ks|mo|center|ctr)\b")

# A single value token: a signed number (optionally trailing '?'), 'Flat', '*',
# or a bare '?'. Guarded by (?<![\w'.]) so it can't start mid-number and a forward
# crop-year like '26 isn't read as a value — while still allowing a leading +/-
# sign (a plain \b would swallow the sign). Two tokens may be joined by '/', with
# an optional trailing tag letter.
# The tag must abut the value (no space): tags are written joined, "9z"/"0z". A
# space before an [uzhkn] would otherwise swallow the leading letter of the next
# period (e.g. the "N" of "Nov" reads as tag "n").
# NB: the whitespace lives INSIDE the optional offer group, so a cell with no
# offer doesn't consume the trailing space and let the tag grab the next period's
# leading letter (e.g. the "N" of "Nov").
_VAL = r"[+-]?\d+(?:\.\d+)?\??|flat|\*|\?"
_CELL = re.compile(rf"(?<![\w'.])({_VAL})(?:\s*/\s*({_VAL}))?([uzhkn])?", re.I)


def _val(tok):
    """One value token → (number_or_None, raw_or_None). raw is set only for a
    pending '?' so the board renders "?" instead of "—"."""
    if tok is None:
        return None, None
    t = tok.strip().lower()
    if t == "?":
        return None, "?"
    if t in ("", "*"):
        return None, None
    if t == "flat":
        return 0, None
    t = t.rstrip("?")
    try:
        return int(round(float(t))), None
    except ValueError:
        return None, None


def _to_full(short: str, as_of: date) -> str:
    """Short corn code ('CZ') → full CME symbol ('ZCZ26') for the active year at
    `as_of`, rolling the year forward once that contract is past First Notice."""
    y = as_of.year
    for _ in range(6):
        if corn_fnd(short, y) >= as_of:
            break
        y += 1
    return f"ZC{short[1]}{y % 100:02d}"


def _futures_for(period: str, rail: str | None, tag: str | None, as_of: date):
    """Resolve a cell's futures: full symbol (ZCZ26), 'R' (spanning), or None."""
    short = corn_futures(period, rail, as_of)          # 'CH' / 'CZ' / 'R' / None
    if tag:
        short = _TAG2SHORT[tag.lower()]                # explicit tag wins
    elif period_start_month(period) == 12:
        short = "CH"                                   # Dec defaults to CH unless tagged z
    if not short or short == "R":
        return short                                   # None or 'R' (no year/roll math)
    return _to_full(short, as_of)


def _clean_period(raw: str, corridor: str) -> str:
    """Strip brackets, the word 'rundown', the corridor's own name/aliases, and
    desk notations from the text that precedes a value — leaving the period."""
    s = re.sub(r"[\[\]()]", " ", raw)
    s = re.sub(r"(?i)\brundown\b", " ", s)
    s = _DROP_NOTES.sub(" ", s)
    for name in {corridor, canonical_corridor(corridor)}:
        for tok in str(name).split():
            if len(tok) > 1:                           # skip 1-char tokens (e.g. rail initials collide with months)
                s = re.sub(rf"(?i)\b{re.escape(tok)}\b", " ", s)
    s = s.strip(" \t\r\n/,;:-")
    return re.sub(r"\s+", " ", s).strip()


def parse_rundown(text: str, corridor: str, as_of=None):
    """Parse one corridor's pasted rundown into (rows, warnings).

    rows: dicts ready for database.save_rail_fob(date, 'manual', rows).
    warnings: human-readable notes about cells that couldn't be placed.
    """
    if as_of is None:
        as_of = date.today()
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    corridor = canonical_corridor(corridor)
    rail = RAIL_BY_CORRIDOR.get(corridor)

    rows, warnings = [], []
    last_end, order = 0, 0
    for m in _CELL.finditer(text or ""):
        period = _clean_period(text[last_end:m.start()], corridor)
        last_end = m.end()
        bid, bid_raw = _val(m.group(1))
        offer, offer_raw = _val(m.group(2))
        tag = m.group(3)
        if bid is None and offer is None and not bid_raw and not offer_raw:
            continue                                   # blank / N-A cell
        if not period:
            warnings.append(f"Skipped a value with no period: “{m.group(0).strip()}”.")
            continue
        rows.append({
            "market": corridor, "rail": rail, "commodity": "Corn",
            "period": period, "period_order": order,
            "futures": _futures_for(period, rail, tag, as_of),
            "bid": bid, "offer": offer,
            "bid_raw": bid_raw, "offer_raw": offer_raw,
        })
        order += 1
    return rows, warnings


# ── Multi-corridor detection ────────────────────────────────────────────────────
# Shorthand the desk pastes as a corridor header → canonical market name (matching
# the names on the Rail FOB board). Keys are matched case-insensitively and
# tolerate flexible spacing / apostrophes / parentheses (see _alias_tokens).
_CORR_ALIASES = {
    # Eastern
    "ns ft wayne": "NS Ft Wayne", "ft wayne": "NS Ft Wayne", "fort wayne": "NS Ft Wayne",
    "ns": "NS Ft Wayne",
    "csx columbus": "CSX Columbus", "columbus": "CSX Columbus", "col": "CSX Columbus",
    "csx evansville": "CSX Evansville", "evansville": "CSX Evansville", "eville": "CSX Evansville",
    "csx freight": "CSX Freight",
    # Gulf export
    "cn 105s": "CN 105s", "cn 105": "CN 105s",
    "cn 25s": "CN 25's", "cn 25": "CN 25's",
    # UP western
    "up group 3": "UP Group 3", "group 3": "UP Group 3", "up grp 3": "UP Group 3",
    "up interior ia": "UP Interior IA", "interior ia": "UP Interior IA", "up interior": "UP Interior IA",
    "up illinois dom": "UP Illinois (Dom)", "up il dom": "UP Illinois (Dom)",
    "allen station dom": "UP Illinois (Dom)",
    "up illinois mex": "UP Illinois (Mex)", "up il mex": "UP Illinois (Mex)",
    "allen station mex": "UP Illinois (Mex)",
    "up freight": "UP Freight", "up 110 shuttle": "UP Freight", "up shuttle": "UP Freight",
    # BN western
    "bn hereford": "BN Hereford", "hereford": "BN Hereford", "bncn sellers": "BN Hereford",
    "bn pnw be": "BN PNW BE", "pnw be": "BN PNW BE",
    "bn pnw cp": "BN PNW CP", "cp pnw": "BN PNW CP",
    "bn pnw": "BN PNW", "pnw": "BN PNW",
    "bn cobo": "BN COBO", "cobo": "BN COBO",
    "bn freight": "BN Freight", "bn 110 shuttle": "BN Freight", "bn shuttle": "BN Freight",
}


def _alias_tokens(a: str) -> tuple:
    """Alphanumeric word tokens of an alias/header, lower-cased."""
    return tuple(re.findall(r"[a-z0-9]+", a.lower()))


# canonical corridor keyed by its token tuple (unambiguous; duplicate tuples that
# map to the same corridor are harmless).
_ALIAS_BY_TOKENS = {_alias_tokens(a): c for a, c in _CORR_ALIASES.items()}

# One alternation, alternatives longest-first so "BN PNW BE" wins over "BN PNW"
# over "PNW". Tokens may be separated by spaces / apostrophes / parentheses, and
# the whole thing is bounded so it can't match inside a longer word.
_CORR_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(
        r"[\s'’()]*".join(re.escape(t) for t in _alias_tokens(a))
        for a in sorted(_CORR_ALIASES, key=len, reverse=True)
    ) + r")(?![A-Za-z0-9])", re.I)


def detect_segments(text: str):
    """Split a multi-corridor paste into [(corridor, cell_text), …] by finding
    corridor headers. Text before the first header is ignored."""
    text = text or ""
    ms = list(_CORR_RE.finditer(text))
    segs = []
    for i, m in enumerate(ms):
        corr = _ALIAS_BY_TOKENS.get(_alias_tokens(m.group(1)))
        if not corr:
            continue
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        segs.append((corr, text[start:end]))
    return segs


def parse_multi(text: str, as_of=None, fallback_corridor: str | None = None):
    """Parse a paste that may contain several corridors. Detects each corridor by
    its header name and parses that block. Returns (rows, warnings) where rows
    carry their own `market`. If no header is found, falls back to
    `fallback_corridor` (whole text as one corridor) when given."""
    segs = detect_segments(text)
    if not segs:
        if fallback_corridor:
            return parse_rundown(text, fallback_corridor, as_of)
        return [], ["No corridor name detected — start each block with the corridor "
                    "name (e.g. “NS Ft Wayne”, “Col”, “Eville”)."]
    rows, warnings = [], []
    for corr, seg in segs:
        r, w = parse_rundown(seg, corr, as_of)
        if not r:
            warnings.append(f"“{corr}” — no values parsed from its block.")
        rows.extend(r)
        warnings.extend(w)
    return rows, warnings
