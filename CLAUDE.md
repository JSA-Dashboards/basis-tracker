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
DTN-backed sites are the awkward ones: VistaComm is cracked via a License-Key
JSON proxy (`vistacomm_scraper.py`), aghost/ColdFusion needs a headless render
(`dtn_playwright_scraper.py`).
