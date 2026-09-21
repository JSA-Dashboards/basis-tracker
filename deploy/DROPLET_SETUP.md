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

## 5b. Failure alerting (shared by every job on the droplet)

cron here has **no `MAILTO` and the box has no MTA**, so a failing job is
silent — the only trace is a log file nobody opens. `deploy/alerting/` fixes
that for every job on this droplet, not just the basis tracker's.

```bash
# install (once, as root)
mkdir -p /opt/alerting
scp deploy/alerting/{cron-alert,notify.py,alert.conf.example} root@<droplet>:/opt/alerting/
ssh root@<droplet> 'cd /opt/alerting && chmod 755 cron-alert notify.py     && cp -n alert.conf.example alert.conf && chmod 600 alert.conf'
ssh root@<droplet> '/usr/bin/python3 /opt/alerting/notify.py --check'   # auth only, sends nothing
```

Then wrap each crontab entry:

```
45 15 * * 1-5 /opt/alerting/cron-alert "Basis tracker daily import"     "/opt/basis-tracker/logs/auto_import_*.log" /opt/basis-tracker/deploy/run_daily.sh
```

`cron-alert "<name>" "<log glob>" <command...>` emails on failure and exits
with the job's own code. It catches three things a bare entry does not:

| | |
|---|---|
| non-zero exit | the ordinary case, otherwise invisible |
| a hung job | killed at `ALERT_TIMEOUT` (default 90m). This matters: `run_*.sh` uses `flock` and **exits 0** when a previous run still holds the lock, so one hang would make every later run "succeed" while doing nothing |
| failure before logging | stdout/stderr is captured separately, so "venv missing" or "not executable" still reaches the email with no job log to quote |

Design notes worth keeping:

- **`notify.py` is standard library only** — no venv, no `msal`, no `requests`.
  The alerter must not share failure modes with the jobs it watches; if an
  app's virtualenv breaks, the alert about it still has to go out.
- **Secrets are stripped from the email.** Job logs get pasted into a message
  that lands in mailboxes and Exchange archives, and a traceback can carry a
  DSN or password. Every secret-looking value in `/opt/*/.env` is replaced
  literally, plus a regex for inline `user:pass@host` credentials.
- **The Graph client secret is not copied here.** `alert.conf` points at
  `/opt/basis-tracker/.env`, so there is one copy on the box to rotate
  (it expires 2028-09-16).
- Every alert is also written to syslog (`journalctl -t cron-alert`), and a
  send that fails is appended to `/opt/alerting/undelivered.log` so a mail
  problem can never hide a job problem.

### External heartbeat (healthchecks.io)

Email alerting runs *on this box*, so it cannot report that the box is down,
that cron died, that a job was quietly dropped from the crontab, or that Graph
auth broke. A dead-man's switch covers exactly that: the job pings on success,
and the service emails when an expected ping does not arrive.

`cron-alert` pings `/start` before the job, the bare slug on success and
`/fail` on failure. **Inert until `HC_PING_KEY` is set in `alert.conf`**, so it
ships safely before the account exists.

1. Free account at https://healthchecks.io — 20 checks, well past the five here.
2. Create a project, then **Settings → Ping key → Create**, and paste it into
   `HC_PING_KEY` in `/opt/alerting/alert.conf`.
3. Checks auto-create on first ping (`create=1`) with slugs derived from the
   job names: `river-fob-vessel-pull`, `basis-tracker-daily-import`,
   `basis-tracker-rail-recap`, `cme-feeder-index-update`, `cme-ftp-check`.
4. In each check set **Schedule → Cron**, paste the crontab expression,
   timezone `America/Chicago`, **Grace Time 2h**. Two hours is deliberate:
   `ALERT_TIMEOUT` kills anything past 90m, so a healthy run can never exceed
   it and a slow day will not page you.
5. Add recipients under Integrations. Use a second address so alerting is not
   tied to one person being on holiday.

The ping can never affect the job: it runs outside the job's redirect, always
returns 0, and a failed ping is logged to syslog rather than swallowed. Verify
with a bogus key — the job's exit code must be unchanged:

```bash
printf 'HC_PING_KEY = "x"
HC_BASE_URL = "https://httpbin.org"
' >> /tmp/t.conf
ALERT_CONF=/tmp/t.conf /opt/alerting/cron-alert "t" "/tmp/n_*.log" /bin/true; echo $?   # 0
```

Only job names and timings leave the network. No data does.

Test without touching real jobs:

```bash
ALERT_DRY_RUN=1 /opt/alerting/cron-alert "test" "/tmp/none_*.log" /bin/bash -c 'exit 3'
```

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
