"""
Read-only entry point for the Basis Tracker.

Deploy this file as the Streamlit Cloud "Main file path" to get the view-only
build. Streamlit identifies an app by (repo, branch, main file), so pointing a
second app at THIS file — instead of app.py — gives it its own URL while still
running off master and auto-updating on every push.

It forces VIEW_ONLY on before app.py runs (no VIEW_ONLY secret needed) and then
executes app.py fresh on each Streamlit rerun via runpy. The view app's secrets
need the same backend connection as the main app (the SNOWFLAKE_* block, or
DATABASE_URL on the Postgres rollback path).

This build is ALWAYS an open link: it hands app.py `_OPEN_VIEW_BUILD` through
init_globals, and app.py's _require_password() skips the APP_PASSWORD gate when it
sees it. So even if APP_PASSWORD ends up in this app's Secrets (it has its own
Secrets block, easily re-pasted from the admin app's), the clients who hold the link
are never locked out. Still leave APP_PASSWORD out of this app's Secrets — it is
ignored here, but it is one more secret in the wrong place. The flag is an
in-process global on purpose: no env var or Secret can set it, so it can never open
the admin build (app.py run directly).
"""
import os
import runpy

os.environ["VIEW_ONLY"] = "true"

runpy.run_path(os.path.join(os.path.dirname(__file__), "app.py"),
               init_globals={"_OPEN_VIEW_BUILD": True},
               run_name="__main__")
