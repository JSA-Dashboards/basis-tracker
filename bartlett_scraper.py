"""
Bartlett Grain Company cash-bid scraper.

Uses Barchart Connect's public GraphQL API (connect.api.barchart.com/graphql).
No authentication required — the API issues a temporary device token via
the RegisterDevice mutation which allows public cash-bid access.

Flow:
  1. RegisterDevice (no auth) → device token
  2. GetCashBids with X-DEVICE-TOKEN header → all bids (paginated)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_GQL_URL = "https://connect.api.barchart.com/graphql"
_COMPANY_ID = "MDE6Q29tcGFueToyNDky"  # Bartlett Grain Company, LLC (displayId=BRT)

_MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}
_MONTH_NAMES = {
    1: "January",  2: "February", 3: "March",    4: "April",
    5: "May",      6: "June",     7: "July",      8: "August",
    9: "September",10: "October", 11: "November", 12: "December",
}

# Maps Bartlett commodity names to (display grain name, CME futures root)
_COMMODITY_MAP: dict[str, tuple[str, str]] = {
    "Yellow Corn":              ("Corn",       "ZC"),
    "White Corn":               ("Corn",       "ZC"),
    "Yellow Soybeans":          ("Soybeans",   "ZS"),
    "Soybeans":                 ("Soybeans",   "ZS"),
    "Hard Red Winter Wheat":    ("Wheat HRW",  "ZW"),
    "Soft Red Winter Wheat":    ("Wheat SRW",  "ZW"),
    "Hard White Winter Wheat":  ("Wheat HWW",  "ZW"),
}

_REGISTER_MUTATION = (
    "mutation RegisterDevice($input: RegisterDeviceInput!) {"
    "  registerDevice(input: $input) { token } }"
)

_GET_BIDS_QUERY = """
query GetCashBids($companyId: ID!, $filter: CashBidFilter, $cursor: String) {
  node(id: $companyId) {
    ... on Company {
      cashBids(filter: $filter, sort: [{sortOrder: ASC}], first: 100, after: $cursor) {
        edges {
          node {
            id
            deliveryStart
            deliveryEnd
            futuresMonth
            basis(format: DECIMAL)
            commodity { name }
            location { name city state }
          }
        }
        pageInfo { endCursor hasNextPage }
      }
    }
  }
}
"""


def _register_device() -> str:
    resp = requests.post(
        _GQL_URL,
        json={
            "operationName": "RegisterDevice",
            "variables": {
                "input": {
                    "company": _COMPANY_ID,
                    "type": "DESKTOP",
                    "identifier": str(uuid.uuid4()),
                }
            },
            "query": _REGISTER_MUTATION,
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["data"]["registerDevice"]["token"]


def _futures_to_cme(futures_date: str, cme_root: str) -> str:
    """Convert '2026-07-01' + root 'ZC' -> 'ZCN26'."""
    parts = futures_date.split("-")
    year2 = parts[0][2:]
    month = int(parts[1])
    code = _MONTH_CODES.get(month, "?")
    return f"{cme_root}{code}{year2}"


def _delivery_label(delivery_start: str) -> str:
    """Convert '2026-06-01' -> 'June 2026'."""
    parts = delivery_start.split("-")
    month = int(parts[1])
    return f"{_MONTH_NAMES[month]} {parts[0]}"


def fetch_bartlett_bids() -> list[dict]:
    """
    Fetch all public Bartlett Grain cash bids.

    Returns a list of per-location dicts:
        {"location": str, "city": str, "state": str,
         "timestamp": str, "bids": [...]}
    """
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        device_token = _register_device()
    except Exception as exc:
        log.error("Bartlett: device registration failed: %s", exc)
        return []

    headers = {
        "Content-Type": "application/json",
        "X-DEVICE-TOKEN": device_token,
    }

    all_edges: list[dict] = []
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        variables: dict = {
            "companyId": _COMPANY_ID,
            "filter": {"location": {"id": {}}, "commodity": {"id": {}}},
        }
        if cursor:
            variables["cursor"] = cursor

        try:
            resp = requests.post(
                _GQL_URL,
                json={
                    "operationName": "GetCashBids",
                    "variables": variables,
                    "query": _GET_BIDS_QUERY,
                },
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            cb_data = resp.json()["data"]["node"]["cashBids"]
        except Exception as exc:
            log.error("Bartlett: GetCashBids page %d failed: %s", page, exc)
            break

        all_edges.extend(cb_data["edges"])
        if not cb_data["pageInfo"]["hasNextPage"]:
            break
        cursor = cb_data["pageInfo"]["endCursor"]

    if not all_edges:
        log.warning("Bartlett: no bids returned")
        return []

    loc_map: dict[str, dict] = {}
    seen: set[str] = set()

    for edge in all_edges:
        bid = edge["node"]
        commodity_name = bid["commodity"]["name"]
        futures_date = bid.get("futuresMonth")
        basis_dec = bid.get("basis")
        delivery_start = bid.get("deliveryStart", "")
        bid_id = bid.get("id", "")
        loc_name = bid["location"]["name"]

        if not futures_date or basis_dec is None or not delivery_start:
            continue

        mapped = _COMMODITY_MAP.get(commodity_name)
        if not mapped:
            continue  # skip Milo / unmapped commodities

        grain, cme_root = mapped
        cme_sym = _futures_to_cme(futures_date, cme_root)
        delivery = _delivery_label(delivery_start)

        if bid_id in seen:
            continue
        seen.add(bid_id)

        if loc_name not in loc_map:
            loc_map[loc_name] = {
                "location": loc_name,
                "city": bid["location"].get("city") or "",
                "state": bid["location"].get("state") or "",
                "timestamp": today_ts,
                "bids": [],
            }

        loc_map[loc_name]["bids"].append({
            "bid_id":      bid_id,
            "grain":       grain,
            "delivery":    delivery,
            "cme_symbol":  cme_sym,
            "basis_cents": round(basis_dec * 100),
        })

    result = list(loc_map.values())
    log.info("Bartlett: %d location(s), %d bid(s) total",
             len(result), sum(len(l["bids"]) for l in result))
    return result


if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sys.path.insert(0, str(Path(__file__).parent))
    from parsers.bartlett_parser import parse_bartlett_location

    locs = fetch_bartlett_bids()
    print("=" * 65)
    for loc in locs:
        snap = parse_bartlett_location(loc)
        if snap:
            print(f"  {snap.location:30s}  {snap.provider}  {len(snap.rows)} row(s)")
            for r in snap.rows:
                sign = "+" if (r.basisCents or 0) >= 0 else ""
                print(f"    {r.grain:20s}  {r.deliveryMonth:15s}  {r.futuresSymbol:7s}  {sign}{r.basisCents}c")
        else:
            print(f"  {loc['location']:30s}  (no valid bids)")
