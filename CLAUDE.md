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
number looks wrong, do not "fix" it by pointing back at Postgres. `DATABASE_URL`
remains in secrets only as a fallback and should eventually be removed so a
Snowflake outage fails loudly instead of quietly serving July data.

## This repo deploys as two separate Streamlit apps

Streamlit identifies an app by *(repo, branch, main file)*, so one repo serves
two:

| main file | app | password |
|---|---|---|
| `app.py` | full admin build | `APP_PASSWORD` set |
| `view_app.py` | read-only client build | `APP_PASSWORD` **unset** — that is what keeps it an open link |

`view_app.py` forces `VIEW_ONLY=true` then runs `app.py` via `runpy`, so it
needs the same backend secrets. **Do not add `APP_PASSWORD` to the view app** —
it would lock out the clients who hold that URL.

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
