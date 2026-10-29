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

## Forward curves — one number per delivery month (`forward_month.py`)

A posting quotes a month several ways at once — 'Dec', 'FH Dec', 'LH Dec', and packages that span it ('JFM', 'Nov-Jul'). Every chart that draws a forward
curve from posted labels used to AVERAGE whatever landed in a month, so the chart disagreed with the board: on 2026-10-20 CSX Columbus posted Dec +16 and FH
Dec +13 and the emailed curve drew +14.5 ('+15'); November's +19 was drawn +10 (FH Nov -12, Nov +19 and the Nov-Jul package +22 averaged). Kolten, same day:
"for months that show a full month but also a FH or LH just default to the full month indication, only take the midpoint if its a FH and LH separate bids".
The rule (`forward_month.month_value`, tests `tests/test_forward_month.py`):

- a month's **full** bid is the month — its halves and day windows are left out;
- no full bid: the **midpoint** of the first part (FH, a window that opens the month) and the last part (LH) when both are posted; one part alone stands for the month;
- a **package** ('JFM', 'AMJJ', 'Nov-Jul', 'Oct/Nov') never overrides a month's own quote: it fills a month nothing of its own names, narrowest first, spread
  `CARRY` (2c) a month around its centre so its months average back to its bid (`rail_forward`; the Spread tab's `forward_quotes` has always filled this way).

A label is read by `return_to_carry_data.parse_label` (the shipment table's and the Spread tab's reading), with LP / FP / L/H / F/H (the freight sheets' spelling) =
last / first half and MP = a partial window (`forward_month.label_info`; parse_label alone would call all of them whole months). Used by: `rail_report._fwd_curve` (the emailed seasonal chart), the Rail
FOB tab's seasonal chart in `app.py` (the forward curve, and each month's history — a day's FH / LH / full bids become one value before the weekly average),
the Bids tab's seasonal forward points and the Spread tab's location forward line (`pick_months`), and `rail_spread._cash_by_month` (the Spread tab's forward
spread, both sides). NOT changed on purpose: the Net Carry tab's `monthly_carry` (a month is its BEST slot by net of interest, so the table's peak is never
hidden) and the shipment table's `shipment_quotes` (it already ranks full > window > LH > FH, one bid per month, no averaging). The chart's data labels still
round to a whole cent ('+15' for a 14.5 midpoint). Deploy: the emailed chart runs wherever `rail_report` runs — the droplet (`post_rail.py`, the catch-up
watcher: `git pull`) and the admin Cloud app (Reboot).

## The rail seasonal charts' 5-yr average is Olympic, the range is the true range (`seasonal_band.py`)

Kolten 2026-10-20 ("Check the UP Allen station range and average. It seems like they are too high" ... "I think we might have a year thats skewing the data ...
BN Hereford looks the same. I think we need to take an olympic avg approach to make cleaner"). UP Allen Station's 2022/23 had a Sep-Dec average of +59 against -12 to +27
in the other four years of the window (2021/22-2025/26), set the top of the band in 38 weeks of 52, and by itself lifted this week's 5-yr average from -7 to +7; the same
year is high on Group 3, Interior IA and Hereford. It is real market data, not a bad row (Allen's recorded bids were +58 to +85 in mid-2023), so the fix is in the statistic:
for each marketing week the highest and the lowest year are dropped and the average is the mean of what is left (`olympic_avg`; five years in the week are needed to trim, with fewer
the plain mean is used). **The range is NOT trimmed (Kolten 2026-10-26: "take out the ex hi / low on these and make true 5 year range. Keep the average line as we have it").** The band runs
from the lowest to the highest of EVERY year in the window (`range_lo` / `range_hi`), the freak year included, behind the Olympic average line; the first version had trimmed the range as well
(`olympic_lo` / `olympic_hi`, legend '5-yr range ex hi/lo') — those functions are gone, and Allen's 2022/23 is back in the band's ceiling. Wired into `rail_report._five_year_band` (the emailed
charts, rail and Inco; legend '5-yr Olympic avg' / '5-yr range (2021/22–2025/26)') and the Rail FOB tab's `_rband` in `app.py` (header and end label say Olympic avg; the caption says the gray band is the
true range of the last N completed crop years). With fewer than five years nothing changes: the plain mean and the full range, as before. `tests/test_seasonal_band.py` (rule, the band frame, and the
Vega-Lite spec the email hands the renderer).
**Not changed: the Bids tab's seasonal band and the Spread tab's band still use the plain mean and min / max** — say the word and they take the same helper.

**The BN corridors look back three years, not five** (Kolten 2026-10-21, after "Hereford looked historically high and skewed maybe": "use the last 3 years for the BN corridors").
`rail_corridors.band_years(market)` = 3 for the BNSF basis corridors (BN Hereford, BN PNW, BN PNW BE / CP, BN COBO, and any unregistered 'BN ...' market), 5 for everything else — BN Freight / the
shuttles ($/car) included. Used by `rail_report._seasonal_png` (`_hy`) and the Rail FOB tab (`_rwin_yrs`, labels and caption). Three years is under `seasonal_band.MIN_YEARS`, so nothing is
trimmed: a BN chart shows the plain mean and the min / max of 2023/24-2025/26 and says "3-yr average" / "3-yr range (2023/24–2025/26)". Why: both 'Dlvd Hereford' and 'Dlvd PNW' in the Corn
Basis master are 1,141 TYPED weeks (real data, unlike Allen) and rail_fob equals them, but 2021/22-2022/23 ran 150 / 160 at Hereford (174 / 166 PNW) against 100-108 (113-118) since, so the
five-year benchmark sat ~14c above what is posted (Hereford this week: Olympic 5-yr 109, last 3 years 99, posted ~95). `tests/test_seasonal_band.py`.

**Allen Station's history was an ESTIMATE, not Allen bids** (SA Master, `Corn Basis.xlsx`): `Est Domestic Allen` = the formula `NEGrp3 + 10` and `Est Mexico Allen` =
`NEGrp3 + 20` for ~1,046 of ~1,100 weeks, so every Group 3 swing was inherited. The real bids are col X 'Allen Station UP' — typed numbers, 145 weeks from 2023-05-31 to 2026-07-29 (21
weeks inside that span have none); the estimates were typed over only from 2024. Where both exist the real bid ran 15.6 under the estimate in 2023 and 10.7 under in 2024.

**`UP Illinois (Mex)` rebuilt on the real bids, 2026-10-21 (Kolten: "'Allen Station UP' is not calculated, its actual numbers. lets use this as the base line. I believe its more reflective
of Mexico than Domestic ... use Est Mexico Allen to back fill ... prior to this we need to flag as estimated or even consider removing").** Its 1,139 weekly Spot rows (source 'manual', period
'Spot'): the 145 actual weeks hold the actual bid (88 of them moved: 2023 by -26 on average, 2024 by -28, 2025-26 by -2) and `bid_raw` NULL; the other 994 weeks (everything before 2023-05-31 and the
21 gaps since) keep `Est Mexico Allen` and carry `bid_raw = 'est'`. **`bid_raw = 'est'` is the estimated-week flag** (`rail_corridors.EST_FLAG`): never shown as text (`_disp` in `rail_report` and the
Rail FOB board skip it and tag the number "est"), counted by the seasonal charts — the Rail FOB tab's caption and the emailed chart's subtitle say "N of M weeks behind the 5-yr average and range are
estimates" with the corridor's note (`rail_corridors.ESTIMATE_NOTE` / `ESTIMATE_SHORT`). Updated in place by MERGE ... WHEN MATCHED (no insert, `captured_at` untouched, so the droplet watcher sees
nothing); the old rows are in `Claude Code\_backups\up_illinois_mex_spot_before_actual_baseline_2026-10-21.json` (restore = merge those bids back and clear `bid_raw`). Effect on the Olympic 5-yr
average (Sep-Dec / this week): +22.3 / +4.7 before, +17.3 / +1.3 now; on the 145 actual weeks alone (three years: 2023-24 to 2025-26, so no trimming) -3.1 / -9.0 with a band of -17 to 0.
Whether to keep the estimated weeks in the average and range, leave them out, or delete them is still Kolten's open call. Tests: `tests/test_estimated_weeks.py`.

**`UP Illinois (Dom)` = Allen Station (Mex) less 5 cents from 2023 on (Kolten, same day: "for Allen Domestic have it as 5c discount at all times back to 2023").** Its 187 weekly Spot rows
dated 2023-01-01 to 2026-07-29 were set to the Mex bid - 5 (2023 moved -5 on average, 2024 -12, 2025-26 -4), `bid_raw` 'est' where the Mex week is an estimate (42 of them) else NULL; the 952 weeks
before 2023 keep Group 3 + 10 and are flagged 'est' (nobody has said what they should be); the live UP rundowns after 2026-07-29 are posted bids and stay as posted. Backup
`Claude Code\_backups\up_illinois_dom_spot_before_mex_minus_5_2026-10-21.json`; same in-place MERGE as the Mex rebuild (`captured_at` untouched). The Dom caption says the 2023-on weeks are a rule, not
posted bids. Heads-up given to Kolten: UP's live rundowns have Mex 11-12 over Dom (median +7 since August), a wider gap than the 5-cent rule.

**The estimated weeks are HIDDEN for now (Kolten 2026-10-22: "side bar that still but hide data that is pure estimate at least for now").** Keep / exclude / delete stays open (sidebarred). Meanwhile
`database.get_rail_fob_all` / `get_rail_fob_dates` / `get_rail_fob` leave out every row with `bid_raw = 'est'` — 1,988 rows, 994 each for `UP Illinois (Mex)` and `(Dom)`; `rail_fob` itself is untouched — so
everything that reads the archive sees Allen from 2023-05-31 only: the emailed chart, the Rail FOB tab (board dates, seasonal chart), the Spread tab, Net Carry and Return to Carry (Allen Station's Return to
Carry history is now 2023-24 on — three completed crop years and the one in progress, checked on live data, instead of 21 — and its 'average best net return' is a three-year number). `RAIL_SHOW_ESTIMATES=1` in the environment (or `include_estimates=True` on a call) brings them back; the 'est' tags, captions and chart subtitle above are dormant,
not deleted (the desk's '?' and every posted bid are kept: the clause is `bid_raw IS NULL OR bid_raw <> 'est'`). What had to follow, for the corridors that have hidden weeks ONLY (checked on live data:
of every corridor the emailed report charts, just the two Allen ones change; UP Interior IA, UP Freight and a few tiny corridors have part-years too, and are deliberately left as they were): a prior
marketing year is one of the years behind the Spot band only with ≥26 weekly values (`seasonal_band.full_years`; Allen's 2022/23 is 8-14 actual weeks, +58 to +85, and would lift June-August alone),
the legends and captions count the years actually used ("3-yr average", "3-yr range (2023/24–2025/26)", the Rail FOB tab's caption), and the emailed chart and the Rail FOB tab say why the history stops (`rail_corridors.estimate_hidden_note`:
"Allen Station's actual bids begin May 2023; the estimated weeks before it (Group 3 + 20) are not shown"). Live effect on Allen Mex's band, week 8: average +1.3 (range -10 to +14) with the estimates,
-9.0 (-17 to 0) without; July +47 -> +21. Not changed: the Bids / Spread tab bands (still plain). Other copies of this: the Rail FOB portal's `rail_data.py` has the same filter; the admin portal's
`apps/basis_tracker` mirror gets it on its next sync. Tests: `tests/test_estimated_weeks.py` (the loaders on a throwaway SQLite, the chart note), `tests/test_seasonal_band.py` (`full_years`, a 14-week year).

**A bad bid is corrected by UPDATE, never by `save_rail_fob`** (it re-stamps `captured_at`, and the droplet's catch-up watcher would read the re-save as a new posting and email the
group). 2026-10-14 BN Hereford 'Oct' bid 210 (one-key slip for 110; the same posting's LH Oct is 108/113, FH Oct was 110 on 10-08) was set to 110 on one row, original kept in
`Claude Code\_backups\bn_hereford_2026-10-14_oct_bid_210.json`; it had lifted the week-7 spot average to 132.7.

**A spread note is not a spot bid** (Kolten: "fix the BN PNW as well"). BN PNW's 2026-10-19 posting opened with 'Spread-Sell FH Nov CP / Buy FH Nov ; offered' at -2, and every "nearest
period with a bid" rule took it as the spot (the week-7 average read 76 against ~110). `return_to_carry_data.is_spread_note` (a label with the word 'spread'; `forward_month.is_spread_note`
re-exports it) is now skipped by `obs_from_rail` / `quotes_from_rail` (Return to Carry, the Spread tab's spot line and forward quotes), `forward_month.label_info` (forward curves),
`rail_report._nearest_bid_cell` (the emailed spot line and the table's year-ago fallback row), the Rail FOB tab's seasonal history and board spot row, and the Net Carry items
(`net_carry_compare.rail_items` with its own inline regex — that module is vendored into every portal, `return_to_carry_data` is not under `--no-return`; and the corridor path in `app.py`).
The row itself stays on the board and in the email table as posted. 'Sellers Oct' / 'Buyers Sep' (BN COBO's own line format) are not spread notes. `tests/test_spread_note.py`.

## Spread tab — rail corridors on either side

The 🔀 Spread tab charts Location A − Location B (the front-month spot basis by marketing week against the 5-year range, with the two
forward curves' spread as red points). Rail corridors (Kolten 2026-10-12: "show a spread against rail corridors like UP Allen Station")
are in both pickers, after the locations, labelled `🚂 Rail · UP Allen Station (Dom)` — type "rail" or a name. `rail_spread.py` (pure;
`tests/test_rail_spread.py`) is everything that is not drawing:

- **Which corridors:** every non-freight market in `rail_fob` (manual + Palmetto) with 2+ posting dates for a commodity, minus what the Rail FOB
  board hides (`HIDDEN`: BN COBO Buyers, the Palmetto corn markets). Freight lines ($/car) are never offered. Allen Station is the stored market
  `UP Illinois (Dom)` / `(Mex)`; `DISPLAY` holds the picker names. A rail side is the pair `(rail_spread.PROVIDER, market)`, so the tab's helpers
  (`_spread_series`, `_spread_grains`, `_spread_fwd_side`) dispatch on the provider; the rest of the chart code is the location-vs-location code.
- **The line:** `spot_series` = `return_to_carry_data.obs_from_rail` — the weekly 'Spot' row (the SA Master history, through mid-2026), else the nearest
  forward period with a bid — the Rail FOB tab's Spot series and the Return to Carry tracker's. It merges with the other side on the exact posting date,
  like any pair: the deep-history locations were backfilled from the same weekly (Wednesday) sheet, so ADM Hennepin vs Allen Station overlaps on 1,141 of
  1,166 dates (2004 on). It is the basis as posted against the nearby contract — not converted, like every series in the app.
- **The red forward points are converted to cash first.** A corridor quotes December off March (ZCH, the desk rule) and an elevator off December (ZCZ), so
  the raw December figures sit on different futures months — 15¢ apart on 2026-10-09. `forward_spread` adds each bid's own futures price (the stored curve of
  the later of the two posting dates, `_cached_futures_curve_for`) and subtracts; where both quote off one contract that equals the plain basis difference.
  A corridor's curve is its latest posting mapped to delivery months (`return_to_carry_data.parse_label`): a month's own bids beat a package
  ('JFM' for Jan-Mar, the narrowest first) and a month is ONE value per side (`forward_month`, below: its full-month bid, else the midpoint of its FH and
  LH bids); 'R' (respective-option-month) packages and untagged cells are left out; short tags ('CU', 'SX') take rail_paste's roll. No forward points when the two curves are more than 10 days apart or no common month can be priced — the caption under the chart says which.
- `apps/basis_tracker` in the admin portal is a generated mirror (`bt_` prefix on every sibling module): the sync renames `rail_spread` by itself.

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
(an OWN pill + legend). Every other year keeps its calculated basis from the harvest window, so the comparison with history is unchanged.
**Own = this year ONLY (Kolten 2026-10-19: "just change the current year's calculations ... it's just this year's basis")**: the every-year
what-if box was removed (`own_b0_map` maps only the tracked year), and the controls read as two parts — the radio "This year's harvest basis
(2026-27)" (Calculated / My own) and, under a "Harvest window for the history — every other crop year, and 2026-27 when Calculated" heading, the
Start week / Number of weeks boxes, which stay while My own is in use so the user can set both at once. In the weekly history the interest runs on each week's cash price, not on b0, so an
override shifts every gross and net return by (calculated - own) and the best WEEK does not move (`tests/test_return_to_carry.py`); the
shipment table's net break-even moves by (own - calculated) x (1 + rate x days / 360). Widget keys are
`nc_rtc_b0_{mode,val,kept}_<scope>|<grain>|<crop year>` — callers pass `scope=` (the tracker: the `ref` tuple; the portals: the corridor key
/ `river|<location>`), so a number typed for one location never follows the user to another. `tests/test_return_to_carry_block.py` drives the
block headless (AppTest) over a synthetic series.

**Ship by — a study that stops before the season does** (Kolten 2026-10-12: "limit how late the study goes ... someone with a ground pile that
at the latest has to ship by March ... default to best with no limits"). A **Ship by** box under the Harvest basis switch
(`_ship_by_choice` in `return_to_carry_block`): *No limit (through July / September)* — the default, the whole-season page byte for byte —
or the END of a month, November to June for corn and to August for soybeans (`rtc.ship_by_months`; the season's last month is the no-limit
choice). It is a pure trim of the finished history, not a second engine: `rtc.limit_crop_year(cy, month)` keeps the weeks up to
`rtc.study_end(crop_year, month)` — the SAME `Week` rows, because no week's return depends on a later one — and picks again from them the best
net / gross (the earliest wins a tie; the best can only fall or stay, and never moves later), the best summer basis (None when no week reaches
the July contract, so the by-year table drops that column), the carry banked (`season_carry` = the last week's cumulative carry, `carry_tag` =
the contract it is quoted off: 'Dec → May, banked by Mar 25' instead of the season's Dec → Jul) and `complete` (the last week within a week of
the end). `rd.limit_history` maps it over the history; `summary_rows` carries `ship_by` / `carry_tag`. The shipment table is cut with
`rtc.limit_shipment_table(tbl, month)`: the columns whose shipment date (the 20th) is on or before the end, 'best' picked among them, and its
futures-carry card counting only the spreads up to the last column's contract. The headline ('Best return by Mar 31' / 'Best so far, by
Mar 31'), the by-year table (a note above it), both charts (the season chart's axis and month ticks stop at the end, with a subtitle line), the
method caption and the how-it-works text all say where the study ends. The choice is the user's constraint, not the location's, so its key is
per grain only (`nc_rtc_ship_by_<corn|soy>`) and it follows them from location to location, mirrored in `..._kept` while the box is not drawn
(a freight line, a location with no history). Tests: `tests/test_return_to_carry.py` (the trim, on a synthetic year and on the analyst's
sheets), `_ship` (the table), `_soy`, `_view`, `_block` (the page, headless). Allen Station corn, 2026-10: the average best net return of
its 21 completed years is +39.5¢ with no limit, +20.5¢ when it has to ship by the end of March, +34.3¢ by the end of June.

**Harvest window — which weeks the calculated basis averages** (Kolten 2026-10-12: "we default to establishing corn basis as of Oct and for 7
weeks or how is the logic? ... yes, add start week and number of weeks"). Two number boxes under the Harvest basis switch,
**Start week** and **Number of weeks** (`_harvest_window_choice` in `return_to_carry_block`), choose the weekly bids the CALCULATED harvest basis
averages. **Week 0 = the first Wednesday on or after Sep 1** (Kolten 2026-10-19: "start with week zero as Sep 1 rather than Oct 1";
`rtc.first_september_wednesday`; until then week 1 was the first Wednesday of October and September counted back 0, -1, -2, -3). The engine's
weekly rows still start at the first Wednesday of October (index 0), which is week 4, or week 5 when September has five Wednesdays
(`rtc.report_start_week`: 5 in 2015, 2020, 2021, 2026; 4 in 2022-2025); `rtc.window_index` turns a window's first week into that year's row.
The default, (that week, 7), is the report's window and keeps its year-specific exceptions (`WINDOW_OVERRIDES`: six weeks in 1998-2001, 2010
and 2015, later starts in 2009 and 2019, September in 2012); any other pair is the SAME weeks, counted from September, in every crop year (so a
row earlier after a five-Wednesday September) and replaces those exceptions. No single pair is the report's window in every year any more, so
the block's default follows the tracked crop year (`window_default`) and its `_kept` mirror holds None while the boxes are at it —
`build_crop_year(harvest_window=(first week, n))` (-> `cy.b0_window`, the pair as typed), threaded through
`rd.shipment_table` / `run_history(_noted)` (`tbl.b0_window`, `summary_rows` `b0_window`); the block hands the engine None at the default. A window
that opens at or after the report's changes only WHICH BIDS are averaged: interest still starts at the report's third (soybean: fifth) weekly bid and
the roll spreads are the same, so every weekly return moves by (report's average - window's) and the best WEEK does not move; a window that opens
after the interest clock says so under the boxes. **A window that opens EARLIER moves the purchase with it** (Kolten 2026-10-19, chose "purchase moves
with window" when asked to "start this chart in Sep"): the interest and the returns start at the window's own 3rd (soybean 5th) weekly bid — where the
report's sits in its own window — so the weekly rows begin there, in September, as negative row indexes (`cy.purchase` = that Wednesday; the
shipment table's purchase date too, `build_shipment_table(purchase=)`; "bought Sep 23"). It applies to every crop year, so a September window
changes each year's returns and best, not just the average; the default and later windows keep the report's Oct 20. **The season chart starts in
September**: `seasonal_points` / `_month_ticks` count weeks from the first Wednesday of September (row + `report_start_week`), so the axis runs Sep
→ Jul (soybeans Sep → Sep) for every window, empty until the first return. Checked 2026-10-19 on River FOB's default location (local `?view=1`
run): weeks 1-5 -> purchase Sep 23, the 2026-27 line from late September; My own -5 moved only 2026-27 (+30.6 -> +18.6), the other years untouched;
moving the window with My own on moved only the other years. The posted-quote estimate before the weekly bids belongs to the report's window, so a window of the user's has none ("waiting for the bids of
weeks N to M"); for soybeans October's bids need the Nov-Jan spread (measured Oct 28), so a custom bean window has no basis until then (the table
says why). The boxes stay when My own is chosen: the window still sets the calculated number the box starts from and every earlier year's basis. The
choice is the user's way of establishing a basis, so it follows them from location to location (`nc_rtc_win_<corn|soy>_{start,n,kept}`).

**Best return = the best LOCKABLE return, nearby or forward** (Kolten 2026-10-19: Greenville corn's headline showed the nearby bid's best while
the shipment table's January was higher — "best return on the second chart should be the most active best return that can be locked in at any
time ... you can sell to lock in this value, not have to be able to ship it to execute"; he chose "best lockable, any sale" + "today's forward
curve" on the chart). The weekly history (the report's measure) prices only the NEARBY bid each week. A forward month is lockable the same day —
sell the forward bid, roll the hedge to its contract at that day's spread, carry the interest to its shipment date — which is exactly the
shipment table's return (bid − that day's break-even). `rd.forward_best(cy, quotes, ...)` rebuilds a crop year's shipment table at its end (or
the as-of date / Ship by end) with `build_shipment_table(best_from=)` = the first bid of the harvest window (or the purchase if earlier), so a sale
locked before the report's Oct 20 purchase counts the same way in every year (the tracked year's sale TODAY included); `b0=` lets the tracked
year use the page's harvest basis (it can be the posted-quote estimate). `rd.lockable_bests` takes the higher of that and `cy.best` (a tie goes to
the nearby) per year; `summary_rows(lockable=)` puts it in `best` / `best_date` with `best_kind` ('nearby' | 'forward'), `best_ship`,
`best_nearby`, `best_forward`. The headline ("January shipment sold ahead, locked Oct 19"), the by-year table (`vw.best_when`: "Oct 19 · Jan
ship", plus a legend line and a count of years with no forward bids), the bars' tooltip, ranks and averages all read it. The season chart takes
`forward=` (today's 'return today' by shipment month, open orange dots on a dashed line at each shipment date) and `best=` (the dot sits on the
forward month's shipment date). Displayed shipment table unchanged (its 'best YTD since Oct 20' rows still start at the purchase). Forward bids
exist in the River FOB archive since 2006; elevators (snapshot rows) and rail corridors only from 2026, so their earlier years are the nearby's
best. Interest conventions differ slightly between the two (weekly rate/52 on the cash price vs rate × days/360 on futures + basis). Greenville
corn 10-19, report window: nearby +4.3 vs January forward +16.9; past years split both ways (2025-26 Jul forward +41.1 vs nearby +32.0;
2019-20 nearby +34.6 vs forward +27.2, the July spot rally came after the forward months ran out). Tests: `tests/test_return_to_carry_ship.py`
(the lockable section: synthetic year, the window-start rule, ship-by, tie, the chart points and dot).

**Two fixes the same day (Kolten: "these two charts don't seem to align"; his 2025-26 bar sat ~9c above its line's peak).** (1) `rebase` moved a
bid to the column's contract with RAW settlements while the column's break-even uses `chain_levels`, where a rolled contract hangs from the next
by its frozen spread: on Jul 15 2026 Greenville's July bid (quoted off Sep) was moved by the expired July's last price (Sep − Jul 9.75) against a
frozen 1.75, ~8c of return that was not there. `build_shipment_table` now passes the day's levels (`rebase(levels=)`); live contracts are
unchanged, so the current year's table is the same. (2) A bid counts toward a month's best only on or before its shipment date, and
`forward_best` counts only DEFERRED sales (`build_shipment_table(deferred_only=True)`: before the shipment month begins) — a July bid sold in July
is the nearby sale, and comparing the two only compared the report's two interest conventions (weekly rate/52 on today's cash price vs rate ×
days/360 on the purchase cost: 2-4c by July). After both, River FOB corn/soy at Greenville, STL, Hennepin, Memphis: the forward wins 3-9 of 21
years, mostly by 0.5-5c (2007-08 Jan->Jun up to +12), and the median year's forward best is 1.5-6.5c under the nearby; the bars equal the lines'
peaks except where a deferred sale really paid more. 2026-27 (mid-October, weak spot) forward wins by 12-30c.

**September bids quoted off CU (corn).** A window that reaches back into September can hold bids quoted off SEPTEMBER futures — the expiring old-crop
contract — while the base is Dec (Kolten 2026-10-12: "for months that start in September some will have this month basis the CU and we will need
to roll this accordingly. Use the last day prior to first notice day to establish the spread level to roll at"; "for several locations the all fall
bid or NC or New Crop bid is already basis the CZ. Its only for areas really on lower Mississippi river and the south ... Greenville is an
example on river fob"). So the roll is TAG-DRIVEN: a bid tagged the September contract of its own year, dated in the crop year's first calendar
year (raw tag 'CU', or 'ZCU' + the crop year's digits), is moved to Dec by `basis - (Dec - Sep)`, the spread being ZCZ - ZCU on `sept_roll_day(Y)` =
the weekday before the September contract's first notice day (the last business day of August: 2026 -> Fri Aug 28, 2025 -> Thu Aug 28) via
`sept_to_dec_spread`, stepping back over a gap like every futures lookup and frozen for all of that year's September bids. A bid already quoted off
Dec — untagged (`infer_tag` reads Sep-Nov as Dec) or tagged CZ, which is what the elevator series and every rail corridor post — is left alone;
'ZCU' of a later year (the chain's own September, a year out) is not rolled; and when the spread cannot be measured (no end-of-August prices:
2005-2007) the CU bid is left OUT of the average, never guessed. The block's caption shows the level in use ("2026-27: Dec - Sep = +24.50¢, so a
basis of +10 vs Sep reads -14.50 vs Dec"). In the River FOB archive the sheet whose first column is Sep tags that September's first sheet(s) U for
every location — one sheet in 2008, two in 2009, one each in 2013, 2021 and 2026 (checked 2026-10-12 on Cairo, Greenville, Havana, Hennepin, Memphis
and Peoria) — so those are the bids a September-start window rolls; the default window never reaches September, so the report's numbers do not change.
Soybeans have no September contract and no roll. Tests: `tests/test_return_to_carry.py` (roll day 1996-2040, the spread, rolled vs left alone,
the window vs the report), `_ship` (the table), `_soy`, `_view` (the words), `_block` (the page, headless; the +20 September case).

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

### The daily capture was a day late (found 2026-10-12, fixed from 2026-10-13)

The old `run_futures_capture` stored "the latest Massive flat file" under the run's date. Those files land ~9 PM Central,
so at the 3:45 PM run the latest was **yesterday's** session (the log said so: `session 2026-10-08` ... `stored ... for
2026-10-09`). `audit_futures_dating.py` (read-only; compares every row with Massive's 1session bar of its date and the two
before) found every daily row from 2026-08-31 to 2026-10-09 a day late; the ADM-only rows before that (06-22 to 08-27) are
right except 06-22, 07-17, 08-17 (runs at night / before the open) and three Saturday-dated runs (07-11, 08-29, 09-12) holding
Friday's settle. The backfill (2021-10 on) is right. Massive's REST bars start 2021-10.

**`futures_capture.py` now:** every bar names its session (`session_end_date`), so a price is only ever filed under its own
date. A closed session (after 1:20 PM CT) is stored at its settlement (`captured_at` `massive:settle@<UTC>`), else its close
(`massive:close@`, within ~1¢), and each run re-reads 14 days of sessions, so the next run upgrades a close and fills a
missed day. It owns only dates it wrote (a date holding any other row is left alone) and never writes before
`FIRST_DAY` (2026-10-13). ADM's live quote (`adm:last@`) fills only a symbol Massive lacks, today, between the close and
the 7 PM reopen. `python auto_import.py --futures-only` runs just the capture. Do not use `/snapshot` for this: on
2026-10-12 its `previous_settlement` skipped Columbus Day, and the 10-12 flat file had no grains at all.

`repair_futures_dating.py` is the one-off rebuild of 2026-06-22 to 10-12 from the bars (dry run by default; `--apply
--backup <csv>`; `--restore <csv>` undoes it; rows tagged `repair:massive-settle@`). Tests: `tests/test_futures_capture.py`.

### Trading days = CME's grain calendar, not the federal one

`grain_calendar.py` (`tests/test_grain_calendar.py`): grains close on New Year's, MLK, Presidents, Good Friday, Memorial,
Juneteenth, Independence, Labor, Thanksgiving and Christmas (Sat → Fri, Sun → Mon, New Year's on a Saturday closes
nothing) and trade on Columbus and Veterans Day. `auto_import`'s guard and the Summary tab's "1 / 2 days ago" use it; the
federal check had skipped the whole import on Columbus Day 2026-10-12. A one-off closure goes in `EXTRA_CLOSED`.

### The daily pull is checked, retried and reported on what was STORED (`pull_health.py`, 2026-10-22)

Kolten 2026-10-21: "For the days that have a basis pull fail are we checking and correcting where needed?" Before this: alerted, never corrected, and the
alert counted rows PARSED, not stored — the Snowflake JWT outage of 9/29 and 9/30 ran rc=0 with 1 location stored of ~1,100 and an email naming five empties;
no scraper was ever re-run; the same four dead ones (ZFS, VistaComm, DTN, Georges) were mailed every day. A day not pulled cannot be pulled later (a site shows
today's bids), so the check runs inside the run. `auto_import.__main__` calls `_pull_health_check` right after `run()` and BEFORE the Changes email (a retry
that fills a hole lands in it); `pull_health.post_run` does the rest, all of it pure and tested except one query (`tests/test_pull_health.py`, 139 checks):

1. **Verify** (`load_coverage` → `evaluate`): distinct (provider, location) stored today vs a normal day = the median of the last 10 PULL days (a date is a pull day
   when it stored ≥50% of the window's median; Inco's carried sheet alone is not). Run: <50% = **failed**, <95% = **short** (a normal day is within ~2%; 10/2-10/8 lost
   57 locations = 95%). Provider: normally there (stored on ≥8 of the 10 days) AND ≥5 usual locations: nothing today = **missing**, <80% of usual = **short**. The one-plant
   ethanol sites skip a day now and then, so providers under 5 locations are left to the **stale list**: locations that missed the last two pull days, seen in the last 60 days,
   grouped by provider. Fewer than 5 pull days of history → not judged. **Backtest** (`python pull_health.py backtest`, live Snowflake, 42 trading days since 8/24): finds
   every lost day (9/2, 9/8, 9/29, 9/30, 10/12 failed; 8/28, 9/9, 9/11, 9/24, 10/2-10/7 short) and the provider drops behind them (Cargill's 76 locations on 9/24, Andersons 10/13);
   nothing flagged on a normal day; ~20 mails in 42 days under the policy below, against one a day.
2. **Retry once**, after a 90 s pause, sequentially, within 20 minutes in all (`retry_scrapers`; the `_HEALTH` entry is replaced, tagged `retried`): every scraper that
   crashed / was abandoned / returned nothing, the scraper behind each missing or short provider (`scrapers_for`: same name ignoring case and punctuation, or a prefix), and, if the
   whole run failed, every scraper. **Not retried:** one that failed its last 2 runs in a row (the books, below), one whose abandoned thread is still running (`_THREADS`),
   anything when the database could not be read. The first run has no books, so it enters whatever is failing as already persistent (`seeded`, "failing since X or earlier").
3. **Verify again**, update `state/basis_pull.json` (gitignored; per machine): runs in a row each scraper failed, the issues open at the last alert (`scraper:`, `provider:`,
   `stale:`, `run:` keys), retries that worked, last run / alert / digest.
4. **Tell the operator** (CHANGES_EMAIL_TO, kpostin@; never the group): when the run failed, something is **new**, something **recovered**, the run failed at first and the retry fixed it,
   on **Mondays** (a digest of everything still open, the stale locations and the week's retries that worked) and the first time. A day with only the known issues is quiet;
   `PULL_HEALTH_ALERTS=daily` mails whenever anything is open. A mail that fails to send is retried next run (the issue list is only updated after a send).
5. **Exit code 2** when the run stored <50% of a normal day or the database could not be read to check (3 reads, 20 s apart), after the Changes email and client reports have gone, so
   the droplet's `cron-alert` mails and healthchecks sees a failure (9/29 and 9/30 were rc=0).

Switches: `PULL_HEALTH=off` or the file `state/basis_pull.off` → the old `send_scraper_alert` runs exactly as before; `PULL_HEALTH_RETRY=off` keeps the check and mail, no retry. A run that
enabled <85% of the scrapers (`_RUN_INFO`, counted from `run()`'s flags; the droplet's `--no-landus` is 44 of 45) is not judged. Tools: `python pull_health.py report [--date D]` (verdict + stale
list, read-only), `backtest [--days N]`, `state`. Things to know: snapshots are filed under the UTC date of the run, which is what the check reads (a run that straddles 19:00 Central would split); a scraper
that finishes after its budget keeps writing in its daemon thread, so a provider can look missing a moment and then fill; the Landus/ZFS/Georges/Jennie-O/CHS stale lists are real
(Landus is `--no-landus` on the droplet). Deploy = `git pull --ff-only` on the droplet; `state/` is created on the first run, which mails once ("Basis pull check is on — N open issues") and is quiet after.

## Map tab — production heat under the pins, pins coloured by basis (`heat_layer.py` + `heat_build.py` + `map_basis.py`, 2026-10-27)

Kolten 2026-10-27, after the production heat map had first been built in rma-production-map's own dashboard: "No I am wanting this on the basis dashboard" (and, earlier, "Lets start with production
concentration map but we will trend towards the basis on the map concept"). The 🗺️ Map tab has a **🔥 Production heat** checkbox (off by default; the view build has it too): NASS county production for the
commodity picked in "Commodity (tooltip)" (corn, soybeans, wheat; any other says so) drawn as a deck.gl HeatmapLayer UNDER the basis pins, with Crop year, Smoothing (km), Colors (Heat / Green) and a Pins
toggle (untick it to see the heat alone). The pins get a white edge while there is heat under them. Under the map: the gradient legend and a caption (what is drawn, how much NASS published, how the rest was
filled, which states are left off). `app.py` only places the controls (about 30 lines in the Map tab, CRLF file: patch it in binary mode); everything else is `heat_layer.py`, pure and tested.

**It runs on three small committed files** (`data/heat/`, 430 KB): `county_lattice.csv.gz` (fips, lon, lat: a 0.1-degree lattice of points inside every county), `county_production.csv.gz` (crop, year, fips, value, est:
NASS bushels, corn / soybeans / wheat, 2021-2025) and `county_production_meta.json` (per crop-year: official, drawn, published_pct, estimated_pct, left_off). So the app needs no NASS key, no county shapes and no
shapely, and nothing new in the Cloud Secrets. A county's value is divided evenly over its lattice points (deterministic; random dots speckle), so the heat follows county shapes and adds up to the NASS number. The
smoothing is asked in km and turned into pixels for the Map tab's opening view (`radius_px`): the HeatmapLayer's visible kernel is only about a third of its pixel radius, and a pixel radius does not follow the
zoom, so zooming in tightens the blur. pydeck cannot serialise a DataFrame under pandas 3, so the layer's data goes in as records.

**NASS publishes fewer county figures every year, so the builder completes each year's counties (`heat_build.fill_withheld`).** Published counties held 90% of the US corn crop in 2022 and 73% in 2024 and 2025
(Illinois 100% -> 63%), and NASS published only 2% of 2024 wheat's county figures at all (58 rows against 1,006 in 2023). Scaling the published counties up to the state total (what the S&D tab of
rma-production-map does) moves the hot spots to whichever counties NASS happened to publish, so the builder keeps every published figure and spreads what a state's published counties leave of NASS's official
state total over its withheld counties, by their share of the state in the latest of the 5 earlier years NASS published them. Every state then adds up to NASS's total; a state with no county at all (OR, NJ, MT, FL,
WY, NM for corn: about 0.4% of the crop) is left off and named in the caption. A year NASS published under half of carries a warning ("mostly an estimate", `MOSTLY_ESTIMATED`). This is a copy of rma-production-map's
`heat_map.fill_withheld` (tested in both repos), minus that app's RMA fallback, which is why this one looks back 5 years and that one 3.

**Refreshing the data.** NASS's county figures for a crop year arrive after the January Crop Production Annual Summary; then `python heat_build.py production --years 2022-2026` (a NASS key: `--key`, `NASS_API_KEY`
in the environment, or in `.env`; about 60 calls, answers cached in `state/heat_cache/` so a re-run is free), commit `data/heat/`, push, Reboot both Cloud apps. `lattice` needs shapely (requirements-dev.txt) and was
done once from Plotly's county GeoJSON. The files carry no gzip timestamp, so a rebuild with unchanged data on the same machine shows no diff in git (the CSV's line endings follow the OS, so build on this PC, not the droplet, to keep it that way). The Map tab hides the control (`heat_layer.available()`) when the files
are missing or damaged, so a copy of the app without them (a portal mirror that does not carry `data/`) still works.

Checked on live data 2026-10-27 (local view build): corn 2025 16.89B bu over 1,883 counties (72% published; NASS official 16.96B), soybeans 4.26B over 1,666, wheat 1.95B; the same totals the rma-production-map heat
tab showed (whose district numbers, IA North Central 474.3M bu and IL Northwest 423.0M bu, were checked against an independent QuickStats recompute). Tests: `tests/test_heat_layer.py` (49 checks, the shipped files
included) and `tests/test_heat_build.py` (72: QuickStats rows, the fill, the files, the lattice, the map outlines).

**Pins coloured by basis (`map_basis.py`, same day; Kolten: "we will trend towards the basis on the map concept", then "Yes" to colouring the pins by basis level over the production heat).** A **Pin colors**
switch above the heat controls: *Location type* (the default, as before) or *Basis*. Basis colours each pin by the SAME bid the tooltip shows (the commodity and the delivery month picked above the map): purple
where it is weaker than most of the pins shown, green where it is stronger, grey in the middle, a faint grey pin where the location has no bid for that month. Basis is not comparable across commodities,
delivery months or location types (a river terminal and a country elevator sit at different levels), so the scale is always **relative to the pins shown**: the median of the bids on the map is the grey, the
10th percentile the fullest purple and the 90th the fullest green (clipped beyond), so the Location Type and State filters compare a place with its peers. The legend and a caption under the map give the numbers
(corn, spot, 2026-10-27: median -35c, fullest colours at -80c and +9c over 1,002 bids; weak across the northern Plains and Upper Midwest, strong along the river, the south and the east). Fewer than five bids
(`MIN_BIDS`) and the pins keep their location-type colours and the caption says why. Purple / green, not red / green, so the pins read on the heat's yellow-to-red and for colour-blind readers; a basis-coloured pin
gets a grey edge (a white one when the heat is on). Canola keeps its C$/t unit. `tests/test_map_basis.py` (35 checks: the percentile scale, position and colours, shift / scale invariance, the words).
**Next:** a basis SURFACE (interpolated over the locations) if the pins are not enough, and a 2026 forecast year for the heat (the state forecast spread by last year's shares: `fill_withheld` with no published
counties does exactly that).

## 🌡️ Basis Maps tab — AgManager-style basis surface and deviation (`basis_map.py`, 2026-10-27)

Kolten 2026-10-27, with a page of AgManager.info maps (Nebraska / Kansas soybean and corn basis, "cash price minus nearby futures price", and the basis deviation from the "3 year average basis"): "So now I am
wanting to recreate something like this". A tab of its own after Map, **🌡️ Basis Maps**: for a Commodity (Corn, Soybeans, Soft Red Winter Wheat, Hard Red Winter Wheat), a Region (presets: Corn Belt, Western Corn Belt, Eastern Corn Belt, Nebraska and Kansas, Soft red winter states, Hard red winter states, or Custom states),
the Location types whose bids make the surface (country elevators by default) and a **Compare with** switch, it draws two panels side by side as one PNG (`st.image`; a Download PNG button on the admin build only,
like every download): the **basis surface** and its **deviation** from the baseline. Each panel is the AgManager layout: bold underlined title ('Corn Basis, 10-27-2026'), the formula as subtitle, a futures box ('CBT Dec
Futures Price: $4.49', the price of the contract the bids are quoted against), a ten-class legend box in cents, the surface with county lines and heavier state lines, the JSA logo. The PNG is the product: it is built by
pure code with Pillow and numpy (no matplotlib, no shapely, no Streamlit), so it can go into an email or report as it is.

- **The surface (`interpolate`).** Each location's nearest-delivery basis (the Map tab's 'Spot (Front Month)' bid, `nearby_bids`), kept to ONE futures contract (the most common one, ZCZ26 for 97% of corn bids; the
  rest are counted and left out) so a December and a March basis are never mixed. A Gaussian-weighted local mean of the 24 nearest bids, the bandwidth following how far apart they are (the 5th-nearest distance,
  kept between 15 and 70 km): fine where the bids are dense, broad where they are not; computed on a 4-pixel grid inside the states picked and smoothed up; blank further than 100 km from any bid. 0.2-0.4 s.
  Level maps colour by DECILES of the surface (as the AgManager legends do: the class labels are the lower bounds, each colour a tenth of the mapped area); blue strong, red weak.
- **The deviation (`deviation`, `build_pair`).** A symmetric scale around zero (a deviation must not colour 'stronger than average' as weak); purple above the baseline, brown below. Baselines: *3-year average (same days)*
  and *Last year (same days)* read `database.get_basis_window` for the same calendar days +-5 of each year and reduce them (`nearby_from_rows`: the weekly spot history files' mean, else the forward rows of the nearest
  snapshot); *30 days ago* / *7 days ago* read the stored daily bids of that day against the SAME futures contract, so a change is a change of basis, not of contract. **The history is thin:** the daily scrape began 2026-06,
  so for the same days of 2023-25 only the `jsa_history` weekly files exist: 29 corn and 64 soybean locations with two or more years (checked 2026-10-27), almost all crush plants and river terminals (the Illinois River
  corn terminals share one series), which is why 3-year / last-year ignore the Location-type filter (history exists for those alone), use a broad 'regional' interpolation (up to 220 km) and mark the locations with dots,
  and why the caption says so. 30 / 7 days ago have every location (478 corn country elevators). The history deepens by itself: by Oct 2027 last year's days are daily bids, by 2029 a true 3-year average.
- **Wheat (Kolten 2026-10-27, "Add wheat to the basis maps").** The grain map has no wheat classes (only plain `Wheat`, `Wheat (HWW)`, `Wheat (Durum)`), so a class is **the futures market the bid is quoted against**: the registry
  `COMMODITIES` (`commodity_spec`) makes *Soft Red Winter Wheat* the `Wheat` bids on **ZW** (Chicago: 151 locations with a nearby bid on 10-27, 86 of them on the December contract ZWZ26, the deferred ZWN27-only ones are
  left out and counted as 'another contract') and *Hard Red Winter Wheat* the `Wheat` bids on **KE** (Kansas City: 62 locations, 59 on KEZ26). `nearby_bids(..., bid_ok=)` narrows a location's bids to its own market BEFORE the
  nearest delivery is chosen, so an elevator that posts both a Chicago and a Kansas City bid is in each map with its own bid; `Wheat (HWW)` / `Wheat (Durum)` are not either class. The futures box names the exchange from the
  symbol root (`futures_label`: 'CBT Dec Futures Price: $5.12', 'KC Dec Futures Price: $5.30'). Spring wheat (MWE / MW) has 5 locations: too few to map, left out. **Wheat compares with 30 or 7 days ago only:** the tracker
  holds no wheat before June 2026 (the 2026 web forward rows, 257 locations), so `baselines_for` offers those two, the tab resets 'Compare with' and Region when the Commodity changes, and `build_pair` asked for 3yr / 1yr
  makes no database call and says on the panel that there is no history. Country-elevator wheat maps are patchy (about 56 soft red winter and 41 hard red winter country elevators): clear the Location type for the plants
  and terminals too. The legend prints tenths of a cent where whole cents would repeat (`edge_labels`: a hard red winter month that moved 3 cents would read +3 +2 +1 +1 0 -1 -1 -2 -3 -3).
- **Data readers added to `database.py`:** `get_raw_grains(canonical)` (the grain map's raw names, inactive ones left out) and `get_basis_window(grains, start, end, spot=None, symbol=None)` (the basis rows of the snapshots in a
  window; spot / forward and one contract can be asked for, which keeps a recent window to one day's bids). **Outlines:** `data/maps/us_geometry.json.gz` (341 KB: the lower 48's state rings, county lines and state lines,
  simplified to 0.01 degree, county borders noded and de-duplicated), built once by `python heat_build.py geometry` (shapely, requirements-dev.txt) from the same Plotly county GeoJSON as the heat lattice. A copy of the
  app without that file hides the tab's content with a note (`basis_map.available()`).
- **Tests:** `tests/test_basis_map.py` (the view and its mask against Iowa's real area, the surface on constant / gradient / sparse fields, classes and colours, the points and baselines, panels as pixels, the whole pair
  with stand-in readers for each baseline, the wheat classes (futures label and root, registry, regions, baselines, the Chicago / Kansas City split of `nearby_bids`, a soft and a hard red winter pair, the legend decimals), the
  two database readers on a throwaway SQLite). **Not built yet:** a $/bushel legend option (the AgManager sample's), and putting the PNG into an email or report (the picture is pure, so it is a call).

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

### Canola — units, and the Northern Canola (NCGA) table (2026-10-14)

Canola basis is quoted in **Canadian dollars per metric tonne against ICE canola futures** (RSF27 = Jan 2027 ...), not cents per bushel. Every scraper
stores `basis_cents = round(float(basis) * 100)` whatever the crop, so ADM Velva's -45 is stored -4500 (the same x100 rule as soybean meal's $/ton) and
used to show as "-4500¢". `grain_units.py` puts it right **where rows are read**: `database.get_snapshots / get_snapshots_bulk / get_snapshots_asof /
get_map_data` hand canola back as the quoted whole number (`scale_basis`; raw grains 'Canola', 'Canola (Us)', 'Canola Usd/Cwt', 'Canola (Canadian)';
canola MEAL is a $/ton product and is left alone). The database is NOT migrated and nothing writes a value read this way back (every write comes from a
parser); a Snowpark copy of database.py without the helper just returns the stored values. The app says "C$/t" (`_basis_unit` in `app.py`: the Bids table
as "-C$45/t", the Bids charts, the Summary tab, the Spread tab, the Export caption); soybean meal keeps its older `is_meal` handling. Tests:
`tests/test_grain_units.py`, `tests/test_canola_units.py`. There is no ICE canola price in `futures_prices` (Massive does not carry it), so nothing
converts basis to flat price and the Bids tab's "vs prev / wk / mo" columns show "⚠ roll" whenever a canola row's contract changes.

**Canola bids we hold:** ADM Velva and Enderlin (adm_scraper), the CHS ND / MN sites incl. Hallock Drayton, NG and Devils Lake (chs_scraper), Cargill West
Fargo and Bunge Altona via `ncga_scraper.py` below. ADM's Canadian locations (Carberry, Lloydminster, Watson) are captured daily; they were hidden (`grain_map`:
'Canola (Canadian)' inactive) until the 2026-10-28 go-live of the Canadian conversion, which un-hides them (section 'ADM's Canadian bids in US terms').

**`ncga_scraper.py`** (`run_ncga` in `auto_import.py`, flags `--ncga-only` / `--no-ncga`, runs after Bunge; parser `parsers/ncga_parser.py`; saved page
`tests/fixtures/ncga_home_2026-10-13.html`; `tests/test_ncga_scraper.py`). https://northerncanola.com/ (the Northern Canola Growers Association) publishes a
"Daily Cash Prices" table: five buyers x Oct-Feb arrival months, price (US$/cwt), change and basis (C$/t). We take only the two buyers with no usable feed:

- **Bunge Altona** (Manitoba crush plant; bungeag.com has no Altona location) -> provider Bunge, location "Altona, MB" (`location_meta`: MB, Oilseed
  Processing, 49.1 / -97.567).
- **Cargill West Fargo** — Cargill's Barchart WebSol feed (id 5891, root RS) posts canola as a FLAT PRICE only (futuresymbol "n.a.", basis "0.0000"), so
  its own feed has no basis; NCGA derives one from the flat price, in steps of 5 — an estimate, not a posted basis. `parsers/cargill_parser.py` now
  skips canola bids with no futures month (they were stored as a zero basis with the junk symbol 'ZN.2a'; so does `parsers/bunge_parser.py` for Cairo's
  '@RS' barge canola); flaxseed and hi-oleic sunflower at that location are flat-priced the same way and are still stored as zeros.

The other three on the page (ADM Velva, ADM Enderlin, CHS Hallock) are read from their own sites and agreed with it on 2026-10-13 (one month differs: ADM
Enderlin's November, -10 ours vs -8 on the page). **Filing:** rows go in under the **fetch date**, like every other scraper (NCGA updates during the
business day, so a morning import meets yesterday's table, and filing that under yesterday would leave Cargill West Fargo's newest snapshot without
canola); NCGA's own date is kept in the snapshot's `email_subject` / `email_date` ("NCGA cash table of 2026-10-13"); source 'ncga'; row ids
`NCGA_BG_yyyymm` / `NCGA_CG_yyyymm`; basis stored x100. A second run the same day adds nothing (same row ids, ON CONFLICT DO NOTHING). The futures month of
each delivery month is INFERRED (the CHS / Altona pattern: Oct-Nov vs Nov, Dec-Jan vs Jan, Feb-Mar vs Mar, Apr-May vs May, Jun-Jul vs Jul, Aug-Sep vs
the new-crop Nov; `ice_canola_symbol`) — the page does not say, and Cargill's numbers do not step like one contract, so read its basis as +-5 to 10.
**Nothing is filed** (the run reports 'empty' and the operator alert email goes out) when the page cannot be fetched (3 tries), has no table or no 'Last
Updated' date, no longer says "Canadian dollars per metric ton", or its table is more than 5 days old. Terms read 2026-10-13: the disclaimer is
informational / as-is with no reuse clause and robots.txt allows everything.

**The zero rows, cleaned up (2026-10-14, Kolten: "yes delete the zero rows").** Two feeds had been storing flat-priced canola as a basis of 0: Cargill West
Fargo ('ZN.2a', from "n.a.") and Bunge Cairo's winter canola at its barge points (symbol '@RS', basis 0.00; Lower Miss / Mid Miss / Ohio River).
`parsers/cargill_parser.py` and `parsers/bunge_parser.py` now skip canola with no futures month, and the 815 existing placeholder rows (Cargill West
Fargo 671 over 49 snapshots, Bunge Cairo 144 over 48; 2026-06-11 to 10-13; all basis 0) were deleted from `snapshot_rows` after a backup — every column,
same ids — in `Claude Code\_backups\canola_zero_rows_2026-10-14.csv`. `Claude Code\_backups\canola_zero_rows.py` is the one-off that did it (dry run by
default, refuses unless every candidate is a zero-basis non-spot non-NCGA row and no snapshot would end up empty); `--restore <csv>` puts rows back. Cargill
West Fargo's flaxseed (147 rows) and hi-oleic sunflower (588) are flat-priced the same way and are still stored as zeros.

### Hollander & Feuerhaken's daily bid PDF (hfgrain.com/bids, 2026-10-23)

Kolten: "Can we try to scrap this page daily? It has some useful locations. But its a daily PDF." H&F is a Northbrook, IL grain brokerage; its Bids page is one link ("Today's Bid PDF",
`/s/HFBids-YYYYMMDD.pdf` -> Squarespace's CDN) to a one-page landscape Access report with a block of delivery periods per buyer (period | basis in cents | contract | flat price), a
"Futures Settlements" panel, the buyers' hours and a USDA calendar. `hfgrain_scraper.py` (`run_hfgrain` in `auto_import.py`, flags `--hfgrain-only` / `--no-hfgrain`, parser
`parsers/hfgrain_parser.py`, `tests/test_hfgrain_scraper.py` with the saved sheet `tests/fixtures/hfbids_2026-10-22.pdf`; needs pdfplumber = `requirements-dev.txt`, installed on the
droplet at 0.11.0 and skipped on Cloud):

- **Terms (read 2026-10-23):** no terms-of-use or copyright page on hfgrain.com (Home / About / Bids / Contact), the Squarespace robots.txt leaves `/bids` and `/s/` open, the PDF carries no
  restriction. One page and one PDF a day. Not a licence problem; worth knowing it is a brokerage's own customer sheet.
- **Only today's PDF exists** — every older `/s/HFBids-YYYYMMDD.pdf` is a 404 (checked 2026-10-23), so there is nothing to backfill and a sheet not captured the day it is up can be replaced
  before we see it. H&F uploaded the 10-22 sheet at 15:39 CT (the address carries the upload time in ms); the daily run starts 15:45, so a later posting is caught by `deploy/run_hfgrain.sh`
  (cron `10 17 * * 1-5`, `auto_import.py --hfgrain-only`, through `cron-alert`, installed 2026-10-23; crontab backup `/opt/alerting/crontab.backup.20261023_141121`). Its healthchecks heartbeat
  pings return 404 (`cron-alert` sends `?create=1` but the check `hf-grain-bid-pdf` was not created — the same happened that day to three other new jobs, so the account may be at its check limit):
  create the check by hand with a weekday 17:10 schedule, or accept that this catch-up has no dead-man's-switch (cron-alert's failure email works regardless).
- **Deployed 2026-10-23 (c3da023):** droplet pulled, tests pass there on pdfplumber 0.11.0; the first live run (14:09 CT, through the wrapper) filed the 10-22 sheet — Ingredion / Chicago, IL 4 rows
  (929 snapshots now: the Dow Jones 2010-2023-07 history plus this), South Bend Ethanol 3, Lakeside Grain 6, and folded Incobrasa into INCO (10-22 replaced, 10-23 carried from it; read back, no
  duplicates). Backup of INCO's October snapshots before the fold: `Claude Code\_backups\inco_before_hf_fold_2026-10-23.json` (restore = put those snapshots and rows back with the
  delete-then-upsert `inco_bids.apply_plan` uses). Ingredion's location_meta now has type Corn Processing and Argo's coordinates (41.77, -87.81, approximate).
- **Every row is checked:** flat price = the day's settlement + basis, so `flat - basis` must equal the settlement of the row's contract in the sheet's own panel (all 28 rows of the
  10-22 sheet do). A row that does not add up, or whose period is unknown, is not filed; with no settlement panel (or no date, or no bid blocks) nothing is filed and the run reports
  `empty` (the pull-health alert). A sheet more than 5 days old is not filed either.
- **Filed under the SHEET's date**, source `hfgrain`, row ids `HF_<buyer>_<C|S|W><n>`, subject `H&F bids PDF of <date> (revised), uploaded <UTC>`. Labels carry a month name
  (`Oct 2026`, `LH Oct 2026`, `By Oct 25` for 'BY 10/25'); contracts are `CZ26 -> ZCZ26`, `SX26 -> ZSX26`, `WZ26 -> ZWZ26` (CBOT soft red winter, raw grain 'Wheat (Soft Red Winter)').
  First capture of a day wins (a REVISED sheet re-posted after we filed is not picked up: ask for replace-on-change if the logs show late revisions).
- **Which buyers (`TARGETS`) — Kolten 2026-10-23: "We are only needing Ingredion, South Bend Ethanol, Lake side, and Incobrassa on this. Add inco into the existing one we have."** Filed under their own
  names (source `hfgrain`): **Ingredion** (corn, the **Argo** plant, Bedford Park IL — Kolten: "Argo"; filed in the existing Ingredion / Chicago, IL series, the Dow Jones Chicago processor basis the archive holds
  2010 to 2023-07, so it continues after a three-year gap; CHS's own 'Ingredion' location is Indianapolis, a different stream 2c under H&F's), **South Bend Ethanol** (corn, 3201 W Calvert, South Bend IN), **Lakeside Grain** (soybeans + wheat; GROWMARK's Calumet
  River terminal in Chicago, ex-COFCO B-House; coordinates approximate). **Incobrasa is folded into the existing INCO / Gilman, IL series, not filed beside it** (below). Read and checked but NOT
  filed (`enabled: False`): Marquis Hennepin, Cargill Hammond, ADM Mendota wheat, CHS-Rochelle — H&F's bids are not the buyers' own posted ones (10-22: Cargill Hammond Nov +5 in our archive vs -10 on the
  sheet, ADM Mendota wheat Nov +32 vs +30, CHS Rochelle 2c apart) and filing them under the buyer would merge two bid streams into one snapshot. An unknown block (a new buyer) is logged and skipped.
- **Incobrasa into INCO (`inco_bids.fold_hf`, tests `tests/test_inco_hf_merge.py`, 30 checks).** The sheet quotes Incobrasa for Oct / Nov / Dec only, a subset of the six-month sheet Kolten pastes, so
  the block REPLACES those months on the sheet's own day and leaves Jan-Mar as the last Inco sheet had them (a plain 'Oct' and an 'LH Oct' are both October: H&F's replace all windows of the month).
  The day is replaced outright, like `inco_bids.py post` does, and the days after it re-carried; a second run of the same sheet writes nothing, a re-issued (REVISED) sheet replaces the day. The folded
  day's subject is `carried forward from <last Inco sheet> sheet; Oct/Nov/Dec from H&F bids PDF of <date>` — still 'carried', so the stale-sheet guard and the 15:45 `inco_bids.py carry` keep measuring
  from the last sheet Kolten PASTED (the H&F refresh must not hide that Jan-Mar are weeks old); when that sheet is more than 21 days before the day, its other months are NOT carried into the H&F day
  (a visible gap, as in `plan_carry`). With no Inco history at all, H&F alone starts the series (subject `H&F bids PDF of <date> (Oct/Nov/Dec)`). INCO keeps source `manual` and its location_meta
  (the scraper adds none). Its 10-22 numbers equal the 10-19 hand sheet, so the first fold changes nothing but the subject. **A day that holds a sheet Kolten POSTED is never replaced by the fold
  (`plan_hf`, 2026-10-26)**: H&F publishes every business day, so a posted sheet and H&F's PDF of the same day are the normal case, and the fold would have overwritten the posted Oct / Nov / Dec and marked the day
  'carried' (from itself), which the stale-sheet guard then no longer counted as a real sheet. Only carried days take H&F's block.
- Pull health: the three new locations are three different providers, each under pull_health's 5-location provider floor, and INCO is already carried every business day, so a sheet that is late never raises a provider-missing alert; a
  sheet that stops coming (or an unreadable one) shows as the `HFGrain` scraper being `empty`.

### Inco update email (`inco_report.py`, 2026-10-26)

Kolten, the day he pasted a sheet ("Oct/Nov: +25X, Dec: +20F, Jan: +25F, Feb: +20H, Mar: +30H"): "Can we make an auto email like we do on rail when Inco update occurs. Put the normal table of bids and trends
and then a seasonal chart" and "setup to send to soyprocessing@jpsi.com". `inco_report.py` is the rail update email for Incobrasa (Gilman, IL), made of the same pieces (`rail_report`'s chart, `changes_report`'s
button / watermark / signature / `send_email`):

- **When:** `inco_bids.py post --commit` emails the sheet once the write has been read back (VERIFIED). `--no-email` skips it, `--to ADDR` sends a test copy to ADDR only (logged as kind 'test'; the list is untouched),
  `--force-email` sends a sheet that already went out. A send that fails leaves the sheet saved, says so and exits 3 with the resend command (`python inco_report.py --send --date D`). There is no catch-up watcher
  like the rail one: `post` is the only writer. The daily carry-forward and the H&F fold change what a day shows without a new sheet and never email.
- **To whom:** To `kpostin@jpsi.com` (`INCO_EMAIL_TO` overrides) with **`soyprocessing@jpsi.com` on BCC** (`INCO_EMAIL_BCC` overrides), the rail email's pattern; Kolten 2026-10-29, after the first list send had gone To the list: "We need the Soy Processing email address to be in BCC please". The ledger's `to_addr` records both ("kpostin@jpsi.com; bcc soyprocessing@jpsi.com"); a test copy (`--to`) has no BCC. Sent by `changes_report.send_email`: from a PC that is the local Outlook, from the droplet Graph (basis-tracker@jpsi.com),
  so post from the droplet when the list should hear from the tracker rather than from Kolten.
- **What:** the sheet as a table - Period / Fut / Bid and the change in cents vs the previous sheet (the day before: on a posting day that is the last sheet, carried or H&F-refreshed), ~1 week, ~1 month and ~1 year
  (the nearest snapshot within 3 / 4 / 4 days) - matched by DELIVERY MONTH (`delivery_period.canonical`, so 'FH Oct', '9/27-Oct' and 'Oct 2026' are all October; a split month uses its nearest slot). A contract roll is
  spread-adjusted and marked with a rolled arrow exactly as in the rail email. When the earlier sheet did not quote that month, the FRONT row is compared with that sheet's front bid (raw); that is also how the year-ago
  column reads INCO's weekly 'Spot' history (the archive before August 2026 is one weekly Spot row, from 2023-01). Then the spot seasonal chart: `rail_report._seasonal_png` itself, handed INCO's history in the shape it
  reads (`inco_report.rail_shaped`) - this year black, last year blue, the average (Olympic once five complete years exist) and the true range of the complete years before them (three now: 2023/24-2025/26), the posted
  curve in red. INCO's 2022/23 is a part-year (Jan-Aug 2023) and is left out of the band (`MIN_YEAR_WEEKS`).
- **No double sends:** `inco_email_log` (created on first use) holds a signature of each emailed sheet (its date and every label / contract / bid); posting the same sheet again does not mail the list a second time, a
  corrected sheet is a new signature and does. A failed send writes nothing, so the retry is not mistaken for a duplicate.
- **"Oct/Nov +25X" is one bid for two months:** filed as TWO rows, Oct 2026 and Nov 2026, both +25 on ZSX26 (`delivery_period` reads only the first month name, so a single 'Oct/Nov' row would have left November out of
  the series).
- Tests: `tests/test_inco_report.py` (the series as the live archive has it - weekly Spot history, real sheets, carried days - the table cell by cell, the chart's spec and a real render, the send, the ledger, and
  `inco_bids.py post` end to end with a fake sender) and `tests/test_inco_hf_merge.py` (the guard above).

### Scoular: the PAGE names the state, not the id table (2026-10-23)

Kolten: "This should be the state of KS, not IL" — Scoular `Ada, KS` sat in `location_meta` as IL with Kansas coordinates. `scoular_scraper` takes a location's NAME from the page's `<h2>` but its
STATE from `SCOULAR_LOCATIONS`, which is keyed by Bushel id, and Bushel answers a retired id with one sibling's page, `Ada, KS` (9 of the 66 ids do: 1889 'Andres, IL', 1894, 1904, 1923, 1925, 1947-49,
2344). The name de-dup keeps the FIRST id to return a page and 1889 heads the table, so Ada's bids (a real elevator: sorghum + HRW wheat, 58 daily snapshots since 2026-06-12) were filed with
'Andres, IL's state, and since `upsert_location_metas` is `COALESCE(new, old)` every daily run wrote IL back. `_page_state(name)` now reads the state off the page name (`Ada, KS` -> KS,
`Pisgah, IL - Elevator` -> IL; a name with no ', XX' keeps the table's), checked against all 66 live pages: it changes Ada, KS and nothing else. The retired ids stay in the table (the de-dup makes them
harmless). Live row corrected with `upsert_location_meta` (state only; it was IL). `tests/test_scoular_state.py`. **Deploy = `git pull --ff-only` on the droplet — until then the next daily run puts IL back.**

### Map pins come from `coords_seed.json` — frozen name-only geocodes (2026-10-25)

`location_meta` lat/lon are filled from `coords_seed.json` (`database.seed_geocoding`, called from `init_db`) only where a row has NO coordinates: a pin already in the database is never overwritten by it, and
`chs_populate_meta.py` writes state and type, not pins. The seed's entries are Nominatim lookups by NAME, so a small place can land on a bigger one of the same name elsewhere. Five IL rows did (Kolten: "ADM Mendota is in
Mendota, IL / CHS Rt 47 is in Northern IL / Cargill Paris Mill is in Paris, IL / CHS Ottawa River Market is in Ottawa, IL / GPRE Madison is in Madison, IL"): ADM 'Mendota (Wheat Milling)' was in California, CHS 'Rt. 47
Terminal' in Los Angeles, Cargill 'Paris Mill' in Florida, CHS 'Ottawa River Mkt' in Ohio, GPRE 'Madison' in Nebraska. **Fix a pin in BOTH places:** `upsert_location_meta(provider, location, lat=, lon=)` on the live row
(the database wins) and the entry in `coords_seed.json` (so a fresh or local database does not re-create it). CHS 'Rt. 47 Terminal' is pinned at Elburn IL as an APPROXIMATION — Kolten only knew "Northern IL"; it posts
soybeans only and prices like the CHS country elevators (Meredith Rd Elburn, Newark), not the Illinois River terminals (Morris, Seneca), and CHS already has a separate 'Morris, IL' — so correct it when the town is known.
Old rows: `Claude Code\_backups\location_meta_pins_before_2026-10-25.json`. A second pass the same day (Kolten: "check every state's pins. Stateline is wrong and Idalia West looks to be as well") tested every pinned row against its state's bounding box (+0.1 degree) and against the state
in its own name (', KS'): 10 of 657 failed, all fixed (old rows: `..._b.json`). **ADM 'State Line, OK'** was State Line, MS (it is the ADM elevator at Optima, OK); GPRE 'Superior' was Superior, NE (it is Superior, IA); Scoular
'Walnut Grove Farms / Schochoh' sat in TN; CGB 'Tallulah Port' and 'Falls City' in TX; CHS 'Idalia West' in Arizona and 'Stateline' at Lake Tahoe, both now APPROXIMATE (Idalia CO; Julesburg CO — Stateline's bids equal
Julesburg's in every line); CHS 'New Haven' was labeled ND and pinned in Connecticut: it has HRW wheat bids at the NE Colorado level, OpenStreetMap has no New Haven in ND and does in Logan County CO, so it is CO now
(`chs_populate_meta.STATE_CORRECTIONS` said ND and would have undone it); Tyson Camilla / Humboldt had state 'Unknown' from Tyson's own feed (GA / TN now; `tyson_scraper._clean_state` stops the daily upsert writing 'Unknown'
back; `tests/test_tyson_state.py`). **A bounding box cannot see a right-state, wrong-town pin.** ADM's bootstrap (`adm.gradable.com/api/commodities/merchandising/bootstrap`, the call `adm_scraper` already makes) carries a
Google-geocoded address for every market (`markets[].address.geocode.lat/lng`): 112 of 131 ADM pins are within 12 miles of it (median 1.4) and 8 were not — Marshall MN x2 (270 mi off), Oakland IL (a Chicago neighborhood, 151),
Country Store KS (96), Panhandle OK (24), Clinton IA x2 (20) — all moved to ADM's own coordinates. The CHS (Bushel) and Scoular feeds carry no coordinates. **The one-time pin pass (2026-10-25; Kolten: "Why don't those have a pin?" ... "yes, run the one-time pass").** 636 of 1,301 rows had no pin: none was in `coords_seed.json` (built mid-June by the manual,
name-only `geocode_locations.py`), 565 first appeared after it, 552 were still posting bids (so they showed in the Bids tab but not on the map: `get_map_data` requires a pin), and nothing re-runs the geocoder (the Map tab only
tells you to; `init_db` returns before seeding on Snowflake; scrapers set no coordinates, except Tyson and the ones added 10-23 / 10-25). The pass (`Claude Code\_backups\pin_pass_2026-10-25\pin_pass.py`, with the revert
list and every Nominatim answer; dry run by default) placed **586**, lat / lon only: 11 from ADM's own addresses, **217 from the coordinates every AgriCharts `cashbids-js.php` feed carries per location** (27 hosts probed, all
answer; matched by name inside the provider's own host), 356 from structured Nominatim lookups (city + state, never a bare name) accepted only when the result is a settlement of that name inside the state's border (the
same-name town nearest the provider's other pins wins when there are several), and 2 copied pins. Independent check: 23 feed pins re-looked-up in Nominatim were a median 0.1 mi away, at most 10.7. **50 still have no pin:**
names that are not a place (Futures-Only, 'CGB - Cross Country', 'To Buy for HCP', the See-Mor and GSI- rows ...), towns OpenStreetMap lacks (Grebner SD, Kintyre Flats MT, Gladstone Bluff / Suez / Yorkwood IL, Planters KY,
Fairbanks IA), six held back by hand because the only same-name town OSM has is 95-240 miles from the provider's other sites (Skyline KY, LeRoy IL, Old Dominion VA, Keystone's Holland and White Cloud IN, CPI 'Lewis'), and two
where the co-op's own feed contradicts our state (CFE 'Germantown, MN' sits near Paullina IA; FS Grain 'Kentland, IN' at Newark IL). **State labels found wrong on the way (the pins were right), FIXED 2026-10-26 (Kolten: "Both"):**
Premier Cooperative's provider-wide 'WI' (`dtn_content_scraper.SITES` `state`) covered 10 Iowa sites (Big River Dyersville, Fairbanks, Ft Atkinson/Waucoma, Homeland (Lawler), Manchester, New Albin, Ossian, Postville, Valero CC at Charles
City, Viserion Mcgregor; premiercooperative.com/locations) and Keystone's 'IN' covered three Ohio sites (College Corner, Eldorado, Verona) and four Michigan ones (Fremont MI, Hamilton, Holland, White Cloud; Keystone is Co-Alliance + Ceres
Solutions and keystonecoop.com/locations gives every site's state and coordinates). The live rows were corrected (Premier: state only; Keystone: state AND all 16 pins from its own coordinates, because the label had made the pin pass put
College Corner, Verona, Pershing and Hamilton at same-named Indiana towns; old rows: `Claude Code\_backups\keystone_premier_labels_pins_before_2026-10-26.json`) AND the two SITES rows now carry a `states` override
(`dtn_content_scraper._state_from`: the override, else the ', XX' in the name, else the row's `state`). Both are needed: `fetch_dtn_content` writes `state` into `location_meta` every day and the upsert keeps a stored value only for None, so a
label fixed in the database alone is put back by the next run. The bounding-box audit cannot see this kind of error (north-east Iowa is inside Wisconsin's box). `tests/test_dtn_content_states.py` (29 checks on the real SITES rows with canned
responses, including that WITHOUT the overrides the old labels come back). **Keeping it from recurring:** the weekly pin job below, and scrapers passing the coordinates their feeds carry (`agricharts_scraper` `pins: True` does it for Alcivia).

### Weekly pin job — `pin_missing.py` (2026-10-26)

Kolten's "Both" (fix the Premier / Keystone labels, and build the job that keeps new locations pinned). A scraper creates a `location_meta` row with a state and a type and no coordinates, and `get_map_data` needs a pin, so every location a
scraper adds would again show in the Bids tab and not on the map. `pin_missing.py` is the one-time pass as a standalone job, for the droplet (`deploy/run_pin_missing.sh`; cron `45 17 * * 5`, Fridays 5:45 PM Central, through
`/opt/alerting/cron-alert`, after the 15:45 import and the 17:10 H&F catch-up). Its sources, in order of trust:

- **ADM's own Google-geocoded addresses** (the bootstrap call `adm_scraper` makes), matched by display name and checked against the row's state.
- **The coordinates every AgriCharts `cashbids-js.php` feed carries** (the hosts of `agricharts_scraper.TENANTS` + `agricharts_md_scraper.SITES`; matched by name inside the provider's own hosts; a host whose locations share one pin, four or more,
  is a head-office address and is not trusted; a feed pin that falls outside the row's own state is refused and is NOT passed on to the geocoder — FS Grain 'Kentland, IN' sits at Newark IL in its own feed).
- **Structured Nominatim** (city + state, never a bare name; at least 1.25 s apart; answers cached in `state/nominatim_cache.json`), accepted only for a settlement whose name is the city AND whose point is inside the state's box; when the state
  has several such towns the one nearest the provider's other pins wins (Primient's LeRoy), else the first is taken and the row is listed as ambiguous. `ALIAS` holds the hand-made readings: names that are not a town ('Homeland (Lawler)',
  'Meredith Rd Elburn') and rows that copy another row's pin ('Mendota BN Rail').

What it will not do: write anything but lat / lon (plus `state` for a row that has none, when an ALIAS names it — never a label that exists, never a pin that exists). A pin more than 120 miles (`FAR_MILES`) from the same provider's other
pins in that state is **held back**, not written (Hopkinsville's 'Skyline': the only Skyline OSM has is in Letcher County, 242 mi away); `NATIONAL` providers are exempt, `ACCEPT_FAR` lets through ones checked by hand, `HOLD` keeps one out
(CPI 'Lewis, NE'). A name that is not a place (`SKIP_RE`: 'Futures-Only', 'CGB - Cross Country', the GSI- and See-Mor rows) is not looked up, and a name whose own state ('Fremont MI') disagrees with the label on file is not guessed at.

**Silent unless something happened.** The operator (`CHANGES_EMAIL_TO`, kpostin@; never the group) gets one mail when a location was pinned, held back or could not be placed, each with its source; "not a place name" rows are remembered but
never mailed (`QUIET`). What it could not place is remembered in `state/pin_missing.json` (gitignored; per machine) and tried again after 30 days, or at once when the row's state label changes; a held-back row is mailed once, not every
month; a mail that fails to send is retried next run (the row is stored `emailed: False`); a row somebody pins by hand drops out of the books; a failed geocoder request is NOT written off (the row is tried again next run). The first run
mails everything still open from before, once. Switches: `state/pin_missing.off` (touch it), `--dry-run` (plans and prints; writes and mails nothing), `--no-email` (what it would have mailed counts as seen), `--retry-all`, `--max-lookups N`
(new geocoder requests per run, default 400), `--state-dir`, `--allow-sqlite` (tests; without it the job refuses a backend that is not Snowflake, because a local SQLite is empty). Undo a run: set lat = lon = NULL on the rows it mailed.

**Other spellings of the same town are asked too.** The first dry run held Primient's 'LeRoy' back: the geocoder's answer for 'LeRoy' is only Leroy Township in Boone County (155 mi from Primient's other Illinois sites), and the lookup stopped
there, so 'Le Roy' (OSM's spelling of the McLean County town beside Heyworth) was never asked. After a hit the job now also asks the same town's other spellings (`spelling()`: Le Roy, Fort / Ft, Saint / St, Mount / Mt) and the
nearest-to-the-provider's-pins rule picks; the other towns a name lists ('Hitchcock-Tulare, SD') are not asked, and a row is never placed on half an answer (a failed or over-budget request for the second spelling leaves it for next run).

**Dry run on the live database, 2026-10-26:** 43 of 1,301 rows had no pin and two were placeable (Premier 'Fairbanks' -> Fairbank, IA through ALIAS; Primient 'LeRoy' -> Le Roy, McLean County, about 20 mi from its Heyworth; two geocoder
requests, both answered); the other 41 are the one-time pass's leftovers (28 not place names, Grebner SD / Kintyre Flats MT / Gladstone Bluff, Suez, Yorkwood IL / Planters KY / C&O Grain OH not in OpenStreetMap, the held Skyline KY /
Old Dominion VA / CPI Lewis NE, the two feed contradictions, and Cargill 'Forest City' — an orphan row seeded from `facility_tags_seed.json`, no state and no bids; the live location is 'Forest City (MO)'). That run found a bug the
tests could not: Snowflake's cursor hands back dicts, so unpacking a row as a tuple reads the column NAMES (SQLite's rows unpack as values and hid it). `load_rows` now goes through `database.get_all_location_meta()` and is tested with a
dict-only stub. Tests: `tests/test_pin_missing.py` (71 checks; no network — the geocoder is a dict, the clock simulated, the database a throwaway SQLite); 40 deliberate breakages of its rules, run on a scratch copy, are all caught.

**Deployed 2026-10-26 (`34208b1`).** Droplet pulled at 08:36 CT, before that day's 15:45 import, so the Premier / Keystone labels hold (a deploy that missed the 15:45 run would have had it write WI / IN back; the pins would have stayed).
The six related test files pass on its Python 3.12.3, and `state/nominatim_cache.json` was seeded from the pass's answers (412 keys). The cron line is installed (08:50; crontab backup `/opt/alerting/crontab.backup.20261026_0850`, diff = exactly
the comment and the job):
`45 17 * * 5 /opt/alerting/cron-alert "Map pins for new locations" "/opt/basis-tracker/logs/pin_missing_*.log" /opt/basis-tracker/deploy/run_pin_missing.sh`, next run Fri 10-30 17:45 CT. **First real run 08:51 CT**, through the wrapper
and not `cron-alert` (so it could not ping healthchecks): 11 s, rc 0, pinned Premier 'Fairbanks' (42.6392, -92.0489) and Primient 'LeRoy' (40.3520, -88.7642) with no geocoder request, mailed the operator once (2 placed, 2 held back,
11 to place by hand) and remembered the other 41 (28 of them 'not a place name', never mailed). A before / after dump of all 1,301 `location_meta` rows shows exactly those two rows changed, lat and lon only. Undo: set lat = lon = NULL
on those two. **Heartbeat:** `cron-alert` pings healthchecks with `?create=1` under the slug `map-pins-for-new-locations`. If the account is at its check limit that 404s (as the H&F check's did on 10-23) and the job has no dead-man's
switch, only `cron-alert`'s failure email; if it does auto-create the check, the default period is one day and it will false-alarm six days a week for this weekly job — create it by hand with a Friday 17:45 CT schedule. Data noise seen on
the way: CHS has a second, double-encoded copy of its two dump-fee locations ('Fergus Falls South (8Â¢ Dump fee)', 'Underwood (13Â¢ Dump fee)', no state, no pin) beside the proper ones; they are two of the 28 quiet rows.

### River Valley Coop (StoneX Stonehedge component) and Alcivia (AgriCharts), 2026-10-25

Kolten: "A couple scrapers to add. River Valley Coop https://www.rivervalleycoop.com/products-services/grain and Alcivia https://www.alcivia.com/wisconsin-grain-services/cash-bids-grain-wisconsin-grain/".

**Alcivia** is the AgriCharts platform: the page's bids table is `alciviacoop.agricharts.com/inc/cashbids/cashbids-js.php` (`var bids = [...]`). One `TENANTS` row in `agricharts_scraper.py` (multi shape, state WI) files 15
locations (14 elevators + Menomonie Feed Mill; corn, soybeans, wheat; soybean meal is on the page but nothing is posted). The feed's names are ALL CAPS, so `titlecase` ('FALL CREEK' -> 'Fall Creek, WI'), and it carries each
location's own latitude / longitude, which `pins` turns into the map pins (`run_agricharts_tenants` now passes `lat` / `lon`; a tenant without `pins` sends None, so its stored pins are untouched). Windows are labeled by their
start month like every AgriCharts tenant ('Oct/Nov 26' -> 'Oct 2026'). `tests/test_alcivia_agricharts.py` (28 checks; fixture `tests/fixtures/alcivia_feed_2026-10-25.json`).

**River Valley Cooperative** (Eldridge IA; 22 IA / IL elevators). Its "Grain Bids" section is a StoneX Stonehedge iframe (`stonehedge.stonex.com/component/bids?key=...`), and the component's own JavaScript is a thin client over ONE
REST call: `GET https://api.stonehedge.stonex.com/settings/v2/delivery-periods` with `Authorization: key <embed key>`. Plain requests, no OAuth or streaming (that is the full StoneX app), re-polled by the widget every 60 s, "Data
provided by StoneX. All prices delayed 10 minutes." The response is delivery periods (commodity x window, contract symbol 'CZ26' -> ZCZ26, `lastUpdatedDateUtc`), each with `bids` [{location, cash, basis}] in $/bu.
`stonex_scraper.py` (`run_stonex`, flags `--stonex-only` / `--no-stonex`) files only the co-op's OWN 22 locations (state and pin from its Locations map: `RIVER_VALLEY_LOCATIONS`; 20 post bids today, the Eldridge HQ and the
seasonal Duncan do not) as provider 'River Valley Coop', Country Elevator, corn + soybeans, labels 'Nov 2026' / 'FH Nov 2026' / 'LH Nov 2026'. **Not filed: the 26 destination points on the board** (ADM - Burlington, CGB, Cargill,
CHS, Gavilon, Marquis, Ingredion ...): they are River Valley's direct-ship delivered bids, not those buyers' own posted ones, and filing them under ADM would merge two bid streams into one snapshot (the H&F reason). A board whose
newest own-location update is more than 5 days old files nothing (`MAX_AGE_DAYS`; the merchandisers touch the headline months most days and deferred months sit unchanged for a week). `tests/test_stonex_scraper.py` (48 checks;
fixture `tests/fixtures/stonex_rv_2026-10-25.json`). River Valley also posts three daily closing-bid PDFs (IL North / IL South / IA under `rivervalleycoop.com/getmedia/`, behind Incapsula bot protection): not needed, the board
has the same locations. **Other StoneX customers:** if United Cooperative's cash-bids page embeds the same `component/bids` iframe, one more SITES row would do (not checked).

**Terms read 2026-10-25.** `stonehedge.stonex.com/robots.txt` allows everything (`Disallow:` empty) and the API host has none. The StoneX terms of use (stonex.com/terms-of-use/) say nothing about scraping or automated access;
the one relevant sentence is "No part of this material may be copied, photocopied or duplicated in any form by any means or redistributed without the prior written consent of StoneX Group Inc." The data are River Valley's own
public bids. We use the co-op's public embed key from our server once a day (the call its own page makes every minute); if StoneX or River Valley object, the scraper is one SITES row to remove. AgriCharts' robots.txt
disallows `/markets/` only (we read `/inc/cashbids/`); alcivia.com's and rivervalleycoop.com's robots files block nothing we read. Nothing was filed live before the code was pushed.

### Beachner Grain, Ag Partners Coop and Ray-Carroll (2026-10-27), and what Canada looks like

Kolten, 2026-10-27: "Can we check if we can scrap ray carroll and also be sure we are scrapping Canadian locations for all the spots we can for the Major grain companies? Are we also scrapping canola prices for these as well? Lets also
add in this bid scrapper https://www.beachnergrain.com/cash-bids, https://www.agpartnerscoop.com/grain/cash-bids". All three scrapers are built and tested; **committed and pushed 2026-10-27 as 8810378 and pulled on the droplet the same evening**, so their first filing is the next daily import
(Wed 10-28 15:45 CT: Beachner, Ag Partners and Ray-Carroll come in with it). Fixtures are the real pages / feed of 2026-10-27, trimmed.

**Beachner Grain** (Parsons KS; 24 elevators: 21 in Kansas, Afton / Miami / Welch in Oklahoma). Bushel's ASP.NET FarmCentric page: `beachnergrain.com/cash-bids` switches location by a postback, but each location also has its own GET page
`/cashbidssingle-<id>` (the dropdown's option value, also `/cbloc-<id>` on the Locations page), so it is one `bushelsites_scraper.SITES` row of the existing "pages" style (the Harvestone / Prairie Grain Partners pattern) plus four
options: `grains` (the boards are named by commodity CLASS: 'Hard Wheat' = Kansas City KE, stored raw as 'Wheat HRW'; 'Soft Wheat' = Chicago ZW, 'Wheat SRW'; 'Milo' is priced off corn; a heading not in the map is skipped and logged, never guessed),
a row id that carries the grain whenever a map is used (Corn and Milo are both ZC; every other site keeps its old ids), a per-page `pin`, and retries (3 tries, 2 s then 4 s; a page that keeps failing is skipped, all pages failing raises as before:
the first live look hit a connection reset, which would have cost the whole site). Every bid is the 'Cash' bid against a futures month (Dec corn / KE / ZW, Nov soybeans): 24 x 5 = 120 rows; 10-27 basis corn -35 to -69, soybeans -6 to -36,
HRW -102 to -116, SRW -112 to -128, milo -110 to -150. **Pins** (the page has none): each elevator's street address from its `/cbloc` page, looked up in Nominatim once (15 matched a street, 9 are the town; Harris is the hamlet, its address is a Garnett
mailing one; Neosho Valley sits at Erie); they live in the SITES row and flow board `lat`/`lon` -> `run_bushelsites` metas -> `location_meta` (None keeps a stored pin). `tests/test_beachner_bushel.py` (29 checks; fixtures
`beachner_{afton,welch}_2026-10-27.html`). **Terms read 2026-10-27:** Beachner's robots.txt is Bushel's white-label template, search bots allowed and `User-Agent: * Disallow: /` — the same file the eight Bushel sites we already scrape serve
(See-Mor, Ace, One Earth, Harvestone, Big River, Prairie Grain Partners, Madison, Agrex). Bushel's Terms of Use (pages.bushelpowered.com/hubfs/Bushel-Terms-of-Use.pdf, 2021-06-25) is the app licence and has no automated-access, scraping or
copying clause for a customer's website. Beachner's page says "All prices are subject to confirmation by an authorized Beachner Grain representative" and "Data provided by Bushel. All prices delayed 10 minutes", carries (c)2026 All rights reserved and links
no terms page. This follows the practice already in place for Bushel sites (one GET per elevator per day, 1 s apart, an honest user agent); if Beachner or Bushel object it is one SITES row to remove.

**Ag Partners Coop** (Seneca KS; its page embeds `agpartners.agricharts.com/inc/cashbids/cashbids-js.php`): one TENANTS row in `agricharts_scraper.py`, 20 elevators (NE Kansas, Rulo NE, Tarkio MO; state and pin come from the feed, as for Alcivia).
Options: `skip` (the feed's 'AGP St Joe' is a 'Direct Ship' row, the co-op's bid to AGP's plant; AGP's own plant is the AGP provider's: one bid stream per snapshot, the River Valley reason), `grain_alias` (its 'Red Wheat' is Kansas City hard red winter,
KEZ26, which grain_map reads as SOFT red winter, so it is filed as 'Wheat, Hrw'; Milo stays 'Milo') and `open_windows` (below). 91 rows: corn, soybeans, HRW at 10 elevators, milo at Seneca. Provider 'Ag Partners Coop', so it is not mistaken for
South Dakota's 'Prairie Ag Partners'. `tests/test_agpartners_agricharts.py` (24 checks). Terms: `agpartners.agricharts.com/robots.txt` disallows `/markets/` only (we read `/inc/cashbids/`); agpartnerscoop.com's robots file blocks
`/*?`, sandbox and pillar pages (that site is not read). **The shared AgriCharts parser was dropping in-season bids (found while adding this tenant).** `parsers/ksethanol_parser.py` skips a bid whose delivery START month is before the scrape month, so a
harvest window ('09/01/2026 - 11/30/2026') disappears for all of October and November. Counted on 10-27's feeds, the bids dropped although their window is still open: **Ag Partners 41 of 92, Garden City Coop 114 of 134** (HRW wheat
'06/02/2026 - 12/01/2026', corn and milo windows that began 09/01/2025), **Alcivia 8 of 181** (wheat '07/01/2026 - 02/28/2027'); Mid Missouri, JBS, Gold Eagle and Meadowland none. `open_windows` (a TENANTS key, passed to the parser on the loc; the bids now
carry `delivery_end`) keeps a window that began earlier and has not ended, filed under the CURRENT month; a window that ended, or a bid with no end date, is still stale. **Switched on for Garden City Coop and Alcivia too on 2026-10-28 (Kolten: "Fix this")**, which was not just one key: Garden City posts an 'Old Crop' (since 09/01/2025) and a 'New Crop 2026' window with the same basis, so under
`open_windows` two overlapping windows that land on one (grain, month, contract) collapse to ONE row, the later-starting window (then the later bid id); four of its corn bids are posted at exactly 0 (a posted-but-not-bid placeholder: the other 17 are +10 to +45), dropped by the
tenant option `skip_zero_basis`; Alcivia's wheat 'Storage' / 'Storage Only' windows (07/01/2026 - 02/28/2027, basis -100 to -160, 8 locations) are storage prices, not bids, dropped by `skip_notes: ["Storage"]` (the old rule dropped them by accident), and its harvest window
'Oct/Nov 26' (10/01 - 11/30) would have vanished on 11-01 without `open_windows`. A window is stale the day AFTER its end date (to the day, not the month: one ending 12/01 is filed on 12/01 and gone on 12/02). Live feeds of 10-28 after the change: Garden City 21 locations,
86 rows (corn at 14, HRW wheat 21, milo 20, soybeans 7, hard white 4: the whole in-season board that had been missing), Alcivia 179 rows, no duplicates, no zero basis. Mid Missouri, JBS, Gold Eagle and Meadowland lose nothing today and stay on the old rule.
`tests/test_open_windows_tenants.py` (25 checks; fixtures `gccoop_feed_2026-10-28.json`, `alcivia_storage_feed_2026-10-28.json`).

**Ray-Carroll County Grain Growers** (Carrollton MO; ray-carroll.com, DTN's aghost hosting, "layout 19"). The note that stood here said it needed dropdown iteration per location x commodity; it does not: the page takes the location in the URL
(`index.cfm?show=11&mid=26&theLocation=<id>&cmid=all&layout=19`, the id is the Location dropdown's value; without `cmid=all` it shows only the first commodity) and is server-rendered with the usual displayNumber() obfuscation. So it is eight GETs in
`dtn_http_scraper.SITES` and one new parser, `_parse_cashbid_grid`, which finds its columns by the header's NAMES (Delivery | Cash Price | Futures Month | Basis; never by position or by the |v| < 2 rule the older parsers lean on) and files the END date of
the window as its month ('Nov 30, 2026' -> 'Nov 2026'). Eight elevators: Carrollton, Hardin, Norborne, Brunswick, Corder, Slater, Sumner (Country Elevator) and St. Louis (a River Terminal, 2.49 million bushels at 4040 Duncan Ave; wheat only); Wakenda posts
nothing. **The six 'Off Farm' ids (2, 8, 11, 19, 21, 22) are NOT filed**: they post monthly deferred corn / soybean bids (Dec-Jul) for grain picked up on the farm, a different bid from the elevator's, and would put a second pin on each town (say so if
that forward curve is wanted). 10-27: corn -40 (Carrollton -15), soybeans -45, SRW wheat -50 Oct / -40 Jul 2027 at the elevators, St. Louis wheat -15 / -10 / -15: 30 rows. Pins from each elevator's address on its own location page (Nominatim; Brunswick, Corder
and Slater are the town). The requests carry an identifying user agent (`ua`) and a 1.2 s pause between pages of the same host (`delay`), and `run_dtn_http` now passes `lat`/`lon` to `location_meta`. `tests/test_raycarroll_dtn.py` (25 checks; the real
markup and the page's own decoder). **Terms:** robots.txt (the aghost network's boilerplate) disallows /popup/, /showcashbid.cfm, /cashbid_view_detail.cfm and others, none of which is read here, and blocks named bots (Python-urllib, wget, httplib ...), not
ours. The DTN "Disclaimer and Terms of Use" on aghost.net/disclaimer.cfm has a copyright clause ("no copying, redistribution, retransmission, publication or commercial exploitation of downloaded material will be permitted without the express written
permission of DTN and the copyright owner") and says nothing about automated access; the same clause sits behind the seven aghost sites already scraped.

**Canada, audited 2026-10-27** (Snowflake + a research pass over the nine companies; evidence in the session scratchpad `ca_research/`). **ADM** publishes its whole network through one Gradable feed ("151 locations across the US and Canada") and `adm_scraper` fetches every market. Twelve of them are Canadian: nine post a basis and are in the database (Blenheim, Maidstone, Midland, Port Colborne,
Windsor, **Bacres Grain** (Ontario, named without a province), Carberry MB, Lloydminster AB, Watson SK; corn / soybeans / wheat at the Ontario sites, canola at the three prairie sites, 10 rows each, C$/t against ICE canola RSF27 ...), and three post only flat prices that the basis model
cannot hold (**Calgary** AB posts nothing, **North Battleford** SK yellow peas at C$8.25/bu, **Vanscoy** SK 14 pulse bids in C$/lb: no futures, no basis, so `parse_instruments` skips them). Until 2026-10-28 Windsor ON was dropped on purpose (`_ADM_SKIP`) and 'Canola (Canadian)',
'Corn (Canadian)', 'Soybeans (Canadian)' were hidden by grain_map (`is_active = 0`), because the Ontario bids are a Canadian-dollar cash price against US futures in US dollars (Blenheim soybeans +379, corn +130 on 10-27): see the next section for the conversion to US cents per bushel
that replaced both. 'Canadian Eastern Hrww' (Midland) stays hidden; Midland and Port Colborne last posted 09-28, Carberry's CWRS 09-10 (left as it was).
**Bunge:** only Altona MB canola, through the NCGA table. **Cargill:** US only; Canada is behind a CargillAg login (cargill.ca answers an Akamai 403) and the old Barchart tenant `cargillag.websol.barchart.com` is dead (last import 2023-02-14).
**CHS:** no Canadian locations (177 Bushel locations, all US side). **Louis Dreyfus:** `ldc_scraper` excludes `isCanadaPopulation` on purpose; the same public API lists Yorkton SK (canola and Nexera, C$/t vs ICE canola, 10 rows each), **but myldc.com's terms (last
modified 2016-11-23, read 2026-10-27) say "Use any robot, spider or other automatic device, process or means to access the Site for any purpose, including monitoring or copying any of the material on the Site" and (s.7) "the Site may contain pricing information confidential
to LDC": that clause covers the eight US LDC sites already scraped too, so it is Kolten's decision; nothing was built.** **Viterra (now Bunge), Richardson, P&H, Paterson, G3, Ingredion Canada:** bids are behind customer logins (myBunge, your.richardsonpioneer.ca / richardsoneast.ca,
P&H Central, patersonportal.com, g3.ca/smartsell, ag.ingredion.com); Richardson's legal page says "The framing, mirroring, scraping or data-mining of the Website or any of its content in any form and by any means is strictly prohibited", Paterson and G3 have copy / reproduce
clauses; none is scraped. Public leads, none built: PDQ (pdqinfo.ca, Alberta Grains: free regional averages in nine zones, no elevator detail, its terms bar republishing), GFO SellSmart (Ontario elevator-level corn / soy / wheat basis, app-only), 620 CKRM / Sask Today's daily
Moose Jaw (Bunge) net prices. The way to the majors' Canadian bids is a data licence (GFO, Alberta Grains) or a JSA customer login, not a scraper. **Canola we hold:** ADM Velva, Enderlin ND and Webberville MI ('Canola (Us)'); 31 CHS ND / MN / MT sites ('Canola Usd/Cwt');
Bunge Altona and Cargill West Fargo (NCGA); ADM's three Canadian ones (hidden until the 2026-10-27 go-live, next section).
**Bunge Hamilton ON, asked again 2026-10-27 ("any other local elevators or pages that list a Bunge Hamilton bid?"): no page labels one.** The nearest is the **Ontario Commodity Report** (fmn1.agricharts.com; Farm Market News, University of Guelph Ridgetown Campus, editor John Jordan; GFO's cash-bid source): 205 town-level lines with no company names, refreshed daily,
including an unlabeled 'Hamilton Transfer' soybeans line (only soybeans: Oct ZSX26 C$16.51 = +420 against US futures as posted, -56 in US terms at USDCAD 1.405; new crop ZSX27 C$16.17, -61) and a 'Windsor Crusher' soybeans line that equals ADM Windsor's posted C$16.505 to half a cent, so the report copies buyers' bids. Whose bid 'Hamilton Transfer' is cannot be told from the report (G3, Bunge's joint venture with SALIC,
has run a lake terminal at Hamilton's Pier 26 since 2017; Bunge's own Hamilton refinery is on Pier 11). Farmbucks' `/grain-prices/bunge` lists Bunge's former-Viterra Western Canada points (AB / SK / MB / BC) and US ones, no Ontario ("more locations coming soon"). **Neither can be scraped as it stands:** the Ontario report is Barchart-hosted and Barchart's Terms of Use (updated 2026-02-03) say no user may
"Use any data mining, robots, or similar data gathering and extraction tools to capture data or content from the Barchart Services" (its robots.txt also says `Disallow: /markets/`); Farmbucks' terms bar "spider, crawl, or scrape" (s.12) and copying or reselling without written permission (s.2). **The same Barchart footer and terms are on the AgriCharts tenant sites `agricharts_scraper` already reads** (KS Ethanol, Alcivia, Ag Partners; Garden City's homepage shows no footer, same platform):
until 2026-10-27 only their robots.txt had been checked (the tenant feeds, `/inc/cashbids/`, sit outside the `/markets/` disallow). That is the same question as LDC's above and is Kolten's decision; nothing was built or changed.

### ADM's Canadian bids in US terms: Windsor, the hidden grains and `fx_rates` (2026-10-28)

Kolten, 2026-10-28: "Add this and Windsor but add ability to convert to $/bu ... Also for this. Hidden grains: ADM's Canadian canola ... along with Ontario corn and soybeans." **LIVE since the evening of 2026-10-27: pushed as 8810378, pulled on the droplet, `adm_canada_golive.py` applied (below).**

- **What ADM posts.** Each Gradable instrument names its currencies and units: Blenheim corn is `cash_bid` 5.785 (currency cad, bushels) against `futures` 4.4875 (currency usd, bushels), and `basis_bid` 1.2975 is just the first minus the second, which the tracker stored as a +130 cent basis (soybeans +379, wheat +107).
  The real basis in US terms is cash / USDCAD - futures: 5.785 / 1.405 - 4.4875 = -0.37 (corn -37, soybeans -86, wheat -108 on 10-28). Canola is CAD against ICE canola futures in CAD per tonne (both sides one currency): its posted basis is already a basis and stays as posted (stored x100, shown as C$/t; `grain_units`).
- **`parsers/adm_parser.canadian_basis(instr, usdcad)`** reads those flags rather than guessing from the grain: cash currency not CAD -> as posted (every US market); CAD cash against CAD futures -> as posted (canola); CAD cash against USD futures in bushels with a futures price, a cash bid and a rate -> `cash / usdcad - futures`;
  any other CAD-against-USD case (no rate, no price, a unit other than bushels) -> the bid is SKIPPED, never stored as the mixed number. `parse_instruments(..., usdcad=)` takes the rate; `auto_import.run_adm` fetches it once per run (`fx_rates.usdcad_now()`) and `_ADM_SKIP` is now empty (Windsor is parsed like the rest: 15 bids, 14 converted, its winter canola as posted).
  `adm_names._STATE_OVERRIDES` gives 'Bacres Grain' the state ON. The Bids tab says, under a Canadian ADM location's corn / soybean / wheat table, that the numbers are converted to a US basis (a caption; canola keeps C$/t).
- **`fx_rates.py` and the `fx_rates` table** (`date, pair, rate, captured_at`; DDL in `database.py` for SQLite / Postgres and in `snowflake/01_schema.sql`, which is NOT run by `init_db`: create it by hand on Snowflake, the go-live script does): Massive's `C:USDCAD` daily bars on the same key as the futures (`MASSIVE_API_KEY`;
  CAD per US dollar, 1.405 on 10-27; the Sunday-evening bar is dropped, as for the Brazil tab's USDBRL), stored per weekday. `capture()` never raises, `rate_on(day)` falls back to the latest rate within 7 days, `usdcad_now()` captures the last 5 days then returns today's or the latest rate, None when there is none that recent (the import then files no CAD-against-US$ bid that day rather than a wrong one).
  `database.upsert_fx_rates / get_fx_rates / get_fx_rate_on`. `python fx_rates.py` prints the stored rates. The stored rate is also the hook for a future $/bu view of canola (C$/t x USDCAD-inverse / 44.0925 bushels per tonne): not built, canola is shown as quoted.
- **The history and the switch: `Claude Code/_backups/adm_canada_golive.py`** (dry run by default, `--apply`, `--restore <backup>`, a marker file stops a second apply). Dry run on 10-28: 1,034 stored rows under the three Canadian grain names; 999 convertible with that day's USDCAD (Massive closes since 05-25) and that day's futures settlement
  (`futures_prices`, the nearest earlier one within 6 days for a weekend): corn n=273 posted +111..+220 -> US -65..+29 (median -37), soybeans 388 +326..+502 -> -99..+13 (median -68), wheat 338 +104..+242 -> -112..-25 (median -102). 35 rows are NOT converted but moved to a hidden raw grain ('<grain> [C$ as posted]', inactive in grain_map):
  26 whose futures contract has no settlement in reach (expired ZWU26 / ZWN26, some deferred contracts) and 9 that are not in the mixed convention at all (Bacres Grain's corn -25 / -40 and wheat -95 on its first three days, 06-12 / 06-19 / 06-26: posted below a quarter of the currency gap F x (USDCAD - 1)).
  The conversion of history uses the settlement, not the intraday futures price ADM used, so it is right to within a cent or two; the converted ingest from now on is exact (the payload carries its own futures price). `--apply` also creates `fx_rates` + stores the USDCAD history, un-hides 'Canola (Canadian)', 'Corn (Canadian)', 'Soybeans (Canadian)' in grain_map, and gives Bacres Grain its state (its pin was already there).
  Not touched: 'Canadian Western Red Spring Wheat' (Carberry, 65 rows to 09-10, MGEX has no futures_prices), 'Canadian Eastern Hrww', canola. `grain_seed.json` has the three grains active for a fresh database.
- **Go-live (done 2026-10-27 evening CT, in this order, which mattered):** (1) commit and push (8810378); (2) `git pull --ff-only` on the droplet (it was at 835f7fd, so the pull also carried the heat layer, Basis Maps and wheat commits; the 8 relevant test files pass on its Python 3.12.3 venv); (3) `adm_canada_golive.py` BEFORE the next 15:45 import (it creates `fx_rates`, which the import writes to,
  and un-hides the grains: un-hiding on old code would show the mixed numbers); (4) the Cloud apps need a Reboot for the Bids-tab caption. **What happened:** the first `--apply` was killed by my own 10-minute timeout part-way through ~1,000 one-row UPDATEs on an autocommit connection (644 rows converted, the table, the FX history and the hidden grain names written, nothing else), with its output lost to buffering.
  A second `--apply` would have converted those 644 twice, so the script gained `--finish <backup>` (the final value of a row is a function of its ORIGINAL value alone, which the backup holds: it recomputes all 1,034 rows and writes them in 5 MERGE statements, whatever state they are in, and again with the same result), `--restore` and `--apply` now write by MERGE too, and `python -u` shows progress. `--finish` result:
  999 converted, 35 moved to the hidden grains, 0 rows differ from the plan on read-back, the three grains active (one grain_map row each), Bacres Grain state ON; backup `_backups/adm_canada_golive_20261027_195329.json` (undo = `--restore` it). **Read back through the app's own readers:** the Map tab has 9 ADM Canadian locations with pins (Blenheim corn -37, soybeans -85, wheat -106; Bacres corn -62..-30; Windsor corn -9..+5, soybeans -65..-21; the three prairie canola sites C$/t), and the Bids tab for Blenheim corn shows -37 with the caption and a history from June. **On the droplet** with the repo `.env` loaded (as `auto_import` does at its first lines; a bare script without it finds no MASSIVE_API_KEY and falls back to SQLite, which is only a trap for ad-hoc checks): USD/CAD 1.40502, Blenheim -37 / -85 / -107, Windsor 15 bids, Watson canola as posted.
  **Check after the first real run (Wed 10-28 15:45 CT):** `/opt/basis-tracker/logs/auto_import_*.log` should say `ADM: USD/CAD 1.40...` and file Windsor, ON; a missing rate logs 'USD/CAD unavailable' and skips the C$-against-US$ bids that day.
- **Tests:** `tests/test_adm_canada_fx.py` (41 checks: the real payloads of Blenheim, Windsor, Watson canola and Cedar Rapids IA, every skip case, the rate fetch / store / fallback on a throwaway SQLite, the `auto_import` wiring); 28 deliberate breaks across it and `tests/test_open_windows_tenants.py` all caught.

### Known-unbuilt

- **Ray-Carroll** was built 2026-10-27: see above (`theLocation` + `cmid=all`; no dropdown iteration).
- **United Cooperative** is **not** DTN — it is StoneX/Stonehedge, served via an
  OAuth token exchange plus a streaming gateway. Not requests-scrapeable.
  Left sheet-fed deliberately. (2026-10-25: River Valley Coop's StoneX board turned out to be a plain REST component, `stonex_scraper.py`, so check whether United's page embeds the same `component/bids` iframe before assuming the streaming app.)
