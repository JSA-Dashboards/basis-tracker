"""
Helpers to derive state and a clean city label from ADM location names.

ADM names encode the state and a description, e.g.:
    "Decatur, IL (Soy Processing)"  → state "IL", city "Decatur"
    "Des Moines, IA"                → state "IA", city "Des Moines"
    "Green Bison Soy Processing"    → state "ND", city "Green Bison" (override)
"""
import re

# Locations whose state / city isn't cleanly encoded in the name
_STATE_OVERRIDES = {
    "Green Bison Soy Processing": "ND",   # ADM Spiritwood, ND
}
_CITY_OVERRIDES = {
    "Green Bison Soy Processing": "Green Bison",
}

_US_STATES = {
    "AL", "AR", "AZ", "CA", "CO", "CT", "DE", "FL", "GA", "IA", "ID", "IL",
    "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT",
    "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
}

# "City, ST" at the start of the name (ST = 2 uppercase letters)
_CITY_STATE_RE = re.compile(r'^\s*(.*?),\s*([A-Z]{2})\b')
_PARENS_RE     = re.compile(r'\s*\(.*?\)\s*$')


def adm_state_from_name(name: str):
    """Return the 2-letter state code encoded in an ADM location name, or None."""
    if name in _STATE_OVERRIDES:
        return _STATE_OVERRIDES[name]
    m = _CITY_STATE_RE.match(name or "")
    if m and m.group(2) in _US_STATES:
        return m.group(2)
    return None


def adm_city_from_name(name: str) -> str:
    """Return just the city label — no state, company, or description suffix."""
    if name in _CITY_OVERRIDES:
        return _CITY_OVERRIDES[name]
    m = _CITY_STATE_RE.match(name or "")
    if m:
        return m.group(1).strip()
    # No "City, ST" pattern — drop any trailing "(...)" description
    return _PARENS_RE.sub("", name or "").strip()
