"""return_to_carry_block.py — the Return to Carry section of a Net Carry tab, as one Streamlit block.

Shared by the basis tracker and the River / Rail FOB portals (each keeps its own copy of this file next to the return_to_carry modules —
see sync_carry_modules.py): the caller loads the location's weekly nearby bids (`obs`) and every posted forward period (`quotes`) and
says how to load the futures, prime and fed funds histories; this block does the rest — the Interest switch, the report's
shipment-by-month table, the headline numbers, the season chart, the best-return-by-year bars, the by-year table and the explanations.

    render(obs=..., quotes=..., asof=date, grain='Corn', measure='net' | 'gross', tab_rate_pct=6.13,
           load_futures=lambda root: {date: {symbol: cents}}, load_prime=..., load_fed_funds=..., logo_uri=None, note=None)

The Interest radio keeps the key `nc_rtc_rate2` (a page has one Return to Carry block at a time). The Harvest basis switch (Calculated, or the
user's own number) keys its widgets `nc_rtc_b0_{mode,val,all}_<scope>|<grain>|<crop year>`, so what was typed for one location never follows
the user to another; pass `scope=` (the location's identity on the page) to every call. The Ship by box (no limit by default, or the end of a month
from November on: the study stops there) keys `nc_rtc_ship_by_<corn|soy>`: the user's constraint, so it follows them from one location to the next.
"""
from __future__ import annotations

from datetime import timedelta

import streamlit as st

import carry_rate as cr
import return_to_carry as rtc
import return_to_carry_data as rd
import return_to_carry_view as vw

def _stretch() -> dict:
    """width='stretch' on a Streamlit that has it, use_container_width=True on the older ones (the keyword was deprecated)."""
    import inspect
    return {"width": "stretch"} if "width" in inspect.signature(st.altair_chart).parameters else {"use_container_width": True}


HEADING = ('<div style="margin-top:28px;margin-bottom:2px;font-size:10px;color:#64748b;font-weight:700;'
           'text-transform:uppercase;letter-spacing:.1em">Return to carry — what storing from harvest has paid</div>')


SECTION = ('<div style="margin-top:10px;margin-bottom:-6px;font-size:11px;color:#1f4e79;font-weight:700;text-transform:uppercase;'
           'letter-spacing:.06em">{}</div>')


def _harvest_basis_choice(spec, key: str, calc: float | None, label: str):
    """This year's harvest basis, under the Interest switch: the calculated average (the report's method, over the harvest window below) by
    default, or the user's own number — which measures ONLY the crop year being tracked (Kolten 2026-10-19: "just change the current year's
    calculations ... it's just this year's basis"; the what-if that applied it to every year is gone). Returns the number when it is their
    own, else None. `key` carries the location, grain and crop year, so a number typed for one location is never carried over to another;
    `calc` = what the calculated method gives now (the number box starts there)."""
    base = rtc.LETTER_NAME[spec.base]
    mode = st.radio(f"This year's harvest basis ({label})", ["Calculated (the average of the harvest window below)", "My own"],
                    horizontal=True, key=f"nc_rtc_b0_mode_{key}",
                    help=(f"Calculated: {label} is measured from the average of the weekly bids in the harvest window below, against {base} futures "
                          f"(by default the report's first {rtc.WINDOW_WEEKS}). My own: enter the basis you actually bought at (cents per bushel against "
                          f"{base}, negative when under); the shipment table, the headline numbers and {label}'s line and row are measured from it. "
                          "Every other crop year keeps the harvest window's average either way."))
    if not mode.startswith("My own"):
        return None
    try:
        c1, c2 = st.columns([1, 2], vertical_alignment="bottom")
    except TypeError:                                        # an older Streamlit has no vertical_alignment
        c1, c2 = st.columns([1, 2])
    default = 0.0 if calc is None else max(-500.0, min(500.0, round(float(calc), 2)))
    kept = f"nc_rtc_b0_kept_{key}"                           # a widget's state is dropped while it is not drawn: keep what was typed, so
    shown = max(-500.0, min(500.0, float(st.session_state.get(kept, default))))       # flipping to Calculated and back does not lose it
    with c1:
        value = st.number_input(f"Your harvest basis (¢/bu vs {base})", min_value=-500.0, max_value=500.0, step=0.25, format="%.2f",
                                value=shown, key=f"nc_rtc_b0_val_{key}",
                                help=f"Cents per bushel against {base} futures, negative when under: what the grain was (or would be) bought at "
                                     "harvest. It starts at the calculated number.")
    if abs(float(value) - default) > 1e-9:
        st.session_state[kept] = float(value)
    else:
        st.session_state.pop(kept, None)                     # not typed over: it follows the calculated number again next time
    with c2:
        st.caption(f"Only {label}. Every other crop year keeps its calculated basis from the harvest window below.")
    return float(value)


def window_default(crop_year: int) -> tuple:
    """The report's window in the boxes' numbering (week 0 = the first Wednesday of September): 7 weeks from the first Wednesday of October, which
    is week 5 when September has five Wednesdays (2026-27) and week 4 when it has four (2025-26)."""
    return (rtc.report_start_week(crop_year), rtc.WINDOW_WEEKS)


def _harvest_window_choice(spec, crop_year: int):
    """The Start week / Number of weeks boxes under the Harvest basis switch: which weekly bids the CALCULATED harvest basis averages. Week 0 is the first
    Wednesday of September (Kolten 2026-10-19: "start with week zero as Sep 1 rather than Oct 1"), so the first Wednesday of October is week 4 or 5; the
    default, that week and 7, is the report's own window and keeps its year-specific exceptions. A window set here is the same weeks, counted from
    September, in every crop year. Returns None for the report's window, else (first week, number of weeks) as build_crop_year(harvest_window=) takes it.
    Kept per grain across locations: it is how the user establishes their basis, not a fact about a location (and mirrored in `..._kept` while the boxes
    are not drawn; None there = the report's window, so it follows the tracked crop year's start week)."""
    key = f"nc_rtc_win_{spec.key}"
    default = window_default(crop_year)
    kept = st.session_state.get(f"{key}_kept") or default
    label = rtc.crop_label(crop_year)
    st.markdown(SECTION.format(f"Harvest window for the history — every other crop year, and {label} when Calculated"), unsafe_allow_html=True)
    try:
        c1, c2, c3 = st.columns([1, 1, 3], vertical_alignment="bottom")
    except TypeError:                                        # an older Streamlit has no vertical_alignment
        c1, c2, c3 = st.columns([1, 1, 3])
    with c1:
        start = st.number_input("Start week", min_value=0, max_value=34, step=1, value=int(kept[0]), key=f"{key}_start",
                                help="The first weekly bid of the calculated harvest basis. Week 0 is the first Wednesday of September, week 1 the next "
                                     f"and so on; the report starts at the first Wednesday of October, week {default[0]} in {rtc.crop_label(crop_year)} "
                                     "(week 4 in a year whose September has four Wednesdays, 5 when it has five).")
    with c2:
        n = st.number_input("Number of weeks", min_value=1, max_value=26, step=1, value=int(kept[1]), key=f"{key}_n",
                            help=f"How many weekly bids are averaged. The report averages {rtc.WINDOW_WEEKS}.")
    start, n = int(start), int(n)
    custom = (start, n) != default
    st.session_state[f"{key}_kept"] = (start, n) if custom else None
    wt = vw.window_text(crop_year, (start, n))
    wt = wt[0].upper() + wt[1:]
    if custom:
        text = (f"{wt} for {rtc.crop_label(crop_year)}: the same weeks, counted from the first Wednesday of September, in every crop year, in place of "
                f"the report's first {rtc.WINDOW_WEEKS} and its year-specific windows.")
    else:
        text = (f"{wt} for {rtc.crop_label(crop_year)} — the report's window (week 0 is the first Wednesday of September; the report starts at the "
                "first Wednesday of October).")
    accr = spec.accrual_overrides.get(crop_year, spec.accrual_start)
    which = "third" if spec.key == "corn" else "fifth"
    moved = rtc.window_index((start, n), crop_year) + spec.accrual_start - spec.window[0]
    if custom and moved < accr:                              # a window that opens before the report's takes the purchase with it (build_crop_year)
        d = rtc.first_wednesday(crop_year) + timedelta(days=7 * moved)
        text += (f" The purchase moves with it: interest and the weekly returns start at this window's {which} weekly bid "
                 f"({d.strftime('%b')} {d.day} in {label}), in every crop year.")
    elif custom and rtc.window_index((start, n), crop_year) > accr:     # a later window: the interest clock stays the report's
        d = rtc.first_wednesday(crop_year) + timedelta(days=7 * accr)
        text += f" Interest still starts at the report's {which} weekly bid ({d.strftime('%b')} {d.day}), before this window opens."
    with c3:
        st.caption(text)
    return (start, n) if custom else None


def _september_roll_note(spec, win, futs: dict, crop_year: int) -> None:
    """When the user's window reaches back into September (corn), say how a bid quoted off September (CU) futures is moved to Dec and at what level —
    the Dec - Sep spread of the last trading day before first notice — and that a bid already quoted off Dec is not touched."""
    if spec.key != "corn" or win is None or rtc.window_index(win, crop_year) >= 0:
        return
    sp = rtc.sept_to_dec_spread(futs, crop_year, spec.root)
    day = rtc.sept_roll_day(crop_year)
    head = ("September bids quoted off September (CU) futures are moved to Dec by the Dec − Sep spread of the last trading day before the contract's "
            f"first notice day ({day.strftime('%b')} {day.day}); bids already quoted off Dec (every untagged bid, and most locations' fall and new-crop "
            "bids) are left as they are. ")
    if sp is None:
        st.caption(head + f"That spread is not in the futures history for {rtc.crop_label(crop_year)}, so its CU-quoted September bids are left out of the average.")
    else:
        st.caption(head + f"{rtc.crop_label(crop_year)}: Dec − Sep = {sp[0]:+.2f}¢, so a basis of +10 vs Sep reads {10 - sp[0]:+.2f} vs Dec.")


def _ship_by_choice(spec):
    """The Ship by box under the Harvest basis switch: how late the study may go. No limit by default (the best week of the whole season);
    or a month — a pile that has to be gone by March picks March and every crop year is read only up to the end of it. Returns the month
    number (11, 12, 1 ... ) or None for no limit. The choice is kept per grain across locations (a constraint of the user's, not of the
    location), even when the box is not drawn for a while (a freight line, a location with no history)."""
    whole = "July" if spec.key == "corn" else "September"
    months = rtc.ship_by_months(spec)
    labels = [f"No limit (through {whole})"] + [f"End of {vw.MONTH_FULL[m - 1]}" for m in months]
    key = f"nc_rtc_ship_by_{spec.key}"
    kept = st.session_state.get(f"{key}_kept")
    try:
        c1, c2 = st.columns([2, 3], vertical_alignment="bottom")
    except TypeError:                                        # an older Streamlit has no vertical_alignment
        c1, c2 = st.columns([2, 3])
    with c1:
        pick = st.selectbox("Ship by", labels, index=labels.index(kept) if kept in labels else 0, key=key,
                            help=("Limit how late the study goes. By default there is no limit: each crop year's best return is the best week of the whole "
                                  f"season (through {whole}). Pick a month and every year is read only up to the end of it — for grain that has to be "
                                  "shipped by then, when the best carry comes later. The headline, the table, both charts and the shipment table all stop there."))
    st.session_state[f"{key}_kept"] = pick
    if pick == labels[0]:
        return None
    month = months[labels.index(pick) - 1]
    with c2:
        st.caption(f"Only shipments through the end of {vw.MONTH_FULL[month - 1]} count: the best return, the charts, the table and the shipment table stop there.")
    return month


def render(*, obs: list, quotes: list, asof, grain: str, measure: str, tab_rate_pct: float, load_futures, load_prime,
           load_fed_funds, logo_uri: str | None = None, note: str | None = None, message: str | None = None,
           history_hint: str | None = None, location: str = "", derived: dict | None = None, scope: str = "") -> None:
    """Draw the block.

    obs / quotes   the location's weekly nearby bids and posted forward periods (return_to_carry_data's shapes), any dates;
                   what is after `asof` is ignored
    asof           the as-of date of the Net Carry tab (the history and the shipment table end there)
    tab_rate_pct   the tab's interest rate box (annual %): an edit there moves the default rate on every date by the same amount
    load_*         zero/one-argument callables the caller caches: futures(root) -> {date: {symbol: cents}}, prime() and fed_funds()
    note           one extra caption under the explanations (where this location's history comes from)
    message        show this in place of the block (e.g. a freight line has no storage return)
    history_hint   appended to the 'no weekly history' message
    location       the location's name (the derived-history banner says whose basis it is not)
    derived        when part of `obs` is ESTIMATED from another series (bids flagged 'derived': True, river_derived): that series' description
                   {fob_location, gap, q1, q3, pairs, own_from, derived_weeks, first}. A banner says so above the history and the years
                   built from those bids are marked DERIVED / PART DERIVED in the table and drawn lighter in the bars.
    scope          what identifies this location + commodity on the page ('basis|ADM|Havana, IL', a corridor key ...): the Harvest basis widgets'
                   keys carry it (with the grain and crop year) so a number typed for one location is not kept for the next. Defaults to `location`.

    The Harvest basis switch (Calculated by default) lets the user measure the crop year being tracked — the shipment table, the headline,
    the orange line, the latest row — from their OWN harvest basis; every other crop year keeps the harvest window's calculated average, and
    the window boxes stay (and still move that history) while their own number is in use."""
    st.markdown(HEADING, unsafe_allow_html=True)
    spec = rtc.SPECS.get(grain)
    if spec is None:
        st.info("The Return to Carry tracker is built for corn and soybeans (the analyst's two reports); wheat "
                "needs its own contract chain and harvest window.")
        return
    if message:
        st.info(message)
        return
    obs = [o for o in obs if o["date"] <= asof]
    quotes = [q for q in quotes if q["date"] <= asof]
    marks = rd.derived_years(obs)                    # {crop year: 'derived' | 'part'}, empty for a location's own history
    if derived and marks:
        mixed = next((y for y, v in marks.items() if v == "part"), None)
        weeks = (sum(1 for o in obs if o.get("derived") and (o["date"].year if o["date"].month >= 10 else o["date"].year - 1) == mixed)
                 if mixed is not None else 0)
        st.markdown(vw.derived_banner_html(location, dict(derived, mixed_weeks=weeks)), unsafe_allow_html=True)
    if len(obs) < 12 and not quotes:
        st.info("No weekly spot history for this location to track (the tracker needs a bid every week from "
                "October through " + ("July" if spec.key == "corn" else "September") + "; most locations only began posting "
                "forward curves in 2026)." + (" " + history_hint if history_hint else ""))
        return
    mode = st.radio("Interest", [f"Same as the rest of this tab (fed funds + {cr.FED_FUNDS_SPREAD_PCT:.2f}%)",
                                 "Bank prime (as in the Return to Carry report)"], horizontal=True, key="nc_rtc_rate2",
                    help="By default the same rate as the Net Carry calculations above: the effective fed funds rate on "
                         "each date plus the Cost of Carry spread, following the rate box (an edit there moves every "
                         "date by the same amount). The second choice is the bank prime rate the Research Analyst's "
                         "report uses, for tying out to it.")
    if mode.startswith("Bank prime"):
        prime = load_prime()
        rate_on = (lambda d: cr.prime_on(prime, d, None))
        rate_note = "bank prime"
    else:
        ff = load_fed_funds()
        offset = round(tab_rate_pct - cr.rate_for(asof, ff).rate_pct, 4)      # how far the rate box was edited (0 = default)
        rate_on = (lambda d: cr.rate_for(d, ff).rate_pct + offset)
        rate_note = f"fed funds + {cr.FED_FUNDS_SPREAD_PCT:.2f}%, as in the rest of this tab" + (
            f" ({offset:+.2f} from the rate box)" if abs(offset) >= 0.005 else "")
    crop_year = rtc.shipment_crop_year(asof)                 # the crop year being tracked: the shipment table's, and the history's newest
    slot_basis = st.container()                              # the Harvest basis switch needs the calculated basis (its number box starts there), and the
    slot_window = st.container()                             # calculated basis needs the window: draw the window first, but keep the switch above it
    with slot_window:
        win = _harvest_window_choice(spec, crop_year)        # None = the report's window
    try:
        futs = load_futures(spec.root)                       # the futures history comes from the database
        tbl, est = rd.shipment_table(obs, quotes, futs, rate_on, asof, measure, spec, harvest_window=win)       # the calculated harvest basis
    except Exception as e:                                   # noqa: BLE001 — a data problem must not take the tab down
        st.warning(f"Couldn't build the Return to Carry history right now ({type(e).__name__}: {e}).")
        return
    with slot_window:
        _september_roll_note(spec, win, futs, crop_year)
    with slot_basis:
        own = _harvest_basis_choice(spec, f"{scope or location}|{grain}|{crop_year}", tbl.b0, rtc.crop_label(crop_year))   # None = calculated
    ship_month = _ship_by_choice(spec)                       # None = no limit: the whole season, as before
    try:
        if own is not None:                                  # the user's own harvest basis: this year's table and line are measured from it
            tbl, est = rd.shipment_table(obs, quotes, futs, rate_on, asof, measure, spec, b0_override=own, harvest_window=win)
        res, skipped = (rd.run_history_noted(obs, futs, rate_on, spec=spec, harvest_window=win,
                                             b0_overrides=rd.own_b0_map(own, crop_year, obs, spec))       # the tracked year only
                        if len(obs) >= 12 else ([], []))
    except Exception as e:                                   # noqa: BLE001
        st.warning(f"Couldn't build the Return to Carry history right now ({type(e).__name__}: {e}).")
        return
    if own is not None:
        st.markdown(vw.own_basis_note_html(own, tbl.b0_calc, rtc.crop_label(crop_year), spec,
                                           in_history=any(cy.crop_year == crop_year for cy in res)), unsafe_allow_html=True)
    if tbl is not None and (tbl.b0 is not None or any(c.bid is not None for c in tbl.cols)):
        st.markdown(vw.shipment_html(rtc.limit_shipment_table(tbl, ship_month), est, rate_note), unsafe_allow_html=True)
    if not res:
        st.info("Not enough weekly history yet to chart the crop years for this location.")
        return
    res = rd.limit_history(res, ship_month)                  # a study told to ship by the end of a month reads only the weeks up to it
    rows = rd.summary_rows(res, measure)
    st.markdown(vw.headline_html(rows, measure, spec), unsafe_allow_html=True)
    ch = vw.seasonal_chart(res, measure, logo_uri=logo_uri, spec=spec)
    if ch is not None:
        st.altair_chart(ch, **_stretch())
    bar = vw.best_bar_chart(rows, measure, derived=marks)
    if bar is not None:
        st.altair_chart(bar, **_stretch())
    st.markdown(vw.table_html(rows, measure, derived=marks), unsafe_allow_html=True)
    if skipped:
        st.caption(f"Not shown: {', '.join(skipped)} — the weekly archive repeats the previous year's bids for it "
                   "(a copy, not that year's market).")
    st.caption(vw.method_caption(spec, stop=res[-1].window_end))
    if note:
        st.caption(note)
    with st.expander("How the return is calculated"):
        st.markdown(vw.how_it_works(spec))
