"""Phase 5b: stage the scraper code for the Snowpark stored proc.

Zips the scraper fleet (root *.py + parsers/ package) into basis_scrapers.zip and
PUTs it to @CODE_STAGE, plus cargill_locations.json separately (a zip's data files
aren't readable via __file__, so it's imported as a loose file — cargill_scraper
looks for it in the Snowpark import dir). Works on a trial account (no EAI needed).

Run: python snowflake/06_stage_code.py
"""
import pathlib
import sys
import tempfile
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sf_conn import connect, PROJ  # noqa: E402

DB, SCHEMA, STAGE = "JSA", "BASIS_TRACKER", "CODE_STAGE"
# streamlit-only / not needed by the scrape path — keep the zip lean & import-safe
EXCLUDE = {"app.py", "view_app.py", "_cloud_sim.py"}

conn = connect()
cur = conn.cursor()
cur.execute(f"USE SCHEMA {DB}.{SCHEMA}")
cur.execute(f"CREATE STAGE IF NOT EXISTS {STAGE} DIRECTORY = (ENABLE = TRUE)")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="sf_code_"))
zip_path = tmp / "basis_scrapers.zip"
n_py = 0
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(PROJ.glob("*.py")):
        if p.name in EXCLUDE:
            continue
        z.write(p, p.name)
        n_py += 1
    for p in sorted((PROJ / "parsers").glob("*.py")):
        z.write(p, f"parsers/{p.name}")
        n_py += 1
print(f"zipped {n_py} modules -> {zip_path.name} ({zip_path.stat().st_size/1e3:.0f} KB)")

root = tmp.as_posix()
cur.execute(f"PUT 'file://{root}/basis_scrapers.zip' @{STAGE} OVERWRITE=TRUE AUTO_COMPRESS=FALSE")
cargill = (PROJ / "cargill_locations.json").as_posix()
cur.execute(f"PUT 'file://{cargill}' @{STAGE} OVERWRITE=TRUE AUTO_COMPRESS=FALSE")
cur.execute(f"ALTER STAGE {STAGE} REFRESH")
cur.execute(f"SELECT relative_path, size FROM DIRECTORY(@{STAGE}) ORDER BY relative_path")
print("staged files:")
for r in cur.fetchall():
    print(f"   {r[0]}  ({r[1]} bytes)")
conn.close()
print("\nStaged. Next (paid account): python snowflake/05_scraper_eai.py then 07_scrape_sproc.py")
