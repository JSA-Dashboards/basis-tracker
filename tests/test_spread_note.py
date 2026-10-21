"""A posted SPREAD line is not a delivery period: its number is the spread, so it must never become a spot bid, a forward quote or a month's value.

    python tests/test_spread_note.py

BN PNW's 2026-10-19 posting opened with 'Spread-Sell FH Nov CP / Buy FH Nov ; offered' at -2, and every 'nearest period with a bid' rule took it: the spot line charted -2
against ~115 around it (Kolten: "fix the BN PNW as well"). The rows below are that posting. Pure functions; nothing here touches a database.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL"):
    os.environ[k] = ""

import forward_month as fm  # noqa: E402
import net_carry_compare as ncc  # noqa: E402
import rail_report  # noqa: E402
import rail_spread as rs  # noqa: E402
import return_to_carry_data as rd  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:400]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


NOTE = "Spread-Sell FH Nov CP / Buy FH Nov ; offered"
M = "BN PNW"
D0, D1 = "2026-10-15", "2026-10-19"


def row(d, period, bid, order, fut="ZCZ26", offer=None):
    return {"date": d, "market": M, "commodity": "Corn", "period": period, "period_order": order, "futures": fut, "bid": bid, "offer": offer}


# the 10-19 posting, as stored (period_order, label, bid / offer), and the 10-15 one before it
ROWS = [row(D0, "Nov", None, 0, offer=120), row(D0, "LH Nov", None, 1, offer=130), row(D0, "Dec", 110, 2, "ZCH27", 138), row(D0, "AM", 125, 4, "ZCK27", 145),
        row(D1, NOTE, -2, 0), row(D1, "Oct", None, 1, offer=110), row(D1, "FH Nov", None, 2, offer=110), row(D1, "LH Nov", None, 3, offer=115),
        row(D1, "LP Nov", 100, 4), row(D1, "Dec", None, 5, "ZCH27", 135), row(D1, "split JFM", 118, 6, None, 140), row(D1, "AM", None, 7, "ZCK27", 120)]

print("what is a spread note")
check("the 10-19 line is one", rd.is_spread_note(NOTE) and fm.is_spread_note(NOTE))
check("ordinary periods and notes of other kinds are not ('Nov', 'FH Dec', 'JFM', 'Sellers Oct' (BN COBO's own format), 'Oct Sellers Equipment', None, '')",
      not any(rd.is_spread_note(x) for x in ("Nov", "FH Dec", "JFM", "Sellers Oct", "Buyers Sep/Oct", "Oct Sellers Equipment", "Next 10 days dlvd Hugton", None, "")))
check("any spelling of the word: 'SPREAD Dec/Mar', 'Dec-Mar spread'", rd.is_spread_note("SPREAD Dec/Mar") and rd.is_spread_note("Dec-Mar spread") and not rd.is_spread_note("Spreadsheet"))

print("the spot series (Return to Carry, Spread tab, Rail FOB tab's Spot)")
obs = {o["date"].isoformat(): o["basis"] for o in rd.obs_from_rail(ROWS, M)}
check("10-19's nearest bid is the first DELIVERY period with one: LP Nov 100, not the spread's -2", obs["2026-10-19"] == 100.0, obs)
check("10-15 is unchanged (Dec 110, the first period with a bid)", obs["2026-10-15"] == 110.0, obs)
check("rail_spread's spot_series is the same series", dict((d.isoformat(), b) for d, b in rs.spot_series(ROWS, M, "Corn"))["2026-10-19"] == 100.0)

print("the shipment table's quotes")
q = rd.quotes_from_rail(ROWS, M)
check("the spread note is not a quote", not any(x["label"] == NOTE for x in q) and any(x["label"] == "LP Nov" for x in q), [x["label"] for x in q])
_, fq = rs.forward_quotes(ROWS, M, "Corn")
check("nor a forward quote for the Spread tab (the latest posting's Nov is LP Nov 100)", not any(x["label"] == NOTE for x in fq) and any(x["label"] == "LP Nov" for x in fq), [x["label"] for x in fq])

print("the emailed chart")
by_md, mkt_dates = {}, {M: set()}
for r in ROWS:
    by_md.setdefault((M, r["date"]), {})[r["period"]] = r
    mkt_dates[M].add(r["date"])
spot = rail_report._spot_series(M, by_md, mkt_dates)
check("its spot line reads 100 on 10-19 (not -2) and 110 on 10-15", spot == {D0: 110, D1: 100}, spot)
cells = sorted(by_md[(M, D1)].values(), key=lambda r: r["period_order"])
check("the table's year-ago fallback row is LP Nov, not the note", rail_report._nearest_bid_cell(cells)["period"] == "LP Nov")
check("a posting made only of spread notes and blanks has no spot", rail_report._nearest_bid_cell([row(D1, NOTE, -2, 0), row(D1, "Oct", None, 1, offer=110)]) is None)
check("the forward curve ignores it too (label_info reads it as no period; rail_forward drops it)", fm.label_info(NOTE) == (None, ()) and fm.rail_forward([(NOTE, -2)]) == {}
      and fm.rail_forward([(NOTE, -2), ("LP Nov", 100)]) == {11: 100.0})

print("Net Carry's items")
items = ncc.rail_items(ROWS, M, "Corn", D1)
check("rail_items leaves the note out and keeps the real bids", [i["delivery"] for i in items] == ["LP Nov", "split JFM"], [i["delivery"] for i in items])

print()
if FAILS:
    print("FAILED (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("all spread-note checks passed")
