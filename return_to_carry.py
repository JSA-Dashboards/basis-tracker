"""return_to_carry.py — JSA's "Return to Carry" (return to storage), automated, for corn and soybeans.

Buy grain at harvest, store it, sell it later: what has the market paid to carry the grain, after the
interest on the money tied up in it? This module rebuilds, from the weekly basis series and the futures
settlements, what the Research Analyst's yearly workbooks (JSA - Documents/Research Analyst/Misc/Return
to Carry: corn `NNcolcry.xlsx`, `NNcolcarryBA.xlsx`; soybeans `BeanCarry/*/NNdeccry.xlsx` ...) and the weekly
"Corn Return to Carry" PDF report compute by hand. The formulas below are theirs (checked cell for cell against
the workbooks — see tests/test_return_to_carry.py and tests/test_return_to_carry_soy.py):

  Harvest purchase   b0 = the AVERAGE of the first weekly cash bids of the crop year — the first Wednesday of
                     October through mid-November ("Oct / F-H Nov") — expressed against ONE futures contract:
                     corn Dec (every harvest bid is quoted off Dec); soybeans Jan (the October bids are quoted
                     off Nov and are moved to Jan by the Nov-Jan spread measured the last Wednesday of October).
  Weekly return      R(t) = basis(t) − b0 + S(t) − interest(t)
      basis(t)       that week's bid for the nearby shipment, vs the contract it is quoted off (corn: Dec through
                     Nov, Mar Dec-Feb, May Mar-Apr, Jul May-Jun, Sep Jul-Aug; soybeans: Nov in Oct, Jan Nov-Dec, Mar
                     Jan-Feb, May Mar-Apr, Jul May-Jun, Aug in Jul, next Nov in Aug-Sep).
      S(t)           the futures carry banked by rolling the hedge along the chain: each roll spread is measured the
                     LAST WEDNESDAY BEFORE the month the next contract takes over (corn Dec, Mar, May, Jul 1;
                     soybeans Nov, Jan, Mar, May, Jul, Aug 1) and applies from the week the bid moves to the new
                     contract. ("Futures spreads are based on the actual spread the last week prior to the expiring
                     month ... carried forward through the crop year.")
      interest(t)    cash price × (running sum of the weekly rate, in percent) / 5200 — one week of rate/52 per
                     week since the purchase, charged from the 3rd weekly bid for corn (~Oct 20) and the 5th for
                     soybeans (~Nov 1), on the cash price (futures + basis). The rate is whatever `rate_on`
                     returns: the bank PRIME in the analyst's sheets; in the app, by default, the Net Carry tab's
                     own rate (effective fed funds + 2.25%).
  Gross return       the same without the interest term (what the market paid before the cost of money).
  Best return        the highest weekly R of the crop year, and the week it happened.

Everything here is a pure function of plain lists/dicts — no database, no Streamlit — so it can be
unit-tested; return_to_carry_data.py turns the app's tables into those inputs. `Spec` holds everything that
differs between the grains (CORN, SOY); every function defaults to corn.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

# ── the commodity specs ───────────────────────────────────────────────────────────────────────
CHAIN = ("Z", "H", "K", "N", "U")            # corn: Dec, Mar, May, Jul, Sep
LETTER_MONTH = {"Z": 12, "H": 3, "K": 5, "N": 7, "U": 9}
PAIRS = (("Z", "H"), ("H", "K"), ("K", "N"), ("N", "U"))      # the four corn roll spreads
PAIR_KEY = {("Z", "H"): "ZH", ("H", "K"): "HK", ("K", "N"): "KN", ("N", "U"): "NU"}
WINDOW_WEEKS = 7                              # the Oct / F-H Nov harvest-basis average
ACCRUAL_START = 2                             # corn interest starts at the 3rd weekly bid (index 2)
ROOT = "ZC"

# Where a yearly CORN workbook departs from the rule above. Read from `NNcolcry.xls[x]` (every Columbus sheet,
# 1996-97 through 2025-26): the harvest-basis window the analyst used (found by reproducing the sheet's own
# "Oct/FH Nov" cell) and the day a roll spread was measured (the Wednesday row holding both contracts). Keeping
# them means the tracker ties to the published numbers for those years.
WINDOW_OVERRIDES = {                          # crop year -> (first week index, number of weeks)
    1998: (0, 6), 1999: (0, 6), 2000: (0, 6), 2001: (0, 6),
    2009: (2, 6),                             # "6 wks starting 10-21-09"
    2010: (0, 6), 2012: (-4, 9), 2013: (1, 6), 2015: (0, 6),   # 2012-13's sheet began in September: its 9 weeks run Sep 5 - Oct 31
    2019: (2, 7),                             # Oct 16 - Nov 27: the 39.29 in the 6-17-20 report
}
ROLL_OVERRIDES = {1998: {"KN": date(1999, 5, 5)},
                  1999: {"ZH": date(1999, 12, 1), "HK": date(2000, 3, 1)},
                  2002: {"NU": date(2003, 7, 2)},
                  2020: {"KN": date(2021, 4, 21)},       # 2020-21: the May/Jul squeeze — measured a week early
                  2024: {"NU": date(2025, 7, 2)},        # 2024-25 and 2025-26 measured Jul/Sep the first Wednesday of July
                  2025: {"NU": date(2026, 7, 1)}}

# Where a yearly SOYBEAN workbook (`BeanCarry/{Decatur,DesMoines,Hennepin,STL}CRY/NN*.xls[x]`) departs from the rule. Found by
# fitting every sheet from 2005-06 to 2022-23 (the same four families agree in every year): the week the interest sum starts
# (the 3rd row through 2008-09, the 5th from 2009-10), the harvest-average weeks (2015-16 to 2017-18 skipped the fifth week,
# which is why those years' published harvest basis differs) and the few roll spreads measured a week off the rule.
SOY_WINDOW_OVERRIDES = {y: (0, 1, 2, 3, 5, 6) for y in (2015, 2016, 2017)}
SOY_ROLL_OVERRIDES = {2010: {"XF": date(2010, 11, 3)},            # Nov/Jan measured the first Wednesday of November (2010, 2011)
                      2011: {"XF": date(2011, 11, 2)},
                      2013: {"FH": date(2013, 12, 26)},           # the sheet's row for the Jan/Mar measurement was dated Dec 26
                      2016: {"HK": date(2017, 3, 1)},             # Mar/May measured the first Wednesday of March
                      2019: {"FH": date(2019, 12, 30)}}           # the 2019-20 sheets' Dec 30 row (a Monday: its dates drift after Dec 18)
SOY_ACCRUAL_OVERRIDES = {2005: 2, 2006: 2, 2007: 2, 2008: 2}

LETTER_NAME = {"Z": "Dec", "H": "Mar", "K": "May", "N": "Jul", "U": "Sep", "X": "Nov", "F": "Jan", "Q": "Aug", "x": "Nov"}


@dataclass(frozen=True, eq=False)
class Spec:
    """Everything that differs between the grains. A chain label is the CME month letter, except soybeans' NEXT
    crop's November, which is the lower-case 'x' (the same letter as the harvest November 'X', a year later)."""
    key: str
    grain: str                    # the app's name for the grain: 'Corn', 'Soybeans'
    root: str                     # ZC, ZS
    chain: tuple                  # the contracts the hedge rolls through, in time order
    base: str                     # the contract the harvest basis b0 is expressed in
    roll_months: tuple            # per consecutive pair: the calendar month whose 1st starts quoting off the later contract
    accrual_start: int            # week index of the first week that earns interest (0 = first Wednesday of October)
    accrual_overrides: dict       # crop year -> accrual start where a yearly workbook started the interest sum elsewhere
    horizon: tuple                # (month, day) of the following year the weekly rows run to
    complete_from: tuple          # (month, day) of the following year: a year whose last week is on/after it has run its course
    summer: str                   # the contract whose basis is the report's "best summer basis"
    carry_from: str               # the headline futures carry runs from this contract ...
    carry_to: str                 # ... to this one
    ship_months: tuple            # the page-1 table's columns
    ship_letter: dict             # shipment month -> the contract its bid is quoted in
    window: tuple                 # the harvest-basis average's week indexes
    window_overrides: dict        # crop year -> tuple of week indexes
    roll_overrides: dict          # crop year -> {pair key: date}
    normalize_tags: bool          # move a bid whose tag is not the date's contract to it by that day's spread
    min_crop_year: int            # the first crop year the weekly archive supports
    harvest_name: str             # the sheets' name for the average ("Oct / F-H Nov")

    @property
    def pairs(self) -> tuple:
        return tuple(zip(self.chain, self.chain[1:]))

    def pair_key(self, a: str, b: str) -> str:
        return a + b

    @property
    def carry_pairs(self) -> tuple:
        """The pair keys the headline carry adds up (carry_from -> carry_to)."""
        i, j = self.chain.index(self.carry_from), self.chain.index(self.carry_to)
        return tuple(self.pair_key(a, b) for a, b in self.pairs[i:j])


CORN = Spec(
    key="corn", grain="Corn", root="ZC", chain=CHAIN, base="Z", roll_months=(12, 3, 5, 7), accrual_start=ACCRUAL_START,
    accrual_overrides={},
    horizon=(7, 31), complete_from=(7, 24), summer="N", carry_from="Z", carry_to="N",
    ship_months=(11, 12, 1, 2, 3, 4, 5, 6, 7), ship_letter={11: "Z", 12: "H", 1: "H", 2: "H", 3: "K", 4: "K", 5: "N", 6: "N", 7: "N"},
    window=tuple(range(WINDOW_WEEKS)), window_overrides={y: tuple(range(s, s + n)) for y, (s, n) in WINDOW_OVERRIDES.items()},
    roll_overrides=ROLL_OVERRIDES, normalize_tags=False, min_crop_year=2004, harvest_name="Oct / F-H Nov")

SOY = Spec(
    key="soy", grain="Soybeans", root="ZS", chain=("X", "F", "H", "K", "N", "Q", "x"), base="F",
    roll_months=(11, 1, 3, 5, 7, 8), accrual_start=4, accrual_overrides=SOY_ACCRUAL_OVERRIDES, horizon=(9, 30),
    complete_from=(9, 17), summer="N",
    carry_from="F", carry_to="N",
    ship_months=(11, 12, 1, 2, 3, 4, 5, 6, 7, 8), ship_letter={11: "F", 12: "F", 1: "H", 2: "H", 3: "K", 4: "K", 5: "N", 6: "N", 7: "Q", 8: "x"},
    window=tuple(range(7)), window_overrides=SOY_WINDOW_OVERRIDES, roll_overrides=SOY_ROLL_OVERRIDES,
    normalize_tags=True, min_crop_year=2005, harvest_name="Oct / F-H Nov")

SPECS = {CORN.grain: CORN, SOY.grain: SOY}


# ── calendar helpers ──────────────────────────────────────────────────────────────────────────
def crop_year_of(d: date) -> int:
    """The crop (marketing) year a date belongs to — it starts Sep 1 — as its starting year."""
    return d.year if d.month >= 9 else d.year - 1


def crop_label(crop_year: int) -> str:
    return f"{crop_year}-{(crop_year + 1) % 100:02d}"


def contract_symbol(letter: str, crop_year: int, root: str = ROOT) -> str:
    """ZCZ25 for ('Z', 2025); ZCH26 for ('H', 2025); ZSX25 for ('X', 2025) and ZSX26 for ('x', 2025): the harvest Dec and
    Nov are the crop year's own, the rest fall in the next calendar year."""
    year = crop_year if letter in ("Z", "X") else crop_year + 1
    return f"{root}{letter.upper()}{year % 100:02d}"


def letter_of(symbol: str | None) -> str | None:
    """'ZCH26' -> 'H' (None when it isn't one of the corn chain's months)."""
    if not symbol or len(symbol) < 4:
        return None
    ch = symbol[2].upper()
    return ch if ch in CHAIN else None


def tag_label(tag: str | None, d: date, crop_year: int, spec: Spec = CORN) -> str | None:
    """The chain label a bid's futures tag names — a label ('H', 'x') as is, a symbol ('ZSH26') by its month letter — or None.
    The year digits of a symbol are not trusted (the archive writes a soybean January as ZSF19 for Jan 2020); only soybeans'
    November needs a year: the harvest one (X) up to Dec 31 of the crop year, the next crop's (x) after it."""
    t = (tag or "").strip()
    if not t:
        return None
    if t in spec.chain:
        return t
    if len(t) < 4:
        return None
    ch = t[2].upper()
    if ch == "X" and "x" in spec.chain:
        return "X" if d <= date(crop_year, 12, 31) else "x"
    return ch if ch in spec.chain else None


def last_wednesday_before(year: int, month: int) -> date:
    """The last Wednesday strictly before the 1st of `month` (the roll-spread day)."""
    d = date(year, month, 1) - timedelta(days=1)
    while d.weekday() != 2:
        d -= timedelta(days=1)
    return d


def roll_dates(crop_year: int, spec: Spec = CORN) -> dict:
    """{pair key: the last Wednesday before the 1st of the month the later contract takes over} — corn 'ZH' Dec 1, 'HK' Mar 1,
    'KN' May 1, 'NU' Jul 1; soybeans 'XF' Nov 1, 'FH' Jan 1, 'HK' Mar 1, 'KN' May 1, 'NQ' Jul 1, 'Qx' Aug 1 — except the few days a
    workbook measured differently (the spec's roll_overrides)."""
    out = {}
    for (a, b), m in zip(spec.pairs, spec.roll_months):
        out[spec.pair_key(a, b)] = last_wednesday_before(crop_year if m >= 9 else crop_year + 1, m)
    out.update(spec.roll_overrides.get(crop_year, {}))
    return out


def infer_tag(d: date, spec: Spec = CORN, crop_year: int | None = None) -> str:
    """The contract a nearby bid is quoted off, when the posting doesn't say.
    Corn moves to Mar the first week of December, May in March, Jul in the last week of April, Sep in July — the pattern in
    every year's weekly sheet (2025-26: Dec 3, Mar 4, Apr 29, Jul 1). Soybeans move to Jan the first week of November, Mar in
    January, May in March, Jul in May, Aug in July and the next Nov in August (`crop_year` says which year's September it is)."""
    if spec.key == "soy":
        cy = crop_year if crop_year is not None else crop_year_of(d)
        for first_day, label in ((date(cy, 11, 1), "X"), (date(cy + 1, 1, 1), "F"), (date(cy + 1, 3, 1), "H"),
                                 (date(cy + 1, 5, 1), "K"), (date(cy + 1, 7, 1), "N"), (date(cy + 1, 8, 1), "Q")):
            if d < first_day:
                return label
        return "x"
    m, day = d.month, d.day
    if m in (9, 10, 11):                                           # September bids belong to the NEW crop (Dec)
        return "Z"
    if m in (12, 1, 2):
        return "H"
    if m == 3 or (m == 4 and day < 25):
        return "K"
    if m in (4, 5, 6):                                             # Apr 25 - Jun 30
        return "N"
    return "U"                                                     # Jul, Aug


def first_wednesday(crop_year: int) -> date:
    """The first Wednesday on or after Oct 1 — the first row of every yearly sheet."""
    d = date(crop_year, 10, 1)
    while d.weekday() != 2:
        d += timedelta(days=1)
    return d


def week_index(d: date, crop_year: int) -> int:
    """Which weekly row a date belongs to: 0 = the first Wednesday of October. A bid posted a day or two
    either side of its Wednesday (a holiday week) still counts for that week."""
    return round((d - first_wednesday(crop_year)).days / 7)


def basis_cost(b0: float, f_base: float, rate_pct: float, purchase: date, ship: date, spread_cum: float) -> float:
    """The break-even basis for a shipment on `ship` — the "Basis Cost" row of the report's table: what the harvest
    purchase has to be bid back to for the storage to wash.

        basis_cost = b0 - spread_cum + (f_base + b0) * rate * days / 360

    b0          the harvest basis (cents vs the base contract: corn Dec, soybeans Jan)
    spread_cum  the futures carry banked rolling to the shipment's contract (corn Mar - Dec, May - Dec, ...); 0 for the base
    f_base      the base-equivalent futures price the interest is charged on (with b0, the cash price)
    The return on a bid is that bid's basis (vs the same contract) minus this."""
    return b0 - spread_cum + (f_base + b0) * (rate_pct / 100.0) * (ship - purchase).days / 360.0


# ── futures access ────────────────────────────────────────────────────────────────────────────
def price_on(futs: dict, d: date, symbol: str, max_back: int = 5):
    """(price, date used) of `symbol` on `d`, stepping back over weekends/holidays/gaps; (None, None) if absent.
    futs = {date: {symbol: cents}}."""
    for k in range(max_back + 1):
        dd = d - timedelta(days=k)
        px = futs.get(dd, {}).get(symbol)
        if px is not None:
            return px, dd
    return None, None


def roll_spreads(futs: dict, crop_year: int, spec: Spec = CORN) -> dict:
    """The roll spreads of a crop year, measured on their Wednesdays (`roll_dates`):
    {pair key: (later - earlier contract, date) | None}; None where a contract has no price that day."""
    rolls = roll_dates(crop_year, spec)
    out = {}
    for a, b in spec.pairs:
        key = spec.pair_key(a, b)
        rd = rolls[key]
        pa, _ = price_on(futs, rd, contract_symbol(a, crop_year, spec.root))
        pb, _ = price_on(futs, rd, contract_symbol(b, crop_year, spec.root))
        out[key] = (round(pb - pa, 6), rd) if pa is not None and pb is not None else None
    return out


# ── the crop year ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Week:
    date: date
    tag: str                      # contract label the bid is quoted off: Z H K N U (corn); X F H K N Q x (soybeans)
    basis: float                  # cents
    fut: float | None             # that contract's settlement
    cash: float | None            # fut + basis
    prime: float | None           # annual percent in force (the rate the interest runs on)
    interest: float | None        # cumulative interest to date, cents (None before the purchase accrues)
    carry: float | None           # cumulative futures roll spreads banked so far, cents
    gross: float | None           # basis - b0 + carry
    net: float | None             # gross - interest
    idx: int = 0


@dataclass
class CropYear:
    crop_year: int
    label: str
    weeks: list = field(default_factory=list)
    b0: float | None = None                  # the harvest basis every return is measured from (cents vs the base contract): the Oct / F-H Nov
                                             # average, or the user's own when build_crop_year(b0_override=...) was given one
    b0_calc: float | None = None             # that average, whichever one `b0` is
    b0_own: bool = False                     # `b0` is the user's own harvest basis, not the calculated average
    b0_dates: tuple | None = None            # (first, last) week of the average
    b0_weeks: int = 0                        # how many weeks the average is built from (7 once complete)
    spreads: dict = field(default_factory=dict)   # {pair key: (cents, date)|None, ...}
    season_carry: float | None = None        # the report's headline futures carry (corn Dec -> Jul, soybeans Jan -> Jul)
    best: dict = field(default_factory=dict)  # {'net': Week, 'gross': Week}
    summer: Week | None = None               # best basis quoted vs Jul futures (the report's blue triangle)
    complete: bool = False                   # the last week is in the closing months of the crop year
    spec: Spec = CORN

    @property
    def dec_jul_carry(self) -> float | None:
        """Corn's name for the headline carry (kept for the existing callers)."""
        return self.season_carry


def build_crop_year(obs: list[dict], futs: dict, crop_year: int, rate_on, spec: Spec = CORN,
                    window_start: int = 0, b0_override: float | None = None) -> CropYear:
    """Run the weekly return for one crop year.

    obs      [{'date', 'basis', 'tag'?}] — the nearby bid, about one per week (any order, any years); `tag`
             is the futures contract label or symbol it is quoted off (inferred from the date when absent).
    futs     {date: {symbol: cents}} settlements (ZCZ25, ZCH26, ... / ZSX25, ZSF26, ...); `price_on` steps over gaps.
    rate_on  date -> annual interest rate in percent (the bank prime in the report; the app's default is the tab's
             fed funds + 2.25%).
    spec     CORN or SOY.
    window_start  week index of the first week of the harvest-basis average (0 normally; the 2019-20 corn
             sheet started it two weeks late, which is why that year's published harvest basis is 39.29).
    b0_override  the user's OWN harvest basis (cents vs the spec's base contract), used in place of the calculated average: every weekly
             return is measured from it (`cy.b0`, `cy.b0_own`); the calculated average is kept in `cy.b0_calc`. None = the calculated one.
             The interest does not depend on it (it runs on the week's cash price), so an override shifts every gross and net return, and
             the best return, by the same amount (calculated - own) and leaves the best WEEK where it was.

    Like the sheets, time runs on a WEEKLY GRID from the first Wednesday of October: interest accrues for
    every week from the spec's accrual start on, whether or not a bid was posted that week (a week with no bid just
    has no return)."""
    cy = CropYear(crop_year, crop_label(crop_year), spec=spec)
    lo, hi = date(crop_year, 9, 1), date(crop_year + 1, *spec.horizon)       # (Sep only feeds a harvest window)
    start = first_wednesday(crop_year)
    window = spec.window_overrides.get(crop_year, spec.window)
    if window_start:
        window = tuple(range(window_start, window_start + len(window)))
    lowest = window[0]
    accr = spec.accrual_overrides.get(crop_year, spec.accrual_start)
    n_idx = {lab: i for i, lab in enumerate(spec.chain)}
    byweek: dict = {}
    for o in sorted((o for o in obs if lo <= o["date"] <= hi and o.get("basis") is not None), key=lambda o: o["date"]):
        k = week_index(o["date"], crop_year)
        if k >= lowest:                                    # a September bid only matters to a window that reaches back to it
            byweek[k] = o                                  # the later bid of a week wins
    if not any(k >= 0 for k in byweek):
        return cy
    last_k = max(byweek)

    def resolve(o, k: int):
        """(contract label, basis) of a bid: its own tag, else the date's contract. Soybeans move a bid quoted off another
        contract to the date's by that day's spread, as the sheets' columns do (a November bid quoted vs Nov is a Jan bid)."""
        basis = float(o["basis"])
        sched = infer_tag(o["date"], spec, crop_year)
        t = tag_label(o.get("tag"), o["date"], crop_year, spec)
        if t is None:
            return sched, basis
        if spec.normalize_tags and t != sched:
            wd = start + timedelta(days=7 * k)
            pa, _ = price_on(futs, wd, contract_symbol(t, crop_year, spec.root))
            pb, _ = price_on(futs, wd, contract_symbol(sched, crop_year, spec.root))
            if pa is not None and pb is not None:
                return sched, basis + pa - pb
        return t, basis

    # the roll spreads, measured on their Wednesdays
    cy.spreads = roll_spreads(futs, crop_year, spec)
    cp = spec.carry_pairs
    if all(cy.spreads.get(k) for k in cp):
        cy.season_carry = sum(cy.spreads[k][0] for k in cp)

    def cum_carry(label: str):
        """Futures carry banked by the time a bid is quoted off `label`, counted from the base contract (negative for a
        contract before it: soybeans' Nov is the Nov-Jan spread below Jan); None if a spread is missing."""
        bi, li = n_idx[spec.base], n_idx[label]
        tot = 0.0
        if li >= bi:
            for a, b in spec.pairs[bi:li]:
                sp = cy.spreads.get(spec.pair_key(a, b))
                if sp is None:
                    return None
                tot += sp[0]
        else:
            for a, b in spec.pairs[li:bi]:
                sp = cy.spreads.get(spec.pair_key(a, b))
                if sp is None:
                    return None
                tot -= sp[0]
        return tot

    # the harvest basis: mean of the bids in the window weeks, all expressed against the base contract
    vals, got = [], []
    for k in window:
        o = byweek.get(k)
        if o is None:
            continue
        t, v = resolve(o, k)
        if t != spec.base:                              # quoted off another month: re-base to the base contract
            c = cum_carry(t)
            if c is None:
                continue
            v += c
        vals.append(v)
        got.append(o["date"])
    if vals:
        cy.b0 = sum(vals) / len(vals)
        cy.b0_weeks = len(vals)
        cy.b0_dates = (got[0], got[-1])                   # the bids' own dates
    cy.b0_calc = cy.b0
    if b0_override is not None:                           # the user's own harvest basis replaces the calculated average
        cy.b0, cy.b0_own = float(b0_override), True

    run = 0.0                                           # running sum of the weekly rate, percent
    for k in range(0, last_k + 1):
        wd = start + timedelta(days=7 * k)
        o = byweek.get(k)
        prime = rate_on(wd) if rate_on else None
        accruing = k >= accr and prime is not None
        if accruing:
            run += prime                                # every week counts, bid or no bid
        if o is None:
            continue
        tag, basis = resolve(o, k)
        f, _ = price_on(futs, wd, contract_symbol(tag, crop_year, spec.root))     # that week's Wednesday settlement
        cash = f + basis if f is not None else None
        carry = cum_carry(tag)
        interest = gross = net = None
        if accruing and cash is not None:
            interest = cash * run / 5200.0
        if cy.b0 is not None and carry is not None and k >= accr:
            gross = basis - cy.b0 + carry
            net = None if interest is None else gross - interest
        cy.weeks.append(Week(wd, tag, basis, f, cash, prime, interest, carry, gross, net, k))

    for kind in ("net", "gross"):
        have = [w for w in cy.weeks if getattr(w, kind) is not None]
        if have:
            cy.best[kind] = max(have, key=lambda w: (getattr(w, kind), -w.idx))     # earliest week wins a tie
    sm = [w for w in cy.weeks if w.tag == spec.summer]
    if sm:
        cy.summer = max(sm, key=lambda w: (w.basis, -w.idx))
    cy.complete = cy.weeks[-1].date >= date(crop_year + 1, *spec.complete_from) if cy.weeks else False
    return cy


# ══ the report's page 1: what each shipment month would return ═══════════════════════════════════
# The table at the front of the weekly report (`NNcolcarryBA.xlsx`): bought at the harvest basis on Oct 20, what does the
# grain have to be bid back to for each shipment month (the 20th of Nov .. Jul) to cover the interest and the futures
# carry — and what are the forward bids actually paying against that?
#
#   Basis Cost(M)   b0 - (F_M - F_base) + (F_base + b0) * rate * days / 360        (days = Oct 20 -> the shipment date)
#   Current Basis   the latest bid for shipment in M, in the terms of M's futures month (corn Dec, Mar, Mar, Mar, May, May,
#                   Jul, Jul, Jul for Nov .. Jul)
#   Return          Current Basis - Basis Cost
#   Best YTD        the best bid seen since the purchase, and the best return (the bid less THAT day's Basis Cost)
#
# "Current Futures" (F_M) is the report's chain: the contracts still trading at today's settlement, and — once a roll
# spread has been measured (the last Wednesday before the expiring month) — the expired ones hung from the next
# contract by that frozen spread ("Futures spreads are based on the actual spread the last week prior to the expiring
# month. At this time, the spread on that last week is carried forward ..."). Gross = the same without the interest.
# The report exists for corn; the soybean table applies the same arithmetic to the soybean chain (Nov .. Aug columns).
SHIP_MONTHS = CORN.ship_months
SHIP_LETTER = CORN.ship_letter
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
PURCHASE_MONTH, PURCHASE_DAY = 10, 20


def purchase_date(crop_year: int) -> date:
    """The report's Purchase Date: Oct 20 (the third weekly bid, where the interest starts)."""
    return date(crop_year, PURCHASE_MONTH, PURCHASE_DAY)


def ship_date(crop_year: int, month: int) -> date:
    """The shipment dates of the table's columns: the 20th of each month."""
    return date(crop_year if month >= PURCHASE_MONTH else crop_year + 1, month, 20)


def shipment_crop_year(asof: date) -> int:
    """The crop year whose page-1 table is the live one on `asof`. August belongs to the NEW crop (the old one's July
    is over and the next one's table is being set up — the analyst builds it in August)."""
    return asof.year if asof.month == 8 else crop_year_of(asof)


@dataclass
class Levels:
    """The report's "Current Futures" chain on one day."""
    asof: date
    anchor: str                   # the contract the expired ones hang from (corn: Dec until Z/H is measured, then Mar ...)
    levels: dict                  # {label: cents|None}
    spreads: dict                 # {pair key: (carry cents, date, frozen)|None}: the roll spreads, measured or current


def chain_levels(futs: dict, crop_year: int, t: date, spec: Spec = CORN) -> Levels:
    """The futures level of every contract in the chain on day `t`: live contracts at their settlement, rolled ones
    (those whose spread was measured on its Wednesday, `roll_dates`) hung from the next contract by that frozen spread.
    Reproduces the report: 6-17-20 -> Dec 307.75 / Mar 318.25 / May 322.25 / Jul 330.25 off the Jul price and the
    measured spreads (10.5, 4, 8)."""
    chain = spec.chain
    rolls = roll_dates(crop_year, spec)
    frozen = roll_spreads(futs, crop_year, spec)
    n = 0                                                    # how many roll spreads are measured by `t`
    for a, b in spec.pairs:
        key = spec.pair_key(a, b)
        if rolls[key] <= t and frozen.get(key) is not None:
            n += 1
        else:
            break
    levels = {L: None for L in chain}
    for i in range(n, len(chain)):
        levels[chain[i]], _ = price_on(futs, t, contract_symbol(chain[i], crop_year, spec.root))
    anchor = chain[n]
    if levels[anchor] is not None:
        lvl = levels[anchor]
        for i in range(n - 1, -1, -1):
            lvl -= frozen[spec.pair_key(chain[i], chain[i + 1])][0]
            levels[chain[i]] = lvl
    spreads = {}
    for i, (a, b) in enumerate(spec.pairs):
        key = spec.pair_key(a, b)
        if i < n:
            spreads[key] = (frozen[key][0], rolls[key], True)
        elif levels[a] is not None and levels[b] is not None:
            spreads[key] = (levels[b] - levels[a], t, False)
        else:
            spreads[key] = None
    return Levels(t, anchor, levels, spreads)


def tag_symbol(tag: str | None, crop_year: int, spec: Spec = CORN, d: date | None = None) -> str | None:
    """The contract a quote's futures tag names. Corn: a full symbol ('ZCH27') as is, a chain letter ('H') as this crop year's
    contract. Soybeans: by the month letter alone (the archive's year digits are not reliable — see tag_label) as this crop
    year's contract (`d` says which November). None for 'R' (a package that spans months — quoted off each month's own
    contract), blanks and anything else."""
    t = (tag or "").strip()
    if spec.normalize_tags:
        lab = tag_label(t, d or date(crop_year, 10, 1), crop_year, spec)
        return contract_symbol(lab, crop_year, spec.root) if lab else None
    t = t.upper()
    if len(t) == 5 and t.startswith(spec.root) and t[2] in "FGHJKMNQUVXZ" and t[3:].isdigit():
        return t
    if len(t) == 1 and t in spec.chain:
        return contract_symbol(t, crop_year, spec.root)
    return None


def rebase(basis: float, tag: str | None, col_letter: str, crop_year: int, futs: dict, t: date, spec: Spec = CORN):
    """A bid in the terms of the table's column: (value, converted) — the bid is moved from the contract it was quoted off
    to the column's by that day's spread, `basis + F(quoted) - F(column)` (nothing to move when they are the same, or
    the quote names no contract). None when the move needs a price that is missing."""
    col_sym = contract_symbol(col_letter, crop_year, spec.root)
    sym = tag_symbol(tag, crop_year, spec, t)
    if sym is None or sym == col_sym:
        return float(basis), False
    pq, _ = price_on(futs, t, sym)
    pc, _ = price_on(futs, t, col_sym)
    if pq is None or pc is None:
        return None
    return float(basis) + pq - pc, True


def ship_cost(measure: str, b0, level_base, rate_pct, purchase: date, ship: date, spread_cum):
    """The break-even basis of one shipment month on the measure: net (with the interest) or gross (the carry only)."""
    if b0 is None or spread_cum is None:
        return None
    if measure == "gross":
        return b0 - spread_cum
    if level_base is None or rate_pct is None:
        return None
    return basis_cost(b0, level_base, rate_pct, purchase, ship, spread_cum)


@dataclass
class ShipCol:
    month: int
    ship: date
    letter: str                         # the futures month the column is quoted in
    level: float | None = None          # "Current Futures"
    cost: float | None = None           # "Basis Cost": the break-even basis today
    bid: float | None = None            # "Current Basis": the latest bid for this month, in `letter` terms
    bid_date: date | None = None
    bid_label: str | None = None        # how it was posted ("JFM")
    bid_posted: float | None = None     # the number as posted, when it had to be moved to `letter`
    ret: float | None = None            # "Current Basis Return": bid - cost
    best_bid: float | None = None       # "Best Basis YTD" and its date
    best_bid_date: date | None = None
    best_ret: float | None = None       # "Best Basis Return YTD": the bid less THAT day's break-even, and its date
    best_ret_date: date | None = None


@dataclass
class ShipTable:
    crop_year: int
    label: str
    asof: date
    measure: str
    purchase: date
    b0: float | None
    b0_weeks: int
    b0_est: bool
    f_base: float | None                # base-equivalent futures the interest is charged on (with b0, the cash price)
    rate: float | None                  # annual percent (the report: prime)
    levels: Levels | None
    cols: list = field(default_factory=list)
    spec: Spec = CORN
    b0_own: bool = False                # `b0` is the user's own harvest basis, not the calculated average / estimate
    b0_calc: float | None = None        # what the calculated method gives (the average so far, or the estimate before the weekly bids)

    @property
    def f_dec(self) -> float | None:
        """Corn's name for the base-contract level (kept for the existing callers)."""
        return self.f_base


def build_shipment_table(crop_year: int, asof: date, b0, quotes: list[dict], futs: dict, rate_on, measure: str = "net",
                         b0_weeks: int = 0, b0_est: bool = False, spec: Spec = CORN, max_age_days: int = 10,
                         b0_own: bool = False, b0_calc: float | None = None) -> ShipTable:
    """The page-1 table on `asof`.

    b0       the harvest basis (cents vs the base contract): the average of the first weekly bids so far, or an estimate before they post —
             or the user's own (then `b0_own` is True and `b0_calc` is what the calculated method gives, to show beside it).
    quotes   one bid per (posting date, shipment month): [{'date', 'month', 'basis', 'tag', 'label'}] — the data layer
             (return_to_carry_data.shipment_quotes) picks them from a corridor's forward periods.
    rate_on  date -> annual percent (the bank prime in the report; the app's default is the tab's fed funds + 2.25%).
    A bid counts as current when it is at most `max_age_days` old. Best-YTD starts at the purchase date (Oct 20)."""
    purchase = purchase_date(crop_year)
    lv = chain_levels(futs, crop_year, asof, spec)
    rate_now = rate_on(asof) if rate_on else None
    tbl = ShipTable(crop_year, crop_label(crop_year), asof, measure, purchase, b0, b0_weeks, b0_est,
                    lv.levels.get(spec.base), rate_now, lv, [], spec, b0_own, b0_calc)
    cols = {m: ShipCol(m, ship_date(crop_year, m), spec.ship_letter[m]) for m in spec.ship_months}
    tbl.cols = [cols[m] for m in spec.ship_months]

    def cost_on(lvl: Levels, rate, col: ShipCol):
        lb, lc = lvl.levels.get(spec.base), lvl.levels.get(col.letter)
        sc = None if lb is None or lc is None else lc - lb
        return ship_cost(measure, b0, lb, rate, purchase, col.ship, sc)

    for col in tbl.cols:                                    # the break-even today
        col.level = lv.levels.get(col.letter)
        col.cost = cost_on(lv, rate_now, col)

    by_date: dict = {}
    for q in quotes:
        if q["date"] <= asof and q["month"] in cols:
            by_date.setdefault(q["date"], []).append(q)
    for t in sorted(by_date):
        lvl_t = lv if t == asof else chain_levels(futs, crop_year, t, spec)
        rate_t = rate_now if t == asof else (rate_on(t) if rate_on else None)
        for q in by_date[t]:
            col = cols[q["month"]]
            moved = rebase(q["basis"], q.get("tag"), col.letter, crop_year, futs, t, spec)
            if moved is None:
                continue
            bid, converted = moved
            if (asof - t).days <= max_age_days and (col.bid_date is None or t >= col.bid_date):
                col.bid, col.bid_date, col.bid_label = bid, t, q.get("label")
                col.bid_posted = float(q["basis"]) if converted else None
            if t < purchase:
                continue                                    # the position starts at the purchase
            if col.best_bid is None or bid > col.best_bid + 1e-9:
                col.best_bid, col.best_bid_date = bid, t
            c = cost_on(lvl_t, rate_t, col)
            if c is not None:
                r = bid - c
                if col.best_ret is None or r > col.best_ret + 1e-9:
                    col.best_ret, col.best_ret_date = r, t
    for col in tbl.cols:
        if col.bid is not None and col.cost is not None:
            col.ret = col.bid - col.cost
            if col.bid_date is not None and col.bid_date >= purchase and (col.best_ret is None or col.ret > col.best_ret + 1e-9):
                col.best_ret, col.best_ret_date = col.ret, col.bid_date     # today's break-even can differ from the quote day's
    return tbl


def best_column(tbl: ShipTable, field_name: str = "ret"):
    """The column with the highest `ret` (today) or `best_ret` (year to date); the earliest month wins a tie. None if no column has one."""
    have = [c for c in tbl.cols if getattr(c, field_name) is not None]
    return max(have, key=lambda c: getattr(c, field_name)) if have else None
