"""
facility_overrides.py — per-grain facility-type overrides.

`location_meta.facility_type` is one value per (provider, location), but a single
site can play different roles for different grains. ADM Beech Grove, for example,
is a rail terminal for corn/soybeans but a flour MILL for its wheat — so its SRW
wheat belongs in the "Wheat Mills" trend/change sections, not "Rail Terminals",
while its corn stays put.

The trend/change builders pick locations by base facility_type (grain-blind) and
only filter by grain later on the rows, so an override has to act in two places:
  • load()/_trend_load() must ADD an overridden location to the data pulled for
    its override facility_type (so its snapshots are available there), and
  • each builder must use effective_ftype() per (location, grain) to include it
    in the override category and exclude it from its base category.

Keys use the DISPLAY grain (what _grain_disp returns and TREND_CATEGORIES uses),
e.g. "Soft Red Winter (SRW)", not the raw snapshot grain "Wheat (Soft Red Winter)".
"""
from __future__ import annotations

# (provider, location, display_grain) -> facility_type to use for THAT grain only.
FACILITY_GRAIN_OVERRIDES: dict[tuple[str, str, str], str] = {
    # Kolten, 2026-07-21: ADM Beech Grove's wheat is milled here, not railed.
    ("ADM", "Beech Grove (Elevator)", "Soft Red Winter (SRW)"): "Wheat Milling",
}


def effective_ftype(provider: str, location: str, grain: str, base_ftype):
    """The facility_type a location acts as for a given grain (override or base)."""
    return FACILITY_GRAIN_OVERRIDES.get((provider, location, grain), base_ftype)


def override_pairs_for(facility_type: str) -> set[tuple[str, str]]:
    """Locations overridden INTO `facility_type` for some grain — their snapshots
    must be loaded under this type even though their base type differs."""
    return {(p, loc) for (p, loc, _g), ft in FACILITY_GRAIN_OVERRIDES.items()
            if ft == facility_type}
