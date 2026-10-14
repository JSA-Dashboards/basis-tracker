"""grain_units.py — the quote unit of grains whose basis is not cents per bushel.

`snapshot_rows.basis_cents` is always 100 x the number a source quotes (the scrapers do `round(float(basis) * 100)` for every crop): a corn
basis of -0.25 $/bu is stored -25, which the app shows as "-25¢". The same rule stores a number quoted in dollars per TON as hundredths of a
dollar (soybean meal: $3/ton = 300; the Bids and Summary tabs divide by 100 and say "$/t") and canola, which US crushers (ADM Velva and Enderlin,
CHS Hallock, Bunge Altona, Cargill West Fargo) quote in Canadian dollars per metric tonne against ICE canola futures: -45 C$/t is stored -4500.

Canola is put right where the rows are READ (database.get_snapshots / get_snapshots_bulk / get_snapshots_asof / get_map_data call `scale_basis`),
so every tab, chart and email sees -45 and only the unit label needed changing ("C$/t", see `unit_label`). The database keeps the x100 values
(the same convention as meal), nothing is migrated, and nothing ever writes a value read through these functions back. Soybean meal is left
exactly as it was: the app already divides it for display (`fmt_basis(is_meal=True)`, the Summary tab's `_mdiv`).
"""
from __future__ import annotations

CANOLA_UNIT = "C$/t"                    # Canadian dollars per metric tonne, vs ICE canola futures (also in C$/t)


def is_canola_seed(grain: str | None) -> bool:
    """True for the raw grain spellings of canola SEED ('Canola', 'Canola (Us)', 'Canola Usd/Cwt', 'Canola (Canadian)'), not canola MEAL
    ('Canola Meal Picked Up Marion', a $/ton product that is stored the way soybean meal is)."""
    g = (grain or "").strip().lower()
    return g.startswith("canola") and "meal" not in g


def scale_basis(grain: str | None, basis_cents):
    """The basis a reader should see: a canola row is stored x100 (hundredths of C$ per tonne) and comes back as the quoted whole number
    (-4500 -> -45). Everything else, and None, is returned as it was. The model wants an int, so a half-dollar quote (-4250) rounds away from
    zero (-43) — canola is quoted in whole dollars (steps of 5)."""
    if basis_cents is None or not is_canola_seed(grain):
        return basis_cents
    v = basis_cents / 100.0
    return int(v + 0.5) if v >= 0 else -int(-v + 0.5)


def unit_label(canonical_grain: str | None) -> str:
    """The unit to print beside a basis for a canonical display grain: 'C$/t' for Canola, '$/t' for Soybean Meal (stored x100, divided by the
    caller), '¢' for everything else."""
    g = (canonical_grain or "").strip().lower()
    if g == "canola":
        return CANOLA_UNIT
    if g == "soybean meal":
        return "$/t"
    return "¢"
