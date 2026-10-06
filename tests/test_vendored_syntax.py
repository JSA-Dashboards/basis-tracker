"""The modules the portals vendor (sync_carry_modules.py) must also import on the Python 3.11 that rail-fob-portal's
environment.sis.yml pins. This repo runs 3.12+, where an f-string may hold a backslash, a comment, a line break or the SAME quote
character inside a replacement field (PEP 701) — all of which are a SyntaxError on 3.11. net_carry_compare.py shipped one such
f-string and would have cost the Snowflake-hosted portal its whole Net Carry tab; this keeps the vendored files clean.

    python tests/test_vendored_syntax.py        (checks nothing on Python < 3.12, where the file could not even import)
"""
import io
import os
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sync_carry_modules as sync          # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def pep701_only(path: str) -> list:
    """[(line, why)] for every f-string construct in the file that only Python 3.12+ accepts."""
    src = open(path, encoding="utf-8").read()
    bad, stack = [], []                 # stack of [quote, brace depth inside the replacement field]
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type == tokenize.FSTRING_START:
            q = t.string[-3:] if t.string[-3:] in ("'''", '"""') else t.string[-1]
            stack.append([q, 0])
        elif t.type == tokenize.FSTRING_END:
            stack.pop()
        elif stack:
            top = stack[-1]
            if t.type == tokenize.OP and t.string == "{":
                top[1] += 1
            elif t.type == tokenize.OP and t.string == "}":
                top[1] -= 1
            elif top[1] > 0:            # inside a replacement field
                if t.type == tokenize.STRING:
                    if "\\" in t.string:
                        bad.append((t.start[0], "a backslash in a string inside a replacement field: " + t.string[:40]))
                    if t.string.lstrip("rbfuRBFU")[:1] == top[0][:1] and len(top[0]) == 1:
                        bad.append((t.start[0], "the enclosing f-string's own quote inside a replacement field: " + t.string[:40]))
                elif t.type == tokenize.COMMENT:
                    bad.append((t.start[0], "a comment inside a replacement field"))
                elif t.type in (tokenize.NL, tokenize.NEWLINE) and len(top[0]) == 1:
                    bad.append((t.start[0], "a line break inside the replacement field of a single-quoted f-string"))
    return bad


if sys.version_info < (3, 12):
    print("Python %d.%d: PEP 701 f-strings do not parse here at all — nothing to check." % sys.version_info[:2])
    sys.exit(0)

print("vendored modules: no f-string construct that needs Python 3.12")
files = [f for f in sync.FILES + sync.RETURN_FILES + sync.RIVER_FILES if f.endswith(".py")]
for rel in files:
    path = os.path.join(ROOT, rel)
    check(rel, os.path.exists(path) and not pep701_only(path), pep701_only(path) if os.path.exists(path) else "missing")
check("this test file is itself clean", not pep701_only(__file__))
probe = os.path.join(HERE, "_probe_pep701.tmp.py")
try:
    open(probe, "w", encoding="utf-8").write("x = 1\ny = f'{\"a\\'b\"}'\n")
    check("...and flags a backslash inside a replacement field", len(pep701_only(probe)) == 1, pep701_only(probe))
finally:
    if os.path.exists(probe):
        os.remove(probe)

print("\n" + ("ALL PASS" if not FAILS else "FAILURES: %s" % FAILS))
sys.exit(1 if FAILS else 0)
