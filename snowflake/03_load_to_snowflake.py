"""Phase 2: create JSA.BASIS_TRACKER and load every exported Parquet file.

Flow: create DB -> run 01_schema.sql -> internal stage -> PUT each parquet ->
COPY INTO (MATCH_BY_COLUMN_NAME, preserves the exported IDENTITY ids) ->
verify each table's row count against _manifest.json. Idempotent: TRUNCATEs
each table before COPY, so it can be re-run.

The export dir path contains spaces (breaks Snowflake PUT), so files are copied
to a temp no-spaces dir first.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sf_conn import connect, PROJ  # noqa: E402

EXPORT = PROJ / "snowflake" / "export"
SCHEMA_SQL = PROJ / "snowflake" / "01_schema.sql"
DB = os.environ["SNOWFLAKE_DATABASE"]        # JSA
SCHEMA = os.environ["SNOWFLAKE_SCHEMA"]       # BASIS_TRACKER
STAGE = "parquet_stage"

manifest = json.loads((EXPORT / "_manifest.json").read_text())
tables = list(manifest["tables"])

conn = connect(database="", schema="")
cur = conn.cursor()

print(f"1) CREATE DATABASE {DB} + schema/tables")
cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB}")
cur.execute(f"USE DATABASE {DB}")
conn.execute_string(SCHEMA_SQL.read_text())          # creates schema + 16 tables
cur.execute(f"USE SCHEMA {DB}.{SCHEMA}")
cur.execute(f"CREATE OR REPLACE STAGE {STAGE} FILE_FORMAT = (TYPE = PARQUET)")

# Stage parquet through a temp dir without spaces (PUT can't handle the spaces
# in the OneDrive path).
tmp = pathlib.Path(tempfile.mkdtemp(prefix="sf_load_"))
print(f"2) PUT + COPY INTO  (staging via {tmp})")
results = []
try:
    for t in tables:
        src = EXPORT / f"{t}.parquet"
        dst = tmp / f"{t}.parquet"
        shutil.copy(src, dst)
        put_uri = "file://" + dst.as_posix()          # forward slashes, no spaces
        cur.execute(f"PUT '{put_uri}' @{STAGE}/{t}/ OVERWRITE = TRUE AUTO_COMPRESS = FALSE")
        cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute(
            f"COPY INTO {t} FROM @{STAGE}/{t}/ "
            f"FILE_FORMAT = (TYPE = PARQUET) "
            f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE "
            f"ON_ERROR = ABORT_STATEMENT"
        )
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        got = cur.fetchone()[0]
        exp = manifest["tables"][t]["rows"]
        ok = "OK " if got == exp else "MISMATCH"
        results.append((t, exp, got, ok))
        print(f"   [{ok}] {t:22} loaded {got:>8,} / expected {exp:>8,}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# id preservation check on the two IDENTITY tables
print("3) IDENTITY id-preservation check")
for t in ("snapshots", "snapshot_rows"):
    cur.execute(f"SELECT MIN(id), MAX(id) FROM {t}")
    lo, hi = cur.fetchone()
    print(f"   {t:14} id range {lo} .. {hi}")

bad = [r for r in results if r[3] != "OK "]
print("\n" + ("ALL TABLES MATCH" if not bad else f"{len(bad)} TABLE(S) MISMATCH"))
conn.close()
sys.exit(1 if bad else 0)
