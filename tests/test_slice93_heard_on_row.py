#!/usr/bin/env python3
"""Spec 093: the heard percentage on the row you are already looking at.

The question is whether a tracker is actually quiet or was just caught between packets, and the
answer is the heard histogram, which lived on a different page from the list where the question
gets asked. The row already says "quiet" against the silent threshold; the percentage says how the
node has behaved over the window, which is what tells a flat battery from a walk behind a hill.

Both pages render it through one helper, or an operator ends up with two figures for one node.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

web = read("src/mesh_manager/web.py") or ""


def fn(name, src=None):
    s = src if src is not None else web
    i = s.find(f"def {name}(")
    if i < 0:
        return ""
    j = s.find("\ndef ", i + 10)
    return s[i:j if j > 0 else len(s)]


row = fn("node_row")
check_true("the row renders at all", bool(row), "no node_row")

# ---- AC1, AC3 the figure, from the one helper -----------------------------------------------------
check_true("AC1 the row takes the availability it is to render",
           re.search(r"def node_row\([^)]*availability", row) is not None, row[:90])
check_true("AC3 and renders it through the shared helper, not its own markup",
           "_avcell(" in row or "_avword(" in row, "the row builds its own percentage")
# the guarantee is one builder behind both, not one function name used in both places: the row
# needs an inline span and the register page needs a cell, so they are different wrappers.
check_true("AC3 the register page still renders the figure", "_avcell(" in fn("register_rows"))
for w in ("_avword", "_avcell"):
    check_true(f"AC3 {w} builds from the shared parts rather than its own",
               "_avbits(" in fn(w), fn(w)[:120])

# ---- AC5 the quiet verdict survives -----------------------------------------------------------------
check_true("AC5 the quiet verdict is still there", "verdict warn" in row and "quiet" in row,
           "the percentage replaced the verdict instead of joining it")

# ---- AC4 nothing invented when nobody asked ----------------------------------------------------------
helper = fn("_avword")
check_true("AC4 the inline one has an empty case", re.search(r"if not a[\s:]", helper) is not None, helper[:160])
check_true("AC4 and so does the cell", re.search(r"if not a[\s:]", fn("_avcell")) is not None)
check("AC4 and neither claims 0% when there is no answer",
      re.findall(r"return[^\n]*\b0%", helper) + re.findall(r"return[^\n]*\b0%", fn("_avcell")), [])

# ---- AC6 database-only rows are not measured ------------------------------------------------------------
check_true("AC6 a database-only row carries no percentage",
           re.search(r"if not db|db is False|not db and", row) is not None
           or re.search(r"db\b[^\n]{0,40}availability|availability[^\n]{0,40}\bdb\b", row) is not None,
           "nothing distinguishes a db row for this cell")

# ---- AC7 the page asks for it and the fragment keeps it ----------------------------------------------------
tables = fn("nodes_tables")
check_true("AC7 nodes_tables takes availability", "availability" in tables, tables[:120])
# a call spanning two lines is not matched by a regex that stops at the first bracket, and the
# first caller, the one the fragment redraw uses, is exactly that shape.
_calls = []
for i in range(len(web)):
    if web.startswith("nodes_tables(", i) and not web.startswith("def nodes_tables(", max(0, i - 4)):
        _calls.append(web[i:i + 240].split("\n\n")[0])
check_true("AC7 there are callers to check", len(_calls) >= 3, str(len(_calls)))
_bare = [c.split("\n")[0][:70] for c in _calls if "availability" not in c]
check("AC7 every caller hands it over, the fragment redraw included", _bare, [])

finish()
