"""
Microsoft Graph API email client — adapted for Streamlit (no FastAPI needed).
Uses MSAL device-code flow; token cached in a local JSON file.
"""
import json
import os
from pathlib import Path
from typing import Optional

import msal
import httpx

from parsers.adm_parser import parse_adm_email
from models import ParsedSnapshot

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES     = ["Mail.Read"]

ADM_KEYWORDS     = ["adm decatur", "adm cedar rapids", "adm st louis", "adm mendota"]
MENDOTA_KEYWORDS = ["mendota grain bids", "mendota", "ottawa", "havana", "burlington", "clinton proc"]

TOKEN_CACHE_PATH = Path(__file__).parent / ".token_cache.json"


def _make_app(client_id: str, tenant_id: str, cache: msal.SerializableTokenCache):
    return msal.PublicClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )


def load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    return cache


def save_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())


def get_token(client_id: str, tenant_id: str) -> Optional[str]:
    cache = load_cache()
    app   = _make_app(client_id, tenant_id, cache)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_cache(cache)
            return result["access_token"]
    return None


def is_authenticated(client_id: str, tenant_id: str) -> bool:
    return bool(get_token(client_id, tenant_id))


def get_account_name(client_id: str, tenant_id: str) -> Optional[str]:
    cache    = load_cache()
    app      = _make_app(client_id, tenant_id, cache)
    accounts = app.get_accounts()
    return accounts[0].get("username") if accounts else None


def start_device_flow(client_id: str, tenant_id: str) -> dict:
    """Returns {user_code, verification_uri, message, _flow_obj}."""
    if not client_id:
        return {"error": "AZURE_CLIENT_ID not set in .env"}
    cache = load_cache()
    app   = _make_app(client_id, tenant_id, cache)
    flow  = app.initiate_device_flow(scopes=SCOPES)
    if "error" in flow:
        return {"error": f"{flow.get('error')}: {flow.get('error_description', 'unknown error')}"}
    return {
        "user_code":        flow.get("user_code"),
        "verification_uri": flow.get("verification_uri"),
        "message":          flow.get("message"),
        "_flow":            flow,   # keep in session_state to complete later
    }


def complete_device_flow(client_id: str, tenant_id: str, flow: dict) -> bool:
    cache = load_cache()
    app   = _make_app(client_id, tenant_id, cache)
    result = app.acquire_token_by_device_flow(flow["_flow"])
    save_cache(cache)
    return "access_token" in result


def _get(client_id, tenant_id, path, params=None):
    token = get_token(client_id, tenant_id)
    if not token:
        raise RuntimeError("Not authenticated")
    r = httpx.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params, timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _get_bytes(client_id, tenant_id, path):
    token = get_token(client_id, tenant_id)
    r = httpx.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.content


def fetch_bid_emails(client_id, tenant_id, max_results=50) -> list[dict]:
    # Fetch recent emails from all folders, filter by subject keywords
    data = _get(client_id, tenant_id,
        "/me/messages",
        {"$top": max_results,
         "$select": "id,subject,receivedDateTime,from,hasAttachments",
         "$orderby": "receivedDateTime desc"},
    )
    messages = []
    for m in data.get("value", []):
        subj = (m.get("subject") or "").lower()
        is_bid = any(k in subj for k in ADM_KEYWORDS + MENDOTA_KEYWORDS)
        if not is_bid:
            continue
        provider = "Mendota" if "mendota grain" in subj else "ADM"
        messages.append({
            "id":            m["id"],
            "subject":       m.get("subject", ""),
            "receivedAt":    m.get("receivedDateTime", ""),
            "sender":        m.get("from", {}).get("emailAddress", {}).get("address", ""),
            "hasAttachment": m.get("hasAttachments", False),
            "provider":      provider,
        })
    return messages


def get_email_body(client_id, tenant_id, email_id: str) -> str:
    """Fetch plain text version of an email body for debugging."""
    data = _get(client_id, tenant_id,
        f"/me/messages/{email_id}",
        {"$select": "subject,body"})
    body = data.get("body", {})
    content = body.get("content", "")
    # Strip HTML tags for readability
    from bs4 import BeautifulSoup
    if body.get("contentType", "").lower() == "html":
        content = BeautifulSoup(content, "html.parser").get_text(separator="\n")
    # Collapse blank lines
    lines = [l for l in content.splitlines() if l.strip()]
    return "\n".join(lines)


def _search_messages(client_id, tenant_id, search_term, max_results=25):
    """Search messages mailbox-wide for a single search term."""
    data = _get(client_id, tenant_id,
        "/me/messages",
        {"$top": max_results,
         "$select": "id,subject,receivedDateTime,from,hasAttachments",
         "$search": f'"{search_term}"'},
    )
    return data.get("value", [])


def _search_messages_paged(client_id, tenant_id, search_term, max_results=500):
    """
    Paginated mailbox-wide search — follows @odata.nextLink until done or max_results hit.
    Use for retroactive / bulk imports.
    """
    token = get_token(client_id, tenant_id)
    if not token:
        raise RuntimeError("Not authenticated")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE}/me/messages"
    params = {
        "$top": 50,
        "$select": "id,subject,receivedDateTime,from,hasAttachments",
        "$search": f'"{search_term}"',
    }
    results = []
    first = True
    while url and len(results) < max_results:
        r = httpx.get(url, headers=headers,
                      params=params if first else None,
                      timeout=60)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        first = False
    return results[:max_results]


# Subjects that belong to the 3 main ADM locations (not Mendota)
_ADM_3_KEYWORDS = ["adm decatur", "adm cedar rapids", "adm st louis", "adm st. louis"]


def _get_child_folder_ids(client_id: str, tenant_id: str,
                           parent: str = "inbox", depth: int = 2) -> list[str]:
    """
    Recursively collect child folder IDs under `parent` up to `depth` levels.
    parent can be a well-known name ("inbox") or a folder ID.
    Returns a flat list of folder IDs (not including the parent itself).
    """
    if depth == 0:
        return []
    token = get_token(client_id, tenant_id)
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE}/me/mailFolders/{parent}/childFolders"
    params = {"$top": 50, "$select": "id,displayName"}
    ids: list[str] = []
    first = True
    while url:
        r = httpx.get(url, headers=headers,
                      params=params if first else None,
                      timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        for f in data.get("value", []):
            ids.append(f["id"])
            ids.extend(_get_child_folder_ids(client_id, tenant_id, f["id"], depth - 1))
        url = data.get("@odata.nextLink")
        first = False
    return ids


def _fetch_folder_year(client_id: str, tenant_id: str,
                        folder_id: str, year: int) -> list[dict]:
    """
    Fetch all message metadata from one specific folder for one calendar year.
    Uses $filter on receivedDateTime — no search-index limit, no depth cap.
    """
    token = get_token(client_id, tenant_id)
    if not token:
        raise RuntimeError("Not authenticated")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages"
    params = {
        "$top": 50,
        "$select": "id,subject,receivedDateTime,from,hasAttachments",
        "$filter": (
            f"receivedDateTime ge {year}-01-01T00:00:00Z "
            f"and receivedDateTime lt {year + 1}-01-01T00:00:00Z"
        ),
    }
    results: list[dict] = []
    first = True
    while url:
        r = httpx.get(url, headers=headers,
                      params=params if first else None,
                      timeout=60)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        first = False
    return results


def fetch_all_bid_emails_retroactive(
    client_id: str,
    tenant_id: str,
    adm_only: bool = True,
    start_year: int = 2020,
) -> tuple[list[dict], dict[str, int]]:
    """
    Full-history retroactive fetch using two complementary strategies:

    Strategy 1 — Exact-subject $search (fast, searches ALL folders including
    subfolders, but limited to ~1 year of search-index depth).

    Strategy 2 — Year-by-year $filter scan of inbox + every inbox subfolder
    (no date limit, explicit folder traversal bypasses search-index age cap).

    Results are deduplicated and sorted newest-first.
    Returns (emails_list, year_counts_dict).
    """
    from datetime import date
    from collections import Counter

    current_year = date.today().year
    seen_ids: set[str] = set()
    all_bid: list[dict] = []

    def _add(m: dict) -> None:
        eid = m["id"]
        if eid in seen_ids:
            return
        subj_l = (m.get("subject") or "").lower()
        if adm_only:
            if not any(k in subj_l for k in _ADM_3_KEYWORDS):
                return
        else:
            if not any(k in subj_l for k in ADM_KEYWORDS + MENDOTA_KEYWORDS):
                return
        seen_ids.add(eid)
        provider = "Mendota" if "mendota grain" in subj_l else "ADM"
        all_bid.append({
            "id":            m["id"],
            "subject":       m.get("subject", ""),
            "receivedAt":    m.get("receivedDateTime", ""),
            "sender":        m.get("from", {}).get("emailAddress", {}).get("address", ""),
            "hasAttachment": m.get("hasAttachments", False),
            "provider":      provider,
        })

    # ── Strategy 1: exact-subject $search (mailbox-wide, all folders) ──────────
    exact_subjects = [
        "ADM Decatur Cash Bids",
        "ADM Cedar Rapids Cash Bids",
        "ADM St Louis Cash Bids",
    ]
    if not adm_only:
        exact_subjects.append("ADM Mendota Grain Bids")

    for subj in exact_subjects:
        try:
            for m in _search_messages_paged(client_id, tenant_id, subj, max_results=2000):
                _add(m)
        except Exception:
            pass

    # ── Strategy 2: explicit folder scan (inbox + subfolders, year-by-year) ────
    # This catches emails that are beyond the search-index depth limit.
    folder_ids = ["inbox"] + _get_child_folder_ids(client_id, tenant_id, "inbox", depth=2)

    for year in range(start_year, current_year + 1):
        for fid in folder_ids:
            try:
                for m in _fetch_folder_year(client_id, tenant_id, fid, year):
                    _add(m)
            except Exception:
                pass

    all_bid.sort(key=lambda x: x["receivedAt"], reverse=True)
    year_counts = dict(Counter(e["receivedAt"][:4] for e in all_bid))
    return all_bid, year_counts


def fetch_all_bid_emails(client_id, tenant_id, max_results=500, adm_only=True):
    """
    Fetch ALL bid emails across the entire mailbox with pagination.
    adm_only=True  → only ADM Decatur / Cedar Rapids / St. Louis (skip Mendota PDF emails)
    adm_only=False → all bid emails (ADM + Mendota)
    Returns list sorted newest-first.
    """
    seen_ids = set()
    all_msgs = []
    for term in ["Cash Bids", "Grain Bids"]:
        for m in _search_messages_paged(client_id, tenant_id, term, max_results=max_results):
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                all_msgs.append(m)

    results = []
    for m in all_msgs:
        subj = (m.get("subject") or "").lower()
        if adm_only:
            if not any(k in subj for k in _ADM_3_KEYWORDS):
                continue
        else:
            if not any(k in subj for k in ADM_KEYWORDS + MENDOTA_KEYWORDS):
                continue
        provider = "Mendota" if "mendota grain" in subj else "ADM"
        results.append({
            "id":            m["id"],
            "subject":       m.get("subject", ""),
            "receivedAt":    m.get("receivedDateTime", ""),
            "sender":        m.get("from", {}).get("emailAddress", {}).get("address", ""),
            "hasAttachment": m.get("hasAttachments", False),
            "provider":      provider,
        })

    results.sort(key=lambda x: x["receivedAt"], reverse=True)
    return results


def fetch_bid_emails_debug(client_id, tenant_id, max_results=50):
    """Search mailbox for bid emails in any folder, any date."""
    seen_ids = set()
    all_raw = []
    for term in ["Cash Bids", "Grain Bids"]:
        for m in _search_messages(client_id, tenant_id, term, max_results=25):
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                all_raw.append({
                    "subject":       m.get("subject", ""),
                    "id":            m["id"],
                    "receivedAt":    m.get("receivedDateTime", ""),
                    "sender":        m.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "hasAttachment": m.get("hasAttachments", False),
                })
    bid_emails = []
    for m in all_raw:
        subj = m["subject"].lower()
        provider = "Mendota" if "mendota grain" in subj else "ADM"
        bid_emails.append({**m, "provider": provider})
    # sort newest first
    bid_emails.sort(key=lambda x: x["receivedAt"], reverse=True)
    return all_raw, bid_emails


def parse_email(client_id, tenant_id, email_id: str, provider: str):
    # Try PDF attachment first
    att_data = _get(client_id, tenant_id, f"/me/messages/{email_id}/attachments")
    pdf_bytes = None
    for att in att_data.get("value", []):
        if (att.get("name") or "").lower().endswith(".pdf"):
            pdf_bytes = _get_bytes(client_id, tenant_id,
                f"/me/messages/{email_id}/attachments/{att['id']}/$value")
            break

    html_body = None
    if not pdf_bytes:
        body_data = _get(client_id, tenant_id,
            f"/me/messages/{email_id}", {"$select": "body"})
        html_body = body_data.get("body", {}).get("content", "")

    meta = _get(client_id, tenant_id,
        f"/me/messages/{email_id}", {"$select": "subject,receivedDateTime"})
    subject  = meta.get("subject", "")
    received = meta.get("receivedDateTime", "")

    if provider == "Mendota":
        # Mendota locations dropped — skip importing these emails entirely.
        return ParsedSnapshot(
            emailId=email_id, emailSubject=subject, emailDate=received,
            provider="Mendota", snapshots=[], needsReview=False,
            parseError="Mendota import disabled.")
    return parse_adm_email(email_id, subject, received, pdf_bytes, html_body)
