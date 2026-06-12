"""
One-time (or refresh) script to populate the location_meta table
for all CHS locations with state and facility_type.

Run after geocoding completes:
    python chs_populate_meta.py

State data comes from chs_geocoded.json (produced by the geocoding step
embedded in chs_scraper.py's background task, or via --list-ids).

Facility type is classified from the location name using keyword rules.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from database import init_db, upsert_location_meta


# ── Facility-type keyword rules ───────────────────────────────────────────────
# Applied in order; first match wins.

_SOY_CRUSH_NAMES = {
    "agp - hastings",    # American Grain Products soy crush, Hastings NE
    "agp aberdeen",      # AGP soy crush, Aberdeen SD
    "fairmont",          # CHS soy processing, Fairmont MN
    "mankato",           # CHS soy processing, Mankato MN
}

_ETHANOL_NAMES = {
    "absolute energy",      # Absolute Energy ethanol, Laddonia MO
    "adm clinton",          # ADM corn wet mill / ethanol, Clinton IA
    "annawan",              # Big River Resources ethanol, Annawan IL
    "bridgeport ethanol",   # ethanol, Bridgeport NE
    "corn inc",             # corn processing / ethanol, IL
    "delivered tharaldson", # Tharaldson Ethanol, Casselton ND
    "gpc muscatine",        # Grain Processing Corp corn wet mill, Muscatine IA
    "ingredion",            # corn starch / sweeteners, IN
    "marshall",             # ethanol, Marshall MN
    "rochelle",             # ethanol, Rochelle IL
}

_CORN_PROCESSING_NAMES = {
    "nestle davenport",     # Nestle food-grade grain processing, Davenport IA
}

_ETHANOL_KW = re.compile(r'\bethanol\b', re.I)

_RIVER_NAMES = {
    "boyle terminal",       # Mississippi River, MN
    "chs cahokia",          # Illinois/Mississippi confluence
    "collins, mississippi", # Mississippi River, MS
    "davenport",            # Mississippi River, IA
    "havana/beardstown",    # Illinois River, IL
    "joliet container mkt", # Illinois River, IL
    "morris",               # Illinois River, IL (or MN — user-confirmed river terminal)
    "ottawa river mkt",     # Illinois River, IL
    "peru marketstreet",    # Illinois River, IL
    "rt. 47 terminal",      # Illinois River, IL
    "savage",               # Minnesota River / Mississippi, MN
    "seneca",               # Illinois River, IL
    "kennewick/lomo",       # Columbia River, WA
    "macon",                # Illinois River area, MO
}

_RIVER_KW = re.compile(r'\briver\b', re.I)

_RAIL_NAMES = {
    "crookston terminal",
    "kindred terminal",
    "lemmon terminal",
    "chs primeland",        # shuttle loader network in Pacific NW
    "connell elevator",     # rail shuttle, WA
}

_RAIL_KW = re.compile(r'\bshuttle\b', re.I)


def classify_type(name: str) -> str:
    """
    Classify a CHS location into one of:
      Soy Crush | Ethanol | Corn Processing | River Terminal | Rail Terminal | Country Elevator
    """
    nl = name.lower().strip()

    if nl in _SOY_CRUSH_NAMES:
        return "Soy Crush"

    if nl in _ETHANOL_NAMES or _ETHANOL_KW.search(name):
        return "Ethanol"

    if nl in _CORN_PROCESSING_NAMES:
        return "Corn Processing"

    if nl in _RIVER_NAMES or _RIVER_KW.search(name):
        return "River Terminal"

    if nl in _RAIL_NAMES or _RAIL_KW.search(name):
        return "Rail Terminal"

    return "Country Elevator"


# ── Manual state corrections ──────────────────────────────────────────────────
# Nominatim picks the most-populated city of that name, which is often wrong
# for small Midwest grain towns.  Corrections verified against CHS elevator lists.
STATE_CORRECTIONS: dict[str, str] = {
    # IA / IL swaps
    "ADM Clinton":            "IA",   # Clinton, Iowa (ADM corn wet mill)
    "Carrollton/White Hal":   "IL",   # Carrollton & White Hall, IL
    "Newark":                 "IL",   # Newark, IL (from CHS-Illinois page)
    "Ottawa River Mkt":       "IL",   # Ottawa, IL — Illinois River terminal
    "Meredith Rd Elburn":     "IL",   # Elburn, IL
    "Corn Inc":               "IL",   # Corn Products / Illinois
    "Glasgow":                "MT",   # Glasgow, MT (not IL)
    "Glenwood":               "MN",   # Glenwood, MN (not IL)
    "Malta":                  "IL",   # Malta, IL (CHS-Illinois page)
    "Marshall":               "MN",   # Marshall, MN (not IL)
    "New Salem":              "ND",   # New Salem, ND (not IL)
    "Shelby":                 "MT",   # Shelby, MT (not IL)
    "Smithfield":             "NE",   # Smithfield, NE (not IL)
    "Strasburg":              "ND",   # Strasburg, ND (not IL)
    "Warren":                 "MN",   # Warren, MN (not IL)
    # MN corrections
    "Ada":                    "MN",   # Ada, MN (not OH)
    "Greenbush":              "MN",   # Greenbush, MN (not MI)
    "Herman":                 "MN",   # Herman, MN (not NE)
    "Lowry":                  "MN",   # Lowry, MN (not SD)
    "Magnolia":               "MN",   # Magnolia, MN (not IA)
    "Morris":                 "MN",   # Morris, MN (not KS) — both duplicates
    # ND corrections
    "Canton":                 "SD",   # Canton, SD (not OH)
    "Davidson":               "ND",   # Davidson, ND
    "Garrison":               "ND",   # Garrison, ND (not IA)
    "Lakota":                 "ND",   # Lakota, ND (not IA)
    "Milton":                 "ND",   # Milton, ND (not WA)
    "Napoleon":               "ND",   # Napoleon, ND (not OH)
    "New Haven":              "ND",   # New Haven, ND (not MI)
    "Norma":                  "ND",   # Norma, ND (not WI)
    "Selkirk":                "ND",   # Selkirk, ND (not MI)
    "Underwood (13¢ Dump fee)": "ND",  # Underwood, ND
    # SD corrections
    "Akron":                  "CO",   # Akron, CO (CHS grain elevator there)
    "Brandon":                "SD",   # Brandon, SD (not MS)
    "Bruce Elevator":         "SD",   # Bruce, SD (not MS)
    "Elkton":                 "SD",   # Elkton, SD (not MD)
    "Fairfax":                "SD",   # Fairfax, SD (not IA)
    "Isabel":                 "SD",   # Isabel, SD (not KS)
    "Mitchell":               "SD",   # Mitchell, SD (not IA)
    "Wilmot":                 "SD",   # Wilmot, SD (not OH)
    # NE corrections
    "Alma":                   "NE",   # Alma, NE (not KS)
    "Amherst":                "NE",   # Amherst, NE (not MA)
    "Bertrand":               "NE",   # Bertrand, NE (not MO)
    "Callaway":               "NE",   # Callaway, NE (not MO)
    "Loomis":                 "NE",   # Loomis, NE (not WA)
    "Merritt Bunker":         "NE",   # Merritt, NE (not MO)
    "Wheeler Elevator ML":    "NE",   # Wheeler, NE (not TX)
    # CO corrections
    "Byers":                  "CO",   # Byers, CO (not KS)
    "Fleming":                "CO",   # Fleming, CO
    "Sterling":               "CO",   # Sterling, CO
    "Stateline":              "CO",   # Stateline CO/KS border
    # KS corrections
    "County Line":            "SD",   # County Line, SD (CHS elevator)
    "Otis":                   "CO",   # Otis, CO (not KS)
    "Wallace":                "KS",   # Wallace, KS — correct as-is
    # MO
    "Macon":                  "MO",   # Macon, MO (not IL)
    # River terminals misidentified
    "Boyle Terminal":         "MN",   # Boyle Terminal, Winona area, MN — Mississippi River
    "Quincy Elevator":        "IL",   # Quincy, IL — Mississippi River
    # Unknown → known
    "Absolute Energy":        "MO",   # Absolute Energy ethanol, Laddonia, MO
    "Bridgeport Ethanol":     "NE",   # Bridgeport, NE (not CT)
    "Delivered Tharaldson":   "ND",   # Tharaldson Ethanol, Casselton, ND
    "Nestle Davenport":       "IA",   # Nestle, Davenport, IA
    "CHS Primeland":          "WA",   # CHS Primeland shuttle loaders, WA
    "Kershaw":                "MN",   # Kershaw, MN (small elevator)
}


# ── River terminal location names ─────────────────────────────────────────────
# Supplement keyword detection for specific locations
_EXTRA_RIVER = {
    "boyle terminal",    # Mississippi River, MN
    "quincy elevator",   # Mississippi River, IL
    "savage",            # Minnesota River / Mn, MN
    "collins, mississippi",
}

# Update river check to include extra names
_orig_river_names = _RIVER_NAMES.copy()
_RIVER_NAMES.update(_EXTRA_RIVER)


def run(geocoded_path: str = "chs_geocoded.json"):
    init_db()

    geo_file = Path(geocoded_path)
    if not geo_file.exists():
        print(f"ERROR: {geocoded_path} not found.")
        print("Run the geocoding step first (it runs automatically with chs_scraper.py).")
        sys.exit(1)

    with open(geo_file, encoding="utf-8") as f:
        geocoded: dict[str, dict] = json.load(f)

    print(f"Populating location_meta for {len(geocoded)} CHS locations…")
    for loc_name, geo in geocoded.items():
        # Apply manual correction if available, else use geocoded result
        state = STATE_CORRECTIONS.get(loc_name) or geo.get("state") or ""
        if state in ("?", "N/A"):
            state = ""
        facility_type = classify_type(loc_name)
        upsert_location_meta("CHS", loc_name, state or None, facility_type)

    print("Done.")

    # Quick summary
    from database import get_location_meta
    meta = get_location_meta("CHS")
    states = sorted({v["state"] for v in meta.values() if v["state"] and v["state"] != "N/A"})
    types  = sorted({v["facility_type"] for v in meta.values() if v["facility_type"]})
    print(f"\nStates ({len(states)}): {', '.join(states)}")
    print(f"Types:  {', '.join(types)}")

    unknown = [k for k, v in meta.items() if not v["state"] or v["state"] == "?"]
    if unknown:
        print(f"\n{len(unknown)} locations with unknown state:")
        for u in sorted(unknown):
            print(f"  {u}")


if __name__ == "__main__":
    run()
