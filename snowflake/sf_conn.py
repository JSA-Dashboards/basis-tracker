"""Shared Snowflake connection helper for the migration scripts.
Reads SNOWFLAKE_* from the project .env (never printed). Run directly to test:
    python snowflake/sf_conn.py
"""
import os
import pathlib

from dotenv import load_dotenv

PROJ = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(PROJ / ".env", override=True)


def _load_private_key():
    """RSA private key for Snowflake key-pair auth (the account enforces MFA on
    password sign-ins), as DER bytes; None if not configured (falls back to password).
    Source: SNOWFLAKE_PRIVATE_KEY_PATH (.p8 file) or SNOWFLAKE_PRIVATE_KEY (PEM text)."""
    path = (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH") or "").strip()
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY") or ""
    if not path and not pem.strip():
        return None
    from cryptography.hazmat.primitives import serialization
    data = open(path, "rb").read() if path else pem.replace("\\n", "\n").encode()
    pwd = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PWD") or None
    key = serialization.load_pem_private_key(data, password=pwd.encode() if pwd else None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def connect(database: str | None = None, schema: str | None = None):
    """Open a Snowflake connection from the .env settings. Optional db/schema
    override (used before the target DB exists)."""
    import snowflake.connector as sc

    kw = dict(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        role=os.environ.get("SNOWFLAKE_ROLE") or None,
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE") or None,
        login_timeout=30,
        network_timeout=30,
    )
    pkey = _load_private_key()
    if pkey is not None:
        kw["private_key"] = pkey
    else:
        pwd = os.environ.get("SNOWFLAKE_PASSWORD") or ""
        if not pwd:
            raise SystemExit("No Snowflake auth configured — set SNOWFLAKE_PRIVATE_KEY_PATH "
                             "(key-pair) or SNOWFLAKE_PASSWORD in .env and retry.")
        kw["password"] = pwd
    db = database if database is not None else os.environ.get("SNOWFLAKE_DATABASE")
    sc_ = schema if schema is not None else os.environ.get("SNOWFLAKE_SCHEMA")
    if db:
        kw["database"] = db
    if sc_:
        kw["schema"] = sc_
    return sc.connect(**{k: v for k, v in kw.items() if v is not None})


if __name__ == "__main__":
    # Connect WITHOUT a database (JSA may not exist yet) just to prove auth works.
    try:
        conn = connect(database="", schema="")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print("CONNECTION FAILED:", msg[:400])
        low = msg.lower()
        if "multi-factor" in low or "mfa" in low or "duo" in low:
            print("\n-> MFA appears to be enforced. We'll switch to key-pair auth "
                  "(generate a key, register the public key on the user, point "
                  "SNOWFLAKE_PRIVATE_KEY_PATH at it).")
        elif "incorrect username or password" in low or "not authorized" in low:
            print("\n-> Check SNOWFLAKE_USER / SNOWFLAKE_PASSWORD in .env.")
        elif "account" in low or "could not connect" in low or "name resolution" in low:
            print("\n-> The account identifier may need a different form. Tried: "
                  f"{os.environ.get('SNOWFLAKE_ACCOUNT')!r}. "
                  "Alternatives to try: 'GNC89034.us-east-1.aws' or bare 'GNC89034'.")
        raise SystemExit(1)

    cur = conn.cursor()
    cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_REGION(), CURRENT_ROLE(), "
                "CURRENT_WAREHOUSE(), CURRENT_VERSION()")
    acct, region, role, wh, ver = cur.fetchone()
    print("CONNECTED OK")
    print(f"  account={acct}  region={region}  role={role}  warehouse={wh}")
    print(f"  snowflake version={ver}")
    conn.close()
