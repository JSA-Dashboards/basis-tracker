"""The Trends tab's outlier list must be READABLE by a login that cannot create tables.

    python tests/test_index_excludes_readonly.py

2026-11-10: the client portal runs as a SELECT-only Snowflake role. get_index_excludes() used to run
CREATE TABLE IF NOT EXISTS before its SELECT, Snowflake refused that statement for the role even though the table
exists, and the whole Trends tab failed for clients. It now reads first and creates only when the read fails; a login
that cannot create gets an empty set. Throwaway SQLite, never Snowflake.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
for k in ("USE_SNOWFLAKE", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY", "DATABASE_URL"):
    os.environ[k] = ""

import database  # noqa: E402

database.DB_PATH = Path(tempfile.mkdtemp(prefix="idx_ro_")) / "t.db"
assert database._backend() == "sqlite", "refusing to run on a non-sqlite backend"
database.init_db()

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)[:300]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def refuse(conn, c):
    raise RuntimeError("SQL access control error: Insufficient privileges to operate on schema 'BASIS_TRACKER'")


print("normal login (can create)")
check("no table yet: created on first read, nothing excluded", database.get_index_excludes() == set())
database.set_index_excludes({("ADM", "Windsor, ON"), ("CHS", "Hallock")})
check("stored outliers come back", database.get_index_excludes() == {("ADM", "Windsor, ON"), ("CHS", "Hallock")})

print("read-only login (CREATE refused, table exists)")
with mock.patch.object(database, "_ensure_index_excludes", refuse):
    got = None
    try:
        got = database.get_index_excludes()
    except Exception as e:  # the old behaviour
        check("get_index_excludes does not raise", False, e)
    check("the stored list is still read", got == {("ADM", "Windsor, ON"), ("CHS", "Hallock")}, got)

print("read-only login, table missing")
database.DB_PATH = Path(tempfile.mkdtemp(prefix="idx_ro2_")) / "t2.db"
database.init_db()
with mock.patch.object(database, "_ensure_index_excludes", refuse):
    try:
        got = database.get_index_excludes()
        check("nothing excluded, no error", got == set(), got)
    except Exception as e:
        check("get_index_excludes does not raise", False, e)

print()
print("FAILED: %s" % FAILS if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
