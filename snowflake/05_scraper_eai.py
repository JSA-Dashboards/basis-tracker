"""Phase 5a: External Access Integration for the scraper fleet.

Derives every external host the scrapers hit (from the *scraper*.py / futures /
parser modules), builds a Snowflake NETWORK RULE (egress) allowing each on
:443 and :80, and an EXTERNAL ACCESS INTEGRATION referencing it. Snowpark stored
procs/tasks that fetch cash bids attach this EAI. Re-runnable (CREATE OR REPLACE).

Run: python snowflake/05_scraper_eai.py
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sf_conn import connect, PROJ  # noqa: E402

DB, SCHEMA = "JSA", "BASIS_TRACKER"
RULE = "CASH_BID_EGRESS"
EAI = "CASH_BID_ACCESS"

# derive hosts from the scraper/parser/futures modules
hosts: set[str] = set()
globs = ["*scraper*.py", "*futures*.py", "futures_curve.py", "adm_futures.py",
         "massive_futures.py", "parsers/*.py"]
for g in globs:
    for f in PROJ.glob(g):
        for m in re.findall(r"https?://([a-zA-Z0-9.-]+)", f.read_text(encoding="utf-8", errors="ignore")):
            h = m.strip().lower()
            if h and "." in h and not h.startswith(("www.w3", "schemas", "localhost")):
                hosts.add(h)
# always include the futures-curve data hosts
hosts |= {"files.massive.com", "api.massive.com", "adm.gradable.com", "api.dtn.com"}
hosts = sorted(hosts)
print(f"{len(hosts)} hosts")

value_list = ", ".join(f"'{h}:443', '{h}:80'" for h in hosts)

conn = connect()
cur = conn.cursor()
cur.execute(f"USE SCHEMA {DB}.{SCHEMA}")
print("creating network rule…")
cur.execute(
    f"CREATE OR REPLACE NETWORK RULE {RULE} "
    f"MODE = EGRESS TYPE = HOST_PORT VALUE_LIST = ({value_list})")
print("creating external access integration…")
cur.execute(
    f"CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION {EAI} "
    f"ALLOWED_NETWORK_RULES = ({DB}.{SCHEMA}.{RULE}) ENABLED = TRUE")
cur.execute(f"SHOW EXTERNAL ACCESS INTEGRATIONS LIKE '{EAI}'")
print("EAI created:", [r[0] for r in cur.fetchall()])
print(f"\nNetwork rule {RULE}: {len(hosts)} hosts × (443,80). EAI = {EAI}.")
conn.close()
