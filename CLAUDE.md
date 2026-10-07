# JSA Basis Tracker

Cash grain basis tracking — scraped bid sheets, rail and river FOB, snapshots
and trends. The largest repo in the fleet; `app.py` is the whole dashboard and
the many `*_scraper.py` files feed it.

## Snowflake is the live database

`JSA.BASIS_TRACKER` is authoritative. `database.py` picks the backend:
Snowflake wins whenever `USE_SNOWFLAKE` is truthy, **even if `DATABASE_URL` is
still set**.

The Supabase Postgres database is a stale copy — as of 2026-09-06 Snowflake was
ahead on every table (`SNAPSHOT_ROWS` +9,715, `FREIGHT_HISTORY` +45,749). If a
number looks wrong, do not "fix" it by pointing back at Postgres.

**Supabase is retired (2026-09-18).** `_backend()` no longer selects Postgres — it's
Snowflake (prod) or local SQLite only — so a Snowflake misconfig now fails to an
empty SQLite (obviously broken) instead of quietly serving stale Supabase data.
`DATABASE_URL` is ignored by the picker (the Postgres SQL branches remain as dead
code, not yet ripped out); the `RIVER_DATABASE_URL` path was removed entirely
(River FOB reads Snowflake `RIVER_FOB.PUBLIC`). Both secrets can be deleted from
the Cloud apps, and the Supabase project decommissioned.

## This repo deploys as two separate Streamlit apps

Streamlit identifies an app by *(repo, branch, main file)*, so one repo serves
two:

| main file | app | password |
|---|---|---|
| `app.py` | full admin build | `APP_PASSWORD` set |
| `view_app.py` | read-only client build | **never gated** — an open link, whatever its Secrets say |

`view_app.py` forces `VIEW_ONLY=true` then runs `app.py` via `runpy`, so it
needs the same backend secrets. Leave `APP_PASSWORD` out of the view app's
Secrets — but if it ends up there (the two Secrets blocks are easy to re-paste
from one to the other, which is how the view link got a password prompt on
2026-10-02) it is **ignored**: `view_app.py` passes `_OPEN_VIEW_BUILD` to `app.py`
through `runpy`'s `init_globals`, and `_require_password()` skips the gate when it
sees it. The flag is an in-process Python global on purpose — no env var, Secret or
`VIEW_ONLY` setting can set it, so nothing can open the admin build (`app.py` run
directly). Every email button links to the view app, so it must stay open.

`_view_only()` in `app.py` hides everything that downloads or modifies data:
scrapes, exports, copy buttons, the River FOB update, snapshot deletes.

## Pushing to GitHub does not deploy

This repo moved from a personal account into the `JSA-Dashboards` org, and
Streamlit still has both apps registered under the old owner path. The webhook
fires, returns `200 OK`, and does nothing.

To ship: push, then **Manage app → ⋮ → Reboot app** — on *both* apps if the
change affects both. Allow 2–5 minutes.

## Secrets

`USE_SNOWFLAKE=1` plus the `SNOWFLAKE_*` block; `SNOWFLAKE_SCHEMA` is
`BASIS_TRACKER` here (safe to set — this is a single-purpose app, unlike the
portals where it must stay unset). `RIVER_DATABASE_URL` cross-reads the River
FOB Postgres database, which is **not** yet in Snowflake.

Also note `MASSIVE_S3_ACCESS_KEY` / `MASSIVE_S3_SECRET_KEY` for the futures
feed, and `APP_PASSWORD` on the admin build only.

The admin build also needs the four `GRAPH_*` keys (`GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`,
`GRAPH_CLIENT_SECRET`, `GRAPH_SENDER`): its email goes out through Microsoft Graph. They must sit at the
**top level** of the Secrets, above any `[section]` (TOML puts a key under the last header above it).
Without them `changes_report.send_email` falls through to the dead SMTP relay and fails with a 535 — on
2026-10-06 a whole evening went into that error before anyone noticed the Graph keys were missing. The Rail
Entry tab now warns up front when they are, and `send_email` names the missing configuration.

## Rail update email, and its catch-up job

A saved rail rundown is emailed (To kpostin@, BCC the JSA group) by whatever saved it: the admin app's Rail
Entry → Save all, or `post_rail.py --commit` on the droplet. Both go through
`rail_report.send_rail_update_email`, which logs every send (and `send_rail_recap_email` its recaps) to the
`rail_email_log` table (`rail_email_log.py`: one row per corridor — which posting date it showed and when the
board was read). A save made with the email switched off writes a `skipped` row; opening the Rail Entry tab
writes a `heartbeat`.

`rail_email_watch.py` is the safety net (droplet cron `*/10 6-22 * * *` Central, `deploy/run_rail_email_watch.sh`,
wrapped in `cron-alert`): any posting saved more than 15 minutes ago that no row covers is emailed as a normal
update (logged as `catchup`). A row covers a posting when it is for that corridor, shows that date or a later
one, and its snapshot was taken after the posting's last capture — so a re-save is uncovered again until it is
emailed. Because it is a group broadcast it errs towards silence:

- The first run only records a **baseline**; nothing saved before it is ever emailed by the job.
- It stays **unarmed** (logs what it would send) until the Cloud app has written the ledger after the baseline
  (the heartbeat, or a logged send) — an app that still runs old code sends without logging, and a catch-up
  would be a duplicate. After deploying this, **Reboot the admin app and open its Rail Entry tab**.
- What it sends is also remembered in `state/rail_email_watch.json`, so a failed ledger write cannot make it
  send twice; `state/rail_email_watch.off` (touch it) is the kill switch; `--dry-run` sends and writes nothing;
  `--force-armed --to you@... --no-bcc --grace-min 0` is a live test that emails only you.
- It exits non-zero (a `cron-alert` email) only when the same problem repeats (2nd, 12th, 72nd failing run), so an
  outage does not alert every 10 minutes. Its single rolling log is `/opt/basis-tracker/logs/rail_email_watch.log`.
- The warehouse is already busy most of the day (~27 credits/day, hourly), so the poll is effectively free; the
  job is limited to 06:00–22:50 and its queries are plain literals, so Snowflake's result cache answers them while
  nothing has changed.
- The Rail Entry tab's default Posting date is the **Central** date (`rail_corridors.today_ct()`), not UTC (after
  ~7 PM Central the UTC date is already tomorrow); `post_rail.py --date` defaults the same way.

Tests: `tests/test_rail_email_log.py` (decisions, ledger on a throwaway SQLite, the watcher end to end with a
simulated clock, the send functions), `tests/test_rail_entry_outcome.py` (the admin app headless),
`tests/test_send_email_dispatch.py`.

## Net Carry tab — modules and data

The 💵 Net Carry tab is a stack of small pure modules (each has a `tests/test_*.py`; run them with `python tests/<file>`):

| module | what |
|---|---|
| `net_carry.py` | the forward curve: basis vs one reference contract, interest, NC/front, monthly points, top of net carry |
| `net_carry_chart.py` | the River-style "Cash Fwd Curve" chart |
| `net_carry_compare.py` | several locations / corridors side by side, same reference + same interest clock |
| `return_to_carry.py` (+ `_data`, `_view`) | the Research Analyst's **Return to Carry** (return to storage) automated for corn **and soybeans** (one engine, a `Spec` per commodity: chain, base contract, roll months, horizon): harvest basis, weekly interest, roll spreads banked, by crop year, net or gross — and the report's front page, the **shipment-by-month table** (break-even basis, current/best bid and return for each shipment month Nov-Jul; soybeans Nov-Aug) |
| `carry_rate.py` | the interest rates: fed funds + 2.25% (Cost of Carry) and bank prime (the Return to Carry report), FRED with committed snapshots in `data/` |

`return_to_carry.py` reproduces the analyst's yearly workbooks (`JSA - Documents/Research Analyst/Misc/Return to Carry`); the
per-year quirks of those sheets (harvest-basis window, roll days) are kept in `WINDOW_OVERRIDES` / `ROLL_OVERRIDES`.
Its history reads `futures_prices` for 2006+ and `data/rtc_futures_1996_2006.csv` (extracted from the old sheets) before that.

**Interest = the tab's own logic (Kolten 2026-10-04: "use the same interest rate logic we have used on the other net carry
calcs").** The tracker's "Interest" switch defaults to effective fed funds on each date + 2.25% (`carry_rate.rate_for`), moved
by whatever the Net Carry rate box was edited by; bank prime (what the analyst's report charges) is the second choice, for tying
out to her numbers. History charges it as her sheets do (that week's rate ÷ 52 on the cash price, from the 3rd weekly bid); the
shipment table charges rate × days ÷ 360, like every other carry calc on the tab.

**Shipment-by-month table** (`return_to_carry.build_shipment_table`, `return_to_carry_data.shipment_table`): the analyst's
`NNcolcarryBA.xlsx`. Bought Oct 20 at the harvest basis b0; for each shipment date (20th of Nov-Jul) *Basis Cost* =
`b0 - (F_M - F_Dec) + (F_Dec + b0) * rate * days / 360`, *Current Basis* = the latest posted bid for that month, *Return* = bid -
cost, *Best YTD* = the best bid and the best (bid less THAT day's cost) since Oct 20. `F_M` is the report's "Current Futures"
chain (`chain_levels`): live contracts at their settlement, rolled ones hung from the next by the spread measured the last Wednesday
before the expiring month. Verified to 1e-13 against the analyst's 2026 Columbus template and her 2019-20 and 2024-25 St. Louis
sheets (`tests/test_return_to_carry_ship.py`). Forward bids come from the rail rundown's posted periods (`rail_fob`, which only holds
forward periods from Aug 2026 — older years are Spot only) or a location's snapshot rows; `parse_label` maps 'JFM'/'AMJJ'/'FH Dec'/
'Dec 1-20' to months (a month's own quote beats a package; the narrowest package fills the rest, as her template does), and a bid
quoted off another futures month than its column (JFM is quoted off Mar, the Mar column is in May terms) is moved by that day's
spread — her sheet types the raw 18 there. Before the weekly harvest bids start (first Wednesday of October) b0 is estimated from
the posted FH Oct / LH Oct / FH Nov bids and the table says so. Her 6-17-20 PDF types K/N as -8 where her weekly sheet measured -10
(Apr 29) — a page-1 vs weekly-sheet inconsistency in her files, not the engine. `shipment_table` hands the engine only the
futures up to the as-of date, so a past as-of date cannot use a roll spread measured after it.

**The user's own harvest basis** (Kolten 2026-10-06: "add the ability to apply your own harvest basis into the models, but default to the
calculated method"). A **Harvest basis** switch under the Interest one in `return_to_carry_block.render`: *Calculated* (the report's
method, the default) or *My own*, a number in cents vs the base contract (Dec / Jan) that starts at the calculated value and is kept
if the user flips back and forth (`nc_rtc_b0_kept_*`). It measures the crop year being tracked (`rtc.shipment_crop_year(asof)`): the
shipment table (`shipment_table(b0_override=)`: break-even, returns and the interest on futures + harvest basis; `tbl.b0_own`,
`tbl.b0_calc`) and, when that year has weekly bids, the history (`build_crop_year(b0_override=)` -> `cy.b0`, `cy.b0_own`, `cy.b0_calc`;
`run_history_noted(b0_overrides={year: value})`, built by `own_b0_map`): the headline card, the orange line, the bar and the year's row
(an OWN pill + legend). Earlier years keep their calculated basis, so the comparison with history is still the analyst's method. A
what-if box applies the number to EVERY crop year. In the weekly history the interest runs on each week's cash price, not on b0, so an
override shifts every gross and net return by (calculated - own) and the best WEEK does not move (`tests/test_return_to_carry.py`); the
shipment table's net break-even moves by (own - calculated) x (1 + rate x days / 360). Widget keys are
`nc_rtc_b0_{mode,val,all,kept}_<scope>|<grain>|<crop year>` — callers pass `scope=` (the tracker: the `ref` tuple; the portals: the corridor key
/ `river|<location>`), so a number typed for one location never follows the user to another. `tests/test_return_to_carry_block.py` drives the
block headless (AppTest) over a synthetic series.

**Soybeans** (Kolten 2026-10-05, "yes, do soybeans next"): the same engine run from `return_to_carry.SOY`, validated against the
analyst's `BeanCarry` workbooks (`Research Analyst/Misc/Return to Carry/BeanCarry/{Decatur,DesMoines,Hennepin,STL}CRY`, 2005-06 to
2022-23, 61 usable sheets). Chain Nov → Jan → Mar → May → Jul → Aug → next Nov (labels X F H K N Q x — the lower-case `x` is NEXT
November); the harvest basis is expressed against **Jan** (October's bids are quoted off Nov and moved to Jan by the Nov/Jan spread
measured the last Wednesday of October, so before Oct 28 there is no weekly average and the table uses the posted-quote estimate,
Oct and Nov bids weighted 4 : 3); the headline carry is Jan → Jul; weeks run to Sep 30; interest starts at the 5th weekly bid (the 3rd
in 2005-06 to 2008-09); the page-1 columns are Nov-Aug (Aug is quoted off next Nov, so its break-even carries the new-crop discount).
The sheets' own per-year departures are `SOY_WINDOW_OVERRIDES` (2015-17 left the 5th week out of the harvest average),
`SOY_ROLL_OVERRIDES` and `SOY_ACCRUAL_OVERRIDES`. The engine reproduces 75% of the comparable weeks to the hundredth of a cent; the
rest are rows where her own sheet is inconsistent, listed per sheet as `off` in `tests/test_return_to_carry_soy.py` (fixtures:
`tests/fixtures/rtc_soy_sheets.json`). Two data traps: the archive's symbol year digits cannot be trusted for soybeans (a January
bid is written ZSF19 for Jan 2020), so a tag is read by its month letter and the date (`tag_label`) and an off-schedule tag is moved to
the date's contract by that day's spread (`Spec.normalize_tags`; her sheets keep what was typed); and `futures_prices` has no soybean
near contracts before 2008 (ZSX07 is missing in Oct 2007), so `data/rtc_futures_soy_2005_2007.csv`, from her sheets, fills 2005 to Oct
2007 (`load_sheet_futures(root="ZS")`). Weekly-archive gaps drop a year rather than guess it: ADM Hennepin has no Oct 2007 to Aug 2008
bids (no 2007-08), ADM St. Louis 2023-24 / 2024-25 are sparse, ADM Des Moines 2010-11 is partial. The only soybean rail corridors are
'CN 105s Beans' and Palmetto "COL, OH Beans 90's" and they hold forward periods only since Aug 2026, so they get the page-1 table and
not yet the weekly history. **Line endings:** `app.py` is CRLF in the working copy (`.gitattributes` normalises to LF in the repo);
write it in binary mode or through the Edit tool, never a text-mode Python rewrite.

**River FOB = a third Net Carry location type, with the full history (Kolten 2026-10-05: "we should be able to do all the history
... the numbers are still all there ... look in the existing FOB sheet archive for the river", not in workbooks).** The River FOB
portal archives one sheet per as-of date in Snowflake `RIVER_FOB.PUBLIC` (`cif_history`, `freight_history`, `calendar_history`):
weekly since **2006-09-07**, 1,119 sheets. The portal stores only CIF and barge freight; FOB = CIF − tariff × freight / 2000 × bushel
weight (`fob_model`, owned by the river portal — keep this repo's copy identical; the 2026-10 sync added Lacon and the rule that a
0/blank freight means the river is closed, so there is no FOB). `river_carry.py` (pure, `tests/test_river_carry.py`) turns the loaded
archive (`river_fob_data.load_archive`, 3 queries) into the Net Carry tab's inputs: `curve_items` (one sheet's curve), `nearby_obs` (the
weekly nearby FOB with the contract the sheet maps that month to — the first column, or the month after a leading 'Spot', never a
later month) and `forward_quotes` (every posted month, labelled with its year, for the shipment table, which therefore works for any
past as-of date). A sheet's columns carry no year: the first month must start 0-2 months after the sheet's date (a stale header sets
the sheet aside — 2 soybean / 7 corn sheets of 1,119) and the contract year is the occurrence of the month letter nearest the delivery
month. The upper-river reaches (Quincy, Burlington, Davenport, Prairie du Chien, Savage) have no freight in winter, so their series
have gaps; Illinois River, STL, Ohio and the Lower Mississippi are complete. Every one of the 18 sheet locations gets 21 crop years of
corn and soybean Return to Carry from 2006-07 (soybean average best +33 to +47¢, corn +35 to +53¢ — Hennepin soybeans +38.4, STL corn
+43.6); wheat gets the Net Carry curve only. **Corn 2007-08:** `futures_prices` holds no front contract (Dec 2007) that autumn, so the Dec→Mar roll could not be measured
(no best return in any corn series until 2026-10-06); the 9 weekly Dec 2007 prices (Oct 3 - Nov 28) now come from the analyst's `07colcry.xlsx`
into `data/rtc_futures_1996_2006.csv`, and her Mar 2008 prices there equal the stored ZCH08 to the cent. The FOB series is the sell side, not an elevator's buy basis — the existing truck series differ from it
by a varying amount (the corn Hennepin series sits about 3¢ under FOB, interquartile 2-6¢; the soybean one about 11¢ under, interquartile
2-22¢), which is why the river history is shown as its own location type rather than merged into the elevator rows. `return_to_carry_block.py` is the whole Return to Carry section as one
Streamlit block (`render(obs=..., quotes=...)`), shared with the portals, which vendor it next to the `return_to_carry*.py` modules.

**Derived history for river elevators that only began posting in 2026 (Kolten 2026-10-06: "okay to add them, just highlight and note
that these are derived through the historical FOB river values, not actual basis history").** About 35 locations (ADM Havana, Morris,
Memphis, Quincy Barge Dock, Evansville, Mt. Vernon, Clinton; Cargill Havana, Beardstown, Meredosia, Cincinnati; CHS Havana/Beardstown,
Morris, Seneca, Quincy; CGB Joliet, West Memphis; LDC West Memphis; Bunge Cairo) have bids only since June 2026. `river_derived.py`
(tracker only; `tests/test_river_derived.py`) maps each to the River FOB sheet location whose reach it sits on (`FOB_REACH`) and estimates
its history as that reach's weekly nearby FOB less a **gap** = the median of (FOB − the location's bid) over every same-day, same-month
full-month quote pair since June (the FOB moved to the bid's contract by that day's futures spread). Accepted only with 40+ pairs, a
middle half no wider than 12¢ and a median no larger than 30¢; otherwise the location keeps no history rather than a misleading one
(left out: CHS Quincy Elevator corn at −61¢ ± 22, CGB Joliet soybeans −19, ADM Clinton soybeans 14 with a 10-25 middle half, Bunge Cairo
soybeans 10 with 5-18). Observed gaps, corn / soybeans: Havana 3-5 / 9-11¢, Seneca and Morris 0-1 / 7-9, Memphis 18-21 / 17-24,
Evansville and Mt. Vernon 8-10 / 10-11, Cincinnati 14 / 13-15. Each derived bid carries `'derived': True` and exists only for the weeks
before the location's own first bid; it is computed on the fly (`_cached_rtc_derived`) and NOTHING is written to the snapshots table, so
the Bids and Trends tabs never see it. The Return to Carry block marks it three ways: an amber banner (reach, gap, pairs, "derived through
the historical FOB river values, not actual basis history"), DERIVED / PART DERIVED pills in the by-year table (2025-26 is the mixed year)
and lighter bars with a legend. A constant gap cancels exactly in the gross return and moves the net return only by the interest on the
gap (under half a cent); the harvest-basis and summer-basis LEVELS carry the gap.

**Vendored modules (portals).** `sync_carry_modules.py <portal dir> [--river] [--no-return] [--check]` copies the Net Carry modules,
the Return to Carry modules and their data files (and `river_carry.py` with `--river`) into a portal. The rail portal's
`environment.sis.yml` pins Python **3.11**, where an f-string may not hold a backslash, a comment, a line break or its own quote
inside a replacement field (all legal from 3.12 on); `tests/test_vendored_syntax.py` scans every vendored file for them (the check
needs Python 3.12+, i.e. it runs here and not on 3.11).

### `futures_prices` was backfilled (2026-10-04)

The daily capture only began 2026-06-22. `backfill_futures_history.py` copied ZC/ZS (from 2006-11) and ZW/KE (from 2021-10)
settlements from `JSA.COST_OF_CARRY` into it (rows tagged `captured_at = 'backfill:…'`; re-runnable; undo with
`DELETE FROM futures_prices WHERE captured_at LIKE 'backfill:%'`). A past as-of date therefore gets its own day's curve,
not today's.

## Scrapers

One module per source (`adm_`, `chs_`, `cargill_`, `bunge_`, `scoular_`, …).
Most are plain `requests` + parse. The DTN-backed sites are the awkward ones and
are worth reading about before you add another.

### DTN is not one wall

DTN cash-bid pages hide the basis client-side — it is blank in the served HTML
and in print view. But every site wraps DTN differently, and the data layer is
usually reachable with plain `requests` once you capture the endpoint and key.

**How to capture a new one:** open the page in a browser, patch
`XMLHttpRequest.prototype.send` to record `/api/...` bodies, then call the
widget's own refresh function to re-trigger it. Read `window.__caps`. Adding a
site to an already-cracked platform is usually one config row, not new code.

### The cracked platforms

| Platform | Module | How |
|---|---|---|
| **VistaComm** (`vc-dtn` WordPress plugin) | `vistacomm_scraper.py` | `POST spacentral.vistacomm.com/api/v1/dtn` with a `License-Key` header — a per-site GUID exposed in the page JS as `spalicensekey`. Helpers `…/dtn-locations` and `…/dtn-commodities` need no body. |
| **AgriCharts `writeBidRow`** | `agricharts_md_scraper.py` | `*/markets/cash.php?location_filter=<id>`. Basis and delivery are literals inside `writeBidRow(...)`. Find the id in the page's `<option value=ID>`. |
| **AgriCharts `/bidlist`** | `agricharts_md_scraper.py` (`parse_bidlist_site`) | Rows print via `document.write()`, not `writeBidRow`. Futures come from the `quotevarNNN['ZCU26']` assignment emitted just before each row — take the **last** one in the preceding segment. |
| **Agrex "FarmCentric"** | `agrex_scraper.py` (`--agrex-only`) | ASP.NET GridView, plain requests. `<li class='cN'>`: c1 delivery, c3 basis, c6 futures month. c6 gives contract *and* commodity ("Sep 26 KCBT Red Wheat"), so read the symbol directly. |
| **aghost / ColdFusion** | `dtn_playwright_scraper.py` (`--dtn-only`) | `index.cfm?show=11` computes basis client-side with no clean JSON — the AJAX endpoints need session state and `requests` lands on a different default location. Render in headless Chromium and read the finished grid. |

### Gotchas that cost real time

- **VistaComm body types are strict** — wrong types return HTTP 500, not a
  helpful error. `columns` must be a JSON **array**, `locationid` an **int**,
  `formatting`/`charts` **booleans**, `commodity` a lowercase name.
- **Futures symbols** come back as `@C{yeardigit}{monthcode}` — `@C6U` → ZCU26.
  Year digit is 2020+d, bumped +10 if that would be in the past.
- **aghost column order varies between pages.** The extractor is deliberately
  position-agnostic: basis is the small signed decimal (`|v| < 2`, versus cash
  ~4.xx and tick-format futures like `438'2`); delivery is found by scanning
  backward from the `@` symbol. Do not "simplify" it to fixed indices.
- **AgriCharts: skip `basis == 0`** — those are months posted but not bid.
- **Playwright is `requirements-dev` and runs local-only**, guarded at 300s. It
  is not available on Streamlit Cloud, so that scraper never runs in the
  deployed app.

### Known-unbuilt

- **Ray-Carroll** (`ray-carroll.com`) is the same aghost family but a
  15-location co-op needing dropdown iteration per location × commodity.
  Bigger job, not started.
- **United Cooperative** is **not** DTN — it is StoneX/Stonehedge, served via an
  OAuth token exchange plus a streaming gateway. Not requests-scrapeable.
  Left sheet-fed deliberately.
