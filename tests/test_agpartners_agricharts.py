"""Ag Partners Cooperative (Seneca KS) on the AgriCharts tenant scraper: agpartners.agricharts.com's cashbids-js.php feed (the one agpartnerscoop.com/grain/cash-bids embeds).

    python tests/test_agpartners_agricharts.py

Kolten 2026-10-27: "Lets also add in this bid scrapper ... agpartnerscoop.com/grain/cash-bids". One TENANTS row plus three small options in agricharts_scraper / the shared parser:
`skip` (the feed's 'AGP St Joe' row is the co-op's direct-ship bid to AGP's plant, not an elevator), `grain_alias` (its 'Red Wheat' is Kansas City hard red winter, KEZ26) and `open_windows`
(its main bids are HARVEST windows, '09/01/2026 - 11/30/2026': the parser's start-month rule alone drops them from October on, which cost this feed 41 of its 92 bids on the day it was added).
Uses a trimmed copy of the real feed (tests/fixtures/agpartners_feed_2026-10-27.json: Axtell, Denton, Rulo, Seneca and the AGP row, original values) - no network; the database is a throwaway SQLite.
"""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["USE_SNOWFLAKE"] = ""      # BEFORE importing database: the repo .env turns Snowflake on

import agricharts_scraper as ag                                  # noqa: E402
import database as db                                            # noqa: E402
from parsers.ksethanol_parser import parse_ksethanol_location    # noqa: E402

db.DB_PATH = Path(tempfile.mkdtemp(prefix="agpartners_")) / "t.db"
assert db._backend() == "sqlite", "refusing to run on a non-sqlite backend"
logging.disable(logging.CRITICAL)

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


FEED = json.load(open(os.path.join(HERE, "fixtures", "agpartners_feed_2026-10-27.json"), encoding="utf-8"))
AGP = next((t for t in ag.TENANTS if t["provider"] == "Ag Partners Coop"), None)

print("ag partners: the tenant row")
check("a multi-location AgriCharts tenant on agpartners.agricharts.com, KS, with the feed's pins", AGP is not None and (AGP["host"], AGP["shape"], AGP["state"], AGP.get("pins")) == ("https://agpartners.agricharts.com", "multi", "KS", True), AGP)
check("it skips the AGP direct-ship row, files Red Wheat as 'Wheat, Hrw' and keeps open harvest windows", AGP is not None and AGP.get("skip") == ["AGP St Joe"] and AGP.get("grain_alias") == {"Red Wheat": "Wheat, Hrw"} and AGP.get("open_windows") is True, AGP)
check("no other tenant has skip or grain_alias; open_windows is on for Ag Partners and, since 2026-10-28, Garden City Coop and Alcivia (test_open_windows_tenants.py), for none of the rest",
      all(not (t.get("skip") or t.get("grain_alias")) for t in ag.TENANTS if t["provider"] != "Ag Partners Coop")
      and [t["provider"] for t in ag.TENANTS if t.get("open_windows")] == ["Garden City Coop", "Alcivia", "Ag Partners Coop"],
      [t["provider"] for t in ag.TENANTS if t.get("skip") or t.get("grain_alias") or t.get("open_windows")])
check("the provider name cannot be confused with South Dakota's Prairie Ag Partners", AGP is not None and AGP["provider"] != "Prairie Ag Partners" and not any(t["provider"] == "Ag Partners" for t in ag.TENANTS))


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, feed):
        self.headers = {}
        self.urls = []
        self.feed = feed

    def get(self, url, timeout=None):
        self.urls.append(url)
        return FakeResp("/* AgriCharts */\nvar bids = " + json.dumps(self.feed) + ";\nvar x = 1;")


def fetch(tenant, feed=FEED):
    sess = FakeSession(feed)
    with mock.patch.object(ag.requests, "Session", lambda: sess), mock.patch.object(ag, "TENANTS", [tenant]), mock.patch.object(ag.time, "sleep", lambda s: None):
        return ag.fetch_agricharts_bids(), sess


print("\nag partners: the fetch (the real feed's shape)")
locs, sess = fetch(AGP)
by = {l["location_name"]: l for l in locs}
check("it asks the tenant's own host for cashbids-js.php", len(sess.urls) == 1 and sess.urls[0].startswith("https://agpartners.agricharts.com/inc/cashbids/cashbids-js.php?"), sess.urls)
check("four elevators are filed, 'AGP St Joe' (a Direct Ship row) is not", sorted(by) == ["Axtell, KS", "Denton, KS", "Rulo, NE", "Seneca, KS"], sorted(by))
check("each location's state is the feed's own (Rulo is Nebraska, not the tenant's KS)", {n: l["state"] for n, l in by.items()} == {"Axtell, KS": "KS", "Denton, KS": "KS", "Rulo, NE": "NE", "Seneca, KS": "KS"}, {n: l["state"] for n, l in by.items()})
check("every one is a Country Elevator filed under Ag Partners Coop", {(l["provider"], l["facility_type"]) for l in locs} == {("Ag Partners Coop", "Country Elevator")}, [(l["provider"], l["facility_type"]) for l in locs])
check("map pins are the feed's own coordinates (Axtell 39.87339 / -96.25811, Rulo 40.0537 / -95.42888)", (by["Axtell, KS"]["lat"], by["Axtell, KS"]["lon"], by["Rulo, NE"]["lat"], by["Rulo, NE"]["lon"]) == (39.87339, -96.25811, 40.0537, -95.42888),
      (by["Axtell, KS"].get("lat"), by["Axtell, KS"].get("lon")))
check("every location carries open_windows and every bid its window's end", all(l.get("open_windows") is True and all(b["delivery_end"] for b in l["cashbids"]) for l in locs))
grains = {l["location_name"]: sorted({b["grain"] for b in l["cashbids"]}) for l in locs}
check("'Red Wheat' reaches the parser as 'Wheat, Hrw'; Milo is left alone", grains["Axtell, KS"] == ["Corn", "Soybeans", "Wheat, Hrw"] and grains["Seneca, KS"] == ["Corn", "Milo", "Soybeans", "Wheat, Hrw"] and grains["Denton, KS"] == ["Corn", "Soybeans"], grains)
locs_all, _ = fetch({k: v for k, v in AGP.items() if k not in ("skip",)})
check("without `skip` the AGP direct-ship row would be filed (so the option is what keeps it out)", "AGP St Joe, MO" in {l["location_name"] for l in locs_all}, sorted(l["location_name"] for l in locs_all))


def parse(loc_list, when):
    out = {}
    for l in loc_list:
        l = dict(l, timestamp=when)
        out[l["location_name"]] = parse_ksethanol_location(l)
    return out


def rowmap(req):
    return {(r.grain, r.futuresSymbol): (r.deliveryMonth, r.basisCents) for r in req.rows}


print("\nag partners: parsed through the shared AgriCharts parser, on the day it was added (2026-10-27)")
reqs = parse(locs, "2026-10-27T00:00:00Z")
ax = rowmap(reqs["Axtell, KS"])
check("Axtell: the harvest windows (Sep 1 - Nov 30) are kept and filed under THIS month: soybeans ZSX26 'Oct 2026' at -60, corn ZCZ26 'Oct 2026' at -55",
      ax.get(("Soybeans", "ZSX26")) == ("Oct 2026", -60) and ax.get(("Corn", "ZCZ26")) == ("Oct 2026", -55), ax)
check("Axtell: the 2027 windows keep their own start month ('Sep 2027'), the October wheat window is 'Oct 2026' against KEZ26 (Kansas City) at -70",
      ax.get(("Soybeans", "ZSX27")) == ("Sep 2027", -80) and ax.get(("Corn", "ZCZ27")) == ("Sep 2027", -60) and ax.get(("Wheat, Hrw", "KEZ26")) == ("Oct 2026", -70), ax)
check("Axtell has exactly its 5 bids, with unique row ids", len(reqs["Axtell, KS"].rows) == 5 and len({r.id for r in reqs["Axtell, KS"].rows}) == 5, [r.id for r in reqs["Axtell, KS"].rows])
check("Seneca's milo (priced off December corn) is kept as 'Milo' at -140 against ZCZ26", rowmap(reqs["Seneca, KS"]).get(("Milo", "ZCZ26")) == ("Oct 2026", -140), rowmap(reqs["Seneca, KS"]))
check("20 bids in the trimmed feed, 19 after the AGP row (it carries one): 19 rows filed over the four elevators", sum(len(r.rows) for r in reqs.values()) == 19, sum(len(r.rows) for r in reqs.values()))

print("\nag partners: the window rule on other days")
dec = parse(locs, "2026-12-05T00:00:00Z")
ax_dec = rowmap(dec["Axtell, KS"])
check("on 2026-12-05 the harvest windows (ended Nov 30) and the October wheat window are stale and gone; only the 2027 bids are left", sorted(ax_dec) == [("Corn", "ZCZ27"), ("Soybeans", "ZSX27")], ax_dec)
sep = parse(locs, "2026-09-15T00:00:00Z")
ax_sep = rowmap(sep["Axtell, KS"])
check("on 2026-09-15 a window that starts this month keeps its own start month ('Sep 2026'): nothing is moved, the October wheat is still ahead ('Oct 2026')",
      ax_sep.get(("Soybeans", "ZSX26")) == ("Sep 2026", -60) and ax_sep.get(("Corn", "ZCZ26")) == ("Sep 2026", -55) and ax_sep.get(("Wheat, Hrw", "KEZ26")) == ("Oct 2026", -70), ax_sep)
off = [dict(l) for l in locs]
for l in off:
    l.pop("open_windows", None)
old = parse(off, "2026-10-27T00:00:00Z")
check("WITHOUT open_windows the shared start-month rule drops the harvest windows (the behaviour every other tenant still has): Axtell keeps only its 2027 and wheat bids",
      sorted(rowmap(old["Axtell, KS"])) == [("Corn", "ZCZ27"), ("Soybeans", "ZSX27"), ("Wheat, Hrw", "KEZ26")], rowmap(old["Axtell, KS"]))
ended = [dict(l, cashbids=[dict(b, delivery_start="08/01/2025", delivery_end="09/30/2025") for b in l["cashbids"]]) for l in locs]
check("a window that ENDED before this month is stale even with open_windows (the 'lingering Sep 2025 bid' the rule exists for)", all(r is None for r in parse(ended, "2026-10-27T00:00:00Z").values()))
noend = [dict(l, cashbids=[{k: v for k, v in b.items() if k != "delivery_end"} for b in l["cashbids"]]) for l in locs]
check("a bid with no end date cannot be shown to be open: the old rule applies (dropped when its start month is past)", sorted(rowmap(parse(noend, "2026-10-27T00:00:00Z")["Axtell, KS"])) == [("Corn", "ZCZ27"), ("Soybeans", "ZSX27"), ("Wheat, Hrw", "KEZ26")])

print("\nag partners: pins and bids reach the database (throwaway SQLite)")
db.init_db()
db.upsert_snapshots([r.model_dump() for r in reqs.values()])
metas = [{"location": r.location, "state": by[r.location]["state"], "facility_type": by[r.location]["facility_type"], "lat": by[r.location].get("lat"), "lon": by[r.location].get("lon")} for r in reqs.values()]
db.upsert_location_metas("Ag Partners Coop", metas)       # the same dict shape run_agricharts_tenants builds
conn = db.get_conn()
c = conn.cursor()
c.execute("SELECT location, state, facility_type, lat, lon FROM location_meta WHERE provider = 'Ag Partners Coop' ORDER BY location")
got = [tuple(r) for r in c.fetchall()]
conn.close()
check("four locations in location_meta with state, type and the feed's pin (Rulo is NE)", got == [("Axtell, KS", "KS", "Country Elevator", 39.87339, -96.25811), ("Denton, KS", "KS", "Country Elevator", 39.73221, -95.26988),
      ("Rulo, NE", "NE", "Country Elevator", 40.0537, -95.42888), ("Seneca, KS", "KS", "Country Elevator", 39.83491, -96.05462)], got)
snaps = db.get_snapshots("Ag Partners Coop", "Axtell, KS")
check("Axtell reads back one snapshot (source 'web') with its 5 rows through the app's own loader", len(snaps) == 1 and snaps[0].source == "web" and len(snaps[0].rows) == 5, [(s.source, len(s.rows)) for s in snaps])
db.upsert_snapshots([r.model_dump() for r in reqs.values()])
check("a second run the same day adds nothing (same row ids)", len(db.get_snapshots("Ag Partners Coop", "Axtell, KS")) == 1 and len(db.get_snapshots("Ag Partners Coop", "Axtell, KS")[0].rows) == 5)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
