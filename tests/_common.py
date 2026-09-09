"""Shared check helpers for the suites: one shape, so output reads the same everywhere."""
import os
import sys

FAILURES = 0
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def check(name, got, want):
    global FAILURES
    ok = got == want
    if not ok:
        FAILURES += 1
    print(f"{'ok  ' if ok else 'FAIL'} {name:62s} got {got!r} want {want!r}")


def check_true(name, cond, detail=""):
    global FAILURES
    if not cond:
        FAILURES += 1
    print(f"{'ok  ' if cond else 'FAIL'} {name:62s} {detail}")


def skip(name, why):
    """A check that cannot run here, said out loud: never a silent pass, never a failure."""
    print(f"skip {name:64s} {why}")


def finish():
    print(f"\nFAILURES: {FAILURES}")
    sys.exit(1 if FAILURES else 0)


def read(path):
    p = os.path.join(ROOT, path)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def plus_side(patch_text):
    """The '+' side of a whole-file unified diff is the patched file (the whole-file-diff trick)."""
    lines = patch_text.split("\n")
    # skip any provenance header: the diff proper starts at the first '--- ' line
    start = next((i for i, ln in enumerate(lines) if ln.startswith("--- ")), 0)
    return "\n".join(ln[1:] for ln in lines[start + 3:] if ln.startswith("+"))
