"""The Streamlit keep-alive + health-check job on the droplet (deploy/warm-ping/ping.sh and health_alert.py).

    python tests/test_warm_ping.py

2026-11-09: the admin Cloud app died and stayed down for hours while /opt/warm-ping/ping.log said "200" every 10 minutes, because the
job only loaded each app's WRAPPER page (it answers 200 even when the app behind it is dead). ping.sh now also asks the app's real
health endpoint (/~/+/_stcore/health must say "ok") and e-mails kpostin@ through /opt/alerting/notify.py when it does not.

Nothing here touches the network or sends mail: curl is a stub that serves a scripted scenario, the alert command is a stub that records
its arguments, and health_alert.py runs with ALERT_DRY_RUN=1. Needs bash (Git Bash on Windows, anything on the droplet).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "warm-ping" / "ping.sh"
ALERT_PY = ROOT / "deploy" / "warm-ping" / "health_alert.py"
FAILS = []
RCS = []          # the exit code of every ping.sh run below


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:500]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


BASH = shutil.which("bash")
if BASH:
    probe = subprocess.run([BASH, "-c", 'echo "v=${BASH_VERSION:-none}"; command -v head >/dev/null && echo head-ok'], capture_output=True, text=True)
    if "v=none" in probe.stdout or "head-ok" not in probe.stdout:
        BASH = None
if not BASH:
    print("  [SKIP] no usable bash on this machine")
    sys.exit(0)

CURL_STUB = r"""#!/usr/bin/env bash
# fake curl: the last argument is the URL; FAKE_DIR holds the scenario
url="${@: -1}"
echo "$url" >> "$FAKE_DIR/calls.txt"
case "$url" in
  *graph.microsoft.com*) cat "$FAKE_DIR/control" 2>/dev/null || echo 404 ;;
  */_stcore/health)
    host="${url#https://}"; host="${host%%/*}"
    q="$FAKE_DIR/health_${host}.queue"
    if [ -s "$q" ]; then line=$(head -n1 "$q"); tail -n +2 "$q" > "$q.tmp"; mv "$q.tmp" "$q"
    else line=$(cat "$FAKE_DIR/health_${host}" 2>/dev/null || echo "200 ok"); fi
    code="${line%% *}"; body="${line#* }"
    printf '%s\n%s' "$body" "$code" ;;
  *)
    host="${url#https://}"; host="${host%%/*}"
    cat "$FAKE_DIR/warm_${host}" 2>/dev/null || echo "200 0.100s" ;;
esac
exit 0
"""
ALERT_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DIR/alerts.txt"
exit "$(cat "$FAKE_DIR/alert_rc" 2>/dev/null || echo 0)"
"""


class Scenario:
    """A fresh scratch directory with the stubs, a scripted network, and a way to run ping.sh against it."""

    def __init__(self, hosts="app-a.streamlit.app app-b.streamlit.app", urls=None):
        self.dir = Path(tempfile.mkdtemp(prefix="warm_ping_"))
        self.fake = self.dir / "fake"
        self.fake.mkdir()
        self.state = self.dir / "state"
        self.log = self.dir / "ping.log"
        for name, body in (("curl", CURL_STUB), ("alert", ALERT_STUB)):
            p = self.dir / name
            p.write_text(body, encoding="utf-8", newline="\n")
            p.chmod(0o755)
        self.hosts = hosts
        self.urls = urls or " ".join("https://%s/?view=1" % h for h in hosts.split())

    def set(self, name, text):
        (self.fake / name).write_text(text + "\n", encoding="utf-8", newline="\n")

    def clear(self, name):
        (self.fake / name).unlink(missing_ok=True)

    def run(self, now):
        env = dict(os.environ, FAKE_DIR=self.fake.as_posix(), WARM_LOG=self.log.as_posix(), WARM_STATE=self.state.as_posix(),
                   WARM_CURL=(self.dir / "curl").as_posix(), WARM_ALERT=(self.dir / "alert").as_posix(), WARM_SLEEP="0",
                   WARM_NOW=str(now), WARM_URLS=self.urls, HEALTH_HOSTS=self.hosts)
        r = subprocess.run([BASH, SCRIPT.as_posix()], env=env, capture_output=True, text=True, timeout=180)
        RCS.append(r.returncode)
        return r

    def alerts(self):
        p = self.fake / "alerts.txt"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def calls(self):
        p = self.fake / "calls.txt"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def logtext(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def st(self, host, kind):
        p = self.state / ("%s.%s" % (host.replace(".", "_"), kind))
        return p.read_text(encoding="utf-8").strip() if p.exists() else None


T0 = 1_794_000_000          # a fixed 'now' (the job takes it from WARM_NOW)
A, B = "app-a.streamlit.app", "app-b.streamlit.app"

print("healthy apps")
s = Scenario()
r = s.run(T0)
check("exits 0 and says nothing on stdout/stderr", r.returncode == 0 and not r.stdout.strip() and not r.stderr.strip(), r.stderr)
check("each app's wrapper page is still warmed, in the old log format", all(re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d 200 0\.100s https://%s/\?view=1$" % re.escape(h), s.logtext(), re.M) for h in (A, B)), s.logtext())
check("... and each app's health endpoint was asked", ("https://%s/~/+/_stcore/health" % A) in s.calls() and ("https://%s/~/+/_stcore/health" % B) in s.calls(), s.calls())
check("a healthy run logs 'HEALTH ok' per app, sends no alert and leaves no state", s.logtext().count("HEALTH ok https://") == 2 and not s.alerts() and not list(s.state.glob("*.fails")), s.logtext())

print("\na failing app: one run is not enough, two are")
s = Scenario()
s.set("health_%s" % A, "503 application is down")
r = s.run(T0)
check("first failing run: counted (1), logged with what the endpoint said, NOT alerted", s.st(A, "fails") == "1" and "HEALTH FAIL(1) https://%s FAIL 503 application is down" % A in s.logtext() and not s.alerts(), s.logtext())
check("... the failing app was asked three times (a restart blip gets two more tries), the healthy one once", s.calls().count("https://%s/~/+/_stcore/health" % A) == 3 and s.calls().count("https://%s/~/+/_stcore/health" % B) == 1, s.calls())
check("... the healthy neighbour is untouched", s.st(B, "fails") is None and "HEALTH ok https://%s" % B in s.logtext())
r = s.run(T0 + 600)
al = s.alerts()
check("second consecutive failing run: ONE 'down' alert for that app", len(al) == 1 and "--state down --app %s" % A in al[0] and "--fails 2" in al[0] and "FAIL 503 application is down" in al[0], al)
check("... carrying when it started failing", ("--since %d" % T0) in al[0], al)
check("... and the alert is remembered", s.st(A, "alerted") == str(T0 + 600))
s.run(T0 + 1200)
check("third failing run: no second alert", len(s.alerts()) == 1, s.alerts())
s.run(T0 + 600 + 21600)
al = s.alerts()
check("six hours after the alert: a 'still-down' reminder", len(al) == 2 and "--state still-down --app %s" % A in al[1], al)
check("... and not another one on the very next run", (s.run(T0 + 600 + 21600 + 600).returncode == 0) and len(s.alerts()) == 2, s.alerts())
s.clear("health_%s" % A)
s.run(T0 + 40000)
al = s.alerts()
check("when the app answers again: one 'recovered' alert naming when it went down", len(al) == 3 and "--state recovered --app %s" % A in al[2] and ("--since %d" % T0) in al[2], al)
check("... and all of that app's state is cleared", all(s.st(A, k) is None for k in ("fails", "since", "alerted", "detail")))
s.run(T0 + 40600)
check("a healthy run after that sends nothing more", len(s.alerts()) == 3)

print("\nonly HTTP 200 with the body 'ok' is healthy")
s = Scenario()
s.set("health_%s" % A, "200 <!DOCTYPE html><title>Streamlit</title>")
s.run(T0)
s.run(T0 + 600)
check("a 200 with any other body (a proxy or wrapper page) is a failure, not a pass", s.st(A, "fails") == "2" and len(s.alerts()) == 1 and "--state down --app %s" % A in s.alerts()[0], s.alerts())
s = Scenario()
s.set("health_%s" % A, "200 ok")
s.run(T0)
check("... while a plain 'ok' passes", s.st(A, "fails") is None and not s.alerts())

print("\nblips, no network, a mail that fails")
s = Scenario()
(s.fake / ("health_%s.queue" % A)).write_text("503 application is down\n200 ok\n", encoding="utf-8", newline="\n")
s.run(T0)
check("one failed try followed by a good one is not a failure", s.st(A, "fails") is None and "HEALTH ok https://%s" % A in s.logtext() and s.calls().count("https://%s/~/+/_stcore/health" % A) == 2, s.logtext())
s = Scenario()
s.set("health_%s" % A, "000 ")
s.set("control", "000")
for i in range(3):
    s.run(T0 + 600 * i)
check("no network on the droplet (the control request fails): nothing counted, nothing alerted", not s.alerts() and s.st(A, "fails") is None and s.logtext().count("HEALTH skipped: no network") == 3, s.logtext())
s = Scenario()
s.set("health_%s" % A, "502 ")
s.set("alert_rc", "1")
s.run(T0)
s.run(T0 + 600)
check("an alert that cannot be sent is not remembered: it is tried again on the next run", len(s.alerts()) == 1 and s.st(A, "alerted") is None and "could not be sent" in s.logtext(), s.logtext())
s.run(T0 + 1200)
check("... and tried again", len(s.alerts()) == 2)
s.clear("alert_rc")
s.run(T0 + 1800)
check("... until it goes through", len(s.alerts()) == 3 and s.st(A, "alerted") == str(T0 + 1800))

print("\nwhat is and is not checked")
s = Scenario(urls="https://%s/?view=1 https://dead.streamlit.app/?view=1" % A)
s.set("warm_dead.streamlit.app", "404 0.200s")
s.run(T0)
check("an app listed only for warming (the long-dead jsa-us-rail-dashboard) is requested and logged but never health-checked or alerted",
      "404 0.200s https://dead.streamlit.app/?view=1" in s.logtext() and not any("dead.streamlit.app/~/+/_stcore" in c for c in s.calls()) and not s.alerts(), s.calls())
s = Scenario()
s.log.write_text("".join("old line %d\n" % i for i in range(2500)), encoding="utf-8", newline="\n")
s.run(T0)
lines = s.logtext().splitlines()
check("the log is trimmed to the last 2000 lines, newest kept", len(lines) == 2000 and "HEALTH ok" in lines[-1] and lines[0] == "old line %d" % (2500 + 4 - 2000), (len(lines), lines[:1], lines[-1:]))
check("every ping.sh run above (%d of them, failing apps, no network, dead mail path included) exited 0: cron never sees a failure" % len(RCS), len(RCS) >= 15 and set(RCS) == {0}, RCS)

print("\nhealth_alert.py (dry run: prints the mail, sends nothing)")
conf = Path(tempfile.mkdtemp(prefix="warm_conf_")) / "alert.conf"
conf.write_text('ALERT_TO = "someone@example.com"\nGRAPH_ENV_FILE = "/nonexistent/.env"\n', encoding="utf-8")


def alert(*args, dry=True, conf_path=conf):
    env = dict(os.environ, ALERT_DIR=(ROOT / "deploy" / "alerting").as_posix(), ALERT_CONF=conf_path.as_posix())
    if dry:
        env["ALERT_DRY_RUN"] = "1"
    return subprocess.run([sys.executable, ALERT_PY.as_posix(), *args], env=env, capture_output=True, text=True, timeout=60)


r = alert("--state", "down", "--app", "jsa-basis-tracker.streamlit.app", "--since", str(T0), "--now", str(T0 + 1800), "--fails", "3", "--detail", "FAIL 503 application is down")
out = r.stdout
check("a 'down' mail names the app, what the endpoint said, how long and how to fix it", r.returncode == 0 and "DRY RUN" in out and "Streamlit app DOWN: Basis Tracker (admin)" in out and "FAIL 503 application is down" in out
      and "30 min" in out and "Reboot app" in out and "/~/+/_stcore/health" in out and "3" in out, out)
check("... it explains why the keep-alive's 200 was no help", "wrapper page" in out, out)
r = alert("--state", "still-down", "--app", "river-fob.streamlit.app", "--since", str(T0), "--now", str(T0 + 7500), "--fails", "12", "--detail", "FAIL 000 <empty>")
check("a reminder says STILL DOWN and for how long", "STILL DOWN: River FOB Portal (2 h 05 min)" in r.stdout, r.stdout)
r = alert("--state", "recovered", "--app", "jsa-basis-tracker.streamlit.app", "--since", str(T0), "--now", str(T0 + 4000))
check("a recovery mail says the app is back and how long it was down", "back up: Basis Tracker (admin)" in r.stdout and "answering again" in r.stdout and "66 min" in r.stdout.replace("1 h 06 min", "66 min"), r.stdout)
r = alert("--state", "test")
check("a test mail says nothing is wrong", "TEST" in r.stdout and "Nothing is wrong" in r.stdout, r.stdout)
r = alert("--state", "down", "--app", "unknown-app.streamlit.app", "--since", str(T0), "--now", str(T0 + 700), "--fails", "2")
check("an app it has no name for still gets a useful mail", r.returncode == 0 and "unknown-app.streamlit.app" in r.stdout and "no answer" in r.stdout, r.stdout)
r = alert("--state", "down")
check("--app is required for anything but a test (exit 2, even in a dry run, so it is not hidden behind the config check)", r.returncode == 2 and "--app is required" in r.stderr, r.stderr)
r = alert("--state", "down", "--app", "x.streamlit.app", dry=False, conf_path=conf)
check("a real send with no Graph credentials is refused as misconfigured (exit 2), never a crash", r.returncode == 2 and "misconfigured" in r.stderr and "GRAPH_TENANT_ID" in r.stderr, r.stderr)

if FAILS:
    print("\n%d FAILED:" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nall warm-ping checks passed")
