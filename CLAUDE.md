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

## The rail seasonal charts' 5-yr average and range are Olympic (`seasonal_band.py`)

Kolten 2026-10-20 ("Check the UP Allen station range and average. It seems like they are too high" ... "I think we might have a year thats skewing the data ...
BN Hereford looks the same. I think we need to take an olympic avg approach to make cleaner"). UP Allen Station's 2022/23 had a Sep-Dec average of +59 against -12 to +27
in the other four years of the window (2021/22-2025/26), set the top of the band in 38 weeks of 52, and by itself lifted this week's 5-yr average from -7 to +7; the same
year is high on Group 3, Interior IA and Hereford. It is real market data, not a bad row (Allen's recorded bids were +58 to +85 in mid-2023), so the fix is in the statistic:
for each marketing week the highest and the lowest year are dropped; the average is the mean of what is left and the band runs from the lowest to the highest of what is
left (`olympic_avg` / `olympic_lo` / `olympic_hi`; five years in the week are needed to trim, with fewer the plain mean and min / max are used). Wired into
`rail_report._five_year_band` (the emailed chart; legend '5-yr Olympic avg' / '5-yr range ex hi/lo (2021/22–2025/26)') and the Rail FOB tab's `_rband` in `app.py`
(header, end label and caption say Olympic). `tests/test_seasonal_band.py` (rule, the band frame, and the Vega-Lite spec the email hands the renderer).
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
Fargo and Bunge Altona via `ncga_scraper.py` below. ADM's Canadian locations (Carberry, Lloydminster, Watson) are captured but hidden (`grain_map`:
'Canola (Canadian)' is inactive).

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

### Known-unbuilt

- **Ray-Carroll** (`ray-carroll.com`) is the same aghost family but a
  15-location co-op needing dropdown iteration per location × commodity.
  Bigger job, not started.
- **United Cooperative** is **not** DTN — it is StoneX/Stonehedge, served via an
  OAuth token exchange plus a streaming gateway. Not requests-scrapeable.
  Left sheet-fed deliberately.
