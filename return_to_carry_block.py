"""return_to_carry_block.py — the Return to Carry section of a Net Carry tab, as one Streamlit block.

Shared by the basis tracker and the River / Rail FOB portals (each keeps its own copy of this file next to the return_to_carry modules —
see sync_carry_modules.py): the caller loads the location's weekly nearby bids (`obs`) and every posted forward period (`quotes`) and
says how to load the futures, prime and fed funds histories; this block does the rest — the Interest switch, the report's
shipment-by-month table, the headline numbers, the season chart, the best-return-by-year bars, the by-year table and the explanations.

    render(obs=..., quotes=..., asof=date, grain='Corn', measure='net' | 'gross', tab_rate_pct=6.13,
           load_futures=lambda root: {date: {symbol: cents}}, load_prime=..., load_fed_funds=..., logo_uri=None, note=None)

The Interest radio keeps the key `nc_rtc_rate2` (a page has one Return to Carry block at a time).
"""
from __future__ import annotations

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


def render(*, obs: list, quotes: list, asof, grain: str, measure: str, tab_rate_pct: float, load_futures, load_prime,
           load_fed_funds, logo_uri: str | None = None, note: str | None = None, message: str | None = None,
           history_hint: str | None = None, location: str = "", derived: dict | None = None) -> None:
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
                   built from those bids are marked DERIVED / PART DERIVED in the table and drawn lighter in the bars."""
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
    try:
        futs = load_futures(spec.root)                       # the futures history comes from the database
        tbl, est = rd.shipment_table(obs, quotes, futs, rate_on, asof, measure, spec)
        res, skipped = rd.run_history_noted(obs, futs, rate_on, spec=spec) if len(obs) >= 12 else ([], [])
    except Exception as e:                                   # noqa: BLE001 — a data problem must not take the tab down
        st.warning(f"Couldn't build the Return to Carry history right now ({type(e).__name__}: {e}).")
        return
    if tbl is not None and (tbl.b0 is not None or any(c.bid is not None for c in tbl.cols)):
        st.markdown(vw.shipment_html(tbl, est, rate_note), unsafe_allow_html=True)
    if not res:
        st.info("Not enough weekly history yet to chart the crop years for this location.")
        return
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
    st.caption(vw.method_caption(spec))
    if note:
        st.caption(note)
    with st.expander("How the return is calculated"):
        st.markdown(vw.how_it_works(spec))
