"""sync_carry_modules.py — vendor the Net Carry modules into a sibling portal repo.

The Net Carry logic lives HERE (the basis tracker) and is copied, unchanged, into the portals that show it
(rail-fob-portal, river-fob-portal) — the same copy-and-keep-in-sync pattern those repos already use for
delivery_period.py and rail_corridors.py. Edit the module in this repo first, then re-run this.

    python sync_carry_modules.py ../rail-fob-portal                    # copy (overwrites the vendored copies)
    python sync_carry_modules.py ../river-fob-portal --check           # list which vendored files differ; changes nothing
    python sync_carry_modules.py ../river-fob-portal --river           # also river_carry.py (the FOB-sheet archive -> carry inputs)
    python sync_carry_modules.py ../rail-fob-portal --no-return        # the Net Carry modules only (no Return to Carry)

What it copies (flat, next to the portal's app.py, because the modules import each other by bare name):
    net_carry.py          the carry ladder: basis re-expressed vs one futures contract, interest, net, top of net carry
    net_carry_chart.py    the "Cash Fwd Curve" chart (Altair)
    net_carry_compare.py  several locations / corridors side by side
    carry_rate.py         the interest rate: effective fed funds + 2.25% (FRED, with the committed snapshot below)
    delivery_period.py    delivery-label normalisation ('FH Dec', 'JFM', 'Dec 2026' -> a month)
    data/fed_funds_dff.csv    offline snapshot of the fed funds history carry_rate falls back to
    tests/test_net_carry.py, tests/test_net_carry_chart.py    the logic's own checks (pure; run them in the portal too)
and, unless --no-return, the Return to Carry history (return_to_carry*.py + its data files + checks):
    return_to_carry.py / _data.py / _view.py   the engine, the adapters from archive rows, the tables and charts
    return_to_carry_block.py                   the whole section as one Streamlit block: block.render(obs=..., quotes=..., ...)
    data/prime_rate.csv, data/rtc_futures_*.csv   the bank prime snapshot and the analyst-sheet futures before the settlement archive
with --river, river_carry.py: the River FOB sheet archive (cif / freight / calendar history) read as Net Carry curves, the weekly
nearby FOB and the forward quotes (it needs the portal's own fob_model.py).
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = [
    "net_carry.py", "net_carry_chart.py", "net_carry_compare.py", "carry_rate.py", "delivery_period.py",
    "data/fed_funds_dff.csv",
    "tests/test_net_carry.py", "tests/test_net_carry_chart.py",
]
RETURN_FILES = [
    "return_to_carry.py", "return_to_carry_data.py", "return_to_carry_view.py", "return_to_carry_block.py",
    "data/prime_rate.csv", "data/rtc_futures_1996_2006.csv", "data/rtc_futures_soy_2005_2007.csv",
    "tests/test_return_to_carry.py", "tests/test_return_to_carry_view.py", "tests/test_return_to_carry_ship.py",
    "tests/test_return_to_carry_soy.py", "tests/test_return_to_carry_block.py",
    "tests/fixtures/rtc_soy_sheets.json", "tests/fixtures/rtc_sheets.json",
]
RIVER_FILES = ["river_carry.py", "tests/test_river_carry.py"]


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    check = "--check" in argv
    if len(args) != 1:
        print(__doc__)
        return 2
    dest = Path(args[0]).resolve()
    if not dest.is_dir():
        print(f"not a directory: {dest}", file=sys.stderr)
        return 2
    if dest == HERE:
        print("destination is this repo", file=sys.stderr)
        return 2
    files = FILES + ([] if "--no-return" in argv else RETURN_FILES) + (RIVER_FILES if "--river" in argv else [])
    differ = 0
    for rel in files:
        src, dst = HERE / rel, dest / rel
        if not src.exists():
            print(f"  MISSING in source: {rel}", file=sys.stderr)
            return 1
        same = dst.exists() and filecmp.cmp(src, dst, shallow=False)
        state = "same" if same else ("differs" if dst.exists() else "absent")
        if not same:
            differ += 1
        if check:
            print(f"  {state:8} {rel}")
        elif not same:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            print(f"  copied   {rel}  ({state})")
        else:
            print(f"  same     {rel}")
    print(("%d of %d files differ" if check else "%d of %d files written") % (differ, len(files)))
    return 1 if (check and differ) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
