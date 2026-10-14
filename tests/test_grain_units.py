"""grain_units: canola basis is stored x100 (hundredths of C$ per tonne) and read back as the quoted number; soybean meal and the rest are left alone.

    python tests/test_grain_units.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grain_units as gu          # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


print("grain_units: which raw grains are canola seed")
check("the four spellings the scrapers store are canola seed",
      all(gu.is_canola_seed(g) for g in ("Canola", "Canola (Us)", "Canola Usd/Cwt", "Canola (Canadian)")))
check("case and spaces do not matter", gu.is_canola_seed("  CANOLA  ") and gu.is_canola_seed("canola (us)"))
check("canola MEAL is not seed (it is a $/ton product, stored like soybean meal)",
      not gu.is_canola_seed("Canola Meal Picked Up Marion") and not gu.is_canola_seed("Canola Meal Picked Up Dd Ingredients"))
check("other grains, empty and None are not canola", not any(gu.is_canola_seed(g) for g in ("Corn", "Soybeans", "Soybean Meal", "Flaxseed", "Hioleic Sunflower Seed", "", None)))

print("grain_units: scale_basis")
check("a stored -4500 reads -45 (ADM Velva FH Oct) and +3000 reads +30", gu.scale_basis("Canola (Us)", -4500) == -45 and gu.scale_basis("Canola", 3000) == 30)
check("every canola spelling is scaled (CHS 'Canola Usd/Cwt' -6000 -> -60, the hidden Canadian ones too)",
      gu.scale_basis("Canola Usd/Cwt", -6000) == -60 and gu.scale_basis("Canola (Canadian)", -7700) == -77)
check("zero stays zero and None stays None", gu.scale_basis("Canola", 0) == 0 and gu.scale_basis("Canola", None) is None)
check("the result is an int (the row model wants one)", isinstance(gu.scale_basis("Canola", -4500), int) and isinstance(gu.scale_basis("Canola", 0), int))
check("a half-dollar quote rounds away from zero, both signs (-4250 -> -43, 4250 -> 43, -4249 -> -42)",
      gu.scale_basis("Canola", -4250) == -43 and gu.scale_basis("Canola", 4250) == 43 and gu.scale_basis("Canola", -4249) == -42)
check("corn, soybeans, wheat and meal come back exactly as stored (the app divides meal itself)",
      gu.scale_basis("Corn", -25) == -25 and gu.scale_basis("Soybeans", 1234) == 1234 and gu.scale_basis("Soybean Meal", 300) == 300 and gu.scale_basis("Wheat", None) is None)
check("canola meal is not touched", gu.scale_basis("Canola Meal Picked Up Marion", 3300) == 3300)

print("grain_units: unit_label")
check("Canola says C$/t, Soybean Meal $/t, the rest cents", gu.unit_label("Canola") == "C$/t" and gu.unit_label("Soybean Meal") == "$/t"
      and gu.unit_label("Corn") == "¢" and gu.unit_label("Wheat (HRW)") == "¢" and gu.unit_label(None) == "¢")

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
