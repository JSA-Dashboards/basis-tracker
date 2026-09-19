# Basis Tracker — daily scrape on a DigitalOcean Droplet

Runs `auto_import.py` once each weekday: scrapes every source into Snowflake and
emails the daily Changes report (via Microsoft Graph). This is the same job that
used to run on Kolten's local machine — moving it here means the PDF scrapers
(HEC, Georges) and any future Playwright scrapers run reliably on a server that's
always on. **The Streamlit apps still run on Streamlit Cloud** — this Droplet only
runs the scrape/ETL and writes to the shared Snowflake DB the apps read.

Assumes Ubuntu 22.04/24.04. Adjust `APP_DIR` if you clone somewhere other than
`/opt/basis-tracker` (also edit it at the top of `deploy/run_daily.sh`).

## 1. System prep (once, as root/sudo)

```bash
# Central time so cron + logs line up with the desk (DST handled automatically)
sudo timedatectl set-timezone America/Chicago

sudo apt update
sudo apt install -y git python3-venv python3-pip
```

## 2. Clone + virtualenv + dependencies

```bash
sudo mkdir -p /opt/basis-tracker && sudo chown "$USER" /opt/basis-tracker
git clone https://github.com/JSA-Dashboards/basis-tracker.git /opt/basis-tracker
cd /opt/basis-tracker

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
# requirements.txt = the app; requirements-dev.txt adds pdfplumber (needed by the
# HEC/Georges PDF scrapers) and playwright.
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Playwright (only needed if a Playwright-rendered source is ever added — the
current `dtn_playwright_scraper.SITES` is empty, so you can skip this for now):

```bash
.venv/bin/python -m playwright install --with-deps chromium
```

## 3. Secrets — `.env`

`auto_import.py` reads `/opt/basis-tracker/.env` via `load_dotenv()`. The easiest
path is to copy your **local** `.env` up (it already has everything):

```bash
# from your local machine:
scp ".env" youruser@DROPLET_IP:/opt/basis-tracker/.env
```

It must contain at least:
- `USE_SNOWFLAKE=1` and the full `SNOWFLAKE_*` block (account/user/password/role/warehouse; `SNOWFLAKE_SCHEMA=BASIS_TRACKER`)
- `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` / `GRAPH_SENDER` — so the daily Changes email sends via Graph
- `CHANGES_EMAIL_TO` (defaults to kpostin@jpsi.com) and optional `CHANGES_EMAIL_CC`
- `MASSIVE_S3_ACCESS_KEY` / `MASSIVE_S3_SECRET_KEY` (futures feed). `RIVER_DATABASE_URL` is **not** needed — River FOB data is read from Snowflake now, and `auto_import` doesn't touch it anyway.

Do **not** put `APP_PASSWORD` here — that's only for the Streamlit admin app.

```bash
chmod 600 /opt/basis-tracker/.env    # keep secrets private
```

## 4. Test it once by hand

```bash
cd /opt/basis-tracker
chmod +x deploy/run_daily.sh
# dry run without sending the Changes email:
.venv/bin/python auto_import.py --no-email
# then a full end-to-end via the wrapper (scrape + email):
./deploy/run_daily.sh && tail -n 40 logs/auto_import_*.log | tail -40
```

Confirm the log ends with `auto_import finished … rc=0` and that a Changes email
arrives.

## 5. Install the cron job

Runs 3:45 PM Central, Mon–Fri (matches the desk's old schedule; the weekly
auto-prune still fires on Mondays). Add it to the user's crontab:

```bash
chmod +x deploy/run_daily.sh deploy/run_rail_recap.sh
( crontab -l 2>/dev/null | grep -v -e run_daily.sh -e run_rail_recap.sh
  echo "45 15 * * 1-5 /opt/basis-tracker/deploy/run_daily.sh"
  echo "0 11 * * 1 /opt/basis-tracker/deploy/run_rail_recap.sh" ) | crontab -
crontab -l    # verify
```

Two jobs: the daily scrape+email (3:45 PM Central, Mon–Fri) and the weekly JSA
Rail Basis recap (`run_rail_recap.sh`, 11 AM Central Mondays). (For the daily job
every day including weekends, use `45 15 * * *`.)

## 6. Monitoring

```bash
ls -lt /opt/basis-tracker/logs | head          # newest run first
tail -f /opt/basis-tracker/logs/auto_import_*.log
grep -iE "error|fail|no data" /opt/basis-tracker/logs/auto_import_*.log
```

## Updating the code later

```bash
cd /opt/basis-tracker && git pull
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # only if deps changed
```

---

### Notes
- **cron vs systemd timer:** cron is used here per request. A systemd timer
  (`OnCalendar=Mon..Fri 15:45`, `Persistent=true`) is a sturdier alternative — it
  catches missed runs after a reboot and gives `journalctl` logs. Ask if you want
  that instead.
- The wrapper's `flock` plus `auto_import`'s own single-instance lock mean a long
  run can't collide with the next day's trigger.
- Everything writes to the same **Snowflake** DB the Cloud apps read, so no app
  redeploy is needed for the data to appear — just the ~5 min for the scrape.
