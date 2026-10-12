"""rail_spread.py — a rail corridor as one side of the Spread tab's  A − B  basis spread.

The Spread tab compares the front-month basis of two locations, posting date by posting date, against the 5-year range, with the spread
of their forward curves on top. A rail corridor (UP Allen Station, CSX Columbus, ...) can stand on either side. This is the pure part of
that — which corridors to offer, a corridor's nearby bid by date, its forward bids by delivery month, and the forward spread between two
sides — so the Streamlit code only draws.

  options(rows)                         the corridors worth offering (freight rates and dead side-markets left out), with display names
  spot_series(rows, market, commodity)  [(date, basis)] — the Rail FOB tab's "Spot" series: the weekly Spot row, then the nearest forward bid
  forward_quotes(rows, market, commodity)  the latest posting's bids by delivery month, each with the futures contract it is quoted off
  snapshot_quotes(snap_rows, grain, f)  the same for a basis location's newest snapshot
  forward_spread(a, b, curve)           A − B per delivery month, both sides in CASH (basis + the futures price it is quoted off)

Why the forward spread goes through cash: a rail corridor quotes December off March (ZCH) while an elevator quotes it off December
(ZCZ), so the two December basis figures sit on different futures months — on 2026-10-09 that was 15 cents (ZCH27 515.25 vs ZCZ26
500.25). Subtracting them raw would put the futures spread into the "basis spread". Basis + its futures price is the cash price, and
the difference of two cash prices is the spread that matters; where both sides quote off the same contract it equals the plain
difference of the two bases. The spot series is NOT converted — it is the basis as posted against whatever contract is nearby, which is
how every other series in the app is kept (and both sides are nearby, so they share the contract except around a roll).
"""
from __future__ import annotations

import re
from datetime import date

import delivery_period as _dp
import net_carry as _nc
import rail_paste as _rp
import return_to_carry_data as _rd
from rail_corridors import CORRIDOR_ORDER

PROVIDER = "Rail FOB"        # stands where a basis location's provider would, in the Spread tab's (provider, location) pairs
PREFIX = "🚂 Rail · "         # a corridor's label in the pickers
MIN_DATES = 2                # posting dates a corridor needs before it is worth offering
FRESH_DAYS = 10              # forward curves further apart than this are not compared (the Net Carry tab's rule)

# The names the Rail FOB board shows, except Allen Station, which is "UP Allen Station" here (every other corridor starts with its railroad).
DISPLAY = {
    "UP Illinois (Dom)": "UP Allen Station (Dom)",
    "UP Illinois (Mex)": "UP Allen Station (Mex)",
    "BN PNW CP": "CP PNW",
    "COL, OH Beans 90's": "CSX Columbus Beans",
}
# Kept off the Rail FOB board, so kept off here: the Palmetto feed's corn markets (the manual corridors carry the same bids) and a dead side-market.
HIDDEN = frozenset({"BN COBO Buyers", "COL, OH Corn 90's", "EVILLE, Corn- 90's", "NS FT. WAYNE, IN Corn- 105's"})

_OWN_KINDS = ("full", "window", "fh", "lh")      # parse_label kinds that name ONE delivery month


# ── which corridors ──────────────────────────────────────────────────────────────────────────────────
def is_freight(market: str, commodity: str | None = None) -> bool:
    """A freight rate ($/car), not a basis bid — the shuttle lines ('BN Freight', 'UP 110 Shuttle', commodity 'Freight')."""
    name = market or ""
    return name.endswith("Freight") or "Shuttle" in name or commodity == "Freight"


def display(market: str) -> str:
    return DISPLAY.get(market, market)


def label(market: str) -> str:
    """A corridor's label in the pickers: '🚂 Rail · UP Allen Station (Dom)'."""
    return PREFIX + display(market)


def plain(text: str) -> str:
    """A picker label without the emoji (a chart title or a file name: the PNG export has no emoji font)."""
    return text.replace("🚂 ", "", 1)


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def options(rows: list[dict]) -> list[dict]:
    """The corridors the Spread tab offers: [{'market', 'label', 'commodities'}], in the Rail FOB tab's order.
    rows = rail_fob rows ({date, market, commodity, bid, ...}). A corridor needs MIN_DATES posting dates with a bid for a commodity."""
    days: dict = {}
    for r in rows:
        m, c = r.get("market") or "", r.get("commodity") or "Corn"
        if r.get("bid") is None or not m or m in HIDDEN or is_freight(m, c):
            continue
        d = _day(r.get("date"))
        if d is not None:
            days.setdefault((m, c), set()).add(d)
    by_market: dict = {}
    for (m, c), ds in days.items():
        if len(ds) >= MIN_DATES:
            by_market.setdefault(m, []).append(c)
    return [{"market": m, "label": label(m), "commodities": sorted(cs)}
            for m, cs in sorted(by_market.items(), key=lambda kv: (CORRIDOR_ORDER.get(kv[0], 999), kv[0]))]


# ── the spot series ──────────────────────────────────────────────────────────────────────────────────
def spot_series(rows: list[dict], market: str, commodity: str = "Corn") -> list[tuple]:
    """[(date, basis in cents)], one per posting date: the corridor's 'Spot' bid (the weekly sheets up to mid-2026), else its nearest
    forward period's bid — the series behind the Rail FOB tab's Spot seasonal and the Return to Carry tracker (return_to_carry_data)."""
    return [(o["date"], float(o["basis"])) for o in _rd.obs_from_rail(rows, market, commodity)]


def latest_posting(rows: list[dict], market: str, commodity: str = "Corn") -> date | None:
    """The newest posting date with a bid, or None."""
    ds = [_day(r.get("date")) for r in rows
          if r.get("market") == market and (r.get("commodity") or "Corn") == commodity and r.get("bid") is not None]
    ds = [d for d in ds if d is not None]
    return max(ds) if ds else None


# ── the forward curve ────────────────────────────────────────────────────────────────────────────────
def resolve_tag(tag, posted: date) -> str | None:
    """The CME contract a posted tag names, as a full symbol ('ZCH27'), or None when it names none. A rundown cell carries a full symbol;
    the older Palmetto feed and some early rows carry the short code ('CU', 'SX'), whose year is the next such contract at the posting
    date (rail_paste's roll). 'R' (the respective option month of a multi-contract package) and a blank name no single contract."""
    t = str(tag or "").strip().upper()
    if _nc.parse_symbol(t):
        return t
    if re.fullmatch(r"C[HKNUZ]", t):
        return _rp._to_full(t, posted)
    if re.fullmatch(r"S[FHKNQUX]", t):
        return _rp._soy_to_full(t, posted)
    return None


def forward_quotes(rows: list[dict], market: str, commodity: str = "Corn") -> tuple:
    """(posting date, quotes) — the latest posting of a corridor, as delivery-month quotes [{'ym': (year, month), 'basis', 'tag', 'label'}].

    A label names its months (return_to_carry_data.parse_label) and its year is the next such month from the posting date, as in the
    shipment table. A month with a quote of its own ('Dec', 'FH Dec', 'LH Dec', 'Dec 1-20') uses those — several are averaged later, the
    way halves are on the elevator side; a month with none is filled by the narrowest package that names it ('JFM' for Jan, Feb and
    Mar; of equals the higher bid), so a package never overrides a month's own bid. Cells with no bid, labels that name no month
    ('Spot', 'Return Trip', note rows) and quotes whose contract cannot be named ('R', blank) are left out."""
    posted = latest_posting(rows, market, commodity)
    if posted is None:
        return None, []
    own: dict = {}                      # ym -> [quote]
    bundles: list = []                  # (span, -basis, ym, quote)
    for r in rows:
        if (r.get("market") != market or (r.get("commodity") or "Corn") != commodity or r.get("bid") is None
                or _day(r.get("date")) != posted):
            continue
        info = _rd.parse_label(r.get("period"))
        tag = resolve_tag(r.get("futures"), posted)
        if info is None or not info.get("months") or tag is None:
            continue
        for m in info["months"]:
            y = info["year"] if (info["year"] is not None and info["kind"] != "bundle") else (posted.year if m >= posted.month else posted.year + 1)
            q = {"ym": (y, m), "basis": float(r["bid"]), "tag": tag, "label": r.get("period") or ""}
            if info["kind"] in _OWN_KINDS:
                own.setdefault((y, m), []).append(q)
            elif info["kind"] == "bundle":
                bundles.append((info["span"], -q["basis"], (y, m), q))
    out = [q for qs in own.values() for q in qs]
    filled = set(own)
    for _span, _nb, ym, q in sorted(bundles, key=lambda t: (t[0], t[1])):
        if ym not in filled:
            out.append(q)
            filled.add(ym)
    return posted, sorted(out, key=lambda q: (q["ym"], q["label"]))


def snapshot_quotes(rows, grain: str, grain_disp) -> list[dict]:
    """A basis location's forward rows for one display grain as delivery-month quotes — the rows the Spread tab's own forward curve
    uses: not the spot row, with a basis and a futures symbol, the month from delivery_period.canonical."""
    out = []
    for r in rows:
        if r.isSpot or r.basisCents is None or not r.futuresSymbol or grain_disp(r.grain) != grain:
            continue
        ym = _dp.canonical(r.deliveryMonth or "", r.futuresSymbol)
        if ym:
            out.append({"ym": ym, "basis": float(r.basisCents), "tag": r.futuresSymbol, "label": r.deliveryMonth or ""})
    return out


def market_year(year: int, month: int) -> int:
    """The marketing year (Sep-Aug) a delivery month belongs to, named by the year it starts in."""
    return year if month >= 9 else year - 1


def market_week(year: int, month: int) -> int:
    """The marketing week (1-52) a delivery month starts in — the Spread chart's x position for a forward point."""
    return max(1, min(52, ((date(year, month, 1) - date(market_year(year, month), 9, 1)).days // 7) + 1))


def _cash_by_month(quotes: list[dict], curve: dict) -> dict:
    """{ym: mean cash price (basis + its contract's futures price)} over the quotes whose contract the curve prices."""
    acc: dict = {}
    for q in quotes:
        px = (curve or {}).get(str(q.get("tag") or ""))
        if px is None or q.get("basis") is None or not q.get("ym"):
            continue
        acc.setdefault(q["ym"], []).append(float(q["basis"]) + float(px))
    return {ym: sum(v) / len(v) for ym, v in acc.items()}


def forward_spread(a: list[dict], b: list[dict], curve: dict) -> list[dict]:
    """A − B for every delivery month both sides quote and the curve can price: [{'ym', 'MktYearNum', 'MktWeek', 'Spread', 'A', 'B'}]
    sorted by month, spreads rounded to a tenth of a cent. A, B = the quotes of the two sides; curve = {symbol: cents}, the futures day
    both are priced on. Several quotes of one month (halves) are averaged in cash."""
    ca, cb = _cash_by_month(a, curve), _cash_by_month(b, curve)
    out = []
    for ym in sorted(set(ca) & set(cb)):
        out.append({"ym": ym, "MktYearNum": market_year(*ym), "MktWeek": market_week(*ym),
                    "Spread": round(ca[ym] - cb[ym], 1), "A": ca[ym], "B": cb[ym]})
    return out


def comparable(date_a: date | None, date_b: date | None, days: int = FRESH_DAYS) -> bool:
    """Whether two forward curves are close enough in time to put side by side."""
    return date_a is not None and date_b is not None and abs((date_a - date_b).days) <= days
