"""Phase 5c: create the scraper stored procedure + scheduled task.

REQUIRES A PAID SNOWFLAKE ACCOUNT — the procedure attaches the CASH_BID_ACCESS
External Access Integration (outbound HTTP), which trial accounts reject. Run,
in order, once the account is paid:
    python snowflake/05_scraper_eai.py    # network rule + EAI
    python snowflake/06_stage_code.py     # (already staged; re-run to refresh code)
    python snowflake/07_scrape_sproc.py   # this

Then test immediately with:  CALL JSA.BASIS_TRACKER.RUN_SCRAPERS();
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sf_conn import connect  # noqa: E402

DB, SCHEMA, STAGE, EAI, WH = "JSA", "BASIS_TRACKER", "CODE_STAGE", "CASH_BID_ACCESS", "COMPUTE_WH"

conn = connect()
cur = conn.cursor()
cur.execute(f"USE SCHEMA {DB}.{SCHEMA}")

print("creating stored procedure RUN_SCRAPERS…")
cur.execute(f"""
CREATE OR REPLACE PROCEDURE {DB}.{SCHEMA}.RUN_SCRAPERS()
  RETURNS STRING
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python', 'httpx', 'requests', 'beautifulsoup4',
              'pydantic', 'boto3', 'botocore', 'urllib3')
  IMPORTS = ('@{STAGE}/basis_scrapers.zip', '@{STAGE}/cargill_locations.json')
  HANDLER = 'sf_scrape_task.run'
  EXTERNAL_ACCESS_INTEGRATIONS = ({EAI})
""")
print("  ok")

print("creating daily TASK SCRAPE_DAILY (05:00 America/Chicago)…")
cur.execute(f"""
CREATE OR REPLACE TASK {DB}.{SCHEMA}.SCRAPE_DAILY
  WAREHOUSE = {WH}
  SCHEDULE = 'USING CRON 0 5 * * * America/Chicago'
  AS CALL {DB}.{SCHEMA}.RUN_SCRAPERS()
""")
cur.execute(f"ALTER TASK {DB}.{SCHEMA}.SCRAPE_DAILY RESUME")
print("  task created + resumed")
print("\nDeployed. Test now:  CALL JSA.BASIS_TRACKER.RUN_SCRAPERS();")
conn.close()
