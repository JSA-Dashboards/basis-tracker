"""
Read-only entry point for the Basis Tracker.

Deploy this file as the Streamlit Cloud "Main file path" to get the view-only
build. Streamlit identifies an app by (repo, branch, main file), so pointing a
second app at THIS file — instead of app.py — gives it its own URL while still
running off master and auto-updating on every push.

It forces VIEW_ONLY on before app.py runs (no VIEW_ONLY secret needed) and then
executes app.py fresh on each Streamlit rerun via runpy. The view app's secrets
only need DATABASE_URL; leaving APP_PASSWORD unset keeps it an open link.
"""
import os
import runpy

os.environ["VIEW_ONLY"] = "true"

runpy.run_path(os.path.join(os.path.dirname(__file__), "app.py"),
               run_name="__main__")
