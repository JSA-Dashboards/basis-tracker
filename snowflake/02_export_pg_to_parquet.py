"""Phase 2 (staging): export every Postgres table to a Parquet file for the
Snowflake load. Read-only against the live DB. Reads DATABASE_URL from .env
(never printed). Output -> snowflake/export/<table>.parquet (gitignored).

Types are cast from information_schema so Parquet round-trips cleanly into the
01_schema.sql types:
    integer/smallint/bigint  -> nullable Int64
    double precision/real    -> float64
    timestamp with time zone -> UTC datetime
    text/other               -> string

The IDENTITY id columns (snapshots.id, snapshot_rows.id) are exported AS-IS so
snapshot_rows.snapshot_id keeps pointing at the right snapshot; the COPY INTO in
Phase 2 must load those explicit id values (do not let Snowflake re-generate them).

Re-run this right before the real load so the data is fresh (bids change daily).
"""
import json
import pathlib
import sys
import os
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

PROJ = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJ / ".env", override=True)
sys.path.insert(0, str(PROJ))
os.chdir(PROJ)
import database as db

assert db._use_pg(), "NOT on Postgres — aborting (would read empty local SQLite)."

OUT = PROJ / "snowflake" / "export"
OUT.mkdir(parents=True, exist_ok=True)

# 16 live tables (rail_fob_bak_20260721 backup intentionally excluded).
TABLES = [
    "snapshots", "snapshot_rows", "rail_fob", "futures_prices", "futures_history",
    "location_meta", "grain_map", "index_excludes", "imported_emails", "client_reports",
    "nightly_override", "spot_forward_manual", "freight_history", "cif_history",
    "calendar_history", "spreads_history",
]

INT_TYPES = {"integer", "smallint", "bigint"}
FLOAT_TYPES = {"double precision", "real", "numeric"}


def col_types(cur, table: str) -> dict[str, str]:
    cur.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema='public' and table_name=%s order by ordinal_position",
        (table,),
    )
    return {r["column_name"]: r["data_type"] for r in cur.fetchall()}


def main() -> None:
    conn = db.get_conn()
    cur = conn.cursor()
    manifest = {"exported_at": datetime.now(timezone.utc).isoformat(), "tables": {}}

    for t in TABLES:
        types = col_types(cur, t)
        cur.execute(f'select * from "{t}"')
        rows = cur.fetchall()
        df = pd.DataFrame(rows, columns=list(types))

        for c, dt in types.items():
            if dt in INT_TYPES:
                df[c] = pd.array(df[c].tolist(), dtype="Int64")
            elif dt in FLOAT_TYPES:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
            elif "timestamp" in dt:
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
            else:
                df[c] = df[c].astype("string")

        fp = OUT / f"{t}.parquet"
        df.to_parquet(fp, engine="pyarrow", index=False)
        sz = fp.stat().st_size
        manifest["tables"][t] = {"rows": int(len(df)), "bytes": sz,
                                 "columns": list(types)}
        print(f"  {t:22} {len(df):>8,} rows  {sz/1e6:6.2f} MB")

    conn.close()
    (OUT / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(v["rows"] for v in manifest["tables"].values())
    print(f"\nExported {len(TABLES)} tables, {total:,} rows total -> {OUT}")
    print("Manifest: snowflake/export/_manifest.json (use its row counts to verify the Snowflake load).")


if __name__ == "__main__":
    main()
