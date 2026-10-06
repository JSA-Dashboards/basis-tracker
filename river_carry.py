"""river_carry.py — the River FOB sheet archive as Net Carry / Return to Carry inputs.

The River FOB portal archives one sheet per as-of date (weekly since 2006-09, daily lately) in Snowflake RIVER_FOB.PUBLIC:

    cif_history       as_of, commodity, month, value    the CIF NOLA basis by delivery month ($/bu)
    freight_history   as_of, region,    month, value    barge freight by reach and month (a multiple of the tariff, 6.1 = 610%)
    calendar_history  as_of, commodity, seq, month, contract   the sheet's delivery months in order and the futures month each is priced off

and the sheet's own rule turns them into the FOB barge basis of every river location (fob_model):

    FOB[location][month] = CIF[month] - tariff factor x freight[reach][month] / 2000 x bushel weight

so twenty years of weekly forward curves exist for every river location even though no one wrote a report on them.
This module reads that into the shapes the Net Carry tab already consumes — pure functions over the loaded archive
(river_fob_data.load_archive), no database and no Streamlit:

    curve_items(archive, as_of, location, commodity)       one date's forward curve  [{'delivery', 'futures', 'basis'}]  (cents)
    nearby_obs(archive, location, commodity)               the weekly nearby FOB     [{'date', 'basis', 'tag'}]          (cents)
    forward_quotes(archive, location, commodity)           every posted month        [{'date', 'label', 'tag', 'basis'}] (cents)

`nearby_obs` and `forward_quotes` are what return_to_carry_data's tracker and shipment table take in place of a rail
corridor's or an elevator's bids.

How a sheet column becomes a delivery month and a futures contract
  * A sheet column is a calendar MONTH label ('Oct', 'July', 'Sept', and in 2023-25 a leading 'Spot') with no year. The
    first month starts at the sheet's date (0-2 months ahead) and each later column is the next such month. A sheet whose
    first month is already behind its date (stale headers) is set aside rather than guessed at.
  * The contract code ('CZ', 'SX') is the sheet's own mapping for that date; its year is the occurrence of that month letter
    NEAREST the delivery month (5 months before to 6 after): Dec 2026 + 'CH' is ZCH27, Jan 2027 + 'CH' is ZCH27.
  * 'Spot' is the prompt bid in the sheet's own month. When the sheet also has that month's column the month wins (the
    Spot cell is the same grain priced a day earlier), so a month is never quoted twice.
  * Only columns with a CIF and a barge freight give a FOB. A freight cell of 0 or blank means the river is closed at that
    reach (the upper river in winter) and the FOB is left undefined, never computed against zero freight (fob_model.fob_value).
"""
from __future__ import annotations

from datetime import date

import fob_model as M

ROOT = {"Corn": "ZC", "Soybeans": "ZS", "Wheat": "ZW"}
_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH = {}
for _n, (_a, _full) in enumerate((("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may", "may"),
                                  ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "september"), ("oct", "october"),
                                  ("nov", "november"), ("dec", "december")), start=1):
    _MONTH[_a] = _MONTH[_full] = _n
_MONTH["sept"] = 9
_CODE_MONTH = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
MAX_LEAD = 2          # the first month may start this many months after the sheet's date

# the river locations the sheet publishes today, in the sheet's order (Gulfport was dropped from it in 2023)
CURRENT_LOCATIONS = tuple(l.name for l in M.LOCATIONS if l.name != "Gulfport")
LOCATION = {l.name: l for l in M.LOCATIONS}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def month_number(label) -> int | None:
    """Calendar month of a sheet label ('Oct' 10, 'June' 6, 'Sept' 9); None for 'Spot' and anything else."""
    return _MONTH.get(str(label or "").strip().lower().rstrip("."))


def is_spot(label) -> bool:
    return str(label or "").strip().lower() == "spot"


def contract_symbol(commodity: str, code, ym: tuple) -> str | None:
    """'ZCZ26' for Corn + 'CZ' + delivery (2026, 10): the occurrence of the code's month letter nearest the delivery month."""
    letter = str(code or "").strip().upper()[-1:]
    cm = _CODE_MONTH.get(letter)
    root = ROOT.get(commodity)
    if cm is None or root is None or not ym:
        return None
    off = ((cm - ym[1] + 5) % 12) - 5
    i = ym[0] * 12 + ym[1] - 1 + off
    return f"{root}{letter}{(i // 12) % 100:02d}"


def delivery_label(ym: tuple, spot: bool = False) -> str:
    """'Oct 2026' / 'Spot Oct 2026': the label the Net Carry modules read, with the year spelled out."""
    return f"{'Spot ' if spot else ''}{_ABBR[ym[1] - 1]} {ym[0]}"


def place_columns(commodity: str, as_of: date, calendar_cols) -> list:
    """[(label, (year, month), code, symbol, spot)] for one sheet's columns in order, or [] when the sheet can't be placed
    (stale headers, an unreadable label). `calendar_cols` = [(month label, contract code), ...] as archived (calendar_history).
    A repeated label keeps its first column; 'Spot' is dropped when the sheet also has that month's own column."""
    cols, seen = [], set()
    for label, code in calendar_cols or []:
        key = str(label or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        cols.append((str(label).strip(), code))
    out, prev = [], None
    for label, code in cols:
        if is_spot(label):
            ym = (as_of.year, as_of.month)
        else:
            m = month_number(label)
            if m is None:
                return []
            if prev is None:
                y = as_of.year if m >= as_of.month else as_of.year + 1
                if (y * 12 + m) - (as_of.year * 12 + as_of.month) > MAX_LEAD:
                    return []
            else:
                y = prev[0] + (1 if m <= prev[1] else 0)
            ym = (y, m)
            prev = ym
        out.append((label, ym, code, contract_symbol(commodity, code, ym), is_spot(label)))
    own = {ym for _l, ym, _c, _s, spot in out if not spot}
    return [c for c in out if not (c[4] and c[1] in own)]


def fob_cents(archive: dict, as_of_iso: str, location: str, commodity: str, label: str) -> float | None:
    """The FOB barge basis of a location for one sheet column, in cents/bu (None when CIF or freight is missing)."""
    loc = LOCATION.get(location)
    if loc is None:
        return None
    cif = _num(((archive.get("cif") or {}).get(as_of_iso) or {}).get(commodity, {}).get(label))
    frt = _num(((archive.get("freight") or {}).get(as_of_iso) or {}).get(loc.region, {}).get(label))
    if cif is None or not frt:                      # no CIF, or a freight of 0 / blank: the river is closed at that reach, so no FOB
        return None                                 # (checked here as well, because an older fob_model.fob_value returns the CIF)
    v = M.fob_value(cif, frt, loc.factor, M.BUSHEL_WEIGHT[commodity])
    return None if v is None else v * 100.0


def _columns(archive: dict, as_of_iso: str, commodity: str) -> list:
    cal = ((archive.get("calendar") or {}).get(as_of_iso) or {}).get(commodity)
    try:
        d = date.fromisoformat(as_of_iso[:10])
    except ValueError:
        return []
    return place_columns(commodity, d, cal) if cal else []


def curve_items(archive: dict, as_of_iso: str, location: str, commodity: str) -> list[dict]:
    """One sheet's forward curve for a location, in the form net_carry reads: [{'delivery': 'Oct 2026', 'futures': 'ZCZ26',
    'basis': -40.1}] — cents per bushel, only the months that have a FOB."""
    out = []
    for label, ym, _code, sym, spot in _columns(archive, as_of_iso, commodity):
        b = fob_cents(archive, as_of_iso, location, commodity, label)
        if b is not None:
            out.append({"delivery": delivery_label(ym, spot), "futures": sym, "basis": round(b, 4)})
    return out


def nearby_obs(archive: dict, location: str, commodity: str) -> list[dict]:
    """The weekly nearby FOB of a location: one bid per archived sheet — the sheet's own month ('Spot' first when it has one) —
    quoted off the contract the sheet maps that month to. [{'date': date, 'basis': cents, 'tag': 'ZCZ26'}], oldest first.
    A sheet with no FOB for its own month (the upper river closed for the winter) gives no bid that week."""
    out = []
    for iso in sorted((archive.get("calendar") or {})):
        cols = _columns(archive, iso, commodity)
        if not cols:
            continue
        first = cols[0]
        cand = [first] + ([cols[1]] if first[4] and len(cols) > 1 else [])      # 'Spot', else the first month
        for label, _ym, _code, sym, _spot in cand:
            b = fob_cents(archive, iso, location, commodity, label)
            if b is not None:
                out.append({"date": date.fromisoformat(iso[:10]), "basis": b, "tag": sym})
                break
    return out


def forward_quotes(archive: dict, location: str, commodity: str) -> list[dict]:
    """Every month every archived sheet posted a FOB for, as the shipment table's quote rows:
    [{'date', 'label': 'Dec 2026', 'tag': 'ZCZ26', 'basis': cents}] (return_to_carry_data.shipment_quotes picks one per month)."""
    out = []
    for iso in sorted((archive.get("calendar") or {})):
        d = date.fromisoformat(iso[:10])
        for label, ym, _code, sym, spot in _columns(archive, iso, commodity):
            b = fob_cents(archive, iso, location, commodity, label)
            if b is not None:
                out.append({"date": d, "label": delivery_label(ym, spot), "tag": sym, "basis": b})
    return out


def dates(archive: dict, commodity: str | None = None) -> list[str]:
    """The archived as-of dates (ISO strings), newest first; with a commodity, only those that sheet could be placed for."""
    ds = sorted((archive.get("calendar") or {}), reverse=True)
    if commodity is None:
        return ds
    return [d for d in ds if _columns(archive, d, commodity)]


def reach_peers(location: str, n: int = 3) -> list[str]:
    """The `n` other river locations to compare a location with by default: those on its own reach first (nearest in the sheet's
    order), then the closest of the rest."""
    names = list(CURRENT_LOCATIONS)
    if location not in names:
        return []
    i, reach = names.index(location), LOCATION[location].reach
    others = sorted((x for x in names if x != location), key=lambda x: (LOCATION[x].reach != reach, abs(names.index(x) - i)))
    return others[:n]
