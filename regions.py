"""
Region assignment by the Mississippi River divide.

States (and Canadian provinces) west of the Mississippi River → "West";
those east of it → "East". The river forms the border for several states, so
each is classified by which side the bulk of the state sits on:
  IL, WI, TN, MS (state), KY  → East   (river is their western border)
  IA, MO, MN, AR, LA          → West   (river is their eastern border)
"""

_WEST = {
    # Plains / Midwest west of the river
    "IA", "MO", "MN", "NE", "KS", "SD", "ND", "OK", "TX", "AR", "LA",
    # Mountain / West coast
    "CO", "MT", "WY", "NM", "AZ", "NV", "UT", "ID", "WA", "OR", "CA", "AK", "HI",
    # Canadian prairies / west
    "MB", "SK", "AB", "BC",
}

_EAST = {
    # Eastern Corn Belt / Great Lakes
    "IL", "IN", "OH", "MI", "WI", "KY", "TN",
    # South-east (state of Mississippi sits east of the river)
    "MS", "AL", "GA", "FL", "SC", "NC", "VA", "WV",
    # North-east
    "PA", "NY", "NJ", "DE", "MD", "DC", "CT", "RI", "MA", "VT", "NH", "ME",
    # Canadian east
    "ON", "QC",
}


def region_from_state(state):
    """Return 'West' or 'East' for a 2-letter state/province code, else None."""
    if not state:
        return None
    s = state.strip().upper()
    if s in _WEST:
        return "West"
    if s in _EAST:
        return "East"
    return None
