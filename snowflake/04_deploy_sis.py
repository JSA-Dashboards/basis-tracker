"""Phase 4: deploy the basis tracker as a Streamlit-in-Snowflake app.

Stages all app modules + assets + an environment.yml to an internal stage, then
CREATE OR REPLACE STREAMLIT. The app auto-detects the SiS active session (see
database.py _backend / _sf_active_session_conn) → reads/writes JSA.BASIS_TRACKER
with no credentials. Re-runnable.

Note: live Palmetto scrape + live Massive S3 curve need an External Access
Integration to work inside SiS; until then those specific fetches degrade
(the app reads stored curves from the DB first, so it's rarely hit).
"""
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sf_conn import connect, PROJ  # noqa: E402

DB = os.environ["SNOWFLAKE_DATABASE"]        # JSA
SCHEMA = os.environ["SNOWFLAKE_SCHEMA"]       # BASIS_TRACKER
STAGE = "APP_STAGE"
APP = "BASIS_TRACKER_APP"
WH = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")

ENVIRONMENT_YML = """\
name: app_environment
channels:
  - snowflake
dependencies:
  - streamlit=1.52.2
  - altair=6.2.2
  - python-dotenv
  - pandas
  - numpy
  - pydantic
  - holidays
  - pillow
  - vl-convert-python
  - openpyxl
  - pydeck
  - requests
  - beautifulsoup4
  - boto3
"""

conn = connect()
cur = conn.cursor()
cur.execute(f"USE SCHEMA {DB}.{SCHEMA}")
cur.execute(f"CREATE STAGE IF NOT EXISTS {STAGE} "
            f"DIRECTORY = (ENABLE = TRUE) FILE_FORMAT = (TYPE = 'CSV')")

# Assemble the app bundle in a temp dir without spaces (PUT can't handle the
# spaces in the OneDrive project path).
tmp = pathlib.Path(tempfile.mkdtemp(prefix="sis_deploy_"))
try:
    n_py = 0
    for p in PROJ.glob("*.py"):
        shutil.copy(p, tmp / p.name)
        n_py += 1
    (tmp / "environment.yml").write_text(ENVIRONMENT_YML)
    assets_src = PROJ / "assets"
    assets_tmp = tmp / "assets"
    assets_tmp.mkdir(exist_ok=True)
    n_asset = 0
    if assets_src.is_dir():
        for a in assets_src.iterdir():
            if a.is_file():
                shutil.copy(a, assets_tmp / a.name)
                n_asset += 1
    print(f"Bundled {n_py} .py + environment.yml + {n_asset} asset(s)")

    root = tmp.as_posix()
    print("PUT app files -> stage ...")
    cur.execute(f"PUT 'file://{root}/*.py' @{STAGE} OVERWRITE=TRUE AUTO_COMPRESS=FALSE")
    cur.execute(f"PUT 'file://{root}/environment.yml' @{STAGE} OVERWRITE=TRUE AUTO_COMPRESS=FALSE")
    if n_asset:
        cur.execute(f"PUT 'file://{root}/assets/*' @{STAGE}/assets/ OVERWRITE=TRUE AUTO_COMPRESS=FALSE")
    cur.execute(f"ALTER STAGE {STAGE} REFRESH")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

cur.execute(f"SELECT COUNT(*) FROM DIRECTORY(@{STAGE})")
print("files on stage:", cur.fetchone()[0])

print("CREATE STREAMLIT ...")
cur.execute(f"""
    CREATE OR REPLACE STREAMLIT {DB}.{SCHEMA}.{APP}
      ROOT_LOCATION = '@{DB}.{SCHEMA}.{STAGE}'
      MAIN_FILE = 'app.py'
      QUERY_WAREHOUSE = {WH}
      TITLE = 'Basis Tracker'
""")
cur.execute(f"SHOW STREAMLITS LIKE '{APP}' IN SCHEMA {DB}.{SCHEMA}")
rows = cur.fetchall()
print("\nStreamlit object created:")
cur.execute(f"DESC STREAMLIT {DB}.{SCHEMA}.{APP}")
for r in cur.fetchall():
    if r[0].lower() in ("name", "url_id", "main_file", "query_warehouse", "title"):
        print(f"   {r[0]}: {r[1]}")
print("\nOpen it in Snowsight: Projects -> Streamlit -> Basis Tracker "
      f"(under {DB}.{SCHEMA}).")
conn.close()
