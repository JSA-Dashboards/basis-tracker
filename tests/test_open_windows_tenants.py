"""Open-window AgriCharts tenants: Garden City Coop and Alcivia join Ag Partners Coop on `open_windows` (a bid whose window began in an earlier month but has not ended is THIS month's bid).

    python tests/test_open_windows_tenants.py

Kolten 2026-10-28: "Fix this" (the parser's start-month rule dropped in-season bids: Garden City Coop 114 of 134, Alcivia 8 of 181, found 2026-10-27). Switching it on was not just one key:
Garden City posts an 'Old Crop' and a 'New Crop 2026' window with the same basis (both would land on 'Oct 2026': the later-starting one is kept), four of its corn bids are posted at exactly 0
(`skip_zero_basis`), and Alcivia's 'Storage' / 'Storage Only' wheat windows are storage prices, not bids (`skip_notes`). Uses trimmed copies of the real feeds of 2026-10-28
(tests/fixtures/gccoop_feed_2026-10-28.json: Alamota and Garden City; alcivia_storage_feed_2026-10-28.json: Baldwin and Belleville) - no network.
"""
import json
import logging
import os
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing anything that imports database: the repo .env turns Snowflake on

import agricharts_scraper as ag                                  # noqa: E402
from parsers.ksethanol_parser import parse_ksethanol_location    # noqa: E402

logging.disable(logging.CRITICAL)
FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


GC_FEED = json.load(open(os.path.join(HERE, "fixtures", "gccoop_feed_2026-10-28.json"), encoding="utf-8"))
AL_FEED = json.load(open(os.path.join(HERE, "fixtures", "alcivia_storage_feed_2026-10-28.json"), encoding="utf-8"))
T = {t["provider"]: t for t in ag.TENANTS}


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, feed):
        self.headers = {}
        self.feed = feed

    def get(self, url, timeout=None):
        return FakeResp("/* AgriCharts */\nvar bids = " + json.dumps(self.feed) + ";\nvar x = 1;")


def fetch(tenant, feed):
    sess = FakeSession(feed)
    with mock.patch.object(ag.requests, "Session", lambda: sess), mock.patch.object(ag, "TENANTS", [tenant]), mock.patch.object(ag.time, "sleep", lambda s: None):
        return {l["location_name"]: l for l in ag.fetch_agricharts_bids()}


def parse(locs, when, **override):
    out = {}
    for name, l in locs.items():
        l = dict(l, timestamp=when)
        for k, v in override.items():
            if v is None:
                l.pop(k, None)
            else:
                l[k] = v
        out[name] = parse_ksethanol_location(l)
    return out


def rows(req):
    return sorted((r.grain, r.futuresSymbol, r.deliveryMonth, r.basisCents) for r in req.rows) if req else []


print("open windows: which tenants have it")
check("Garden City Coop: open_windows and skip_zero_basis", T["Garden City Coop"].get("open_windows") is True and T["Garden City Coop"].get("skip_zero_basis") is True, T["Garden City Coop"])
check("Alcivia: open_windows and skip_notes ['Storage'] (its pins, titlecase and state are as they were)", T["Alcivia"].get("open_windows") is True and T["Alcivia"].get("skip_notes") == ["Storage"]
      and T["Alcivia"].get("pins") is True and T["Alcivia"].get("titlecase") is True and T["Alcivia"]["state"] == "WI", T["Alcivia"])
check("exactly these three tenants have open_windows (Mid Missouri, JBS, Gold Eagle and Meadowland lose nothing today and are unchanged)",
      [t["provider"] for t in ag.TENANTS if t.get("open_windows")] == ["Garden City Coop", "Alcivia", "Ag Partners Coop"], [t["provider"] for t in ag.TENANTS if t.get("open_windows")])
check("only Garden City has skip_zero_basis and only Alcivia skip_notes", [t["provider"] for t in ag.TENANTS if t.get("skip_zero_basis")] == ["Garden City Coop"] and [t["provider"] for t in ag.TENANTS if t.get("skip_notes")] == ["Alcivia"])

print("\ngarden city coop: the fetch")
gc = fetch(T["Garden City Coop"], GC_FEED)
check("both locations are filed ('Alamota Elevator' -> 'Alamota, KS')", sorted(gc) == ["Alamota, KS", "Garden City, KS"], sorted(gc))
raw_alamota = next(l for l in GC_FEED if l["name"] == "Alamota Elevator")["cashbids"]
check("the fixture is the real shape: Alamota posts corn at 0 in an Old Crop and a New Crop window", sorted((b["name"], b["basis"]) for b in raw_alamota if b["name"] == "Corn") == [("Corn", 0), ("Corn", 0)], raw_alamota)
check("skip_zero_basis drops those two at the fetch (Alamota has no corn left); Garden City's corn at +10 stays, in both windows", not [b for b in gc["Alamota, KS"]["cashbids"] if b["grain"] == "Corn"]
      and len([b for b in gc["Garden City, KS"]["cashbids"] if b["grain"] == "Corn"]) == 2, [(b["grain"], b["basis"]) for b in gc["Garden City, KS"]["cashbids"]])
gc_all = fetch({k: v for k, v in T["Garden City Coop"].items() if k != "skip_zero_basis"}, GC_FEED)
check("without skip_zero_basis the zero corn bids are filed (so the option is what removes them)", len([b for b in gc_all["Alamota, KS"]["cashbids"] if b["grain"] == "Corn"]) == 2)

print("\ngarden city coop: parsed on 2026-10-28")
p = parse(gc, "2026-10-28T00:00:00Z")
check("Alamota: HRW wheat 'Oct 2026' against KEZ26 at -70 (its Old Crop window since 06/02), the deferred KEN27 'Jun 2027' at -70, and milo ONCE ('Old Crop' and 'New Crop 2026' collapse) at -60",
      rows(p["Alamota, KS"]) == [("Hard Red Wheat", "KEN27", "Jun 2027", -70), ("Hard Red Wheat", "KEZ26", "Oct 2026", -70), ("Milo", "ZCZ26", "Oct 2026", -60)], rows(p["Alamota, KS"]))
check("Garden City: corn once at +10, milo once at -65, HRW -75 and -70, hard white -85: five rows, no duplicate",
      rows(p["Garden City, KS"]) == [("Corn", "ZCZ26", "Oct 2026", 10), ("Hard Red Wheat", "KEN27", "Jun 2027", -70), ("Hard Red Wheat", "KEZ26", "Oct 2026", -75), ("Hard White Wheat", "KEZ26", "Oct 2026", -85), ("Milo", "ZCZ26", "Oct 2026", -65)],
      rows(p["Garden City, KS"]))
check("row ids are unique within each snapshot", all(len({r.id for r in q.rows}) == len(q.rows) for q in p.values()))
old = parse(gc, "2026-10-28T00:00:00Z", open_windows=None)
check("WITHOUT open_windows (what the tracker has filed since the windows opened): only the deferred KEN27 wheat survives at each location",
      rows(old["Alamota, KS"]) == [("Hard Red Wheat", "KEN27", "Jun 2027", -70)] and rows(old["Garden City, KS"]) == [("Hard Red Wheat", "KEN27", "Jun 2027", -70)], (rows(old["Alamota, KS"]), rows(old["Garden City, KS"])))
dec = parse(gc, "2026-12-05T00:00:00Z")
check("on 2026-12-05 the windows that ended 12/01 are stale and gone; the 2027 wheat stays", rows(dec["Alamota, KS"]) == [("Hard Red Wheat", "KEN27", "Jun 2027", -70)], rows(dec["Alamota, KS"]))
dec1 = parse(gc, "2026-12-01T00:00:00Z")
check("on 2026-12-01 itself the windows ending that day are still open and are filed under 'Dec 2026'", ("Milo", "ZCZ26", "Dec 2026", -60) in rows(dec1["Alamota, KS"]), rows(dec1["Alamota, KS"]))

print("\nthe overlapping-window rule (synthetic windows)")


def bid(i, start, end, basis, grain="Corn", sym="ZCZ26"):
    return {"bid_id": str(i), "grain": grain, "symbol": sym, "basis": basis, "delivery_start": start, "delivery_end": end}


def loc(*bids, ow=True):
    return {"provider": "X", "location_name": "Y, KS", "timestamp": "2026-10-28T00:00:00Z", "cashbids": list(bids), **({"open_windows": True} if ow else {})}


a = parse_ksethanol_location(loc(bid(1, "09/01/2025", "12/01/2026", -50), bid(2, "09/01/2026", "12/01/2026", -60)))
b = parse_ksethanol_location(loc(bid(2, "09/01/2026", "12/01/2026", -60), bid(1, "09/01/2025", "12/01/2026", -50)))
check("two windows on one month with DIFFERENT basis: the later-starting one wins, whichever comes first in the feed", rows(a) == rows(b) == [("Corn", "ZCZ26", "Oct 2026", -60)], (rows(a), rows(b)))
a2 = parse_ksethanol_location(loc(bid(5, "09/01/2026", "12/01/2026", -60), bid(9, "09/01/2025", "12/01/2026", -50)))
check("the START decides, not the bid id: a later-starting window with the LOWER id (5, since 09/01/2026) beats the older window with the higher one (9, since 09/01/2025)", rows(a2) == [("Corn", "ZCZ26", "Oct 2026", -60)], rows(a2))
c = parse_ksethanol_location(loc(bid(7, "09/01/2026", "12/01/2026", -40), bid(9, "09/01/2026", "12/01/2026", -45)))
check("equal starts: the later bid id wins", rows(c) == [("Corn", "ZCZ26", "Oct 2026", -45)], rows(c))
d = parse_ksethanol_location(loc(bid(1, "09/01/2026", "12/01/2026", -50), bid(2, "09/01/2026", "12/01/2026", -50, grain="Milo")))
check("a different grain or contract on the same month is not a duplicate", len(d.rows) == 2)
e = parse_ksethanol_location(loc(bid(1, "09/01/2025", "12/01/2026", -50), bid(2, "09/01/2026", "12/01/2026", -60), ow=False))
check("without open_windows the rule is as it was: both windows are stale (start month before this month) and nothing is filed", e is None)

print("\nalcivia: the fetch and the months")
al = fetch(T["Alcivia"], AL_FEED)
n_total = sum(len(l["cashbids"]) for l in AL_FEED)
n_storage = sum(1 for l in AL_FEED for b in l["cashbids"] if "storage" in (b["notes"] or "").lower())
n_kept = sum(len(l["cashbids"]) for l in al.values())
check("the 'Storage' and 'Storage Only' wheat bids (%d in the fixture) are dropped at the fetch, nothing else" % n_storage, n_storage >= 2 and n_kept == n_total - n_storage, (n_total, n_storage, n_kept))
check("the other wheat bid ('July 27', ZWU27) is kept", all(any(b["grain"] == "WHEAT" and b["symbol"] == "ZWU27" for b in l["cashbids"]) for l in al.values()), [(n, [b["symbol"] for b in l["cashbids"] if b["grain"] == "WHEAT"]) for n, l in al.items()])
oct28 = parse(al, "2026-10-28T00:00:00Z")
bal = rows(oct28["Baldwin, WI"])
check("Baldwin on 10-28: corn 'Oct 2026' -75 and soybeans 'Oct 2026' -95 as before, and wheat only for July 2027 (-150)", ("Corn", "ZCZ26", "Oct 2026", -75) in bal and ("Soybeans", "ZSX26", "Oct 2026", -95) in bal
      and [r for r in bal if r[0] == "Wheat"] == [("Wheat", "ZWU27", "Jul 2027", -150)], bal)
nov = parse(al, "2026-11-15T00:00:00Z")
check("on 2026-11-15 the 'Oct/Nov 26' harvest window (10/01 - 11/30) is still open and is filed under 'Nov 2026': corn at -75, soybeans at -95", ("Corn", "ZCZ26", "Nov 2026", -75) in rows(nov["Baldwin, WI"]) and ("Soybeans", "ZSX26", "Nov 2026", -95) in rows(nov["Baldwin, WI"]), rows(nov["Baldwin, WI"]))
nov_old = parse(al, "2026-11-15T00:00:00Z", open_windows=None)
check("WITHOUT open_windows those two vanish on 2026-11-15, which is what would have happened to Alcivia's main bids from the first of November", not [r for r in rows(nov_old["Baldwin, WI"]) if r[1] in ("ZCZ26", "ZSX26") and r[2] in ("Oct 2026", "Nov 2026")], rows(nov_old["Baldwin, WI"]))
dec5 = parse(al, "2026-12-05T00:00:00Z")
check("on 2026-12-05 the Oct/Nov window is gone and the 'December 26' window (12/01 - 12/31) is filed as 'Dec 2026'", ("Corn", "ZCZ26", "Dec 2026", -60) in rows(dec5["Baldwin, WI"]) and not [r for r in rows(dec5["Baldwin, WI"]) if r[2] in ("Oct 2026", "Nov 2026")], rows(dec5["Baldwin, WI"]))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
